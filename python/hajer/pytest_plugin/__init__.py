"""Collect committed suites in customer CI. Deterministic non-passes fail; model judgments advise only.

The pytest11 entry point loads this plugin into every pytest of an environment with the SDK installed, so it is inert
unless `--hajer-suites` asks for a Hajer run (as the GitHub Action's `run-suites.sh` does): a customer's ordinary `pytest` collects no
Hajer case, keeps its own outcomes and exit status, and writes or uploads nothing.

The job's outcome is read from the recorded checks, not only from pytest's item outcomes, so the customer's pytest
configuration cannot turn a Hajer case green: a re-run plugin cannot run a case again (which would also spend more
than the qualification rule's five live attempts), an xfail marker cannot turn a failed case into an expected failure, and a
hook that resets the exit status runs before this plugin's own `pytest_sessionfinish`. Never green on
UNKNOWN.
"""

import json
from collections import Counter
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import JsonValue

from hajer._ci_spend import Spend
from hajer._settings import HajerSettings
from hajer.pytest_plugin._adapters import adapter_documents, adapter_status, case_adapter, neutral
from hajer.pytest_plugin._fixed_repeats import FixedRepeats, complete_collection, start_collection
from hajer.pytest_plugin._judge import remote_judge
from hajer.pytest_plugin._load import load_suite
from hajer.pytest_plugin._models import Case, State, Suite
from hajer.pytest_plugin._policy import disposition
from hajer.pytest_plugin._private_inputs import hydrate
from hajer.pytest_plugin._run import run
from hajer.pytest_plugin._upload import upload

__all__ = [
    "pytest_addoption",
    "pytest_collect_file",
    "pytest_collection_modifyitems",
    "pytest_configure",
    "pytest_runtest_makereport",
    "pytest_sessionfinish",
    "pytest_sessionstart",
]
_STATE = pytest.StashKey[State]()
_FIXED = pytest.StashKey[FixedRepeats | None]()
_MODE_NOTES = {
    "replay": (
        "Each verdict is one read of the app on fixed recorded answers; labels stay not_yet, so a replay PASS is not "
        "a qualified PASS."
    ),
    "live": "Each verdict is the one its label settled on after at most five live attempts; model judgments advise only.",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("hajer")
    group.addoption("--hajer-suites", default=None, help="Run Hajer's suites from a directory or exact JSON file")
    group.addoption(
        "--hajer-live", action="store_true", help="Ordinary live qualification, at most five attempts per case"
    )
    group.addoption(
        "--hajer-fixed-repeats",
        type=int,
        default=0,
        help="Opt in to N additional fresh measurements per case after ordinary qualification; requires explicit CI budget",
    )
    group.addoption("--hajer-results", default=".hajer/results.json", help="Compact local result receipt")
    group.addoption("--hajer-upload", action="store_true", help="Upload through the SDK transport")
    group.addoption(
        "--hajer-temporary-local-executions",
        default=None,
        help="Private temporary local proof inputs (unscanned; never a publication clearance)",
    )
    group.addoption(
        "--hajer-private-inputs",
        action="store_true",
        help="Fetch approved traffic inputs for live CI; never write a repository binding",
    )
    group.addoption("--hajer-project-id", default=None, help="Hajer project UUID for upload")
    group.addoption("--hajer-summary", default=None, help="Append Markdown summary to this file")
    group.addoption(
        "--hajer-adapter-checks", default=None, help="Legacy report path; adapters are always verified in this session"
    )


def pytest_configure(config: pytest.Config) -> None:
    state = State()
    config.stash[_STATE] = state
    config.stash[_FIXED] = None


def _directory(config: pytest.Config) -> Path | None:
    """The explicit suite directory or file, or None when the plugin is inert."""
    option = cast(str | None, config.getoption("hajer_suites"))
    return Path(option).resolve() if option else None


def _files(selection: Path) -> tuple[Path, ...]:
    return (
        (selection,) if selection.is_file() and selection.suffix == ".json" else tuple(sorted(selection.glob("*.json")))
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    directory = _directory(session.config)
    if directory is None:
        return
    config = session.config
    if hasattr(config, "workerinput") or config.getoption("numprocesses", default=None) not in (None, 0):
        raise pytest.UsageError(
            "Hajer suites run in one pytest process: pytest-xdist (-n) would split one receipt across workers; "
            "run without -n or with -p no:xdist"
        )
    if not directory.is_dir() and not (directory.is_file() and directory.suffix == ".json"):
        raise pytest.UsageError("The Hajer suite directory or JSON file does not exist")
    if not _files(directory):
        raise pytest.UsageError("The Hajer suite directory has no suite files")
    config.stash[_FIXED] = start_collection(config, config.stash[_STATE].run_id)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session: pytest.Session, items: list[pytest.Item]) -> None:
    state = session.config.stash[_STATE]
    directory = _directory(session.config)
    if directory is None or state.suites:
        return
    for path in _files(directory):
        # Pytest's public factory has untyped **kw; this boundary supplies its documented arguments.
        collector = SuiteFile.from_parent(session, path=path)  # pyright: ignore[reportUnknownMemberType]
        items.extend(collector.collect())


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> "SuiteFile | None":
    directory = _directory(parent.config)
    if (
        directory is not None
        and file_path.suffix == ".json"
        and (file_path == directory or file_path.parent == directory)
    ):
        return SuiteFile.from_parent(parent, path=file_path)  # pyright: ignore[reportUnknownMemberType]
    return None


def _attempts(check: dict[str, JsonValue]) -> str:
    return ", ".join(str(attempt) for attempt in cast(list[JsonValue], check["attempts"]))


class SuiteFile(pytest.File):
    def collect(self) -> Iterator["CaseItem"]:
        temporary = cast(str | None, self.config.getoption("hajer_temporary_local_executions"))
        if temporary is not None and self.config.getoption("hajer_upload"):
            raise pytest.UsageError("Temporary local proof execution cannot upload")
        suite = load_suite(self.path, temporary=None if temporary is None else Path(temporary))
        if self.config.getoption("hajer_private_inputs"):
            if temporary is not None or not self.config.getoption("hajer_live"):
                raise pytest.UsageError("Private CI inputs require live execution and cannot use temporary proof mode")
            project = cast(str | None, self.config.getoption("hajer_project_id"))
            hydrate(suite, self.path, project, HajerSettings.from_env())
        if (fixed := self.config.stash[_FIXED]) is not None:
            try:
                fixed.register(suite)
            except ValueError:
                raise pytest.UsageError("Fixed-repeat suite identities are missing or invalid") from None
        self.config.stash[_STATE].suites.append(suite)
        for case in suite.cases:
            item = CaseItem.from_parent(self, name=case.case_id)  # pyright: ignore[reportUnknownMemberType]
            item.suite, item.case = suite, case
            yield item


class CaseItem(pytest.Item):
    suite: Suite
    case: Case

    def runtest(self) -> None:
        state = self.config.stash[_STATE]
        key = (self.suite.suite_id, self.suite.version, self.case.case_id)
        if key in state.executed:
            state.refused_reruns += 1
            pytest.fail(
                "Hajer runs each case once per session; a re-run cannot replace its first result or spend more attempts",
                pytrace=False,
            )
        state.executed.add(key)
        settings = HajerSettings.from_env()
        checked = adapter_status(state, self.suite, self.case, settings)
        if checked is not None and checked[0] != "VERIFIED":
            self.suite.results.append(neutral(self.case, *checked))
            pytest.skip(f"ADAPTER_UNVERIFIED ({checked[0]}): {checked[1]}")
        live = bool(self.config.getoption("hajer_live"))
        # A judge check is judged live on Hajer's JUDGE route (the team key and project); replay never judges.
        project = self.config.getoption("hajer_project_id")
        judge = (
            remote_judge(settings, str(project) if project else None, state.run_id, suite_id=self.suite.suite_id)
            if live
            else None
        )
        result = run(self.suite, self.case, live=live, settings=settings, judge=judge)
        if (self.suite.temporary or self.suite.private_inputs) and checked is not None:
            # Temporary mode checks each case with its own input: its own check, not only the session's last one.
            result["adapterCheck"] = case_adapter(self.suite, self.case, checked)
        self.suite.results.append(result)
        # A SHADOW check's non-PASS is reported in the receipt and summary, never a failed test.
        checks = [
            check
            for check in cast(list[dict[str, JsonValue]], result["checks"])
            if check["checkId"] not in self.suite.shadow
        ]
        decision = disposition(self.suite, result)
        if decision == "FAIL":
            pytest.fail(
                "; ".join(
                    f"{check['checkId']}: {check['verdict']} ({check['label']}, {check['reason']}; "
                    f"attempts {_attempts(check)})"
                    for check in checks
                ),
                pytrace=False,
            )
        if decision == "NEUTRAL":
            pytest.skip("Advisory or unverified evidence; see retained check verdicts and attempts")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """An xfail marker on a Hajer case is ignored: its failure stays a failure and its pass a plain pass."""
    report = yield
    if isinstance(item, CaseItem) and hasattr(report, "wasxfail"):
        delattr(report, "wasxfail")
        neutral_result = any(
            result.get("caseId") == item.case.case_id and disposition(item.suite, result) == "NEUTRAL"
            for result in item.suite.results
        )
        if not (report.outcome == "skipped" and neutral_result):
            report.outcome = "passed" if report.outcome == "passed" else "failed"
    return report


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    state = session.config.stash[_STATE]
    if not state.suites:
        return
    fixed = session.config.stash[_FIXED]
    for suite in state.suites:
        executed = {result["caseId"] for result in suite.results if isinstance(result["caseId"], str)}
        for case in suite.cases:
            if case.case_id in executed:
                continue
            checks: list[JsonValue] = []
            for check in case.checks:
                identity = cast(dict[str, JsonValue], check["check"])
                checks.append(
                    {
                        "checkId": identity["id"],
                        "verdict": "UNABLE_TO_VERIFY",
                        "label": "not_yet",
                        "attempts": ["UNABLE_TO_VERIFY"],
                        "reason": "NOT_EXECUTED",
                    }
                )
            suite.results.append({"caseId": case.case_id, "durationMs": 0, "checks": checks})
    # Retain every declared case. Adapter failures carry UNKNOWN checks; omitting them
    # makes a dashboard lose the entire suite when none of its adapters can start.
    # The local skip marker is a pytest disposition, not a claim that checks passed.
    uploaded = [
        (
            suite,
            [
                {key: value for key, value in case.items() if key not in {"skipped", "adapter"}}
                if case.get("skipped") == "ADAPTER_UNVERIFIED"
                else case
                for case in suite.results
            ],
        )
        for suite in state.suites
    ]
    suites: list[JsonValue] = [
        {"suiteId": suite.suite_id, "suiteVersion": suite.version, "cases": cast(list[JsonValue], cases)}
        for suite, cases in uploaded
        if cases
    ]
    settings = HajerSettings.from_env()
    payload: dict[str, JsonValue] = {
        "receiptId": str(UUID(state.run_id)),
        "commitSha": settings.ci_commit_sha,
        "branch": settings.ci_branch,
        "mode": "live" if session.config.getoption("hajer_live") else "replay",
        "suites": suites,
        "adapters": adapter_documents(state),
    }
    if session.config.getoption("hajer_live"):
        payload["spend"] = dict(Spend(settings).snapshot())
    if fixed is not None:
        payload["fixedRepeatCount"] = fixed.count
    result_path = Path(str(session.config.getoption("hajer_results")))
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload, indent=2) + "\n")
    if fixed is not None:
        try:
            complete_collection(fixed, session.exitstatus)
        finally:
            payload["spend"] = dict(Spend(settings).snapshot())
            payload["repeatMeasurements"] = fixed.documents()
            result_path.write_text(json.dumps(payload, indent=2) + "\n")
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    count = sum(len(suite.results) for suite in state.suites)
    if terminal is not None:
        terminal.write_line(f"{count} hajer case(s); mode={payload['mode']}; results: {result_path}")
        if fixed is not None:
            terminal.write_line(fixed.summary())
            terminal.write_line(f"Fixed-repeat measurement files: {fixed.root}; independent of CI blocking policy")
        live_only = sum(case.get("skipped") == "NEEDS_LIVE_RUN" for suite in state.suites for case in suite.results)
        if live_only:
            terminal.write_line(f"{live_only} situation-derived case(s) skipped in replay: NEEDS_LIVE_RUN")
        unverified_adapters = sum(
            case.get("skipped") == "ADAPTER_UNVERIFIED" for suite in state.suites for case in suite.results
        )
        if unverified_adapters:
            terminal.write_line(
                f"{unverified_adapters} case(s) neutral: their adapter is not VERIFIED (ADAPTER_UNVERIFIED)"
            )
        # Judge and metamorphic checks need a live run; a replay lists them apart, never as a pass.
        waiting = Counter(
            str(item.get("skipped"))
            for suite in state.suites
            for case in suite.results
            for item in cast(list[dict[str, JsonValue]], case.get("liveOnly") or [])
        )
        for reason, count in sorted(waiting.items()):
            terminal.write_line(f"{count} check reading(s) not run in replay: {reason}")
    unverified = sum(disposition(suite, case) == "FAIL" for suite in state.suites for case in suite.results)
    advisory = sum(disposition(suite, case) == "NEUTRAL" for suite in state.suites for case in suite.results)
    if advisory and terminal is not None:
        terminal.write_line(f"Hajer: {advisory} case(s) neutral/advisory; original verdicts retained, not PASS")
    if not any(suite.results for suite in state.suites) and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.NO_TESTS_COLLECTED
    shadowed = sum(
        check["verdict"] != "PASS" and check["checkId"] in suite.shadow
        for suite in state.suites
        for case in suite.results
        for check in cast(list[dict[str, JsonValue]], case["checks"])
    )
    if shadowed and terminal is not None:
        terminal.write_line(f"Hajer: {shadowed} SHADOW check reading(s) not PASS; reported, never failing the job")
    if unverified or state.refused_reruns:
        # Deterministic non-passes and execution failures still fail after pytest rerun/xfail/exit-status hooks.
        if session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        if terminal is not None:
            terminal.write_line(
                f"Hajer: {unverified} check(s) not PASS, {state.refused_reruns} re-run(s) refused; the job fails"
            )
    summary = session.config.getoption("hajer_summary")
    if summary:
        with Path(str(summary)).open("a") as handle:
            handle.write(f"\n**Hajer suites, mode={payload['mode']}.** {_MODE_NOTES[str(payload['mode'])]}\n")
            handle.write(f"\n{advisory} case(s) NEUTRAL/advisory; {unverified} case(s) failed CI policy.\n")
            if fixed is not None:
                handle.write(f"\n{fixed.summary()} No population reliability claim.\n")
            if "spend" in payload:
                handle.write(
                    f"\nProvider spend (local list-price estimate; unresolved reservations are not zero): `{json.dumps(payload['spend'], sort_keys=True)}`.\n"
                )
            handle.write(
                "\n| Suite | Case | Check | Verdict | Label | Attempts | CI policy |\n|---|---|---|---|---|---|---|\n"
            )
            for suite in state.suites:
                for case in suite.results:
                    for check in cast(list[dict[str, JsonValue]], case["checks"]):
                        values = [
                            suite.suite_id,
                            case["caseId"],
                            check["checkId"],
                            check["verdict"],
                            check["label"],
                            _attempts(check),
                            disposition(suite, case),
                        ]
                        handle.write(
                            "| "
                            + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in values)
                            + " |\n"
                        )
    if session.config.getoption("hajer_upload"):
        project_id = session.config.getoption("hajer_project_id")
        if not project_id or not upload(payload, str(project_id), HajerSettings.from_env()):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            if terminal is not None:
                terminal.write_line("Hajer result upload unavailable; local receipt retained")

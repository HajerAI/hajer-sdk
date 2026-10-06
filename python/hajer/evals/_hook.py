"""`before_all` — the engine's `beforeAll` hook: classify every test's `metadata.hajer`, filter the suite, say what it saw.

Why a hook, and why this one: promptfoo runs `beforeAll` once, in a fresh Python process of its own, after the suite
is resolved and before the first provider call. That is the one moment a malformed `metadata.hajer` can stop a run
before it costs anything, and the one place a `--workflow` / `--obligation` filter can shrink the suite the engine
actually runs rather than discard results afterwards. It is also why this module may raise: the process is the
engine's, never the customer's application, and "Extension hook beforeAll failed" with the test named is the point.

Everything else here is non-raising on purpose. A context without the shape promptfoo documents is returned
untouched rather than argued with; the report is best-effort (a path that cannot be written is ignored, and the
raise for errors still happens); a plain promptfoo test with no `hajer` object passes through unchanged. The whole
context comes back, with only `suite.tests` replaced, because the engine reads every mutable suite property off
the returned object — a hook that returned `{"suite": {"tests": [...]}}` alone would erase the prompts.

The report is how the parent `hajer eval` process learns what happened inside the engine's: the hook's own stderr
is the engine's to swallow, so it writes `HAJER_EVAL_HOOK_REPORT` and the CLI reads it back once the engine exits
(`read_report`, then `format_warnings` for the terminal).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hajer._errors import EvalMetadataError
from hajer._json import JsonObject, JsonValue
from hajer._settings import HajerSettings
from hajer.evals._metadata import (
    E_OBLIGATION_UNDECLARED,
    HAJER_METADATA_KEY,
    METADATA_KEY,
    Correlation,
    TestClassification,
    classify_test,
    merged_hajer_metadata,
    reference,
)

#: The report's own schema version. The reader is this same package, so it moves only when the shape does.
HOOK_REPORT_SCHEMA_VERSION: Final[int] = 1
#: What every terminal line the CLI prints from a report starts with.
WARNING_PREFIX: Final = "hajer eval: warning"

SUITE_KEY: Final = "suite"
TESTS_KEY: Final = "tests"
DEFAULT_TEST_KEY: Final = "defaultTest"


@dataclass(frozen=True, slots=True)
class ReportTest:
    """One test as the hook saw it — enough for the CLI to name it and for a reader to see what it was bound to.

    The report rows are dataclasses with a hand-written `to_wire`, not pydantic models dumped by alias, so that every
    wire key is spelled once, here, next to the attribute it carries: the reader (`format_warnings`) is this same
    package and looks keys up by those spellings.
    """

    index: int
    label: str
    test_case_id: str | None
    correlation: Correlation
    workflow_id: str | None
    obligation_ids: tuple[str, ...] | None
    component_ids: tuple[str, ...] | None
    warnings: tuple[str, ...]

    def to_wire(self) -> JsonObject:
        return {
            "index": self.index,
            "label": self.label,
            "testCaseId": self.test_case_id,
            "correlation": self.correlation,
            "workflowId": self.workflow_id,
            "obligationIds": None if self.obligation_ids is None else list(self.obligation_ids),
            "componentIds": None if self.component_ids is None else list(self.component_ids),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ReportFilter:
    """What the suite was narrowed by, and from how many tests to how many."""

    before: int
    after: int
    workflow_id: str | None
    obligation_ids: tuple[str, ...]

    def to_wire(self) -> JsonObject:
        return {
            "before": self.before,
            "after": self.after,
            "workflowId": self.workflow_id,
            "obligationIds": list(self.obligation_ids),
        }


@dataclass(frozen=True, slots=True)
class HookReport:
    """Everything the hook decided, in one file the parent process reads after the engine exits."""

    run_id: str | None
    tests: tuple[ReportTest, ...]
    filtered: ReportFilter
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    schema_version: int = HOOK_REPORT_SCHEMA_VERSION

    def to_wire(self) -> JsonObject:
        return {
            "schemaVersion": self.schema_version,
            "runId": self.run_id,
            "tests": [test.to_wire() for test in self.tests],
            "filtered": self.filtered.to_wire(),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class _Filter:
    """The two narrowing switches `hajer eval` carries into the hook, both of which must hold for a test to stay."""

    workflow_id: str | None
    obligation_ids: tuple[str, ...]

    @property
    def active(self) -> bool:
        return self.workflow_id is not None or bool(self.obligation_ids)

    def describe(self) -> str:
        parts: list[str] = []
        if self.workflow_id is not None:
            parts.append(f"workflowId={self.workflow_id}")
        if self.obligation_ids:
            parts.append(f"obligationIds={', '.join(self.obligation_ids)}")
        return "(" + "; ".join(parts) + ")"

    def keeps(self, item: TestClassification) -> bool:
        if not self.active:
            return True
        if item.hajer is None:
            return False
        if self.workflow_id is not None and item.hajer.workflow_id != self.workflow_id:
            return False
        return not self.obligation_ids or bool(set(item.hajer.obligation_ids or ()).intersection(self.obligation_ids))


def before_all(context: JsonObject, *, settings: HajerSettings | None = None) -> JsonObject:
    """Classify, deep-merge, filter and report `context["suite"]["tests"]`; the complete context comes back.

    `settings` is a seam for the tests. The engine's process gets them from its environment, where `hajer eval` put
    the run id, the filters and the report path before it started the engine.
    """
    if settings is None:
        settings = HajerSettings.from_env()
    suite = context.get(SUITE_KEY)
    tests = suite.get(TESTS_KEY) if isinstance(suite, dict) else None
    if not isinstance(suite, dict) or not isinstance(tests, list):
        return context
    inherited = _inherited_hajer(suite)
    default_test = _default_test(suite)
    updated: list[JsonValue] = []
    classified: list[TestClassification] = []
    for index, test in enumerate(tests):
        if not isinstance(test, dict):
            updated.append(test)
            classified.append(_passthrough(index))
            continue
        merged = merged_hajer_metadata(test, inherited)
        updated.append(test if merged is None else _with_hajer(test, merged))
        item = classify_test(test, index, inherited=inherited, default_test=default_test)
        classified.append(_against_manifest(item, settings))
    narrowing = _Filter(workflow_id=settings.eval_workflow, obligation_ids=settings.eval_obligation_ids)
    errors = tuple(line for item in classified for line in item.errors)
    if errors:
        _write_report(settings, _report(settings, classified, narrowing, after=len(classified), errors=errors))
        raise EvalMetadataError(errors)
    kept = [test for test, item in zip(updated, classified, strict=True) if narrowing.keeps(item)]
    if narrowing.active and not kept:
        bound = sum(1 for item in classified if item.correlation == "platform")
        nothing_matched = (
            f"no test matches the filter {narrowing.describe()}: {len(classified)} tests in the suite, "
            f"{bound} with metadata.{HAJER_METADATA_KEY}"
        )
        # The report carries the refusal too: `hajer eval` reads it back to name the cause and exit 2.
        _write_report(settings, _report(settings, classified, narrowing, after=0, errors=(nothing_matched,)))
        raise EvalMetadataError((nothing_matched,))
    _write_report(settings, _report(settings, classified, narrowing, after=len(kept), errors=()))
    return {**context, SUITE_KEY: {**suite, TESTS_KEY: kept}}


def read_report(path: Path) -> JsonObject | None:
    """The report the hook wrote, or `None` when there is none to read: a missing or garbled file is "no report"."""
    try:
        parsed: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def format_warnings(report: JsonObject) -> tuple[str, ...]:
    """The report's per-test warnings as terminal lines, each naming the row, the test and the workflow it is bound to."""
    tests = report.get(TESTS_KEY)
    if not isinstance(tests, list):
        return ()
    lines: list[str] = []
    for entry in tests:
        if not isinstance(entry, dict):
            continue
        warnings = entry.get("warnings")
        if not isinstance(warnings, list):
            continue
        index = entry.get("index")
        label = entry.get("label")
        workflow_id = entry.get("workflowId")
        ref = reference(index, label) if isinstance(index, int) and isinstance(label, str) else None
        for warning in warnings:
            if isinstance(warning, str):
                lines.append(_terminal_line(warning, ref, workflow_id if isinstance(workflow_id, str) else None))
    return tuple(lines)


def _terminal_line(warning: str, ref: str | None, workflow_id: str | None) -> str:
    """`[CODE] test <ref>: message` becomes `[CODE] test <ref> (workflowId=…): message`; anything else is printed as is."""
    if ref is not None and workflow_id is not None:
        marker = f"] test {ref}: "
        code, found, message = warning.partition(marker)
        if found:
            return f"{WARNING_PREFIX} {code}] test {ref} (workflowId={workflow_id}): {message}"
    return f"{WARNING_PREFIX} {warning}"


def _default_test(suite: JsonObject) -> JsonObject | None:
    """`suite.defaultTest` when it is an object; promptfoo has loaded a `file://` one by the time the hook runs."""
    default = suite.get(DEFAULT_TEST_KEY)
    return default if isinstance(default, dict) else None


def _inherited_hajer(suite: JsonObject) -> JsonObject | None:
    """`suite.defaultTest.metadata.hajer` when it is an object: what promptfoo would shallow-merge under every test."""
    default = _default_test(suite)
    if default is None:
        return None
    metadata = default.get(METADATA_KEY)
    if not isinstance(metadata, dict):
        return None
    hajer = metadata.get(HAJER_METADATA_KEY)
    return hajer if isinstance(hajer, dict) else None


def _with_hajer(test: JsonObject, merged: JsonObject) -> JsonObject:
    """The test with the merged object under `metadata.hajer` and every other metadata key as it was."""
    metadata = test.get(METADATA_KEY)
    rewritten: JsonObject = dict(metadata) if isinstance(metadata, dict) else {}
    rewritten[HAJER_METADATA_KEY] = merged
    return {**test, METADATA_KEY: rewritten}


def _against_manifest(item: TestClassification, settings: HajerSettings) -> TestClassification:
    """With a manifest in force, every obligation a test names must be one the manifest declares.

    An undeclared id is an error, like a malformed key: the platform would store a run whose obligation nobody
    declared, and the terminal is the place to learn that, before the first provider call. Without a manifest
    (`HAJER_EVAL_MANIFEST` unset) there is nothing to check against and nothing is.
    """
    if settings.eval_manifest is None or item.hajer is None or not item.hajer.obligation_ids:
        return item
    declared = settings.eval_declared_obligation_ids
    undeclared = [
        f"[{E_OBLIGATION_UNDECLARED}] test {reference(item.index, item.label)}: metadata.{HAJER_METADATA_KEY}."
        f"obligationIds.{position} {name!r} is not declared in {Path(settings.eval_manifest).name}"
        + (f" (declared: {', '.join(declared)})" if declared else " (it declares none)")
        for position, name in enumerate(item.hajer.obligation_ids)
        if name not in declared
    ]
    if not undeclared:
        return item
    return TestClassification(
        index=item.index,
        label=item.label,
        test_case_id=item.test_case_id,
        correlation="none",
        hajer=None,
        warnings=item.warnings,
        errors=(*item.errors, *undeclared),
    )


def _passthrough(index: int) -> TestClassification:
    """A test that is not an object: not ours to judge, so it is kept and reported as uncorrelated."""
    return TestClassification(
        index=index, label=f"#{index}", test_case_id=None, correlation="none", hajer=None, warnings=(), errors=()
    )


def _report(
    settings: HajerSettings,
    classified: list[TestClassification],
    narrowing: _Filter,
    *,
    after: int,
    errors: tuple[str, ...],
) -> HookReport:
    return HookReport(
        run_id=settings.eval_run_id,
        tests=tuple(_entry(item) for item in classified),
        filtered=ReportFilter(
            before=len(classified),
            after=after,
            workflow_id=narrowing.workflow_id,
            obligation_ids=narrowing.obligation_ids,
        ),
        warnings=tuple(line for item in classified for line in item.warnings),
        errors=errors,
    )


def _entry(item: TestClassification) -> ReportTest:
    hajer = item.hajer
    return ReportTest(
        index=item.index,
        label=item.label,
        test_case_id=item.test_case_id,
        correlation=item.correlation,
        workflow_id=hajer.workflow_id if hajer is not None else None,
        obligation_ids=hajer.obligation_ids if hajer is not None else None,
        component_ids=hajer.component_ids if hajer is not None else None,
        warnings=item.warnings,
    )


def _write_report(settings: HajerSettings, report: HookReport) -> None:
    """Best-effort by design: a report nobody can read must not be the reason an otherwise valid run did not start."""
    if settings.eval_hook_report is None:
        return
    try:
        path = Path(settings.eval_hook_report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_wire(), indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        return

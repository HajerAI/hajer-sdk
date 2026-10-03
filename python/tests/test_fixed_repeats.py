"""Synthetic providers prove fixed accounting and CLI wiring, never live model reliability."""

import json
import warnings
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from hajer._settings import HajerSettings
from hajer.pytest_plugin import _fixed_repeats
from hajer.pytest_plugin._fixed_repeats import FixedRepeats, complete_collection
from hajer.pytest_plugin._load import load_suite
from hajer.pytest_plugin._models import Attempt, Case, RepeatMeasurement, Suite
from hajer.pytest_plugin._request_fingerprint import RequestFingerprint
from tests.test_pytest_plugin import build_fixture, run_plugin

FINGERPRINT = "sha256:" + "a" * 64
RESULT: dict[str, JsonValue] = {
    "caseId": "case-1",
    "executionEvidence": [{"mode": "live", "requestFingerprint": FINGERPRINT, "reason": None}],
}


def _collection(root: Path, count: int = 5) -> tuple[FixedRepeats, Suite]:
    suite = load_suite(build_fixture(root, expected="HIGH"))
    suite.results.append(dict(RESULT))
    fixed = FixedRepeats(root / "measured", count, "team-1", "project-1", "b" * 40, HajerSettings())
    fixed.register(suite)
    return fixed, suite


def _read(root: Path) -> list[RepeatMeasurement]:
    return [
        RepeatMeasurement.model_validate_json(path.read_bytes()) for path in sorted(root.glob("*.measurement.json"))
    ]


def test_fixed_attempts_continue_past_agreement_and_preserve_timeout_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed, suite = _collection(tmp_path)
    outputs = iter(["HIGH", "HIGH", "HIGH", "LOW", "TIMEOUT"])
    called: list[bool] = []

    def execute(_suite: Suite, _case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        del settings
        called.append(live)
        # Before even the first dispatch, every planned attempt is already on disk.
        documents = _read(fixed.root)
        assert len(documents[0].plan.planned_attempt_ids) == 5
        assert len(tuple(fixed.root.glob("*.plan.json"))) == 1
        value = next(outputs)
        return Attempt(
            output={"priority": value},
            mode="live",
            provider_successes=1,
            request_fingerprint=FINGERPRINT,
            reason="TIMEOUT" if value == "TIMEOUT" else None,
        )

    monkeypatch.setattr(_fixed_repeats, "execute", execute)
    complete_collection(fixed, 0)
    measured = _read(fixed.root)[0]
    assert called == [True] * 5
    assert [reading.outcome for reading in measured.attempts] == ["SATISFIES"] * 3 + ["VIOLATES", "UNKNOWN"]
    assert measured.attempts[-1].reason == "TIMEOUT"
    assert measured.plan.sampling_assumption == "NOT_ESTABLISHED"
    assert suite.results == [RESULT]
    frozen = RepeatMeasurement.model_validate_json(next(fixed.root.glob("*.plan.json")).read_bytes())
    assert frozen.plan == measured.plan
    assert not frozen.attempts
    assert all(attempt.observed_at >= measured.plan.planned_at for attempt in measured.attempts)


def test_interrupt_preserves_partial_measurement_and_other_cases_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed, suite = _collection(tmp_path)
    extra = Case("case-2", suite.cases[0].checks, suite.cases[0].execution)
    other = Suite("suite-2", 1, tmp_path, [extra], workflow_id="workflow-2")
    fixed.register(other)
    calls = 0

    def execute(_suite: Suite, _case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        nonlocal calls
        del live, settings
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return Attempt(output={"priority": "HIGH"}, mode="live", provider_successes=1, request_fingerprint=FINGERPRINT)

    monkeypatch.setattr(_fixed_repeats, "execute", execute)
    with pytest.raises(KeyboardInterrupt):
        complete_collection(fixed, 0)
    measurements = _read(fixed.root)
    assert sorted(len(item.attempts) for item in measurements) == [0, 1]
    assert all(len(item.plan.planned_attempt_ids) == 5 for item in measurements)


@pytest.mark.parametrize("reason", ["CI_BUDGET_EXHAUSTED", "TIMEOUT"])
def test_unavailable_attempts_still_consume_the_fixed_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    fixed, _ = _collection(tmp_path)

    def execute(_suite: Suite, _case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        del live, settings
        return Attempt(reason=reason)

    monkeypatch.setattr(_fixed_repeats, "execute", execute)
    complete_collection(fixed, 0)
    readings = _read(fixed.root)[0].attempts
    assert len(readings) == 5
    assert all(item.outcome == "UNKNOWN" and item.reason == reason for item in readings)


def test_interrupted_session_does_not_start_more_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed, _ = _collection(tmp_path)

    def forbidden(_suite: Suite, _case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        del live, settings
        pytest.fail("An interrupted session must not dispatch additional calls")

    monkeypatch.setattr(_fixed_repeats, "execute", forbidden)
    complete_collection(fixed, pytest.ExitCode.INTERRUPTED)
    assert _read(fixed.root)[0].attempts == ()


def test_request_fingerprint_changes_with_model_input_and_order_but_not_credentials() -> None:
    def fingerprint(model: str, token: str) -> str | None:
        collector = RequestFingerprint()
        collector.observe(
            httpx.Request(
                "POST", "https://api.example.test/v1", json={"model": model}, headers={"authorization": token}
            )
        )
        return collector.digest()

    assert fingerprint("model-a", "secret-a") == fingerprint("model-a", "secret-b")
    assert fingerprint("model-a", "secret-a") != fingerprint("model-b", "secret-a")


def test_changed_request_is_unknown_even_when_the_output_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed, _ = _collection(tmp_path, 1)

    def execute(_suite: Suite, _case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        del live, settings
        return Attempt(
            output={"priority": "HIGH"}, mode="live", provider_successes=1, request_fingerprint="sha256:" + "c" * 64
        )

    monkeypatch.setattr(_fixed_repeats, "execute", execute)
    complete_collection(fixed, 0)
    reading = _read(fixed.root)[0].attempts[0]
    assert reading.outcome == "UNKNOWN"
    assert reading.reason == "REQUEST_CONFIGURATION_CHANGED"


def test_twenty_cases_make_one_hundred_fixed_attempts_with_all_plans_written_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed, original = _collection(tmp_path)
    for index in range(1, 20):
        case = Case(f"case-{index + 1}", original.cases[0].checks, original.cases[0].execution)
        suite = Suite(f"suite-{index + 1}", 1, tmp_path, [case], workflow_id="workflow-1")
        suite.results.append({**RESULT, "caseId": case.case_id})
        fixed.register(suite)
    calls = 0

    def execute(_suite: Suite, _case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        nonlocal calls
        del live, settings
        calls += 1
        assert len(tuple(fixed.root.glob("*.plan.json"))) == 20
        return Attempt(output={"priority": "HIGH"}, mode="live", provider_successes=1, request_fingerprint=FINGERPRINT)

    monkeypatch.setattr(_fixed_repeats, "execute", execute)
    complete_collection(fixed, 0)
    assert calls == 100
    assert sum(len(item.attempts) for item in _read(fixed.root)) == 100


@pytest.mark.parametrize("saved", [0, 1])
def test_evidence_write_failure_stops_further_dispatch_and_retains_missing_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], saved: int
) -> None:
    fixed, _ = _collection(tmp_path)
    calls = 0

    def execute(_suite: Suite, _case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        nonlocal calls
        del live, settings
        calls += 1
        return Attempt(output={"priority": "HIGH"}, mode="live", provider_successes=1, request_fingerprint=FINGERPRINT)

    def no_space(path: Path, text: str) -> None:
        if calls > saved:
            raise OSError("disk full")
        path.write_text(text)

    monkeypatch.setattr(_fixed_repeats, "execute", execute)
    monkeypatch.setattr(_fixed_repeats, "_atomic", no_space)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        complete_collection(fixed, 0)
    assert "HAJER_FIXED_REPEAT_EVIDENCE_UNAVAILABLE" in capsys.readouterr().err
    assert calls == saved + 1
    assert len(_read(fixed.root)[0].attempts) == saved
    assert len(_read(fixed.root)[0].plan.planned_attempt_ids) == 5
    assert f"{5 - saved} missing / 5 planned" in fixed.summary()


def test_unwritable_plan_refuses_all_extra_calls_and_keeps_declared_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixed, _ = _collection(tmp_path)

    def no_space(_path: Path, _text: str) -> None:
        raise OSError("disk full")

    def forbidden(_suite: Suite, _case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        del live, settings
        pytest.fail("An unwritable plan cannot dispatch calls")

    monkeypatch.setattr(_fixed_repeats, "write_private", no_space)
    monkeypatch.setattr(_fixed_repeats, "execute", forbidden)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        complete_collection(fixed, 0)
    assert "HAJER_FIXED_REPEAT_EVIDENCE_UNAVAILABLE: no extra calls started" in capsys.readouterr().err
    assert "5 missing / 5 planned" in fixed.summary()
    assert not _read(fixed.root)


@pytest.mark.parametrize("options", [[], ["--hajer-live"]])
def test_fixed_mode_refuses_without_live_and_budget(tmp_path: Path, options: list[str]) -> None:
    build_fixture(tmp_path)
    result = run_plugin(tmp_path, *options, "--hajer-fixed-repeats", "5")
    assert result.returncode == 4
    assert "Fixed repeats require" in result.stderr


def test_pytest_opt_in_executes_fixed_plan_after_ordinary_policy_without_changing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_fixture(tmp_path, expected="HIGH")
    monkeypatch.setenv("HAJER_CI_BUDGET_MICROUSD", "1000000")
    monkeypatch.setenv("HAJER_CI_BUDGET_FILE", str(tmp_path / "spend.sqlite"))
    monkeypatch.setenv("HAJER_TEAM_ID", "team-1")
    monkeypatch.setenv("HAJER_CI_COMMIT_SHA", "b" * 40)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    (tmp_path / "conftest.py").write_text("""
from pathlib import Path
from hajer.pytest_plugin import _run, _fixed_repeats
from hajer.pytest_plugin._models import Attempt
calls = 0
def execute(suite, case, *, live, settings):
    global calls
    calls += 1
    Path("calls.txt").write_text(str(calls))
    if calls > 3:
        assert Path("results.json").is_file()
        assert list(Path(".").glob("results-repeats-*/*.plan.json"))
    return Attempt(output={"priority": "HIGH" if calls <= 3 else "LOW"}, mode="live", provider_successes=1, request_fingerprint="sha256:" + "a" * 64)
_run.execute = execute
_fixed_repeats.execute = execute
""")
    result = run_plugin(tmp_path, "--hajer-live", "--hajer-fixed-repeats", "5", "--hajer-project-id", "project-1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "calls.txt").read_text() == "8"
    report = json.loads((tmp_path / "results.json").read_text())
    assert report["suites"][0]["cases"][0]["checks"][0]["attempts"] == ["PASS"] * 3
    assert report["receiptId"]
    measured = _read(next(tmp_path.glob("results-repeats-*")))[0]
    assert report["repeatMeasurements"] == [measured.model_dump(mode="json")]
    assert [item.outcome for item in measured.attempts] == ["VIOLATES"] * 5
    assert "Fixed-repeat measurement files" in result.stdout


def test_oversized_measurement_plan_is_refused_before_dispatch(tmp_path: Path) -> None:
    fixed, suite = _collection(tmp_path)
    case = suite.cases[0]
    oversized = Suite("oversized", 1, tmp_path, [Case(str(i), case.checks, case.execution) for i in range(512)])
    with pytest.raises(pytest.UsageError, match="receipt limit"):
        fixed.register(oversized)
    assert len(fixed.pending) == 1
    assert not tuple(fixed.root.glob("*.measurement.json"))

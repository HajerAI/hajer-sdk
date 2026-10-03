"""Advisory CI decisions preserve the original evidence, including unknowns."""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from hajer._settings import HajerSettings
from hajer.pytest_plugin import _run
from hajer.pytest_plugin._judge import JudgeAnswer
from hajer.pytest_plugin._models import Attempt, Case, Execution, Suite, Verdict
from hajer.pytest_plugin._policy import disposition, evaluator


def _suite(*kinds: str) -> Suite:
    checks: list[dict[str, JsonValue]] = [
        {"check": {"id": str(index), "evaluator": kind}} for index, kind in enumerate(kinds)
    ]
    return Suite("suite", 1, Path("."), [Case("case", checks, None)])


def _result(*verdicts: str) -> dict[str, JsonValue]:
    return {
        "caseId": "case",
        "checks": [
            {"checkId": str(index), "verdict": verdict, "attempts": [verdict], "label": "qualified", "reason": None}
            for index, verdict in enumerate(verdicts)
        ],
    }


@pytest.mark.parametrize("verdict", ["FAIL", "UNABLE_TO_VERIFY"])
def test_judge_advisory_retains_serialized_evidence(verdict: str) -> None:
    result = _result(verdict)
    before = json.dumps(result)
    assert disposition(_suite("SCOPED_SEMANTIC_JUDGE"), result) == "NEUTRAL"
    assert json.dumps(result) == before


def test_mixed_deterministic_failure_still_fails() -> None:
    suite = _suite("REVIEWED_PREDICATE", "SCOPED_SEMANTIC_JUDGE")
    assert disposition(suite, _result("FAIL", "FAIL")) == "FAIL"
    assert disposition(suite, _result("PASS", "FAIL")) == "NEUTRAL"
    assert disposition(_suite("REVIEWED_PREDICATE"), _result("UNABLE_TO_VERIFY")) == "FAIL"


def test_missing_identity_empty_and_unexecuted_are_not_pass() -> None:
    assert disposition(_suite("future-kind"), _result("PASS")) == "NEUTRAL"
    assert disposition(_suite("SCOPED_SEMANTIC_JUDGE"), _result()) == "FAIL"
    for reason in ("NOT_EXECUTED", "APP_EXECUTION_UNAVAILABLE: TIMEOUT"):
        result: dict[str, JsonValue] = {
            "caseId": "case",
            "checks": [{"checkId": "0", "verdict": "UNABLE_TO_VERIFY", "reason": reason}],
        }
        assert disposition(_suite("SCOPED_SEMANTIC_JUDGE"), result) == "FAIL"
    assert disposition(_suite("SCOPED_SEMANTIC_JUDGE"), {"caseId": "case", "skipped": "ADAPTER_UNVERIFIED"}) == "FAIL"


def test_ambiguous_duplicate_definition_is_unverified() -> None:
    suite = _suite("REVIEWED_PREDICATE")
    suite.cases[0].checks.append({"check": {"id": "0"}})
    assert evaluator(suite.cases[0], "0") == "UNKNOWN"
    assert disposition(suite, _result("PASS")) == "NEUTRAL"


@pytest.mark.parametrize(
    "reason",
    [
        "TIMEOUT",
        "CHILD_FAILED",
        "MISSING_OR_STALE_EXECUTION_BINDING",
        "ValueError",
        "RUNTIME_SIDE_EFFECT_REFUSED: host",
        "REPLAY_REFUSED_OR_WEAK_MATCH",
    ],
)
def test_judge_cannot_hide_application_or_infrastructure_failure(reason: str) -> None:
    suite = _suite("REVIEWED_PREDICATE", "SCOPED_SEMANTIC_JUDGE")
    result: dict[str, JsonValue] = {
        "caseId": "case",
        "checks": [
            {"checkId": "0", "verdict": "PASS", "reason": None},
            {"checkId": "1", "verdict": "UNABLE_TO_VERIFY", "reason": f"APP_EXECUTION_UNAVAILABLE: {reason}"},
        ],
    }
    assert disposition(suite, result) == "FAIL"


@pytest.mark.parametrize("failure", ["TIMEOUT", "CHILD_FAILED"])
@pytest.mark.parametrize("metamorphic", [False, True])
@pytest.mark.parametrize("later", ["PASS", "UNABLE_TO_VERIFY"])
def test_later_execution_failure_survives_earlier_judge_unknown(
    monkeypatch: pytest.MonkeyPatch, failure: str, later: Verdict, *, metamorphic: bool
) -> None:
    suite = _suite("SCOPED_SEMANTIC_JUDGE")
    case = suite.cases[0]
    case.execution = Execution(adapter={}, input={"body": "Checkout is down"}, boundaries=[])
    case.checks[0]["judge"] = {}
    if metamorphic:
        case.checks[0]["metamorphic"] = {"relation": "DIR", "edit": {"kind": "TONE", "field": ["body"]}}
    calls = 0

    def execute(suite: Suite, case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        nonlocal calls
        del suite, case, live, settings
        index = calls
        calls += 1
        # Second main attempt, or its edited child: first judge reading was already unavailable.
        return Attempt(reason=failure) if index == (3 if metamorphic else 1) else Attempt(output={"ok": True})

    def judge(check: dict[str, JsonValue], output: JsonValue, request: JsonValue, attempt: int) -> JudgeAnswer:
        del check, output, request
        return "UNABLE_TO_VERIFY" if attempt == 0 else later

    monkeypatch.setattr(_run, "execute", execute)
    result = _run.run(suite, case, live=True, settings=HajerSettings(), judge=judge)
    (reading,) = cast(list[dict[str, JsonValue]], result["checks"])
    assert reading["attempts"] == ["UNABLE_TO_VERIFY", "UNABLE_TO_VERIFY", later, later, later]
    assert reading["verdict"] == later
    assert reading["reason"] == f"APP_EXECUTION_UNAVAILABLE: {failure}"
    assert disposition(suite, result) == "FAIL"

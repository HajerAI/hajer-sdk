"""Local attempt evidence distinguishes an early return from a live provider response."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from hajer._ci_spend import Spend
from hajer._settings import HajerSettings
from hajer._upload import upload
from hajer.pytest_plugin import _run
from hajer.pytest_plugin._models import Attempt, Case, Suite


@pytest.mark.parametrize("successes", [None, 0, 1])
def test_attempt_provenance_is_local_and_preserves_unknown(
    monkeypatch: pytest.MonkeyPatch, successes: int | None
) -> None:
    def execute(suite: Suite, case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        return Attempt(output={"score": 0}, mode="live", provider_successes=successes)

    monkeypatch.setattr(_run, "execute", execute)
    check: dict[str, JsonValue] = {
        "check": {"id": "range", "evaluator": "REVIEWED_PREDICATE"},
        "draft": {"expression": {"op": "range", "path": ["output", "score"], "lower": -1, "upper": 1}},
    }
    case = Case("case", [check], None)
    result = _run.run(Suite("suite", 1, Path("."), [case]), case, live=True, settings=HajerSettings())
    evidence = result["executionEvidence"]
    assert isinstance(evidence, list)
    assert evidence
    assert all(isinstance(item, dict) and item["providerSuccesses"] == successes for item in evidence)


def test_legacy_child_has_unknown_not_measured_zero() -> None:
    assert Attempt.model_validate_json('{"output": null}').provider_successes is None


def test_local_provenance_does_not_expand_upload_wire() -> None:
    evidence: list[JsonValue] = [{"mode": "live", "providerSuccesses": 1, "reason": None, "edited": []}]
    case: dict[str, JsonValue] = {
        "caseId": "case",
        "checks": [{"checkId": "check", "verdict": "FAIL", "attempts": ["PASS", "FAIL"]}],
        "executionEvidence": evidence,
    }
    payload: dict[str, JsonValue] = {"suites": [{"suiteId": "suite", "cases": [case]}]}
    received: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request.content)
        return httpx.Response(201)

    assert upload(
        payload, "project", HajerSettings(api_key="fixture", team_id="team"), transport=httpx.MockTransport(handle)
    )
    assert b"executionEvidence" not in received[0]
    assert b'"FAIL"' in received[0]
    assert b'"PASS"' in received[0]
    assert "executionEvidence" in json.dumps(payload)


def test_spend_snapshot_is_uploaded_as_estimate_or_unknown(tmp_path: Path) -> None:
    settings = HajerSettings(api_key="fixture", team_id="team")
    funded = settings.model_copy(update={"ci_budget_file": str(tmp_path / "budget.sqlite"), "ci_budget_microusd": 1000})
    received: list[dict[str, JsonValue]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(201)

    for configuration in (settings, funded):
        snapshot: dict[str, JsonValue] = dict(Spend(configuration).snapshot())
        assert upload({"suites": [], "spend": snapshot}, "project", settings, transport=httpx.MockTransport(handle))
        assert received[-1]["spend"] == snapshot
    assert received[0]["spend"] == {
        "version": 1,
        "source": "caller_reported",
        "scope": "ci_budget_ledger",
        "basis": "unavailable",
    }

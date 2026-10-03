"""Approved traffic inputs enter the actual suite model without becoming repository files."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from hajer._settings import HajerSettings
from hajer._upload import upload
from hajer._verify_adapters import AdapterCheck, Authored
from hajer.pytest_plugin import _adapters
from hajer.pytest_plugin._load import load_suite
from hajer.pytest_plugin._models import State
from hajer.pytest_plugin._private_inputs import hydrate
from tests.test_pytest_plugin import build_fixture, run_plugin


def private_suite(root: Path) -> Path:
    path = build_fixture(root, bound=False)
    payload = json.loads(path.read_text())
    payload["members"][0]["case"]["inputExport"] = {
        "status": "UNAVAILABLE",
        "sensitivity": "TRAFFIC_DERIVED",
        "reason": "TRAFFIC_DERIVED_NOT_EXPORTED",
    }
    path.write_text(json.dumps(payload))
    return path


def response_for(path: Path) -> dict[str, JsonValue]:
    binding: dict[str, JsonValue] = {
        "suiteId": "suite-fixture",
        "suiteVersion": 7,
        "suiteDigest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "cases": {
            "case-1": {
                "adapter": {"module": "customer", "qualname": "classify", "constructor": "NONE", "input": "SINGLE"},
                "input": {"text": "redacted test input"},
                "boundaries": [],
            }
        },
    }
    return {"executionDocument": json.dumps(binding), "unavailableCaseIds": [], "revision": "a" * 40}


def ci_settings() -> HajerSettings:
    return HajerSettings(api_key="hjk_test", team_id="team", ci_commit_sha="a" * 40)


def test_fetched_case_enters_suite_only_in_memory(tmp_path: Path) -> None:
    path = private_suite(tmp_path)
    before = {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}
    suite = load_suite(path)
    assert suite.cases[0].execution is None

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/teams/team/projects/project/suite-runs/execution-inputs"
        assert request.headers["Authorization"] == "Bearer hjk_test"
        assert json.loads(request.content)["revision"] == "a" * 40
        return httpx.Response(200, json=response_for(path))

    hydrate(suite, path, "project", ci_settings(), transport=httpx.MockTransport(answer))
    assert suite.private_inputs
    assert suite.cases[0].execution is not None
    assert suite.cases[0].execution.input == {"text": "redacted test input"}
    assert suite.cases[0].execution.boundaries == []
    assert before == {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}


@pytest.mark.parametrize("change", ["revision", "digest", "extra_case", "boundary", "overlap"])
def test_mismatched_or_recorded_response_is_refused(tmp_path: Path, change: str) -> None:
    path = private_suite(tmp_path)
    response = response_for(path)
    binding = json.loads(str(response["executionDocument"]))
    if change == "revision":
        response["revision"] = "b" * 40
    elif change == "digest":
        binding["suiteDigest"] = "sha256:" + "0" * 64
    elif change == "extra_case":
        binding["cases"]["unrequested"] = binding["cases"]["case-1"]
    elif change == "boundary":
        binding["cases"]["case-1"]["boundaries"] = [{"output": "must not be delivered"}]
    else:
        response["unavailableCaseIds"] = ["case-1"]
    response["executionDocument"] = json.dumps(binding)
    suite = load_suite(path)
    with pytest.raises(ValueError, match="PRIVATE_CI_INPUTS_UNAVAILABLE"):
        hydrate(
            suite,
            path,
            "project",
            ci_settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        )
    assert suite.cases[0].execution is None


def test_unapproved_case_stays_without_execution(tmp_path: Path) -> None:
    path = private_suite(tmp_path)
    response = response_for(path)
    binding = json.loads(str(response["executionDocument"]))
    binding["cases"] = {}
    response.update(executionDocument=json.dumps(binding), unavailableCaseIds=["case-1"])
    suite = load_suite(path)
    with pytest.raises(ValueError, match="PRIVATE_CI_INPUTS_UNAVAILABLE"):
        hydrate(
            suite,
            path,
            "project",
            ci_settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        )
    assert suite.cases[0].execution is None
    assert not suite.private_inputs


def test_private_input_flag_cannot_enable_offline_replay(tmp_path: Path) -> None:
    private_suite(tmp_path)
    ran = run_plugin(tmp_path, "--hajer-private-inputs")
    assert ran.returncode != 0
    assert "Private CI inputs require live execution" in ran.stdout + ran.stderr


def test_private_adapter_evidence_stays_local_when_uploading() -> None:
    payload: dict[str, JsonValue] = {
        "suites": [{"cases": [{"caseId": "case-1", "adapterCheck": {"status": "VERIFIED"}, "executionEvidence": []}]}]
    }

    def answer(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["suites"][0]["cases"] == [{"caseId": "case-1"}]
        return httpx.Response(201)

    assert upload(payload, "project", ci_settings(), transport=httpx.MockTransport(answer))


def test_over_budget_response_closes_without_installing_private_input(tmp_path: Path) -> None:
    path = private_suite(tmp_path)
    suite = load_suite(path)
    small = ci_settings().model_copy(update={"ci_input_max_bytes": 32})
    with pytest.raises(ValueError, match="PRIVATE_CI_INPUTS_UNAVAILABLE"):
        hydrate(
            suite,
            path,
            "project",
            small,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response_for(path))),
        )
    assert suite.cases[0].execution is None


def test_private_adapter_is_checked_again_when_case_input_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = private_suite(tmp_path)
    suite = load_suite(path)
    hydrate(
        suite,
        path,
        "project",
        ci_settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response_for(path))),
    )
    seen: list[JsonValue] = []

    def check(
        root: Path,
        adapter: dict[str, JsonValue],
        *,
        environment: dict[str, str],
        settings: HajerSettings,
        authored: Authored | None = None,
    ) -> AdapterCheck:
        del root, adapter, environment, settings
        assert authored is not None
        seen.append(authored.value)
        return AdapterCheck("customer:classify", None, "customer.py:3", "VERIFIED", "fixture")

    monkeypatch.setattr(_adapters, "check_adapter", check)
    state = State()
    assert _adapters.adapter_status(state, suite, suite.cases[0], ci_settings()) == ("VERIFIED", "fixture")
    assert suite.cases[0].execution is not None
    suite.cases[0].execution.input = {"text": "another redacted case"}
    _adapters.adapter_status(state, suite, suite.cases[0], ci_settings())
    assert seen == [{"text": "redacted test input"}, {"text": "another redacted case"}]


@pytest.mark.parametrize("declare_missing", [False, True])
def test_partial_payload_installs_no_private_bindings(tmp_path: Path, declare_missing: bool) -> None:
    path = private_suite(tmp_path)
    document = json.loads(path.read_text())
    second = json.loads(json.dumps(document["members"][0]))
    second["case"]["id"] = "case-2"
    document["members"].append(second)
    path.write_text(json.dumps(document))
    response = response_for(path)
    response["unavailableCaseIds"] = ["case-2"] if declare_missing else []
    suite = load_suite(path)
    with pytest.raises(ValueError, match="PRIVATE_CI_INPUTS_UNAVAILABLE"):
        hydrate(
            suite,
            path,
            "project",
            ci_settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        )
    assert all(case.execution is None for case in suite.cases)
    assert not suite.private_inputs

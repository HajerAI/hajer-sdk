"""The customer's app must actually execute; a recording is never the assertion."""

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from hajer._settings import HajerSettings
from hajer.pytest_plugin._upload import upload
from tests.repo import platform_path, requires_platform


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


#: The model call the fixture's suite is about, and the adapter check's result for it: these tests are about what the
#: plugin does with a verified adapter's cases (`test_adapter_site.py` and `test_adapter_authority.py` cover the others).
FIXTURE_SITE = "customer.py:3"

#: The plugin checks every adapter itself in the session and no longer reads a persisted `verify-adapters` report
#: (`_adapters.adapter_status`; `test_adapter_authority.py`). These fixture apps stand in for a provider with an
#: unwrapped host or an `httpx.MockTransport`, which the real check cannot reach, so the pytest process under test
#: loads this double of that one check (`-p`), answering from the fixture's own recorded adapter statuses. The case
#: child and the reach child start with `-I -S`, so nothing else sees it; `test_committed_fixture_replays_without_
#: provider_or_hajer_key` runs the real check end to end on an app it can reach.
ADAPTER_DOUBLE = "hajer_adapter_double"
_ADAPTER_DOUBLE_SOURCE = '''\
"""Test double for the plugin's in-session adapter check (tests/test_pytest_plugin.py::verified)."""
import json
from pathlib import Path

from hajer._verify_adapters import AdapterCheck
from hajer.pytest_plugin import _adapters

_checked = _adapters.check_adapter


def _recorded(root, adapter, *, environment, settings):
    named = adapter.get("adapterId")
    adapter_id = named if isinstance(named, str) else f"{adapter.get('module')}:{adapter.get('qualname')}"
    for item in json.loads((Path(root) / ".hajer" / "adapter-checks.json").read_text())["adapters"]:
        if (item["adapterId"], item["site"]) == (adapter_id, adapter.get("site")):
            return AdapterCheck(adapter_id, None, item["site"], item["status"], item["detail"])
    return _checked(root, adapter, environment=environment, settings=settings)


_adapters.check_adapter = _recorded
'''


def verified(root: Path, adapter_id: str = "customer:classify", site: str = FIXTURE_SITE) -> None:
    (root / f"{ADAPTER_DOUBLE}.py").write_text(_ADAPTER_DOUBLE_SOURCE)
    document = {
        "mode": "replay",
        "suites": [],
        "adapters": [
            {
                "adapterId": adapter_id,
                "site": site,
                "status": "VERIFIED",
                "detail": "a model call was made from the site",
            }
        ],
    }
    (root / ".hajer" / "adapter-checks.json").write_text(json.dumps(document))


def build_fixture(root: Path, *, expected: str = "high", bound: bool = True, site: str | None = FIXTURE_SITE) -> Path:
    suites = root / ".hajer" / "suites"
    suites.mkdir(parents=True)
    if site is not None:
        verified(root, site=site)
    (root / "customer.py").write_text(
        "import httpx\n"
        "def classify(value):\n"
        "    response = httpx.post('https://api.example.test/classify', content=value)\n"
        "    return {'priority': response.json()['answer'].upper()}\n"
    )
    suite = {
        **({"site": site} if site is not None else {}),
        "suiteId": "suite-fixture",
        "version": 7,
        "workflowKey": "classify",
        "label": "DIAGNOSTIC",
        "members": [
            {
                "case": {"id": "case-1"},
                "check": {
                    "check": {"id": "check-1", "evaluator": "REVIEWED_PREDICATE"},
                    "draft": {
                        "expression": {"op": "eq", "path": ["output", "priority"], "valueJson": json.dumps(expected)}
                    },
                },
            }
        ],
    }
    path = suites / "classify.json"
    path.write_text(json.dumps(suite))
    if bound:
        body = json.dumps({"answer": "high"})
        sidecar = {
            "suiteId": "suite-fixture",
            "suiteVersion": 7,
            "suiteDigest": _digest(path.read_bytes()),
            "cases": {
                "case-1": {
                    "adapter": {"module": "customer", "qualname": "classify", "constructor": "NONE", "input": "SINGLE"},
                    "input": "ticket",
                    "boundaries": [
                        {
                            "method": "POST",
                            "scheme": "https",
                            "host": "api.example.test",
                            "path": "/classify",
                            "status": 200,
                            "body": body,
                            "bodyDigest": _digest(body.encode()),
                            "requestBodyDigest": _digest(b"ticket"),
                            "contentType": "application/json",
                        }
                    ],
                }
            },
        }
        sidecar_path = root / ".hajer" / "executions"
        sidecar_path.mkdir()
        (sidecar_path / path.name).write_text(json.dumps(sidecar))
    return path


def run_plugin(root: Path, *options: str) -> subprocess.CompletedProcess[str]:
    checks = root / ".hajer" / "adapter-checks.json"
    handed = ["--hajer-adapter-checks", str(checks)] if checks.is_file() else []
    double = ["-p", ADAPTER_DOUBLE] if (root / f"{ADAPTER_DOUBLE}.py").is_file() else []
    return subprocess.run(  # noqa: S603 - invoke the current interpreter against our local fixture
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "hajer.pytest_plugin",
            *double,
            "--hajer-suites",
            str(root / ".hajer/suites"),
            "--hajer-results",
            str(root / "results.json"),
            *handed,
            *options,
            "-q",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def suites_action_python(root: Path, prelude: str = "") -> Path:
    """A directory whose `python` is the one `run-suites.sh` finds first on PATH: package installation stubbed, the
    adapter double loaded into its pytest when the fixture recorded adapter statuses, else the current interpreter."""
    binaries = root / "bin"
    binaries.mkdir()
    double = (
        f'if [[ "$1 $2" == "-m pytest" ]]; then exec "{sys.executable}" "$@" -p {ADAPTER_DOUBLE}; fi\n'
        if (root / f"{ADAPTER_DOUBLE}.py").is_file()
        else ""
    )
    python = binaries / "python"
    python.write_text(
        f'#!/bin/bash\nif [[ "$1 $2" == "-m pip" ]]; then exit 0; fi\n{prelude}{double}exec "{sys.executable}" "$@"\n'
    )
    python.chmod(0o700)
    return binaries


@pytest.mark.parametrize(
    ("expected", "bound", "verdict", "reason"),
    [
        ("HIGH", True, "PASS", "REPLAY_HAS_NO_VARIANCE"),
        ("low", True, "FAIL", "REPLAY_HAS_NO_VARIANCE"),
        # An app that could not be executed is named as such (`_run._reason`), which CI policy fails, never neutral.
        ("HIGH", False, "UNABLE_TO_VERIFY", "APP_EXECUTION_UNAVAILABLE: MISSING_OR_STALE_EXECUTION_BINDING"),
    ],
)
def test_a_replay_is_decided_by_its_single_read_and_labelled_not_yet(
    tmp_path: Path, expected: str, bound: bool, verdict: str, reason: str
) -> None:
    # The app's real output on fixed recorded answers is read against the declared expectation once: that read
    # decides the job (an unknown read is never green), while the label stays not_yet (no live repetition).
    build_fixture(tmp_path, expected=expected, bound=bound)
    result = run_plugin(tmp_path)
    assert "1 hajer case" in result.stdout, result.stdout + result.stderr
    assert "mode=replay" in result.stdout
    payload = json.loads((tmp_path / "results.json").read_text())
    assert payload["mode"] == "replay"
    check = payload["suites"][0]["cases"][0]["checks"][0]
    assert (check["attempts"], check["label"], check["verdict"], check["reason"]) == (
        [verdict],
        "not_yet",
        verdict,
        reason,
    )
    assert payload["suites"][0]["suiteVersion"] == 7
    assert payload["suites"][0]["cases"][0]["caseId"] == "case-1"
    assert result.returncode == (0 if verdict == "PASS" else 1)


def test_a_child_that_fails_to_start_names_its_last_stderr_line_locally_and_never_in_the_upload(tmp_path: Path) -> None:
    # `CHILD_FAILED` alone hides the traceback that names the cause. The last stderr line is kept in the local results and
    # the failure text, and the upload strips it with the rest of `executionEvidence`: it is the application's own stderr.
    build_fixture(tmp_path, expected="HIGH")
    (tmp_path / "customer.py").write_text("raise SystemExit('the app refused to start: FIXTURE_BOOT_FAILURE')\n")
    result = run_plugin(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "APP_EXECUTION_UNAVAILABLE: CHILD_FAILED" in result.stdout
    assert "child stderr: the app refused to start: FIXTURE_BOOT_FAILURE" in result.stdout
    payload = json.loads((tmp_path / "results.json").read_text())
    case = payload["suites"][0]["cases"][0]
    assert case["checks"][0]["reason"] == "APP_EXECUTION_UNAVAILABLE: CHILD_FAILED"
    assert case["executionEvidence"][0]["stderrTail"] == "the app refused to start: FIXTURE_BOOT_FAILURE"
    received: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request.content)
        return httpx.Response(201)

    settings = HajerSettings(api_key="fixture", team_id="team")
    assert upload(payload, "project", settings, transport=httpx.MockTransport(handle))
    assert b"FIXTURE_BOOT_FAILURE" not in received[0]
    assert b"stderrTail" not in received[0]


def test_unreachable_adapter_retains_its_case_and_unknown_check_in_receipt(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    path = tmp_path / ".hajer" / "adapter-checks.json"
    checks = json.loads(path.read_text())
    checks["adapters"][0]["status"] = "RUNTIME_UNREACHED"
    checks["adapters"][0]["detail"] = "configuration unavailable"
    path.write_text(json.dumps(checks))
    result = run_plugin(tmp_path)
    assert result.returncode != 0
    receipt = json.loads((tmp_path / "results.json").read_text())
    case = receipt["suites"][0]["cases"][0]
    assert case["caseId"] == "case-1"
    assert "skipped" not in case
    assert case["checks"][0]["verdict"] == "UNABLE_TO_VERIFY"
    assert case["checks"][0]["reason"] == "APP_EXECUTION_UNAVAILABLE: ADAPTER_UNVERIFIED"


def test_changed_suite_refuses_stale_execution_binding(tmp_path: Path) -> None:
    path = build_fixture(tmp_path, expected="HIGH")
    path.write_text(path.read_text() + "\n")
    result = run_plugin(tmp_path)
    assert "UNABLE_TO_VERIFY" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 1


def test_declared_environment_reaches_the_application_child(tmp_path: Path) -> None:
    build_fixture(tmp_path, expected="HIGH")
    (tmp_path / ".hajer" / "replay.toml").write_text('[environment]\nHAJER_FIXTURE_FLAG = "blocked"\nAPP_MODE = "ci"\n')
    app = tmp_path / "customer.py"
    app.write_text('import os\nassert os.environ["APP_MODE"] == "ci"\n' + app.read_text())
    result = run_plugin(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((tmp_path / "results.json").read_text())
    assert receipt["suites"][0]["cases"][0]["checks"][0]["verdict"] == "PASS"


def live_app(root: Path, values: list[str | None]) -> None:
    """A fake-provider app whose answer on the Nth process is values[N % len]; None raises instead."""
    (root / "customer.py").write_text(
        "from pathlib import Path\nimport httpx\n"
        "def classify(value):\n"
        "    counter = Path('calls.txt')\n"
        "    count = int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(count + 1))\n"
        f"    answer = {values!r}[count % {len(values)}]\n"
        "    if answer is None:\n"
        "        raise RuntimeError('fixture failure')\n"
        "    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'priority': answer}))) as client:\n"
        "        return client.post('https://fake.invalid', content=value).json()\n"
    )


def rebind(root: Path, path: Path, document: dict[str, JsonValue]) -> None:
    """Rewrite a suite and pin its execution binding to the new bytes."""
    path.write_text(json.dumps(document))
    binding_path = root / ".hajer/executions" / path.name
    binding = json.loads(binding_path.read_text())
    binding["suiteDigest"] = _digest(path.read_bytes())
    binding_path.write_text(json.dumps(binding))


@pytest.mark.parametrize(
    ("values", "attempts", "label", "verdict"),
    [
        (["HIGH"], ["PASS"] * 3, "qualified", "PASS"),
        (["low"], ["FAIL"] * 3, "qualified", "FAIL"),
        (["low", "HIGH"], ["FAIL", "PASS"], "flaky", "UNABLE_TO_VERIFY"),
        ([None], ["UNABLE_TO_VERIFY"] * 5, "not_yet", "UNABLE_TO_VERIFY"),
        (["HIGH", None], ["PASS", "UNABLE_TO_VERIFY"] * 2 + ["PASS"], "qualified", "PASS"),
    ],
)
def test_live_repeats_until_the_shared_rule_settles_or_the_cap(
    tmp_path: Path, values: list[str | None], attempts: list[str], label: str, verdict: str
) -> None:
    build_fixture(tmp_path, expected="HIGH")
    live_app(tmp_path, values)
    result = run_plugin(tmp_path, "--hajer-live")
    assert int((tmp_path / "calls.txt").read_text()) == len(attempts), result.stdout + result.stderr
    check = json.loads((tmp_path / "results.json").read_text())["suites"][0]["cases"][0]["checks"][0]
    assert (check["attempts"], check["label"], check["verdict"]) == (attempts, label, verdict)
    # A settled PASS is green only when no attempt failed to execute: an execution failure (here the app raising) is
    # retained as APP_EXECUTION_UNAVAILABLE and fails CI even beside a later settled PASS (`_policy.disposition`).
    assert result.returncode == (0 if verdict == "PASS" and "UNABLE_TO_VERIFY" not in attempts else 1)


def test_live_case_repeats_while_any_of_its_checks_is_unsettled(tmp_path: Path) -> None:
    path = build_fixture(tmp_path, expected="HIGH")
    document = json.loads(path.read_text())
    unsettled = json.loads(json.dumps(document["members"][0]))
    unsettled["check"]["check"]["id"] = "check-2"
    unsettled["check"]["draft"]["expression"]["op"] = "guess"
    document["members"].append(unsettled)
    rebind(tmp_path, path, document)
    live_app(tmp_path, ["HIGH"])
    result = run_plugin(tmp_path, "--hajer-live")
    assert int((tmp_path / "calls.txt").read_text()) == 5, result.stdout + result.stderr
    checks = json.loads((tmp_path / "results.json").read_text())["suites"][0]["cases"][0]["checks"]
    assert [(check["checkId"], check["label"], len(check["attempts"])) for check in checks] == [
        ("check-1", "qualified", 5),
        ("check-2", "not_yet", 5),
    ]
    assert result.returncode == 1


def test_upload_uses_team_transport_and_propagates_refusal() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201 if len(sent) == 1 else 403)

    settings = HajerSettings(api_key="hjk_fixture", team_id="team", base_url="https://hajer.invalid")
    payload: dict[str, JsonValue] = {"commitSha": None, "mode": "replay", "suites": []}
    assert upload(payload, "project", settings, transport=httpx.MockTransport(handler))
    assert sent[0].url.path == "/api/teams/team/projects/project/suite-runs"
    assert sent[0].headers["Authorization"] == "Bearer hjk_fixture"
    assert json.loads(sent[0].content) == payload
    assert not upload(payload, "project", settings, transport=httpx.MockTransport(handler))


def test_unrecorded_network_attempt_never_passes_even_when_application_catches_it(tmp_path: Path) -> None:
    build_fixture(tmp_path, expected="HIGH")
    (tmp_path / "customer.py").write_text(
        "import httpx\ndef classify(value):\n"
        "    try:\n        httpx.post('https://missing.invalid', content=value)\n"
        "    except Exception:\n        return {'priority': 'HIGH'}\n"
    )
    result = run_plugin(tmp_path)
    assert result.returncode == 1
    assert "UNABLE_TO_VERIFY" in result.stdout


def test_unknown_predicate_never_passes(tmp_path: Path) -> None:
    path = build_fixture(tmp_path, expected="HIGH")
    document = json.loads(path.read_text())
    document["members"][0]["check"]["draft"]["expression"]["op"] = "guess"
    path.write_text(json.dumps(document))
    binding_path = tmp_path / ".hajer/executions/classify.json"
    binding = json.loads(binding_path.read_text())
    binding["suiteDigest"] = _digest(path.read_bytes())
    binding_path.write_text(json.dumps(binding))
    result = run_plugin(tmp_path)
    assert result.returncode == 1
    assert "UNABLE_TO_VERIFY" in result.stdout


def test_early_stop_retains_every_unexecuted_case(tmp_path: Path) -> None:
    path = build_fixture(tmp_path, bound=False)
    document = json.loads(path.read_text())
    second = json.loads(json.dumps(document["members"][0]))
    second["case"]["id"] = "case-2"
    document["members"].append(second)
    path.write_text(json.dumps(document))
    result = run_plugin(tmp_path, "-x")
    assert result.returncode == 1
    cases = json.loads((tmp_path / "results.json").read_text())["suites"][0]["cases"]
    assert [case["caseId"] for case in cases] == ["case-1", "case-2"]
    assert cases[1]["checks"][0]["verdict"] == "UNABLE_TO_VERIFY"
    assert cases[1]["checks"][0]["reason"] == "NOT_EXECUTED"


def test_empty_explicit_suite_directory_cannot_be_green_from_unrelated_tests(tmp_path: Path) -> None:
    (tmp_path / ".hajer/suites").mkdir(parents=True)
    (tmp_path / "test_customer.py").write_text("def test_example():\n    assert True\n")
    result = run_plugin(tmp_path)
    assert result.returncode != 0


def test_exact_suite_selection_keeps_neighbouring_publications_out_of_the_run(tmp_path: Path) -> None:
    selected = build_fixture(tmp_path, expected="HIGH")
    neighbour = selected.parent / "other-workflow.json"
    neighbour.write_text("invalid neighbouring publication")
    result = run_plugin(tmp_path, "--hajer-suites", str(selected))
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((tmp_path / "results.json").read_text())
    assert [suite["suiteId"] for suite in receipt["suites"]] == ["suite-fixture"]
    assert neighbour.read_text() == "invalid neighbouring publication"


@pytest.mark.parametrize(("field", "read"), [("priority", "PASS"), ("other", "FAIL")])
def test_materialized_exists_predicate_checks_the_real_app_result(tmp_path: Path, field: str, read: str) -> None:
    path = build_fixture(tmp_path)
    document = json.loads(path.read_text())
    document["members"][0]["check"]["draft"]["expression"] = {"op": "exists", "path": ["output", "priority"]}
    path.write_text(json.dumps(document))
    binding_path = tmp_path / ".hajer/executions/classify.json"
    binding = json.loads(binding_path.read_text())
    binding["suiteDigest"] = _digest(path.read_bytes())
    binding_path.write_text(json.dumps(binding))
    app = tmp_path / "customer.py"
    app.write_text(app.read_text().replace("'priority'", repr(field)))
    result = run_plugin(tmp_path)
    check = json.loads((tmp_path / "results.json").read_text())["suites"][0]["cases"][0]["checks"][0]
    assert check["attempts"] == [read], result.stdout + result.stderr


@requires_platform
def test_ci_setting_defaults_match_registered_backend_bounds() -> None:
    # This guards a cross-package contract; it is intentionally independent of imports/interpreters.
    source = platform_path("backend/app/engine/s4_evaluation/ci_runs/values.py")
    constants: dict[str, int] = {}
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    assert HajerSettings().ci_case_timeout_seconds == constants["CI_CASE_TIMEOUT_SECONDS"]


def test_the_repeat_cap_and_agreement_are_the_rule_not_settings() -> None:
    # MAX_REPEATS and MIN_AGREEING come from the shared rule (test_qualification_vectors.py); no knob moves them.
    assert {"ci_live_max_attempts", "ci_stable_attempts"}.isdisjoint(HajerSettings.model_fields)


def test_an_earlier_unknown_attempt_stays_in_the_receipt_of_a_settled_pass(tmp_path: Path) -> None:
    build_fixture(tmp_path, expected="HIGH")
    (tmp_path / "customer.py").write_text(
        "from pathlib import Path\n"
        "def classify(value):\n"
        "    counter = Path('attempted')\n"
        "    if not counter.exists():\n"
        "        counter.touch()\n"
        "        raise RuntimeError('fixture failure')\n"
        "    return {'priority': 'HIGH'}\n"
    )
    result = run_plugin(tmp_path, "--hajer-live")
    # U,S,S,S settles on S (`ci_verdict`): the receipt keeps the qualified PASS and the U with its reason, and the
    # retained execution failure fails CI rather than turning green (`_run._reason`, `_policy.disposition`).
    assert result.returncode == 1, result.stdout + result.stderr
    check = json.loads((tmp_path / "results.json").read_text())["suites"][0]["cases"][0]["checks"][0]
    assert check["attempts"] == ["UNABLE_TO_VERIFY", "PASS", "PASS", "PASS"]
    assert check["label"] == "qualified"
    assert check["verdict"] == "PASS"
    assert check["reason"] == "APP_EXECUTION_UNAVAILABLE: RuntimeError"


def _tool_boundary(root: Path, arguments: JsonValue) -> None:
    """Rebind case-1 to an Anthropic tool-use reply: the model's structured form."""
    body = json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "SearchForm", "input": arguments}],
            "stop_reason": "tool_use",
        }
    )
    binding_path = root / ".hajer/executions/classify.json"
    binding = json.loads(binding_path.read_text())
    boundary = binding["cases"]["case-1"]["boundaries"][0]
    boundary.update({"body": body, "bodyDigest": _digest(body.encode())})
    binding_path.write_text(json.dumps(binding))
    (root / "customer.py").write_text(
        "import httpx\n"
        "def classify(value):\n"
        "    response = httpx.post('https://api.example.test/classify', content=value)\n"
        "    return {'form': response.json()['content'][0]['input']}\n"
    )


@pytest.mark.parametrize(("seniority", "read"), [(["vp"], "PASS"), (["VP"], "FAIL")])
def test_a_check_on_the_models_tool_call_arguments_reads_the_recorded_reply(
    tmp_path: Path, seniority: JsonValue, read: str
) -> None:
    """A structured-output rule is checked at `output.tool_calls.<name>.arguments.<field>`, read from the
    case's recorded reply as the platform's verifier reads it — not from the application's return value."""
    path = build_fixture(tmp_path)
    document = json.loads(path.read_text())
    member: dict[str, JsonValue] = {"op": "in", "path": [], "valueJson": json.dumps(["c_suite", "vp", "director"])}
    document["members"][0]["check"]["draft"]["expression"] = {
        "op": "all",
        "path": ["output", "tool_calls", "SearchForm", "arguments", "seniority"],
        "children": [member],
    }
    rebind(tmp_path, path, document)
    _tool_boundary(tmp_path, {"seniority": seniority})
    result = run_plugin(tmp_path)
    check = json.loads((tmp_path / "results.json").read_text())["suites"][0]["cases"][0]["checks"][0]
    assert check["attempts"] == [read], result.stdout + result.stderr

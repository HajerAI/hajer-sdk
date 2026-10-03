"""A suite whose scanned authored inputs Hajer committed beside it (`.hajer/executions/<name>.json`) runs in the
SDK's normal mode, with no temporary bundle and nothing new in the SDK.

`tests/vectors/exported_suite/` holds the bytes the Hajer platform's exporter writes (the platform's own tests rebuild
them byte for byte from `custodian.json`): one case whose input scanned clean and was exported, and one flagged case the exporter refused, so it
has no execution. The input carries non-ASCII text: its bytes in the file are the custodian's canonical bytes.
"""

import hashlib
import json
import shutil
from pathlib import Path

from tests.test_pytest_plugin import ADAPTER_DOUBLE, run_plugin, verified

VECTORS = Path(__file__).with_name("vectors") / "exported_suite"


def _canonical_digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _customer(root: Path) -> None:
    """The customer's app: its model call is a fake provider through the app's own transport (live mode)."""
    (root / "customer.py").write_text(
        "import httpx\n"
        "def classify(request):\n"
        "    answer = 'high' if request['body'] else 'low'\n"
        "    transport = httpx.MockTransport(lambda sent: httpx.Response(200, json={'priority': answer}))\n"
        "    with httpx.Client(transport=transport) as client:\n"
        "        return {'priority': client.post('https://fake.invalid', content=request['body']).json()['priority'].upper()}\n"
    )


def _checkout(root: Path) -> None:
    """The committed files exactly as Hajer's pull request wrote them."""
    (root / ".hajer" / "suites").mkdir(parents=True)
    (root / ".hajer" / "executions").mkdir()
    shutil.copyfile(VECTORS / "suite.json", root / ".hajer" / "suites" / "classify.json")
    shutil.copyfile(VECTORS / "execution.json", root / ".hajer" / "executions" / "classify.json")
    _customer(root)
    verified(root, adapter_id="customer:classify", site="customer.py:6")


def test_the_exported_suite_runs_in_normal_mode_on_the_custodians_bytes(tmp_path: Path) -> None:
    _checkout(tmp_path)
    assert (tmp_path / f"{ADAPTER_DOUBLE}.py").is_file()
    result = run_plugin(tmp_path, "--hajer-live")
    payload = json.loads((tmp_path / "results.json").read_text())
    (suite,) = payload["suites"]
    cases = {case["caseId"]: case for case in suite["cases"]}
    clean, flagged = cases["case-clean"], cases["case-flagged"]
    # The exported case ran on its committed input, and what the child was handed is the custodian's digest.
    assert [check["verdict"] for check in clean["checks"]] == ["PASS"], result.stdout + result.stderr
    custodian = json.loads((VECTORS / "custodian.json").read_text())
    assert {item["inputDigest"] for item in clean["executionEvidence"]} == {custodian["case-clean"]}
    assert custodian["case-clean"] == _canonical_digest(custodian["request"])
    # The flagged case has no execution: it is never a pass, and CI fails on it rather than reading it green.
    (check,) = flagged["checks"]
    assert check["verdict"] == "UNABLE_TO_VERIFY"
    assert check["reason"] == "APP_EXECUTION_UNAVAILABLE: MISSING_OR_STALE_EXECUTION_BINDING"
    assert result.returncode == 1
    # No temporary bundle was used: the run read only the committed sibling file.
    assert "--hajer-temporary-local-executions" not in " ".join(result.args)


def test_a_changed_suite_file_is_not_run_on_a_stale_export(tmp_path: Path) -> None:
    """The execution file is bound to the suite's exact bytes (`suiteDigest`): an edited suite reads no input."""
    _checkout(tmp_path)
    suite = tmp_path / ".hajer" / "suites" / "classify.json"
    suite.write_text(suite.read_text() + "\n")
    run_plugin(tmp_path, "--hajer-live")
    payload = json.loads((tmp_path / "results.json").read_text())
    reasons = {check["reason"] for case in payload["suites"][0]["cases"] for check in case["checks"]}
    assert reasons == {"APP_EXECUTION_UNAVAILABLE: MISSING_OR_STALE_EXECUTION_BINDING"}


def test_a_suite_file_without_cases_is_refused_at_collection(tmp_path: Path) -> None:
    """Why a retired or relocated suite's file is deleted rather than left members-empty."""
    (tmp_path / ".hajer" / "suites").mkdir(parents=True)
    document: dict[str, object] = {
        "schema": "hajer-suite-v1",
        "workflow": "w",
        "version": 2,
        "members": [],
        "retirement": {},
    }
    (tmp_path / ".hajer" / "suites" / "retired.json").write_text(json.dumps(document))
    result = run_plugin(tmp_path)
    assert result.returncode != 0
    assert "Suite identity/version is missing" in result.stdout + result.stderr

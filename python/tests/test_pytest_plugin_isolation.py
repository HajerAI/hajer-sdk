"""The customer's pytest configuration cannot turn a Hajer case green or spend more than five live attempts.

LLM repositories commonly run pytest-rerunfailures, mark tests xfail or reset the exit status. `RERUN_STANDIN`
re-runs a failed item the way pytest-rerunfailures does, reporting the earlier failure with outcome `rerun`; the
real package is not installable offline. The job is never green on UNKNOWN, and at most
five attempts run per case.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.repo import ACTION
from tests.test_pytest_plugin import build_fixture, live_app, run_plugin, suites_action_python

RERUN_STANDIN = """\
from pathlib import Path

import pytest
from _pytest.runner import runtestprotocol

Path("rerun-loaded").touch()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    for remaining in (2, 1, 0):
        reports = runtestprotocol(item, nextitem=nextitem, log=False)
        again = remaining and any(report.failed for report in reports)
        for report in reports:
            if again and report.failed:
                report.outcome = "rerun"
            item.ihook.pytest_runtest_logreport(report=report)
        if not again:
            break
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True
"""
XFAIL_EVERYTHING = """\
import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.xfail(strict=False))
"""
EXIT_ZERO = """\
def pytest_sessionfinish(session):
    session.exitstatus = 0
"""


def _receipt(root: Path) -> list[dict[str, object]]:
    return json.loads((root / "results.json").read_text())["suites"][0]["cases"]


@pytest.mark.parametrize(
    ("values", "attempts"),
    [
        # The case in live form: the first run never settles (five UNKNOWNs), and a re-run of the item
        # would start a fresh history that settles on PASS and turns the job green.
        ([None] * 5 + ["HIGH"] * 5, ["UNABLE_TO_VERIFY"] * 5),
        # Never settles: five attempts, and a re-run would spend five more each time.
        ([None], ["UNABLE_TO_VERIFY"] * 5),
    ],
)
def test_a_rerun_plugin_neither_greens_an_unknown_nor_spends_past_five(
    tmp_path: Path, values: list[str | None], attempts: list[str]
) -> None:
    build_fixture(tmp_path, expected="HIGH")
    live_app(tmp_path, values)
    (tmp_path / "conftest.py").write_text(RERUN_STANDIN)
    result = run_plugin(tmp_path, "--hajer-live")
    assert result.returncode == 1, result.stdout + result.stderr
    assert (tmp_path / "rerun-loaded").exists()
    assert int((tmp_path / "calls.txt").read_text()) == len(attempts) <= 5
    cases = _receipt(tmp_path)
    assert len(cases) == 1
    check = cases[0]["checks"][0]  # pyright: ignore[reportIndexIssue, reportUnknownVariableType]
    assert check["attempts"] == attempts
    assert check["verdict"] == "UNABLE_TO_VERIFY"


@pytest.mark.parametrize("conftest", [XFAIL_EVERYTHING, EXIT_ZERO], ids=["xfail", "exit-zero"])
def test_customer_hooks_cannot_make_a_failed_case_green(tmp_path: Path, conftest: str) -> None:
    build_fixture(tmp_path, expected="low")
    (tmp_path / "conftest.py").write_text(conftest)
    result = run_plugin(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "xfailed" not in result.stdout
    assert _receipt(tmp_path)[0]["checks"][0]["attempts"] == ["FAIL"]  # pyright: ignore[reportIndexIssue]


@pytest.mark.parametrize(("answer", "code"), [("HIGH", 0), ("low", 1)])
def test_run_suites_ignores_the_customers_pytest_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, code: int
) -> None:
    # Live, so a settled PASS can be green: the hostile configuration must neither block that nor mask a FAIL.
    build_fixture(tmp_path, expected="HIGH")
    live_app(tmp_path, [answer])
    (tmp_path / "rerun_standin.py").write_text(RERUN_STANDIN)
    (tmp_path / "conftest.py").write_text('from pathlib import Path\nPath("conftest-loaded").touch()\n' + EXIT_ZERO)
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = -p rerun_standin --maxfail=1\nxfail_strict = false\nrequired_plugins = pytest-rerunfailures\n"
    )
    (tmp_path / "test_customer.py").write_text(
        'from pathlib import Path\n\ndef test_customer():\n    Path("customer-test-ran").touch()\n'
    )
    binaries = suites_action_python(tmp_path)
    action = ACTION
    monkeypatch.setenv("PATH", f"{binaries}:/usr/bin:/bin")
    monkeypatch.setenv("GITHUB_ACTION_PATH", str(action))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p rerun_standin")
    # A report `run-suites.sh` hands on (`--hajer-adapter-checks`) is informational only: the plugin checks the adapter
    # itself, which the fixture's double answers (`tests.test_pytest_plugin.ADAPTER_DOUBLE`).
    (tmp_path / "hajer-adapter-checks.json").write_text((tmp_path / ".hajer" / "adapter-checks.json").read_text())
    monkeypatch.setenv("PYTEST_PLUGINS", "rerun_standin")
    monkeypatch.setenv("HAJER_SUITES_LIVE", "true")
    for name in ("HAJER_API_KEY", "HAJER_TEAM_ID", "HAJER_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    result = subprocess.run(["/bin/bash", str(action / "run-suites.sh")], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == code, result.stdout + result.stderr
    for marker in ("rerun-loaded", "conftest-loaded", "customer-test-ran"):
        assert not (tmp_path / marker).exists(), marker
    cases = json.loads((tmp_path / "hajer-suite-results.json").read_text())["suites"][0]["cases"]
    assert [case["caseId"] for case in cases] == ["case-1"]


def _records_its_environment(root: Path) -> None:
    app = root / "customer.py"
    app.write_text(
        app.read_text().replace(
            "def classify(value):\n",
            "def classify(value):\n"
            "    import os\n"
            "    Path('environment.txt').write_text(os.environ.get('HAJER_ENVIRONMENT', 'absent'))\n",
        )
    )


def test_the_case_process_tags_its_traffic_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CI traffic is tagged `ci` so it is never selected as a test input, whatever the customer's CI exports.
    build_fixture(tmp_path, expected="HIGH")
    live_app(tmp_path, ["HIGH"])
    _records_its_environment(tmp_path)
    monkeypatch.setenv("HAJER_ENVIRONMENT", "production")
    result = run_plugin(tmp_path, "--hajer-live")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "environment.txt").read_text() == "ci"


def test_run_suites_tags_its_traffic_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_fixture(tmp_path, expected="HIGH")
    live_app(tmp_path, ["HIGH"])
    _records_its_environment(tmp_path)
    binaries = suites_action_python(tmp_path, f'echo "$HAJER_ENVIRONMENT" > "{tmp_path}/pytest-environment.txt"\n')
    action = ACTION
    monkeypatch.setenv("PATH", f"{binaries}:/usr/bin:/bin")
    monkeypatch.setenv("GITHUB_ACTION_PATH", str(action))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("HAJER_SUITES_LIVE", "true")
    monkeypatch.setenv("HAJER_ENVIRONMENT", "production")
    result = subprocess.run(["/bin/bash", str(action / "run-suites.sh")], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "pytest-environment.txt").read_text() == "ci\n"
    assert (tmp_path / "environment.txt").read_text() == "ci"


@pytest.mark.parametrize(("body", "code", "outcome"), [("assert True", 0, "1 passed"), ("assert False", 1, "1 failed")])
def test_a_plain_pytest_in_an_onboarded_repo_runs_only_the_customers_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, code: int, outcome: str
) -> None:
    # The pytest11 entry point loads the plugin into every pytest of an environment with the SDK installed; without
    # --hajer-suites it collects no Hajer case, changes no outcome and writes or uploads nothing.
    build_fixture(tmp_path, expected="low")
    (tmp_path / "test_customer.py").write_text(f"def test_customer():\n    {body}\n")
    for name in ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HAJER_API_KEY", "hjk_fixture")
    monkeypatch.setenv("HAJER_TEAM_ID", "team")
    monkeypatch.setenv("HAJER_BASE_URL", "https://hajer.invalid")

    def pytest_in_repo(*options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the current interpreter against a local fixture repository
            [sys.executable, "-m", "pytest", *options], cwd=tmp_path, capture_output=True, text=True, check=False
        )

    assert "--hajer-suites" in pytest_in_repo("--help").stdout  # the entry point did load the plugin
    result = pytest_in_repo("-q")
    assert result.returncode == code, result.stdout + result.stderr
    assert outcome in result.stdout
    assert "classify.json" not in result.stdout
    assert "hajer case" not in result.stdout
    assert not (tmp_path / ".hajer/results.json").exists()


XDIST_STANDIN = """\
def pytest_addoption(parser):
    # pytest-xdist registers -n the same way: lowercase short options are reserved to plugins' private adder.
    parser.getgroup("xdist")._addoption("-n", "--numprocesses", dest="numprocesses", default=None)
"""


def test_application_child_cannot_read_the_parents_hajer_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_fixture(tmp_path, expected="HIGH")
    monkeypatch.setenv("HAJER_API_KEY", "parent-only-synthetic-secret")
    path = tmp_path / "customer.py"
    path.write_text(
        "import os\n"
        + path.read_text().replace(
            "def classify(value):", "def classify(value):\n    assert 'HAJER_API_KEY' not in os.environ"
        )
    )
    result = run_plugin(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "parent-only-synthetic-secret" not in (tmp_path / "results.json").read_text()


def test_a_hajer_run_refuses_to_be_split_across_xdist_workers(tmp_path: Path) -> None:
    build_fixture(tmp_path, expected="HIGH")
    (tmp_path / "conftest.py").write_text(XDIST_STANDIN)
    result = run_plugin(tmp_path, "-n", "2")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "pytest-xdist" in result.stderr
    assert not (tmp_path / "results.json").exists()

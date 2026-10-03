"""Run the suites shell against a real plugin; stub only package installation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.repo import ACTION
from tests.test_pytest_plugin import build_fixture, suites_action_python


def test_committed_fixture_replays_without_provider_or_hajer_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = ACTION / "fixtures" / "support"
    for name in ("HAJER_API_KEY", "HAJER_TEAM_ID", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    result = subprocess.run(  # noqa: S603 - real committed fixture, SDK replay blocks unrecorded egress
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "hajer.pytest_plugin",
            "--hajer-suites",
            str(fixture / ".hajer/suites"),
            "--hajer-results",
            str(tmp_path / "results.json"),
            "-q",
        ],
        cwd=fixture,
        capture_output=True,
        text=True,
        check=False,
    )
    # The committed app runs on its recorded answer and reads PASS: a replay's single read decides the job, and its
    # label stays not_yet because nothing was repeated live.
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads((tmp_path / "results.json").read_text())
    check = payload["suites"][0]["cases"][0]["checks"][0]
    assert (payload["mode"], check["attempts"], check["label"], check["verdict"]) == (
        "replay",
        ["PASS"],
        "not_yet",
        "PASS",
    )


def test_suites_action_needs_no_investigation_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_fixture(tmp_path, expected="HIGH")
    binaries = suites_action_python(tmp_path)
    action = ACTION
    monkeypatch.setenv("PATH", f"{binaries}:/usr/bin:/bin")
    monkeypatch.setenv("GITHUB_ACTION_PATH", str(action))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_HEAD_REF", "feature/refunds")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    # A report `run-suites.sh` hands on (`--hajer-adapter-checks`) is informational only: the plugin checks the adapter
    # itself, which the fixture's double answers (`tests.test_pytest_plugin.ADAPTER_DOUBLE`).
    (tmp_path / "hajer-adapter-checks.json").write_text((tmp_path / ".hajer" / "adapter-checks.json").read_text())
    for name in ("HAJER_API_KEY", "HAJER_TEAM_ID", "HAJER_PROJECT_ID", "HAJER_CREDENTIAL_FILE", "HAJER_BUDGET_FILE"):
        monkeypatch.delenv(name, raising=False)
    result = subprocess.run(["/bin/bash", str(action / "run-suites.sh")], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr
    summary = (tmp_path / "summary.md").read_text()
    assert "mode=replay" in summary
    assert "| check-1 | PASS | not_yet | PASS |" in summary
    assert '"commitSha": "' + "a" * 40 + '"' in (tmp_path / "hajer-suite-results.json").read_text()
    assert '"branch": "feature/refunds"' in (tmp_path / "hajer-suite-results.json").read_text()

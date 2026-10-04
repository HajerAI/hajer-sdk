"""`python -m hajer doctor` — the first command to run when nothing is arriving.

The claim under test is the one that makes the command worth having: **every setting is printed with
where its value came from**. A value on its own cannot tell a variable somebody forgot to set from a
default that happens to match it, and that confusion is most of what "the SDK isn't working" turns out
to be. The keyless probe goes through the same injectable transport as everything else, so no test here
opens a socket.
"""

from __future__ import annotations

import json
from typing import Final, cast

import httpx
import pytest

import hajer
from hajer.__main__ import doctor, main, redaction_catalog
from hajer._json import JsonObject
from hajer._paths import HEALTH_PATH, VERSION
from hajer._settings import VARIABLES, HajerSettings
from hajer._transport import probe

SECRET: Final[str] = "sk-not-a-real-key-0123456789"


def _answers(status: int) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == HEALTH_PATH
        assert "Authorization" not in request.headers, "the probe is keyless: that is what it is for"
        return httpx.Response(status, json={"status": "ok"})

    return httpx.MockTransport(handle)


def _refuses() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handle)


def _rows(printed: str) -> dict[str, tuple[str, str]]:
    """The settings table, as `{variable: (source, value)}`. Four whitespace-separated columns."""
    found: dict[str, tuple[str, str]] = {}
    for line in printed.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[1].startswith("HAJER_"):
            found[parts[1]] = (parts[2], parts[3])
    return found


class TestDoctor:
    def test_doctor_reports_each_setting_with_its_source(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every `HAJER_*` variable, its value in force, and `env` or `default` beside it."""
        monkeypatch.setenv("HAJER_BASE_URL", "https://hajer.test")
        monkeypatch.setenv("HAJER_CAPTURE_CONTENT", "yes")
        monkeypatch.setenv("HAJER_TEAM_ID", "team-1")
        monkeypatch.delenv("HAJER_EVAL_RUNS_KEEP", raising=False)
        monkeypatch.delenv("HAJER_ATTACH", raising=False)

        assert doctor(as_json=False, settings=HajerSettings.from_env(), transport=_answers(200)) == 0
        printed = capsys.readouterr().out
        rows = _rows(printed)

        for name, variable in VARIABLES:
            assert variable in rows, f"{name} was not printed"
        assert rows["HAJER_BASE_URL"] == ("env", "https://hajer.test")
        assert rows["HAJER_TEAM_ID"] == ("env", "team-1")
        assert rows["HAJER_CAPTURE_CONTENT"] == ("env", "true"), "a boolean is printed one way whatever was set"
        assert rows["HAJER_EVAL_RUNS_KEEP"] == ("default", "20")
        assert rows["HAJER_ATTACH"] == ("default", "false")
        assert f"hajer {VERSION}" in printed
        assert redaction_catalog() in printed

    def test_the_key_is_never_printed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A key in a terminal is a key in a scrollback buffer. `set` is the whole answer."""
        monkeypatch.setenv("HAJER_API_KEY", SECRET)
        monkeypatch.setenv("HAJER_TEAM_ID", "team-1")
        doctor(as_json=True, settings=HajerSettings.from_env(), transport=_answers(200))
        printed = capsys.readouterr().out
        assert SECRET not in printed
        assert '"value": "set"' in printed

    def test_json_is_one_document_carrying_the_same_facts(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HAJER_BASE_URL", "https://hajer.test")
        monkeypatch.delenv("HAJER_API_KEY", raising=False)
        monkeypatch.delenv("HAJER_TEAM_ID", raising=False)
        assert doctor(as_json=True, settings=HajerSettings.from_env(), transport=_answers(401)) == 0
        report = _parsed(capsys.readouterr().out)
        assert report["version"] == VERSION
        assert report["baseUrl"] == "https://hajer.test"
        assert report["baseUrlAnswers"] is True, "a 401 is an answer: something is listening"
        assert report["baseUrlStatus"] == 401
        assert report["inert"] is True
        because = report["inertBecause"]
        assert isinstance(because, str)
        assert "HAJER_API_KEY and HAJER_TEAM_ID" in because
        rows = report["settings"]
        assert isinstance(rows, list)
        assert len(rows) == len(VARIABLES)

    def test_a_base_url_that_does_not_answer_is_said_so_rather_than_raised(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The wrong URL is the commonest cause, so it is a line of output and never a traceback."""
        monkeypatch.setenv("HAJER_BASE_URL", "https://nothing.invalid")
        assert doctor(as_json=True, settings=HajerSettings.from_env(), transport=_refuses()) == 0
        report = _parsed(capsys.readouterr().out)
        assert report["baseUrlAnswers"] is False
        assert report["baseUrlStatus"] is None

    def test_an_inert_client_says_which_variable_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HAJER_API_KEY", SECRET)
        monkeypatch.delenv("HAJER_TEAM_ID", raising=False)
        doctor(as_json=True, settings=HajerSettings.from_env(), transport=_answers(200))
        report = _parsed(capsys.readouterr().out)
        because = report["inertBecause"]
        assert isinstance(because, str)
        assert because.startswith("HAJER_TEAM_ID absent")

    def test_disabled_outranks_a_present_key(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HAJER_API_KEY", SECRET)
        monkeypatch.setenv("HAJER_TEAM_ID", "team-1")
        monkeypatch.setenv("HAJER_DISABLED", "on")
        doctor(as_json=True, settings=HajerSettings.from_env(), transport=_answers(200))
        report = _parsed(capsys.readouterr().out)
        assert report["inert"] is True
        because = report["inertBecause"]
        assert isinstance(because, str)
        assert because.startswith("HAJER_DISABLED is set")

    def test_the_redaction_catalog_is_reported_either_way(self) -> None:
        """This build may or may not carry the generated catalog; `doctor` answers in both cases."""
        reported = redaction_catalog()
        assert reported
        assert reported.startswith("absent") or "hajer._rules" in reported or len(reported) > 1


class TestTheSubcommand:
    def test_doctor_is_wired_into_the_command_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`python -m hajer doctor --json` reaches `doctor` with the environment's own settings."""
        monkeypatch.setenv("HAJER_BASE_URL", "https://hajer.test")
        monkeypatch.setattr("hajer.__main__.probe", _answered)
        assert main(["doctor", "--json"]) == 0
        report = _parsed(capsys.readouterr().out)
        assert report["baseUrlStatus"] == 200
        assert report["baseUrlPath"] == HEALTH_PATH

    def test_the_prose_form_needs_no_flag(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("hajer.__main__.probe", _unanswered)
        assert main(["doctor"]) == 0
        printed = capsys.readouterr().out
        assert "no answer" in printed
        assert "setting" in printed


def _answered(*_: object, **__: object) -> int:
    return 200


def _unanswered(*_: object, **__: object) -> int | None:
    return None


def _parsed(printed: str) -> JsonObject:
    parsed: object = json.loads(printed)
    assert isinstance(parsed, dict)
    return cast(JsonObject, parsed)


def test_the_probe_sends_no_credential_and_returns_none_when_nothing_answers() -> None:
    """The transport seam of the probe itself, asserted once rather than in every doctor test."""
    assert probe("https://hajer.test", HEALTH_PATH, timeout_ms=50, transport=_answers(503)) == 503
    assert probe("https://hajer.test", HEALTH_PATH, timeout_ms=50, transport=_refuses()) is None
    assert hajer.HajerSettings().base_url.startswith("https://"), "the default is the hosted service"

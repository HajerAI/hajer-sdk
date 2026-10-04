"""`HAJER_ENVIRONMENT`: every span this process emits says which environment it came from.

It is lower-case, `^[a-z0-9][a-z0-9-]{0,63}$`; a value outside that is dropped and `doctor` says so, because a
tag is a hint and a hint never costs the call it rides on (the SDK does not raise into application code). That the
tag reaches the spans is `tests/test_telemetry.py`'s claim; this file is about the setting itself.
"""

from __future__ import annotations

import pytest

from hajer.__main__ import doctor
from hajer._settings import HajerSettings, settings_sources
from tests.test_cli import _answers, _rows  # pyright: ignore[reportPrivateUsage]

_CREDENTIALS = {"HAJER_API_KEY": "key-for-tests", "HAJER_TEAM_ID": "team-1", "HAJER_BASE_URL": "https://hajer.test"}


class TestTheSetting:
    def test_a_tagged_process_keeps_its_environment(self) -> None:
        settings = HajerSettings.from_env({**_CREDENTIALS, "HAJER_ENVIRONMENT": "staging"})
        assert settings.environment == "staging"

    def test_an_untagged_process_has_none(self) -> None:
        assert HajerSettings.from_env(_CREDENTIALS).environment is None

    def test_the_spelling_is_folded_to_lower_case(self) -> None:
        assert HajerSettings.from_env({"HAJER_ENVIRONMENT": "  Production "}).environment == "production"
        assert HajerSettings(environment="Staging").environment == "staging"

    @pytest.mark.parametrize("value", ["Bad Env", "-staging", "prod_1", "x" * 65, "ünïcode"])
    def test_an_invalid_value_is_dropped_and_never_raised(self, value: str) -> None:
        settings = HajerSettings.from_env({**_CREDENTIALS, "HAJER_ENVIRONMENT": value})

        assert settings.environment is None
        assert HajerSettings(environment=value).environment is None, "a constructor is held to the same rule"


class TestDoctor:
    def test_an_invalid_value_is_reported_as_ignored(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HAJER_ENVIRONMENT", "Bad Env")
        settings = HajerSettings.from_env()

        assert doctor(as_json=False, settings=settings, transport=_answers(200)) == 0

        assert _rows(capsys.readouterr().out)["HAJER_ENVIRONMENT"] == ("env", "ignored: invalid")
        (row,) = (row for row in settings_sources(settings) if row.variable == "HAJER_ENVIRONMENT")
        assert f"{row.name} {row.value}" == "environment ignored: invalid"

    def test_a_valid_value_is_reported_as_it_is_in_force(self) -> None:
        environment = {"HAJER_ENVIRONMENT": "Staging"}
        rows = {row.variable: row for row in settings_sources(HajerSettings.from_env(environment), environment)}
        assert (rows["HAJER_ENVIRONMENT"].source, rows["HAJER_ENVIRONMENT"].value) == ("env", "staging")
        absent = {row.variable: row for row in settings_sources(HajerSettings.from_env({}), {})}
        assert (absent["HAJER_ENVIRONMENT"].source, absent["HAJER_ENVIRONMENT"].value) == ("default", "absent")

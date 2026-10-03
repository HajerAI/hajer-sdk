"""Settings, and the inert mode a missing key produces."""

from __future__ import annotations

import subprocess
import sys
import traceback
from pathlib import Path

import pytest

import hajer
from hajer._settings import BOOLEAN_FIELDS, DEFAULT_BASE_URL, VARIABLES, ci_execution_environment, settings_sources


def test_replay_configuration_cannot_override_budget_or_leak_hajer_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("HAJER_API_KEY", "synthetic-team-secret")
    settings = hajer.HajerSettings(ci_budget_microusd=100, ci_budget_file=str(tmp_path / "budget.sqlite"))
    declared = {"DATABASE_URL": "unused", "HAJER_CI_BUDGET_MICROUSD": "999999", "HAJER_API_KEY": "bad"}
    replay = ci_execution_environment(declared, live=False, settings=settings)
    assert replay["DATABASE_URL"] == "unused"
    assert replay["HAJER_CI_BUDGET_MICROUSD"] == "100"
    assert "HAJER_API_KEY" not in replay
    assert replay["OPENAI_API_KEY"] == "hajer-verify-adapters-no-key"
    assert "OPENAI_API_KEY" not in ci_execution_environment(declared, live=True, settings=settings)


class TestFromEnv:
    @pytest.mark.parametrize("option", [[], ["--json"]])
    def test_cli_does_not_echo_an_invalid_secret_value(self, option: list[str]) -> None:
        canary = "sk-synthetic-cli-canary@example.test"
        result = subprocess.run(  # noqa: S603 - own CLI, with a synthetic value; validation occurs before networking
            [sys.executable, "-m", "hajer", "doctor", *option],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1]), "HAJER_DEADLINE_MS_DEFAULT": canary},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0
        assert canary not in result.stdout + result.stderr
        assert "HAJER_INVALID_CONFIGURATION" in result.stderr
        assert "HAJER_DEADLINE_MS_DEFAULT" in result.stderr

    @pytest.mark.parametrize(
        "variable",
        ["HAJER_DEADLINE_MS_DEFAULT", "HAJER_CI_BUDGET_MICROUSD", "HAJER_CAPTURE_CONTENT"],
    )
    def test_invalid_values_never_enter_error_messages_attributes_or_chains(self, variable: str) -> None:
        canary = "sk-synthetic-canary person@example.test"
        with pytest.raises(hajer.HajerConfigError) as raised:
            hajer.HajerSettings.from_env({variable: canary})
        error = raised.value
        assert error.variable == variable
        assert error.code == "HAJER_INVALID_CONFIGURATION"
        assert error.value == "[withheld]"
        assert canary not in str(error)
        assert canary not in repr(error)
        assert canary not in repr(error.args)
        assert canary not in repr(vars(error))
        assert canary not in "".join(traceback.format_exception(error))
        assert error.__cause__ is None
        assert error.__context__ is None
        assert variable in str(error)
        assert error.expected in str(error)

    def test_invalid_numeric_value_is_withheld_too(self) -> None:
        with pytest.raises(hajer.HajerConfigError) as raised:
            hajer.HajerSettings.from_env({"HAJER_DEADLINE_MS_DEFAULT": "-9876543210"})
        assert "9876543210" not in str(raised.value)
        assert raised.value.expected == "a positive integer"

    def test_reads_every_documented_variable(self) -> None:
        settings = hajer.HajerSettings.from_env(
            {
                "HAJER_API_KEY": "k",
                "HAJER_TEAM_ID": "team-9",
                "HAJER_BASE_URL": "http://localhost:8000",
                "HAJER_DEADLINE_MS_DEFAULT": "900",
                "HAJER_OBSERVE_QUEUE_MAX": "7",
                "HAJER_OBSERVE_FLUSH_INTERVAL_MS": "111",
                "HAJER_OBSERVE_BATCH_MAX": "3",
                "HAJER_OBSERVE_FLUSH_DEADLINE_MS": "2500",
                "HAJER_OBSERVE_BACKOFF_INITIAL_MS": "50",
                "HAJER_OBSERVE_BACKOFF_MAX_MS": "800",
                "HAJER_BODY_MAX_BYTES": "1024",
                "HAJER_CAPTURE_CONTENT": "yes",
                "HAJER_WRAPPED_CALLS_MAX": "4",
                "HAJER_DISABLED": "off",
            }
        )
        assert settings.api_key == "k"
        assert settings.team_id == "team-9"
        assert settings.base_url == "http://localhost:8000"
        assert settings.deadline_ms_default == 900
        assert settings.observe_queue_max == 7
        assert settings.observe_flush_interval_ms == 111
        assert settings.observe_batch_max == 3
        assert settings.observe_flush_deadline_ms == 2500
        assert settings.observe_backoff_initial_ms == 50
        assert settings.observe_backoff_max_ms == 800
        assert settings.body_max_bytes == 1024
        assert settings.capture_content is True
        assert settings.wrapped_calls_max == 4
        assert settings.disabled is False
        assert settings.inert is False

    def test_empty_environment_has_documented_defaults_and_is_inert(self) -> None:
        settings = hajer.HajerSettings.from_env({})
        assert settings.base_url == DEFAULT_BASE_URL
        assert settings.deadline_ms_default == 1_500
        assert settings.observe_queue_max == 1_000
        assert settings.observe_flush_interval_ms == 2_000
        assert settings.observe_batch_max == 32
        assert settings.body_max_bytes == 65_536
        assert settings.capture_content is True
        assert settings.wrapped_calls_max == 32
        assert settings.inert is True

    def test_blank_values_are_absent_not_empty(self) -> None:
        settings = hajer.HajerSettings.from_env({"HAJER_API_KEY": "   ", "HAJER_TEAM_ID": "t"})
        assert settings.api_key is None
        assert settings.inert is True

    @pytest.mark.parametrize("value", ["abc", "1.5", "1_000ms"])
    def test_a_non_integer_bound_is_a_configuration_error(self, value: str) -> None:
        with pytest.raises(hajer.HajerConfigError) as raised:
            hajer.HajerSettings.from_env({"HAJER_DEADLINE_MS_DEFAULT": value})
        assert raised.value.variable == "HAJER_DEADLINE_MS_DEFAULT"
        assert raised.value.expected == "an integer"

    def test_a_non_positive_bound_is_a_configuration_error(self) -> None:
        with pytest.raises(hajer.HajerConfigError) as raised:
            hajer.HajerSettings.from_env({"HAJER_OBSERVE_BATCH_MAX": "0"})
        assert raised.value.expected == "a positive integer"

    def test_a_non_boolean_switch_is_a_configuration_error(self) -> None:
        with pytest.raises(hajer.HajerConfigError) as raised:
            hajer.HajerSettings.from_env({"HAJER_CAPTURE_CONTENT": "maybe"})
        assert raised.value.variable == "HAJER_CAPTURE_CONTENT"

    @pytest.mark.parametrize(("raw", "expected"), [("1", True), ("TRUE", True), ("on", True), ("n", False)])
    def test_boolean_spellings(self, raw: str, expected: bool) -> None:
        assert hajer.HajerSettings.from_env({"HAJER_CAPTURE_CONTENT": raw}).capture_content is expected

    def test_settings_are_frozen(self) -> None:
        settings = hajer.HajerSettings.from_env({})
        with pytest.raises(ValueError, match="frozen"):
            settings.body_max_bytes = 1


class TestInertMode:
    """A missing key must never raise and never send. A customer's tests run unchanged."""

    def test_verify_is_unavailable_disabled(self) -> None:
        with hajer.Hajer(settings=hajer.HajerSettings.from_env({})) as client:
            assert client.inert is True
            assessment = client.verify("refund-policy", {"q": 1}, "reply", {"orderState": "paid"})
        assert assessment.status == "unavailable"
        assert assessment.reason == "DISABLED"
        assert assessment.decided_locally is True

    def test_observe_is_a_no_op_receipt(self) -> None:
        with hajer.Hajer(settings=hajer.HajerSettings.from_env({})) as client:
            receipt = client.observe("refund-policy", {"q": 1}, "reply")
            assert client.queue_depth() == 0
        assert receipt.state == "disabled"
        assert receipt.accepted is False
        assert receipt.poll() is None

    def test_explicitly_disabled_with_a_key_present(self) -> None:
        settings = hajer.HajerSettings.from_env({"HAJER_API_KEY": "k", "HAJER_TEAM_ID": "t", "HAJER_DISABLED": "1"})
        with hajer.Hajer(settings=settings) as client:
            assert client.verify("v", {}, "out").reason == "DISABLED"

    def test_a_missing_team_is_also_inert(self) -> None:
        settings = hajer.HajerSettings.from_env({"HAJER_API_KEY": "k"})
        assert settings.inert is True

    async def test_async_client_is_inert_too(self) -> None:
        async with hajer.AsyncHajer(settings=hajer.HajerSettings.from_env({})) as client:
            assessment = await client.verify("v", {}, "out")
            receipt = client.observe("v", {}, "out")
        assert assessment.reason == "DISABLED"
        assert receipt.state == "disabled"
        assert await receipt.poll() is None

    def test_raise_on_unavailable_is_opt_in(self) -> None:
        settings = hajer.HajerSettings.from_env({})
        with (
            hajer.Hajer(settings=settings, raise_on_unavailable=True) as client,
            pytest.raises(hajer.AssessmentUnavailableError) as raised,
        ):
            client.verify("v", {}, "out")
        assert raised.value.assessment.reason == "DISABLED"


class TestTheVariableTable:
    """`VARIABLES` is what `python -m hajer doctor` prints and what `from_env` reads.

    One table, or neither can be trusted: a field with no variable is a bound nobody can change, and a
    variable with no field is a line of documentation that does nothing.
    """

    def test_every_setting_has_a_variable(self) -> None:
        declared = {name for name, _ in VARIABLES}
        assert declared == set(hajer.HajerSettings.model_fields), "a setting was added without its variable"
        assert len({variable for _, variable in VARIABLES}) == len(VARIABLES), "two fields share one variable"
        assert all(variable.startswith("HAJER_") for _, variable in VARIABLES)

    def test_boolean_spellings(self) -> None:
        """Every boolean setting reads every spelling, and they are the same spellings for all four.

        A customer who learned `HAJER_CAPTURE_CONTENT=true` must not discover that `HAJER_ATTACH` wanted
        `1`. Asserted per field rather than on the shared helper, because the helper being shared is
        exactly the thing that could quietly stop being true.
        """
        for field in BOOLEAN_FIELDS:
            variable = f"HAJER_{field.upper()}"
            for spelling in ("1", "true", "TRUE", "t", "yes", "Y", "on", "ON"):
                # capture_raw refuses to be on while capture_content is off, so it is asked for as well.
                settings = hajer.HajerSettings.from_env({variable: spelling, "HAJER_CAPTURE_CONTENT": spelling})
                assert getattr(settings, field) is True, f"{variable}={spelling} was not read as true"
            for spelling in ("0", "false", "FALSE", "f", "no", "N", "off", "OFF"):
                settings = hajer.HajerSettings.from_env({variable: spelling})
                assert getattr(settings, field) is False, f"{variable}={spelling} was not read as false"

    def test_an_unreadable_boolean_names_the_variable_and_the_spellings(self) -> None:
        with pytest.raises(hajer.HajerConfigError) as raised:
            hajer.HajerSettings.from_env({"HAJER_ATTACH": "maybe"})
        assert "HAJER_ATTACH" in str(raised.value)
        assert "1/0" in str(raised.value)

    def test_the_sources_say_env_or_default_and_never_the_key(self) -> None:
        environment = {"HAJER_API_KEY": "sk-secret-value", "HAJER_TAIL_LIMIT": "5"}
        settings = hajer.HajerSettings.from_env(environment)
        rows = {row.variable: row for row in settings_sources(settings, environment)}
        assert rows["HAJER_API_KEY"].source == "env"
        assert rows["HAJER_API_KEY"].value == "set", "the value of a key is never rendered"
        assert rows["HAJER_TAIL_LIMIT"].source == "env"
        assert rows["HAJER_TAIL_LIMIT"].value == "5"
        assert rows["HAJER_TAIL_LIMIT"].name == "tail_limit"
        assert rows["HAJER_BASE_URL"].source == "default"
        assert rows["HAJER_BASE_URL"].value == DEFAULT_BASE_URL
        assert rows["HAJER_TEAM_ID"].value == "absent"

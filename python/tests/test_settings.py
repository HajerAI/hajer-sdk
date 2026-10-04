"""Settings, and the inert mode a missing key produces."""

from __future__ import annotations

import subprocess
import sys
import traceback
from pathlib import Path

import pytest

import hajer
from hajer._settings import BOOLEAN_FIELDS, DEFAULT_BASE_URL, VARIABLES, settings_sources


class TestFromEnv:
    @pytest.mark.parametrize("option", [[], ["--json"]])
    def test_cli_does_not_echo_an_invalid_secret_value(self, option: list[str]) -> None:
        canary = "sk-synthetic-cli-canary@example.test"
        result = subprocess.run(  # noqa: S603 - own CLI, with a synthetic value; validation occurs before networking
            [sys.executable, "-m", "hajer", "doctor", *option],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1]), "HAJER_WRAPPED_CALLS_MAX": canary},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0
        assert canary not in result.stdout + result.stderr
        assert "HAJER_INVALID_CONFIGURATION" in result.stderr
        assert "HAJER_WRAPPED_CALLS_MAX" in result.stderr

    @pytest.mark.parametrize(
        "variable",
        ["HAJER_WRAPPED_CALLS_MAX", "HAJER_EVAL_UPLOAD_ATTEMPTS", "HAJER_CAPTURE_CONTENT"],
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
            hajer.HajerSettings.from_env({"HAJER_WRAPPED_CALLS_MAX": "-9876543210"})
        assert "9876543210" not in str(raised.value)
        assert raised.value.expected == "a positive integer"

    def test_reads_every_documented_variable(self) -> None:
        settings = hajer.HajerSettings.from_env(
            {
                "HAJER_API_KEY": "k",
                "HAJER_TEAM_ID": "team-9",
                "HAJER_BASE_URL": "http://localhost:8000",
                "HAJER_EVAL_UPLOAD_ATTEMPTS": "9",
                "HAJER_EVAL_UPLOAD_BACKOFF_INITIAL_MS": "50",
                "HAJER_EVAL_UPLOAD_BACKOFF_MAX_MS": "800",
                "HAJER_BODY_MAX_BYTES": "1024",
                "HAJER_CAPTURE_CONTENT": "yes",
                "HAJER_WRAPPED_CALLS_MAX": "4",
                "HAJER_TRACES_ENABLED": "no",
                "HAJER_OTLP_HEADERS": "x-team=team%201,x-empty=",
                "HAJER_SERVICE_NAME": "support-api",
                "HAJER_TRACE_EXPORT_TIMEOUT_MS": "750",
                "HAJER_TRACE_BATCH_DELAY_MS": "100",
                "HAJER_TRACE_QUEUE_MAX": "16",
                "HAJER_TRACE_BATCH_MAX": "8",
                "HAJER_DISABLED": "off",
            }
        )
        assert settings.api_key == "k"
        assert settings.team_id == "team-9"
        assert settings.base_url == "http://localhost:8000"
        assert settings.eval_upload_attempts == 9
        assert settings.eval_upload_backoff_initial_ms == 50
        assert settings.eval_upload_backoff_max_ms == 800
        assert settings.body_max_bytes == 1024
        assert settings.capture_content is True
        assert settings.wrapped_calls_max == 4
        assert settings.traces_enabled is False
        assert settings.otlp_header_values == {"x-team": "team 1", "x-empty": ""}
        assert settings.service_name == "support-api"
        assert (settings.trace_export_timeout_ms, settings.trace_batch_delay_ms) == (750, 100)
        assert (settings.trace_queue_max, settings.trace_batch_max) == (16, 8)
        assert settings.disabled is False
        assert settings.inert is False

    def test_the_standard_otel_variables_are_fallbacks(self) -> None:
        settings = hajer.HajerSettings.from_env(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318", "OTEL_SERVICE_NAME": "svc"}
        )
        assert (settings.otlp_endpoint, settings.service_name) == ("http://collector:4318", "svc")
        own = hajer.HajerSettings.from_env(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
                "HAJER_OTLP_ENDPOINT": "http://mine:4318",
                "OTEL_SERVICE_NAME": "svc",
                "HAJER_SERVICE_NAME": "mine",
            }
        )
        assert (own.otlp_endpoint, own.service_name) == ("http://mine:4318", "mine")

    def test_malformed_header_pairs_are_skipped_not_refused(self) -> None:
        settings = hajer.HajerSettings(otlp_headers="no-equals, =empty-key ,ok=1")
        assert settings.otlp_header_values == {"ok": "1"}
        assert hajer.HajerSettings().otlp_header_values == {}

    def test_empty_environment_has_documented_defaults_and_is_inert(self) -> None:
        settings = hajer.HajerSettings.from_env({})
        assert settings.base_url == DEFAULT_BASE_URL
        assert settings.eval_upload_attempts == 3
        assert settings.eval_upload_backoff_initial_ms == 200
        assert settings.eval_upload_backoff_max_ms == 30_000
        assert settings.traces_enabled is True
        assert (settings.otlp_endpoint, settings.otlp_headers, settings.service_name) == (None, None, None)
        assert (settings.trace_export_timeout_ms, settings.trace_batch_delay_ms) == (5_000, 5_000)
        assert (settings.trace_queue_max, settings.trace_batch_max) == (2_048, 128)
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
            hajer.HajerSettings.from_env({"HAJER_WRAPPED_CALLS_MAX": value})
        assert raised.value.variable == "HAJER_WRAPPED_CALLS_MAX"
        assert raised.value.expected == "an integer"

    def test_a_non_positive_bound_is_a_configuration_error(self) -> None:
        with pytest.raises(hajer.HajerConfigError) as raised:
            hajer.HajerSettings.from_env({"HAJER_EVAL_UPLOAD_ATTEMPTS": "0"})
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

    def test_an_empty_environment_is_inert(self) -> None:
        assert hajer.HajerSettings.from_env({}).inert is True

    def test_explicitly_disabled_with_a_key_present(self) -> None:
        settings = hajer.HajerSettings.from_env({"HAJER_API_KEY": "k", "HAJER_TEAM_ID": "t", "HAJER_DISABLED": "1"})
        assert settings.inert is True

    def test_a_missing_team_is_also_inert(self) -> None:
        settings = hajer.HajerSettings.from_env({"HAJER_API_KEY": "k"})
        assert settings.inert is True

    def test_a_key_and_a_team_are_not_inert(self) -> None:
        settings = hajer.HajerSettings.from_env({"HAJER_API_KEY": "k", "HAJER_TEAM_ID": "t"})
        assert settings.inert is False


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
        """Every boolean setting reads every spelling, and they are the same spellings for all of them.

        A customer who learned `HAJER_CAPTURE_CONTENT=true` must not discover that `HAJER_ATTACH` wanted
        `1`. Asserted per field rather than on the shared helper, because the helper being shared is
        exactly the thing that could quietly stop being true.
        """
        for field in BOOLEAN_FIELDS:
            variable = f"HAJER_{field.upper()}"
            for spelling in ("1", "true", "TRUE", "t", "yes", "Y", "on", "ON"):
                settings = hajer.HajerSettings.from_env({variable: spelling})
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
        environment = {"HAJER_API_KEY": "sk-secret-value", "HAJER_EVAL_RUNS_KEEP": "5"}
        settings = hajer.HajerSettings.from_env(environment)
        rows = {row.variable: row for row in settings_sources(settings, environment)}
        assert rows["HAJER_API_KEY"].source == "env"
        assert rows["HAJER_API_KEY"].value == "set", "the value of a key is never rendered"
        assert rows["HAJER_EVAL_RUNS_KEEP"].source == "env"
        assert rows["HAJER_EVAL_RUNS_KEEP"].value == "5"
        assert rows["HAJER_EVAL_RUNS_KEEP"].name == "eval_runs_keep"
        assert rows["HAJER_BASE_URL"].source == "default"
        assert rows["HAJER_BASE_URL"].value == DEFAULT_BASE_URL
        assert rows["HAJER_TEAM_ID"].value == "absent"

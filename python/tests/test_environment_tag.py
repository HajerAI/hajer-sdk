"""`HAJER_ENVIRONMENT`: every recorded call says which environment it came from.

The team decides which environments' calls may become test inputs, so the tag has to be on the wire from the first
call. It is lower-case, `^[a-z0-9][a-z0-9-]{0,63}$`; a value outside that is dropped and `doctor` says so, because a
tag is a hint and a hint never costs the call it rides on (the SDK does not raise into application code). A
replay `verify` is CI traffic and says `ci` whatever the shell around it was tagged.
"""

from __future__ import annotations

import pytest
from pydantic import JsonValue

import hajer
from hajer import _wire
from hajer.__main__ import doctor
from hajer._settings import CI_ENVIRONMENT, HajerSettings, settings_sources
from hajer.replay import _case
from tests.conftest import Recorder, assessment_json, responds
from tests.test_cli import _answers, _rows  # pyright: ignore[reportPrivateUsage]

_CREDENTIALS = {"HAJER_API_KEY": "key-for-tests", "HAJER_TEAM_ID": "team-1", "HAJER_BASE_URL": "https://hajer.test"}
_SPEC: dict[str, JsonValue] = {
    "runId": "replay-run-1",
    "planId": "plan-1",
    "caseId": "c1",
    "attempt": 1,
    "workflowId": "classify",
    "revisionLabel": "candidate",
    "input": {"text": "hello"},
    "adapter": {"verifier": "classify@2"},
    "hajer": {"baseUrl": "https://hajer.test", "teamId": "team-1"},
}


def _observed(settings: HajerSettings) -> dict[str, JsonValue]:
    """The one observation a flush sent, as the ingest route receives it."""
    recorder = Recorder(responds({"observationIds": ["obs-1"]}, status=202))
    with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
        client.observe("refund-policy@1", {"orderId": "o-1"}, "reply")
        client.flush()
    (flush,) = recorder.bodies()
    observations = flush["observations"]
    assert isinstance(observations, list)
    (observation,) = observations
    assert isinstance(observation, dict)
    return observation


class TestTheSetting:
    def test_a_tagged_process_sends_its_environment_on_every_observation(self) -> None:
        settings = HajerSettings.from_env({**_CREDENTIALS, "HAJER_ENVIRONMENT": "staging"})

        body = _observed(settings)

        assert settings.environment == "staging"
        assert body["environment"] == "staging"
        assert _wire.VerifyIn.model_validate(body).environment == "staging", "the backend's own schema admits it"

    def test_an_untagged_process_sends_no_key(self) -> None:
        body = _observed(HajerSettings.from_env(_CREDENTIALS))
        assert "environment" not in body, "no tag is a silence the server records as none, never a guess"

    def test_a_verify_carries_it_too(self) -> None:
        settings = HajerSettings.from_env({**_CREDENTIALS, "HAJER_ENVIRONMENT": "production"})
        recorder = Recorder(responds(assessment_json()))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            client.verify("refund-policy@1", {"orderId": "o-1"}, "reply", deadline_ms=4_000)
        assert recorder.bodies()[0]["environment"] == "production"

    def test_the_spelling_is_folded_to_lower_case(self) -> None:
        assert HajerSettings.from_env({"HAJER_ENVIRONMENT": "  Production "}).environment == "production"
        assert HajerSettings(environment="Staging").environment == "staging"

    @pytest.mark.parametrize("value", ["Bad Env", "-staging", "prod_1", "x" * 65, "ünïcode"])
    def test_an_invalid_value_is_dropped_and_never_raised(self, value: str) -> None:
        settings = HajerSettings.from_env({**_CREDENTIALS, "HAJER_ENVIRONMENT": value})

        assert settings.environment is None
        assert "environment" not in _observed(settings)
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


def test_a_replay_verify_is_tagged_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replay child builds its own settings; the shell's `HAJER_ENVIRONMENT` never reaches them."""
    monkeypatch.setenv("HAJER_ENVIRONMENT", "production")
    recorder = Recorder(responds(assessment_json(observationId="ing-" + "c3" * 16)))

    def recording(*, settings: HajerSettings) -> hajer.Hajer:
        return hajer.Hajer(settings=settings, transport=recorder.transport())

    monkeypatch.setattr(_case, "Hajer", recording)

    _, _, sent = _case._verify(_SPEC, {"label": "POSITIVE"}, "key", [])  # pyright: ignore[reportPrivateUsage]

    assert sent is True
    (body,) = recorder.bodies()
    assert body["environment"] == CI_ENVIRONMENT == "ci"

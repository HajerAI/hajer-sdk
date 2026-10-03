"""Default content disclosure and doctor delivery stay bounded by real wire evidence."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import httpx
import pytest

import hajer
from hajer.__main__ import doctor
from hajer._json import JsonObject, JsonValue
from hajer._observe_sink import ObservationFileSink
from hajer._settings import HajerSettings
from hajer._targets_genai import observation_from_gen_ai
from tests.fakes import FakeChatCompletion, FakeChoice, FakeFunction, FakeMessage, FakeOpenAI, FakeToolCall


@pytest.mark.parametrize("from_env", [False, True])
def test_content_defaults_on_and_opt_out_is_explicit(from_env: bool) -> None:
    settings = HajerSettings.from_env({}) if from_env else HajerSettings()
    assert settings.capture_content is True
    assert HajerSettings.from_env({"HAJER_CAPTURE_CONTENT": "0"}).capture_content is False


@pytest.mark.parametrize("capture", [True, False])
def test_default_content_reaches_export_only_after_redaction(capture: bool) -> None:
    values = {"HAJER_API_KEY": "fake-team-key", "HAJER_TEAM_ID": "team-1"}
    if not capture:
        values["HAJER_CAPTURE_CONTENT"] = "0"
    settings = HajerSettings.from_env(values)
    provider = hajer.wrap(FakeOpenAI(), settings=settings)
    provider.chat.completions.create(messages=[{"role": "user", "content": "doctor@example.com"}])
    call = hajer.wrapped_calls()[-1]
    assert (call.content is not None) is capture
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(202, json={"observationIds": ["obs-1"]})

    with hajer.Hajer(settings=settings, transport=httpx.MockTransport(respond)) as client:
        receipt = client.observe_call(call)
        client.flush()
    assert receipt.observation_id == "obs-1"
    assert len(sent) == 1
    assert b"doctor@example.com" not in sent[0].content
    assert (b"[redacted:EMAIL]" in sent[0].content) is capture


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize(
    ("part", "expected"),
    [
        ({"result": {"success": False}, "response": {"success": True}}, {"success": False}),
        ({"result": None, "response": "not-the-result"}, None),
        ({"response": "legacy"}, "legacy"),
    ],
)
def test_modern_tool_response_parity(encoded: bool, part: JsonObject, expected: JsonValue) -> None:
    messages: list[JsonValue] = [{"role": "tool", "parts": [{"type": "tool_call_response", "id": "call-1", **part}]}]
    attributes: dict[str, object] = {
        "gen_ai.provider.name": "openai",
        "gen_ai.operation.name": "chat",
        "gen_ai.input.messages": json.dumps(messages) if encoded else messages,
    }
    call = observation_from_gen_ai(attributes, start_ns=1, end_ns=2, capture_content=True)
    assert call is not None
    assert len(call.tool_results) == 1
    assert call.tool_results[0].tool_use_id == "call-1"
    assert call.tool_results[0].content == expected
    hidden = observation_from_gen_ai(attributes, start_ns=1, end_ns=2, capture_content=False)
    assert hidden is not None
    assert hidden.tool_results[0].content is None


def test_doctor_emit_without_key_never_opens_transport(capsys: pytest.CaptureFixture[str]) -> None:
    def forbidden(_request: httpx.Request) -> httpx.Response:
        pytest.fail("missing-key doctor must not open a socket")

    assert doctor(as_json=False, emit=True, settings=HajerSettings(), transport=httpx.MockTransport(forbidden)) == 6
    assert "configured but unverified" in capsys.readouterr().out


@pytest.mark.parametrize(("status", "body"), [(202, {"observationIds": ["obs-1"]}), (202, {}), (401, {}), (500, {})])
def test_doctor_emits_once_but_never_invents_permalink(
    status: int, body: JsonObject, capsys: pytest.CaptureFixture[str]
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer fake-team-key"
        assert request.url.path == "/api/teams/team-1/observe"
        assert b"doctor@example.com" not in request.content
        assert b"[redacted:EMAIL]" in request.content
        document: JsonObject = json.loads(request.content)
        observations = document["observations"]
        assert isinstance(observations, list)
        assert len(observations) == 1
        return httpx.Response(status, json=body)

    settings = HajerSettings(api_key="fake-team-key", team_id="team-1", base_url="https://hajer.test")
    assert doctor(as_json=True, emit=True, settings=settings, transport=httpx.MockTransport(respond)) == 6
    report: JsonObject = json.loads(capsys.readouterr().out)
    assert len(requests) == 1
    assert report["status"] == "configured but unverified"
    assert report["observationId"] == ("obs-1" if status == 202 and body else None)
    assert report["permalink"] is None


def test_doctor_transport_error_is_sanitized_and_not_retried(capsys: pytest.CaptureFixture[str]) -> None:
    requests: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("fake-team-key doctor@example.com", request=request)

    settings = HajerSettings(api_key="fake-team-key", team_id="team-1")
    assert doctor(as_json=False, emit=True, settings=settings, transport=httpx.MockTransport(fail)) == 6
    output = capsys.readouterr().out
    assert "configured but unverified" in output
    assert "fake-team-key" not in output
    assert "doctor@example.com" not in output
    assert len(requests) == 1


def test_doctor_cli_invalid_url_exits_unverified_without_transport_requests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"observationIds": ["unexpected"]})

    def transport(*, retries: int) -> httpx.MockTransport:
        del retries
        return httpx.MockTransport(respond)

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    monkeypatch.setenv("HAJER_API_KEY", "fake-team-key")
    monkeypatch.setenv("HAJER_TEAM_ID", "team-1")
    monkeypatch.setenv("HAJER_BASE_URL", "https://example.com:bad")
    monkeypatch.setenv("HAJER_DISABLED", "0")
    monkeypatch.setattr(sys, "argv", ["hajer", "doctor", "--emit", "--json"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(Path(hajer.__file__).with_name("__main__.py")), run_name="__main__")
    assert stopped.value.code == 6
    output = capsys.readouterr()
    report: JsonObject = json.loads(output.out)
    assert report["status"] == "configured but unverified"
    assert report["observationId"] is None
    assert report["permalink"] is None
    assert "fake-team-key" not in output.out
    assert "example.com" not in output.out
    assert "Invalid port" not in output.out
    assert output.err == ""
    assert requests == []


@pytest.mark.parametrize(("capture", "raw"), [(True, False), (True, True), (False, False)])
def test_file_sink_redacts_telemetry_copy_without_changing_provider_values(
    capture: bool, raw: bool, tmp_path: Path
) -> None:
    prompt, answer, argument, result = (
        "prompt@example.com",
        "answer@example.com",
        "args@example.com",
        "result@example.com",
    )
    key = "sk-ant-abcdefghijklmnop"
    response = FakeChatCompletion(
        choices=[
            FakeChoice(
                message=FakeMessage(
                    content=answer,
                    tool_calls=[
                        FakeToolCall(id="call-1", function=FakeFunction("lookup", json.dumps({"email": argument})))
                    ],
                )
            )
        ]
    )
    settings = HajerSettings(capture_content=capture, capture_raw=raw)
    provider = hajer.wrap(FakeOpenAI(chat_script=[response]), settings=settings)
    returned = provider.chat.completions.create(
        messages=[
            {"role": "user", "content": f"{prompt} {key}"},
            {"role": "tool", "tool_call_id": "call-previous", "content": result},
        ]
    )
    call = hajer.wrapped_calls()[-1]
    before = json.dumps(call.to_wire(), sort_keys=True)
    sink = ObservationFileSink(tmp_path)
    sink.record(call, "test")
    sink.flush()
    persisted = sink.path.read_text()
    for secret in (prompt, answer, argument, result, key):
        assert secret not in persisted
    assert ("[redacted:EMAIL]" in persisted) is capture
    assert ("[redacted:ANTHROPIC_KEY]" in persisted) is capture
    assert json.dumps(call.to_wire(), sort_keys=True) == before
    assert returned is response
    assert response.choices[0].message.content == answer
    assert argument in response.choices[0].message.tool_calls[0].function.arguments

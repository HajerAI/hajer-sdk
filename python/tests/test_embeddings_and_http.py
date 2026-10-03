"""The provider calls a real traffic run never exported: embeddings, and outbound HTTP to non-model APIs.

In one traffic run, 6 `embeddings.create` requests and 22 search- and people-API requests reached the
provider gateway and no observation. An embedding is recorded as metadata — the model,
how many inputs, how many vectors of which size — and never its vectors. An HTTP exchange is recorded only when
`HAJER_CAPTURE_HTTP` is on, never with its headers or query string, never twice for a wrapped model call, and
never for the SDK's own requests.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager
from typing import cast

import httpx
import pytest

import hajer
from hajer import _http_capture, _payload
from hajer._json import JsonObject, JsonValue
from hajer._transport import USER_AGENT

VECTOR: list[JsonValue] = [0.125, -0.5, 0.75]
EMBEDDINGS: JsonObject = {
    "object": "list",
    "model": "text-embedding-3-small",
    "data": [
        {"object": "embedding", "index": 0, "embedding": VECTOR},
        {"object": "embedding", "index": 1, "embedding": VECTOR},
    ],
    "usage": {"prompt_tokens": 4, "total_tokens": 4},
}
CHAT: JsonObject = {
    "id": "chat-1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-5.1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
SETTINGS = hajer.HajerSettings(capture_content=True, capture_raw=True, disabled=True)


def _respond(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/embeddings"):
        return httpx.Response(200, json=EMBEDDINGS)
    return httpx.Response(200, json=CHAT)


def openai_client(*, asynchronous: bool, transport: httpx.AsyncBaseTransport | None = None) -> object:
    openai = pytest.importorskip("openai")
    if asynchronous:
        http: object = httpx.AsyncClient(transport=transport or httpx.MockTransport(_respond))
        make = cast(Callable[..., object], openai.AsyncOpenAI)
    else:
        http = httpx.Client(transport=httpx.MockTransport(_respond))
        make = cast(Callable[..., object], openai.OpenAI)
    return make(api_key="fake", max_retries=0, http_client=http)


def at(value: object, dotted: str) -> Callable[..., object]:
    for part in dotted.split("."):
        value = getattr(value, part)
    return cast(Callable[..., object], value)


async def awaited(value: object) -> object:
    return await cast(Awaitable[object], value) if inspect.isawaitable(value) else value


# ── embeddings ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("given", ["one query", ["first", "second"]])
async def test_an_embedding_is_recorded_as_metadata_and_never_as_vectors(
    asynchronous: bool, given: str | list[str]
) -> None:
    provider = hajer.wrap(openai_client(asynchronous=asynchronous), settings=SETTINGS)
    with hajer.scope() as operation:
        answer = await awaited(at(provider, "embeddings.create")(model="text-embedding-3-small", input=given))
    assert len(cast("list[object]", getattr(answer, "data"))) == 2, "the application's vectors are untouched"  # noqa: B009
    (call,) = operation.calls
    assert (call.provider, call.api, call.model) == ("openai", "embeddings", "text-embedding-3-small")
    assert call.message_count == (1 if isinstance(given, str) else 2)
    assert call.usage["input_tokens"] == 4
    assert call.raw is None, "a vector is not a capture: no raw document, even with raw capture on"
    assert call.content == {"output": {"vectors": 2, "dimensions": 3}}, "metadata only: no input text either"
    sent = json.dumps([call.to_wire(), _payload.attach_output(call), _payload.attach_request(call)])
    assert "0.125" not in sent, "no vector value leaves the process"
    assert "one query" not in sent, "the embedded text does not leave the process"
    assert "first" not in sent
    assert _payload.attach_output(call)["embedding"] == {"vectors": 2, "dimensions": 3}
    assert any("vector" in limitation for limitation in call.limitations)


def test_an_embedding_with_content_capture_off_keeps_its_shape_and_not_its_input() -> None:
    settings = hajer.HajerSettings(capture_content=False, disabled=True)
    provider = hajer.wrap(openai_client(asynchronous=False), settings=settings)
    with hajer.scope() as operation:
        at(provider, "embeddings.create")(model="text-embedding-3-small", input="a private query")
    (call,) = operation.calls
    assert call.content is None
    assert "a private query" not in json.dumps(call.to_wire())
    assert _payload.attach_output(call)["embedding"] == {"vectors": 2, "dimensions": 3}


# ── HTTP capture ────────────────────────────────────────────────────────────────────────────────


def _vendor(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/chat/completions"):
        return httpx.Response(200, json=CHAT)
    return httpx.Response(200, json={"results": [{"url": "https://example.com/a"}]})


@pytest.fixture
def default_transports(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """httpx's own default transports, answering in memory: what `HAJER_CAPTURE_HTTP` patches is theirs."""
    mock = httpx.MockTransport(_vendor)

    def handle(_self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return mock.handle_request(request)

    async def handle_async(_self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return await mock.handle_async_request(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async)
    yield
    _http_capture.uninstall_http_capture()


HTTP_SETTINGS = SETTINGS.model_copy(update={"capture_http": True})


async def app_search(http: httpx.AsyncClient) -> httpx.Response:
    return await http.post("http://exa.invalid/search?apiKey=SECRET-QUERY", json={"query": "who"})


@pytest.mark.usefixtures("default_transports")
async def test_an_outbound_http_call_is_recorded_when_http_capture_is_on() -> None:
    hajer.wrap(openai_client(asynchronous=True), settings=HTTP_SETTINGS)
    async with httpx.AsyncClient(headers={"x-api-key": "SECRET-HEADER"}) as http:
        with hajer.scope() as operation:
            response = await app_search(http)
    assert response.json() == {"results": [{"url": "https://example.com/a"}]}, "the application's bytes"
    (call,) = operation.calls
    assert (call.provider, call.api) == ("http", "POST")
    assert call.provider_host == "exa.invalid:80"
    assert call.request_settings["url"] == "http://exa.invalid/search"
    assert call.caller_frames[0].qualname == "app_search"
    assert call.request_settings["status"] == 200
    assert call.content is None, "no body leaves the process, the request's or the response's"
    sent = json.dumps([call.to_wire(), _payload.attach_request(call), _payload.attach_output(call)])
    assert "who" not in sent
    assert "example.com/a" not in sent
    assert "SECRET-QUERY" not in sent, "a query string can carry a credential"
    assert "SECRET-HEADER" not in sent, "headers are never recorded"


@pytest.mark.parametrize(
    ("url", "sent"),
    [
        (
            # Built from parts so secret scanners do not read a fixture as a real webhook.
            "https://hooks.slack.com/services/" + "T0AAAAAAA/B0BBBBBBB/" + "xoxbAAAA1111bbbb2222CCCC",
            "https://hooks.slack.com/services/T0AAAAAAA/B0BBBBBBB/{token}",
        ),
        (
            "https://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-aBcD",
            "https://discord.com/api/webhooks/{id}/{token}",
        ),
        (
            "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage",
            "https://api.telegram.org/{token}/sendMessage",
        ),
        (
            "https://api.example.com/v1/users/jane.doe@example.com/profile",
            "https://api.example.com/v1/users/{redacted}/profile",
        ),
        ("https://api.example.com/v2/people/48213/contacts", "https://api.example.com/v2/people/{id}/contacts"),
        ("https://api.example.com/search", "https://api.example.com/search"),
    ],
)
@pytest.mark.usefixtures("default_transports")
def test_a_path_that_carries_a_token_or_an_id_is_sent_as_a_template(url: str, sent: str) -> None:
    """Webhooks and bots put the credential in the path, not the query string."""
    hajer.wrap(openai_client(asynchronous=False), settings=HTTP_SETTINGS)
    with httpx.Client() as http, hajer.scope() as operation:
        http.post(url, json={"text": "hi"})
    (call,) = operation.calls
    assert call.request_settings["url"] == sent


@pytest.mark.usefixtures("default_transports")
def test_a_synchronous_http_call_is_recorded_too() -> None:
    hajer.instrument(settings=HTTP_SETTINGS)
    try:
        with httpx.Client() as http, hajer.scope() as operation:
            http.get("http://people.invalid/v1/people", params={"q": "x"})
    finally:
        hajer.uninstrument()
    (call,) = operation.calls
    assert (call.api, call.provider_host, call.request_settings["url"]) == (
        "GET",
        "people.invalid:80",
        "http://people.invalid/v1/people",
    )


@pytest.mark.usefixtures("default_transports")
def test_a_response_closed_early_is_not_recorded_as_a_failure() -> None:
    """A caller that stops reading a streamed body is a short read, not a failed request."""
    hajer.wrap(openai_client(asynchronous=False), settings=HTTP_SETTINGS)
    with httpx.Client() as http, hajer.scope() as operation:
        with http.stream("GET", "http://people.invalid/v1/people") as response:
            for _ in response.iter_bytes():
                break
    (call,) = operation.calls
    assert (call.error, call.error_type) == (None, None)


@pytest.mark.usefixtures("default_transports")
async def test_a_wrapped_model_call_is_not_recorded_again_at_the_transport() -> None:
    provider = hajer.wrap(
        openai_client(asynchronous=True, transport=httpx.AsyncHTTPTransport()), settings=HTTP_SETTINGS
    )
    with hajer.scope() as operation:
        await awaited(at(provider, "chat.completions.create")(model="gpt-5.1", messages=[]))
    assert [call.provider for call in operation.calls] == ["openai"]


@pytest.mark.usefixtures("default_transports")
def test_the_sdks_own_requests_are_never_recorded() -> None:
    hajer.wrap(openai_client(asynchronous=False), settings=HTTP_SETTINGS)
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as http, hajer.scope() as operation:
        http.post("http://hajer.invalid/api/teams/t/observe", json={"observations": []})
    assert operation.calls == ()


@pytest.mark.usefixtures("default_transports")
async def test_nothing_is_recorded_at_the_transport_while_http_capture_is_off() -> None:
    hajer.wrap(openai_client(asynchronous=True), settings=SETTINGS)
    async with httpx.AsyncClient() as http:
        with hajer.scope() as operation:
            await app_search(http)
    assert operation.calls == ()


def test_http_capture_is_one_variable() -> None:
    assert hajer.HajerSettings.from_env({"HAJER_CAPTURE_HTTP": "1"}).capture_http is True
    assert hajer.HajerSettings.from_env({}).capture_http is False


def test_a_stream_helper_is_recorded_once_with_http_capture_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """`chat.completions.stream()` sends its request on `__enter__`, which ran outside the wrapped call,
    so the transport recorded it a second time."""
    chunk: JsonObject = {
        "id": "chat-s",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-5.1",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    }
    body = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
    mock = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )

    def handle(_self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return mock.handle_request(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    openai = pytest.importorskip("openai")
    client = cast(Callable[..., object], openai.OpenAI)(api_key="fake", max_retries=0, http_client=httpx.Client())
    try:
        hajer.wrap(client, settings=HTTP_SETTINGS)
        with hajer.scope() as operation:
            manager = cast(
                "AbstractContextManager[Iterator[object]]",
                at(client, "chat.completions.stream")(model="m", messages=[]),
            )
            with manager as stream:
                for _ in stream:
                    pass
    finally:
        _http_capture.uninstall_http_capture()
    assert [call.provider for call in operation.calls] == ["openai"]


def test_a_streamed_model_request_captured_at_the_transport_carries_its_id_and_answer() -> None:
    """A streamed call recorded only at the transport kept its bytes and nothing it said — no response
    id, no text — so a span of the same call could not be matched to it (`_claims`)."""
    events: list[JsonObject] = [
        {
            "id": "chatcmpl-s1",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "hel"}}],
        },
        {"id": "chatcmpl-s1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "lo"}}]},
        {
            "id": "chatcmpl-s1",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    ]
    sse = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    transport = hajer.CaptureTransport(httpx.MockTransport(respond), settings=SETTINGS)
    with httpx.Client(transport=transport) as http, hajer.scope() as operation:
        with http.stream("POST", "http://models.invalid/v1/chat/completions", json={"model": "m", "stream": True}) as r:
            assert b"".join(r.iter_bytes()) == sse.encode(), "the application's bytes"
    (call,) = operation.calls
    assert (call.response_id, call.usage.get("output_tokens"), call.finish_reason) == ("chatcmpl-s1", 2, "stop")
    assert (call.content or {}).get("streamedText") == "hello"

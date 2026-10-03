"""What came back is recorded whichever door the application used: raw responses, root-level raw proxies, streams.

In a real traffic run, `langchain_openai` calls `with_raw_response.create(...)`, so the instrumented
`create` was handed an openai `LegacyAPIResponse` and recorded `"LegacyAPIResponse"` as the answer — no usage,
no finish reason, no text, on all 67 calls. The real `openai` library is used here with an in-memory transport.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast

import httpx
import pytest

import hajer
from hajer._json import JsonObject

CHAT: JsonObject = {
    "id": "chat-1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-5.1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "the answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}
RESPONSE: JsonObject = {
    "id": "resp-1",
    "object": "response",
    "created_at": 0,
    "model": "gpt-5.1",
    "status": "completed",
    "output": [
        {
            "type": "message",
            "id": "msg-1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "the answer", "annotations": []}],
        }
    ],
    "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
}


def _sse(*chunks: JsonObject) -> bytes:
    return b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"


STREAM = _sse(
    {
        "id": "chat-s",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-5.1",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "the "}, "finish_reason": None}],
    },
    {
        "id": "chat-s",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-5.1",
        "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": "stop"}],
    },
    {
        "id": "chat-s",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-5.1",
        "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    },
)


def _respond(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/responses"):
        return httpx.Response(200, json=RESPONSE)
    sent = json.loads(request.content)
    if sent.get("response_format"):
        choice = {"index": 0, "message": {"role": "assistant", "content": '{"name": "x"}'}, "finish_reason": "stop"}
        return httpx.Response(200, json={**CHAT, "choices": [choice]})
    if sent.get("stream"):
        return httpx.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})
    return httpx.Response(200, json=CHAT)


def client(*, asynchronous: bool) -> object:
    openai = pytest.importorskip("openai")
    if asynchronous:
        make = cast(Callable[..., object], openai.AsyncOpenAI)
        return make(
            api_key="fake", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(_respond))
        )
    make = cast(Callable[..., object], openai.OpenAI)
    return make(api_key="fake", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(_respond)))


def at(value: object, dotted: str) -> Callable[..., object]:
    for part in dotted.split("."):
        value = getattr(value, part)
    return cast(Callable[..., object], value)


async def awaited(value: object) -> object:
    return await cast(Awaitable[object], value) if inspect.isawaitable(value) else value


SETTINGS = hajer.HajerSettings(capture_content=True, capture_raw=True, disabled=True)
CHAT_REQUEST: JsonObject = {"model": "gpt-5.1", "messages": [{"role": "user", "content": "q"}]}


def _only_call(operation: hajer.Operation) -> hajer.WrappedCall:
    (call,) = operation.calls
    return call


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("door", "request_body", "response_id"),
    [
        ("chat.completions.with_raw_response.create", CHAT_REQUEST, "chat-1"),
        # A root-level raw proxy is built on first use, after `wrap` replaced `create`: its wrapper calls
        # Hajer's replacement, which is handed the raw response rather than the answer.
        ("with_raw_response.chat.completions.create", CHAT_REQUEST, "chat-1"),
        ("with_raw_response.responses.create", {"model": "gpt-5.1", "input": "q"}, "resp-1"),
        ("chat.completions.with_raw_response.parse", CHAT_REQUEST, "chat-1"),
    ],
)
async def test_a_raw_response_the_application_parses_is_recorded_as_the_answer(
    asynchronous: bool, door: str, request_body: JsonObject, response_id: str
) -> None:
    provider = hajer.wrap(client(asynchronous=asynchronous), settings=SETTINGS)
    with hajer.scope() as operation:
        raw = await awaited(at(provider, door)(**request_body))
        parsed = await awaited(at(raw, "parse")())
    dumped = cast(JsonObject, at(parsed, "model_dump")())
    assert dumped["id"] == response_id, "the application's answer is untouched"
    call = _only_call(operation)
    assert call.response_id == response_id
    assert call.usage["input_tokens"] == 3
    assert call.raw is not None
    assert json.loads(call.raw.response_body)["id"] == response_id
    assert "LegacyAPIResponse" not in call.raw.response_body
    assert call.content is not None
    assert "the answer" in json.dumps(call.content["output"])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "door", ["chat.completions.with_raw_response.create", "with_raw_response.chat.completions.create"]
)
async def test_a_raw_streamed_response_records_the_streamed_answer(asynchronous: bool, door: str) -> None:
    provider = hajer.wrap(client(asynchronous=asynchronous), settings=SETTINGS)
    with hajer.scope() as operation:
        raw = await awaited(at(provider, door)(**CHAT_REQUEST, stream=True, stream_options={"include_usage": True}))
        stream = await awaited(at(raw, "parse")())
        if asynchronous:
            texts = [chunk async for chunk in cast(AsyncIterator[object], stream)]
        else:
            texts = list(cast("list[object]", stream))
    assert len(texts) == 3, "every chunk reaches the application"
    call = _only_call(operation)
    assert call.streamed
    assert call.stream_complete is True
    assert call.finish_reason == "stop"
    assert call.usage["output_tokens"] == 2
    assert call.content is not None
    assert call.content["streamedText"] == "the answer"


def test_a_provider_model_with_a_serializer_warning_is_read_without_warning(recwarn: pytest.WarningsRecorder) -> None:
    """`ParsedChatCompletion.parsed` holds the caller's own model, which pydantic warns about when it dumps it."""
    provider = hajer.wrap(client(asynchronous=False), settings=SETTINGS)
    pydantic = pytest.importorskip("pydantic")

    class Named(pydantic.BaseModel):
        name: str

    with hajer.scope() as operation:
        at(provider, "chat.completions.parse")(
            model="gpt-5.1",
            messages=[{"role": "user", "content": "q"}],
            response_format=Named,
        )
    assert not [warning for warning in recwarn if "serializ" in str(warning.message).lower()]
    assert _only_call(operation).raw is not None

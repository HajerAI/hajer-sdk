"""Real provider SDKs, identical HTTP fixtures: Hajer must not change their answers."""

from __future__ import annotations

import inspect
import json
import sys
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import ModuleType
from typing import cast

import httpx
import httpx2
import pytest

import hajer
from hajer import _wrap
from hajer._json import JsonObject

SURFACES = (
    ("openai", "chat.completions.create"),
    ("openai", "responses.create"),
    ("openai", "beta.chat.completions.parse"),
    ("openai", "responses.parse"),
    ("openai", "chat.completions.with_raw_response.create"),
    ("openai", "responses.with_raw_response.create"),
    ("openai", "chat.completions.with_streaming_response.create"),
    ("openai", "responses.with_streaming_response.create"),
    ("anthropic", "messages.create"),
    ("anthropic", "messages.stream"),
    ("anthropic", "messages.with_raw_response.create"),
    ("anthropic", "messages.with_streaming_response.create"),
)


@pytest.fixture(autouse=True)
def _real_provider_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    # Legacy import-hook tests leave a synthetic package in sys.modules. Restore that test state
    # after each differential case, but run this case against the installed distributions.
    for name in ("openai", "anthropic"):
        if not isinstance(getattr(sys.modules.get(name), "__version__", None), str):
            monkeypatch.delitem(sys.modules, name, raising=False)


def leaf(value: object, path: str) -> Callable[..., object]:
    for part in path.split("."):
        value = getattr(value, part)
    assert callable(value)
    return value


def document(provider: str, path: str) -> JsonObject:
    if provider == "anthropic":
        return {
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "model": "test-model",
            "content": [{"type": "tool_use", "id": "call-1", "name": "refund", "input": {"amount": 5}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }
    if path.startswith("responses"):
        return {
            "id": "resp-1",
            "object": "response",
            "created_at": 0,
            "model": "test-model",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "refund",
                    "arguments": '{"amount":5}',
                    "status": "completed",
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        }
    return {
        "id": "chat-1",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "refund", "arguments": '{"amount":5}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }


def client_for(provider: str, *, asynchronous: bool, body: JsonObject | bytes, status: int = 200) -> object:
    module = pytest.importorskip(provider)
    name = ("Async" if asynchronous else "") + ("OpenAI" if provider == "openai" else "Anthropic")
    # Anthropic 1.x moved to httpx2; use that library's identical in-memory transport seam.
    http: ModuleType = httpx2 if provider == "anthropic" else httpx

    def respond(_request: object) -> object:
        if isinstance(body, bytes):
            return http.Response(status, content=body, headers={"content-type": "text/event-stream"})
        return http.Response(status, json=body)

    transport = cast(Callable[..., object], http.MockTransport)(respond)
    client_class = "AsyncClient" if asynchronous else "Client"
    http_client = cast(Callable[..., object], getattr(http, client_class))(transport=transport)
    return cast(Callable[..., object], getattr(module, name))(api_key="fake", http_client=http_client, max_retries=0)


async def awaited(value: object) -> object:
    return await cast(Awaitable[object], value) if inspect.isawaitable(value) else value


async def answer(client: object, provider: str, path: str) -> object:
    kwargs: dict[str, object] = {"model": "test-model"}
    kwargs["input" if path.startswith("responses") else "messages"] = []
    if provider == "anthropic":
        kwargs["max_tokens"] = 20
    value = await awaited(leaf(client, path)(**kwargs))
    if "with_streaming_response" in path:
        enter = "__aenter__" if hasattr(value, "__aenter__") else "__enter__"
        exit_name = "__aexit__" if enter == "__aenter__" else "__exit__"
        manager = value
        value = await awaited(leaf(manager, enter)())
        parsed = await awaited(leaf(value, "parse")())
        await awaited(leaf(manager, exit_name)(None, None, None))
        value = parsed
    elif "with_raw_response" in path:
        value = await awaited(leaf(value, "parse")())
    return leaf(value, "model_dump")(mode="json")


@pytest.mark.parametrize(("provider", "path"), [item for item in SURFACES if item[1] != "messages.stream"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("installation", ["wrap", "instrument"])
@pytest.mark.parametrize("opt_out", [False, True])
async def test_every_nonstream_surface_preserves_answer_and_records_once(
    provider: str, path: str, asynchronous: bool, installation: str, opt_out: bool
) -> None:
    """Missing parse/raw targets loses observations; stacking targets duplicates them."""
    body = document(provider, path)
    plain = client_for(provider, asynchronous=asynchronous, body=body)
    settings = hajer.HajerSettings.from_env({"HAJER_CAPTURE_CONTENT": "0"} if opt_out else {})
    if installation == "instrument":
        hajer.instrument(settings=settings)
    wrapped = client_for(provider, asynchronous=asynchronous, body=body)
    if installation == "wrap":
        hajer.wrap(wrapped, settings=settings)
    try:
        assert await answer(wrapped, provider, path) == await answer(plain, provider, path)
        calls = hajer.wrapped_calls()
        assert len(calls) == 1
        assert calls[0].tool_calls[0].id == "call-1"
        assert calls[0].usage["input_tokens"] == 3
        assert (calls[0].content is None) is opt_out
        assert (calls[0].tool_calls[0].arguments is None) is opt_out
    finally:
        await awaited(leaf(plain, "close")())
        await awaited(leaf(wrapped, "close")())
        hajer.uninstrument()


def test_responses_tool_result_links_to_function_call() -> None:
    from tests.fakes import FakeOpenAI  # noqa: PLC0415

    client = hajer.wrap(FakeOpenAI(), settings=hajer.HajerSettings(capture_content=True))
    client.responses.create(
        model="test-model",
        input=[
            {"type": "function_call_output", "call_id": "call-1", "output": '{"success":true}'},
        ],
    )
    result = hajer.wrapped_calls()[0].tool_results
    assert len(result) == 1
    assert result[0].tool_use_id == "call-1"
    assert result[0].content == '{"success":true}'


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_stream_error_is_not_replaced_by_cleanup(asynchronous: bool) -> None:
    from tests.fakes import FakeOpenAI  # noqa: PLC0415

    failure = ValueError("provider failure")

    class BrokenStream:
        def __iter__(self) -> Iterator[object]:
            raise failure

        def close(self) -> None:
            raise RuntimeError("cleanup failed")

    class BrokenAsyncStream(BrokenStream):
        def __aiter__(self) -> AsyncIterator[object]:
            return self

        async def __anext__(self) -> object:
            raise failure

        async def aclose(self) -> None:
            self.close()

    client = hajer.wrap(FakeOpenAI(chat_script=[BrokenAsyncStream() if asynchronous else BrokenStream()]))
    stream = client.chat.completions.create(stream=True)
    if asynchronous:
        with pytest.raises(ValueError, match="provider failure") as caught:
            await cast(AsyncIterator[object], stream).__anext__()
    else:
        with pytest.raises(ValueError, match="provider failure") as caught:
            next(cast(Iterator[object], stream))
    assert caught.value is failure


def test_exporter_failure_does_not_change_provider_answer() -> None:
    from tests.fakes import FakeOpenAI  # noqa: PLC0415

    class BrokenExport:
        def opened(self, call: hajer.WrappedCall) -> object:
            raise RuntimeError("telemetry broke at open")

        def settled(self, call: hajer.WrappedCall, handle: object | None) -> None:
            raise RuntimeError("telemetry broke at settle")

    plain = FakeOpenAI()
    wrapped = hajer.wrap(FakeOpenAI())
    _wrap.set_call_observer(BrokenExport())
    try:
        assert wrapped.chat.completions.create() == plain.chat.completions.create()
    finally:
        _wrap.set_call_observer(None)


def sse(provider: str, path: str) -> bytes:
    if provider == "anthropic":
        events: list[JsonObject] = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "test-model",
                    "usage": {"input_tokens": 3, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "call-1", "name": "refund", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"amount":5}'},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}},
            {"type": "message_stop"},
        ]
    elif path.startswith("responses"):
        events = [
            {"type": "response.created", "sequence_number": 0, "response": document("openai", "responses")},
            {"type": "response.completed", "sequence_number": 1, "response": document("openai", "responses")},
        ]
    else:
        events = [
            {
                "id": "chat-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "refund", "arguments": '{"amount":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chat-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "5}"}}]},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            },
        ]
    return "".join(f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


async def streamed_answer(client: object, provider: str, path: str, *, asynchronous: bool, early: bool) -> list[object]:
    kwargs: dict[str, object] = {"model": "test-model", "input" if path.startswith("responses") else "messages": []}
    if provider == "anthropic":
        kwargs["max_tokens"] = 20
    if path != "messages.stream":
        kwargs["stream"] = True
    value = await awaited(leaf(client, path)(**kwargs))
    manager: object | None = None
    if "with_streaming_response" in path or path == "messages.stream":
        manager = value
        value = await awaited(leaf(manager, "__aenter__" if asynchronous else "__enter__")())
    if "with_" in path:
        value = await awaited(leaf(value, "parse")())
    chunks: list[object] = []
    if asynchronous:
        async for chunk in cast(AsyncIterator[object], value):
            chunks.append(leaf(chunk, "model_dump")(mode="json"))
            if early:
                break
    else:
        for chunk in cast(Iterator[object], value):
            chunks.append(leaf(chunk, "model_dump")(mode="json"))
            if early:
                break
    if manager is not None:
        await awaited(leaf(manager, "__aexit__" if asynchronous else "__exit__")(None, None, None))
    else:
        closer = "aclose" if asynchronous and hasattr(value, "aclose") else "close"
        await awaited(leaf(value, closer)())
    return chunks


@pytest.mark.parametrize(("provider", "path"), [item for item in SURFACES if "parse" not in item[1]])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("early", [False, True])
@pytest.mark.parametrize("installation", ["wrap", "instrument"])
async def test_real_streams_preserve_chunks_and_record_tool_calls(
    provider: str, path: str, asynchronous: bool, early: bool, installation: str
) -> None:
    body = sse(provider, path)
    plain = client_for(provider, asynchronous=asynchronous, body=body)
    if installation == "instrument":
        hajer.instrument(settings=hajer.HajerSettings(capture_call_site=False))
    wrapped = client_for(provider, asynchronous=asynchronous, body=body)
    if installation == "wrap":
        hajer.wrap(wrapped, settings=hajer.HajerSettings(capture_call_site=False))
    try:
        assert await streamed_answer(
            wrapped, provider, path, asynchronous=asynchronous, early=early
        ) == await streamed_answer(plain, provider, path, asynchronous=asynchronous, early=early)
        calls = hajer.wrapped_calls()
        assert len(calls) == 1
        # A raw or streaming response is the provider's own object, observed at its body's bytes.
        # This in-memory body arrived whole before the application read a chunk, so its stream is complete
        # whatever the reader did; a body still arriving is cut short by an early stop
        # (`test_raw_responses.py::test_a_raw_stream_the_application_stops_reading_early_is_incomplete`).
        assert calls[0].stream_complete is (not early or "with_" in path)
        if not early:
            assert calls[0].tool_calls[0].id == "call-1"
    finally:
        await awaited(leaf(plain, "close")())
        await awaited(leaf(wrapped, "close")())
        hajer.uninstrument()


@pytest.mark.parametrize(("provider", "path"), SURFACES)
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_provider_failures_preserve_type_attributes_and_traceback_origin(
    provider: str, path: str, asynchronous: bool
) -> None:
    body: JsonObject = {"error": {"message": "quota", "type": "rate_limit_error"}}
    plain = client_for(provider, asynchronous=asynchronous, body=body, status=429)
    wrapped = client_for(provider, asynchronous=asynchronous, body=body, status=429)
    hajer.wrap(wrapped, settings=hajer.HajerSettings(capture_call_site=False))

    async def invoke(client: object) -> object:
        if path == "messages.stream":
            return await streamed_answer(client, provider, path, asynchronous=asynchronous, early=False)
        return await answer(client, provider, path)

    try:
        with pytest.raises(Exception, match="quota") as expected:
            await invoke(plain)
        with pytest.raises(type(expected.value), match="quota") as actual:
            await invoke(wrapped)
        assert actual.value.args == expected.value.args
        for attribute in ("status_code", "body"):
            assert getattr(actual.value, attribute) == getattr(expected.value, attribute)
        origin = traceback.extract_tb(actual.value.__traceback__)[-1]
        expected_origin = traceback.extract_tb(expected.value.__traceback__)[-1]
        assert (origin.filename, origin.name, origin.lineno) == (
            expected_origin.filename,
            expected_origin.name,
            expected_origin.lineno,
        )
        assert hajer.wrapped_calls()[0].error_type == type(actual.value).__qualname__
    finally:
        await awaited(leaf(plain, "close")())
        await awaited(leaf(wrapped, "close")())

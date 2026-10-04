"""A raw or streaming response is recorded once, on the first read of its body, whichever accessor reads it.

The call was settled only when the application called `.parse()`. A body read through
`iter_lines()`, `http_response.json()`, `.text` or `.read()` was never exported, `parse()` twice exported the call
twice, and the object handed back was Hajer's proxy rather than the provider's `LegacyAPIResponse`. The real
`openai` library runs here over an in-memory transport; `lazy` bodies are served as a stream nobody has read yet,
the way a network response arrives, and `eager` ones already loaded, the way httpx loads a small body.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import cast

import httpx
import pytest

import hajer
from hajer import _wrap
from hajer._json import JsonObject

CHAT: JsonObject = {
    "id": "chat-raw",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-5.1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "the answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def _sse(*chunks: JsonObject) -> bytes:
    return b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"


def _chunk(delta: JsonObject, finish: str | None) -> JsonObject:
    return {
        "id": "chat-raw-s",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-5.1",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


STREAM = _sse(
    _chunk({"role": "assistant", "content": "the "}, None),
    _chunk({"content": "answer"}, "stop"),
    {
        "id": "chat-raw-s",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-5.1",
        "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    },
)


class _Lazy(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A body nobody has read yet, delivered in two pieces."""

    def __init__(self, body: bytes) -> None:
        self._parts = (body[: len(body) // 2], body[len(body) // 2 :])

    def __iter__(self) -> Iterator[bytes]:
        yield from self._parts

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self._parts:
            yield part


def _client(*, asynchronous: bool, lazy: bool) -> object:
    openai = pytest.importorskip("openai")

    def respond(request: httpx.Request) -> httpx.Response:
        streamed = bool(json.loads(request.content).get("stream"))
        body = STREAM if streamed else json.dumps(CHAT).encode()
        headers = {"content-type": "text/event-stream" if streamed else "application/json"}
        if lazy:
            return httpx.Response(200, stream=_Lazy(body), headers=headers)
        return httpx.Response(200, content=body, headers=headers)

    transport = httpx.MockTransport(respond)
    if asynchronous:
        make = cast(Callable[..., object], openai.AsyncOpenAI)
        return make(api_key="fake", max_retries=0, http_client=httpx.AsyncClient(transport=transport))
    make = cast(Callable[..., object], openai.OpenAI)
    return make(api_key="fake", max_retries=0, http_client=httpx.Client(transport=transport))


def at(value: object, dotted: str) -> Callable[..., object]:
    for part in dotted.split("."):
        value = getattr(value, part)
    return cast(Callable[..., object], value)


async def awaited(value: object) -> object:
    return await cast(Awaitable[object], value) if inspect.isawaitable(value) else value


class _Settled:
    """The call observer, recording every call that settled — what reaches the span emitter."""

    def __init__(self, seen: list[hajer.WrappedCall]) -> None:
        self.seen = seen

    def opened(self, call: hajer.WrappedCall) -> object:
        return None

    def settled(self, call: hajer.WrappedCall, handle: object | None) -> None:
        self.seen.append(call)


@pytest.fixture
def settled() -> Iterator[list[hajer.WrappedCall]]:
    """Every call handed to the observer — what reaches the span emitter."""
    seen: list[hajer.WrappedCall] = []
    previous = _wrap.call_observer()
    _wrap.set_call_observer(_Settled(seen))
    yield seen
    _wrap.set_call_observer(previous)


SETTINGS = hajer.HajerSettings(capture_content=True, disabled=True)
REQUEST: JsonObject = {"model": "gpt-5.1", "messages": [{"role": "user", "content": "q"}]}


async def _read(raw: object, accessor: str, *, asynchronous: bool) -> None:
    """Read the body the way an application might, through one accessor."""
    if accessor == "parse":
        await awaited(at(raw, "parse")())
    elif accessor == "parse twice":
        await awaited(at(raw, "parse")())
        await awaited(at(raw, "parse")())
    elif accessor == "http_response.json":
        http = getattr(raw, "http_response")  # noqa: B009
        if asynchronous and not getattr(http, "is_stream_consumed", True):
            await awaited(at(http, "aread")())
        at(http, "json")()
    elif accessor == "iter_lines":
        if asynchronous:
            async for _ in cast(AsyncIterator[str], at(raw, "iter_lines")()):
                pass
        else:
            for _ in cast(Iterator[str], at(raw, "iter_lines")()):
                pass
    elif accessor == "read":
        await awaited(at(raw, "read")())


def _answered(call: hajer.WrappedCall) -> None:
    assert call.response_id == "chat-raw"
    assert call.usage["input_tokens"] == 3
    assert call.content is not None
    assert "the answer" in json.dumps(call.content["output"])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("lazy", [False, True])
@pytest.mark.parametrize("accessor", ["parse", "parse twice", "http_response.json"])
async def test_a_raw_response_is_recorded_once_whichever_accessor_reads_it(
    settled: list[hajer.WrappedCall], asynchronous: bool, lazy: bool, accessor: str
) -> None:
    provider = hajer.wrap(_client(asynchronous=asynchronous, lazy=lazy), settings=SETTINGS)
    with hajer.scope():
        raw = await awaited(at(provider, "chat.completions.with_raw_response.create")(**REQUEST))
        assert type(raw).__name__ == "LegacyAPIResponse", "the provider's own object, not a proxy"
        await _read(raw, accessor, asynchronous=asynchronous)
    assert len(settled) == 1
    _answered(settled[0])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("lazy", [False, True])
@pytest.mark.parametrize("accessor", ["parse", "iter_lines", "read"])
async def test_a_streaming_response_is_recorded_once_whichever_accessor_reads_it(
    settled: list[hajer.WrappedCall], asynchronous: bool, lazy: bool, accessor: str
) -> None:
    provider = hajer.wrap(_client(asynchronous=asynchronous, lazy=lazy), settings=SETTINGS)
    manager = at(provider, "chat.completions.with_streaming_response.create")(**REQUEST)
    with hajer.scope():
        if asynchronous:
            async with cast(AbstractAsyncContextManager[object], manager) as response:
                assert type(response).__name__ == "AsyncAPIResponse"
                await _read(response, accessor, asynchronous=True)
        else:
            with cast(AbstractContextManager[object], manager) as response:
                assert type(response).__name__ == "APIResponse"
                await _read(response, accessor, asynchronous=False)
    assert len(settled) == 1
    _answered(settled[0])


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_a_streaming_response_closed_unread_is_recorded_once_and_says_so(
    settled: list[hajer.WrappedCall], asynchronous: bool
) -> None:
    provider = hajer.wrap(_client(asynchronous=asynchronous, lazy=True), settings=SETTINGS)
    manager = at(provider, "chat.completions.with_streaming_response.create")(**REQUEST)
    with hajer.scope():
        if asynchronous:
            async with cast(AbstractAsyncContextManager[object], manager):
                pass
        else:
            with cast(AbstractContextManager[object], manager):
                pass
    assert len(settled) == 1
    assert any(note.startswith("RAW_RESPONSE_NOT_READ") for note in settled[0].limitations)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("lazy", [False, True])
async def test_a_raw_streamed_response_is_recorded_once_from_its_events(
    settled: list[hajer.WrappedCall], asynchronous: bool, lazy: bool
) -> None:
    provider = hajer.wrap(_client(asynchronous=asynchronous, lazy=lazy), settings=SETTINGS)
    with hajer.scope():
        raw = await awaited(
            at(provider, "chat.completions.with_raw_response.create")(
                **REQUEST, stream=True, stream_options={"include_usage": True}
            )
        )
        stream = await awaited(at(raw, "parse")())
        assert type(stream).__name__ in {"Stream", "AsyncStream"}, "the provider's own stream"
        if asynchronous:
            chunks = [chunk async for chunk in cast(AsyncIterator[object], stream)]
        else:
            chunks = list(cast(Iterator[object], stream))
    assert len(chunks) == 3
    assert len(settled) == 1
    (call,) = settled
    assert (call.streamed, call.stream_complete, call.finish_reason) == (True, True, "stop")
    assert call.usage["output_tokens"] == 2
    assert call.content is not None
    assert call.content["streamedText"] == "the answer"


async def test_a_root_level_raw_proxy_built_before_wrap_still_records(settled: list[hajer.WrappedCall]) -> None:
    """A raw holder the application touched before `wrap` binds the unwrapped `create`; wrap rebuilds it."""
    client = _client(asynchronous=False, lazy=False)
    at(client, "with_raw_response.chat.completions")
    at(client, "chat.completions.with_raw_response")
    provider = hajer.wrap(client, settings=SETTINGS)
    with hajer.scope():
        at(at(provider, "with_raw_response.chat.completions.create")(**REQUEST), "parse")()
        at(at(provider, "chat.completions.with_raw_response.create")(**REQUEST), "parse")()
    assert len(settled) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_a_raw_stream_the_application_stops_reading_early_is_incomplete(
    settled: list[hajer.WrappedCall], asynchronous: bool
) -> None:
    provider = hajer.wrap(_client(asynchronous=asynchronous, lazy=True), settings=SETTINGS)
    manager = at(provider, "chat.completions.with_streaming_response.create")(**REQUEST, stream=True)
    with hajer.scope():
        if asynchronous:
            async with cast(AbstractAsyncContextManager[object], manager) as response:
                async for _ in cast(AsyncIterator[bytes], at(response, "iter_bytes")()):
                    break
        else:
            with cast(AbstractContextManager[object], manager) as response:
                for _ in cast(Iterator[bytes], at(response, "iter_bytes")()):
                    break
    assert len(settled) == 1
    assert settled[0].stream_complete is False


def _text_client(*, asynchronous: bool, lazy: bool) -> object:
    """A provider answering 200 with a body that is not JSON: a proxy's HTML page, a plain-text reply."""
    openai = pytest.importorskip("openai")

    def respond(_request: httpx.Request) -> httpx.Response:
        body = b"<html>upstream said hello</html>"
        if lazy:
            return httpx.Response(200, stream=_Lazy(body), headers={"content-type": "text/html"})
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    transport = httpx.MockTransport(respond)
    if asynchronous:
        make = cast(Callable[..., object], openai.AsyncOpenAI)
        return make(api_key="fake", max_retries=0, http_client=httpx.AsyncClient(transport=transport))
    make = cast(Callable[..., object], openai.OpenAI)
    return make(api_key="fake", max_retries=0, http_client=httpx.Client(transport=transport))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("lazy", [False, True])
@pytest.mark.parametrize("door", ["with_raw_response", "with_streaming_response"])
async def test_a_body_that_is_not_json_is_recorded_with_a_limitation_never_dropped(
    settled: list[hajer.WrappedCall], asynchronous: bool, lazy: bool, door: str
) -> None:
    """The parse of a 2xx body that is not JSON raised inside the settle, and the call vanished."""
    provider = hajer.wrap(_text_client(asynchronous=asynchronous, lazy=lazy), settings=SETTINGS)
    with hajer.scope():
        if door == "with_raw_response":
            raw = await awaited(at(provider, "chat.completions.with_raw_response.create")(**REQUEST))
            http = getattr(raw, "http_response")  # noqa: B009
            if asynchronous and not getattr(http, "is_stream_consumed", True):
                await awaited(at(http, "aread")())
            assert "hello" in cast(str, getattr(http, "text"))  # noqa: B009
        else:
            manager = at(provider, "chat.completions.with_streaming_response.create")(**REQUEST)
            if asynchronous:
                async with cast(AbstractAsyncContextManager[object], manager) as response:
                    assert "hello" in cast(str, await awaited(at(response, "text")()))
            else:
                with cast(AbstractContextManager[object], manager) as response:
                    assert "hello" in cast(str, at(response, "text")())
    assert len(settled) == 1
    (call,) = settled
    assert any(note.startswith("RAW_RESPONSE_UNREADABLE") for note in call.limitations)

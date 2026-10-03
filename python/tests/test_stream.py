"""A streamed provider call is recorded — when the stream ends, and never at the interpreter's leisure.

Four claims, one test each, and they are the claims this behaviour rests on:

- an **abandoned** stream is recorded before the `with` block returns, not when it is collected;
- `get_final_message()` — the helper a `text_stream` consumer uses to read the answer — is recorded;
- the proxy **exposes the provider's surface**: nothing a caller could reach before has gone;
- **usage is recorded at exhaustion**, and when the provider named none the record says so instead of
  reporting zero tokens as a fact.

And the fifth, which is the rule the other four rest on: a recording path that fails never reaches the
caller.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import cast

import pytest

import hajer
from hajer._stream import AsyncRecordingStream, RecordingStream, StreamRecorder
from hajer._wire import WrappedCallSummaryIn
from hajer._wrap import STREAM_USAGE_UNOBSERVED
from tests.conftest import as_async_message_stream, as_message_stream, as_stream
from tests.fakes import (
    FakeAnthropic,
    FakeAnthropicDelta,
    FakeAnthropicEvent,
    FakeAnthropicMessage,
    FakeAnthropicUsage,
    FakeAsyncAnthropic,
    FakeAsyncMessageStream,
    FakeAsyncMessageStreamManager,
    FakeChatChunk,
    FakeChatDelta,
    FakeChatStreamChoice,
    FakeMessageStream,
    FakeMessageStreamManager,
    FakeOpenAI,
    FakeOpenAIUsage,
    FakeStream,
    ProviderError,
)

QUIET = hajer.HajerSettings(api_key="k", team_id="t")


def _events() -> list[object]:
    """Three frames of an Anthropic message stream: two text deltas, then the usage and the stop reason."""
    return [
        FakeAnthropicEvent(delta=FakeAnthropicDelta(text="ab")),
        FakeAnthropicEvent(delta=FakeAnthropicDelta(text="c")),
        FakeAnthropicEvent(
            type="message_delta", usage=FakeAnthropicUsage(input_tokens=19, output_tokens=8), stop_reason="end_turn"
        ),
    ]


def _final() -> FakeAnthropicMessage:
    return FakeAnthropicMessage(usage=FakeAnthropicUsage(input_tokens=19, output_tokens=8))


def _helper_client() -> tuple[FakeAnthropic, FakeMessageStream]:
    """A client whose `messages.stream(...)` returns the provider's own manager over one stream."""
    inner = FakeMessageStream(_events(), _final())
    client = FakeAnthropic(stream_script=[FakeMessageStreamManager(inner)])
    return client, inner


class TestTheAbandonedStream:
    def test_abandoned_stream_is_recorded_before_the_with_block_exits(self) -> None:
        """One chunk read, the rest abandoned: the call is recorded, incomplete, as the block returns.

        The assertion is made **inside** the scope and after the inner `with`, which is the whole point:
        `close()` runs from the wrapper's own `__exit__`, so the record exists while the operation is
        still open and the `verify` that will carry it has not been made yet. A `__del__` would settle
        after that, into nothing.
        """
        client, inner = _helper_client()
        wrapped = hajer.wrap(client, settings=QUIET)
        with hajer.scope() as operation:
            with as_message_stream(wrapped.messages.stream(model="claude-fake-1", messages=[], max_tokens=8)) as stream:
                first = next(iter(stream))
            assert getattr(first, "type", None) == "content_block_delta"
            (call,) = operation.calls
            assert call.streamed is True
            assert call.stream_complete is False
            assert call.stream_chunks == 1
            assert call.duration_ms >= 0
        assert inner.closed is True, "the provider's own stream is closed as well"

    def test_an_abandoned_create_stream_settles_on_close(self) -> None:
        """The same for `create(stream=True)`, whose return value is the stream itself."""
        stream = FakeStream(_events())
        wrapped = hajer.wrap(FakeAnthropic(script=[stream]), settings=QUIET)
        returned = as_stream(wrapped.messages.create(model="claude-fake-1", messages=[], max_tokens=8, stream=True))
        assert next(iter(returned)) is stream.chunks[0]
        returned.close()
        (call,) = hajer.wrapped_calls()
        assert call.stream_complete is False
        assert call.stream_chunks == 1
        assert stream.closed is True


class TestTheProviderSurface:
    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_dual_protocol_stream_keeps_the_consumers_choice(self, asynchronous: bool) -> None:
        class DualStream(FakeStream):
            async def __aiter__(self) -> AsyncIterator[object]:
                for chunk in self.chunks:
                    yield chunk

        inner = DualStream(_events())
        client = hajer.wrap(FakeAnthropic(script=[inner]), settings=QUIET)
        returned = client.messages.create(model="claude-fake-1", messages=[], max_tokens=8, stream=True)
        if asynchronous:
            seen = [chunk async for chunk in cast(AsyncIterator[object], returned)]
        else:
            seen = list(cast(Iterator[object], returned))
        assert seen == inner.chunks
        assert all(actual is expected for actual, expected in zip(seen, inner.chunks, strict=True))
        (call,) = hajer.wrapped_calls()
        assert call.stream_complete is True
        assert call.stream_chunks == 3
        assert call.usage == {"input_tokens": 19, "output_tokens": 8}
        assert inner.closed is True

    def test_get_final_message_is_recorded(self) -> None:
        """A `text_stream` consumer reads the answer with `get_final_message()`; that is the record.

        The provider's helper iterates the stream inside its own code, so the frames never cross the
        proxy and `stream_chunks` stays 0 — an honest 0, beside a reading of the whole answer taken from
        the message the helper returned: the model, the stop reason and both token counts.
        """
        client, inner = _helper_client()
        wrapped = hajer.wrap(client, settings=QUIET)
        with as_message_stream(wrapped.messages.stream(model="claude-fake-1", messages=[], max_tokens=8)) as stream:
            text = "".join(stream.text_stream)
            final = stream.get_final_message()
        assert text == "abc", "the provider's own helper still produces the provider's own text"
        assert final is inner.final, "and the value the caller gets back is the provider's, untouched"
        (call,) = hajer.wrapped_calls()
        assert call.stream_chunks == 0
        assert call.stream_complete is True
        assert call.model == "claude-fake-1"
        assert call.finish_reason == "end_turn"
        assert call.usage == {"input_tokens": 19, "output_tokens": 8}
        assert call.limitations == ()

    def test_wrapped_stream_exposes_the_provider_surface(self) -> None:
        """Nothing a caller could reach before is gone: `response`, `text_stream`, `until_done`, `close`."""
        client, inner = _helper_client()
        wrapped = hajer.wrap(client, settings=QUIET)
        with as_message_stream(wrapped.messages.stream(model="claude-fake-1", messages=[], max_tokens=8)) as stream:
            assert stream.response is inner.response, "an attribute is delegated, not copied"
            assert isinstance(stream.text_stream, Iterator)
            stream.until_done()
            assert inner.drained is True, "a draining helper still drains the provider's stream"
            # Delegation is not invention: a name the provider's stream does not carry is still absent.
            absent = "no_such_attribute"
            with pytest.raises(AttributeError):
                getattr(stream, absent)
        (call,) = hajer.wrapped_calls()
        assert call.stream_complete is True, "`until_done()` means the stream ran to the end"

    async def test_the_async_stream_helper_keeps_its_surface_and_records(self) -> None:
        """`__aiter__` / `__anext__` / `aclose()`, and an awaited `get_final_message()` is recorded."""
        inner = FakeAsyncMessageStream(_events(), _final())
        client = FakeAsyncAnthropic(stream_script=[FakeAsyncMessageStreamManager(inner)])
        wrapped = hajer.wrap(client, settings=QUIET)
        manager = as_async_message_stream(wrapped.messages.stream(model="claude-fake-1", messages=[], max_tokens=8))
        async with manager as stream:
            assert stream.response is inner.response
            seen = [chunk async for chunk in stream]
            final = await stream.get_final_message()
        assert len(seen) == 3
        assert final is inner.final
        (call,) = hajer.wrapped_calls()
        assert call.stream_chunks == 3
        assert call.stream_complete is True
        assert call.usage == {"input_tokens": 19, "output_tokens": 8}
        assert inner.closed is True


class TestUsage:
    def test_usage_recorded_at_exhaustion(self) -> None:
        """The counts arrive on the last frame, and the record has them the moment the stream ends."""
        stream = FakeStream(_events())
        wrapped = hajer.wrap(FakeAnthropic(script=[stream]), settings=QUIET)
        returned = as_stream(wrapped.messages.create(model="claude-fake-1", messages=[], max_tokens=8, stream=True))
        assert len(list(returned)) == 3
        (call,) = hajer.wrapped_calls()
        assert call.stream_complete is True
        assert call.stream_chunks == 3
        assert call.finish_reason == "end_turn"
        assert call.usage == {"input_tokens": 19, "output_tokens": 8}
        assert call.limitations == (), "usage was observed, so there is nothing to disclaim"

    def test_an_openai_stream_without_include_usage_records_the_limitation(self) -> None:
        """OpenAI sends usage on the final chunk only when asked. Unasked, the record says so.

        Zero tokens would be a claim; `None` with a sentence naming `stream_options` is the fact.
        """
        chunks: list[object] = [FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="hi"))])]
        wrapped = hajer.wrap(FakeOpenAI(chat_script=[FakeStream(chunks)]), settings=QUIET)
        returned = as_stream(wrapped.chat.completions.create(model="gpt-fake-1", messages=[], stream=True))
        assert len(list(returned)) == 1
        (call,) = hajer.wrapped_calls()
        assert call.usage == {}
        assert call.limitations == (STREAM_USAGE_UNOBSERVED,)
        assert "include_usage" in call.limitations[0]

    def test_include_usage_on_the_final_chunk_is_read(self) -> None:
        """With `stream_options={"include_usage": True}` the last chunk carries usage, and it is read."""
        chunks: list[object] = [
            FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="hi"))]),
            FakeChatChunk(choices=[], usage=FakeOpenAIUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18)),
        ]
        wrapped = hajer.wrap(FakeOpenAI(chat_script=[FakeStream(chunks)]), settings=QUIET)
        returned = as_stream(
            wrapped.chat.completions.create(
                model="gpt-fake-1", messages=[], stream=True, stream_options={"include_usage": True}
            )
        )
        assert len(list(returned)) == 2
        (call,) = hajer.wrapped_calls()
        assert call.usage == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
        assert call.limitations == ()

    def test_the_limitation_travels_on_the_wire(self) -> None:
        """A summary that could not observe usage says so to the service, not only to the caller.

        Without this the wire is ambiguous in the one place it must not be: an empty `usage` from a
        stream that was never asked for token counts looks exactly like a call that used none, and the
        service prices the second and must refuse to price the first. `WrappedCallSummaryIn.limitations`
        is the field that separates them, and `to_wire()` is what fills it.
        """
        chunks: list[object] = [FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="hi"))])]
        wrapped = hajer.wrap(FakeOpenAI(chat_script=[FakeStream(chunks)]), settings=QUIET)
        returned = as_stream(wrapped.chat.completions.create(model="gpt-fake-1", messages=[], stream=True))
        assert len(list(returned)) == 1
        (call,) = hajer.wrapped_calls()
        body = call.to_wire()
        assert body["limitations"] == [STREAM_USAGE_UNOBSERVED]
        assert WrappedCallSummaryIn.model_validate(body).limitations == (STREAM_USAGE_UNOBSERVED,)


class TestRecordingNeverReachesTheCaller:
    def test_recording_failure_never_reaches_the_caller(self) -> None:
        """A recorder that raises is a Hajer bug. It may not become the application's exception.

        The proxy is built here with a recorder whose every callback raises, because that is the only way
        to assert the rule rather than the current absence of bugs: the chunks still arrive, the stream
        still closes, and nothing propagates.
        """
        chunks: list[object] = ["a", "b"]
        inner = FakeStream(chunks)
        stream = RecordingStream(inner, _exploding_recorder())
        assert list(stream) == chunks
        stream.close()
        assert inner.closed is True

    def test_the_provider_exception_is_re_raised_even_when_recording_it_fails(self) -> None:
        """Two failures at once: the provider's, which is the caller's, and the recorder's, which is not."""

        class Exploding(FakeStream):
            def __iter__(self) -> Iterator[object]:
                yield "a"
                raise ProviderError("stream broke")

        stream = RecordingStream(Exploding([]), _exploding_recorder())
        with pytest.raises(ProviderError, match="stream broke"):
            list(stream)

    async def test_the_async_proxy_keeps_the_same_rule(self) -> None:
        inner = FakeAsyncMessageStream(["a", "b"], final=None)
        stream = AsyncRecordingStream(inner, _exploding_recorder())
        assert [chunk async for chunk in stream] == ["a", "b"]
        await stream.aclose()
        assert inner.closed is True


def _exploding_recorder() -> StreamRecorder:
    def explode(*_: object) -> None:
        raise RuntimeError("the recorder is broken")

    return StreamRecorder(chunk=explode, final=explode, failed=explode, settled=explode)

"""`wrap` — what it records, what it refuses to record, and the behaviour it must not change."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest

import hajer
from hajer import _wrap
from tests.conftest import as_async_stream, as_stream
from tests.fakes import (
    FakeAnthropic,
    FakeAnthropicDelta,
    FakeAnthropicEvent,
    FakeAnthropicMessage,
    FakeAnthropicUsage,
    FakeAsyncAnthropic,
    FakeAsyncOpenAI,
    FakeAsyncStream,
    FakeChatAnthropic,
    FakeChatChunk,
    FakeChatCompletion,
    FakeChatDelta,
    FakeChatStreamChoice,
    FakeChoice,
    FakeFunction,
    FakeMessage,
    FakeOpenAI,
    FakeOpenAIUsage,
    FakeResponse,
    FakeResponseItem,
    FakeStream,
    FakeTextBlock,
    FakeToolCall,
    FakeToolUseBlock,
    NotAProviderClient,
    ProviderError,
)

QUIET = hajer.HajerSettings(capture_content=False)
LOUD = hajer.HajerSettings(capture_content=True)


def _chat_stream() -> FakeStream:
    return FakeStream(
        [
            FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="he"))]),
            FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="llo"))]),
            FakeChatChunk(
                choices=[FakeChatStreamChoice(delta=FakeChatDelta(), finish_reason="stop")],
                usage=FakeOpenAIUsage(),
            ),
        ]
    )


class TestIdentityAndBehaviour:
    def test_wrap_returns_the_same_object(self) -> None:
        client = FakeOpenAI()
        assert hajer.wrap(client, settings=QUIET) is client

    def test_the_provider_object_is_returned_unchanged(self) -> None:
        expected = FakeChatCompletion(id="chatcmpl-42")
        client = hajer.wrap(FakeOpenAI(chat_script=[expected]), settings=QUIET)
        assert client.chat.completions.create(model="gpt-fake-1", messages=[]) is expected

    def test_the_provider_exception_is_re_raised_unchanged(self) -> None:
        boom = ProviderError("rate limited")
        client = hajer.wrap(FakeOpenAI(error=boom), settings=QUIET)
        with pytest.raises(ProviderError) as raised:
            client.chat.completions.create(model="gpt-fake-1", messages=[])
        assert raised.value is boom
        call = hajer.wrapped_calls()[0]
        assert call.error == "rate limited"
        assert call.error_type == "ProviderError"

    async def test_the_async_provider_exception_is_re_raised_unchanged(self) -> None:
        boom = ProviderError("overloaded")
        client = hajer.wrap(FakeAsyncAnthropic(error=boom), settings=QUIET)
        with pytest.raises(ProviderError) as raised:
            await client.messages.create(model="claude-fake-1", messages=[])
        assert raised.value is boom
        assert hajer.wrapped_calls()[0].error_type == "ProviderError"

    def test_the_caller_arguments_reach_the_provider_untouched(self) -> None:
        client = hajer.wrap(FakeOpenAI(), settings=QUIET)
        client.chat.completions.create(model="gpt-fake-1", messages=[{"role": "user", "content": "hi"}], seed=7)
        assert client.completions.calls[0]["seed"] == 7
        assert client.completions.calls[0]["model"] == "gpt-fake-1"

    def test_wrapping_twice_instruments_once(self) -> None:
        client = hajer.wrap(hajer.wrap(FakeOpenAI(), settings=QUIET), settings=QUIET)
        client.chat.completions.create(model="gpt-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 1

    def test_an_unrecognised_client_is_refused_rather_than_silently_ignored(self) -> None:
        with pytest.raises(hajer.UnsupportedClientError):
            hajer.wrap(NotAProviderClient(), settings=QUIET)


class TestOpenAIChatCompletions:
    def test_records_the_call_without_content(self) -> None:
        response = FakeChatCompletion(
            choices=[
                FakeChoice(
                    message=FakeMessage(
                        content="the answer",
                        tool_calls=[FakeToolCall(id="call-1", function=FakeFunction("lookup_order", '{"id":"o-1"}'))],
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
        client = hajer.wrap(FakeOpenAI(chat_script=[response]), settings=QUIET)
        client.chat.completions.create(
            model="gpt-fake-1",
            messages=[
                {"role": "user", "content": "where is my order"},
                {"role": "tool", "tool_call_id": "call-0", "content": "shipped"},
            ],
            temperature=0.2,
            tools=[{"type": "function", "function": {"name": "lookup_order"}}],
        )
        call = hajer.wrapped_calls()[0]
        assert call.provider == "openai"
        assert call.api == "chat.completions"
        assert call.model == "gpt-fake-1"
        assert call.request_settings["temperature"] == 0.2
        assert call.declared_tools == ("lookup_order",)
        assert call.message_count == 2
        assert call.message_roles == ("user", "tool")
        assert call.usage == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
        assert call.tool_calls[0].name == "lookup_order"
        assert call.tool_calls[0].id == "call-1"
        assert call.tool_results[0].tool_use_id == "call-0"
        assert call.finish_reason == "tool_calls"
        assert call.response_id == "chatcmpl-1"
        assert call.streamed is False
        assert call.retries == 0
        assert call.duration_ms >= 0

    def test_content_is_absent_unless_opted_in(self) -> None:
        response = FakeChatCompletion()
        client = hajer.wrap(FakeOpenAI(chat_script=[response]), settings=QUIET)
        client.chat.completions.create(model="gpt-fake-1", messages=[{"role": "user", "content": "secret"}])
        call = hajer.wrapped_calls()[0]
        assert call.content is None
        assert "secret" not in repr(call)

    def test_content_is_present_when_opted_in(self) -> None:
        client = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion()]), settings=LOUD)
        client.chat.completions.create(model="gpt-fake-1", messages=[{"role": "user", "content": "secret"}])
        call = hajer.wrapped_calls()[0]
        assert call.content is not None
        assert "secret" in repr(call.content["messages"])
        assert call.content["output"] == ["ok"]

    def test_tool_arguments_are_content_and_follow_the_same_switch(self) -> None:
        response = FakeChatCompletion(
            choices=[
                FakeChoice(
                    message=FakeMessage(
                        tool_calls=[FakeToolCall(id="c", function=FakeFunction("refund", '{"amount": 99}'))]
                    )
                )
            ]
        )
        quiet = hajer.wrap(FakeOpenAI(chat_script=[response]), settings=QUIET)
        quiet.chat.completions.create(model="m", messages=[])
        assert hajer.wrapped_calls()[0].tool_calls[0].arguments is None
        assert hajer.wrapped_calls()[0].tool_calls[0].name == "refund"


class TestAsyncOpenAI:
    async def test_async_chat_completion_is_returned_unchanged(self) -> None:
        expected = FakeChatCompletion(id="chatcmpl-async")
        client = hajer.wrap(FakeAsyncOpenAI(chat_script=[expected]), settings=QUIET)
        returned = await client.chat.completions.create(model="gpt-fake-1", messages=[])
        assert returned is expected
        call = hajer.wrapped_calls()[0]
        assert call.provider == "openai"
        assert call.response_id == "chatcmpl-async"
        assert call.streamed is False

    async def test_async_streamed_chat_completion(self) -> None:
        chunks: list[object] = [
            FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="a"))]),
            FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(), finish_reason="stop")]),
        ]
        stream = FakeAsyncStream(chunks)
        client = hajer.wrap(FakeAsyncOpenAI(chat_script=[stream]), settings=QUIET)
        returned = as_async_stream(await client.chat.completions.create(model="gpt-fake-1", messages=[], stream=True))
        assert [chunk async for chunk in returned] == chunks
        call = hajer.wrapped_calls()[0]
        assert call.streamed is True
        assert call.stream_complete is True
        assert call.finish_reason == "stop"


class TestOpenAIResponses:
    def test_records_a_responses_call(self) -> None:
        response = FakeResponse(
            output=[FakeResponseItem(type="function_call", name="send_email", call_id="fc-1", arguments="{}")]
        )
        client = hajer.wrap(FakeOpenAI(responses_script=[response]), settings=QUIET)
        client.responses.create(model="gpt-fake-1", input=[{"role": "user", "content": "hi"}])
        call = hajer.wrapped_calls()[0]
        assert call.api == "responses"
        assert call.tool_calls[0].name == "send_email"
        assert call.finish_reason == "completed"
        assert call.message_count == 1


class TestAnthropicMessages:
    def test_records_a_messages_call(self) -> None:
        response = FakeAnthropicMessage(
            content=[FakeTextBlock(text="hello"), FakeToolUseBlock(id="tu-1", name="lookup", input={"id": "o-1"})]
        )
        client = hajer.wrap(FakeAnthropic(script=[response]), settings=QUIET)
        client.messages.create(
            model="claude-fake-1",
            max_tokens=256,
            messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu-0", "is_error": False}]}],
        )
        call = hajer.wrapped_calls()[0]
        assert call.provider == "anthropic"
        assert call.api == "messages"
        assert call.request_settings["max_tokens"] == 256
        assert call.usage == {"input_tokens": 13, "output_tokens": 5}
        assert call.tool_calls[0].name == "lookup"
        assert call.tool_calls[0].arguments is None
        assert call.tool_results[0].tool_use_id == "tu-0"
        assert call.tool_results[0].is_error is False
        assert call.finish_reason == "end_turn"

    def test_the_beta_messages_surface_is_instrumented_too(self) -> None:
        """A client that sends `betas` calls `beta.messages.create`, a different function entirely.

        `langchain_anthropic` picks that branch whenever the payload carries `betas`, so an
        uninstrumented `beta` would be a capture hole that looks exactly like a workflow making no model
        calls at all.
        """
        client = hajer.wrap(FakeAnthropic(), settings=QUIET)
        client.beta.messages.create(model="claude-fake-1", messages=[], betas=["some-beta"])
        call = hajer.wrapped_calls()[0]
        assert call.provider == "anthropic"
        assert call.api == "messages"
        assert call.model == "claude-fake-1"

    async def test_async_messages(self) -> None:
        client = hajer.wrap(FakeAsyncAnthropic(script=[FakeAnthropicMessage()]), settings=QUIET)
        result = await client.messages.create(model="claude-fake-1", messages=[], max_tokens=16)
        assert isinstance(result, FakeAnthropicMessage)
        assert hajer.wrapped_calls()[0].model == "claude-fake-1"


class TestStreaming:
    def test_a_consumed_sync_stream_is_complete(self) -> None:
        stream = _chat_stream()
        client = hajer.wrap(FakeOpenAI(chat_script=[stream]), settings=QUIET)
        returned = as_stream(client.chat.completions.create(model="gpt-fake-1", messages=[], stream=True))
        chunks = list(returned)
        assert chunks == stream.chunks, "the chunks are the provider's own objects"
        call = hajer.wrapped_calls()[0]
        assert call.streamed is True
        assert call.stream_complete is True
        assert call.stream_chunks == 3
        assert call.finish_reason == "stop"
        assert call.usage["total_tokens"] == 18
        assert call.content is None

    def test_an_abandoned_sync_stream_is_incomplete(self) -> None:
        client = hajer.wrap(FakeOpenAI(chat_script=[_chat_stream()]), settings=QUIET)
        returned = as_stream(client.chat.completions.create(model="gpt-fake-1", messages=[], stream=True))
        iterator = iter(returned)
        next(iterator)
        returned.close()
        call = hajer.wrapped_calls()[0]
        assert call.stream_complete is False
        assert call.stream_chunks == 1

    def test_a_sync_stream_forwards_the_context_manager_and_closes_the_inner(self) -> None:
        stream = _chat_stream()
        client = hajer.wrap(FakeOpenAI(chat_script=[stream]), settings=QUIET)
        with as_stream(client.chat.completions.create(model="gpt-fake-1", messages=[], stream=True)) as returned:
            assert list(returned) == stream.chunks
        assert stream.closed is True
        assert hajer.wrapped_calls()[0].stream_complete is True

    def test_streamed_text_is_captured_only_when_opted_in(self) -> None:
        client = hajer.wrap(FakeOpenAI(chat_script=[_chat_stream()]), settings=LOUD)
        list(as_stream(client.chat.completions.create(model="gpt-fake-1", messages=[], stream=True)))
        content = hajer.wrapped_calls()[0].content
        assert content is not None
        assert content["streamedText"] == "hello"

    def test_a_sync_stream_that_raises_is_incomplete_and_re_raises(self) -> None:
        class Exploding(FakeStream):
            def __iter__(self) -> Iterator[object]:
                yield FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="x"))])
                raise ProviderError("stream broke")

        client = hajer.wrap(FakeOpenAI(chat_script=[Exploding([])]), settings=QUIET)
        returned = as_stream(client.chat.completions.create(model="gpt-fake-1", messages=[], stream=True))
        with pytest.raises(ProviderError):
            list(returned)
        call = hajer.wrapped_calls()[0]
        assert call.stream_complete is False
        assert call.error == "stream broke"

    async def test_a_consumed_async_stream_is_complete(self) -> None:
        chunks: list[object] = [
            FakeAnthropicEvent(delta=FakeAnthropicDelta(text="he")),
            FakeAnthropicEvent(delta=FakeAnthropicDelta(text="llo"), stop_reason="end_turn"),
            FakeAnthropicEvent(type="message_delta", usage=FakeAnthropicUsage(input_tokens=3, output_tokens=2)),
        ]
        stream = FakeAsyncStream(chunks)
        client = hajer.wrap(FakeAsyncAnthropic(script=[stream]), settings=QUIET)
        returned = as_async_stream(
            await client.messages.create(model="claude-fake-1", messages=[], max_tokens=8, stream=True)
        )
        seen = [chunk async for chunk in returned]
        assert seen == chunks
        call = hajer.wrapped_calls()[0]
        assert call.streamed is True
        assert call.stream_complete is True
        assert call.stream_chunks == 3
        assert call.finish_reason == "end_turn"
        assert call.usage == {"input_tokens": 3, "output_tokens": 2}

    async def test_an_async_stream_forwards_aclose_and_records_incomplete(self) -> None:
        stream = FakeAsyncStream([FakeAnthropicEvent(delta=FakeAnthropicDelta(text="a"))])
        client = hajer.wrap(FakeAsyncAnthropic(script=[stream]), settings=QUIET)
        returned = as_async_stream(
            await client.messages.create(model="claude-fake-1", messages=[], max_tokens=8, stream=True)
        )
        await returned.aclose()
        assert stream.closed is True
        assert hajer.wrapped_calls()[0].stream_complete is False


class TestContextAndBounds:
    def test_two_calls_are_attached_to_the_next_verify(self) -> None:
        client = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion(), FakeChatCompletion()]), settings=QUIET)
        client.chat.completions.create(model="gpt-fake-1", messages=[])
        client.chat.completions.create(model="gpt-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 2
        hajer.clear_wrapped_calls()
        assert hajer.wrapped_calls() == ()
        assert hajer.wrapped_calls_dropped() == 0

    def test_the_cap_keeps_the_first_and_counts_the_rest(self) -> None:
        capped = hajer.HajerSettings(wrapped_calls_max=2)
        client = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion() for _ in range(5)]), settings=capped)
        for _ in range(5):
            client.chat.completions.create(model="gpt-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 2
        assert hajer.wrapped_calls_dropped() == 3

    def test_two_calls_are_attached_to_the_next_verify_is_now_the_task_context(self) -> None:
        """Calls outside every scope accumulate on the task until something clears them."""
        client = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion()]), settings=QUIET)
        client.chat.completions.create(model="gpt-fake-1", messages=[{"role": "user", "content": "hi"}])
        call = hajer.wrapped_calls()[0]
        assert call.started_ns > 0
        assert call.started_at.endswith("+00:00")


class TestTheCallObserver:
    """The seam the span emitter listens on: told once when a call opens, once when it settles, never raising."""

    @pytest.fixture(autouse=True)
    def _no_observer_leaks(self) -> Iterator[None]:
        yield
        _wrap.set_call_observer(None)

    def test_opened_then_settled_with_the_handle_it_was_given(self) -> None:
        seen: list[tuple[str, object]] = []

        class Observer:
            def opened(self, call: hajer.WrappedCall) -> object:
                seen.append(("opened", call.model))
                return {"handle": call.model}

            def settled(self, call: hajer.WrappedCall, handle: object | None) -> None:
                seen.append(("settled", (call.response_id, handle)))

        _wrap.set_call_observer(Observer())
        assert _wrap.call_observer() is not None
        client = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion(id="chatcmpl-obs")]), settings=QUIET)
        client.chat.completions.create(model="gpt-fake-1", messages=[])
        assert seen == [("opened", "gpt-fake-1"), ("settled", ("chatcmpl-obs", {"handle": "gpt-fake-1"}))]

    def test_a_stream_settles_when_the_stream_ends(self) -> None:
        settled: list[bool | None] = []

        class Observer:
            def opened(self, call: hajer.WrappedCall) -> object:
                return None

            def settled(self, call: hajer.WrappedCall, handle: object | None) -> None:
                settled.append(call.stream_complete)

        _wrap.set_call_observer(Observer())
        client = hajer.wrap(FakeOpenAI(chat_script=[_chat_stream()]), settings=QUIET)
        returned = as_stream(client.chat.completions.create(model="gpt-fake-1", messages=[], stream=True))
        assert settled == [], "a stream has not settled while it is still being read"
        list(returned)
        assert settled == [True]

    def test_an_observer_that_raises_costs_nothing_to_the_call(self) -> None:
        class Broken:
            def opened(self, call: hajer.WrappedCall) -> object:
                raise RuntimeError("opened broke")

            def settled(self, call: hajer.WrappedCall, handle: object | None) -> None:
                raise RuntimeError("settled broke")

        _wrap.set_call_observer(Broken())
        expected = FakeChatCompletion(id="chatcmpl-fine")
        client = hajer.wrap(FakeOpenAI(chat_script=[expected]), settings=QUIET)
        assert client.chat.completions.create(model="gpt-fake-1", messages=[]) is expected
        assert hajer.wrapped_calls()[0].response_id == "chatcmpl-fine"

    def test_a_failed_call_still_settles_and_carries_its_status(self) -> None:
        errors: list[tuple[str | None, int | None]] = []

        class Observer:
            def opened(self, call: hajer.WrappedCall) -> object:
                return None

            def settled(self, call: hajer.WrappedCall, handle: object | None) -> None:
                errors.append((call.error_type, call.error_status))

        _wrap.set_call_observer(Observer())
        client = hajer.wrap(FakeAnthropic(error=ProviderError("overloaded", status_code=529)), settings=QUIET)
        with pytest.raises(ProviderError):
            client.messages.create(model="claude-fake-1", messages=[])
        assert errors == [("ProviderError", 529)]


class TestAFrameworkChatModel:
    """`wrap(ChatAnthropic(...))`: the chat model carries no surface, its inner clients do.

    The subject repository of the s0-s3 acceptance run injects a `ChatAnthropic` into its own agent and
    the agent calls `bind_tools` on it, so the object the five inline lines can reach is the chat model
    and never the provider client underneath. These tests pin the two things that have to keep being
    true: the same object comes back (the agent holds a reference to it), and the call that leaves the
    process is the one that is recorded.
    """

    def test_wrap_returns_the_same_chat_model(self) -> None:
        model = FakeChatAnthropic()
        assert hajer.wrap(model, settings=QUIET) is model

    def test_the_sync_inner_client_is_recorded(self) -> None:
        expected = FakeAnthropicMessage(id="msg-inner")
        model = hajer.wrap(FakeChatAnthropic(script=[expected]), settings=QUIET)
        assert model.invoke([{"role": "user", "content": "hi"}], max_tokens=64) is expected
        call = hajer.wrapped_calls()[0]
        assert call.provider == "anthropic"
        assert call.api == "messages"
        assert call.model == "claude-fake-1"
        assert call.response_id == "msg-inner"
        assert call.usage == {"input_tokens": 13, "output_tokens": 5}

    async def test_the_async_inner_client_is_recorded(self) -> None:
        model = hajer.wrap(FakeChatAnthropic(script=[FakeAnthropicMessage()]), settings=QUIET)
        result = await model.ainvoke([])
        assert isinstance(result, FakeAnthropicMessage)
        assert hajer.wrapped_calls()[0].api == "messages"

    def test_binding_tools_does_not_lose_the_instrumentation(self) -> None:
        """The agent binds tools after `wrap`, and `bind_tools` returns the model, not a fresh client."""
        model = hajer.wrap(FakeChatAnthropic(), settings=QUIET)
        bound = model.bind_tools([{"name": "SubmitCriteria"}])
        bound.invoke([])
        assert len(hajer.wrapped_calls()) == 1

    def test_the_provider_exception_still_arrives_unchanged(self) -> None:
        boom = ProviderError("overloaded")
        model = hajer.wrap(FakeChatAnthropic(error=boom), settings=QUIET)
        with pytest.raises(ProviderError) as raised:
            model.invoke([])
        assert raised.value is boom
        assert hajer.wrapped_calls()[0].error_type == "ProviderError"

    def test_a_real_provider_client_is_still_instrumented_at_its_own_surface(self) -> None:
        """A real provider client keeps its httpx client on `_client` too (the fake does as well); the
        inner search must not shadow a surface the object itself already carries."""
        client = FakeAnthropic()
        assert getattr(client, "_client", None) is not None
        wrapped = hajer.wrap(client, settings=QUIET)
        wrapped.messages.create(model="claude-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 1


class TestContentBounds:
    """Content is bounded by `HAJER_WRAPPED_CALL_MAX_BYTES`: clipped in the middle, never dropped whole."""

    def test_a_long_prompt_is_clipped_in_the_content(self) -> None:
        prompt = "HEAD " + "m" * 50_000 + " TAIL"
        client = hajer.wrap(FakeAnthropic(), settings=LOUD)
        client.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": prompt}], max_tokens=16)
        call = hajer.wrapped_calls()[0]
        content = json.dumps(call.content, ensure_ascii=False, separators=(",", ":"))
        assert "HEAD m" in content
        assert "m TAIL" in content
        assert len(content.encode()) <= LOUD.wrapped_call_max_bytes
        assert any(limitation.startswith("CONTENT_TRUNCATED") for limitation in call.limitations)

    def test_a_message_whose_shape_alone_does_not_fit_keeps_its_two_ends(self) -> None:
        """Past the smallest clip the content still cannot fit: the text's two ends are kept, never nothing."""
        settings = hajer.HajerSettings(capture_content=True, wrapped_call_max_bytes=64)
        client = hajer.wrap(FakeAnthropic(), settings=settings)
        client.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": "q" * 5_000}])
        call = hajer.wrapped_calls()[0]
        assert call.usage == {"input_tokens": 13, "output_tokens": 5}
        assert "qqqq" in json.dumps(call.content), "never the text dropped entirely"

    def test_a_failed_call_with_no_status_records_the_error_only(self) -> None:
        """A connection that never opened has no status line; the record says the error and nothing invented."""
        client = hajer.wrap(FakeAnthropic(error=ProviderError("connection refused")), settings=LOUD)
        with pytest.raises(ProviderError):
            client.messages.create(model="claude-fake-1", messages=[])
        call = hajer.wrapped_calls()[0]
        assert (call.error_type, call.error_status) == ("ProviderError", None)


class TestTheTaskBoundary:
    """A call made in a child task reaches the operation that opened the scope. F-SDK-2, closed.

    `wrap` records into a per-task `contextvars` context, and a child task gets a *copy* of it — so a
    call recorded inside `asyncio.gather` never reached the `verify` in the parent. `langchain_core`
    1.5.1's `BaseChatModel.agenerate` fans its calls out exactly that way, measured against a real
    subject repository: four provider calls made, zero wrapped calls attached to the two submissions
    that followed them.

    `hajer.scope()` is the fix, and these tests are the two halves of why it is a scope rather than one
    sink installed at `wrap()` time. **Inside** a scope the sink is a mutable object, so a copied
    context still points at the same list and the parent sees what its children recorded. **Outside**
    every scope nothing changed: a sink shared by every context descended from client construction
    would attach one request's model calls to another request's `verify`, because a server wraps its
    client once at start-up and every request task descends from that context. Under-capture is safe;
    over-attachment is not.
    """

    async def test_a_call_in_a_child_task_is_not_seen_by_the_parent_outside_a_scope(self) -> None:
        """The unchanged half: with no operation open, the task boundary is still the boundary."""
        client = hajer.wrap(FakeAsyncAnthropic(), settings=QUIET)
        seen_by_the_child: list[int] = []

        async def child() -> None:
            await client.messages.create(model="claude-fake-1", messages=[])
            seen_by_the_child.append(len(hajer.wrapped_calls()))

        # `gather` wraps the coroutine in a Task, and a Task copies the context it was created in.
        await asyncio.gather(child())

        assert seen_by_the_child == [1]
        assert hajer.wrapped_calls() == ()

    async def test_a_call_awaited_in_the_same_task_is_seen(self) -> None:
        client = hajer.wrap(FakeAsyncAnthropic(), settings=QUIET)
        await client.messages.create(model="claude-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 1

    async def test_a_scope_sees_the_calls_its_child_tasks_made(self) -> None:
        """The fix: `asyncio.gather`'s children append to the operation the parent is holding."""
        client = hajer.wrap(FakeAsyncAnthropic(), settings=QUIET)

        async def child(index: int) -> None:
            await client.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": str(index)}])

        with hajer.scope() as operation:
            await asyncio.gather(child(1), child(2), child(3))
            assert len(hajer.wrapped_calls()) == 3
            assert len(operation.calls) == 3
            assert operation.dropped == 0

    async def test_the_fan_out_a_framework_performs_reaches_the_submission(self) -> None:
        """The measured shape: a chat model's own `gather`, and a `verify` that carries all four calls."""
        model = hajer.wrap(FakeChatAnthropic(), settings=QUIET)

        async def agenerate(prompts: int) -> None:
            await asyncio.gather(*(model.ainvoke([{"role": "user", "content": str(n)}]) for n in range(prompts)))

        with hajer.scope():
            await agenerate(4)
            attached = hajer.wrapped_calls()
        assert len(attached) == 4
        assert {call.provider for call in attached} == {"anthropic"}

    async def test_a_scope_holds_calls_made_in_a_worker_thread(self) -> None:
        """`asyncio.to_thread` copies the context too, so the sink is locked and not merely shared."""
        client = hajer.wrap(FakeAnthropic(), settings=QUIET)

        def call() -> None:
            client.messages.create(model="claude-fake-1", messages=[])

        with hajer.scope() as operation:
            await asyncio.gather(*(asyncio.to_thread(call) for _ in range(8)))
            assert len(operation.calls) == 8

    def test_reading_the_operation_does_not_take_the_calls(self) -> None:
        client = hajer.wrap(FakeAnthropic(), settings=QUIET)
        with hajer.scope() as operation:
            client.messages.create(model="claude-fake-1", messages=[])
            assert len(operation.calls) == 1
            assert len(operation.calls) == 1
            assert len(hajer.wrapped_calls()) == 1

    def test_the_operation_cap_counts_what_it_refused(self) -> None:
        client = hajer.wrap(FakeAnthropic(), settings=hajer.HajerSettings(wrapped_calls_max=2))
        with hajer.scope() as operation:
            for _ in range(5):
                client.messages.create(model="claude-fake-1", messages=[])
            assert len(operation.calls) == 2
            assert operation.dropped == 3
            assert hajer.wrapped_calls_dropped() == 3

    def test_a_closed_scope_leaves_the_task_context_alone(self) -> None:
        """A call made after the block belongs to the task again, and the operation is closed."""
        client = hajer.wrap(FakeAnthropic(), settings=QUIET)
        with hajer.scope() as operation:
            client.messages.create(model="claude-fake-1", messages=[])
        client.messages.create(model="claude-fake-1", messages=[])
        assert len(operation.calls) == 1
        assert len(hajer.wrapped_calls()) == 1

    def test_nested_scopes_take_their_own_calls(self) -> None:
        client = hajer.wrap(FakeAnthropic(), settings=QUIET)
        with hajer.scope() as outer:
            client.messages.create(model="claude-fake-1", messages=[])
            with hajer.scope() as inner:
                client.messages.create(model="claude-fake-1", messages=[])
                assert len(inner.calls) == 1
            client.messages.create(model="claude-fake-1", messages=[])
            assert len(outer.calls) == 2

    def test_clearing_inside_a_scope_empties_the_operation(self) -> None:
        """What `verify` does: it takes what the operation holds, and the block continues from empty."""
        client = hajer.wrap(FakeAnthropic(), settings=QUIET)
        with hajer.scope() as operation:
            client.messages.create(model="claude-fake-1", messages=[])
            hajer.clear_wrapped_calls()
            assert operation.calls == ()
            client.messages.create(model="claude-fake-1", messages=[])
            assert len(operation.calls) == 1

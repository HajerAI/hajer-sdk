"""Every model call `wrap` records is a `gen_ai` span: named, attributed and timed as the conventions say.

Asserted on exported spans through the shared in-memory provider (`emitting`), one provider dialect at a time, and
on the rules around them: content only with capture and only redacted and bounded, an error by its type and never
its message, a stream settled when the stream ends, another instrumentation's span around the same call suppressing
the SDK's own, and the code running whether or not the OTel half is there.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import cast

import pytest

import hajer
from hajer import _model_spans, _semconv, _telemetry
from hajer._json import JsonObject
from hajer._wrap import CONTENT_TRUNCATED, WrappedCall
from tests.conftest import Emitting, as_stream
from tests.fakes import (
    FakeAnthropic,
    FakeAnthropicMessage,
    FakeChatChunk,
    FakeChatCompletion,
    FakeChatDelta,
    FakeChatStreamChoice,
    FakeChoice,
    FakeFunction,
    FakeGenaiClient,
    FakeMessage,
    FakeOpenAI,
    FakeOpenAIUsage,
    FakeResponse,
    FakeResponseItem,
    FakeStream,
    FakeTextBlock,
    FakeToolCall,
    FakeToolUseBlock,
    ProviderError,
    fake_litellm,
)

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, SpanKind, StatusCode

LOUD = hajer.HajerSettings(capture_content=True)
QUIET = hajer.HajerSettings(capture_content=False)


def _spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return list(cast(Sequence[ReadableSpan], exporter.get_finished_spans()))


def _only(exporter: InMemorySpanExporter) -> ReadableSpan:
    (span,) = _spans(exporter)
    return span


def _attributes(span: ReadableSpan) -> dict[str, object]:
    return dict(span.attributes or {})


def _context(span: ReadableSpan) -> SpanContext:
    assert span.context is not None
    return span.context


def _messages(span: ReadableSpan, key: str) -> list[JsonObject]:
    value = _attributes(span)[key]
    assert isinstance(value, str)
    parsed = json.loads(value)
    assert isinstance(parsed, list)
    return cast(list[JsonObject], parsed)


class TestOpenAIChat:
    def test_a_chat_call_is_a_chat_span_in_the_conventions_words(self, emitting: Emitting) -> None:
        exporter = emitting(LOUD)
        answer = FakeChatCompletion(
            choices=[
                FakeChoice(
                    message=FakeMessage(
                        content="the answer",
                        tool_calls=[FakeToolCall(id="call-1", function=FakeFunction("lookup", '{"id": "o-1"}'))],
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
        client = hajer.wrap(FakeOpenAI(chat_script=[answer]), settings=LOUD)
        with hajer.workflow("wf_support"):
            client.chat.completions.create(
                model="gpt-fake-1",
                messages=[{"role": "system", "content": "Be terse."}, {"role": "user", "content": "where is o-1"}],
                temperature=0.2,
                max_tokens=64,
                stop=["END"],
                tools=[{"type": "function", "function": {"name": "lookup"}}],
            )
        spans = {span.name: span for span in _spans(exporter)}
        chat = spans["chat gpt-fake-1"]
        assert chat.kind is SpanKind.CLIENT
        assert chat.parent is not None
        assert chat.parent.span_id == _context(spans["workflow wf_support"]).span_id
        attributes = _attributes(chat)
        assert attributes[_semconv.OPERATION_NAME] == "chat"
        assert attributes[_semconv.PROVIDER_NAME] == "openai"
        assert attributes[_semconv.API] == "chat.completions"
        assert (attributes[_semconv.REQUEST_MODEL], attributes[_semconv.RESPONSE_MODEL]) == ("gpt-fake-1", "gpt-fake-1")
        assert attributes[_semconv.REQUEST_TEMPERATURE] == 0.2
        assert attributes[_semconv.REQUEST_MAX_TOKENS] == 64
        assert attributes[_semconv.REQUEST_STOP_SEQUENCES] == ("END",)
        assert attributes[_semconv.REQUEST_TOOL_NAMES] == ("lookup",)
        assert attributes[_semconv.RESPONSE_ID] == "chatcmpl-1"
        assert attributes[_semconv.RESPONSE_FINISH_REASONS] == ("tool_calls",)
        assert (attributes[_semconv.USAGE_INPUT_TOKENS], attributes[_semconv.USAGE_OUTPUT_TOKENS]) == (11, 7)
        assert attributes[_semconv.USAGE_TOTAL_TOKENS] == 18
        assert attributes[_semconv.WORKFLOW_ID] == "wf_support"
        assert str(attributes[_semconv.CODE_FUNCTION_NAME]).endswith(
            "test_a_chat_call_is_a_chat_span_in_the_conventions_words"
        )
        assert attributes[_semconv.CODE_FILE_PATH] == "tests/test_model_spans.py"
        assert isinstance(attributes[_semconv.CODE_LINE_NUMBER], int)
        assert _semconv.STREAM not in attributes
        assert _semconv.ERROR_TYPE not in attributes
        assert _messages(chat, _semconv.INPUT_MESSAGES) == [
            {"role": "system", "parts": [{"type": "text", "content": "Be terse."}]},
            {"role": "user", "parts": [{"type": "text", "content": "where is o-1"}]},
        ]
        assert _messages(chat, _semconv.OUTPUT_MESSAGES) == [
            {
                "role": "assistant",
                "parts": [
                    {"type": "text", "content": "the answer"},
                    {"type": "tool_call", "id": "call-1", "name": "lookup", "arguments": {"id": "o-1"}},
                ],
                "finish_reason": "tool_calls",
            }
        ]
        assert _semconv.SYSTEM_INSTRUCTIONS not in attributes, "OpenAI chat keeps its instructions in the messages"

    def test_the_span_is_backdated_to_the_call_and_ends_when_it_settled(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        client = hajer.wrap(FakeOpenAI(), settings=QUIET)
        client.chat.completions.create(model="gpt-fake-1", messages=[])
        (call,) = hajer.wrapped_calls()
        span = _only(exporter)
        assert span.start_time == call.started_ns
        assert span.end_time == call.started_ns + call.duration_ms * 1_000_000
        assert span.parent is None, "outside any workflow the model call is a trace of its own"

    def test_content_is_absent_without_capture(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        client = hajer.wrap(FakeOpenAI(), settings=QUIET)
        client.chat.completions.create(model="gpt-fake-1", messages=[{"role": "user", "content": "a secret"}])
        attributes = _attributes(_only(exporter))
        assert not {_semconv.INPUT_MESSAGES, _semconv.OUTPUT_MESSAGES, _semconv.SYSTEM_INSTRUCTIONS} & set(attributes)
        assert "a secret" not in json.dumps(_only(exporter).to_json())
        assert attributes[_semconv.USAGE_INPUT_TOKENS] == 11, "shapes, counts and usage still travel"

    def test_content_is_redacted_under_the_catalog_and_the_configured_policy(self, emitting: Emitting) -> None:
        exporter = emitting(LOUD)
        hajer.configure(
            LOUD,
            tracer_provider=_provider_of(exporter),
            policy=hajer.build_policy(extra_rules=(("TICKET", r"\bTK-\d+\b"),)),
        )
        answer = FakeChatCompletion(choices=[FakeChoice(FakeMessage(content="ticket TK-9 for dana@example.com"))])
        client = hajer.wrap(FakeOpenAI(chat_script=[answer]), settings=LOUD)
        client.chat.completions.create(
            model="gpt-fake-1", messages=[{"role": "user", "content": "card 4111 1111 1111 1111"}]
        )
        span = _only(exporter)
        rendered = json.dumps(span.to_json())
        assert "4111" not in rendered
        assert "dana@example.com" not in rendered
        assert "TK-9" not in rendered
        assert "[redacted:CARD]" in rendered
        assert "[redacted:TICKET]" in rendered

    def test_content_is_bounded_and_the_span_says_so(self, emitting: Emitting) -> None:
        bounded = hajer.HajerSettings(capture_content=True, wrapped_call_max_bytes=2_048)
        exporter = emitting(bounded)
        client = hajer.wrap(FakeOpenAI(), settings=bounded)
        client.chat.completions.create(
            model="gpt-fake-1", messages=[{"role": "user", "content": "HEAD " + "m" * 20_000 + " TAIL"}]
        )
        attributes = _attributes(_only(exporter))
        carried = attributes[_semconv.INPUT_MESSAGES]
        assert isinstance(carried, str)
        assert len(carried.encode("utf-8")) <= 2_048
        assert "HEAD m" in carried
        assert "m TAIL" in carried
        limitations = attributes[_semconv.LIMITATIONS]
        assert isinstance(limitations, tuple)
        assert CONTENT_TRUNCATED in limitations

    def test_an_error_marks_the_span_by_type_and_status_never_message(self, emitting: Emitting) -> None:
        exporter = emitting(LOUD)
        client = hajer.wrap(
            FakeOpenAI(error=ProviderError("card 4111 1111 1111 1111 refused", status_code=529)), settings=LOUD
        )
        with pytest.raises(ProviderError):
            client.chat.completions.create(model="gpt-fake-1", messages=[])
        span = _only(exporter)
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "ProviderError"
        attributes = _attributes(span)
        assert attributes[_semconv.ERROR_TYPE] == "ProviderError"
        assert attributes[_semconv.HTTP_RESPONSE_STATUS_CODE] == 529
        assert "4111" not in json.dumps(span.to_json())
        assert span.events == ()

    def test_a_stream_settles_the_span_when_the_stream_ends(self, emitting: Emitting) -> None:
        exporter = emitting(LOUD)
        stream = FakeStream(
            [
                FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="hel"))]),
                FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="lo"))]),
                FakeChatChunk(
                    choices=[FakeChatStreamChoice(delta=FakeChatDelta(), finish_reason="stop")], usage=FakeOpenAIUsage()
                ),
            ]
        )
        client = hajer.wrap(FakeOpenAI(chat_script=[stream]), settings=LOUD)
        returned = as_stream(client.chat.completions.create(model="gpt-fake-1", messages=[], stream=True))
        assert _spans(exporter) == [], "not settled while the stream is still being read"
        list(returned)
        span = _only(exporter)
        attributes = _attributes(span)
        assert (
            attributes[_semconv.STREAM],
            attributes[_semconv.STREAM_COMPLETE],
            attributes[_semconv.STREAM_CHUNKS],
        ) == (True, True, 3)
        assert attributes[_semconv.RESPONSE_FINISH_REASONS] == ("stop",)
        assert _messages(span, _semconv.OUTPUT_MESSAGES) == [
            {"role": "assistant", "parts": [{"type": "text", "content": "hello"}], "finish_reason": "stop"}
        ]


def _provider_of(exporter: InMemorySpanExporter) -> TracerProvider:
    """The provider the `emitting` fixture built for this exporter: what `_telemetry` was configured with."""
    del exporter
    provider = _telemetry._CONFIGURATION.tracer_provider  # pyright: ignore[reportPrivateUsage]
    assert isinstance(provider, TracerProvider)
    return provider


class TestOtherDialects:
    def test_anthropic_system_and_tool_use(self, emitting: Emitting) -> None:
        exporter = emitting(LOUD)
        answer = FakeAnthropicMessage(
            content=[FakeTextBlock(text="checking"), FakeToolUseBlock(id="tu-1", name="lookup", input={"id": "o-1"})],
            stop_reason="tool_use",
        )
        client = hajer.wrap(FakeAnthropic(script=[answer]), settings=LOUD)
        client.messages.create(
            model="claude-fake-1",
            system="Be terse.",
            max_tokens=32,
            messages=[
                {"role": "user", "content": "where is o-1"},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu-0", "content": "shipped"}]},
            ],
        )
        span = _only(exporter)
        attributes = _attributes(span)
        assert span.name == "chat claude-fake-1"
        assert attributes[_semconv.PROVIDER_NAME] == "anthropic"
        assert attributes[_semconv.API] == "messages"
        assert attributes[_semconv.REQUEST_MAX_TOKENS] == 32
        assert _messages(span, _semconv.SYSTEM_INSTRUCTIONS) == [{"type": "text", "content": "Be terse."}]
        assert _messages(span, _semconv.INPUT_MESSAGES) == [
            {"role": "user", "parts": [{"type": "text", "content": "where is o-1"}]},
            {"role": "user", "parts": [{"type": "tool_call_response", "id": "tu-0", "result": "shipped"}]},
        ]
        assert _messages(span, _semconv.OUTPUT_MESSAGES) == [
            {
                "role": "assistant",
                "parts": [
                    {"type": "text", "content": "checking"},
                    {"type": "tool_call", "id": "tu-1", "name": "lookup", "arguments": {"id": "o-1"}},
                ],
                "finish_reason": "tool_use",
            }
        ]

    def test_the_responses_api(self, emitting: Emitting) -> None:
        exporter = emitting(LOUD)
        answer = FakeResponse(
            output=[FakeResponseItem(type="function_call", name="send", call_id="fc-1", arguments='{"to": "x"}')]
        )
        client = hajer.wrap(FakeOpenAI(responses_script=[answer]), settings=LOUD)
        client.responses.create(model="gpt-fake-1", instructions="Be terse.", input="send it")
        span = _only(exporter)
        attributes = _attributes(span)
        assert (span.name, attributes[_semconv.API]) == ("chat gpt-fake-1", "responses")
        assert _messages(span, _semconv.SYSTEM_INSTRUCTIONS) == [{"type": "text", "content": "Be terse."}]
        assert _messages(span, _semconv.INPUT_MESSAGES) == [
            {"role": "user", "parts": [{"type": "text", "content": "send it"}]}
        ]
        assert _messages(span, _semconv.OUTPUT_MESSAGES) == [
            {
                "role": "assistant",
                "parts": [{"type": "tool_call", "id": "fc-1", "name": "send", "arguments": {"to": "x"}}],
                "finish_reason": "completed",
            }
        ]

    def test_google_genai(self, emitting: Emitting) -> None:
        exporter = emitting(LOUD)
        client = hajer.wrap(FakeGenaiClient(), settings=LOUD)
        client.models.generate_content(
            model="gemini-2.5-pro", contents="hi", config={"system_instruction": "Be terse.", "temperature": 0.1}
        )
        span = _only(exporter)
        attributes = _attributes(span)
        assert span.name == "generate_content gemini-2.5-pro"
        assert attributes[_semconv.OPERATION_NAME] == "generate_content"
        assert attributes[_semconv.PROVIDER_NAME] == "gcp.gen_ai"
        assert attributes[_semconv.RESPONSE_ID] == "genai-1"
        assert (attributes[_semconv.USAGE_INPUT_TOKENS], attributes[_semconv.USAGE_OUTPUT_TOKENS]) == (7, 2)
        assert _messages(span, _semconv.SYSTEM_INSTRUCTIONS) == [{"type": "text", "content": "Be terse."}]
        assert _messages(span, _semconv.INPUT_MESSAGES) == [
            {"role": "user", "parts": [{"type": "text", "content": "hi"}]}
        ]
        assert _messages(span, _semconv.OUTPUT_MESSAGES) == [
            {"role": "assistant", "parts": [{"type": "text", "content": "ok"}], "finish_reason": "STOP"}
        ]

    def test_litellm_names_the_upstream(self, emitting: Emitting, monkeypatch: pytest.MonkeyPatch) -> None:
        exporter = emitting(QUIET)
        module = fake_litellm(answer=FakeChatCompletion(id="chatcmpl-lite", model="anthropic/claude-sonnet-5"))
        monkeypatch.setitem(sys.modules, "litellm", module)
        hajer.wrap(module, settings=QUIET)
        completion = getattr(module, "completion")  # noqa: B009 - a module stand-in
        completion(model="anthropic/claude-sonnet-5", messages=[{"role": "user", "content": "hi"}])
        span = _only(exporter)
        attributes = _attributes(span)
        assert span.name == "chat claude-sonnet-5"
        assert (attributes[_semconv.PROVIDER_NAME], attributes[_semconv.API]) == ("anthropic", "litellm.completion")
        assert attributes[_semconv.RESPONSE_ID] == "chatcmpl-lite"


class TestTheMappingAlone:
    """`model_span` on a record built by hand: the shapes no fake client in this suite produces."""

    def test_an_embedding_carries_counts_and_never_inputs(self) -> None:
        call = WrappedCall(
            provider="openai", api="embeddings", started_at="", started_ns=10, model="text-embedding-3-small"
        )
        call.usage = {"input_tokens": 4}
        call.embedding = (2, 3)
        call.content = {"output": {"vectors": 2, "dimensions": 3}}
        call.duration_ms = 5
        span = _model_spans.model_span(call, LOUD, hajer.build_policy())
        assert span.name == "embeddings text-embedding-3-small"
        assert span.attributes[_semconv.OPERATION_NAME] == "embeddings"
        assert span.attributes[_semconv.EMBEDDINGS_DIMENSION_COUNT] == 3
        assert span.attributes[_semconv.EMBEDDING_VECTORS] == 2
        assert (span.start_ns, span.end_ns) == (10, 5_000_010)
        assert not {_semconv.INPUT_MESSAGES, _semconv.OUTPUT_MESSAGES} & set(span.attributes)

    def test_an_http_exchange_is_an_http_client_span(self) -> None:
        call = WrappedCall(
            provider="http",
            api="GET",
            started_at="",
            started_ns=1,
            request_settings={"method": "GET", "url": "https://people.invalid/v1/people/{id}", "status": 200},
            provider_host="people.invalid:443",
        )
        span = _model_spans.model_span(call, LOUD, hajer.build_policy())
        assert span.name == "GET"
        assert span.attributes == {
            _semconv.HTTP_REQUEST_METHOD: "GET",
            _semconv.URL_TEMPLATE: "https://people.invalid/v1/people/{id}",
            _semconv.HTTP_RESPONSE_STATUS_CODE: 200,
            _semconv.SERVER_ADDRESS: "people.invalid",
            _semconv.SERVER_PORT: 443,
        }

    def test_the_server_address_is_split_from_the_host_port(self) -> None:
        call = WrappedCall(
            provider="openai", api="chat.completions", started_at="", started_ns=1, provider_host="[::1]:8000"
        )
        attributes = _model_spans.model_span(call, QUIET, hajer.build_policy()).attributes
        assert (attributes[_semconv.SERVER_ADDRESS], attributes[_semconv.SERVER_PORT]) == ("[::1]", 8000)

    def test_a_document_whose_shape_alone_does_not_fit_is_left_out_and_said(self) -> None:
        tight = hajer.HajerSettings(capture_content=True, wrapped_call_max_bytes=16)
        call = WrappedCall(provider="openai", api="chat.completions", started_at="", started_ns=1)
        call.content = {"messages": [{"role": "user", "content": "x" * 400}], "output": ["y" * 400]}
        span = _model_spans.model_span(call, tight, hajer.build_policy())
        assert _semconv.INPUT_MESSAGES not in span.attributes
        assert _semconv.OUTPUT_MESSAGES not in span.attributes
        assert span.attributes[_semconv.LIMITATIONS] == (CONTENT_TRUNCATED,)


class TestTheRules:
    def test_model_spans_off_records_the_call_and_emits_no_span(self, emitting: Emitting) -> None:
        off = hajer.HajerSettings(model_spans=False)
        exporter = emitting(off)
        client = hajer.wrap(FakeOpenAI(), settings=off)
        with hajer.workflow("wf"):
            client.chat.completions.create(model="gpt-fake-1", messages=[])
            assert len(hajer.wrapped_calls()) == 1
        assert [span.name for span in _spans(exporter)] == ["workflow wf"]

    def test_a_model_span_current_at_open_means_another_instrumentation_has_this_call(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        provider = _provider_of(exporter)
        client = hajer.wrap(FakeOpenAI(), settings=QUIET)
        with provider.get_tracer("other").start_as_current_span(
            "chat gpt", attributes={"gen_ai.operation.name": "chat"}
        ):
            client.chat.completions.create(model="gpt-fake-1", messages=[])
        assert [span.name for span in _spans(exporter)] == ["chat gpt"], "one call, one span: theirs"
        assert len(hajer.wrapped_calls()) == 1, "still recorded"

    def test_a_model_span_started_inside_the_call_means_the_same(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        provider = _provider_of(exporter)
        tracer = provider.get_tracer("other")
        client = FakeOpenAI()
        original = client.chat.completions.create

        def traced(**kwargs: object) -> object:
            # A client-level instrumentation: its span begins inside Hajer's patch and ends before the call settles.
            span = tracer.start_span("their chat", attributes={"gen_ai.operation.name": "chat"})
            try:
                return original(**kwargs)
            finally:
                span.end()

        client.chat.completions.create = traced
        hajer.wrap(client, settings=QUIET)
        client.chat.completions.create(model="gpt-fake-1", messages=[])
        assert [span.name for span in _spans(exporter)] == ["their chat"]

    def test_an_unrelated_span_around_the_call_is_the_parent_not_a_duplicate(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        provider = _provider_of(exporter)
        client = hajer.wrap(FakeOpenAI(), settings=QUIET)
        with provider.get_tracer("app").start_as_current_span("handle request"):
            client.chat.completions.create(model="gpt-fake-1", messages=[])
        spans = {span.name: span for span in _spans(exporter)}
        assert set(spans) == {"handle request", "chat gpt-fake-1"}
        parent = spans["chat gpt-fake-1"].parent
        assert parent is not None
        assert parent.span_id == _context(spans["handle request"]).span_id

    def test_an_eval_binding_stamps_the_model_span(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        client = hajer.wrap(FakeOpenAI(), settings=QUIET)
        with (
            _telemetry.eval_binding(
                run_id="run-1", test_case_id="case-1", workflow_id="wf", obligation_ids=("obl-1",), traceparent=None
            ),
            hajer.workflow("wf"),
            hajer.component("cmp"),
        ):
            client.chat.completions.create(model="gpt-fake-1", messages=[])
        chat = next(span for span in _spans(exporter) if span.name.startswith("chat"))
        attributes = _attributes(chat)
        assert attributes[_semconv.WORKFLOW_ID] == "wf"
        assert attributes[_semconv.COMPONENT_ID] == "cmp"
        assert attributes[_semconv.EVAL_RUN_ID] == "run-1"
        assert attributes[_semconv.EVAL_TEST_CASE_ID] == "case-1"
        assert attributes[_semconv.EVAL_OBLIGATION_IDS] == ("obl-1",)

    def test_without_the_otel_half_the_call_runs_and_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _telemetry.configure(None)
        monkeypatch.setitem(sys.modules, "hajer._telemetry_otel", None)
        try:
            client = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion(id="chatcmpl-plain")]), settings=QUIET)
            client.chat.completions.create(model="gpt-fake-1", messages=[])
            assert hajer.wrapped_calls()[0].response_id == "chatcmpl-plain"
        finally:
            _telemetry.configure(None)

    def test_the_mapping_is_on_the_public_surface(self) -> None:
        assert hajer.configure is _telemetry.configure
        assert "configure" in hajer.__all__

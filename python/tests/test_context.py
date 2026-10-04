"""`hajer.session` / `user` / `context`: declared once, carried by every span inside — the SDK's own and anybody's."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import cast

import pytest

import hajer
from hajer import _context, _semconv, _telemetry
from hajer._settings import CONTEXT_METADATA_KEYS_MAX, CONTEXT_TAGS_MAX, CONTEXT_VALUE_MAX_CHARS
from tests.conftest import Emitting, as_stream
from tests.fakes import FakeChatChunk, FakeChatDelta, FakeChatStreamChoice, FakeOpenAI, FakeStream

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

QUIET = hajer.HajerSettings(capture_content=False)


def _spans(exporter: InMemorySpanExporter) -> dict[str, dict[str, object]]:
    return {
        span.name: dict(span.attributes or {}) for span in cast(Sequence[ReadableSpan], exporter.get_finished_spans())
    }


def _provider() -> TracerProvider:
    provider = _telemetry._CONFIGURATION.tracer_provider  # pyright: ignore[reportPrivateUsage]
    assert isinstance(provider, TracerProvider)
    return provider


class TestTheStamp:
    def test_every_span_inside_a_session_carries_it(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        client = hajer.wrap(FakeOpenAI(), settings=QUIET)
        with hajer.session("conv-1"), hajer.user("cust-9"), hajer.workflow("wf"), hajer.component("cmp"):
            with hajer.tool("lookup"):
                pass
            client.chat.completions.create(model="gpt-fake-1", messages=[])
        spans = _spans(exporter)
        assert set(spans) == {"workflow wf", "component cmp", "execute_tool lookup", "chat gpt-fake-1"}
        for attributes in spans.values():
            assert (attributes[_semconv.SESSION_ID], attributes[_semconv.USER_ID]) == ("conv-1", "cust-9")

    def test_tags_and_metadata(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        with hajer.context(session="conv-1", tags=["beta", "eu"], metadata={"plan": "pro", "seats": 3, "trial": False}):
            with hajer.workflow("wf"):
                pass
        attributes = _spans(exporter)["workflow wf"]
        assert attributes[_semconv.TAGS] == ("beta", "eu")
        assert attributes["hajer.metadata.plan"] == "pro"
        assert attributes["hajer.metadata.seats"] == 3
        assert attributes["hajer.metadata.trial"] is False

    def test_a_third_party_span_inside_the_scope_is_stamped_too(self, emitting: Emitting) -> None:
        exporter = emitting(hajer.HajerSettings(environment="prod"))
        tracer = _provider().get_tracer("their-instrumentation")
        with hajer.session("conv-1"), hajer.workflow("wf"):
            with tracer.start_as_current_span("their span", attributes={"session.id": "theirs"}):
                pass
        theirs = _spans(exporter)["their span"]
        assert theirs[_semconv.SESSION_ID] == "theirs", "an attribute the instrumentation set is kept"
        assert theirs[_semconv.WORKFLOW_ID] == "wf"
        assert theirs[_semconv.DEPLOYMENT_ENVIRONMENT_NAME] == "prod"

    def test_outside_any_scope_nothing_is_stamped(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        with hajer.workflow("wf"):
            pass
        assert _spans(exporter)["workflow wf"] == {_semconv.WORKFLOW_ID: "wf"}

    def test_nesting_merges_and_the_inner_value_wins(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        with hajer.context(session="outer", tags=["a"], metadata={"k": "outer", "only": 1}):
            with hajer.context(session="inner", user="u", tags=["b"], metadata={"k": "inner"}), hajer.workflow("wf"):
                pass
            with hajer.workflow("after"):
                pass
        spans = _spans(exporter)
        inner = spans["workflow wf"]
        assert (inner[_semconv.SESSION_ID], inner[_semconv.USER_ID], inner[_semconv.TAGS]) == ("inner", "u", ("a", "b"))
        assert (inner["hajer.metadata.k"], inner["hajer.metadata.only"]) == ("inner", 1)
        after = spans["workflow after"]
        assert after[_semconv.SESSION_ID] == "outer"
        assert _semconv.USER_ID not in after
        assert after["hajer.metadata.k"] == "outer"

    def test_the_decorator_keeps_the_signature_sync_and_async(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)

        @hajer.session("conv-d")
        def handle(question: str, *, retries: int = 0) -> tuple[str, int]:
            with hajer.workflow("sync"):
                return question.upper(), retries

        @hajer.user("cust-d")
        async def ahandle() -> str:
            async with hajer.workflow("async"):
                await asyncio.sleep(0)
                return "ok"

        assert handle("hi", retries=2) == ("HI", 2)
        assert handle.__name__ == "handle"
        assert asyncio.run(ahandle()) == "ok"
        spans = _spans(exporter)
        assert spans["workflow sync"][_semconv.SESSION_ID] == "conv-d"
        assert spans["workflow async"][_semconv.USER_ID] == "cust-d"
        assert _context.current() == _context.TraceContext(), "nothing leaks past the decorated call"

    async def test_concurrent_tasks_keep_their_own_session(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)

        async def turn(conversation: str) -> None:
            async with hajer.session(conversation), hajer.workflow(f"wf-{conversation}"):
                await asyncio.sleep(0)

        await asyncio.gather(turn("one"), turn("two"))
        spans = _spans(exporter)
        assert spans["workflow wf-one"][_semconv.SESSION_ID] == "one"
        assert spans["workflow wf-two"][_semconv.SESSION_ID] == "two"

    def test_a_stream_keeps_the_session_it_was_opened_under(self, emitting: Emitting) -> None:
        exporter = emitting(QUIET)
        stream = FakeStream(
            [FakeChatChunk(choices=[FakeChatStreamChoice(delta=FakeChatDelta(content="hi"), finish_reason="stop")])]
        )
        client = hajer.wrap(FakeOpenAI(chat_script=[stream]), settings=QUIET)
        with hajer.session("conv-s"):
            returned = as_stream(client.chat.completions.create(model="gpt-fake-1", messages=[], stream=True))
        list(returned)  # consumed outside the scope, as a response is
        assert _spans(exporter)["chat gpt-fake-1"][_semconv.SESSION_ID] == "conv-s"


class TestTheBounds:
    def test_values_are_clipped_and_extra_keys_dropped_never_refused(self) -> None:
        with hajer.context(
            session="s" * (CONTEXT_VALUE_MAX_CHARS + 10),
            tags=[f"t{index}" for index in range(CONTEXT_TAGS_MAX + 5)],
            metadata={f"k{index}": index for index in range(CONTEXT_METADATA_KEYS_MAX + 5)},
        ):
            current = _context.current()
        assert current.session is not None
        assert len(current.session) == CONTEXT_VALUE_MAX_CHARS
        assert len(current.tags) == CONTEXT_TAGS_MAX
        assert len(current.metadata) == CONTEXT_METADATA_KEYS_MAX

    def test_a_document_as_a_metadata_value_is_refused_where_it_is_written(self) -> None:
        with pytest.raises(TypeError, match="plan"):
            hajer.context(metadata={"plan": {"nested": True}})  # pyright: ignore[reportArgumentType] - the refusal under test

    def test_the_public_names_are_the_module_ones(self) -> None:
        assert (hajer.session, hajer.user, hajer.context) == (_context.session, _context.user, _context.context)
        assert {"session", "user", "context", "TraceContextScope"} <= set(hajer.__all__)

"""The declared spans carry the ids an obligation is written against, and never cost the code they wrap."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from typing import cast

import pytest

import hajer
from hajer import _telemetry
from hajer._wrap import TRUNCATION_MARK
from tests.fakes import FakeOpenAI

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, StatusCode

Emitting = Callable[[hajer.HajerSettings], InMemorySpanExporter]

_SETTINGS = hajer.HajerSettings(environment="production")
#: A W3C traceparent an eval engine would hand a provider: version, trace id, parent span id, sampled.
_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
_PARENT_ID = "b7ad6b7169203331"
_TRACEPARENT = f"00-{_TRACE_ID}-{_PARENT_ID}-01"


@pytest.fixture
def emitting() -> Iterator[Emitting]:
    """An isolated SDK provider with an in-memory exporter, configured as the emitter's; reset afterwards."""
    providers: list[TracerProvider] = []

    def configure(settings: hajer.HajerSettings) -> InMemorySpanExporter:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        providers.append(provider)
        _telemetry.configure(settings, tracer_provider=provider)
        return exporter

    yield configure
    _telemetry.configure(None)
    for provider in providers:
        provider.shutdown()


def _spans(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    """The finished spans by name; every name in these tests is unique."""
    spans = cast(Sequence[ReadableSpan], exporter.get_finished_spans())
    by_name = {span.name: span for span in spans}
    assert len(by_name) == len(spans), "span names repeat"
    return by_name


def _attributes(span: ReadableSpan) -> dict[str, object]:
    return dict(span.attributes or {})


def _context(span: ReadableSpan) -> SpanContext:
    assert span.context is not None
    return span.context


def _parent(span: ReadableSpan) -> SpanContext:
    assert span.parent is not None, f"{span.name} is a root span"
    return span.parent


def test_nesting_copies_the_enclosing_ids_down(emitting: Emitting) -> None:
    exporter = emitting(_SETTINGS)
    with hajer.workflow("answer"), hajer.component("draft"), hajer.tool("lookup", name="order_lookup"):
        pass
    spans = _spans(exporter)
    assert set(spans) == {"workflow answer", "component draft", "execute_tool order_lookup"}
    environment = {"deployment.environment": "production", "deployment.environment.name": "production"}
    assert _attributes(spans["workflow answer"]) == {"hajer.workflow.id": "answer", **environment}
    assert _attributes(spans["component draft"]) == {
        "hajer.workflow.id": "answer",
        "hajer.component.id": "draft",
        **environment,
    }
    assert _attributes(spans["execute_tool order_lookup"]) == {
        "hajer.workflow.id": "answer",
        "hajer.component.id": "draft",
        "hajer.tool.id": "lookup",
        "gen_ai.tool.name": "order_lookup",
        "gen_ai.operation.name": "execute_tool",
        **environment,
    }
    tool, component, workflow = (
        spans[name] for name in ("execute_tool order_lookup", "component draft", "workflow answer")
    )
    assert _parent(tool).span_id == _context(component).span_id
    assert _parent(component).span_id == _context(workflow).span_id
    assert workflow.parent is None


def test_a_tool_outside_any_workflow_carries_only_its_own_ids(emitting: Emitting) -> None:
    exporter = emitting(hajer.HajerSettings())
    with hajer.tool("lookup"):
        pass
    (span,) = _spans(exporter).values()
    assert span.name == "execute_tool lookup"
    assert _attributes(span) == {
        "hajer.tool.id": "lookup",
        "gen_ai.tool.name": "lookup",
        "gen_ai.operation.name": "execute_tool",
    }


def test_the_decorator_keeps_the_signature_and_the_result(emitting: Emitting) -> None:
    exporter = emitting(_SETTINGS)

    @hajer.workflow("answer")
    def answer(question: str, *, retries: int = 0) -> tuple[str, int]:
        with hajer.tool("lookup"):
            return question.upper(), retries

    assert answer("hi", retries=2) == ("HI", 2)
    assert answer.__name__ == "answer"
    spans = _spans(exporter)
    assert _attributes(spans["execute_tool lookup"])["hajer.workflow.id"] == "answer"


async def test_the_decorator_awaits_an_async_function_inside_the_span(emitting: Emitting) -> None:
    exporter = emitting(_SETTINGS)

    @hajer.component("draft")
    async def draft(text: str) -> str:
        await asyncio.sleep(0)
        async with hajer.tool("lookup"):
            await asyncio.sleep(0)
        return text * 2

    @hajer.workflow("answer")
    async def answer() -> str:
        # A thread that copied the context the way `asyncio.to_thread` does sees the enclosing ids.
        return await asyncio.to_thread(lambda: asyncio.run(draft("ab")))

    assert await answer() == "abab"
    spans = _spans(exporter)
    assert _attributes(spans["component draft"])["hajer.workflow.id"] == "answer"
    tool = _attributes(spans["execute_tool lookup"])
    assert (tool["hajer.workflow.id"], tool["hajer.component.id"]) == ("answer", "draft")


async def test_concurrent_tasks_keep_their_own_ids(emitting: Emitting) -> None:
    """Two tasks entering spans of the same object interleave at every await and still close their own."""
    exporter = emitting(hajer.HajerSettings())
    component = hajer.component("shared")

    async def run(workflow_id: str) -> None:
        async with hajer.workflow(workflow_id):
            async with component:
                await asyncio.sleep(0)
                with hajer.tool(f"tool-{workflow_id}"):
                    await asyncio.sleep(0)

    await asyncio.gather(run("one"), run("two"))
    spans = cast(Sequence[ReadableSpan], exporter.get_finished_spans())
    assert len(spans) == 6
    tools = {span.name: _attributes(span) for span in spans if span.name.startswith("execute_tool")}
    assert tools["execute_tool tool-one"]["hajer.workflow.id"] == "one"
    assert tools["execute_tool tool-two"]["hajer.workflow.id"] == "two"
    components = [span for span in spans if span.name == "component shared"]
    assert sorted(str(_attributes(span)["hajer.workflow.id"]) for span in components) == ["one", "two"]


def test_a_workflow_span_enters_a_scope_with_its_id(emitting: Emitting) -> None:
    """The model calls `wrap` records inside the span carry the span's id as their workflow hint."""
    emitting(_SETTINGS)
    client = hajer.wrap(FakeOpenAI())

    @hajer.workflow("support-answer")
    def answer() -> tuple[hajer.WrappedCall, ...]:
        client.chat.completions.create(model="fake", messages=[])
        return hajer.wrapped_calls()

    (call,) = answer()
    assert call.workflow_hint == "support-answer"
    assert hajer.wrapped_calls() == (), "the scope closed with the span"


def test_the_environment_is_carried_only_when_set(emitting: Emitting) -> None:
    exporter = emitting(hajer.HajerSettings())
    with hajer.workflow("answer"):
        pass
    (span,) = _spans(exporter).values()
    assert _attributes(span) == {"hajer.workflow.id": "answer"}


def test_eval_attributes_appear_only_inside_a_binding(emitting: Emitting) -> None:
    exporter = emitting(hajer.HajerSettings(eval_run_id="run-7"))
    with hajer.workflow("outside"):
        pass
    with (
        _telemetry.eval_binding(
            run_id=None,
            test_case_id="case-1",
            workflow_id="answer",
            obligation_ids=("refund-policy", "tone"),
            traceparent=None,
        ),
        hajer.workflow("inside"),
    ):
        pass
    spans = _spans(exporter)
    assert _attributes(spans["workflow outside"]) == {"hajer.workflow.id": "outside", "hajer.eval.run.id": "run-7"}
    assert _attributes(spans["workflow inside"]) == {
        "hajer.workflow.id": "inside",
        "hajer.eval.run.id": "run-7",
        "hajer.eval.test_case.id": "case-1",
        "hajer.eval.workflow.id": "answer",
        "hajer.eval.obligation.ids": ("refund-policy", "tone"),
    }


def test_a_bound_traceparent_roots_the_first_span_and_nesting_stays_inside(emitting: Emitting) -> None:
    exporter = emitting(hajer.HajerSettings())
    with (
        _telemetry.eval_binding(
            run_id="run-1", test_case_id=None, workflow_id=None, obligation_ids=(), traceparent=_TRACEPARENT
        ),
        hajer.workflow("answer"),
        hajer.tool("lookup"),
    ):
        pass
    spans = _spans(exporter)
    workflow, tool = spans["workflow answer"], spans["execute_tool lookup"]
    assert format(_context(workflow).trace_id, "032x") == _TRACE_ID
    assert format(_parent(workflow).span_id, "016x") == _PARENT_ID
    assert _parent(tool).span_id == _context(workflow).span_id
    assert _attributes(workflow)["hajer.eval.run.id"] == "run-1"


def test_tool_arguments_are_redacted_and_clipped(emitting: Emitting) -> None:
    exporter = emitting(hajer.HajerSettings(wrapped_call_max_bytes=2_048))
    arguments: hajer.JsonObject = {"email": "someone@example.com", "note": "x" * 10_000, "count": 3}
    with hajer.tool("lookup", arguments=arguments):
        pass
    (span,) = _spans(exporter).values()
    carried = _attributes(span)["gen_ai.tool.call.arguments"]
    assert isinstance(carried, str)
    assert len(carried.encode("utf-8")) <= 2_048
    decoded = json.loads(carried)
    assert decoded["email"] == "[redacted:EMAIL]"
    assert decoded["count"] == 3
    assert TRUNCATION_MARK.split("{")[0] in decoded["note"]
    assert arguments["note"] == "x" * 10_000, "the caller's document is untouched"


def test_tool_arguments_are_absent_without_content_capture(emitting: Emitting) -> None:
    exporter = emitting(hajer.HajerSettings(capture_content=False))
    with hajer.tool("lookup", arguments={"email": "someone@example.com"}):
        pass
    (span,) = _spans(exporter).values()
    assert "gen_ai.tool.call.arguments" not in _attributes(span)


def test_an_exception_propagates_and_marks_the_span_by_type_only(emitting: Emitting) -> None:
    exporter = emitting(hajer.HajerSettings())

    @hajer.tool("lookup")
    def lookup() -> None:
        raise ValueError("card 4111 1111 1111 1111")

    with pytest.raises(ValueError, match="card"):
        lookup()
    (span,) = _spans(exporter).values()
    assert span.status.status_code is StatusCode.ERROR
    assert _attributes(span)["error.type"] == "builtins.ValueError"
    assert span.events == (), "no recorded exception: no message, no stack trace"
    assert "4111" not in json.dumps(span.to_json())


def test_without_the_otel_half_the_code_runs_and_one_warning_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _telemetry.configure(None)
    monkeypatch.setitem(sys.modules, "hajer._telemetry_otel", None)
    client = hajer.wrap(FakeOpenAI())
    with caplog.at_level(logging.WARNING, logger="hajer"):

        @hajer.workflow("answer")
        def answer() -> str:
            with hajer.component("draft"), hajer.tool("lookup", arguments={"a": 1}):
                client.chat.completions.create(model="fake", messages=[])
            return hajer.wrapped_calls()[0].workflow_hint or ""

        assert answer() == "answer"
        assert answer() == "answer"
        assert _telemetry.flush() is False
    warnings = [record for record in caplog.records if "hajer[otel]" in record.getMessage()]
    assert len(warnings) == 1
    _telemetry.configure(None)


def test_flush_waits_on_the_provider_in_use(emitting: Emitting) -> None:
    emitting(hajer.HajerSettings())
    with hajer.workflow("answer"):
        pass
    assert _telemetry.flush() is True
    assert _telemetry.flush(timeout_ms=10) is True


class _BrokenProvider(TracerProvider):
    """A provider whose exporter is broken: starting a span and flushing both raise."""

    def get_tracer(self, *args: object, **kwargs: object) -> object:  # pyright: ignore[reportIncompatibleMethodOverride]
        del args, kwargs
        raise RuntimeError("no tracer today")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        raise RuntimeError("no flush today")


def test_a_broken_provider_costs_the_spans_and_never_the_code() -> None:
    _telemetry.configure(hajer.HajerSettings(), tracer_provider=_BrokenProvider())
    try:

        @hajer.workflow("answer")
        def answer() -> int:
            with hajer.tool("lookup"):
                return 42

        assert answer() == 42
        assert _telemetry.flush() is False
    finally:
        _telemetry.configure(None)


class _FailingStart(TracerProvider):
    """A provider whose spans start but whose `force_flush` raises — the exporter broke after the span left."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        raise RuntimeError("exporter down")


def test_a_flush_that_raises_is_reported_false() -> None:
    _telemetry.configure(hajer.HajerSettings(), tracer_provider=_FailingStart())
    try:
        with hajer.workflow("answer"):
            pass
        assert _telemetry.flush() is False
    finally:
        _telemetry.configure(None)


def test_the_public_names_are_the_module_ones() -> None:
    assert (hajer.workflow, hajer.component, hajer.tool) == (_telemetry.workflow, _telemetry.component, _telemetry.tool)
    assert {"workflow", "component", "tool"} <= set(hajer.__all__)

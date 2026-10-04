"""`hajer.evals.provider` binds a Promptfoo row to the spans its provider call emits, and never fails the row."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import cast

import pytest

import hajer
import hajer.evals
from hajer import _telemetry

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext

_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_PARENT_ID = "00f067aa0ba902b7"
#: What the engine hands a Python provider for one row: the trace it opened, the row's id, and the suite author's
#: reserved metadata (`test.metadata.hajer`).
_CONTEXT: hajer.JsonObject = {
    "traceparent": f"00-{_TRACE_ID}-{_PARENT_ID}-01",
    "testCaseId": "case-42",
    "evaluationId": "eval-9",
    "vars": {"question": "where is my order?"},
    "test": {"metadata": {"hajer": {"workflowId": "answer-support-question", "obligationIds": ["refund-policy"]}}},
}


class _CountingProvider(TracerProvider):
    """Counts the flushes, so a test can say the binding flushed before the provider answered."""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flushes += 1
        return super().force_flush(timeout_millis)


@pytest.fixture
def receiver() -> Iterator[tuple[_CountingProvider, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = _CountingProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _telemetry.configure(hajer.HajerSettings(environment="eval", eval_run_id="run-1"), tracer_provider=provider)
    yield provider, exporter
    _telemetry.configure(None)
    provider.shutdown()


def _only_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    (span,) = cast(Sequence[ReadableSpan], exporter.get_finished_spans())
    return span


def _bound(span: ReadableSpan) -> dict[str, object]:
    attributes = dict(span.attributes or {})
    return {key: value for key, value in attributes.items() if key.startswith("hajer.eval.")}


def _trace_id(span: ReadableSpan) -> str:
    assert span.context is not None
    return format(span.context.trace_id, "032x")


def _parent(span: ReadableSpan) -> SpanContext:
    assert span.parent is not None, f"{span.name} is a root span"
    return span.parent


_EXPECTED_BINDING = {
    "hajer.eval.run.id": "run-1",
    "hajer.eval.test_case.id": "case-42",
    "hajer.eval.workflow.id": "answer-support-question",
    "hajer.eval.obligation.ids": ("refund-policy",),
}


def test_a_sync_provider_is_bound_and_flushed(receiver: tuple[_CountingProvider, InMemorySpanExporter]) -> None:
    provider, exporter = receiver

    @hajer.evals.provider
    def call_api(prompt: str, options: hajer.JsonObject, context: hajer.JsonObject) -> hajer.JsonObject:
        with hajer.workflow("answer-support-question"):
            return {"output": f"{prompt}|{len(options)}|{context['testCaseId']}"}

    assert call_api("hello", {}, _CONTEXT) == {"output": "hello|0|case-42"}
    assert provider.flushes == 1
    span = _only_span(exporter)
    assert _bound(span) == _EXPECTED_BINDING
    assert _trace_id(span) == _TRACE_ID
    assert format(_parent(span).span_id, "016x") == _PARENT_ID
    assert dict(span.attributes or {})["deployment.environment"] == "eval"


async def test_an_async_provider_is_bound_and_flushed(receiver: tuple[_CountingProvider, InMemorySpanExporter]) -> None:
    provider, exporter = receiver

    @hajer.evals.provider
    async def call_api(prompt: str, options: hajer.JsonObject, context: hajer.JsonObject) -> str:
        del options, context
        async with hajer.tool("lookup"):
            return prompt.upper()

    assert await call_api("hello", {}, context=_CONTEXT) == "HELLO"
    assert provider.flushes == 1
    span = _only_span(exporter)
    assert _bound(span) == _EXPECTED_BINDING
    assert _trace_id(span) == _TRACE_ID


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({}, {}),
        ({"traceparent": 12, "testCaseId": None, "test": "not a mapping"}, {}),
        ({"test": {"metadata": {"hajer": {"workflowId": ["not", "a", "string"], "obligationIds": "not-a-list"}}}}, {}),
        (
            {"test": {"metadata": {"hajer": {"obligationIds": [1, "", "tone"]}}}},
            {"hajer.eval.obligation.ids": ("tone",)},
        ),
    ],
)
def test_a_context_missing_or_mistyped_still_runs_the_call(
    receiver: tuple[_CountingProvider, InMemorySpanExporter], context: hajer.JsonObject, expected: dict[str, object]
) -> None:
    provider, exporter = receiver

    @hajer.evals.provider
    def call_api(prompt: str, options: hajer.JsonObject, context: hajer.JsonObject) -> str:
        del options, context
        with hajer.workflow("answer"):
            return prompt

    assert call_api("hello", {}, context) == "hello"
    assert provider.flushes == 1
    bound = _bound(_only_span(exporter))
    assert bound.pop("hajer.eval.run.id") == "run-1", "the run id comes from the settings, not the row"
    assert bound == expected


def test_a_provider_called_without_a_context_runs_unbound(
    receiver: tuple[_CountingProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = receiver

    @hajer.evals.provider
    def call_api(prompt: str) -> str:
        with hajer.workflow("answer"):
            return prompt

    assert call_api("hello") == "hello"
    assert provider.flushes == 1
    assert _bound(_only_span(exporter)) == {"hajer.eval.run.id": "run-1"}


def test_bind_is_the_block_form(receiver: tuple[_CountingProvider, InMemorySpanExporter]) -> None:
    provider, exporter = receiver
    with hajer.evals.bind(_CONTEXT), hajer.component("draft"):
        pass
    assert provider.flushes == 1
    assert _bound(_only_span(exporter)) == _EXPECTED_BINDING
    assert set(hajer.evals.__all__) == {"bind", "provider"}

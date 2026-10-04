"""The OpenTelemetry half of `hajer._telemetry`: where the spans go, decided once, and never the process's provider.

Three places a span may go, tried in this order, and the order is a rule about ownership:

1. the provider `configure(tracer_provider=…)` named — the application (or a test) said so;
2. the global provider, when it is a real SDK `TracerProvider` — an application with its own OpenTelemetry setup keeps
   its exporters, its sampler and its resource, and the Hajer spans ride beside its own (production parity: the trace
   an eval grades is the trace production emits);
3. an isolated `TracerProvider` exporting over OTLP/HTTP to `HAJER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_ENDPOINT`),
   **never installed as the global provider** — the same rule `_otel.py` keeps: a library that called
   `set_tracer_provider` would have decided, for a process it does not own, where every other library's spans go.

With none of the three the tracer is OpenTelemetry's no-op one, and `hajer._telemetry` still runs the code.
"""

from __future__ import annotations

from contextvars import Token
from dataclasses import dataclass
from typing import Final

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from hajer._paths import VERSION
from hajer._settings import HajerSettings
from hajer._telemetry import Attributes

#: OTLP/HTTP's traces path, appended to the endpoint the settings name (an endpoint, not a traces URL — the same
#: convention `OTEL_EXPORTER_OTLP_ENDPOINT` follows, so one variable serves every signal).
_TRACES_PATH: Final[str] = "/v1/traces"
#: The instrumentation scope the spans are emitted under: the SDK's own name and version.
_SCOPE: Final[str] = "hajer"
#: The exporter takes its timeout in seconds; the SDK speaks milliseconds everywhere, so this is the one conversion.
_MS_PER_S: Final[int] = 1_000
#: What the span records about an exception the wrapped code raised: its class, qualified. Never the message — that is
#: content — and never the stack trace, which is a list of absolute paths; both have their own disclosure rules in
#: this SDK and a span is not where either is waived.
_ERROR_TYPE: Final[str] = "error.type"
#: The W3C header the eval engine hands a provider. A traceparent is W3C by definition, so it is read with the W3C
#: propagator rather than whichever one the application configured globally.
_W3C: Final = TraceContextTextMapPropagator()


@dataclass(slots=True)
class _OpenSpan:
    """One started span and the context token that made it current, so `end` can undo exactly what `start` did."""

    span: trace.Span
    token: Token[Context]

    def end(self, error: BaseException | None) -> None:
        try:
            if error is not None:
                self.span.set_attribute(_ERROR_TYPE, f"{type(error).__module__}.{type(error).__qualname__}")
                self.span.set_status(Status(StatusCode.ERROR, type(error).__qualname__))
            self.span.end()
        finally:
            context.detach(self.token)


class OtelBackend:
    """A tracer, and the provider behind it when there is one to flush."""

    def __init__(self, tracer: trace.Tracer, provider: TracerProvider | None) -> None:
        self.tracer = tracer
        self.provider = provider

    def start(self, name: str, attributes: Attributes, *, traceparent: str | None) -> _OpenSpan:
        """Start a span under the active one — or, when none is active, under `traceparent` when one was bound.

        The active span wins because nesting is the application's own structure; the header only roots a span that
        would otherwise start a trace of its own, which is what the eval engine needs: the first span of the
        provider call joins the engine's trace, and everything inside stays inside.
        """
        parent: Context | None = None
        if traceparent is not None and not trace.get_current_span().get_span_context().is_valid:
            parent = _W3C.extract({"traceparent": traceparent})
        span = self.tracer.start_span(name, context=parent, attributes=attributes)
        return _OpenSpan(span=span, token=context.attach(trace.set_span_in_context(span)))

    def flush(self, timeout_ms: int) -> bool:
        """`force_flush` on the provider in use; False when there is none, so a caller never believes a no-op exported."""
        if self.provider is None:
            return False
        return self.provider.force_flush(timeout_ms)


def build(settings: HajerSettings, tracer_provider: object | None) -> OtelBackend:
    """Choose the provider by the three-step rule in the module docstring, and hand back the tracer on it."""
    provider: TracerProvider | None = None
    if isinstance(tracer_provider, TracerProvider):
        provider = tracer_provider
    elif tracer_provider is None:
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            provider = current
        elif settings.otlp_endpoint is not None:
            provider = _exporting_to(settings.otlp_endpoint, timeout_ms=settings.trace_flush_timeout_ms)
    if provider is None:
        return OtelBackend(trace.NoOpTracer(), None)
    return OtelBackend(provider.get_tracer(_SCOPE, VERSION), provider)


def _exporting_to(endpoint: str, *, timeout_ms: int) -> TracerProvider:
    """An isolated provider batching to the endpoint's traces path, each export bounded by the flush budget.

    `shutdown_on_exit=False` because the atexit flush `hajer._telemetry` registers is the one exit hook, and a provider
    that also shut itself down would race it. The exporter's `timeout` is `HAJER_TRACE_FLUSH_TIMEOUT_MS`, because the
    SDK's `BatchSpanProcessor.force_flush` does not honour its own timeout and blocks on the export: with the exporter's
    default (ten seconds, with retries inside it) an unreachable endpoint would hold the process at exit for all of
    them, and a flush that outlives the budget it was given is not a flush anybody asked for.
    """
    provider = TracerProvider(shutdown_on_exit=False)
    exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + _TRACES_PATH, timeout=timeout_ms / _MS_PER_S)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider

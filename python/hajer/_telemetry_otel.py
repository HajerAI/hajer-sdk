"""The OpenTelemetry half of `hajer._telemetry`: which provider the spans go on, and the exporter that takes them out.

**The provider** is chosen by ownership, and the order is a rule about who decides where a process's spans go:

1. the provider `configure(tracer_provider=…)` named — the application (or a test) said so;
2. the global provider, when it is a real SDK `TracerProvider` — an application with its own OpenTelemetry setup
   keeps its exporters, its sampler and its resource, and the Hajer spans ride beside its own (production parity:
   the trace an eval grades is the trace production emits);
3. an isolated `TracerProvider` the SDK builds, **never installed as the global provider** — a library that called
   `set_tracer_provider` would have decided, for a process it does not own, where every other library's spans go.

**The exporter** is added to whichever provider that is, once per provider, when `hajer._telemetry.export_target`
names somewhere to export to: the platform's receiver for the team when there is a key, or a collector the process
named with `HAJER_OTLP_ENDPOINT`. An application's own exporters stay in place beside it — the same thing a tracing
vendor's SDK does when it adds its processor to the global provider — so pointing a process at Hajer never silences
the collector it already had. With no target and no provider the tracer is OpenTelemetry's no-op one, and
`hajer._telemetry` still runs the code.

**A model span** is emitted when the recorded call settles, backdated to when it opened: OpenTelemetry exports only
ended spans, so nothing is lost by starting late, and a stream settles in whatever task consumed it, where the
context the span belongs under is gone — which is why `opened` captures that context into a handle. The span is never
made current: it is a leaf, and the application's own structure is the structure. When another instrumentation
already traces the same call (a `gen_ai` span current at open, or one that starts inside the call —
`HajerContextProcessor` sees it), the SDK's own span is not emitted: one call, one span.

Nothing here blocks a call: the exporter batches on `BatchSpanProcessor`'s own thread, bounded by the
`HAJER_TRACE_*` settings, and an unreachable receiver costs at most one export timeout at exit.
"""

from __future__ import annotations

import weakref
from collections.abc import Mapping
from contextvars import Token
from dataclasses import dataclass
from typing import Final

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from hajer import _semconv
from hajer._model_spans import Attributes, ModelSpan
from hajer._paths import VERSION
from hajer._settings import HajerSettings
from hajer._telemetry import ExportTarget, export_target
from hajer._wrap import OPEN_CALL, WrappedCall

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
#: The resource attributes the SDK's own provider carries, by the semantic conventions' names.
_SERVICE_NAME: Final[str] = "service.name"
_ENVIRONMENT_NAME: Final[str] = "deployment.environment.name"
_SDK_VERSION: Final[str] = "hajer.sdk.version"


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


@dataclass(slots=True)
class _Opened:
    """What `opened` kept of the moment a call began: the context its span belongs under, and the stamp it carries.

    `foreign` is set when another instrumentation's `gen_ai` span covers this call — already current when the call
    opened, or started inside it — and means this module emits no span of its own for it.
    """

    parent: Context
    stamp: Attributes
    foreign: bool = False


@dataclass(slots=True)
class _Attached:
    """The exporter this module added to one provider, and the target it exports to."""

    processor: BatchSpanProcessor
    target: ExportTarget


#: One exporter per provider, however many times `configure()` names the same one. Weak keys, so a provider a
#: test built and dropped is not kept alive by the record of what was attached to it.
_ATTACHED: Final[weakref.WeakKeyDictionary[TracerProvider, _Attached]] = weakref.WeakKeyDictionary()
#: One `HajerContextProcessor` per provider this module ever emits through, for the same reason.
_PROCESSED: Final[weakref.WeakKeyDictionary[TracerProvider, HajerContextProcessor]] = weakref.WeakKeyDictionary()


def _says_model_call(attributes: Mapping[str, object] | None) -> bool:
    """A span that says it is a model call, in the conventions' own words."""
    return attributes is not None and _semconv.OPERATION_NAME in attributes


def _is_model_span(span: trace.Span) -> bool:
    """A recording SDK span that says it is a model call: another instrumentation's reading of one."""
    return span.is_recording() and isinstance(span, ReadableSpan) and _says_model_call(span.attributes)


class HajerContextProcessor(SpanProcessor):
    """Watches every span the provider starts, for the one the SDK must not duplicate.

    A client-level instrumentation (OpenTelemetry's own `openai` instrumentor, OpenLLMetry) patches the provider
    class, which runs *inside* Hajer's instance-level patch: its model span begins after `opened` ran and before the
    call settles, so it is only visible here. The open call in this context is marked foreign and emits nothing.
    Every method is guarded: a processor that raised would raise inside the application's own span.
    """

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        del parent_context
        self._claim(span.attributes)

    def on_end(self, span: ReadableSpan) -> None:
        self._claim(span.attributes)

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True

    def _claim(self, attributes: Mapping[str, object] | None) -> None:
        try:
            if not _says_model_call(attributes):
                return
            call: WrappedCall | None = OPEN_CALL.get()
            if call is not None and isinstance(call.observer_handle, _Opened):
                call.observer_handle.foreign = True
        except Exception:  # noqa: BLE001, S110 - never into the application's own span
            pass


class OtelBackend:
    """A tracer, the provider behind it when there is one to flush, and the SDK's own exporter when it added one."""

    def __init__(self, tracer: trace.Tracer, provider: TracerProvider | None, *, owned: bool) -> None:
        self.tracer = tracer
        self.provider = provider
        #: True when the SDK built the provider: shutting it down is the SDK's to do, and nobody else's.
        self.owned = owned

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

    def opened(self, call: WrappedCall, stamp: Attributes, *, traceparent: str | None) -> _Opened:
        """Keep the context a call opened in — or the bound `traceparent` when no span is active — and its stamp.

        A `gen_ai` span already current here is another instrumentation's reading of this very call: the handle says
        so, and `settled` emits nothing for it.
        """
        del call
        current = trace.get_current_span()
        parent = context.get_current()
        if traceparent is not None and not current.get_span_context().is_valid:
            parent = _W3C.extract({"traceparent": traceparent})
        return _Opened(parent=parent, stamp=dict(stamp), foreign=_is_model_span(current))

    def settled(self, handle: object, span: ModelSpan) -> None:
        """Emit the model span: started where the call opened, ended when it settled, never made current."""
        if not isinstance(handle, _Opened) or handle.foreign:
            return
        emitted = self.tracer.start_span(
            span.name,
            context=handle.parent,
            kind=SpanKind.CLIENT,
            attributes={**handle.stamp, **span.attributes},
            start_time=span.start_ns,
        )
        if span.error_type is not None:
            emitted.set_status(Status(StatusCode.ERROR, span.error_type))
        emitted.end(end_time=span.end_ns)

    def flush(self, timeout_ms: int) -> bool:
        """`force_flush` on the provider in use; False when there is none, so a caller never believes a no-op exported."""
        if self.provider is None:
            return False
        return self.provider.force_flush(timeout_ms)

    def shutdown(self) -> None:
        """Stop the SDK's own exporter on this provider, and the provider itself when the SDK built it.

        An application's provider keeps running: only the processor this module added is stopped, so its thread
        ends and nothing more is sent to Hajer from a configuration that was replaced.
        """
        if self.provider is None:
            return
        attached = _ATTACHED.pop(self.provider, None)
        if attached is not None:
            attached.processor.shutdown()
        if self.owned:
            self.provider.shutdown()


def build(settings: HajerSettings, tracer_provider: object | None) -> OtelBackend:
    """Choose the provider by the three-step rule in the module docstring, add the exporter, hand back the tracer."""
    provider: TracerProvider | None = None
    owned = False
    if isinstance(tracer_provider, TracerProvider):
        provider = tracer_provider
    elif tracer_provider is None:
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            provider = current
    target = export_target(settings)
    if provider is None and target is not None:
        provider = TracerProvider(resource=_resource(settings), shutdown_on_exit=False)
        owned = True
    if provider is None:
        return OtelBackend(trace.NoOpTracer(), None, owned=False)
    if provider not in _PROCESSED:
        watcher = HajerContextProcessor()
        provider.add_span_processor(watcher)
        _PROCESSED[provider] = watcher
    if target is not None:
        _attach(provider, target, settings)
    return OtelBackend(provider.get_tracer(_SCOPE, VERSION), provider, owned=owned)


def _attach(provider: TracerProvider, target: ExportTarget, settings: HajerSettings) -> None:
    """Add the exporter for `target` to `provider` once; a different target replaces the exporter, the same one keeps it.

    A provider has no way to remove a processor, so a replaced exporter is shut down instead: it stops exporting and
    its thread ends, and the new one takes over for every span from now on.
    """
    standing = _ATTACHED.get(provider)
    if standing is not None:
        if standing.target == target:
            return
        standing.processor.shutdown()
    exporter = OTLPSpanExporter(
        endpoint=target.url, headers=target.headers, timeout=settings.trace_export_timeout_ms / _MS_PER_S
    )
    processor = BatchSpanProcessor(
        exporter,
        max_queue_size=settings.trace_queue_max,
        schedule_delay_millis=settings.trace_batch_delay_ms,
        max_export_batch_size=settings.trace_batch_max,
        export_timeout_millis=settings.trace_export_timeout_ms,
    )
    provider.add_span_processor(processor)
    _ATTACHED[provider] = _Attached(processor=processor, target=target)


def _resource(settings: HajerSettings) -> Resource:
    """What the SDK's own provider says about the process: its service name, its environment, the SDK's version.

    `Resource.create` merges these over OpenTelemetry's defaults (`telemetry.sdk.*`, and `service.name` from the
    standard variables when the settings name none), so an application that configured those the OpenTelemetry way
    is still described the OpenTelemetry way.
    """
    attributes: dict[str, str] = {_SDK_VERSION: VERSION}
    if settings.service_name is not None:
        attributes[_SERVICE_NAME] = settings.service_name
    if settings.environment is not None:
        attributes[_ENVIRONMENT_NAME] = settings.environment
    return Resource.create(attributes)

"""Optional OTel receiver, for `hajer.instrument(tracer_provider=…)`; never replaces the process TracerProvider.

Hajer always records a call through its own wrap or HTTP capture. The receiver is for the calls it cannot wrap —
a provider it has no surface for, a client built before the installation — and so it takes model operations only
(`observation_from_gen_ai`): a framework's workflow, task, tool and agent spans are not calls. A span of a call
Hajer already recorded is matched to that record and dropped, never recorded twice, and a span is dropped only on
evidence (`_claims`).
"""

from __future__ import annotations

from collections.abc import Callable

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider

from hajer._claims import SPAN_MAY_DUPLICATE, span_began, span_ended, verdict
from hajer._settings import HajerSettings
from hajer._targets_genai import observation_from_gen_ai
from hajer._wrap import LINKAGE_DEGRADED, accept_call


class HajerSpanProcessor(SpanProcessor):
    """An inert-on-shutdown processor can remain attached because OTel has no public remove API.

    Every call it records says it has no frames, and `on_degraded` is told the first time one is recorded."""

    def __init__(self, settings: HajerSettings, on_degraded: Callable[[], None] | None = None) -> None:
        self.settings = settings
        self.on_degraded = on_degraded
        self.active = True

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        del parent_context
        try:
            if self.active:
                span_began(span.get_span_context().span_id)
        except Exception:  # noqa: BLE001, S110 - a receiver never breaks the span owner's app
            pass

    def on_end(self, span: ReadableSpan) -> None:
        try:
            context = span.get_span_context()
            if context is None:
                return
            span_ended(context.span_id)
            if not self.active or span.start_time is None or span.end_time is None:
                return
            attributes = span.attributes or {}
            call = observation_from_gen_ai(
                attributes,
                start_ns=span.start_time,
                end_ns=span.end_time,
                capture_content=self.settings.capture_content,
            )
            decided = verdict(context.span_id, attributes.get("gen_ai.request.model"), call)
            if call is None or decided == "drop":
                return
            call.note(LINKAGE_DEGRADED)
            if decided == "flag":
                call.note(SPAN_MAY_DUPLICATE)
            accept_call(call, self.settings)
            if self.on_degraded is not None:
                self.on_degraded()
        except Exception:  # noqa: BLE001, S110 - exporter bugs never escape into the span owner's app
            pass

    def shutdown(self) -> None:
        self.active = False

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        return True


def connect(
    settings: HajerSettings, source: object | None, on_degraded: Callable[[], None] | None = None
) -> tuple[TracerProvider, HajerSpanProcessor, bool]:
    receiver = HajerSpanProcessor(settings, on_degraded)
    isolated = TracerProvider(shutdown_on_exit=False)
    isolated.add_span_processor(receiver)
    existing = source if source is not None else trace.get_tracer_provider()
    connected = isinstance(existing, TracerProvider)
    if isinstance(existing, TracerProvider):
        # Registration on the emitting provider is the actual bridge. An isolated provider alone
        # cannot see another provider's spans. This changes no sampling, exporter, or global owner.
        existing.add_span_processor(receiver)
    return isolated, receiver, connected

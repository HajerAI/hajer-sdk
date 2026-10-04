"""Where the spans go: the provider chosen by ownership, and the platform exporter added to it exactly once.

No socket: `OTLPSpanExporter` is replaced by a stand-in that records what it was built with and what it was
handed, through the real `BatchSpanProcessor`, so what is asserted is the wiring an application would get.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import ClassVar, cast

import pytest

import hajer
from hajer import _telemetry
from hajer._telemetry import ExportTarget, export_target

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hajer import _telemetry_otel

KEYED = hajer.HajerSettings(api_key="key-for-tests", team_id="team-1", base_url="https://hajer.test")
PLATFORM_URL = "https://hajer.test/api/teams/team-1/otel/v1/traces"


class RecordingExporter:
    """What `OTLPSpanExporter` is built with, and every span the batch processor hands it. Never a socket."""

    built: ClassVar[list[RecordingExporter]] = []

    def __init__(self, *, endpoint: str, headers: dict[str, str], timeout: float) -> None:
        self.endpoint = endpoint
        self.headers = headers
        self.timeout = timeout
        self.exported: list[ReadableSpan] = []
        self.shut_down = False
        RecordingExporter.built.append(self)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shut_down = True

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


@pytest.fixture(autouse=True)
def recording(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    RecordingExporter.built.clear()
    monkeypatch.setattr(_telemetry_otel, "OTLPSpanExporter", RecordingExporter)
    # The global provider is OpenTelemetry's proxy unless a test says otherwise: never an SDK provider left behind.
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)
    yield
    _telemetry.configure(None)


def _own_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    """An application's own provider, with the exporter it configured itself."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _only_exporter() -> RecordingExporter:
    (exporter,) = RecordingExporter.built
    return exporter


class TestTheTarget:
    def test_credentials_name_the_platform_receiver_with_the_key_as_bearer(self) -> None:
        target = export_target(KEYED)
        assert target == ExportTarget(PLATFORM_URL, {"Authorization": "Bearer key-for-tests"}, authenticated=True)

    def test_a_collector_of_the_process_s_own_wins_and_carries_no_key(self) -> None:
        settings = KEYED.model_copy(update={"otlp_endpoint": "http://collector.internal:4318/"})
        assert export_target(settings) == ExportTarget(
            "http://collector.internal:4318/v1/traces", {}, authenticated=False
        )

    def test_extra_headers_ride_on_either(self) -> None:
        platform = export_target(KEYED.model_copy(update={"otlp_headers": "x-tenant=acme"}))
        assert platform is not None
        assert platform.headers == {"x-tenant": "acme", "Authorization": "Bearer key-for-tests"}
        collector = export_target(
            KEYED.model_copy(update={"otlp_endpoint": "http://c:4318", "otlp_headers": "Authorization=Basic%20x"})
        )
        assert collector is not None
        assert collector.headers == {"Authorization": "Basic x"}

    @pytest.mark.parametrize(
        "settings",
        [
            hajer.HajerSettings(),
            hajer.HajerSettings(api_key="k"),
            KEYED.model_copy(update={"disabled": True}),
            KEYED.model_copy(update={"traces_enabled": False}),
        ],
        ids=["no credentials", "no team", "disabled", "traces off"],
    )
    def test_nowhere_without_a_key_a_team_or_the_switch(self, settings: hajer.HajerSettings) -> None:
        assert export_target(settings) is None

    def test_a_collector_still_wins_when_the_process_is_inert(self) -> None:
        """`hajer eval` runs the application without the key and still needs the engine's receiver reached."""
        target = export_target(hajer.HajerSettings(otlp_endpoint="http://127.0.0.1:4318"))
        assert target is not None
        assert target.url == "http://127.0.0.1:4318/v1/traces"


class TestTheProvider:
    def test_an_explicit_provider_gets_the_exporter_and_keeps_its_own(self) -> None:
        provider, own = _own_provider()
        try:
            backend = _telemetry_otel.build(KEYED, provider)
            assert backend.provider is provider
            assert backend.owned is False
            backend.start("workflow x", {"hajer.workflow.id": "x"}, traceparent=None).end(None)
            assert backend.flush(1_000) is True
            exporter = _only_exporter()
            assert (exporter.endpoint, exporter.headers) == (PLATFORM_URL, {"Authorization": "Bearer key-for-tests"})
            assert exporter.timeout == KEYED.trace_export_timeout_ms / 1_000
            assert [span.name for span in exporter.exported] == ["workflow x"]
            assert [span.name for span in cast(Sequence[ReadableSpan], own.get_finished_spans())] == ["workflow x"]
        finally:
            provider.shutdown()

    def test_the_applications_global_provider_is_used_the_same_way(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, own = _own_provider()
        monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
        try:
            backend = _telemetry_otel.build(KEYED, None)
            assert backend.provider is provider
            backend.start("workflow x", {}, traceparent=None).end(None)
            backend.flush(1_000)
            assert [span.name for span in _only_exporter().exported] == ["workflow x"]
            assert len(cast(Sequence[ReadableSpan], own.get_finished_spans())) == 1, "its own exporter still runs"
        finally:
            provider.shutdown()

    def test_with_no_provider_an_isolated_one_is_built_and_never_made_global(self) -> None:
        backend = _telemetry_otel.build(
            KEYED.model_copy(update={"service_name": "support-api", "environment": "prod"}), None
        )
        try:
            assert isinstance(backend.provider, TracerProvider)
            assert backend.owned is True
            assert not isinstance(trace.get_tracer_provider(), TracerProvider), "never installed as the global provider"
            backend.start("workflow x", {}, traceparent=None).end(None)
            backend.flush(1_000)
            (span,) = _only_exporter().exported
            resource = span.resource.attributes
            assert resource["service.name"] == "support-api"
            assert resource["deployment.environment.name"] == "prod"
            assert resource["hajer.sdk.version"] == hajer.__version__
            assert str(resource["telemetry.sdk.language"]) == "python", "OpenTelemetry's own resource is kept"
        finally:
            backend.shutdown()

    def test_no_target_and_no_provider_is_the_no_op_tracer(self) -> None:
        silent = _telemetry_otel.build(hajer.HajerSettings(), None)
        assert silent.provider is None
        assert silent.flush(10) is False
        silent.start("workflow x", {}, traceparent=None).end(None)
        assert RecordingExporter.built == []

    def test_an_explicit_provider_with_no_target_gets_no_exporter(self) -> None:
        provider, own = _own_provider()
        try:
            backend = _telemetry_otel.build(hajer.HajerSettings(), provider)
            backend.start("workflow x", {}, traceparent=None).end(None)
            assert RecordingExporter.built == []
            assert len(cast(Sequence[ReadableSpan], own.get_finished_spans())) == 1
        finally:
            provider.shutdown()


class TestOnePerProvider:
    def test_configuring_the_same_provider_twice_adds_one_exporter(self) -> None:
        provider, _ = _own_provider()
        try:
            _telemetry_otel.build(KEYED, provider)
            _telemetry_otel.build(KEYED, provider)
            assert len(RecordingExporter.built) == 1
        finally:
            provider.shutdown()

    def test_a_different_target_replaces_the_exporter(self) -> None:
        provider, _ = _own_provider()
        try:
            first = _telemetry_otel.build(KEYED, provider)
            first.start("one", {}, traceparent=None).end(None)
            first.flush(1_000)
            second = _telemetry_otel.build(KEYED.model_copy(update={"otlp_endpoint": "http://c:4318"}), provider)
            second.start("two", {}, traceparent=None).end(None)
            second.flush(1_000)
            old, new = RecordingExporter.built
            assert old.shut_down is True
            assert [span.name for span in old.exported] == ["one"]
            assert [span.name for span in new.exported] == ["two"]
            assert new.endpoint == "http://c:4318/v1/traces"
        finally:
            provider.shutdown()

    def test_shutting_the_backend_down_stops_the_exporter_and_not_the_applications_provider(self) -> None:
        provider, own = _own_provider()
        try:
            backend = _telemetry_otel.build(KEYED, provider)
            backend.shutdown()
            assert _only_exporter().shut_down is True
            provider.get_tracer("app").start_span("still mine").end()
            assert [span.name for span in cast(Sequence[ReadableSpan], own.get_finished_spans())] == ["still mine"]
        finally:
            provider.shutdown()


class TestThroughThePublicSurface:
    def test_a_workflow_span_reaches_the_platform_exporter(self) -> None:
        hajer.configure(KEYED)
        with hajer.workflow("answer"):
            pass
        assert hajer.flush() is True
        (span,) = _only_exporter().exported
        assert span.name == "workflow answer"

    def test_the_batch_processor_takes_its_bounds_from_the_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        built: list[dict[str, object]] = []

        class Recording(BatchSpanProcessor):
            def __init__(self, exporter: RecordingExporter, **bounds: object) -> None:
                built.append(dict(bounds))
                super().__init__(exporter, **bounds)  # pyright: ignore[reportArgumentType] - the stand-in exporter

        monkeypatch.setattr(_telemetry_otel, "BatchSpanProcessor", Recording)
        bounded = KEYED.model_copy(
            update={
                "trace_queue_max": 7,
                "trace_batch_delay_ms": 11,
                "trace_batch_max": 3,
                "trace_export_timeout_ms": 13,
            }
        )
        hajer.configure(bounded)
        with hajer.workflow("answer"):
            pass
        assert built == [
            {"max_queue_size": 7, "schedule_delay_millis": 11, "max_export_batch_size": 3, "export_timeout_millis": 13}
        ]
        assert _only_exporter().timeout == 0.013

    def test_reconfiguring_shuts_the_previous_isolated_provider_down(self) -> None:
        hajer.configure(KEYED)
        with hajer.workflow("answer"):
            pass
        hajer.flush()
        first = _only_exporter()
        hajer.configure(None)
        assert first.shut_down is True

    def test_traces_off_keeps_the_spans_on_the_applications_provider_only(self) -> None:
        provider, own = _own_provider()
        try:
            hajer.configure(KEYED.model_copy(update={"traces_enabled": False}), tracer_provider=provider)
            with hajer.workflow("answer"):
                pass
            assert RecordingExporter.built == []
            assert len(cast(Sequence[ReadableSpan], own.get_finished_spans())) == 1
        finally:
            provider.shutdown()

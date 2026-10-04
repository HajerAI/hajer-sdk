"""`upload_eval_run`: what goes on the wire, what is dropped to fit, what is retried, and what never leaves.

Every claim is made against the recorded request or the receipt, never against the module's internals; the
sleep is a list the test reads, so the backoff is asserted without a clock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

import httpx
import pytest

from hajer import HajerSettings
from hajer._json import JsonObject, JsonValue
from hajer.evals._upload import UploadReceipt, upload_eval_run
from tests.conftest import Recorder

EXPECTED_PATH = "/api/teams/team-1/projects/proj-1/eval-runs"


def configured(**overrides: object) -> HajerSettings:
    """A non-inert configuration whose backoff is measured in whole milliseconds, so a test can spell it."""
    fields: dict[str, object] = {
        "api_key": "k",
        "team_id": "team-1",
        "project_id": "proj-1",
        "base_url": "https://hajer.test",
        "eval_upload_deadline_ms": 250,
        "observe_backoff_initial_ms": 1,
        "observe_backoff_max_ms": 4,
    }
    fields.update(overrides)
    return HajerSettings.model_validate(fields)


def run_payload(**overrides: JsonValue) -> JsonObject:
    payload: JsonObject = {
        "runId": "run-123",
        "schemaVersion": 1,
        "results": [
            {"testId": "t-1", "passed": True, "output": "a short answer", "spans": [{"name": "workflow"}]},
            {"testId": "t-2", "passed": False, "output": "another answer", "spans": [{"name": "tool"}]},
        ],
    }
    payload.update(overrides)
    return payload


def status(code: int) -> Recorder:
    return Recorder(lambda _request: httpx.Response(code, json={}))


def never_called(request: httpx.Request) -> httpx.Response:
    pytest.fail(f"nothing should have been sent, got {request.method} {request.url}")


class SleepSpy:
    """Records every wait the upload asked for instead of taking it."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def upload(
    recorder: Recorder,
    *,
    settings: HajerSettings | None = None,
    payload: JsonObject | None = None,
    sleep: SleepSpy | None = None,
    project_id: str | None = None,
) -> UploadReceipt:
    return upload_eval_run(
        payload if payload is not None else run_payload(),
        settings=settings if settings is not None else configured(),
        project_id=project_id,
        transport=recorder.transport(),
        sleep=sleep if sleep is not None else SleepSpy(),
    )


def read_timeout_of(request: httpx.Request) -> float:
    extensions: Mapping[str, object] = request.extensions
    timeout = extensions.get("timeout")
    assert isinstance(timeout, dict)
    read = cast(Mapping[str, object], timeout).get("read")
    assert isinstance(read, (int, float))
    return float(read)


class TestTheWire:
    def test_the_request_is_the_path_the_bearer_the_run_id_and_the_payload(self) -> None:
        recorder = status(201)
        payload = run_payload()

        receipt = upload(recorder, payload=payload)

        assert receipt == UploadReceipt(status="uploaded", http_status=201, reason=None, attempts=1, degraded=())
        [request] = recorder.requests
        assert request.method == "POST"
        assert request.url == httpx.URL("https://hajer.test" + EXPECTED_PATH)
        assert request.headers["Authorization"] == "Bearer k"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Idempotency-Key"] == "run-123"
        assert recorder.bodies() == [payload]

    def test_the_per_request_timeout_is_the_upload_deadline(self) -> None:
        recorder = status(201)

        upload(recorder, settings=configured(eval_upload_deadline_ms=250))

        assert read_timeout_of(recorder.requests[0]) == pytest.approx(0.25, abs=0.05)

    def test_a_conflict_means_the_run_is_already_stored(self) -> None:
        receipt = upload(status(409))

        assert receipt.status == "uploaded"
        assert receipt.http_status == 409
        assert receipt.reason is None
        assert receipt.attempts == 1

    def test_an_explicit_project_overrides_the_settings(self) -> None:
        recorder = status(201)

        upload(recorder, project_id="proj-override")

        assert recorder.requests[0].url.path == "/api/teams/team-1/projects/proj-override/eval-runs"


class TestRetry:
    def test_a_server_error_is_retried_under_the_same_key(self) -> None:
        answers = iter([500, 201])
        recorder = Recorder(lambda _request: httpx.Response(next(answers), json={}))
        sleep = SleepSpy()

        receipt = upload(recorder, sleep=sleep)

        assert receipt == UploadReceipt(status="uploaded", http_status=201, reason=None, attempts=2, degraded=())
        assert recorder.keys() == ["run-123", "run-123"]
        assert recorder.bodies()[0] == recorder.bodies()[1], "the same bytes every attempt"
        assert sleep.waits == [pytest.approx(0.001)]

    def test_a_refusal_is_final_and_waits_for_nothing(self) -> None:
        recorder = status(422)
        sleep = SleepSpy()

        receipt = upload(recorder, sleep=sleep)

        assert receipt == UploadReceipt(status="failed", http_status=422, reason="REFUSED", attempts=1, degraded=())
        assert len(recorder.requests) == 1
        assert sleep.waits == []

    def test_every_attempt_can_fail_and_the_receipt_carries_the_last_status(self) -> None:
        recorder = status(503)
        sleep = SleepSpy()

        receipt = upload(recorder, settings=configured(eval_upload_attempts=3), sleep=sleep)

        assert receipt == UploadReceipt(status="failed", http_status=503, reason="UNREACHABLE", attempts=3, degraded=())
        assert len(recorder.requests) == 3
        assert sleep.waits == [pytest.approx(0.001), pytest.approx(0.002)], "no wait after the last attempt"

    def test_the_backoff_doubles_and_is_capped(self) -> None:
        sleep = SleepSpy()

        upload(status(503), settings=configured(eval_upload_attempts=5), sleep=sleep)

        assert sleep.waits == [
            pytest.approx(0.001),
            pytest.approx(0.002),
            pytest.approx(0.004),
            pytest.approx(0.004),
        ]

    def test_a_connection_failure_every_time_is_unreachable(self) -> None:
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        recorder = Recorder(refused)
        sleep = SleepSpy()

        receipt = upload(recorder, settings=configured(eval_upload_attempts=3), sleep=sleep)

        assert receipt == UploadReceipt(
            status="failed", http_status=None, reason="UNREACHABLE", attempts=3, degraded=()
        )
        assert len(recorder.requests) == 3
        assert len(sleep.waits) == 2

    def test_a_timeout_every_time_is_timeout(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated read timeout", request=request)

        receipt = upload(Recorder(slow), settings=configured(eval_upload_attempts=2))

        assert receipt == UploadReceipt(status="failed", http_status=None, reason="TIMEOUT", attempts=2, degraded=())

    def test_the_reason_is_the_last_attempts_outcome(self) -> None:
        outcomes = iter(["timeout", "connect"])

        def mixed(request: httpx.Request) -> httpx.Response:
            if next(outcomes) == "timeout":
                raise httpx.ReadTimeout("simulated read timeout", request=request)
            raise httpx.ConnectError("refused", request=request)

        receipt = upload(Recorder(mixed), settings=configured(eval_upload_attempts=2))

        assert receipt.reason == "UNREACHABLE"
        assert receipt.attempts == 2


class TestNothingSent:
    def test_inert_settings_send_nothing(self) -> None:
        for inert in (configured(disabled=True), configured(api_key=None), configured(team_id=None)):
            receipt = upload(Recorder(never_called), settings=inert)

            assert receipt == UploadReceipt(status="skipped", http_status=None, reason="INERT", attempts=0, degraded=())

    def test_no_project_anywhere_sends_nothing(self) -> None:
        receipt = upload(Recorder(never_called), settings=configured(project_id=None))

        assert receipt == UploadReceipt(
            status="skipped", http_status=None, reason="NO_PROJECT", attempts=0, degraded=()
        )

    def test_a_payload_without_a_run_id_is_reported_not_raised(self) -> None:
        payload = run_payload()
        del payload["runId"]

        receipt = upload(Recorder(never_called), payload=payload)

        assert receipt == UploadReceipt(status="failed", http_status=None, reason="NO_RUN_ID", attempts=0, degraded=())


def encoded_size(payload: JsonObject) -> int:
    """The bytes `encode_body` would produce: canonical JSON, sorted keys, no whitespace."""
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8"))


def results_of(body: JsonObject) -> list[JsonObject]:
    results = body["results"]
    assert isinstance(results, list)
    out: list[JsonObject] = []
    for result in results:
        assert isinstance(result, dict)
        out.append(result)
    return out


def heavy_result(n: int) -> JsonObject:
    """One result whose spans outweigh its output, which outweighs the rest: the order the stages drop them."""
    spans: list[JsonValue] = [{"name": "x" * 500} for _ in range(4)]
    return {"testId": f"t-{n}", "passed": True, "output": "y" * 200, "spans": spans}


class TestFittingTheBody:
    """Bounds are derived from the payload's own sizes, so the tests say exactly which bound each stage meets."""

    @pytest.fixture
    def heavy(self) -> JsonObject:
        results: list[JsonValue] = [heavy_result(n) for n in range(3)]
        return run_payload(results=results)

    @staticmethod
    def sizes(heavy: JsonObject) -> tuple[int, int, int]:
        """Full, without spans, without spans and outputs."""
        no_spans = run_payload(results=[{**result, "spans": []} for result in results_of(heavy)])
        no_outputs = run_payload(results=[{**result, "output": None} for result in results_of(no_spans)])
        return encoded_size(heavy), encoded_size(no_spans), encoded_size(no_outputs)

    def test_a_payload_that_fits_is_sent_whole(self, heavy: JsonObject) -> None:
        full, _, _ = self.sizes(heavy)
        recorder = status(201)

        receipt = upload(recorder, payload=heavy, settings=configured(eval_upload_max_bytes=full))

        assert receipt.degraded == ()
        assert recorder.bodies() == [heavy]

    def test_spans_are_dropped_first(self, heavy: JsonObject) -> None:
        full, no_spans, _ = self.sizes(heavy)
        recorder = status(201)
        before = json.dumps(heavy, sort_keys=True)

        receipt = upload(recorder, payload=heavy, settings=configured(eval_upload_max_bytes=full - 1))

        assert receipt.status == "uploaded"
        assert receipt.degraded == ("spans",)
        [body] = recorder.bodies()
        assert len(recorder.requests[0].content) == no_spans
        for result in results_of(body):
            assert result["spans"] == []
            assert isinstance(result["output"], str), "the outputs survive the first stage"
        assert json.dumps(heavy, sort_keys=True) == before, "the caller's payload is untouched"

    def test_then_the_outputs(self, heavy: JsonObject) -> None:
        _, no_spans, no_outputs = self.sizes(heavy)
        recorder = status(201)

        receipt = upload(recorder, payload=heavy, settings=configured(eval_upload_max_bytes=no_spans - 1))

        assert receipt.status == "uploaded"
        assert receipt.degraded == ("spans", "outputs")
        [body] = recorder.bodies()
        assert len(recorder.requests[0].content) == no_outputs
        for result in results_of(body):
            assert result["spans"] == []
            assert result["output"] is None
            assert result["testId"] in {"t-0", "t-1", "t-2"}, "every other field survives"

    def test_past_both_stages_nothing_is_sent(self, heavy: JsonObject) -> None:
        _, _, no_outputs = self.sizes(heavy)

        receipt = upload(
            Recorder(never_called), payload=heavy, settings=configured(eval_upload_max_bytes=no_outputs - 1)
        )

        assert receipt == UploadReceipt(
            status="failed",
            http_status=None,
            reason="BODY_OVER_BOUND",
            attempts=0,
            degraded=("spans", "outputs"),
        )

    def test_a_result_without_the_key_is_left_alone(self) -> None:
        bare: JsonObject = {"testId": "bare", "passed": True}
        payload = run_payload(
            results=[bare, {"testId": "full", "passed": True, "output": "o" * 300, "spans": ["s" * 300]}]
        )
        recorder = status(201)

        receipt = upload(
            recorder, payload=payload, settings=configured(eval_upload_max_bytes=encoded_size(payload) - 1)
        )

        assert receipt.degraded == ("spans",)
        first, second = results_of(recorder.bodies()[0])
        assert first == bare, "no `spans: []` is invented on a result that had none"
        assert second["spans"] == []

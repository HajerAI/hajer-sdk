"""`observe` — the three receipts, the two bounds, the backoff, and the flush on close."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import hajer
from hajer import _payload
from hajer._queue import Bounds  # the backoff schedule has no other reader
from hajer._settings import HajerSettings
from hajer._transport import encode_body
from tests.conftest import Recorder, Responder, assessment_json, responds


def accepts(*ids: str) -> Responder:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/observe"):
            return httpx.Response(202, json={"observationIds": list(ids)})
        return httpx.Response(200, json=assessment_json())

    return responder


def sleepy(settings: HajerSettings, **overrides: object) -> HajerSettings:
    """The same settings with a flush interval long enough that the worker never fires in a test."""
    return settings.model_copy(update={"observe_flush_interval_ms": 600_000, **overrides})


def _one_observation_bytes(settings: HajerSettings) -> int:
    """How many bytes one padded observation of the shape below actually encodes to, right now."""
    recorder = Recorder(accepts("a"))
    with hajer.Hajer(settings=sleepy(settings), transport=recorder.transport()) as client:
        client.observe("refund-policy", {"n": 0, "pad": "p" * 400}, "reply")
        client.flush()
    sent = recorder.bodies()[0]["observations"]
    assert isinstance(sent, list)
    observation = sent[0]
    assert isinstance(observation, dict)
    return len(encode_body(observation, limit=1 << 30))


class TestReceipts:
    def test_queued_then_accepted_then_complete(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-7"))
        with hajer.Hajer(settings=sleepy(settings), transport=recorder.transport()) as client:
            receipt = client.observe("refund-policy", {"orderId": "o-1"}, "reply", {"orderState": "paid"})
            assert receipt.state == "queued"
            assert receipt.queued is True
            assert receipt.accepted is False
            assert receipt.queued_at > 0
            assert client.queue_depth() == 1

            client.flush()
            assert receipt.state == "accepted"
            assert receipt.accepted is True
            assert receipt.observation_id == "obs-7"
            assert receipt.complete is False

            assessment = receipt.wait(timeout_ms=200)
            assert assessment is not None
            assert receipt.state == "complete"
            assert receipt.assessment is assessment
            assert assessment.status == "satisfied"

    def test_the_observation_carries_mode_observe_and_no_deadline(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        with hajer.Hajer(settings=sleepy(settings), transport=recorder.transport()) as client:
            client.observe("refund-policy", {"orderId": "o-1"}, "reply")
            client.flush()
        body = recorder.bodies()[0]
        observations = body["observations"]
        assert isinstance(observations, list)
        first = observations[0]
        assert isinstance(first, dict)
        assert first["mode"] == "OBSERVE"
        assert "deadlineMs" not in first

    def test_poll_before_acceptance_asks_nothing(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        with hajer.Hajer(settings=sleepy(settings), transport=recorder.transport()) as client:
            receipt = client.observe("refund-policy", {}, "reply")
            assert receipt.poll() is None
            assert recorder.requests == []

    def test_wait_returns_none_when_the_assessment_is_not_ready(self, settings: HajerSettings) -> None:
        def pending(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/observe"):
                return httpx.Response(202, json={"observationIds": ["obs-1"]})
            return httpx.Response(404, json={"error": "not ready"})

        recorder = Recorder(pending)
        with hajer.Hajer(settings=sleepy(settings), transport=recorder.transport()) as client:
            receipt = client.observe("refund-policy", {}, "reply")
            client.flush()
            assert receipt.wait(timeout_ms=150) is None
            assert receipt.state == "accepted"


class TestBounds:
    def test_a_full_queue_refuses_the_newest_and_says_so(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        bounded = sleepy(settings, observe_queue_max=1, observe_batch_max=64)
        with hajer.Hajer(settings=bounded, transport=recorder.transport()) as client:
            first = client.observe("refund-policy", {"n": 1}, "a")
            second = client.observe("refund-policy", {"n": 2}, "b")
            assert first.state == "queued"
            assert second.state == "dropped"
            assert second.detail == "QUEUE_FULL"
            assert client.queue_depth() == 1

    def test_one_observation_over_the_body_bound_is_dropped_locally(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts())
        bounded = sleepy(settings, body_max_bytes=256)
        with hajer.Hajer(settings=bounded, transport=recorder.transport()) as client:
            receipt = client.observe("refund-policy", {"q": "x" * 4096}, "reply")
            assert receipt.state == "dropped"
            assert receipt.detail == "BODY_OVER_BOUND"
            assert client.queue_depth() == 0
        assert recorder.requests == []

    def test_a_flush_is_batched_by_count(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("a", "b"))
        batched = sleepy(settings, observe_batch_max=2)
        with hajer.Hajer(settings=batched, transport=recorder.transport()) as client:
            for index in range(4):
                client.observe("refund-policy", {"n": index}, "reply")
            client.flush()
        assert len(recorder.requests) == 2
        for body in recorder.bodies():
            observations = body["observations"]
            assert isinstance(observations, list)
            assert len(observations) == 2

    def test_a_flush_is_batched_by_bytes(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("a"))
        # Room for one observation plus the envelope, never two. The size is *measured* rather than
        # guessed: a hard-coded estimate here turns every deliberate growth of the wire (`caseKey` and
        # `caseKeySource`) into a red test about batching, which is not what this test is about.
        one = _one_observation_bytes(settings)
        batched = sleepy(settings, observe_batch_max=32, body_max_bytes=one + _payload.batch_overhead_bytes())
        with hajer.Hajer(settings=batched, transport=recorder.transport()) as client:
            for index in range(3):
                client.observe("refund-policy", {"n": index, "pad": "p" * 400}, "reply")
            client.flush()
        assert len(recorder.requests) == 3
        for request in recorder.requests:
            assert len(request.content) <= batched.body_max_bytes

    def test_the_batch_key_is_one_header_whatever_the_batch_size(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("a", "b", "c"))
        with hajer.Hajer(settings=sleepy(settings), transport=recorder.transport()) as client:
            for index in range(3):
                client.observe("refund-policy", {"n": index}, "reply")
            client.flush()
        key = recorder.keys()[0]
        assert key.startswith("hj1batch_")
        assert len(key) < 128


class TestFailureAndRetry:
    def test_a_failed_flush_keeps_the_observations_queued(self, settings: HajerSettings) -> None:
        attempts: list[int] = []

        def flaky(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(202, json={"observationIds": ["obs-1"]})

        recorder = Recorder(flaky)
        with hajer.Hajer(settings=sleepy(settings), transport=recorder.transport()) as client:
            receipt = client.observe("refund-policy", {}, "reply")
            client.flush()
            assert receipt.state == "queued", "a failed flush loses nothing"
            assert client.queue_depth() == 1
            client.flush()
            assert receipt.state == "accepted"
            assert receipt.observation_id == "obs-1"

    def test_the_backoff_doubles_and_is_capped(self) -> None:
        settings = HajerSettings(observe_backoff_initial_ms=100, observe_backoff_max_ms=400)
        bounds = Bounds(settings, overhead=0)
        assert bounds.failed() == pytest.approx(0.1)
        assert bounds.failed() == pytest.approx(0.2)
        assert bounds.failed() == pytest.approx(0.4)
        assert bounds.failed() == pytest.approx(0.4), "capped"
        bounds.succeeded()
        assert bounds.failed() == pytest.approx(0.1), "reset on success"

    def test_a_transport_failure_during_flush_never_reaches_the_caller(self, settings: HajerSettings) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        recorder = Recorder(explode)
        with hajer.Hajer(settings=sleepy(settings), transport=recorder.transport()) as client:
            receipt = client.observe("refund-policy", {}, "reply")
            client.flush()
            assert receipt.state == "queued"


class TestLifecycle:
    def test_close_flushes_what_is_queued(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        client = hajer.Hajer(settings=sleepy(settings), transport=recorder.transport())
        receipt = client.observe("refund-policy", {}, "reply")
        assert receipt.state == "queued"
        client.close()
        assert receipt.state == "accepted"
        assert len(recorder.requests) == 1

    def test_close_is_idempotent(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        client = hajer.Hajer(settings=sleepy(settings), transport=recorder.transport())
        client.observe("refund-policy", {}, "reply")
        client.close()
        client.close()
        assert len(recorder.requests) == 1

    def test_observing_after_close_is_dropped_not_lost_silently(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        client = hajer.Hajer(settings=sleepy(settings), transport=recorder.transport())
        client.close()
        receipt = client.observe("refund-policy", {}, "reply")
        assert receipt.state == "dropped"
        assert receipt.detail == "CLIENT_CLOSED"

    def test_the_background_worker_flushes_on_the_interval(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        prompt = settings.model_copy(update={"observe_flush_interval_ms": 20, "observe_batch_max": 64})
        with hajer.Hajer(settings=prompt, transport=recorder.transport()) as client:
            receipt = client.observe("refund-policy", {}, "reply")
            deadline = time.monotonic() + 3.0
            while receipt.state == "queued" and time.monotonic() < deadline:
                time.sleep(0.01)
            assert receipt.state == "accepted", "the interval flush never fired"


class TestAsyncObserve:
    async def test_queued_then_accepted_then_complete(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-9"))
        async with hajer.AsyncHajer(settings=sleepy(settings), transport=recorder.transport()) as client:
            receipt = client.observe("refund-policy", {}, "reply")
            assert receipt.state == "queued"
            await client.flush()
            assert receipt.state == "accepted"
            assert receipt.observation_id == "obs-9"
            assessment = await receipt.wait(timeout_ms=200)
            assert assessment is not None
            assert receipt.complete is True

    async def test_close_flushes(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        client = hajer.AsyncHajer(settings=sleepy(settings), transport=recorder.transport())
        receipt = client.observe("refund-policy", {}, "reply")
        await client.close()
        assert receipt.state == "accepted"

    async def test_a_full_queue_refuses_the_newest(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        bounded = sleepy(settings, observe_queue_max=1, observe_batch_max=64)
        async with hajer.AsyncHajer(settings=bounded, transport=recorder.transport()) as client:
            client.observe("refund-policy", {"n": 1}, "a")
            second = client.observe("refund-policy", {"n": 2}, "b")
            assert second.detail == "QUEUE_FULL"

    async def test_the_background_task_flushes_on_the_interval(self, settings: HajerSettings) -> None:
        recorder = Recorder(accepts("obs-1"))
        prompt = settings.model_copy(update={"observe_flush_interval_ms": 20, "observe_batch_max": 64})
        async with hajer.AsyncHajer(settings=prompt, transport=recorder.transport()) as client:
            receipt = client.observe("refund-policy", {}, "reply")
            deadline = time.monotonic() + 3.0
            # The condition is a background task's effect on a receipt, not an event we own.
            while receipt.state == "queued" and time.monotonic() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            assert receipt.state == "accepted"


def test_a_rejected_flush_body_does_not_crash_the_worker(settings: HajerSettings) -> None:
    """A 4xx is a failure like any other: the observations stay queued and nothing raises."""
    recorder = Recorder(responds({"error": "bad request"}, status=400))
    with hajer.Hajer(settings=sleepy(settings), transport=recorder.transport()) as client:
        receipt = client.observe("refund-policy", {}, "reply")
        client.flush()
        assert receipt.state == "queued"

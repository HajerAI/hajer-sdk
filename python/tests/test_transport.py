"""The deadline, the body bound, the headers, and the absence of retries."""

from __future__ import annotations

import time

import httpx
import pytest

import hajer
from hajer._payload import batch_overhead_bytes
from hajer._transport import USER_AGENT, Deadline, encode_body
from tests.conftest import Recorder, assessment_json, responds, times_out


class TestDeadline:
    def test_remaining_never_goes_negative(self) -> None:
        deadline = Deadline(1)
        time.sleep(0.01)
        assert deadline.remaining_ms() == 0
        assert deadline.expired() is True
        assert deadline.timeout_s() == 0.0

    def test_budget_is_what_was_asked_for(self) -> None:
        deadline = Deadline(750)
        assert deadline.budget_ms == 750
        assert 0 < deadline.remaining_ms() <= 750
        assert deadline.elapsed_ms() >= 0


class TestEncodeBody:
    def test_canonical_bytes_are_stable_across_key_order(self) -> None:
        first = encode_body({"b": 1, "a": 2}, limit=1024)
        second = encode_body({"a": 2, "b": 1}, limit=1024)
        assert first == second == b'{"a":2,"b":1}'

    def test_over_the_bound_refuses_locally(self) -> None:
        with pytest.raises(hajer.BodyOverBoundError) as raised:
            encode_body({"payload": "x" * 200}, limit=64)
        assert raised.value.limit == 64
        assert raised.value.size > 64

    def test_batch_overhead_is_measured(self) -> None:
        assert batch_overhead_bytes() == len(b'{"observations":[]}')


class TestVerifyOverTheWire:
    def test_headers_and_path(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds(assessment_json()))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            client.verify("refund-policy", {"orderId": "o-1"}, "reply", {"orderState": "paid"})
        request = recorder.requests[0]
        assert request.url.path == "/api/teams/team-1/verify"
        assert request.headers["Authorization"] == "Bearer key-for-tests"
        assert request.headers["User-Agent"] == USER_AGENT
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["Idempotency-Key"].startswith("hj1_")

    def test_the_server_is_told_the_remaining_budget(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds(assessment_json()))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            client.verify("refund-policy", {"orderId": "o-1"}, "reply", deadline_ms=400)
        body = recorder.bodies()[0]
        remaining = body["deadlineMs"]
        assert isinstance(remaining, int)
        assert 0 < remaining <= 400

    def test_a_body_over_the_bound_is_refused_before_the_socket(self) -> None:
        settings = hajer.HajerSettings(api_key="k", team_id="team-1", base_url="https://hajer.test", body_max_bytes=256)
        recorder = Recorder(responds(assessment_json()))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            assessment = client.verify("refund-policy", {"q": "x" * 4096}, "reply")
        assert assessment.status == "unavailable"
        assert assessment.reason == "BODY_OVER_BOUND"
        assert recorder.requests == []

    def test_a_deadline_that_expires_is_unavailable_within_the_budget_plus_50ms(
        self, settings: hajer.HajerSettings
    ) -> None:
        recorder = Recorder(times_out)
        started = time.monotonic()
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            assessment = client.verify("refund-policy", {"orderId": "o-1"}, "reply", deadline_ms=120)
        elapsed_ms = (time.monotonic() - started) * 1000
        assert assessment.status == "unavailable"
        assert assessment.reason == "LOCAL_DEADLINE"
        assert elapsed_ms <= 120 + 50, f"took {elapsed_ms:.0f} ms for a 120 ms deadline"
        assert len(recorder.requests) == 1, "no automatic inline retry"

    def test_a_transport_failure_is_unavailable_transport(self, settings: hajer.HajerSettings) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        recorder = Recorder(explode)
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            assessment = client.verify("refund-policy", {}, "reply")
        assert assessment.reason == "TRANSPORT"
        assert len(recorder.requests) == 1

    def test_a_server_error_is_unavailable_http_status_and_is_not_retried(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds({"error": "boom"}, status=503))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            assessment = client.verify("refund-policy", {}, "reply")
        assert assessment.reason == "HTTP_STATUS"
        assert len(recorder.requests) == 1

    def test_a_body_that_is_not_an_assessment_is_unavailable_malformed(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds({"unexpected": True}))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            assert client.verify("refund-policy", {}, "reply").reason == "MALFORMED_RESPONSE"


class TestAsyncVerifyOverTheWire:
    async def test_the_assessment_is_parsed(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(
            responds(
                assessment_json(
                    status="violated",
                    findings=[
                        {
                            "obligation": "no-unapproved-refund@2",
                            "check": "refund@1",
                            "statement": "promised a refund",
                            "evidenceUsed": ["approvedPolicy"],
                        }
                    ],
                )
            )
        )
        async with hajer.AsyncHajer(settings=settings, transport=recorder.transport()) as client:
            assessment = await client.verify("refund-policy", {}, "we will refund you")
        assert assessment.status == "violated"
        assert assessment.findings[0].obligation == "no-unapproved-refund@2"
        assert assessment.findings[0].evidence_used == ("approvedPolicy",)
        assert assessment.per_check[0].reason == "satisfied"
        assert assessment.cost_microusd == 120
        assert assessment.verifier == "refund-policy@1"

    async def test_a_deadline_that_expires_is_unavailable(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(times_out)
        async with hajer.AsyncHajer(settings=settings, transport=recorder.transport()) as client:
            assessment = await client.verify("refund-policy", {}, "reply", deadline_ms=80)
        assert assessment.reason == "LOCAL_DEADLINE"

    async def test_a_body_over_the_bound_is_refused_before_the_socket(self) -> None:
        settings = hajer.HajerSettings(api_key="k", team_id="t", base_url="https://hajer.test", body_max_bytes=128)
        recorder = Recorder(responds(assessment_json()))
        async with hajer.AsyncHajer(settings=settings, transport=recorder.transport()) as client:
            assessment = await client.verify("v", {"q": "y" * 2048}, "reply")
        assert assessment.reason == "BODY_OVER_BOUND"
        assert recorder.requests == []

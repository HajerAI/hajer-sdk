"""The idempotency key, the observation body, and which wire module is in force."""

from __future__ import annotations

import asyncio
import json
from typing import get_args

import pytest

import hajer
from hajer import _paths, _payload, _wire
from hajer._json import JsonObject, JsonValue
from tests.conftest import Recorder, assessment_json, responds
from tests.fakes import FakeAnthropic as FakeAnthropicClient
from tests.fakes import FakeAnthropicMessage, FakeChatCompletion, FakeOpenAI
from tests.repo import CONTRACT

SNAPSHOT = CONTRACT / "openapi.json"


def key(**overrides: object) -> str:
    args: dict[str, object] = {
        "team_id": "team-1",
        "verifier": "refund-policy",
        "request": {"orderId": "o-1", "question": "where is my refund"},
        "output": "we will refund you",
        "evidence": {"orderState": "paid"},
    }
    args.update(overrides)
    return _payload.derive_idempotency_key(**args)  # pyright: ignore[reportArgumentType]


class TestIdempotencyKey:
    def test_the_same_submission_derives_the_same_key(self) -> None:
        assert key() == key()

    def test_key_order_inside_the_payload_does_not_change_it(self) -> None:
        one = _payload.derive_idempotency_key(
            team_id="t", verifier="v", request={"a": 1, "b": 2}, output="o", evidence={"x": 1, "y": 2}
        )
        two = _payload.derive_idempotency_key(
            team_id="t", verifier="v", request={"b": 2, "a": 1}, output="o", evidence={"y": 2, "x": 1}
        )
        assert one == two

    def test_it_binds_the_tenant(self) -> None:
        assert key(team_id="team-2") != key()

    def test_it_binds_the_verifier(self) -> None:
        assert key(verifier="other-policy") != key()

    def test_it_binds_the_payload(self) -> None:
        assert key(output="we will not refund you") != key()
        assert key(evidence={"orderState": "refunded"}) != key()
        assert key(request={"orderId": "o-2"}) != key()

    def test_absent_evidence_and_empty_evidence_are_the_same_submission(self) -> None:
        assert key(evidence=None) == key(evidence={})

    def test_the_scheme_is_named_in_the_key(self) -> None:
        assert key().startswith(f"{_payload.IDEMPOTENCY_SCHEME}_")

    def test_the_wrapped_calls_do_not_change_it(self, settings: hajer.HajerSettings) -> None:
        """Two identical submissions, one with a model call recorded: the same key."""
        recorder = Recorder(responds(assessment_json()))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            client.verify("refund-policy", {"orderId": "o-1"}, "reply", {"orderState": "paid"})
            wrapped = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion()]), settings=settings)
            wrapped.chat.completions.create(model="gpt-fake-1", messages=[])
            client.verify("refund-policy", {"orderId": "o-1"}, "reply", {"orderState": "paid"})
        first, second = recorder.keys()
        assert first == second
        bodies = recorder.bodies()
        assert bodies[0]["wrappedCalls"] == []
        assert len(bodies[1]["wrappedCalls"]) == 1  # pyright: ignore[reportArgumentType]

    def test_a_caller_supplied_key_is_used_verbatim(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds(assessment_json()))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            client.verify("refund-policy", {}, "reply", idempotency_key="request-42")
        assert recorder.keys() == ["request-42"]
        assert recorder.bodies()[0]["idempotencyKey"] == "request-42"


class TestObservationBody:
    def test_the_shape_the_ingest_route_will_receive(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds(assessment_json()))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            client.verify("refund-policy", {"orderId": "o-1"}, "reply", {"orderState": "paid"})
        body: JsonObject = recorder.bodies()[0]
        assert body["mode"] == "VERIFY"
        assert body["verifier"] == "refund-policy"
        assert body["request"] == {"orderId": "o-1"}
        assert body["output"] == "reply"
        assert body["evidence"] == {"orderState": "paid"}
        assert body["wrappedCalls"] == []
        assert body["wrappedCallsDropped"] == 0
        assert body["contentCaptured"] is True
        assert body["sdk"] == {"name": "hajer-python", "version": hajer.__version__}

    def test_content_capture_is_declared_on_every_submission(self) -> None:
        loud = hajer.HajerSettings(api_key="k", team_id="team-1", base_url="https://hajer.test", capture_content=True)
        recorder = Recorder(responds(assessment_json()))
        with hajer.Hajer(settings=loud, transport=recorder.transport()) as client:
            client.verify("refund-policy", {}, "reply")
        assert recorder.bodies()[0]["contentCaptured"] is True

    def test_the_dropped_count_travels_with_the_calls_that_were_kept(self) -> None:
        capped = hajer.HajerSettings(api_key="k", team_id="team-1", base_url="https://hajer.test", wrapped_calls_max=1)
        recorder = Recorder(responds(assessment_json()))
        wrapped = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion() for _ in range(3)]), settings=capped)
        for _ in range(3):
            wrapped.chat.completions.create(model="gpt-fake-1", messages=[])
        with hajer.Hajer(settings=capped, transport=recorder.transport()) as client:
            client.verify("refund-policy", {}, "reply")
        body = recorder.bodies()[0]
        assert len(body["wrappedCalls"]) == 1  # pyright: ignore[reportArgumentType]
        assert body["wrappedCallsDropped"] == 2


class TestAnOperationsCalls:
    """A `verify` inside `hajer.scope()` carries the calls the operation's child tasks made (F-SDK-2)."""

    async def test_the_submission_carries_a_child_tasks_calls(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds(assessment_json()))
        wrapped = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion() for _ in range(2)]), settings=settings)

        async def child() -> None:
            wrapped.chat.completions.create(model="gpt-fake-1", messages=[])

        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            with hajer.scope():
                await asyncio.gather(child(), child())
                client.verify("refund-policy", {"orderId": "o-1"}, "reply")
        body = recorder.bodies()[0]
        assert len(body["wrappedCalls"]) == 2  # pyright: ignore[reportArgumentType]
        assert body["wrappedCallsDropped"] == 0

    async def test_the_operation_is_emptied_by_the_submission_that_took_it(self, settings: hajer.HajerSettings) -> None:
        """Two verifies in one scope: the second carries what was recorded after the first, and no more."""
        recorder = Recorder(responds(assessment_json()))
        wrapped = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion() for _ in range(2)]), settings=settings)

        async def child() -> None:
            wrapped.chat.completions.create(model="gpt-fake-1", messages=[])

        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            with hajer.scope():
                await asyncio.gather(child())
                client.verify("refund-policy", {}, "one")
                await asyncio.gather(child())
                client.verify("refund-policy", {}, "two")
        first, second = recorder.bodies()
        assert len(first["wrappedCalls"]) == 1  # pyright: ignore[reportArgumentType]
        assert len(second["wrappedCalls"]) == 1  # pyright: ignore[reportArgumentType]


class TestWireSource:
    def test_the_wire_is_generated_from_the_platform_snapshot(self) -> None:
        assert _wire.WIRE_READY is True
        assert _wire.OPERATIONS == _paths.SDK_OPERATIONS
        assert _wire.SNAPSHOT_DIGEST.startswith("sha256:")
        assert hajer.wire_source().startswith("generated from contract/openapi.json")
        assert _wire.SNAPSHOT_DIGEST in hajer.wire_source()

    def test_the_mode_vocabulary_is_the_backends_own(self) -> None:
        assert get_args(_payload.IngestMode) == ("VERIFY", "OBSERVE")

    def test_a_malformed_assessment_is_none_rather_than_an_exception(self) -> None:
        assert _payload.parse_assessment(b"{ not json") is None
        assert _payload.parse_assessment(b'{"status": "made-up"}') is None

    def test_observation_ids_are_read_when_present_and_empty_when_not(self) -> None:
        assert _payload.parse_observation_ids(b'{"observationIds": ["a", "b"]}') == ("a", "b")
        assert _payload.parse_observation_ids(b"nonsense") == ()
        assert _payload.parse_observation_ids(b"{}") == ()


def _schemas() -> dict[str, JsonObject]:
    """The backend's own component schemas, read from the snapshot the wire was generated from."""
    if not SNAPSHOT.is_file():  # pragma: no cover - the snapshot is vendored in contract/
        pytest.skip(f"no backend snapshot at {SNAPSHOT}")
    document: JsonObject = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    components = document["components"]
    assert isinstance(components, dict)
    schemas = components["schemas"]
    assert isinstance(schemas, dict)
    return {name: node for name, node in schemas.items() if isinstance(node, dict)}


def _against(schema: JsonObject, body: JsonObject, *, where: str) -> None:
    """Every required property present, every property declared, and nothing extra — the backend's rule.

    `additionalProperties: false` is on every `*In` schema because `BaseSchema` forbids extras, so a
    field the SDK spells differently is not ignored on the wire: it is a 422.
    """
    assert schema.get("additionalProperties") is False, f"{where}: the backend does not forbid extras"
    properties = schema["properties"]
    assert isinstance(properties, dict)
    required = schema.get("required") or []
    assert isinstance(required, list)
    missing = [name for name in required if name not in body]
    assert missing == [], f"{where}: the body omits required {missing}"
    undeclared = [name for name in body if name not in properties]
    assert undeclared == [], f"{where}: the backend declares no {undeclared}"


class TestTheGeneratedVerifyIn:
    """A real body, through the generated model and then against the snapshot's own JSON schema."""

    def test_a_real_submission_validates_as_the_backends_verify_in(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds(assessment_json()))
        wrapped = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion()]), settings=settings)
        wrapped.chat.completions.create(model="gpt-fake-1", messages=[])
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            client.verify("refund-policy@1", {"orderId": "o-1"}, "reply", {"orderState": "paid"}, deadline_ms=4_000)
        body: JsonObject = recorder.bodies()[0]

        # 1. The generated model — the backend's own schema, as pydantic — accepts it unchanged.
        parsed = _wire.VerifyIn.model_validate(body)
        assert parsed.mode == "VERIFY"
        assert parsed.verifier == "refund-policy@1"
        assert parsed.idempotency_key == body["idempotencyKey"]
        assert parsed.sdk.name == "hajer-python"
        assert parsed.deadline_ms == body["deadlineMs"]
        assert parsed.wrapped_calls is not None
        assert len(parsed.wrapped_calls) == 1

        # 2. And the snapshot's JSON schema for the same operation admits exactly these keys.
        schemas = _schemas()
        _against(schemas["VerifyIn"], body, where="VerifyIn")
        sdk = body["sdk"]
        assert isinstance(sdk, dict)
        _against(schemas["SdkIdentityIn"], sdk, where="VerifyIn.sdk")
        calls = body["wrappedCalls"]
        assert isinstance(calls, list)
        first = calls[0]
        assert isinstance(first, dict)
        # `wrap()` records a *summary*: provider, api, model, settings, counts, usage and timing, and no
        # provider bytes unless content capture is on. That is the second member of the wire's union.
        _against(schemas["WrappedCallSummaryIn"], first, where="VerifyIn.wrappedCalls[0]")

    def test_a_raw_capturing_submission_validates_as_the_backends_capture_shape(self) -> None:
        """`HAJER_CAPTURE_RAW` sends the union's *first* member, and the backend's own schema admits it.

        This is the cross-side half of the wrapped-calls gap: the engine mints a `ModelCallReceipt` only
        from provider bytes, and this asserts that the bytes the SDK now sends are exactly the shape the
        snapshot declares for them — required keys present, no key the backend does not declare.
        """
        raw = hajer.HajerSettings(
            api_key="hjk_test", team_id="team-1", base_url="https://hajer.test", capture_content=True, capture_raw=True
        )
        recorder = Recorder(responds(assessment_json()))
        wrapped = hajer.wrap(FakeAnthropicClient(script=[FakeAnthropicMessage()]), settings=raw)
        wrapped.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": "hi"}], max_tokens=32)
        with hajer.Hajer(settings=raw, transport=recorder.transport()) as client:
            client.verify("refund-policy@1", {"orderId": "o-1"}, "reply", {"orderState": "paid"}, deadline_ms=4_000)
        body: JsonObject = recorder.bodies()[0]

        parsed = _wire.VerifyIn.model_validate(body)
        assert parsed.wrapped_calls is not None
        capture = parsed.wrapped_calls[0]
        assert isinstance(capture, _wire.WrappedCallCaptureIn)
        assert capture.wire == "ANTHROPIC_MESSAGES"
        assert capture.response_status == 200

        calls = body["wrappedCalls"]
        assert isinstance(calls, list)
        first = calls[0]
        assert isinstance(first, dict)
        _against(_schemas()["WrappedCallCaptureIn"], first, where="VerifyIn.wrappedCalls[0]")

    def test_the_pinned_verifier_is_what_the_backend_requires(self) -> None:
        """A verifier is `name@3`, `null`, or refused — and `null` means no verifier, not "latest"."""
        properties = _schemas()["VerifyIn"]["properties"]
        assert isinstance(properties, dict)
        verifier = properties["verifier"]
        assert isinstance(verifier, dict)
        # Two branches: the pinned string and `null`. There is no "latest" — the route refuses an
        # unpinned *name* with a 422 — and `null` is the attach-mode submission that names none at all, for
        # which nothing is verified.
        branches = verifier["anyOf"]
        assert isinstance(branches, list)
        patterns = [str(branch["pattern"]) for branch in branches if isinstance(branch, dict) and "pattern" in branch]
        assert len(patterns) == 1
        assert "@" in patterns[0]
        assert {"type": "null"} in branches

    def test_a_flush_validates_as_the_backends_observe_in(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds({"state": "accepted", "observationIds": ["ing-1"]}, status=202))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            client.observe("refund-policy@1", {"orderId": "o-1"}, "reply", {"orderState": "paid"})
            client.flush()
        flush: JsonObject = recorder.bodies()[0]

        parsed = _wire.ObserveIn.model_validate(flush)
        assert len(parsed.observations) == 1
        assert parsed.observations[0].mode == "OBSERVE"
        # An OBSERVE submission carries no deadline: it is off the response path.
        assert parsed.observations[0].deadline_ms is None
        schemas = _schemas()
        _against(schemas["ObserveIn"], flush, where="ObserveIn")
        submitted = flush["observations"]
        assert isinstance(submitted, list)
        one = submitted[0]
        assert isinstance(one, dict)
        _against(schemas["VerifyIn"], one, where="ObserveIn.observations[0]")

    def test_the_accepted_batch_reads_the_field_the_backend_declares(self) -> None:
        declared = _schemas()["ObserveAcceptedOut"]["properties"]
        assert isinstance(declared, dict)
        assert "observationIds" in declared
        assert _payload.parse_observation_ids(b'{"state": "accepted", "observationIds": ["ing-1"]}') == ("ing-1",)

    def test_the_assessment_the_sdk_returns_is_the_one_the_backend_serves(self) -> None:
        declared = _schemas()["AssessmentOut"]["properties"]
        assert isinstance(declared, dict)
        # The two keys the SDK renames through its alias layer, and nothing else may drift.
        assert "checkOutcomes" in declared
        assert "unavailableReason" in declared
        served: JsonValue = {
            "status": "unavailable",
            "unavailableReason": "SHADOW",
            "shadow": True,
            "verifier": "refund-policy@1",
            "observationId": "ing-1",
            "checkOutcomes": [],
            "findings": [],
            "missingEvidence": [],
            "costMicrousd": 0,
            "latencyMs": 12,
            "seam": "no verifier ran",
            "limitations": ["The client redacted 1 value(s) before sending, under catalog redaction-rules@6."],
        }
        generated = _wire.AssessmentOut.model_validate(served)
        assessment = _payload.parse_assessment(json.dumps(served).encode("utf-8"))
        assert assessment is not None
        assert assessment.status == generated.status
        assert assessment.reason == generated.unavailable_reason
        assert assessment.per_check == ()
        assert assessment.verifier == generated.verifier

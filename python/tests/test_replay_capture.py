"""What a replayed attempt sends to `verify`, and which answers bind its observation.

The child sends the reply's check subject and the capture evidence the generated verifier's contract reads
(`provider_decode`, `application_result`, `capture_layer`, `subject_digest` when decoded), beside the replay
marker. A SHADOW verifier's `unavailable` still names an observation that was captured and assessed, so it is
kept; any other `unavailable` leaves the capture unbound.
"""

from __future__ import annotations

import hashlib
import json
from types import TracebackType
from typing import ClassVar

import pytest
from pydantic import JsonValue

from hajer._models import Assessment
from hajer.replay import _case
from hajer.replay._capture import captured, subject_of

OBSERVATION = "ing-" + "c3" * 16
SPEC: dict[str, JsonValue] = {
    "runId": "replay-run-1",
    "planId": "plan-1",
    "caseId": "c1",
    "attempt": 1,
    "workflowId": "classify",
    "revisionLabel": "candidate",
    "input": {"text": "hello"},
    "adapter": {"verifier": "classify@2"},
    "hajer": {"baseUrl": "http://127.0.0.1:8000", "teamId": "team-1"},
}


def anthropic(text: str, stop: str = "end_turn") -> bytes:
    return json.dumps({"content": [{"type": "text", "text": text}], "stop_reason": stop}).encode()


def test_the_last_reply_decodes_strictly_into_the_check_subject() -> None:
    assert subject_of([anthropic("nope"), anthropic('{"label": "positive"}')]) == ("DECODED", {"label": "positive"})
    assert subject_of([anthropic('["x"]')]) == ("NOT_OBJECT", None)
    assert subject_of([anthropic('{"a": 1, "a": 2}')]) == ("DUPLICATE_KEYS", None)
    assert subject_of([anthropic('{"a": NaN}')]) == ("NON_FINITE", None)
    assert subject_of([anthropic('{"a": 1}', "max_tokens")]) == ("TRUNCATED", None)
    assert subject_of([b"not json"]) == ("NO_CAPTURE", None)
    assert subject_of([]) == ("NO_CAPTURE", None)
    openai = json.dumps({"choices": [{"message": {"content": '{"label": "x"}'}, "finish_reason": "stop"}]})
    assert subject_of([openai.encode()]) == ("DECODED", {"label": "x"})


def test_the_evidence_is_the_generated_verifiers_contract() -> None:
    subject, evidence = captured([anthropic('{"label": "positive"}')], {"label": "POSITIVE"})
    canonical = json.dumps(subject, sort_keys=True, separators=(",", ":")).encode()
    assert subject == {"label": "positive"}
    assert evidence == {
        "provider_decode": "DECODED",
        "application_result": {"label": "POSITIVE"},
        "capture_layer": "HTTP_TRANSPORT_DECODED_BODY",
        "subject_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
    }
    undecoded, why = captured([anthropic("plain words")], "plain words")
    assert undecoded is None
    assert why["provider_decode"] == "NOT_JSON"
    assert why["application_result"] == {"value": "plain words"}
    assert "subject_digest" not in why


class FakeHajer:
    sent: ClassVar[list[tuple[str, JsonValue, JsonValue, dict[str, JsonValue]]]] = []
    answer: ClassVar[Assessment]

    def __init__(self, **_: object) -> None:
        pass

    def __enter__(self) -> FakeHajer:
        return self

    def __exit__(self, *_: type[BaseException] | BaseException | TracebackType | None) -> None:
        return None

    def verify(
        self, verifier: str, request: JsonValue, output: JsonValue, evidence: dict[str, JsonValue], **_: object
    ) -> Assessment:
        FakeHajer.sent.append((verifier, request, output, evidence))
        return FakeHajer.answer


def _answer(status: str, reason: str | None) -> Assessment:
    return Assessment.model_validate({"status": status, "unavailableReason": reason, "observationId": OBSERVATION})


@pytest.mark.parametrize(
    ("status", "reason", "kept"),
    [
        ("unavailable", "SHADOW", OBSERVATION),
        ("unavailable", "VERIFIER_UNKNOWN", None),
        ("unavailable", "DEADLINE", None),
        ("unavailable", "INTERNAL", None),
        ("satisfied", None, OBSERVATION),
    ],
)
def test_only_a_shadow_unavailable_keeps_its_observation(
    status: str, reason: str | None, kept: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeHajer.sent, FakeHajer.answer = [], _answer(status, reason)
    monkeypatch.setattr(_case, "Hajer", FakeHajer)
    observation, _, sent = _case._verify(SPEC, {"label": "POSITIVE"}, "key", [anthropic('{"label": "positive"}')])  # pyright: ignore[reportPrivateUsage]
    assert (observation, sent) == (kept, True)
    ((verifier, request, output, evidence),) = FakeHajer.sent
    assert (verifier, request, output) == ("classify@2", {"text": "hello"}, {"label": "positive"})
    assert evidence["provider_decode"] == "DECODED"
    assert evidence["hajerReplay"] == {"runId": "replay-run-1", "planId": "plan-1", "caseId": "c1", "attempt": 1}

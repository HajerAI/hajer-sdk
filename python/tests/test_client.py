"""What the two clients put on the wire for the keywords a caller passes them.

`tests/test_payload.py` owns the body's shape and the idempotency derivation; this file owns the keywords
`verify` and `observe` accept and what each one becomes on the wire. Asserted on the wire rather than on
internals, because the wire is what the behaviour is about.
"""

from __future__ import annotations

from typing import cast

import hajer
from hajer._case_key import derive_case_key
from hajer._json import JsonObject, JsonValue
from hajer._redact import build_policy
from tests.conftest import Recorder, assessment_json, responds
from tests.fakes import FakeChatCompletion, FakeOpenAI


def test_case_key_keyword_maps_to_caseKey(settings: hajer.HajerSettings) -> None:  # noqa: N802 - the wire key is camelCase
    """`case_key=` is `caseKey` on the wire, labelled `CALLER`; absent, the SDK derives and says so.

    Both halves matter. A caller who names their own scenario is the strongest source of a case identity
    and the row records that; a caller who names none still gets a *stable* one, because the SDK derives it
    here — before client redaction can move the request bytes — rather than leaving ingest to derive it from
    whatever arrived.
    """
    recorder = Recorder(responds(assessment_json()))
    with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
        client.verify("refund-policy@1", {"orderId": "o-1"}, "reply", case_key="case-alpha")
        client.verify("refund-policy@1", {"orderId": "o-1"}, "reply")
        client.observe("refund-policy@1", {"orderId": "o-2"}, "reply", case_key="case-beta")
        client.flush()

    named, derived = recorder.bodies()[:2]
    assert (named["caseKey"], named["caseKeySource"]) == ("case-alpha", "CALLER")
    assert (derived["caseKey"], derived["caseKeySource"]) == (derive_case_key({"orderId": "o-1"}), "DERIVED_CLIENT")
    # `observe` is queued, so its submission arrives inside a flush envelope rather than on its own.
    (observed,) = flushed(recorder)
    assert (observed["caseKey"], observed["caseKeySource"]) == ("case-beta", "CALLER")


def test_two_attempts_at_one_request_carry_one_case_key(settings: hajer.HajerSettings) -> None:
    """Two submissions, two idempotency keys, one case: the pair repeatability needs to see."""
    recorder = Recorder(responds(assessment_json()))
    with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
        client.verify("refund-policy@1", {"orderId": "o-1"}, "first reply")
        client.verify("refund-policy@1", {"orderId": "o-1"}, "second reply")

    first, second = recorder.bodies()
    assert first["idempotencyKey"] != second["idempotencyKey"]
    assert first["caseKey"] == second["caseKey"]


async def test_the_async_client_sends_the_same_two_keys(settings: hajer.HajerSettings) -> None:
    """`AsyncHajer` is the same wire, which is the only thing that keeps the two clients one product."""
    recorder = Recorder(responds(assessment_json()))
    async with hajer.AsyncHajer(settings=settings, transport=recorder.transport()) as client:
        await client.verify("refund-policy@1", {"orderId": "o-1"}, "reply", case_key="case-alpha")
        await client.verify("refund-policy@1", {"orderId": "o-1"}, "reply")

    named, derived = recorder.bodies()
    assert (named["caseKey"], named["caseKeySource"]) == ("case-alpha", "CALLER")
    assert (derived["caseKey"], derived["caseKeySource"]) == (derive_case_key({"orderId": "o-1"}), "DERIVED_CLIENT")


def test_an_attached_call_groups_by_what_it_asked_for_and_not_by_the_clock(settings: hajer.HajerSettings) -> None:
    """Two attached calls of one shape are one case: `startedAt` is declared volatile, everything else is not.

    Without that, every attached observation would be its own case and the column would hold one singleton
    group per provider call — which is the same failure the case key exists to fix, with more rows.
    """
    recorder = Recorder(responds(assessment_json()))
    with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
        wrapped = hajer.wrap(FakeOpenAI(chat_script=[FakeChatCompletion(), FakeChatCompletion()]), settings=settings)
        wrapped.chat.completions.create(model="gpt-fake-1", messages=[])
        wrapped.chat.completions.create(model="gpt-fake-1", messages=[])
        for call in hajer.wrapped_calls():
            client.observe_call(call)
        client.flush()

    submitted = flushed(recorder)
    assert len(submitted) == 2
    assert len({str(observation["caseKey"]) for observation in submitted}) == 1
    assert {str(observation["caseKeySource"]) for observation in submitted} == {"DERIVED_CLIENT"}


def flushed(recorder: Recorder) -> list[JsonObject]:
    """Every observation the queue actually sent, in order, unwrapped from its flush envelopes."""
    return [
        observation
        for body in recorder.bodies()
        if isinstance(body.get("observations"), list)
        for observation in cast("list[JsonValue]", body["observations"])
        if isinstance(observation, dict)
    ]


def test_per_call_policy_overrides_process_default(settings: hajer.HajerSettings) -> None:
    """One endpoint may need a class the rest of the process removes, and says so at that call.

    The process default is built once, at construction, so a broken pattern is a start-up error rather
    than a degraded pass somebody reads about in an assessment; `policy=` on the call is the narrower
    decision, about that submission and nothing else.
    """
    card = "4111 1111 1111 1111"
    process = build_policy(paths_exempt=("request.account",))
    recorder = Recorder(responds(assessment_json()))
    with hajer.Hajer(settings=settings, transport=recorder.transport(), policy=process) as client:
        client.verify("refund-policy@1", {"account": card}, "ok")
        client.verify("refund-policy@1", {"account": card}, "ok", policy=build_policy())

    exempted, redacted = recorder.bodies()
    assert cast("JsonObject", exempted["request"])["account"] == card
    assert "clientRedaction" not in exempted
    assert cast("JsonObject", redacted["request"])["account"] == "[redacted:CARD]"
    assert "clientRedaction" in redacted

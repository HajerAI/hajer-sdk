"""The fixture app end to end: two model calls, one verify, and the decision the assessment caused.

Nothing here touches a network. The provider is a fake client `wrap` instruments by attribute; the
Hajer service is an `httpx.MockTransport` that reads what arrived and answers an assessment.
"""

from __future__ import annotations

import hajer
from hajer._json import JsonObject
from tests.conftest import Recorder, assessment_json, responds
from tests.fakes import FakeChatCompletion, FakeChoice, FakeMessage, FakeOpenAI
from tests.fixture_app import Order, Outbox, Ticket, handle_ticket

POLICY = "Refunds are approved only for orders in state `refunded` or within 30 days of delivery."


def ticket() -> Ticket:
    return Ticket(
        id="t-100",
        question="Where is my refund?",
        order=Order(id="o-1", state="paid", total_cents=4_999, refund_policy=POLICY),
    )


def model_client(reply: str) -> FakeOpenAI:
    """A fake OpenAI client scripted with the classification and then the draft."""
    return FakeOpenAI(
        chat_script=[
            FakeChatCompletion(
                id="chatcmpl-classify",
                model="support-classifier",
                choices=[FakeChoice(message=FakeMessage(content="refund_status"))],
            ),
            FakeChatCompletion(
                id="chatcmpl-draft",
                model="support-writer",
                choices=[FakeChoice(message=FakeMessage(content=reply))],
            ),
        ]
    )


VIOLATED = assessment_json(
    status="violated",
    findings=[
        {
            "obligation": "no-refund-promise-outside-policy@2",
            "check": "refund-consistency@1",
            "statement": "the reply promises a refund the supplied policy and order state do not allow",
            "evidenceUsed": ["approvedPolicy", "orderState"],
        }
    ],
    checkOutcomes=[
        {
            "check": "refund-consistency@1",
            "reason": "violated",
            "detail": "order state is `paid`",
            "evidenceUsed": ["orderState"],
        }
    ],
)


class TestEndToEnd:
    def test_a_satisfied_assessment_sends_and_the_two_model_calls_are_attached(
        self, settings: hajer.HajerSettings
    ) -> None:
        recorder = Recorder(responds(assessment_json()))
        outbox = Outbox()
        provider = hajer.wrap(model_client("Your order is on its way."), settings=settings)
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            outcome = handle_ticket(ticket(), model_client=provider, verifier_client=client, outbox=outbox)

        assert outcome.action == "sent"
        assert outbox.sent == ["Your order is on its way."]
        assert outbox.held == []
        assert outcome.assessment.status == "satisfied"

        body: JsonObject = recorder.bodies()[0]
        calls = body["wrappedCalls"]
        assert isinstance(calls, list)
        assert len(calls) == 2, "wrap attached both model calls to the verify that followed them"
        first, second = calls
        assert isinstance(first, dict)
        assert isinstance(second, dict)
        assert first["model"] == "support-classifier"
        assert second["model"] == "support-writer"
        assert first["provider"] == "openai"
        assert first["api"] == "chat.completions"
        assert "Where is my refund?" in str(first["content"]), "message content is captured by default"

    def test_the_evidence_is_the_named_fields_and_nothing_else(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds(assessment_json()))
        provider = hajer.wrap(model_client("On its way."), settings=settings)
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            handle_ticket(ticket(), model_client=provider, verifier_client=client, outbox=Outbox())
        body = recorder.bodies()[0]
        assert body["evidence"] == {
            "orderId": "o-1",
            "orderState": "paid",
            "orderTotalCents": 4_999,
            "approvedPolicy": POLICY,
        }
        assert body["request"] == {"ticketId": "t-100", "question": "Where is my refund?", "intent": "refund_status"}
        assert body["output"] == "On its way."
        assert body["idempotencyKey"] == "ticket-t-100"

    def test_a_violated_assessment_holds_the_reply(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds(VIOLATED))
        outbox = Outbox()
        provider = hajer.wrap(model_client("We have refunded you in full."), settings=settings)
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            outcome = handle_ticket(ticket(), model_client=provider, verifier_client=client, outbox=outbox)

        assert outcome.action == "held"
        assert outbox.sent == []
        assert outbox.held == ["We have refunded you in full."]
        finding = outcome.assessment.findings[0]
        assert finding.obligation == "no-refund-promise-outside-policy@2"
        assert finding.evidence_used == ("approvedPolicy", "orderState")
        assert outcome.assessment.per_check[0].reason == "violated"

    def test_a_shadow_assessment_is_unavailable_and_never_gates(self, settings: hajer.HajerSettings) -> None:
        """A verifier that has not qualified tells the application nothing it could act on."""
        recorder = Recorder(responds(assessment_json(status="unavailable", unavailableReason="SHADOW", shadow=True)))
        outbox = Outbox()
        provider = hajer.wrap(model_client("We have refunded you in full."), settings=settings)
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            outcome = handle_ticket(ticket(), model_client=provider, verifier_client=client, outbox=outbox)
        assert outcome.assessment.status == "unavailable"
        assert outcome.assessment.reason == "SHADOW"
        assert outcome.assessment.shadow is True
        assert outcome.assessment.decided_locally is False
        assert outcome.action == "sent", "the application's own policy, unchanged by a shadow verifier"

    def test_the_app_runs_unchanged_with_no_credentials_and_opens_no_socket(self) -> None:
        """The whole point of inert mode: a customer's suite passes before they have a key."""
        inert = hajer.HajerSettings.from_env({})
        recorder = Recorder(responds(assessment_json()))
        outbox = Outbox()
        provider = hajer.wrap(model_client("On its way."), settings=inert)
        with hajer.Hajer(settings=inert, transport=recorder.transport()) as client:
            outcome = handle_ticket(ticket(), model_client=provider, verifier_client=client, outbox=outbox)
        assert outcome.action == "sent"
        assert outbox.sent == ["On its way."]
        assert outcome.assessment.reason == "DISABLED"
        assert recorder.requests == [], "nothing left the process"

    def test_the_context_is_emptied_so_the_next_ticket_starts_clean(self, settings: hajer.HajerSettings) -> None:
        recorder = Recorder(responds(assessment_json()))
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            for index in range(2):
                provider = hajer.wrap(model_client(f"reply {index}"), settings=settings)
                handle_ticket(ticket(), model_client=provider, verifier_client=client, outbox=Outbox())
        for body in recorder.bodies():
            calls = body["wrappedCalls"]
            assert isinstance(calls, list)
            assert len(calls) == 2, "each verify carries its own ticket's calls, never the previous one's"
        assert hajer.wrapped_calls() == ()

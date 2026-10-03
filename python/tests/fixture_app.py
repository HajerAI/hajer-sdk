"""The fixture application: the five lines, in the shape a customer's own code has them.

`handle_ticket` is a support-reply workflow with a sink. It calls a model twice — once to classify
the ticket, once to draft the reply — and then, at the point where the draft crosses into a side
effect (`send`), it hands Hajer the request, the output and the evidence it chose, and lets the
assessment decide. That last part is the customer's policy, not the SDK's: `verify` answers, the
application acts.

This is the shape Hajer proposes as a pull request — `wrap` at
client construction, explicit evidence fields (never `order.to_dict()`), the final payload after
transformation and before the effect — and it is written by hand here, which is the other path
that stays open: a developer who writes the five lines gets the same behaviour with no PR.

It runs unchanged with no Hajer credentials: the client is inert, `verify` answers
`unavailable{DISABLED}`, the ticket is sent, and the fixture's own tests pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import hajer
from hajer._json import JsonValue


@dataclass
class Order:
    """The application's own record. Only named fields ever reach the evidence."""

    id: str
    state: str
    total_cents: int
    refund_policy: str


@dataclass
class Ticket:
    id: str
    question: str
    order: Order


@dataclass
class Outbox:
    """The sink. What lands here is what the customer's user receives."""

    sent: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)

    def send(self, body: str) -> None:
        self.sent.append(body)

    def hold(self, body: str) -> None:
        self.held.append(body)


@dataclass
class TicketOutcome:
    """What `handle_ticket` decided, so a test can read the decision and the reason for it."""

    action: str
    reply: str
    assessment: hajer.Assessment


def handle_ticket(
    ticket: Ticket,
    *,
    model_client: object,
    verifier_client: hajer.Hajer,
    outbox: Outbox,
) -> TicketOutcome:
    """Classify, draft, verify, then send — or hold, because the assessment said to."""
    chat = _chat(model_client)

    classification = chat(
        model="support-classifier",
        messages=[{"role": "user", "content": ticket.question}],
        temperature=0.0,
    )
    intent = _text(classification) or "unknown"

    drafted = chat(
        model="support-writer",
        messages=[
            {"role": "system", "content": f"The order is {ticket.order.state}. Intent: {intent}."},
            {"role": "user", "content": ticket.question},
        ],
        temperature=0.3,
    )
    reply = _text(drafted) or ""

    # The boundary. Explicit fields, the final payload, before the effect.
    request: JsonValue = {"ticketId": ticket.id, "question": ticket.question, "intent": intent}
    evidence: dict[str, JsonValue] = {
        "orderId": ticket.order.id,
        "orderState": ticket.order.state,
        "orderTotalCents": ticket.order.total_cents,
        "approvedPolicy": ticket.order.refund_policy,
    }
    assessment = verifier_client.verify(
        # Pinned: `@1` is a compiled version the product showed you, and there is no "latest" — a
        # verifier is that version or it is unknown, and the ingest route refuses an unpinned name.
        "refund-policy@1",
        request,
        reply,
        evidence,
        idempotency_key=f"ticket-{ticket.id}",
    )

    if assessment.status == "violated":
        outbox.hold(reply)
        return TicketOutcome(action="held", reply=reply, assessment=assessment)

    outbox.send(reply)
    return TicketOutcome(action="sent", reply=reply, assessment=assessment)


def _chat(model_client: object) -> Callable[..., object]:
    """The provider's `chat.completions.create`, whatever client the caller handed in."""
    chat = getattr(model_client, "chat", None)
    completions = getattr(chat, "completions", None)
    create = getattr(completions, "create", None)
    if not callable(create):
        raise TypeError("the fixture app needs a client with chat.completions.create")
    return create


def _text(response: object) -> str | None:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list):
        return None
    items = cast(list[object], choices)
    if not items:
        return None
    message = getattr(items[0], "message", None)
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else None

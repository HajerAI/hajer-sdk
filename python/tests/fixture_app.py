"""The fixture application: the shape a customer's own code has once the SDK is in it.

`handle_ticket` is a support-reply workflow. It calls a model twice — once to classify the ticket, once to
draft the reply — inside one declared workflow and the ticket's conversation, and sends the draft. The SDK's
part is the three lines a customer writes: `hajer.wrap(client)` where the client is built,
`@hajer.workflow(...)` on the handler, and `hajer.session(...)` around the turn. Everything else is the
application's own.

It runs unchanged with no Hajer credentials: the calls are still recorded locally, nothing leaves the
process, the ticket is sent, and the fixture's own tests pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import hajer


@dataclass
class Order:
    """The application's own record."""

    id: str
    state: str


@dataclass
class Ticket:
    id: str
    question: str
    order: Order
    #: The conversation this ticket is one turn of: what the platform groups the turn's trace under.
    conversation_id: str = "conv-1"


@dataclass
class Outbox:
    """The sink. What lands here is what the customer's user receives."""

    sent: list[str] = field(default_factory=list)

    def send(self, body: str) -> None:
        self.sent.append(body)


@dataclass
class TicketOutcome:
    """What `handle_ticket` did, so a test can read the reply and the intent that shaped it."""

    intent: str
    reply: str


@hajer.workflow("wf_support")
def handle_ticket(ticket: Ticket, *, model_client: object, outbox: Outbox) -> TicketOutcome:
    """Classify, draft, send — one turn of the ticket's conversation."""
    with hajer.session(ticket.conversation_id):
        return _answer(ticket, model_client=model_client, outbox=outbox)


def _answer(ticket: Ticket, *, model_client: object, outbox: Outbox) -> TicketOutcome:
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
    outbox.send(reply)
    return TicketOutcome(intent=intent, reply=reply)


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

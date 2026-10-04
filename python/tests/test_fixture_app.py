"""The fixture app end to end: two model calls inside one workflow and one session, as the spans say, and the reply sent.

Nothing here touches a network. The provider is a fake client `wrap` instruments by attribute; the spans land in
an in-memory exporter.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, cast

import pytest

import hajer
from hajer import _wrap
from tests.conftest import Emitting
from tests.fakes import FakeChatCompletion, FakeChoice, FakeMessage, FakeOpenAI
from tests.fixture_app import Order, Outbox, Ticket, handle_ticket

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan


class _Settled:
    """The call observer: what the span emitter is handed, one record per settled call."""

    def __init__(self) -> None:
        self.calls: list[hajer.WrappedCall] = []

    def opened(self, call: hajer.WrappedCall) -> object:
        return None

    def settled(self, call: hajer.WrappedCall, handle: object | None) -> None:
        self.calls.append(call)


@pytest.fixture
def settled() -> Iterator[_Settled]:
    observer = _Settled()
    previous = _wrap.call_observer()
    _wrap.set_call_observer(observer)
    yield observer
    _wrap.set_call_observer(previous)


def ticket() -> Ticket:
    return Ticket(id="t-100", question="Where is my refund?", order=Order(id="o-1", state="paid"))


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


class TestEndToEnd:
    def test_the_two_model_calls_are_recorded_under_the_workflow(
        self, settings: hajer.HajerSettings, settled: _Settled
    ) -> None:
        outbox = Outbox()
        provider = hajer.wrap(model_client("Your order is on its way."), settings=settings)
        outcome = handle_ticket(ticket(), model_client=provider, outbox=outbox)

        assert outcome.intent == "refund_status"
        assert outbox.sent == ["Your order is on its way."]
        first, second = settled.calls
        assert (first.model, second.model) == ("support-classifier", "support-writer")
        assert (first.provider, first.api) == ("openai", "chat.completions")
        assert first.content is not None
        assert "Where is my refund?" in str(first.content["messages"]), "message content is captured by default"
        assert (first.workflow_hint, second.workflow_hint) == ("wf_support", "wf_support")

    def test_the_app_runs_unchanged_with_no_credentials(self) -> None:
        """The whole point of inert mode: a customer's suite passes before they have a key."""
        inert = hajer.HajerSettings.from_env({})
        assert inert.inert is True
        outbox = Outbox()
        provider = hajer.wrap(model_client("On its way."), settings=inert)
        outcome = handle_ticket(ticket(), model_client=provider, outbox=outbox)
        assert outcome.reply == "On its way."
        assert outbox.sent == ["On its way."]

    def test_the_turn_is_one_trace_in_one_conversation(self, settings: hajer.HajerSettings, emitting: Emitting) -> None:
        exporter = emitting(settings)
        provider = hajer.wrap(model_client("On its way."), settings=settings)
        handle_ticket(ticket(), model_client=provider, outbox=Outbox())
        finished = cast("Sequence[ReadableSpan]", exporter.get_finished_spans())
        spans = {span.name: span for span in finished}
        assert set(spans) == {"workflow wf_support", "chat support-classifier", "chat support-writer"}
        workflow = spans["workflow wf_support"]
        assert workflow.context is not None
        for name in ("chat support-classifier", "chat support-writer"):
            chat = spans[name]
            attributes = dict(chat.attributes or {})
            assert attributes["session.id"] == "conv-1"
            assert attributes["hajer.workflow.id"] == "wf_support"
            assert chat.parent is not None
            assert chat.parent.span_id == workflow.context.span_id

    def test_the_workflow_owns_its_calls_and_leaves_the_task_clean(
        self, settings: hajer.HajerSettings, settled: _Settled
    ) -> None:
        for index in range(2):
            provider = hajer.wrap(model_client(f"reply {index}"), settings=settings)
            handle_ticket(ticket(), model_client=provider, outbox=Outbox())
        assert len(settled.calls) == 4
        assert hajer.wrapped_calls() == (), "the workflow's scope took them; nothing leaks onto the task"

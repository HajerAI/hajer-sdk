"""`hajer.wrap` composes with existing tracing: the install PR wraps a client that Langfuse's
OpenAI drop-in built, inside a function `@observe()` traces, and neither side loses the call or changes the answer."""

from __future__ import annotations

import hajer
from tests import langfuse_shape
from tests.fakes import FakeChatCompletion

QUIET = hajer.HajerSettings(capture_content=False)
MESSAGES = [{"role": "user", "content": "Where is my order?"}]


def _answer(client: langfuse_shape.OpenAI) -> object:
    @langfuse_shape.observe()
    def answer() -> object:
        return client.chat.completions.create(model="gpt-fake-1", messages=MESSAGES)

    return answer()


class TestWrapBesideLangfuse:
    def test_the_wrapped_drop_in_returns_the_same_response_and_both_record_the_call(self) -> None:
        langfuse_shape.RECORDED.clear()
        unwrapped = FakeChatCompletion(id="chatcmpl-plain")
        assert _answer(langfuse_shape.OpenAI(chat_script=[unwrapped])) is unwrapped
        assert not hajer.wrapped_calls()
        langfuse_shape.RECORDED.clear()

        expected = FakeChatCompletion(id="chatcmpl-traced")
        client = hajer.wrap(langfuse_shape.OpenAI(chat_script=[expected]), settings=QUIET)
        assert _answer(client) is expected
        # Hajer recorded the call...
        (call,) = hajer.wrapped_calls()
        assert (call.model, call.message_count, call.response_id) == ("gpt-fake-1", 1, "chatcmpl-traced")
        # ...and Langfuse's trace and generation are exactly what they are without the wrap.
        assert langfuse_shape.RECORDED == [
            {"trace": "answer"},
            {"generation": "gpt-fake-1", "trace": "answer", "response": expected},
        ]
        assert client.completions.calls == [{"model": "gpt-fake-1", "messages": MESSAGES}]

    def test_wrapping_the_drop_in_twice_still_records_once(self) -> None:
        langfuse_shape.RECORDED.clear()
        client = hajer.wrap(hajer.wrap(langfuse_shape.OpenAI(), settings=QUIET), settings=QUIET)
        _answer(client)
        assert len(hajer.wrapped_calls()) == 1
        assert [item for item in langfuse_shape.RECORDED if "generation" in item] != []

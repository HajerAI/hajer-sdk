"""Every constructor in `hajer/_supported.py` builds a client `wrap()` captures: a fake of each shape is wrapped and
records a call. The install planner wraps exactly this list (the backend's drift guard), so an entry here that `wrap()`
refused would take a customer's app down at import."""

from __future__ import annotations

import hajer
from hajer._supported import WRAP_CONSTRUCTORS
from tests import langfuse_shape
from tests.fakes import FakeAnthropic, FakeAsyncAnthropic, FakeAsyncOpenAI, FakeChatAnthropic, FakeOpenAI

QUIET = hajer.HajerSettings(capture_content=False)
SHAPES: dict[str, type[object]] = {
    "openai.OpenAI": FakeOpenAI,
    "openai.AsyncOpenAI": FakeAsyncOpenAI,
    "openai.AzureOpenAI": FakeOpenAI,
    "openai.AsyncAzureOpenAI": FakeAsyncOpenAI,
    "anthropic.Anthropic": FakeAnthropic,
    "anthropic.AsyncAnthropic": FakeAsyncAnthropic,
    "langchain_anthropic.ChatAnthropic": FakeChatAnthropic,
    "langfuse.openai.OpenAI": langfuse_shape.OpenAI,
    "langfuse.openai.AsyncOpenAI": FakeAsyncOpenAI,
    "langfuse.openai.AzureOpenAI": langfuse_shape.OpenAI,
    "langfuse.openai.AsyncAzureOpenAI": FakeAsyncOpenAI,
    "langfuse.openai.openai.OpenAI": langfuse_shape.OpenAI,
    "langfuse.openai.openai.AsyncOpenAI": FakeAsyncOpenAI,
    "langfuse.openai.openai.AzureOpenAI": langfuse_shape.OpenAI,
    "langfuse.openai.openai.AsyncAzureOpenAI": FakeAsyncOpenAI,
}
# `langchain_openai.ChatOpenAI` needs the installed `openai` (tests/langchain_shape.py, `test_langchain_capture.py`).
COVERED_ELSEWHERE = frozenset({"langchain_openai.ChatOpenAI"})


def test_every_supported_constructor_has_a_shape_wrap_accepts() -> None:
    assert set(SHAPES) | COVERED_ELSEWHERE == WRAP_CONSTRUCTORS
    for name, shape in SHAPES.items():
        client = shape()
        assert hajer.wrap(client, settings=QUIET) is client, name

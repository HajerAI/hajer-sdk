"""Langfuse's OpenAI drop-in and its `observe` decorator, as their shapes reach `hajer.wrap`, in memory.

`from langfuse.openai import OpenAI` hands the application the provider's own client class with its
`chat.completions.create` patched to record a generation under the current trace, and `@observe()` opens that trace
around the decorated function. This stub keeps both behaviours and nothing else: no langfuse dependency, no network.
The install PR wraps such a client at its construction site (`hajer.wrap(OpenAI())`), beside the tracing.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from contextvars import ContextVar
from typing import ParamSpec, TypeVar

from tests.fakes import FakeOpenAI, SyncEndpoint

#: Every trace opened and generation recorded, in order: what Langfuse would have sent.
RECORDED: list[dict[str, object]] = []
_TRACE: ContextVar[str | None] = ContextVar("langfuse_shape_trace", default=None)


P = ParamSpec("P")
R = TypeVar("R")


def observe() -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(function)
        def traced(*args: P.args, **kwargs: P.kwargs) -> R:
            RECORDED.append({"trace": function.__name__})
            token = _TRACE.set(function.__name__)
            try:
                return function(*args, **kwargs)
            finally:
                _TRACE.reset(token)

        return traced

    return decorate


class _TracedCompletions(SyncEndpoint):
    """The integration's patch: the provider call runs unchanged, then its generation is recorded."""

    def create(self, **kwargs: object) -> object:
        response = super().create(**kwargs)
        RECORDED.append({"generation": kwargs.get("model"), "trace": _TRACE.get(), "response": response})
        return response


class _Chat:
    def __init__(self, completions: SyncEndpoint) -> None:
        self.completions = completions


class OpenAI(FakeOpenAI):
    """The drop-in client: the provider's client with the integration's patch on `chat.completions.create`."""

    def __init__(self, *, chat_script: list[object] | None = None) -> None:
        super().__init__(chat_script=chat_script)
        self.completions = _TracedCompletions(chat_script)
        self.chat = _Chat(self.completions)

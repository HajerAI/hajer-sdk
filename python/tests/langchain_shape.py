"""`langchain_openai.ChatOpenAI`'s client shape and call path over the installed `openai`, in memory.

It holds `root_client` / `root_async_client` (an `openai.OpenAI` / `AsyncOpenAI`) and `client` /
`async_client` (their `chat.completions`), calls `with_raw_response.create(...)` then `.parse()`, sends
structured output through `chat.completions.with_raw_response.parse(...)`, and runs every `ainvoke` in an
`asyncio.gather` child task; a structured chain adds a `create_task(..., context=...)` per step. Tests treat
this module as an installed package, as `langchain_openai` is, so its frames are never the application's.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable, Coroutine
from typing import cast

import httpx
import pytest

from hajer._json import JsonObject

CHAT: JsonObject = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-5.1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"name": "x"}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def _handler(request: httpx.Request) -> httpx.Response:
    del request
    return httpx.Response(200, json=CHAT)


class ChatModel:
    """`ChatOpenAI`'s client shape and call path, not its API: enough to be the same seam."""

    def __init__(self) -> None:
        openai = pytest.importorskip("openai")
        make = cast(Callable[..., object], openai.OpenAI)
        make_async = cast(Callable[..., object], openai.AsyncOpenAI)
        self.root_client = make(
            api_key="fake", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(_handler))
        )
        self.root_async_client = make_async(
            api_key="fake", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        )
        self.client = path(self.root_client, "chat.completions")
        self.async_client = path(self.root_async_client, "chat.completions")

    async def ainvoke(self, text: str) -> object:
        # BaseChatModel.agenerate: one child task per prompt, gathered.
        (answer,) = await asyncio.gather(self._agenerate(text))
        return answer

    async def _agenerate(self, text: str) -> object:
        raw = await cast(Awaitable[object], path(self.async_client, "with_raw_response.create")(**_payload(text)))
        return path(raw, "parse")()

    async def astructured(self, text: str) -> object:
        # RunnableSequence.ainvoke: each step in its own task, with an explicit context.
        return await asyncio.get_running_loop().create_task(self._aparse(text), context=contextvars.copy_context())

    async def _aparse(self, text: str) -> object:
        (answer,) = await asyncio.gather(self._aparse_generate(text))
        return answer

    async def _aparse_generate(self, text: str) -> object:
        resource = path(self.root_async_client, "chat.completions.with_raw_response.parse")
        payload = _payload(text)
        payload.pop("stream")  # `_generate`: a `response_format` request goes to `parse`, which takes no stream
        raw = await cast(Awaitable[object], resource(**payload, response_format={"type": "json_object"}))
        return path(raw, "parse")()

    def invoke(self, text: str) -> object:
        raw = path(self.client, "with_raw_response.create")(**_payload(text))
        return path(raw, "parse")()


def path(value: object, dotted: str) -> Callable[..., object]:
    """The attribute at a dotted path, as the callable it is."""
    for part in dotted.split("."):
        value = getattr(value, part)
    return cast(Callable[..., object], value)


def _payload(text: str) -> dict[str, object]:
    return {"model": "gpt-5.1", "messages": [{"role": "user", "content": text}], "stream": False}


class ForeignLoop(asyncio.SelectorEventLoop):
    """A loop whose `create_task` is its own, as uvloop's (Cython) is: it never runs `BaseEventLoop.create_task`,
    and honours a task factory exactly the way uvloop 0.22 does. Here, beside the framework, because it stands
    for an installed package too."""

    def create_task(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, coro: Coroutine[object, object, object], *, name: str | None = None, context: object = None
    ) -> asyncio.Task[object]:
        factory = self.get_task_factory()
        if factory is None:
            task = asyncio.Task(coro, loop=self, context=cast("contextvars.Context | None", context))
        elif context is None:
            task = cast(asyncio.Task[object], factory(self, coro))
        else:
            task = cast(asyncio.Task[object], cast(Callable[..., object], factory)(self, coro, context=context))
        if name is not None:
            task.set_name(name)
        return task

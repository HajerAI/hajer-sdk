"""A LangChain-shaped chat model over the real `openai` client: wrapped whole, its calls keep the app's frame.

`tests/langchain_shape.py:ChatModel` is `langchain_openai.ChatOpenAI`'s client shape and call path over the
installed `openai` with an in-memory transport; its module stands for an installed package. The functions
here are the application: they await the chat model the way a LangChain application's call sites do.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

import hajer
from hajer import _frames
from tests import langchain_shape
from tests.langchain_shape import ChatModel, ForeignLoop, path


@pytest.fixture(autouse=True)
def framework_is_a_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ChatModel` stands for `langchain_openai`, which lives in site-packages."""
    shape = str(Path(langchain_shape.__file__).resolve())
    monkeypatch.setattr(_frames, "_PREFIXES", (*_frames._PREFIXES, shape))  # pyright: ignore[reportPrivateUsage]


def settings() -> hajer.HajerSettings:
    return hajer.HajerSettings(capture_content=True, disabled=True)


def _line_of(function: Callable[..., object], needle: str) -> int:
    lines, start = inspect.getsourcelines(function)
    return start + next(index for index, text in enumerate(lines) if needle in text)


# The application: its own functions, outside the framework, awaiting the chat model.
async def app_call(llm: ChatModel) -> object:
    return await llm.ainvoke("Hello")


async def app_structured(llm: ChatModel) -> object:
    return await llm.astructured("Name it")


def app_sync(llm: ChatModel) -> object:
    return llm.invoke("Hello")


async def _one_call(run: Callable[[ChatModel], Coroutine[object, object, object]], llm: ChatModel) -> hajer.WrappedCall:
    with hajer.scope(workflow="probe") as operation:
        await run(llm)
    (call,) = operation.calls
    return call


class TestWrappingTheChatModel:
    def test_wrap_accepts_the_chat_model_and_instruments_its_root_clients(self) -> None:
        llm = ChatModel()
        assert hajer.wrap(llm, settings=settings()) is llm
        assert getattr(path(llm.async_client, "create"), "__hajer_instrumented__", False)
        assert getattr(path(llm.client, "create"), "__hajer_instrumented__", False)

    async def test_an_ainvoke_fanned_out_to_a_child_task_keeps_the_application_frame(self) -> None:
        llm = hajer.wrap(ChatModel(), settings=settings())
        call = await _one_call(app_call, llm)
        innermost = call.caller_frames[0]
        assert (innermost.qualname, innermost.line) == ("app_call", _line_of(app_call, "llm.ainvoke"))
        assert "ChatModel" not in {frame.qualname.split(".")[0] for frame in call.caller_frames}

    async def test_a_structured_chain_with_a_task_per_step_keeps_the_application_frame(self) -> None:
        llm = hajer.wrap(ChatModel(), settings=settings())
        call = await _one_call(app_structured, llm)
        innermost = call.caller_frames[0]
        assert (innermost.qualname, innermost.line) == ("app_structured", _line_of(app_structured, "astructured"))

    def test_a_synchronous_invoke_keeps_the_application_frame(self) -> None:
        llm = hajer.wrap(ChatModel(), settings=settings())
        with hajer.scope() as operation:
            app_sync(llm)
        (call,) = operation.calls
        assert (call.caller_frames[0].qualname, call.caller_frames[0].line) == (
            "app_sync",
            _line_of(app_sync, "llm.invoke"),
        )

    async def test_concurrent_application_calls_never_share_frames(self) -> None:
        llm = hajer.wrap(ChatModel(), settings=settings())
        first, second = await asyncio.gather(_one_call(app_call, llm), _one_call(app_structured, llm))
        assert first.caller_frames[0].qualname == "app_call"
        assert "app_structured" not in {frame.qualname for frame in first.caller_frames}
        assert second.caller_frames[0].qualname == "app_structured"
        assert "app_call" not in {frame.qualname for frame in second.caller_frames}

    async def test_a_legacy_task_factory_without_a_context_argument_still_creates_tasks(self) -> None:
        loop = asyncio.get_running_loop()
        made: list[object] = []

        def legacy(
            event_loop: asyncio.AbstractEventLoop, coro: Coroutine[object, object, object]
        ) -> asyncio.Task[object]:
            task = asyncio.Task(coro, loop=event_loop)
            made.append(task)
            return task

        llm = hajer.wrap(ChatModel(), settings=settings())
        setter = cast(Callable[[object], None], loop.set_task_factory)
        setter(legacy)
        try:
            call = await _one_call(app_call, llm)
        finally:
            loop.set_task_factory(None)
        assert made
        assert call.response_id == "chatcmpl-1"

    async def test_global_instrument_covers_a_chat_model_built_after_it(self) -> None:
        receipt = hajer.instrument(settings=settings())
        try:
            llm = ChatModel()
            call = await _one_call(app_call, llm)
        finally:
            hajer.uninstrument()
        assert receipt.mode == "wrap"
        assert call.caller_frames[0].qualname == "app_call"


def test_attach_mode_records_a_langchain_openai_call_with_its_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The attach table's `langchain-openai` row says content is captured; this is the call that makes it true."""
    chat_openai = type("ChatOpenAI", (ChatModel,), {})
    module = ModuleType("langchain_openai")
    setattr(module, "ChatOpenAI", chat_openai)  # noqa: B010 - a fake module, read by attach through `vars()`
    monkeypatch.setitem(sys.modules, "langchain_openai", module)
    openai = pytest.importorskip("openai")
    patched: list[type[object]] = [chat_openai]
    patched.extend(getattr(openai, name) for name in ("OpenAI", "AsyncOpenAI", "AzureOpenAI", "AsyncAzureOpenAI"))
    try:
        attachment = hajer.attach(settings=settings())
        assert "langchain_openai.ChatOpenAI" in attachment.classes
        llm = cast(ChatModel, chat_openai())
        app_sync(llm)
    finally:
        hajer.detach()
        for owner in patched:
            if owner.__dict__.get("__hajer_attached__") is True:
                delattr(owner, "__hajer_attached__")
                owner.__init__ = owner.__init__.__wrapped__  # pyright: ignore[reportFunctionMemberAccess]
    (call,) = hajer.wrapped_calls()
    assert call.response_id == "chatcmpl-1"
    assert call.content is not None
    assert "name" in str(call.content["output"])
    assert call.caller_frames[0].qualname == "app_sync"


def _on(
    loop_factory: Callable[[], asyncio.AbstractEventLoop], work: Callable[[], Coroutine[object, object, object]]
) -> object:
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(work())


class TestFramesOnAnyLoop:
    """Frames were carried only by patching `BaseEventLoop.create_task`, which a loop with its
    own `create_task` (uvloop, which a uvicorn server runs) never calls. They are now carried in a context
    variable, set in each child task by a task factory installed on the running loop, whatever loop it is."""

    def test_a_loop_with_its_own_create_task_still_carries_the_application_frame(self) -> None:
        llm = hajer.wrap(ChatModel(), settings=settings())
        call = cast(hajer.WrappedCall, _on(ForeignLoop, lambda: _one_call(app_call, llm)))
        assert call.caller_frames[0].qualname == "app_call"

    def test_a_structured_chain_on_a_foreign_loop_keeps_the_application_frame(self) -> None:
        llm = hajer.wrap(ChatModel(), settings=settings())
        call = cast(hajer.WrappedCall, _on(ForeignLoop, lambda: _one_call(app_structured, llm)))
        assert call.caller_frames[0].qualname == "app_structured"

    def test_the_application_s_own_task_factory_still_makes_every_task(self) -> None:
        made: list[object] = []

        def factory(
            loop: asyncio.AbstractEventLoop, coro: Coroutine[object, object, object], **kwargs: object
        ) -> asyncio.Task[object]:
            task = cast(Callable[..., asyncio.Task[object]], asyncio.Task)(coro, loop=loop, **kwargs)
            made.append(task)
            return task

        def loop_with_factory() -> asyncio.AbstractEventLoop:
            loop = ForeignLoop()
            cast(Callable[[object], None], loop.set_task_factory)(factory)
            return loop

        llm = hajer.wrap(ChatModel(), settings=settings())
        call = cast(hajer.WrappedCall, _on(loop_with_factory, lambda: _one_call(app_call, llm)))
        assert made, "the application's factory made the child tasks"
        assert call.caller_frames[0].qualname == "app_call"

    @pytest.mark.parametrize("loop_factory", [asyncio.new_event_loop, ForeignLoop])
    def test_a_context_the_caller_passes_is_never_written_into(
        self, loop_factory: Callable[[], asyncio.AbstractEventLoop]
    ) -> None:
        """`asyncio.Runner` hands every task it makes its one shared context."""
        shared = contextvars.copy_context()

        async def child() -> None:
            return None

        async def work() -> object:
            hajer.wrap(ChatModel(), settings=settings())
            await asyncio.get_running_loop().create_task(child(), context=shared)
            return dict(shared)

        seen = cast("dict[object, object]", _on(loop_factory, work))
        assert not any(getattr(key, "name", "") == "hajer_application_frames" for key in seen)

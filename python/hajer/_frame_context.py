"""Preserve an application's caller across the async child tasks a framework creates, on any event loop.

A framework's `ainvoke` fans out: `asyncio.gather` wraps each coroutine in a task, a chain runs each
step in its own task, and a graph runs each node in one. The provider call then runs on a stack that
starts at the event loop, and the application function that awaited the framework is suspended in
another task, on no stack at all. Its frame is recoverable only where the child task is **created**:
at that moment the creating task is running, and the application frame is on its stack.

So the frames travel in a context variable, which every child task inherits in its own copy of the
context, and a **task factory** installed on the running loop sets it for each new task: the creating
stack's application frames, then the ones that stack itself inherited. A task factory is the one
creation seam every loop honours — the standard loops and uvloop alike — and it is
composed with a factory the application already set, never in place of it. A context the caller hands
`create_task` is copied, never written into.

The factory is installed on the running loop by `wrap` / `instrument`, by `hajer.scope()` and by every
instrumented call, whichever runs first on that loop. A framework call made before any of them ran on a
loop (a client wrapped at import time, first used inside a task created before) records no inherited
frame, and says so with `CALLER_FRAMES_UNRESOLVED` when it has none of its own.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from types import FrameType
from typing import Final, cast

from hajer._frames import MAX_CALLER_FRAMES, CallerFrame, RawFrame, raw_caller_frames

_FRAMES: ContextVar[tuple[RawFrame, ...]] = ContextVar("hajer_application_frames", default=())
_MARK: Final[str] = "__hajer_causal_frames__"
_TaskFactory = Callable[..., "asyncio.Future[object]"]


def _task_frame() -> FrameType | None:
    """The outermost frame of the running asyncio task's own coroutine, or None outside a task."""
    try:
        task = asyncio.current_task()
    except RuntimeError:  # no running loop in this thread
        return None
    if task is None:
        return None
    coro = task.get_coro()
    frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
    return frame if isinstance(frame, FrameType) else None


def _raw_frames() -> tuple[RawFrame, ...]:
    own = raw_caller_frames(last=_task_frame())
    return tuple(dict.fromkeys((*own, *_FRAMES.get())))[:MAX_CALLER_FRAMES]


def frames_for_call() -> tuple[CallerFrame, ...]:
    """The application frames on this task's stack, innermost first, then the ones it was created under."""
    return tuple(CallerFrame(*raw) for raw in _raw_frames())


def _child_context(given: object) -> contextvars.Context:
    """The context a new task runs in: a copy of the one the caller named, or of the current one, with the
    creating stack's frames set. Never the caller's own object."""
    context = given.copy() if isinstance(given, contextvars.Context) else contextvars.copy_context()
    try:
        frames = _raw_frames()
        if frames:
            context.run(_FRAMES.set, frames)
    except Exception:  # noqa: BLE001, S110 - a frame that cannot be carried is a missing frame, never a task
        pass
    return context


def _composed(previous: _TaskFactory | None) -> _TaskFactory:
    """A task factory that sets the child's frames, then makes the task the way the loop would have."""

    def factory(loop: asyncio.AbstractEventLoop, coro: Coroutine[object, object, object], **kwargs: object) -> object:
        given = kwargs.pop("context", None)
        context = _child_context(given)
        if previous is None:
            return cast(Callable[..., object], asyncio.Task)(coro, loop=loop, context=context, **kwargs)
        if given is None:
            # A factory written before `context` existed copies the current context itself: run it inside ours.
            return context.run(previous, loop, coro, **kwargs)
        return previous(loop, coro, context=context, **kwargs)

    setattr(factory, _MARK, True)
    return cast(_TaskFactory, factory)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def install_task_frames() -> None:
    """Carry application frames into every task the running loop creates from now on. Idempotent; never raises;
    nothing to do outside a running loop."""
    loop = _running_loop()
    if loop is None:
        return
    try:
        previous = cast("_TaskFactory | None", loop.get_task_factory())
        if previous is not None and getattr(previous, _MARK, False):
            return
        cast(Callable[[object], None], loop.set_task_factory)(_composed(previous))
    except Exception:  # noqa: BLE001, S110 - a loop that refuses a factory keeps its tasks' frames on their own stacks
        pass

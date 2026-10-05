"""The two stream proxies: a streamed provider call, recorded when the stream ends.

A streamed answer is not one value; it is a container the caller consumes, and the facts Hajer records
about the call — how many chunks crossed, the usage the last frame named, the finish reason, whether the
stream ran to the end — only exist once that consumption is over. So `wrap` hands back a proxy: the
chunks are the provider's own objects, the container is not.

Two rules shape every line here, and both come from where this code runs — inside somebody else's
request path, on the way to sending an email:

- **The customer's stream still behaves like the provider's.** `__iter__` returns `self` and `__next__`
  forwards one item, so a `for` loop, a bare `next()`, an early `break` and a `close()` all do what they
  did before; unknown attributes are delegated to the stream underneath, so `response`, `text_stream`,
  `until_done()` and `get_final_message()` are still there; and every exception is re-raised as it was.
- **The recording never reaches the caller.** Every call into the recorder is inside
  `try/except Exception`, and nothing here touches the value or the exception on its way out. A
  monitoring path that raised into a request path would be worse than a missing observation.

**Why it settles from the context manager and not from garbage collection.** `close()` is what records
the call, and it runs when the iterator is exhausted, when the caller closes the stream, and from
`__exit__` / `__aexit__` of the wrapping context manager. A `__del__` would settle at an hour the
interpreter chooses — after the operation the call belonged to has closed and its `verify` has already
been sent — so an abandoned stream would be recorded into nothing. Settling from the context manager is
a fact recorded before the `with` block returns.

**What a proxy cannot fix.** A helper that consumes the stream inside the provider's own code
(`text_stream` on an Anthropic message stream) hands the caller its own iterator, so the chunks do not
cross `__next__` and `stream_chunks` stays 0. The call, its model, its timing and whether it ran to the
end are still recorded, and `get_final_message()` — which that consumer calls to read the answer — is
recorded in full, usage included. The support matrix on docs.hajer.ai states this per library.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Final, TypeAlias, cast

#: True while a wrapped provider call is sending its request. The HTTP capture reads it so the request is not
#: recorded a second time at the transport; a streaming helper sends on `__enter__`, so that is covered too.
ACTIVE_CALL: Final[contextvars.ContextVar[bool]] = contextvars.ContextVar("hajer_active_call", default=False)

#: Methods whose answer is a reading of the *whole* response rather than one chunk: a provider helper
#: that drains the stream and folds it back into one message. What they return is recorded and then
#: handed to the caller untouched, which is what makes a `text_stream` consumer's usage observable.
FINAL_METHODS: Final[tuple[str, ...]] = ("get_final_message", "get_final_completion", "get_final_response")
#: Methods that drain the stream and return nothing worth reading. Calling one means the stream ran to
#: the end, so the call settles as exhausted.
DRAINING_METHODS: Final[tuple[str, ...]] = ("until_done",)

_Call: TypeAlias = Callable[..., object]


@dataclass(frozen=True, slots=True)
class StreamRecorder:
    """The four things a stream can report. `_wrap.py` is the only thing that builds one.

    A callback bundle rather than an import, for the same reason `_wrap.set_settled_hook` is one: this
    module sits below the record and knows nothing about it, so a stream proxy can be read and tested as
    what it is — a proxy that forwards a provider's stream and reports four events.
    """

    #: One chunk crossed the proxy, before it reaches the caller.
    chunk: Callable[[object], None]
    #: A provider helper returned the whole answer (`get_final_message()`).
    final: Callable[[object], None]
    #: The stream raised. Reported before the exception continues to the caller, unchanged.
    failed: Callable[[BaseException], None]
    #: The call is over: `True` when the stream ran to the end, `False` when it was abandoned.
    settled: Callable[[bool], None]


class _Recording:
    """What the sync and the async proxy share: the inner stream, and settling exactly once."""

    __slots__ = ("_entered", "_exhausted", "_inner", "_recorder", "_settled_once")

    def __init__(self, inner: object, recorder: StreamRecorder) -> None:
        self._inner = inner
        self._recorder = recorder
        self._entered: object | None = None
        self._exhausted = False
        self._settled_once = False

    @property
    def _target(self) -> object:
        """What the provider's surface is on: the object `__enter__` returned, or the stream itself.

        `messages.stream(...)` returns a *manager* whose `__enter__` returns the stream; `create(stream=True)`
        returns the stream. One proxy covers both by remembering what entering produced.
        """
        return self._inner if self._entered is None else self._entered

    def _observe(self, chunk: object) -> None:
        try:
            self._recorder.chunk(chunk)
        except Exception:  # noqa: BLE001, S110 - recording a chunk may never break the stream
            pass

    def _observe_final(self, answer: object) -> None:
        self._exhausted = True
        try:
            self._recorder.final(answer)
        except Exception:  # noqa: BLE001, S110 - reading the final answer may never break the call
            pass

    def _observe_failure(self, error: BaseException) -> None:
        try:
            self._recorder.failed(error)
        except Exception:  # noqa: BLE001, S110 - recording a failure may never replace it
            pass

    def _settle(self) -> None:
        """Record the call, once. Every later close, exit and exhaustion is a no-op."""
        if self._settled_once:
            return
        self._settled_once = True
        try:
            self._recorder.settled(self._exhausted)
        except Exception:  # noqa: BLE001, S110 - settling may never break the caller's stream
            pass

    def _delegate(self, name: str) -> object:
        """One attribute of the provider's stream, with the two draining helpers recorded.

        Delegation is what keeps the proxy honest: the caller reaches `response`, `text_stream` and the
        provider's own helpers exactly as before. The two exceptions are the helpers that *consume* the
        stream — their answer is the whole response, so it is read into the record before it is returned.
        """
        value = getattr(self._target, name)
        if not callable(value):
            return value
        if name in FINAL_METHODS:
            return self._recording_final(value)
        if name in DRAINING_METHODS:
            return self._recording_drain(value)
        return value

    def _recording_final(self, helper: _Call) -> _Call:
        raise NotImplementedError  # pragma: no cover - each proxy implements its own calling convention

    def _recording_drain(self, helper: _Call) -> _Call:
        raise NotImplementedError  # pragma: no cover - likewise


class RecordingStream(_Recording):
    """A synchronous provider stream, forwarded item by item and recorded when it ends.

    `__iter__` returns `self` and `__next__` forwards one chunk, rather than `__iter__` being a generator:
    a generator would make `next(stream)` and `iter(stream)` two different objects with two different
    lifetimes, and an abandoned generator settles when the interpreter collects it rather than when the
    caller stopped reading.
    """

    __slots__ = ("_iterator",)

    def __init__(self, inner: object, recorder: StreamRecorder) -> None:
        super().__init__(inner, recorder)
        self._iterator: Iterator[object] | None = None

    def __getattr__(self, name: str) -> object:
        return self._delegate(name)

    def __iter__(self) -> RecordingStream:
        return self

    def __next__(self) -> object:
        try:
            chunk = next(self._pull())
        except StopIteration:
            self._exhausted = True
            self.close()
            raise
        except BaseException as error:
            self._observe_failure(error)
            self._settle()
            raise
        self._observe(chunk)
        return chunk

    def _pull(self) -> Iterator[object]:
        if self._iterator is None:
            self._iterator = iter(cast(Iterable[object], self._target))
        return self._iterator

    def __enter__(self) -> RecordingStream:
        entered = getattr(self._inner, "__enter__", None)
        if callable(entered):
            token = ACTIVE_CALL.set(True)
            try:
                produced = cast(Callable[[], object], entered)()
            except BaseException as error:
                self._observe_failure(error)
                self._settle()
                raise
            finally:
                ACTIVE_CALL.reset(token)
            if produced is not None and produced is not self._inner:
                self._entered = produced
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool | None:
        suppressed: bool | None = None
        try:
            self.close()
        finally:
            exited = getattr(self._inner, "__exit__", None)
            if callable(exited):
                suppressed = _as_bool(exited(exc_type, exc, tb))
        return suppressed

    def close(self) -> None:
        """Record the call and close the stream underneath. Idempotent, and the first settle wins."""
        self._settle()
        closer = getattr(self._target, "close", None)
        if callable(closer):
            cast(Callable[[], object], closer)()

    def _recording_final(self, helper: _Call) -> _Call:
        @functools.wraps(helper)
        def recorded(*args: object, **kwargs: object) -> object:
            answer = helper(*args, **kwargs)
            self._observe_final(answer)
            self._settle()
            return answer

        return recorded

    def _recording_drain(self, helper: _Call) -> _Call:
        @functools.wraps(helper)
        def recorded(*args: object, **kwargs: object) -> object:
            answer = helper(*args, **kwargs)
            self._exhausted = True
            return answer

        return recorded


class DualRecordingStream(RecordingStream):
    """Preserve both iteration protocols, as exposed by LiteLLM's stream wrapper.

    The consumer chooses how to read the stream. Both paths share the same
    recording state, so closing through either protocol settles it only once.
    """

    __slots__ = ("_async_iterator",)

    def __init__(self, inner: object, recorder: StreamRecorder) -> None:
        super().__init__(inner, recorder)
        self._async_iterator: AsyncIterator[object] | None = None

    def __aiter__(self) -> DualRecordingStream:
        return self

    async def __anext__(self) -> object:
        try:
            if self._async_iterator is None:
                self._async_iterator = cast(AsyncIterator[object], self._target).__aiter__()
            chunk = await self._async_iterator.__anext__()
        except StopAsyncIteration:
            self._exhausted = True
            await self.aclose()
            raise
        except BaseException as error:
            self._observe_failure(error)
            self._settle()
            raise
        self._observe(chunk)
        return chunk

    async def __aenter__(self) -> DualRecordingStream:
        entered = getattr(self._inner, "__aenter__", None)
        if callable(entered):
            token = ACTIVE_CALL.set(True)
            try:
                produced = await _awaited(entered())
            except BaseException as error:
                self._observe_failure(error)
                self._settle()
                raise
            finally:
                ACTIVE_CALL.reset(token)
            if produced is not None and produced is not self._inner:
                self._entered = produced
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool | None:
        suppressed: bool | None = None
        try:
            await self.aclose()
        finally:
            exited = getattr(self._inner, "__aexit__", None)
            if callable(exited):
                suppressed = _as_bool(await _awaited(exited(exc_type, exc, tb)))
        return suppressed

    async def aclose(self) -> None:
        self._settle()
        closer = getattr(self._target, "aclose", None) or getattr(self._target, "close", None)
        if callable(closer):
            await _awaited(closer())


class AsyncRecordingStream(_Recording):
    """The async twin: `__aiter__` returns `self`, `__anext__` forwards one chunk, `aclose()` settles."""

    __slots__ = ("_iterator",)

    def __init__(self, inner: object, recorder: StreamRecorder) -> None:
        super().__init__(inner, recorder)
        self._iterator: AsyncIterator[object] | None = None

    def __getattr__(self, name: str) -> object:
        return self._delegate(name)

    def __aiter__(self) -> AsyncRecordingStream:
        return self

    async def __anext__(self) -> object:
        try:
            chunk = await self._pull().__anext__()
        except StopAsyncIteration:
            self._exhausted = True
            await self.aclose()
            raise
        except BaseException as error:
            self._observe_failure(error)
            self._settle()
            raise
        self._observe(chunk)
        return chunk

    def _pull(self) -> AsyncIterator[object]:
        if self._iterator is None:
            self._iterator = cast(AsyncIterator[object], self._target).__aiter__()
        return self._iterator

    async def __aenter__(self) -> AsyncRecordingStream:
        entered = getattr(self._inner, "__aenter__", None)
        if callable(entered):
            token = ACTIVE_CALL.set(True)
            try:
                produced = await _awaited(entered())
            except BaseException as error:
                self._observe_failure(error)
                self._settle()
                raise
            finally:
                ACTIVE_CALL.reset(token)
            if produced is not None and produced is not self._inner:
                self._entered = produced
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool | None:
        suppressed: bool | None = None
        try:
            await self.aclose()
        finally:
            exited = getattr(self._inner, "__aexit__", None)
            if callable(exited):
                suppressed = _as_bool(await _awaited(exited(exc_type, exc, tb)))
        return suppressed

    async def aclose(self) -> None:
        """Record the call and close the stream underneath. Idempotent, and the first settle wins."""
        self._settle()
        closer = getattr(self._target, "aclose", None) or getattr(self._target, "close", None)
        if callable(closer):
            await _awaited(closer())

    def _recording_final(self, helper: _Call) -> _Call:
        @functools.wraps(helper)
        async def recorded(*args: object, **kwargs: object) -> object:
            answer = await _awaited(helper(*args, **kwargs))
            self._observe_final(answer)
            self._settle()
            return answer

        return recorded

    def _recording_drain(self, helper: _Call) -> _Call:
        @functools.wraps(helper)
        async def recorded(*args: object, **kwargs: object) -> object:
            answer = await _awaited(helper(*args, **kwargs))
            self._exhausted = True
            return answer

        return recorded


async def _awaited(value: object) -> object:
    """A provider helper's answer, whether it was a coroutine or a plain value."""
    if inspect.isawaitable(value):
        return await cast(Awaitable[object], value)
    return value


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None

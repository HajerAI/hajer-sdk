"""`observe`'s delivery contract, and the bounded queue that keeps it.

"Fire and forget" is not one fact, it is three, and they are three different receipts:

1. **queued locally** — the observation is in this process's memory. Nothing has been sent. A crash
   here loses it, and the receipt says so rather than implying otherwise.
2. **accepted by the server** — a flush returned 2xx and the server named the observation. Durable.
3. **assessment complete** — the verifier ran and there is an answer, which arrives later and only
   if asked for: `receipt.poll()` once, or `receipt.wait(timeout_ms=…)` until it is there.

Two more states exist because pretending they do not would be the dishonest part: `disabled` (the
client is inert, nothing was queued and nothing will be) and `dropped`, with the reason — the queue
was full, or the single observation was over `HAJER_BODY_MAX_BYTES`, or the process exited before an
async client was closed.

The queue is bounded twice, by count (`HAJER_OBSERVE_QUEUE_MAX`) and by the bytes of one flush
(`HAJER_OBSERVE_BATCH_MAX` observations, and never more than `HAJER_BODY_MAX_BYTES` on the wire).
A failed flush is retried with doubling backoff from `HAJER_OBSERVE_BACKOFF_INITIAL_MS` up to
`HAJER_OBSERVE_BACKOFF_MAX_MS`; the observations stay queued while that happens, so a flush that
fails does not lose them, and a service that is down does not turn into an unbounded buffer either.
The synchronous queue flushes at interpreter exit (`atexit`); the asynchronous one cannot — there is
no loop left to await on — so it warns and marks what is left `dropped`, which is why an `AsyncHajer`
should be closed (`await client.close()`, or `async with`).
"""

from __future__ import annotations

import asyncio
import atexit
import threading
import time
import warnings
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from hajer._json import JsonObject
from hajer._models import Assessment
from hajer._settings import HajerSettings

ReceiptState: TypeAlias = Literal["disabled", "queued", "accepted", "complete", "dropped"]

#: Why an observation was dropped. Each one is a fact about this process, never about the service.
DropReason: TypeAlias = Literal["QUEUE_FULL", "BODY_OVER_BOUND", "PROCESS_EXIT", "CLIENT_CLOSED"]

_MS_PER_S: Final[float] = 1_000.0
#: How often `wait()` asks again. Not a bound on anything the service does, so not a setting.
_POLL_INTERVAL_MS: Final[int] = 100


class _ReceiptBase:
    """The state machine both receipts share. Read it; the queue writes it."""

    __slots__ = ("_assessment", "_state", "detail", "idempotency_key", "observation_id", "queued_at")

    def __init__(self, *, idempotency_key: str, state: ReceiptState) -> None:
        self.idempotency_key = idempotency_key
        self._state: ReceiptState = state
        self.observation_id: str | None = None
        self.detail: str | None = None
        self.queued_at = time.time()
        self._assessment: Assessment | None = None

    @property
    def state(self) -> ReceiptState:
        return self._state

    @property
    def queued(self) -> bool:
        """The observation is in this process's memory and nothing has been sent."""
        return self._state == "queued"

    @property
    def accepted(self) -> bool:
        """The server took it. True once accepted, and still true once the assessment arrives."""
        return self._state in ("accepted", "complete")

    @property
    def complete(self) -> bool:
        """The assessment is here. `receipt.assessment` is not None."""
        return self._state == "complete"

    @property
    def assessment(self) -> Assessment | None:
        """The assessment, once `poll()` or `wait()` has found one."""
        return self._assessment

    def mark_accepted(self, observation_id: str | None) -> None:
        if self._state == "queued":
            self._state = "accepted"
        self.observation_id = observation_id or self.observation_id

    def mark_dropped(self, reason: DropReason) -> None:
        if self._state in ("accepted", "complete"):
            return
        self._state = "dropped"
        self.detail = reason

    def mark_complete(self, assessment: Assessment) -> None:
        self._assessment = assessment
        self._state = "complete"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(state={self._state!r}, idempotency_key={self.idempotency_key!r}, "
            f"observation_id={self.observation_id!r}, detail={self.detail!r})"
        )


class ObserveReceipt(_ReceiptBase):
    """What `Hajer.observe` returns. `poll` and `wait` block; both are safe to never call."""

    __slots__ = ("_poller",)

    def __init__(
        self,
        *,
        idempotency_key: str,
        state: ReceiptState,
        poller: Callable[[str], Assessment | None] | None = None,
    ) -> None:
        super().__init__(idempotency_key=idempotency_key, state=state)
        self._poller = poller

    def poll(self) -> Assessment | None:
        """Ask once whether the assessment is ready. `None` until the server has one."""
        if self._assessment is not None:
            return self._assessment
        if self._poller is None or self.observation_id is None:
            return None
        found = self._poller(self.observation_id)
        if found is not None:
            self.mark_complete(found)
        return found

    def wait(self, *, timeout_ms: int) -> Assessment | None:
        """Ask until the assessment is ready or the timeout expires. `None` means not yet."""
        deadline = time.monotonic() + timeout_ms / _MS_PER_S
        while True:
            found = self.poll()
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(_POLL_INTERVAL_MS / _MS_PER_S, max(0.0, deadline - time.monotonic())))


class AsyncObserveReceipt(_ReceiptBase):
    """What `AsyncHajer.observe` returns. The same three states; `poll` and `wait` are awaited."""

    __slots__ = ("_poller",)

    def __init__(
        self,
        *,
        idempotency_key: str,
        state: ReceiptState,
        poller: Callable[[str], Awaitable[Assessment | None]] | None = None,
    ) -> None:
        super().__init__(idempotency_key=idempotency_key, state=state)
        self._poller = poller

    async def poll(self) -> Assessment | None:
        """Ask once whether the assessment is ready. `None` until the server has one."""
        if self._assessment is not None:
            return self._assessment
        if self._poller is None or self.observation_id is None:
            return None
        found = await self._poller(self.observation_id)
        if found is not None:
            self.mark_complete(found)
        return found

    async def wait(self, *, timeout_ms: int) -> Assessment | None:
        """Ask until the assessment is ready or the timeout expires. `None` means not yet."""
        deadline = time.monotonic() + timeout_ms / _MS_PER_S
        while True:
            found = await self.poll()
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(min(_POLL_INTERVAL_MS / _MS_PER_S, max(0.0, deadline - time.monotonic())))


@dataclass(slots=True)
class _Pending:
    body: JsonObject
    size: int
    receipt: _ReceiptBase


class Bounds:
    """The two bounds a batch obeys, and the doubling backoff a failure earns."""

    __slots__ = ("_failures", "overhead", "settings")

    def __init__(self, settings: HajerSettings, overhead: int) -> None:
        self.settings = settings
        self.overhead = overhead
        self._failures = 0

    def take(self, items: deque[_Pending]) -> list[_Pending]:
        """The next batch: at most `observe_batch_max` items and at most `body_max_bytes` on the wire."""
        batch: list[_Pending] = []
        used = self.overhead
        while items and len(batch) < self.settings.observe_batch_max:
            head = items[0]
            # +1 for the comma this item needs once it is not the first.
            projected = used + head.size + (1 if batch else 0)
            if batch and projected > self.settings.body_max_bytes:
                break
            batch.append(items.popleft())
            used = projected
        return batch

    def succeeded(self) -> None:
        self._failures = 0

    def failed(self) -> float:
        """Seconds to wait before the next attempt. Doubling, capped, never zero."""
        self._failures += 1
        waited_ms = min(
            self.settings.observe_backoff_initial_ms * (2 ** (self._failures - 1)),
            self.settings.observe_backoff_max_ms,
        )
        return waited_ms / _MS_PER_S


#: Every live synchronous queue, so one `atexit` hook flushes them all without keeping them alive.
_LIVE: weakref.WeakSet[ObserveQueue] = weakref.WeakSet()


def _flush_at_exit() -> None:
    for queue in list(_LIVE):
        queue.close()


atexit.register(_flush_at_exit)


class ObserveQueue:
    """The synchronous bounded queue: one background thread, batched flushes, flush on exit."""

    # `__weakref__` is here so `_LIVE` can hold this object weakly: the atexit hook must flush a
    # live queue without being the reason it stays alive.
    __slots__ = (
        "__weakref__",
        "_bounds",
        "_items",
        "_lock",
        "_send",
        "_sending",
        "_settings",
        "_stopping",
        "_wake",
        "_worker",
    )

    def __init__(
        self,
        *,
        settings: HajerSettings,
        send: Callable[[Sequence[JsonObject]], tuple[str, ...]],
        overhead: int,
    ) -> None:
        self._settings = settings
        self._send = send
        self._bounds = Bounds(settings, overhead)
        self._items: deque[_Pending] = deque()
        self._lock = threading.Lock()
        self._sending = threading.Lock()
        self._wake = threading.Event()
        self._stopping = False
        self._worker: threading.Thread | None = None
        _LIVE.add(self)

    # ── submission ──────────────────────────────────────────────────────────────────────────────

    def submit(self, body: JsonObject, size: int, receipt: _ReceiptBase) -> None:
        """Enqueue, or drop the newest with `QUEUE_FULL` — never block the caller, never raise."""
        with self._lock:
            if self._stopping:
                receipt.mark_dropped("CLIENT_CLOSED")  # the queue owns the receipt's state
                return
            if len(self._items) >= self._settings.observe_queue_max:
                receipt.mark_dropped("QUEUE_FULL")
                return
            self._items.append(_Pending(body=body, size=size, receipt=receipt))
            due = len(self._items) >= self._settings.observe_batch_max
        self._ensure_worker()
        if due:
            self._wake.set()

    def depth(self) -> int:
        """Observations waiting in memory right now."""
        with self._lock:
            return len(self._items)

    # ── flushing ────────────────────────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """Drain everything queued, in the caller's thread, one attempt per batch, no backoff."""
        while True:
            with self._lock:
                batch = self._bounds.take(self._items)
            if not batch:
                return
            if not self._attempt(batch):
                return

    def close(self) -> None:
        """Stop accepting, stop the worker, flush what is left. Idempotent."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        self._wake.set()
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=self._settings.observe_flush_deadline_ms / _MS_PER_S)
        self.flush()
        with self._lock:
            leftover = list(self._items)
            self._items.clear()
        for pending in leftover:
            pending.receipt.mark_dropped("PROCESS_EXIT")

    # ── internals ───────────────────────────────────────────────────────────────────────────────

    def _attempt(self, batch: list[_Pending]) -> bool:
        """Send one batch. True on acceptance; on failure the items go back to the front."""
        with self._sending:
            try:
                ids = self._send([pending.body for pending in batch])
            except Exception:  # noqa: BLE001 — a failed flush is data, never an exception into a caller
                with self._lock:
                    self._items.extendleft(reversed(batch))
                return False
        for index, pending in enumerate(batch):
            pending.receipt.mark_accepted(ids[index] if index < len(ids) else None)
        self._bounds.succeeded()
        return True

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        with self._lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(target=self._run, name="hajer-observe", daemon=True)
        self._worker.start()

    def _run(self) -> None:
        interval_s = self._settings.observe_flush_interval_ms / _MS_PER_S
        while not self._stopping:
            self._wake.wait(timeout=interval_s)
            self._wake.clear()
            while not self._stopping:
                with self._lock:
                    batch = self._bounds.take(self._items)
                if not batch:
                    break
                if not self._attempt(batch):
                    time.sleep(self._bounds.failed())
                    break


class AsyncObserveQueue:
    """The asynchronous twin: one background task instead of a thread, the same bounds."""

    __slots__ = ("_bounds", "_items", "_send", "_sending", "_settings", "_stopping", "_wake", "_worker")

    def __init__(
        self,
        *,
        settings: HajerSettings,
        send: Callable[[Sequence[JsonObject]], Awaitable[tuple[str, ...]]],
        overhead: int,
    ) -> None:
        self._settings = settings
        self._send = send
        self._bounds = Bounds(settings, overhead)
        self._items: deque[_Pending] = deque()
        self._sending = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stopping = False
        self._worker: asyncio.Task[None] | None = None

    def submit(self, body: JsonObject, size: int, receipt: _ReceiptBase) -> None:
        if self._stopping:
            receipt.mark_dropped("CLIENT_CLOSED")
            return
        if len(self._items) >= self._settings.observe_queue_max:
            receipt.mark_dropped("QUEUE_FULL")
            return
        self._items.append(_Pending(body=body, size=size, receipt=receipt))
        self._ensure_worker()
        if len(self._items) >= self._settings.observe_batch_max:
            self._wake.set()

    def depth(self) -> int:
        return len(self._items)

    async def flush(self) -> None:
        while True:
            batch = self._bounds.take(self._items)
            if not batch:
                return
            if not await self._attempt(batch):
                return

    async def close(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._wake.set()
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        await self.flush()
        leftover = list(self._items)
        self._items.clear()
        for pending in leftover:
            pending.receipt.mark_dropped("PROCESS_EXIT")

    def abandon_at_exit(self) -> None:
        """The interpreter is exiting and there is no loop to await on. Say so; drop what is left."""
        leftover = list(self._items)
        self._items.clear()
        if not leftover:
            return
        warnings.warn(
            f"hajer: {len(leftover)} observation(s) were still queued at interpreter exit and were "
            "dropped — an AsyncHajer cannot be flushed from atexit; close it (`await client.close()` "
            "or `async with hajer.AsyncHajer() as client:`)",
            RuntimeWarning,
            stacklevel=2,
        )
        for pending in leftover:
            pending.receipt.mark_dropped("PROCESS_EXIT")

    async def _attempt(self, batch: list[_Pending]) -> bool:
        async with self._sending:
            try:
                ids = await self._send([pending.body for pending in batch])
            except Exception:  # noqa: BLE001 — a failed flush is data, never an exception into a caller
                self._items.extendleft(reversed(batch))
                return False
        for index, pending in enumerate(batch):
            pending.receipt.mark_accepted(ids[index] if index < len(ids) else None)
        self._bounds.succeeded()
        return True

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        interval_s = self._settings.observe_flush_interval_ms / _MS_PER_S
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval_s)
            except TimeoutError:
                pass
            self._wake.clear()
            while not self._stopping:
                batch = self._bounds.take(self._items)
                if not batch:
                    break
                if not await self._attempt(batch):
                    await asyncio.sleep(self._bounds.failed())
                    break

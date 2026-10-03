"""Observe a raw or streaming response's body as the application reads it; never read it, never replace it.

`with_raw_response.create(...)` and `with_streaming_response.create(...)` hand the application the provider's
own response object (`LegacyAPIResponse`, `APIResponse`), whose body it may read through any accessor:
`parse()`, `read()`, `json()`, `text`, `iter_lines()`, `iter_bytes()`, `http_response.json()`. Every one of them
ends in the HTTP response's `iter_bytes` / `aiter_bytes`, so that is the one seam tapped — on the response
*instance*, so the object the application holds is the provider's, unchanged in type and behaviour. A body
httpx already loaded (a small non-streamed response) is replayed at once. The tap reports each piece and then
the end — complete, cut short, or closed unread — exactly once, however many times the application reads.

Nothing here raises into the application: a response whose body cannot be tapped is reported as ended unread.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from typing import cast

#: One piece of the body as the application's read delivered it.
OnChunk = Callable[[bytes], None]
#: The body is over: whether it was read to its end, and the exception that stopped it, if one did.
OnEnd = Callable[[bool, BaseException | None], None]


def http_response(result: object) -> object | None:
    """The HTTP response a provider's raw-response wrapper carries, or None when `result` is not one."""
    try:
        parse = getattr(result, "parse", None)
        http = getattr(result, "http_response", None)
    except Exception:  # noqa: BLE001 - an object whose attributes raise is not a raw response
        return None
    return http if callable(parse) and http is not None and callable(getattr(http, "iter_bytes", None)) else None


class _Tap:
    """One body's pieces and its end, reported once."""

    def __init__(self, on_chunk: OnChunk, on_end: OnEnd) -> None:
        self._on_chunk = on_chunk
        self._on_end = on_end
        self.ended = False
        #: A read began.
        self.started = False
        #: A read is fetching its next piece. httpx closes the response itself when a read reaches the end,
        #: from inside that fetch and before the reader sees the end, so a close during a fetch is httpx's own
        #: and the reader reports how the read ended; any other close is the application's.
        self.pulling = False

    def chunk(self, piece: bytes) -> None:
        if not self.ended:
            try:
                self._on_chunk(piece)
            except Exception:  # noqa: BLE001, S110 - reading a piece may never break the application's read
                pass

    def end(self, complete: bool, error: BaseException | None) -> None:
        if self.ended:
            return
        self.ended = True
        try:
            self._on_end(complete, error)
        except Exception:  # noqa: BLE001, S110 - settling the record may never break the application's read
            pass


def _sync_reader(original: Callable[..., Iterator[bytes]], tap: _Tap) -> Callable[..., Iterator[bytes]]:
    def iter_bytes(*args: object, **kwargs: object) -> Iterator[bytes]:
        tap.started = True
        pieces = iter(original(*args, **kwargs))
        try:
            while True:
                tap.pulling = True
                try:
                    piece = next(pieces)
                except StopIteration:
                    break
                finally:
                    tap.pulling = False
                tap.chunk(piece)
                yield piece
        except GeneratorExit:
            tap.end(False, None)  # the reader stopped early
            raise
        except BaseException as error:
            tap.end(False, error)
            raise
        tap.end(True, None)

    return iter_bytes


def _async_reader(original: Callable[..., AsyncIterator[bytes]], tap: _Tap) -> Callable[..., AsyncIterator[bytes]]:
    async def aiter_bytes(*args: object, **kwargs: object) -> AsyncIterator[bytes]:
        tap.started = True
        pieces = aiter(original(*args, **kwargs))
        try:
            while True:
                tap.pulling = True
                try:
                    piece = await anext(pieces)
                except StopAsyncIteration:
                    break
                finally:
                    tap.pulling = False
                tap.chunk(piece)
                yield piece
        except GeneratorExit:
            tap.end(False, None)
            raise
        except BaseException as error:
            tap.end(False, error)
            raise
        tap.end(True, None)

    return aiter_bytes


def _closer(original: Callable[[], object], tap: _Tap) -> Callable[[], object]:
    def close() -> object:
        try:
            return original()
        finally:
            if not tap.pulling:
                tap.end(False, None)  # the application closed it, unread or part-read

    return close


def observe_body(http: object, on_chunk: OnChunk, on_end: OnEnd) -> None:
    """Report the body of `http` as it is read — or at once, when httpx has already loaded it."""
    tap = _Tap(on_chunk, on_end)
    try:
        if getattr(http, "is_stream_consumed", False):
            tap.chunk(cast(bytes, getattr(http, "content", b"")))
            tap.end(True, None)
            return
        for name, wrap in (("iter_bytes", _sync_reader), ("aiter_bytes", _async_reader)):
            original = getattr(http, name, None)
            if callable(original):
                setattr(http, name, wrap(cast(Callable[..., Iterator[bytes]], original), tap))  # pyright: ignore[reportArgumentType]
        for name in ("close", "aclose"):
            original = getattr(http, name, None)
            if callable(original):
                setattr(http, name, _closer(cast(Callable[[], object], original), tap))
    except Exception as error:  # noqa: BLE001 - a body that cannot be tapped is a body not observed
        tap.end(False, error)

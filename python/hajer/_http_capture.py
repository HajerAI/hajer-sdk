"""Transport tee: bytes are observed only when the application consumes them.

Two ways in. `CaptureTransport` / `AsyncCaptureTransport` wrap a transport the caller owns — constructing one
is the explicit opt-in. `HAJER_CAPTURE_HTTP` patches httpx's own default transports (`HTTPTransport`,
`AsyncHTTPTransport`) at the first `wrap`, `attach` or `instrument`, so an application's own HTTP calls — the
search API, the data vendor a workflow reads — are recorded without touching its code. Both do the same for
httpx2, the fork openai 3.x and anthropic 1.x send through (`_http_libraries`): its default transports are patched
beside httpx's, and a response is tapped with a stream of its own library, which is the only kind its client accepts.

A POST to a model path (`/chat/completions`, `/responses`, `/messages`) is recorded as that provider call. Any
other request is recorded only with `HAJER_CAPTURE_HTTP`, as an `http` call: the method, the scheme and host,
the path as a **template** — a segment that is an id, a token or anything the redaction catalog recognises is
replaced by a placeholder, because webhooks and bots carry their credential in the path — and the status.
Never a header, never the query string, never a body. Never a request a wrapped provider call is making
(`ACTIVE_CALL`), and never the SDK's own (its `User-Agent`) — whatever other tracing tool is active, or whose span
is current: a model request another tool also traces is recorded here, and its span is matched to this record
(`_claims`).
"""

from __future__ import annotations

import functools
import json
import math
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Final, Literal, cast

import httpx

from hajer._claims import Claimant, claim
from hajer._http_libraries import HTTPX, HttpLibrary, libraries, library_of, not_read_errors
from hajer._json import JsonValue
from hajer._redact import ClientRedactionPolicy, build_policy, redact_document
from hajer._settings import HajerSettings
from hajer._transport import USER_AGENT
from hajer._wrap import (
    ACTIVE_CALL,
    WrappedCall,
    add_installer,
    host_of,
    open_http_call,
    record_http_exchange,
    settle_http_call,
)

_MODEL_PATHS: Final[tuple[str, ...]] = ("/chat/completions", "/responses", "/messages")
#: What the SDK's own requests say they are: `hajer-python/<version>`.
_OWN_AGENT: Final[str] = USER_AGENT.split("/", 1)[0] + "/"
Kind = Literal["model", "http"]


def _kind(request: httpx.Request, *, everything: bool) -> Kind | None:
    """What this request is recorded as, or None when it is not recorded at all."""
    if ACTIVE_CALL.get() or request.headers.get("user-agent", "").startswith(_OWN_AGENT):
        return None
    if request.method == "POST" and request.url.path.endswith(_MODEL_PATHS):
        return "model"
    return "http" if everything else None


#: A path segment this long or longer is a token candidate: shorter ones are words, versions and short codes.
_TOKEN_MIN_CHARACTERS: Final[int] = 16
#: At or past this length a segment is a token whatever it is made of: nobody names a resource with it.
_TOKEN_ALWAYS_CHARACTERS: Final[int] = 32
#: Bits of entropy per character past which a token candidate made of one character class is still random.
_TOKEN_ENTROPY_BITS: Final[float] = 3.5


@functools.cache
def _policy() -> ClientRedactionPolicy:
    return build_policy()


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(count / len(text) * math.log2(count / len(text)) for count in counts.values())


def _token_like(segment: str) -> bool:
    """A credential or an opaque id: long, and random-looking — mixed character classes or high entropy."""
    if len(segment) >= _TOKEN_ALWAYS_CHARACTERS:
        return True
    if len(segment) < _TOKEN_MIN_CHARACTERS:
        return False
    classes = sum(
        (
            any(character.isdigit() for character in segment),
            any(character.islower() for character in segment),
            any(character.isupper() for character in segment),
        )
    )
    return classes >= 2 or _entropy(segment) >= _TOKEN_ENTROPY_BITS


def _segment(segment: str) -> str:
    """One path segment as it may be recorded: itself, or the placeholder for what it is."""
    if segment.isdigit():
        return "{id}"
    redacted, _ = redact_document(segment, policy=_policy())
    if redacted != segment:
        return "{redacted}"
    return "{token}" if _token_like(segment) else segment


def _address(url: httpx.URL) -> str:
    """The URL as it may be recorded: scheme, host, port and a path template — no user info, no query string,
    no fragment, and no path segment that is an id, a token or something the redaction catalog recognises."""
    template = "/".join(_segment(segment) if segment else segment for segment in url.path.split("/"))
    return str(httpx.URL(scheme=url.scheme, host=url.host, port=url.port)).rstrip("/") + template


@dataclass
class _Capture:
    request: httpx.Request
    settings: HajerSettings
    kind: Kind
    started_ns: int = field(default_factory=time.monotonic_ns)
    body: bytearray = field(default_factory=bytearray)
    status: int = 0
    streamed: bool = False
    complete: bool = False
    settled: bool = False
    truncated: bool = False
    call: WrappedCall | None = None
    claimant: Claimant | None = None

    def chunk(self, value: bytes) -> None:
        if self.call is not None:
            return  # an `http` call records no body, so none is kept
        remaining = self.settings.wrapped_call_max_bytes - len(self.body)
        self.body.extend(value[:remaining])
        self.truncated |= len(value) > remaining

    def finish(self, error: BaseException | None = None) -> None:
        if self.settled:
            return
        self.settled = True
        try:
            if self.call is not None:
                self._finish_http(error)
                return
            payload: JsonValue = json.loads(self.request.content)
            if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
                return
            recorded = record_http_exchange(
                path=self.request.url.path,
                request=payload,
                body=bytes(self.body),
                status=self.status,
                streamed=self.streamed,
                complete=self.complete,
                truncated=self.truncated,
                settings=self.settings,
                started_ns=self.started_ns,
                error=error,
            )
            if self.claimant is not None:
                self.claimant.call = recorded
        except Exception:  # noqa: BLE001, S110 - capture failure cannot change HTTP behavior
            pass

    def _finish_http(self, error: BaseException | None) -> None:
        assert self.call is not None  # noqa: S101 - `finish` checked it
        settle_http_call(
            self.call,
            status=self.status,
            streamed=self.streamed,
            complete=self.complete,
            started_ns=self.started_ns,
            error=error,
        )


def _buffer(capture: _Capture, value: bytes) -> None:
    """Buffer faults discard telemetry, never bytes or the provider's success/failure status."""
    try:
        capture.chunk(value)
    except Exception:  # noqa: BLE001 - even a faulty buffer must not break the consumer's stream
        capture.truncated = True


class _SyncTee(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, capture: _Capture) -> None:
        self.stream, self.capture = stream, capture

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self.stream:
                _buffer(self.capture, chunk)
                yield chunk
            self.capture.complete = True
        except GeneratorExit:
            self.capture.finish()  # the reader stopped early: a short read, not a failed request
            raise
        except BaseException as error:
            self.capture.finish(error)
            raise
        self.capture.finish()

    def close(self) -> None:
        self.capture.finish()
        self.stream.close()


class _AsyncTee(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, capture: _Capture) -> None:
        self.stream, self.capture = stream, capture

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.stream:
                _buffer(self.capture, chunk)
                yield chunk
            self.capture.complete = True
        except GeneratorExit:
            self.capture.finish()  # the reader stopped early: a short read, not a failed request
            raise
        except BaseException as error:
            self.capture.finish(error)
            raise
        self.capture.finish()

    async def aclose(self) -> None:
        self.capture.finish()
        await self.stream.aclose()


@functools.cache
def _tees(library: HttpLibrary) -> tuple[type[_SyncTee], type[_AsyncTee]]:
    """The two tees as `library`'s own byte streams too: its client asserts a transport's stream is one of its own.
    Both bases are the same two-method interface, and the tee defines both methods."""
    if library is HTTPX:
        return _SyncTee, _AsyncTee
    return (
        cast(type[_SyncTee], type("_SyncTee", (_SyncTee, library.sync_byte_stream), {})),
        cast(type[_AsyncTee], type("_AsyncTee", (_AsyncTee, library.async_byte_stream), {})),
    )


def _model_of(request: httpx.Request) -> object:
    try:
        payload: JsonValue = json.loads(request.content)
    except (ValueError, *not_read_errors()):
        return None
    return payload.get("model") if isinstance(payload, dict) else None


def _begin(request: httpx.Request, settings: HajerSettings) -> _Capture | None:
    try:
        kind = _kind(request, everything=settings.capture_http)
        if kind is None:
            return None
        capture = _Capture(request, settings, kind)
        if kind == "model":
            capture.claimant = claim(_model_of(request))
        if kind == "http":
            # Begun here, on the caller's own stack: its frames are the application's now, not those of
            # whoever reads the body later.
            url = request.url
            capture.call = open_http_call(
                method=request.method, url=_address(url), host=host_of(str(url)), settings=settings
            )
    except Exception:  # noqa: BLE001 - a record that cannot begin is a missing record, never a failed request
        return None
    return capture


def _observe(capture: _Capture, response: httpx.Response, *, asynchronous: bool) -> None:
    capture.status = response.status_code
    capture.streamed = "text/event-stream" in response.headers.get("content-type", "")
    library = library_of(response)
    sync_tee, async_tee = _tees(library)
    if response.is_stream_consumed:
        _buffer(capture, response.content)
        capture.complete = True
        capture.finish()
    elif asynchronous and isinstance(response.stream, library.async_byte_stream):
        response.stream = async_tee(response.stream, capture)
    elif not asynchronous and isinstance(response.stream, library.sync_byte_stream):
        response.stream = sync_tee(response.stream, capture)


def _observed(capture: _Capture, response: httpx.Response, *, asynchronous: bool) -> None:
    """`_observe`, behind the one boundary it needs: capture may never change the application's response (M6)."""
    try:
        _observe(capture, response, asynchronous=asynchronous)
    except Exception:  # noqa: BLE001, S110 - a response that cannot be tapped is a response not recorded
        pass


def _handle(
    send: Callable[[httpx.Request], httpx.Response], request: httpx.Request, settings: HajerSettings
) -> httpx.Response:
    capture = _begin(request, settings)
    try:
        response = send(request)
    except BaseException as error:
        if capture is not None:
            capture.finish(error)
        raise
    if capture is not None:
        _observed(capture, response, asynchronous=False)
    return response


async def _handle_async(
    send: Callable[[httpx.Request], Awaitable[httpx.Response]], request: httpx.Request, settings: HajerSettings
) -> httpx.Response:
    capture = _begin(request, settings)
    try:
        response = await send(request)
    except BaseException as error:
        if capture is not None:
            capture.finish(error)
        raise
    if capture is not None:
        _observed(capture, response, asynchronous=True)
    return response


class CaptureTransport(httpx.BaseTransport):
    """Wrap a caller-owned transport; constructing this is the explicit opt-in."""

    def __init__(self, transport: httpx.BaseTransport, *, settings: HajerSettings | None = None) -> None:
        self.transport = transport
        self.settings = settings if settings is not None else HajerSettings.from_env()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return _handle(self.transport.handle_request, request, self.settings)

    def close(self) -> None:
        self.transport.close()


class AsyncCaptureTransport(httpx.AsyncBaseTransport):
    """Asynchronous equivalent; never pre-reads a request or response stream."""

    def __init__(self, transport: httpx.AsyncBaseTransport, *, settings: HajerSettings | None = None) -> None:
        self.transport = transport
        self.settings = settings if settings is not None else HajerSettings.from_env()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await _handle_async(self.transport.handle_async_request, request, self.settings)

    async def aclose(self) -> None:
        await self.transport.aclose()


# ── HAJER_CAPTURE_HTTP: each HTTP library's own default transports ───────────────────────────────────

_MARK: Final[str] = "__hajer_http_capture__"
_SyncSend = Callable[[httpx.HTTPTransport, httpx.Request], httpx.Response]
_AsyncSend = Callable[[httpx.AsyncHTTPTransport, httpx.Request], Awaitable[httpx.Response]]


@dataclass(frozen=True, slots=True)
class _Replaced:
    """The two transport methods of one library that `HAJER_CAPTURE_HTTP` replaced."""

    library: HttpLibrary
    sync: _SyncSend
    asynchronous: _AsyncSend


#: What `HAJER_CAPTURE_HTTP` replaced, one entry per library, while it is installed.
_INSTALLED: list[_Replaced] = []
_LOCK = threading.Lock()


def _patch(library: HttpLibrary, settings: HajerSettings) -> _Replaced:
    sync_send = cast(_SyncSend, library.http_transport.handle_request)
    async_send = cast(_AsyncSend, library.async_http_transport.handle_async_request)

    def handle_request(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return _handle(lambda sent: sync_send(transport, sent), request, settings)

    async def handle_async_request(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return await _handle_async(lambda sent: async_send(transport, sent), request, settings)

    setattr(handle_request, _MARK, True)
    setattr(handle_async_request, _MARK, True)
    setattr(library.http_transport, "handle_request", handle_request)  # noqa: B010
    setattr(library.async_http_transport, "handle_async_request", handle_async_request)  # noqa: B010
    return _Replaced(library, sync_send, async_send)


def install_http_capture(settings: HajerSettings) -> None:
    """Patch every HTTP library's default transports once, when `HAJER_CAPTURE_HTTP` is on. Idempotent."""
    if not settings.capture_http:
        return
    with _LOCK:
        if _INSTALLED:
            return
        _INSTALLED.extend(_patch(library, settings) for library in libraries())


def uninstall_http_capture() -> None:
    """Put each library's transport methods back, unless something replaced them after us."""
    with _LOCK:
        replaced = list(_INSTALLED)
        _INSTALLED.clear()
        for item in replaced:
            sync_owner, async_owner = item.library.http_transport, item.library.async_http_transport
            if getattr(sync_owner.handle_request, _MARK, False):
                setattr(sync_owner, "handle_request", item.sync)  # noqa: B010
            if getattr(async_owner.handle_async_request, _MARK, False):
                setattr(async_owner, "handle_async_request", item.asynchronous)  # noqa: B010


add_installer(install_http_capture)

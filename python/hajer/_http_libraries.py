"""httpx and httpx2: the HTTP libraries an application's requests leave through, found in one place.

openai 3.x and anthropic 1.x send through `httpx2`, httpx's fork: the same surface under its own classes, so an
`httpx2.Request` is not an `httpx.Request` and a patch on `httpx.HTTPTransport` never sees it. Every seam that
patches or recognises httpx does the same, with the same semantics, to each library `libraries()` names: the replay
guard (`replay/_hooks.py`, `replay/_guard.py`), `HAJER_CAPTURE_HTTP` and `CaptureTransport` (`_http_capture.py`), and
`record_boundaries` (`_boundary.py`).

The SDK depends on httpx alone: httpx2 is not a dependency and is never imported by name. It is
imported here when the interpreter has it installed, so a seam is in place before the application's first request,
and not at all when it is absent. In a guarded child (`replay/_hooks.install`) that import happens after the audit
hook is added, so httpx2's import-time code runs under it. An httpx2 that is installed but fails to import is said
once, on the `hajer` logger, and then left out: nothing can send through it, so a guarded child refuses such a
request at the socket, and capture and `record_boundaries` do not see it.

`httpx2.alias_httpx()` makes `import httpx` answer with httpx2, and it must run before anything imports httpx. The SDK
imports httpx itself, so an application gets one library only by aliasing before its first `import hajer`; then
`libraries()` names it once. In a guarded child the SDK has imported httpx before any application code runs, so
`alias_httpx()` raises there (httpx2's own `RuntimeError`), and the application's httpx and httpx2 are two libraries,
each guarded.

The fork's classes are typed as httpx's, cast in `_fork` and nowhere else: every attribute the SDK reads is spelled
the same in both, and every object the SDK builds for a request is built from that request's own library.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import logging
from dataclasses import dataclass
from types import ModuleType
from typing import Final, cast

import httpx

#: httpx's fork, the library the current provider majors send through.
FORK: Final = "httpx2"
_LOG: Final = logging.getLogger("hajer")


@dataclass(frozen=True, slots=True)
class HttpLibrary:
    """One HTTP library: the classes of it the SDK patches, builds or recognises."""

    name: str
    http_transport: type[httpx.HTTPTransport]
    async_http_transport: type[httpx.AsyncHTTPTransport]
    client: type[httpx.Client]
    async_client: type[httpx.AsyncClient]
    request: type[httpx.Request]
    response: type[httpx.Response]
    byte_stream: type[httpx.ByteStream]
    sync_byte_stream: type[httpx.SyncByteStream]
    async_byte_stream: type[httpx.AsyncByteStream]
    connect_error: type[httpx.ConnectError]
    request_not_read: type[httpx.RequestNotRead]


HTTPX: Final = HttpLibrary(
    name="httpx",
    http_transport=httpx.HTTPTransport,
    async_http_transport=httpx.AsyncHTTPTransport,
    client=httpx.Client,
    async_client=httpx.AsyncClient,
    request=httpx.Request,
    response=httpx.Response,
    byte_stream=httpx.ByteStream,
    sync_byte_stream=httpx.SyncByteStream,
    async_byte_stream=httpx.AsyncByteStream,
    connect_error=httpx.ConnectError,
    request_not_read=httpx.RequestNotRead,
)


def _fork(module: ModuleType) -> HttpLibrary:
    """The fork's classes, typed as httpx's (see the module docstring): the one place that cast is made."""
    return HttpLibrary(
        name=module.__name__,
        http_transport=cast(type[httpx.HTTPTransport], module.HTTPTransport),
        async_http_transport=cast(type[httpx.AsyncHTTPTransport], module.AsyncHTTPTransport),
        client=cast(type[httpx.Client], module.Client),
        async_client=cast(type[httpx.AsyncClient], module.AsyncClient),
        request=cast(type[httpx.Request], module.Request),
        response=cast(type[httpx.Response], module.Response),
        byte_stream=cast(type[httpx.ByteStream], module.ByteStream),
        sync_byte_stream=cast(type[httpx.SyncByteStream], module.SyncByteStream),
        async_byte_stream=cast(type[httpx.AsyncByteStream], module.AsyncByteStream),
        connect_error=cast(type[httpx.ConnectError], module.ConnectError),
        request_not_read=cast(type[httpx.RequestNotRead], module.RequestNotRead),
    )


@functools.cache
def libraries() -> tuple[HttpLibrary, ...]:
    """httpx, then httpx2 when it is installed and is not httpx under another name. Found once per process."""
    try:
        if importlib.util.find_spec(FORK) is None:
            return (HTTPX,)
        module = importlib.import_module(FORK)
        if module is httpx or module.Client is httpx.Client:
            return (HTTPX,)
        return (HTTPX, _fork(module))
    except Exception as error:  # noqa: BLE001 - an httpx2 that cannot be imported is one nothing can send through either
        _LOG.warning(
            "hajer: %s is installed but could not be imported (%s), so it is left unpatched: a replay refuses a request "
            "through it at the socket, and HTTP capture and record_boundaries do not see it",
            FORK,
            type(error).__name__,
        )
        return (HTTPX,)


def library_of(value: object) -> HttpLibrary:
    """The library whose request or response `value` is; httpx for anything else."""
    if isinstance(value, httpx.Request | httpx.Response):
        return HTTPX
    return next((item for item in libraries() if isinstance(value, (item.request, item.response))), HTTPX)


def not_read_errors() -> tuple[type[httpx.RequestNotRead], ...]:
    """Each library's `RequestNotRead`: what reading a streamed request's body raises before it was read."""
    return tuple(item.request_not_read for item in libraries())

"""The in-process sandbox: the second layer, deciding every outbound byte, name lookup and process.

The first layer is the operating system (Hajer's replay runner isolates the child): the child has no network except
the parent's egress tunnels — one Unix socket per allow-listed endpoint — and cannot fork or exec. This
layer is what makes the *outcome* honest, because it sees what the application tried:

* **httpx and httpx2** (sync and async transports; `hajer._http_libraries`, the fork the current provider majors
  send through, with exactly the same semantics): a request to the declared provider (or, in the reporting phase,
  to Hajer) is sent through that endpoint's tunnel, whatever transport the application configured, and
  its request-body bytes and response status are recorded; a request matching a recorded boundary
  (method + URL + request-body digest) is answered from the recording and never leaves; a request whose
  method and URL were recorded with another body is NETWORK_UNRECORDED (production's answer to a different
  question is not this request's answer). A recording made before the SDK kept request digests is served
  on method + URL alone and the attempt says WEAK_BOUNDARY_MATCH. Anything else is refused before a byte
  leaves — a read (`GET`/`HEAD`/`OPTIONS`) as NETWORK_UNRECORDED, anything that could change the world as
  BLOCKED_EFFECT. Every answer, and every refusal, is the request's own library's (`SandboxRefused` is that
  library's `ConnectError`), so the application's client and its `except` see what they would see from a network.
* **sockets, names and processes** are decided in a CPython audit hook (`_hooks.py`), which fires for
  the C-level call too, so neither `_socket` nor a method taken off the socket's base class slips past it.

**A live CI run has no tunnel** (`direct`): a request to a declared model provider is sent through the application's
own transport, and while it is (`_Window`) a socket may connect to one place only — an address the provider's name
resolved to when the send began, through the guard's own lookup, at the provider's exact port. The request's URL
does not decide where its bytes go; the transport does, so the connect is what is checked. A proxy (the environment's
`HTTP(S)_PROXY` or a client's `proxy=`) that is not the provider itself, a Unix socket (`uds=`), a transport whose own
pool opens a socket elsewhere and a name that answers differently by the time the library connects are all refused
at that connect, naming the address they tried. A proxy that *is* the declared provider (a metered loopback proxy
declared as the provider's host and port) is the provider. The lookup is `_socket.getaddrinfo` as this module found it
on import, before any application code could replace it, and it is made once per send: a provider whose answers
rotate between the guard's lookup and the library's fails closed rather than widening the window.

Every event is appended to the egress log as it happens (`{host, port, allowed, bytes, served}`, `allowed`
meaning the bytes left, `served` meaning a recording answered it), so a run killed at its time limit keeps
the log it had. An allowed entry names where the bytes actually went — for a live send, the address of the
connection its own answer came over, or the tunnelled endpoint — never the host the request's URL spelled. A live
send is credited per send: its answer counts as the provider's only when the connection that answer came over is one
the audit hook admitted to this send's endpoint, whether this send opened it or reused it from the pool. A send whose
answer came over no connection, or over one admitted to another endpoint, is refused and never counted, however many
earlier sends reached the provider. After the application returns, `report()` switches to the reporting phase, where
only the Hajer endpoint is reachable and nothing is logged as application egress.

**What this layer cannot see (known limits).** It decides what goes through CPython's `socket` module, because that is
where the audit hook fires. A C-level network stack beside it does not: an event loop such as uvloop (libuv connects
without a `socket.connect` event), and extensions with their own sockets (grpcio, pycurl, librdkafka). In a live run
bytes sent that way can leave before anything here sees them; a provider request that went that way is refused
afterwards as `unconnected` and fails the attempt, and any other such traffic is neither refused nor logged. In the
hosted replay the operating-system layer still holds. Nor can an in-process guard resist the application deliberately
tampering with the guard's own objects (replacing a `Sandbox` method or this module's functions): what it holds
against is the application's ordinary configuration and libraries, not code written to defeat it.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import socket
from _socket import getaddrinfo as _getaddrinfo
from _socket import inet_pton as _inet_pton
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal, TypeAlias, cast

import httpx
from pydantic import JsonValue

from hajer._ci_spend import Spend
from hajer._http_libraries import HTTPX, HttpLibrary

Refusal: TypeAlias = Literal["BLOCKED_EFFECT", "NETWORK_UNRECORDED", "DB_REQUIRED"]
AsyncSend: TypeAlias = Callable[[httpx.AsyncHTTPTransport, httpx.Request], Awaitable[httpx.Response]]
SyncSend: TypeAlias = Callable[[httpx.HTTPTransport, httpx.Request], httpx.Response]
#: A name's addresses, as `(family, numeric address)`: the guard's own lookup (`system_addresses`) or a test's.
Resolver: TypeAlias = Callable[[str, int], Iterable[tuple[int, str]]]
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_INET = (socket.AF_INET, socket.AF_INET6)
#: The refused-host label of a live send whose answer came over no connection admitted to its endpoint.
UNCONNECTED = "unconnected"


class SandboxRefused(httpx.ConnectError):
    """Raised where the application's request would have left the process. Nothing was sent."""


@functools.cache
def refused_error(library: HttpLibrary) -> type[httpx.ConnectError]:
    """`SandboxRefused` as `library`'s own `ConnectError`: what an application catching its library's error catches."""
    if library.connect_error is httpx.ConnectError:
        return SandboxRefused
    namespace = {"__doc__": SandboxRefused.__doc__, "__module__": __name__, "__qualname__": "SandboxRefused"}
    return cast(type[httpx.ConnectError], type("SandboxRefused", (library.connect_error,), namespace))


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int
    tunnel: str | None = None
    #: `http` or `https`; empty means the port's own: https on 443, http on any other port.
    scheme: str = ""

    def reaches(self, url: httpx.URL) -> bool:
        """Whether a request to `url` is addressed here: host, port and scheme, exactly (`http://…:443` is not)."""
        scheme = self.scheme or ("https" if self.port == 443 else "http")
        return (self.host, self.port, scheme) == (url.host.lower(), _port(url), url.scheme)


@dataclass(slots=True)
class _Window:
    """One live send's permission, in the sending task's own context: its endpoint, the addresses the endpoint's name
    resolved to when the send began, and what the audit hook refused while it was open."""

    endpoint: Endpoint
    addresses: frozenset[tuple[int, bytes]]
    refused: list[str] = field(default_factory=lambda: list[str]())


#: The live send this task is making, while it makes it (`Sandbox.direct_open`, `Sandbox.connecting`).
_DIRECT: ContextVar[_Window | None] = ContextVar("hajer_replay_direct", default=None)


@dataclass(frozen=True, slots=True)
class Recording:
    status: int
    headers: dict[str, str]
    body: str
    request_digest: str | None = None


RecordingKey: TypeAlias = tuple[str, str, str, int, str, str]


def request_key(request: httpx.Request) -> RecordingKey:
    url = request.url
    query = url.query.decode("ascii", errors="replace")
    return (request.method.upper(), url.scheme, url.host.lower(), _port(url), url.path, query)


def _servable(entry: dict[str, JsonValue]) -> Recording | None:
    """A `hajer-boundary-v1` entry that can be answered whole, or None: streamed, cut or altered is not served."""
    body, digest, status = entry.get("body"), entry.get("bodyDigest"), entry.get("status")
    if entry.get("streamed") or entry.get("truncated") or not isinstance(body, str) or not isinstance(status, int):
        return None
    if digest != "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest():
        return None
    content_type = entry.get("contentType")
    headers = {"content-type": content_type} if isinstance(content_type, str) else {}
    requested = entry.get("requestBodyDigest")
    return Recording(status, headers, body, requested if isinstance(requested, str) else None)


def recordings_from(entries: JsonValue) -> dict[RecordingKey, list[Recording]]:
    """The case's recorded boundaries (`evidence.hajerBoundaryResponses.responses`), keyed as a request is."""
    recordings: dict[RecordingKey, list[Recording]] = {}
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict):
            continue
        recording = _servable(item)
        scheme, port = str(item.get("scheme", "")), item.get("port")
        if recording is None:
            continue
        key = (
            str(item.get("method", "")).upper(),
            scheme,
            str(item.get("host", "")).lower(),
            port if isinstance(port, int) else _DEFAULT_PORTS.get(scheme, 0),
            str(item.get("path", "")),
            str(item.get("query") or ""),
        )
        recordings.setdefault(key, []).append(recording)
    return recordings


@dataclass(slots=True)
class SandboxRecord:
    refusals: list[Refusal] = field(default_factory=lambda: list[Refusal]())
    weak_matches: int = 0
    provider_bodies: list[bytes] = field(default_factory=lambda: list[bytes]())
    provider_statuses: list[int] = field(default_factory=lambda: list[int]())
    #: The recorded answers served, in order: the replies a CI check on the model's tool calls reads.
    served_bodies: list[bytes] = field(default_factory=lambda: list[bytes]())


def _port(url: httpx.URL) -> int:
    return url.port or _DEFAULT_PORTS.get(url.scheme, 0)


def numeric(host: str) -> bool:
    return _packed(socket.AF_INET6 if ":" in host else socket.AF_INET, host) is not None


def _packed(family: int, host: object) -> tuple[int, bytes] | None:
    """`(family, the address's bytes)` for a numeric IPv4 or IPv6 address; None for anything else — a name, or a
    scoped `%zone` address, which fails closed. `inet_pton` is `_socket`'s as imported, so replacing `socket.inet_pton`
    or `_socket.inet_pton` later cannot make one address read as another (replacing this module's own functions is
    tampering with the guard, a known limit in the module docstring)."""
    if family not in _INET or not isinstance(host, str):
        return None
    try:
        return family, _inet_pton(family, host)
    except (OSError, ValueError):
        return None


def system_addresses(host: str, port: int) -> Iterable[tuple[int, str]]:
    """The guard's own lookup of a declared provider's name: `_socket.getaddrinfo` as this module imported it, before
    any application code ran, so neither `socket.getaddrinfo` nor `_socket.getaddrinfo` replaced later answers it. A
    name that does not resolve has no addresses, and every connect in its send is refused."""
    try:
        answers = _getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return ()
    return [(family, str(address[0])) for family, _, _, _, address in answers]


def _descriptor(sock: object) -> int | None:
    """A socket's file descriptor, or None: what identifies one connection from its connect to the answer that came over
    it, through a TLS wrapper or an event loop's transport alike."""
    fileno = getattr(sock, "fileno", None)
    try:
        descriptor = fileno() if callable(fileno) else None
    except (OSError, ValueError):
        return None
    return descriptor if isinstance(descriptor, int) and descriptor >= 0 else None


def answered_over(response: httpx.Response) -> int | None:
    """The descriptor of the connection `response` came over, as its transport reports it (httpcore's and httpcore2's
    `network_stream`, HTTP/1.1 and HTTP/2, sync and async), or None when it names none."""
    stream: object = response.extensions.get("network_stream")
    extra = getattr(stream, "get_extra_info", None)
    if not callable(extra):
        return None
    try:
        sock: object = extra("socket")
    except Exception:  # noqa: BLE001 - the application's transport answers this; whatever it raises names no connection
        return None
    return _descriptor(sock)


def address_label(address: object) -> tuple[str, int]:
    """How the egress log names a socket address: its host and port, or the whole address (a Unix path) and 0."""
    if isinstance(address, tuple) and len(address) >= 2:  # pyright: ignore[reportUnknownArgumentType]
        host, port = address[0], address[1]  # pyright: ignore[reportUnknownVariableType]
        return str(host), port if isinstance(port, int) else 0  # pyright: ignore[reportUnknownArgumentType]
    return f"{address!r}", 0


class Sandbox:
    def __init__(
        self,
        *,
        provider: Endpoint | None,
        hajer: Endpoint | None,
        recordings: dict[RecordingKey, list[Recording]],
        egress_log: Path,
        observer: Callable[[httpx.Request], None] | None = None,
        direct: tuple[Endpoint, ...] = (),
        resolve: Resolver = system_addresses,
        spend: Spend | None = None,
    ) -> None:
        self._provider = provider
        self._hajer = hajer
        #: A live CI run's model providers, reached directly (no tunnel exists in customer CI): the one place a byte
        #: may go, and only at the addresses its name resolved to, while this guard is sending to it (`_Window`).
        self._direct = direct
        self._resolve = resolve
        self._spend = spend
        #: Every live connection the audit hook admitted, by descriptor: the endpoint it was admitted to and the address
        #: it connected to. A send is credited only through the connection its own answer came over (`_destination`).
        self._connections: dict[int, tuple[Endpoint, str, int]] = {}
        self._recordings = recordings
        self._log: IO[str] = egress_log.open("a", encoding="utf-8")
        self._reporting = False
        self._in_send = 0
        #: Told of every model request the application makes, answered from a recording or sent to the provider,
        #: while it is being made: `verify-adapters` reads the application frames that made it (`_reach`).
        self._observer = observer
        self.record = SandboxRecord()

    # ── the log and the refusals ─────────────────────────────────────────────────────────────────

    def _egress(self, host: str, port: int, *, allowed: bool, sent: int, served: bool = False) -> None:
        if self._reporting:
            return
        entry = {"host": host, "port": port, "allowed": allowed, "bytes": sent, "served": served}
        self._log.write(json.dumps(entry) + "\n")
        self._log.flush()

    def refuse(self, refusal: Refusal, host: str, port: int) -> None:
        self.record.refusals.append(refusal)
        self._egress(host, port, allowed=False, sent=0)

    def report(self) -> None:
        """The application is done: from here only the Hajer endpoint is reachable, and it is not egress."""
        self._reporting = True

    # ── what may leave ───────────────────────────────────────────────────────────────────────────

    def _endpoint(self, url: httpx.URL) -> Endpoint | None:
        target = self._hajer if self._reporting else self._provider
        return target if target is not None and target.reaches(url) else None

    def _direct_endpoint(self, url: httpx.URL) -> Endpoint | None:
        if self._reporting:
            return None
        return next((item for item in self._direct if item.reaches(url)), None)

    def direct_open(self) -> bool:
        """Whether this task is sending to a direct endpoint. The permission lives in the sending task's own context,
        so nothing else in the process may use the moment."""
        return _DIRECT.get() is not None

    def resolvable(self, host: str) -> bool:
        """Whether a name may be looked up: only a declared direct provider's own, from any thread. An async client
        resolves it in the event loop's executor thread, which does not carry the sending task's context; looking up
        the provider's own name carries no application data, and every other lookup stays refused."""
        return host.lower() in {item.host for item in self._direct}

    def tunnel_open(self, address: object) -> bool:
        """A socket may connect only to an endpoint's tunnel, and only while this guard is sending on it."""
        if self._in_send == 0 or not isinstance(address, str | bytes):
            return False
        path = address.decode(errors="replace") if isinstance(address, bytes) else address
        return any(item is not None and item.tunnel == path for item in (self._provider, self._hajer))

    def connecting(self, sock: object, address: object) -> str | None:
        """The audit hook's `socket.connect` decision: None lets it through; otherwise the refusal is recorded here and
        its message returned.

        Inside a live send only an `AF_INET`/`AF_INET6` address the endpoint's name resolved to, at the endpoint's exact
        port, is let through — never a Unix socket, a name, or any other address or port, whatever the request's URL
        said. Outside one, only an endpoint's tunnel while this guard sends on it."""
        window, descriptor = _DIRECT.get(), _descriptor(sock)
        host, port = address_label(address)
        if descriptor is not None:  # a descriptor reused after its socket closed carries no earlier admission
            self._connections.pop(descriptor, None)
        if window is None:
            if self.tunnel_open(address):
                return None
            self.refuse("BLOCKED_EFFECT", host, port)
            return "HAJER_REPLAY: connection refused in the sandbox"
        family = getattr(sock, "family", None)
        target = _packed(int(family), host) if isinstance(family, int) and isinstance(address, tuple) else None
        if (
            target is not None
            and target in window.addresses
            and port == window.endpoint.port
            and descriptor is not None
        ):
            self._connections[descriptor] = (window.endpoint, host, port)
            return None
        window.refused.append(f"{host}:{port}")
        self.refuse("BLOCKED_EFFECT", host, port)
        where = f"{window.endpoint.host}:{window.endpoint.port}"
        return f"HAJER_REPLAY: a connection to {host}:{port} was refused: it is not the provider endpoint {where}"

    def _window(self, endpoint: Endpoint) -> _Window:
        """A live send's permission, with the endpoint's addresses resolved once, now, by the guard's own lookup."""
        addresses = frozenset(
            packed
            for family, address in self._resolve(endpoint.host, endpoint.port)
            if (packed := _packed(family, address)) is not None
        )
        return _Window(endpoint, addresses)

    async def _window_async(self, endpoint: Endpoint) -> _Window:
        """`_window`, looked up off the event loop when there is one (`resolvable` admits the name from any thread), in
        the loop's default executor: the application's to replace, like the rest of the process (a known limit)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # another event loop (trio): the lookup blocks it once per send
            return self._window(endpoint)
        return await loop.run_in_executor(None, self._window, endpoint)

    def _recorded(self, request: httpx.Request, body: bytes, library: HttpLibrary) -> httpx.Response | Refusal | None:
        """The recording for this exact request; NETWORK_UNRECORDED when only another body was recorded."""
        queue = self._recordings.get(request_key(request))
        if not queue or self._reporting:
            return None
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        chosen = next((item for item in queue if item.request_digest == digest), None)
        if chosen is None:
            chosen = next((item for item in queue if item.request_digest is None), None)
            if chosen is None:
                return "NETWORK_UNRECORDED"
            self.record.weak_matches += 1
        queue.remove(chosen)
        if self._observer is not None:
            self._observer(request)
        self.record.served_bodies.append(chosen.body.encode())
        self._egress(request.url.host, _port(request.url), allowed=False, sent=0, served=True)
        return library.response(chosen.status, headers=chosen.headers, content=chosen.body.encode(), request=request)

    def _refused(
        self, request: httpx.Request, library: HttpLibrary, refusal: Refusal | None = None
    ) -> httpx.ConnectError:
        if refusal is None:
            refusal = "NETWORK_UNRECORDED" if request.method.upper() in SAFE_METHODS else "BLOCKED_EFFECT"
        self.refuse(refusal, request.url.host, _port(request.url))
        message = f"HAJER_REPLAY: {refusal} {request.method} {request.url.host}"
        return refused_error(library)(message, request=request)

    def _route(self, request: httpx.Request, body: bytes, library: HttpLibrary) -> Endpoint | httpx.Response:
        """The endpoint to send to (directly when it has no tunnel), or the recorded answer; anything else raises."""
        direct = self._direct_endpoint(request.url)
        if direct is not None:
            return direct
        endpoint = self._endpoint(request.url)
        if endpoint is not None and endpoint.tunnel is not None:
            return endpoint
        served = None if endpoint is not None else self._recorded(request, body, library)
        if served is None or isinstance(served, str):
            raise self._refused(request, library, served)
        return served

    def _destination(
        self, request: httpx.Request, window: _Window, connection: int | None, library: HttpLibrary
    ) -> tuple[str, int]:
        """Where this live send's bytes went: the address of the connection its own answer came over (`connection`, as
        `answered_over` read it), whether this send opened it or reused it from the pool, when the audit hook admitted
        it to this send's endpoint. Anything else — a refused connect in this send, an answer over no connection or over
        one admitted elsewhere — is refused as that library's `ConnectError`, naming what it reached; its answer is not
        the provider's, whatever an earlier send reached."""
        admitted = None if connection is None else self._connections.get(connection)
        if window.refused or admitted is None or admitted[0] != window.endpoint:
            if not window.refused:
                self.refuse("BLOCKED_EFFECT", UNCONNECTED, 0)
            raise self._diverted(request, window, library)
        return admitted[1], admitted[2]

    def _diverted(self, request: httpx.Request, window: _Window, library: HttpLibrary) -> httpx.ConnectError:
        reached = ", ".join(window.refused) or UNCONNECTED
        where = f"{window.endpoint.host}:{window.endpoint.port}"
        message = f"HAJER_REPLAY: BLOCKED_EFFECT {request.method} {request.url.host} reached {reached}, not {where}"
        return refused_error(library)(message, request=request)

    async def send_async(
        self,
        request: httpx.Request,
        inner: AsyncSend,
        own: httpx.AsyncHTTPTransport | None = None,
        *,
        library: HttpLibrary = HTTPX,
    ) -> httpx.Response:
        """Send one request the application's `library` transport was asked to, as this sandbox decides."""
        body = await request.aread()
        route = self._route(request, body, library)
        if not isinstance(route, Endpoint):
            return route
        if route.tunnel is None:
            if own is None:  # a live send goes through the application's own transport or not at all
                raise self._refused(request, library)
            reservation = self._spend.reserve(request, body) if self._spend else None
            window = await self._window_async(route)
            token = _DIRECT.set(window)
            try:
                response = await inner(own, request)
                connection = answered_over(response)  # before the body: a closed connection names no descriptor
                raw = b"".join([part async for part in response.aiter_raw()])
                await response.aclose()
            except Exception as error:
                if window.refused:
                    raise self._diverted(request, window, library) from error
                raise
            finally:
                _DIRECT.reset(token)
            destination = self._destination(request, window, connection, library)
            if self._spend and reservation:
                decoded = library.response(response.status_code, headers=response.headers, content=raw).content
                self._spend.settle(reservation, response.status_code, decoded)
            return self._sent(request, response, body, raw, library, destination)
        self._in_send += 1
        try:
            async with library.async_http_transport(uds=route.tunnel) as transport:
                response = await inner(transport, request)
                raw = b"".join([part async for part in response.aiter_raw()])
                await response.aclose()
        finally:
            self._in_send -= 1
        return self._sent(request, response, body, raw, library, (route.host, route.port))

    def send_sync(
        self,
        request: httpx.Request,
        inner: SyncSend,
        own: httpx.HTTPTransport | None = None,
        *,
        library: HttpLibrary = HTTPX,
    ) -> httpx.Response:
        """`send_async`, for a synchronous transport."""
        body = request.read()
        route = self._route(request, body, library)
        if not isinstance(route, Endpoint):
            return route
        if route.tunnel is None:
            if own is None:  # a live send goes through the application's own transport or not at all
                raise self._refused(request, library)
            reservation = self._spend.reserve(request, body) if self._spend else None
            window = self._window(route)
            token = _DIRECT.set(window)
            try:
                response = inner(own, request)
                connection = answered_over(response)  # before the body: a closed connection names no descriptor
                raw = b"".join(response.iter_raw())
                response.close()
            except Exception as error:
                if window.refused:
                    raise self._diverted(request, window, library) from error
                raise
            finally:
                _DIRECT.reset(token)
            destination = self._destination(request, window, connection, library)
            if self._spend and reservation:
                decoded = library.response(response.status_code, headers=response.headers, content=raw).content
                self._spend.settle(reservation, response.status_code, decoded)
            return self._sent(request, response, body, raw, library, destination)
        self._in_send += 1
        try:
            with library.http_transport(uds=route.tunnel) as transport:
                response = inner(transport, request)
                raw = b"".join(response.iter_raw())
                response.close()
        finally:
            self._in_send -= 1
        return self._sent(request, response, body, raw, library, (route.host, route.port))

    def _sent(
        self,
        request: httpx.Request,
        response: httpx.Response,
        body: bytes,
        raw: bytes,
        library: HttpLibrary,
        destination: tuple[str, int],
    ) -> httpx.Response:
        """Record one answered send: the provider's status and body, and the egress to where its bytes went."""
        if not self._reporting and self._observer is not None:
            self._observer(request)
        if not self._reporting:
            self.record.provider_statuses.append(response.status_code)
            self.record.provider_bodies.append(
                library.response(response.status_code, headers=response.headers, content=raw).content
            )
        self._egress(*destination, allowed=True, sent=len(body))
        return library.response(
            response.status_code,
            headers=response.headers,
            stream=library.byte_stream(raw),
            extensions=response.extensions,
            request=request,
        )

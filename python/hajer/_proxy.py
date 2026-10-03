"""An opt-in wire proxy: the exchange itself as evidence, beside the SDK's self-report.

Every observation Hajer holds today is a **self-report**: the customer's own process says what it asked a provider
and what came back, and Hajer believes it. That is the right default — it needs no credential of ours, no
traffic through us and no trust from the customer — and it is exactly as strong as the process reporting it.

This is the other option, and the customer chooses it per process:

    python -m hajer._proxy --upstream https://api.anthropic.com --listen 127.0.0.1:8091
    ANTHROPIC_BASE_URL=http://127.0.0.1:8091 python their_workflow.py

Every exchange through it is recorded with `origin: BROKER` — read at the wire rather than reported by the
application — and the readiness sentence says what that is and is not worth: **a broker receipt witnesses
the exchange at the wire, not the application's use of it.** A proxy cannot tell you what the application
did with the answer.

**The credential is the customer's and stays theirs.** Their own `x-api-key` / `authorization` header is
forwarded to the upstream unchanged and is never in a recorded header, a captured body or a log line. Hajer
holds no provider credential for this and could not make a call of its own through it.

**Loopback only, and any other bind is refused by name.** A proxy holding a customer's provider credentials
on a routable interface is a credential-stealing endpoint somebody else can reach. `--listen 0.0.0.0:8091`
is refused with the sentence that says why rather than bound and warned about.

**What the path may be.** `PATH_INFO` that is absolute (`http://…`), protocol-relative (`//host/…`) or
resolves off the upstream host is refused: a proxy that forwarded those is an open relay, and an open relay
inside somebody's network is a worse problem than an unobserved provider call.

**Header policy is an allowlist**, because a denylist is a list of the headers somebody thought of.
`content-type`, `accept`, `anthropic-version`, `anthropic-beta` and `user-agent` go up; the credential goes
up and is never recorded; `accept-encoding` is stripped so the body this records is the body the provider
meant. On the way back `content-length` and `content-encoding` are dropped, because the bytes a WSGI server
re-frames are not the bytes those two describe.

**Streams pass through.** A server-sent-event response is yielded chunk by chunk, unbuffered, so a customer's
own streaming UI still streams; the recorded body is the stream as it went past.

**Content is recorded only with `HAJER_CAPTURE_CONTENT=1`**, the same switch `wrap()` obeys. Without it the
observation carries the exchange's shape — method, path, status, timing, usage — and no message text.
"""

from __future__ import annotations

import argparse
import json
import socketserver
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol, cast
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import httpx

from hajer._json import JsonObject, JsonValue
from hajer._settings import HajerSettings

#: The request headers that go up. An allowlist, because a denylist is a list of the headers somebody
#: thought of. `accept-encoding` is deliberately absent: the recorded body should be the body the provider
#: meant, not a compressed frame this process would have to decode to read.
FORWARDED_HEADERS: Final[frozenset[str]] = frozenset(
    {"content-type", "accept", "anthropic-version", "anthropic-beta", "user-agent"}
)

#: The customer's own credential headers. Forwarded to the upstream and **never** recorded or logged.
CREDENTIAL_HEADERS: Final[frozenset[str]] = frozenset({"x-api-key", "authorization", "api-key"})

#: Response headers this proxy does not pass on: the two that describe bytes the WSGI server re-frames, and
#: the hop-by-hop ones a proxy owns rather than forwards.
DROPPED_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}
)

#: On every response this proxy produced, so a customer reading their own traffic can tell.
PROXY_MARKER: Final[tuple[str, str]] = ("x-hajer-proxy", "1")

#: The error `type` an upstream failure answers with. Structured, and named after this proxy, so a client
#: that retries on provider errors can tell "the provider refused" from "the thing in front of it did".
UPSTREAM_ERROR_TYPE: Final[str] = "hajer_proxy_upstream"

#: The one interface this may bind. A proxy holding a customer's provider credential on a routable address
#: is a credential-stealing endpoint somebody else can reach.
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

#: The provider wires this proxy can label a capture with, by the path the request came in on.
_WIRES: Final[tuple[tuple[str, str], ...]] = (
    ("/v1/messages", "ANTHROPIC_MESSAGES"),
    ("/v1/chat/completions", "OPENAI_CHAT_COMPLETIONS"),
    ("/v1/responses", "OPENAI_RESPONSES"),
)

#: What an exchange whose path matches no known wire is labelled. Recorded, never guessed at.
_UNKNOWN_WIRE: Final[str] = "ANTHROPIC_MESSAGES"


#: A WSGI environment: a mapping of strings to **objects**, one of which is a byte stream. Typed as it is
#: rather than as `Mapping[str, str]`, so every read narrows before it is used.
Environ = Mapping[str, object]

#: One WSGI application, as this module builds and as a test drives.
Application = Callable[[Environ, Callable[[str, list[tuple[str, str]]], object]], Iterable[bytes]]


class ProxyRefusedError(ValueError):
    """A configuration this proxy will not run, named. Raised before the socket, never during a request."""


class Recorder(Protocol):
    """What the proxy hands each exchange to. One method, so a test can be a list."""

    def record_exchange(self, exchange: Exchange) -> None:  # pragma: no cover - a Protocol body
        """Record one proxied exchange. Must not raise: a recording failure is not the customer's problem."""


@dataclass(frozen=True, slots=True)
class ProxyPolicy:
    """The knobs a customer may turn, and the two defaults that are not knobs.

    `timeout_s` is the upstream read timeout. `strip_headers` is **additive** to the allowlist's complement:
    a header a customer names here does not go up even if it is on the allowlist. `pass_headers` is the other
    direction, for a provider header this SDK version has not heard of.

    Loopback-only and credential-never-recorded are not on this record, for the same reason credentials are
    not on `RedactionPolicy`: they are not negotiable.
    """

    timeout_s: float
    strip_headers: frozenset[str] = frozenset()
    pass_headers: frozenset[str] = frozenset()
    capture_content: bool = False

    def forwarded(self) -> frozenset[str]:
        """The headers that actually go up for this policy."""
        return (FORWARDED_HEADERS | self.pass_headers) - self.strip_headers


@dataclass(frozen=True, slots=True)
class Exchange:
    """One proxied request and its answer, as the recorder receives it.

    `request_headers` and `response_headers` never contain a credential header — they are filtered before this
    value exists, not when it is read.

    **The bodies are here whether or not they may be recorded**, and `captured` is the difference. The proxy
    has them in memory anyway: it just forwarded them. What `HAJER_CAPTURE_CONTENT` decides is whether they may
    be *sent to Hajer*, and keeping that decision on this value rather than at the read is what lets one thing
    that is not content — the token usage — be reported at either setting. Usage is what the call cost, not
    what it said.
    """

    method: str
    path: str
    status: int
    started_at: str
    duration_ms: int
    request_headers: Mapping[str, str]
    response_headers: Mapping[str, str]
    request_body: bytes = b""
    response_body: bytes = b""
    streamed: bool = False
    failure: str = ""
    captured: bool = False

    @property
    def wire(self) -> str:
        """Which provider wire this path is, for the capture's own label."""
        for prefix, wire in _WIRES:
            if self.path.startswith(prefix):
                return wire
        return _UNKNOWN_WIRE

    def answer(self) -> JsonObject | None:
        """The response body as an object, when it is one. Read here, sent only when `captured`."""
        return _json_object(self.response_body) if self.response_body else None

    def usage(self) -> JsonObject:
        """The token counts the provider reported, at **either** capture setting.

        Usage is not content: it is what the call cost. Withholding it would make a proxy somebody turned on to
        see their own spending unable to report spending, which is the one thing a broker capture is
        unambiguously good for.
        """
        answered = self.answer()
        reported = answered.get("usage") if answered is not None else None
        if not isinstance(reported, dict):
            return {}
        counted: JsonObject = {name: count for name, count in reported.items() if isinstance(count, int)}
        return counted

    def request_document(self) -> JsonObject:
        """The request as an object, or the shape of it when content capture is off.

        With capture off this is the *metadata* of the exchange: the method, the path and the byte count. It is
        deliberately not an empty object — an observation of "a call happened" with no shape at all would be a
        row nobody can read — and deliberately not the body.
        """
        if self.request_body and self.captured:
            parsed = _json_object(self.request_body)
            if parsed is not None:
                return parsed
        return {"method": self.method, "path": self.path, "requestBytes": len(self.request_body)}

    def output_document(self) -> JsonObject:
        """What came back: the shape and the usage always, the answer only when content capture is on."""
        document: JsonObject = {"status": self.status, "durationMs": self.duration_ms, "streamed": self.streamed}
        usage = self.usage()
        if usage:
            document["usage"] = dict(usage)
        if self.failure:
            document["failure"] = self.failure
        answered = self.answer()
        if answered is not None and self.captured:
            document["answer"] = answered
        return document

    def capture(self) -> JsonObject:
        """This exchange as one `wrappedCalls` entry — a **capture** when content may be sent, else a summary.

        The two shapes are the SDK's own and they are disjoint on the wire. A capture is the provider's own
        request and response bytes, which the service reads itself into a `ModelCallReceipt`: that is what makes
        a broker receipt worth more than a self-report, and it is also the only shape that can be priced
        (`cost_source: LOCAL_PRICED`), because the price comes from the tokens *the service* read.

        With content capture off there are no bytes to send, so the summary goes instead: provider, api, model,
        usage, timing, and a limitation that says a receipt cannot be minted from it. A customer who wants
        broker-grade receipts turns `HAJER_CAPTURE_CONTENT` on, and that is the honest trade to make them make.
        """
        if not self.captured:
            return self.summary()
        return {
            "wire": self.wire,
            "request": self.request_document(),
            "responseBody": self.response_body.decode("utf-8", "replace"),
            "responseStatus": self.status,
            "startedAt": self.started_at,
            "durationMs": self.duration_ms,
            "streamed": self.streamed,
            "truncated": False,
        }

    def summary(self) -> JsonObject:
        """The described shape: what the call was, with no bytes of it. `WrappedCallSummaryIn` on the wire."""
        answered = self.answer()
        model = answered.get("model") if answered is not None else None
        anthropic = self.wire == "ANTHROPIC_MESSAGES"
        return {
            "provider": "anthropic" if anthropic else "openai",
            "api": "messages" if anthropic else "chat.completions",
            "startedAt": self.started_at,
            "durationMs": self.duration_ms,
            "model": model if isinstance(model, str) else None,
            "usage": dict(self.usage()),
            "streamed": self.streamed,
            "error": self.failure or None,
            "limitations": [
                "Read at the wire by `hajer proxy` with content capture off, so the provider's own bytes were "
                "not sent and no model-call receipt can be minted from this."
            ],
        }


def _json_object(raw: bytes) -> JsonObject | None:
    try:
        parsed: JsonValue = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def refuse_non_loopback(host: str) -> None:
    """Raise unless this is an address only this machine can reach, and say which one was asked for."""
    if host not in LOOPBACK_HOSTS:
        raise ProxyRefusedError(
            f"--listen {host} is not a loopback address. This proxy forwards a customer's own provider "
            "credential, so binding it anywhere another machine can reach makes it a credential-stealing "
            f"endpoint. Use one of {', '.join(sorted(LOOPBACK_HOSTS))}, and tunnel if something else has to "
            "reach it."
        )


def refuse_foreign_path(path: str) -> None:
    """Raise for a path that would make this an open relay, and name which shape it was."""
    if path.startswith("//"):
        raise ProxyRefusedError(
            f"the request path {path!r} is protocol-relative, which names another host. This proxy forwards "
            "to one upstream and refuses to be a general relay."
        )
    if "://" in path:
        raise ProxyRefusedError(
            f"the request path {path!r} is an absolute URL, which names its own host. This proxy forwards to "
            "one upstream and refuses to be a general relay."
        )
    if not path.startswith("/"):
        raise ProxyRefusedError(f"the request path {path!r} is not rooted at /; this proxy forwards paths, not hosts.")


def _request_headers(environ: Environ, policy: ProxyPolicy) -> tuple[dict[str, str], dict[str, str]]:
    """What goes up, and what is safe to record. The credential is in the first and never in the second."""
    arriving: dict[str, str] = {
        key[5:].replace("_", "-").lower(): value
        for key, value in environ.items()
        if key.startswith("HTTP_") and isinstance(value, str)
    }
    content_type = _text(environ, "CONTENT_TYPE")
    if content_type:
        arriving["content-type"] = content_type
    allowed = policy.forwarded()
    upstream = {name: value for name, value in arriving.items() if name in allowed or name in CREDENTIAL_HEADERS}
    recorded = {name: value for name, value in upstream.items() if name not in CREDENTIAL_HEADERS}
    return upstream, recorded


def _response_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """The provider's headers minus the ones a proxy owns, plus the marker that says who answered."""
    kept = [(name, value) for name, value in headers if name.lower() not in DROPPED_RESPONSE_HEADERS]
    return [*kept, PROXY_MARKER]


def _upstream_error(status: int, message: str) -> bytes:
    """The structured body an upstream failure answers with (accepted DX row)."""
    return json.dumps({"error": {"type": UPSTREAM_ERROR_TYPE, "status": status, "message": message}}).encode("utf-8")


def _streaming(headers: Mapping[str, str]) -> bool:
    """Is this a server-sent-event stream? Then it passes through unbuffered."""
    return "text/event-stream" in headers.get("content-type", "").lower()


@dataclass(slots=True)
class ListRecorder:
    """A recorder that keeps the exchanges in memory. What a test drives, and what `main` uses when this
    process has no Hajer credentials at all — an inert client records nothing, and keeping the exchanges is
    better than dropping them silently."""

    exchanges: list[Exchange] = field(default_factory=list)

    def record_exchange(self, exchange: Exchange) -> None:
        self.exchanges.append(exchange)


#: The origin a proxied exchange is recorded under. Contract v0's own member: Hajer read the bytes at the
#: wire rather than being told about them by the process that made the call.
BROKER_ORIGIN: Final[str] = "BROKER"


@dataclass(slots=True)
class ObserveRecorder:
    """Submits each exchange as an `observe` observation with `origin: BROKER`.

    The one recorder the `proxy` command runs with. It holds a `Hajer` client and nothing else: the exchange
    becomes a request half (what went up), an output half (what came back) and one **capture** — the
    provider's own bytes, which the service reads itself into a `ModelCallReceipt` instead of taking a
    summary of them. That is the whole difference between a broker receipt and a self-report.

    **It never raises into the request path.** A recording failure is this proxy's problem, not the
    customer's: their provider call already succeeded, and an exception here would turn an observation into a
    failed API call.
    """

    client: object
    submitted: int = 0
    failures: list[str] = field(default_factory=list)

    def record_exchange(self, exchange: Exchange) -> None:
        observe = getattr(self.client, "observe_exchange", None)
        if not callable(observe):  # pragma: no cover - the client is a `Hajer`
            return
        try:
            observe(
                request=exchange.request_document(),
                output=exchange.output_document(),
                capture=exchange.capture(),
                origin=BROKER_ORIGIN,
            )
        except Exception as failure:  # noqa: BLE001 - a recording failure is never the customer's problem
            self.failures.append(f"{type(failure).__name__}: {failure}")
            return
        self.submitted += 1


def build_app(
    *, upstream: str, recorder: Recorder, policy: ProxyPolicy, client: httpx.Client | None = None
) -> Application:
    """One WSGI application that forwards to `upstream` and records what went past.

    `client` is the seam a test uses: an `httpx.Client` on a `MockTransport` answers without a socket, which
    is what keeps this module's tests inside the SDK's own rule that nothing here opens one.
    """
    forwarder = client if client is not None else httpx.Client(base_url=upstream, timeout=policy.timeout_s)

    def application(
        environ: Environ, start_response: Callable[[str, list[tuple[str, str]]], object]
    ) -> Iterable[bytes]:
        method = _text(environ, "REQUEST_METHOD", "GET")
        path = _text(environ, "PATH_INFO", "/")
        query = _text(environ, "QUERY_STRING")
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        try:
            refuse_foreign_path(path)
        except ProxyRefusedError as refused:
            body = _upstream_error(400, str(refused))
            start_response("400 Bad Request", [("content-type", "application/json"), PROXY_MARKER])
            return [body]
        upstream_headers, recorded_headers = _request_headers(environ, policy)
        request_body = _read_body(environ)
        target = f"{path}?{query}" if query else path
        try:
            response = forwarder.request(method, target, content=request_body, headers=upstream_headers)
        except httpx.HTTPError as failure:
            message = f"{type(failure).__name__}: {failure}"
            recorder.record_exchange(
                Exchange(
                    method=method,
                    path=path,
                    status=502,
                    started_at=started_at,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    request_headers=recorded_headers,
                    response_headers={},
                    request_body=request_body,
                    captured=policy.capture_content,
                    failure=message,
                )
            )
            body = _upstream_error(502, message)
            start_response("502 Bad Gateway", [("content-type", "application/json"), PROXY_MARKER])
            return [body]
        headers = {name.lower(): value for name, value in response.headers.items()}
        start_response(f"{response.status_code} {response.reason_phrase}", _response_headers(response.headers.items()))
        if _streaming(headers):
            return _streamed(
                response,
                recorder=recorder,
                policy=policy,
                method=method,
                path=path,
                started=started,
                started_at=started_at,
                request_body=request_body,
                recorded_headers=recorded_headers,
                response_headers=headers,
            )
        recorder.record_exchange(
            Exchange(
                method=method,
                path=path,
                status=response.status_code,
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                request_headers=recorded_headers,
                response_headers=headers,
                request_body=request_body,
                response_body=response.content,
                captured=policy.capture_content,
            )
        )
        return [response.content]

    return application


def _read_body(environ: Environ) -> bytes:
    """The request body, bounded by the `content-length` the caller declared.

    A WSGI environ is a mapping of *objects* — one of them is a byte stream — so every read here narrows
    before it uses, which is the same rule the SDK applies to anything read off a provider object.
    """
    stream = environ.get("wsgi.input")
    reader = getattr(stream, "read", None)
    if not callable(reader):  # pragma: no cover - a WSGI server always supplies one
        return b""
    try:
        wanted = int(_text(environ, "CONTENT_LENGTH") or "0")
    except ValueError:  # pragma: no cover - a malformed length reads as no body
        return b""
    body = cast("Callable[[int], object]", reader)(wanted) if wanted > 0 else b""
    return body if isinstance(body, bytes) else b""


def _text(environ: Environ, key: str, default: str = "") -> str:
    """One environ value as the string it is, or the default when it is anything else."""
    value = environ.get(key, default)
    return value if isinstance(value, str) else default


def _streamed(
    response: httpx.Response,
    *,
    recorder: Recorder,
    policy: ProxyPolicy,
    method: str,
    path: str,
    started: float,
    started_at: str,
    request_body: bytes,
    recorded_headers: Mapping[str, str],
    response_headers: Mapping[str, str],
) -> Iterator[bytes]:
    """Yield the stream as it arrives, then record it. Nothing is buffered to record it.

    The recording happens **after** the last chunk, which is the only honest moment: a stream's own answer is
    not known until it ends, and a receipt written at the first chunk would describe a call still in progress.
    """
    seen: list[bytes] = []
    for chunk in response.iter_bytes():
        seen.append(chunk)
        yield chunk
    recorder.record_exchange(
        Exchange(
            method=method,
            path=path,
            status=response.status_code,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            request_headers=recorded_headers,
            response_headers=response_headers,
            request_body=request_body,
            response_body=b"".join(seen),
            captured=policy.capture_content,
            streamed=True,
        )
    )


class _ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    """One thread per request, because a provider round trip is long and a stream is longer.

    Single-threaded, one slow stream would block every other call the customer's process makes — through a
    proxy they turned on to observe themselves. Daemon threads so Ctrl-C actually stops it.
    """

    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    """A request handler that logs nothing to stderr.

    Not tidiness: a WSGI access log line carries the path and the query string of a customer's provider
    traffic, and this process is one they turned on to *reduce* what leaves their machine.
    """

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def serve(
    *, upstream: str, host: str, port: int, recorder: Recorder, policy: ProxyPolicy
) -> WSGIServer:  # pragma: no cover - the socket half; `build_app` is what the tests drive
    """Bind and return the server. Loopback only; the caller runs `serve_forever`."""
    refuse_non_loopback(host)
    return make_server(
        host, port, build_app(upstream=upstream, recorder=recorder, policy=policy), _ThreadingWSGIServer, _QuietHandler
    )


def policy_from(settings: HajerSettings, *, strip: Sequence[str] = (), keep: Sequence[str] = ()) -> ProxyPolicy:
    """The policy this process runs under: the settings, plus whatever the flags added."""
    return ProxyPolicy(
        timeout_s=float(settings.proxy_timeout_s),
        strip_headers=frozenset(name.lower() for name in strip),
        pass_headers=frozenset(name.lower() for name in keep),
        capture_content=settings.capture_content,
    )


def _recorder(settings: HajerSettings) -> Recorder:  # pragma: no cover - the process entry point's own seam
    """The recorder this process runs with: the observe queue when it can send, memory when it cannot.

    An inert client (no key, no team, `HAJER_DISABLED`) would drop every exchange, and a proxy somebody
    started to observe themselves should not be the thing that silently observes nothing.

    The client is imported **here** rather than at module top because this package keeps a one-way import
    order: `_client` is above `_proxy`, so a top-level import would point backwards. Nothing is
    hidden by it — `build_app`, the whole testable surface, needs no client at all.
    """
    if settings.inert:
        sys.stderr.write("hajer proxy: no HAJER_API_KEY/HAJER_TEAM_ID, so exchanges are kept in memory only\n")
        return ListRecorder()
    from hajer._client import Hajer  # noqa: PLC0415 - see the note above: the import order is one-way

    return ObserveRecorder(client=Hajer(settings=settings))


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the process entry point
    """`python -m hajer._proxy --upstream … --listen 127.0.0.1:8091`.

    The `proxy` subcommand of `python -m hajer` is wired in `hajer/__main__.py` and calls exactly this
    function with exactly these flags, so the two spellings are one command.
    """
    parser = argparse.ArgumentParser(prog="hajer proxy", description="Record provider exchanges at the wire.")
    parser.add_argument("--upstream", required=True, help="the provider base URL, e.g. https://api.anthropic.com")
    parser.add_argument("--listen", default="127.0.0.1:8091", help="host:port; loopback only")
    parser.add_argument("--timeout", type=float, default=None, help="upstream read timeout in seconds")
    parser.add_argument("--strip-header", action="append", default=[], help="a header not to forward (repeatable)")
    parser.add_argument("--pass-header", action="append", default=[], help="a header to forward (repeatable)")
    arguments = parser.parse_args(argv)
    host, _, port = arguments.listen.rpartition(":")
    settings = HajerSettings.from_env()
    policy = policy_from(settings, strip=arguments.strip_header, keep=arguments.pass_header)
    if arguments.timeout is not None:
        policy = ProxyPolicy(
            timeout_s=arguments.timeout,
            strip_headers=policy.strip_headers,
            pass_headers=policy.pass_headers,
            capture_content=policy.capture_content,
        )
    try:
        server = serve(
            upstream=arguments.upstream, host=host, port=int(port), recorder=_recorder(settings), policy=policy
        )
    except ProxyRefusedError as refused:
        sys.stderr.write(f"hajer proxy: {refused}\n")
        return 2
    sys.stderr.write(f"hajer proxy: {arguments.upstream} on http://{host}:{port} (origin BROKER)\n")
    with server:
        server.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover - the module entry point
    raise SystemExit(main())

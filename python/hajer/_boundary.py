"""Opt-in recording of non-model outbound HTTP responses, so a replay can answer them without the network.

A replay of a production trace must never reach a real dependency: every outbound call the application
made that was not a model call is answered from what was recorded, and a call nobody recorded makes the
replay UNABLE_TO_VERIFY rather than a guess. This module is the recording half, and it is **off** until a
caller opts in:

    with hajer.record_boundaries(hosts=["crm.internal", "api.stripe.com"]):
        answer = handle(request)            # the application's own httpx calls to those hosts are recorded
        client.verify("refunds@3", request, answer)

Inside the block, each response to an httpx or httpx2 request (sync or async; `_http_libraries`) whose host is one
of `hosts` is kept:
method, scheme, host, port, path, query, the request body's digest (`requestBodyDigest`, so a replay serves the
recording only to the same request; `UNREADABLE` for a streamed request body, which is then never served), status,
content type, and the body as text — at most
`HAJER_BOUNDARY_BODY_MAX_BYTES` of it, with the whole body's digest, its size and whether it was cut. A streamed
response is recorded without a body (reading it would consume the application's stream) and says so.
`verify` / `observe` put what was recorded in that submission's evidence under `hajerBoundaryResponses`,
where the service's redaction walks it like any other evidence, and reset it — exactly as wrapped model
calls are taken.

Model hosts are refused in `hosts`: a model call is the thing a replay re-executes, and it is recorded
already, by `wrap`. Only named hosts are recorded, so the SDK's own traffic to Hajer and every host nobody
named are never read. Nothing is patched until the first block is entered; outside any block the patched
`send` is a pass-through.
"""

from __future__ import annotations

import contextvars
import hashlib
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import cast

import httpx

from hajer._http_libraries import HttpLibrary, libraries, not_read_errors
from hajer._json import JsonObject, JsonValue
from hajer._settings import HajerSettings

BOUNDARY_EVIDENCE_KEY = "hajerBoundaryResponses"
BOUNDARY_SCHEMA = "hajer-boundary-v1"
#: Hosts (and their subdomains) that serve model calls. Recording one here would duplicate `wrap`.
MODEL_HOST_SUFFIXES = (
    "openai.com",
    "anthropic.com",
    "googleapis.com",
    "openai.azure.com",
    "mistral.ai",
    "cohere.ai",
    "cohere.com",
    "groq.com",
    "together.xyz",
    "fireworks.ai",
)


@dataclass
class _Sink:
    hosts: frozenset[str]
    settings: HajerSettings
    responses: list[JsonObject] = field(default_factory=lambda: [])
    dropped: int = 0

    def wants(self, request: httpx.Request) -> bool:
        return request.url.host.lower() in self.hosts

    def keep(self, entry: JsonObject) -> None:
        if len(self.responses) >= self.settings.boundary_responses_max:
            self.dropped += 1
            return
        self.responses.append(entry)


_SINK: contextvars.ContextVar[_Sink | None] = contextvars.ContextVar("hajer_boundary_sink", default=None)
_PATCHED: list[bool] = []


def _model_host(host: str) -> bool:
    bedrock = host.startswith("bedrock") and host.endswith(".amazonaws.com")
    return bedrock or any(host == suffix or host.endswith("." + suffix) for suffix in MODEL_HOST_SUFFIXES)


UNREADABLE_REQUEST = "UNREADABLE"


def request_body_digest(request: httpx.Request) -> str:
    """The digest of the bytes the request sent, or `UNREADABLE` when its body was a stream nobody kept."""
    try:
        content = request.content
    except not_read_errors():
        return UNREADABLE_REQUEST
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _entry(request: httpx.Request, response: httpx.Response, *, streamed: bool, cap: int) -> JsonObject:
    entry: JsonObject = {
        "method": request.method,
        "scheme": request.url.scheme,
        "host": request.url.host,
        "port": request.url.port,
        "path": request.url.path,
        "query": request.url.query.decode("ascii", errors="replace"),
        "requestBodyDigest": request_body_digest(request),
        "status": response.status_code,
        "contentType": response.headers.get("content-type"),
        "streamed": streamed,
    }
    if streamed:
        entry.update({"body": None, "bodyDigest": None, "bodyBytes": None, "truncated": False})
        return entry
    content = response.content
    kept = content[:cap]
    entry.update(
        {
            "body": kept.decode(response.encoding or "utf-8", errors="replace"),
            "bodyDigest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "bodyBytes": len(content),
            "truncated": len(kept) < len(content),
        }
    )
    return entry


def _record(request: httpx.Request, response: httpx.Response, *, streamed: bool) -> None:
    sink = _SINK.get()
    if sink is not None and sink.wants(request):
        sink.keep(_entry(request, response, streamed=streamed, cap=sink.settings.boundary_body_max_bytes))


def _patch(library: HttpLibrary) -> None:
    """Patch `library`'s `Client.send` and `AsyncClient.send`; each is a pass-through outside a block."""
    sync_send = library.client.send
    async_send = library.async_client.send

    def send(self: httpx.Client, request: httpx.Request, **kwargs: object) -> httpx.Response:
        response = sync_send(self, request, **kwargs)  # pyright: ignore[reportArgumentType] - forwarded untouched
        _record(request, response, streamed=kwargs.get("stream") is True)
        return response

    async def asend(self: httpx.AsyncClient, request: httpx.Request, **kwargs: object) -> httpx.Response:
        response = await async_send(self, request, **kwargs)  # pyright: ignore[reportArgumentType] - forwarded untouched
        _record(request, response, streamed=kwargs.get("stream") is True)
        return response

    setattr(library.client, "send", send)  # noqa: B010 - the one sanctioned patch
    setattr(library.async_client, "send", asend)  # noqa: B010


def _install() -> None:
    """Patch every HTTP library's client `send` once (`_patch`)."""
    if _PATCHED:
        return
    for library in libraries():
        _patch(library)
    _PATCHED.append(True)


@contextmanager
def record_boundaries(hosts: Iterable[str], *, settings: HajerSettings | None = None) -> Generator[None]:
    """Record the responses of httpx and httpx2 calls to `hosts` inside this block; see the module docstring.

    `settings` supplies the two bounds (`HAJER_BOUNDARY_*`); absent, they are read from the environment.
    """
    named = frozenset(host.strip().lower() for host in hosts)
    if not named or "" in named:
        raise ValueError("record_boundaries needs at least one host, and every host must be named")
    refused = sorted(host for host in named if _model_host(host))
    if refused:
        raise ValueError(f"Model hosts are recorded by wrap(), not as boundaries: {', '.join(refused)}")
    _install()
    token = _SINK.set(_Sink(named, settings or HajerSettings.from_env()))
    try:
        yield
    finally:
        _SINK.reset(token)


def taken_boundaries() -> JsonValue | None:
    """The block's recorded responses as one evidence value, reset; `None` outside a block or when empty."""
    sink = _SINK.get()
    if sink is None or not (sink.responses or sink.dropped):
        return None
    value: JsonObject = {
        "schema": BOUNDARY_SCHEMA,
        "responses": cast("list[JsonValue]", list(sink.responses)),
        "dropped": sink.dropped,
    }
    sink.responses.clear()
    sink.dropped = 0
    return value

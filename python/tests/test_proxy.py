"""The wire proxy: what it forwards, what it refuses, what it records, and what it never keeps.

Every test here drives the WSGI application
through `httpx.WSGITransport` and answers the upstream from `httpx.MockTransport`, so nothing opens a socket —
the SDK's own rule, and the reason a proxy can be tested at all without a provider.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import cast

import httpx
import pytest

import hajer
from hajer._json import JsonObject, JsonValue
from hajer._proxy import (
    BROKER_ORIGIN,
    CREDENTIAL_HEADERS,
    PROXY_MARKER,
    UPSTREAM_ERROR_TYPE,
    Exchange,
    ListRecorder,
    ObserveRecorder,
    ProxyPolicy,
    ProxyRefusedError,
    build_app,
    policy_from,
    refuse_foreign_path,
    refuse_non_loopback,
    serve,
)
from hajer._settings import HajerSettings
from tests.conftest import Recorder, responds


def accepted() -> JsonObject:
    """What the observe route answers a flush with: one id per observation it took."""
    return {"state": "accepted", "observationIds": ["ing-broker-1"]}


KEY = "sk-ant-THE-CUSTOMERS-OWN-SECRET"
ANSWER = {
    "id": "msg_01broker",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "the reply"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 19, "output_tokens": 8},
}


Application = Callable[[Mapping[str, str], Callable[[str, list[tuple[str, str]]], object]], Iterable[bytes]]


def policy(*, capture_content: bool = False, timeout_s: float = 5.0) -> ProxyPolicy:
    return ProxyPolicy(timeout_s=timeout_s, capture_content=capture_content)


@dataclass(slots=True)
class Upstream:
    """One in-process provider. `seen` is the request that went up, which is half of what these tests assert."""

    handler: Callable[[httpx.Request], httpx.Response]
    seen: httpx.Request | None = None

    def client(self) -> httpx.Client:
        def answer(request: httpx.Request) -> httpx.Response:
            self.seen = request
            return self.handler(request)

        return httpx.Client(base_url="https://api.anthropic.test", transport=httpx.MockTransport(answer))

    def sent(self) -> httpx.Request:
        assert self.seen is not None, "nothing reached the upstream"
        return self.seen


def answering(
    body: object = ANSWER, *, status: int = 200, headers: dict[str, str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers=headers or {})
        return httpx.Response(status, json=cast("JsonValue", body), headers=headers or {})

    return handler


def through(app: Application, *, path: str = "/v1/messages", headers: dict[str, str] | None = None) -> httpx.Response:
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://proxy") as client:
        return client.post(path, headers=headers or {"x-api-key": KEY}, json={"model": "claude-sonnet-5"})


def test_proxy_forwards_and_records_without_keeping_the_key() -> None:
    """The answer comes back byte for byte, the credential goes up, and no record holds it.

    The two halves are one claim: the proxy is useful only if the customer's own call still works, and it is
    acceptable only if the credential it had to forward is in no recorded header, no captured body and no log
    line. Asserted on the recorded exchange rather than on a `repr`, because a `repr` is not where a secret
    would survive.
    """
    recorder = ListRecorder()
    provider = Upstream(answering())
    app = build_app(upstream="https://api.anthropic.test", recorder=recorder, policy=policy(), client=provider.client())

    response = through(app)

    assert response.status_code == 200
    assert response.json() == ANSWER
    assert dict(response.headers)[PROXY_MARKER[0]] == PROXY_MARKER[1]

    assert provider.sent().headers["x-api-key"] == KEY, "the customer's own credential has to reach the provider"

    (exchange,) = recorder.exchanges
    assert exchange.status == 200
    assert exchange.path == "/v1/messages"
    assert KEY not in json.dumps(dict(exchange.request_headers))
    assert not any(name in exchange.request_headers for name in CREDENTIAL_HEADERS)
    assert KEY not in exchange.request_body.decode("utf-8", "replace")
    assert KEY not in exchange.response_body.decode("utf-8", "replace")


def test_binds_loopback_only() -> None:
    """Every loopback spelling is accepted, and that is the whole allowed set."""
    for host in ("127.0.0.1", "::1", "localhost"):
        refuse_non_loopback(host)


def test_non_loopback_bind_is_refused_by_name() -> None:
    """`0.0.0.0` is refused with the address it was asked for and the reason, before any socket exists.

    A warning would not do: a proxy forwarding a customer's provider credential on a routable interface is a
    credential-stealing endpoint somebody else can reach, and the only safe answer is not to bind.
    """
    for host in ("0.0.0.0", "10.0.0.5", "example.test"):  # noqa: S104 - the point of the test is refusing this
        with pytest.raises(ProxyRefusedError, match=re.escape(host)):
            refuse_non_loopback(host)
    # The address this refuses, named once: a literal on both lines is two chances to typo the assertion.
    everything = "0.0.0.0"  # noqa: S104 - the point of the test is that this is refused
    with pytest.raises(ProxyRefusedError, match=re.escape(everything)):
        serve(upstream="https://api.anthropic.test", host=everything, port=0, recorder=ListRecorder(), policy=policy())


def test_absolute_and_protocol_relative_paths_are_refused() -> None:
    """A path naming its own host would make this an open relay, and each shape is refused by name."""
    with pytest.raises(ProxyRefusedError, match="absolute URL"):
        refuse_foreign_path("http://elsewhere.test/v1/messages")
    with pytest.raises(ProxyRefusedError, match="protocol-relative"):
        refuse_foreign_path("//elsewhere.test/v1/messages")
    with pytest.raises(ProxyRefusedError, match="not rooted"):
        refuse_foreign_path("v1/messages")
    refuse_foreign_path("/v1/messages")

    # And through the application, called the way a server calls it: an httpx client would normalise these
    # shapes away before they ever reached a `PATH_INFO`, and a proxy meets them precisely when a client did
    # not normalise them — an absolute-form request line, which is what an open relay is asked for.
    recorder = ListRecorder()
    provider = Upstream(answering())
    app = build_app(upstream="https://api.anthropic.test", recorder=recorder, policy=policy(), client=provider.client())
    status, body = directly(app, path="//elsewhere.test/v1/messages")

    assert status.startswith("400")
    assert cast("dict[str, dict[str, object]]", json.loads(body))["error"]["type"] == UPSTREAM_ERROR_TYPE
    assert recorder.exchanges == []
    assert provider.seen is None, "nothing may reach the upstream for a path this proxy refuses"


def directly(app: Application, *, path: str, method: str = "POST") -> tuple[str, bytes]:
    """Call the WSGI application the way a server does, with a `PATH_INFO` no client would produce.

    The seam exists because an httpx client normalises `//host/x` and `http://host/x` into a host and a path
    before either reaches the application — and those two shapes are exactly the ones a proxy has to refuse,
    because a client that sends them is asking to be relayed somewhere else.
    """
    captured: list[str] = []
    started: list[tuple[str, list[tuple[str, str]]]] = []

    def start_response(status: str, headers: list[tuple[str, str]]) -> object:
        started.append((status, headers))
        return captured.append

    body = b"".join(app({"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": "0"}, start_response))
    return started[0][0], body


def test_upstream_failure_is_502_and_recorded() -> None:
    """A provider this proxy cannot reach is a 502 **and** an exchange, because it still happened.

    The customer's own call failed and their process needs to know; the attempt is evidence too, and an
    unrecorded failure would make a run's observation count quietly disagree with its provider bill.
    """

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    recorder = ListRecorder()
    app = build_app(
        upstream="https://api.anthropic.test",
        recorder=recorder,
        policy=policy(),
        client=Upstream(unreachable).client(),
    )

    response = through(app)

    assert response.status_code == 502
    (exchange,) = recorder.exchanges
    assert exchange.status == 502
    assert "ConnectError" in exchange.failure


def test_upstream_failure_body_is_structured_and_marked() -> None:
    """The body is JSON a client can branch on, and the marker says which hop answered."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("the provider did not answer", request=request)

    app = build_app(
        upstream="https://api.anthropic.test",
        recorder=ListRecorder(),
        policy=policy(),
        client=Upstream(unreachable).client(),
    )

    response = through(app)

    body = cast("dict[str, dict[str, object]]", response.json())
    assert body["error"]["type"] == UPSTREAM_ERROR_TYPE
    assert body["error"]["status"] == 502
    assert "ReadTimeout" in str(body["error"]["message"])
    assert dict(response.headers)[PROXY_MARKER[0]] == PROXY_MARKER[1]


def test_gzip_upstream_is_byte_correct() -> None:
    """A compressed answer reaches the caller as the bytes it decodes to, and no header lies about it.

    `accept-encoding` is stripped on the way up so this is the unusual case rather than the normal one — and
    when it happens anyway, `content-encoding` and `content-length` are dropped on the way back, because the
    bytes a WSGI server re-frames are not the bytes those two describe.
    """
    packed = gzip.compress(json.dumps(ANSWER).encode("utf-8"))
    app = build_app(
        upstream="https://api.anthropic.test",
        recorder=ListRecorder(),
        policy=policy(),
        client=Upstream(
            answering(packed, headers={"content-encoding": "gzip", "content-type": "application/json"})
        ).client(),
    )

    response = through(app)

    assert response.json() == ANSWER
    assert "content-encoding" not in {name.lower() for name in response.headers}


def test_sse_upstream_streams_through() -> None:
    """A server-sent-event answer is yielded chunk by chunk, and recorded once it ends.

    Buffering it to record it would turn a streaming UI into a pause, through a proxy the customer turned on
    to watch themselves; recording it at the first chunk would describe a call still in progress.
    """
    events = b'event: message_start\ndata: {"type":"message_start"}\n\ndata: {"type":"message_stop"}\n\n'

    def streaming(request: httpx.Request) -> httpx.Response:
        del request

        def chunks() -> Iterator[bytes]:
            yield events[:40]
            yield events[40:]

        return httpx.Response(200, content=chunks(), headers={"content-type": "text/event-stream"})

    recorder = ListRecorder()
    app = build_app(
        upstream="https://api.anthropic.test",
        recorder=recorder,
        policy=policy(capture_content=True),
        client=Upstream(streaming).client(),
    )

    response = through(app)

    assert response.content == events
    (exchange,) = recorder.exchanges
    assert exchange.streamed is True
    assert exchange.response_body == events


def test_content_is_not_recorded_without_capture_content() -> None:
    """Explicit opt-out: the exchange's shape is recorded and its bodies are not."""
    quiet = ListRecorder()
    loud = ListRecorder()
    for recorder, chosen in ((quiet, policy()), (loud, policy(capture_content=True))):
        app = build_app(
            upstream="https://api.anthropic.test",
            recorder=recorder,
            policy=chosen,
            client=Upstream(answering()).client(),
        )
        through(app)

    # The proxy has the bodies in memory either way — it just forwarded them — so the assertion is about what
    # it *records*: with capture off the submission carries a summary and neither half quotes a body.
    (without,) = quiet.exchanges
    assert without.captured is False
    assert "model" not in without.request_document()
    assert "answer" not in without.output_document()
    summary = without.capture()
    assert "responseBody" not in summary, "with capture off there are no bytes to send, so no capture goes"
    assert summary["provider"] == "anthropic"
    # Usage is not content: it is what the call cost, and it travels at either setting.
    assert without.output_document()["usage"] == {"input_tokens": 19, "output_tokens": 8}
    assert summary["usage"] == {"input_tokens": 19, "output_tokens": 8}

    (with_content,) = loud.exchanges
    assert with_content.captured is True
    assert with_content.request_document()["model"] == "claude-sonnet-5"
    assert "the reply" in str(with_content.output_document()["answer"])
    assert "responseBody" in with_content.capture()


def test_timeout_and_header_policy_are_overridable() -> None:
    """`--timeout`, `--strip-header` and `--pass-header`, and what each one does to the request.

    The allowlist is the default and the two flags are the seams around it: additive in one direction for a
    provider header this SDK version has not heard of, subtractive in the other for one a customer does not
    want leaving their process.
    """
    settings = HajerSettings(api_key="k", team_id="t", proxy_timeout_s=11)
    built = policy_from(settings, strip=("User-Agent",), keep=("X-Their-Beta",))

    assert built.timeout_s == 11.0
    assert "user-agent" not in built.forwarded()
    assert "x-their-beta" in built.forwarded()
    assert "anthropic-version" in built.forwarded()

    provider = Upstream(answering())
    app = build_app(
        upstream="https://api.anthropic.test", recorder=ListRecorder(), policy=built, client=provider.client()
    )
    through(
        app,
        headers={
            "x-api-key": KEY,
            "user-agent": "their-client/1.0",
            "x-their-beta": "on",
            "x-not-allowed": "nope",
            "accept-encoding": "gzip",
        },
    )

    sent = provider.sent()
    assert "x-their-beta" in sent.headers
    assert sent.headers.get("user-agent") != "their-client/1.0"
    assert "x-not-allowed" not in sent.headers
    assert sent.headers.get("accept-encoding") != "gzip"


def test_broker_capture_with_usage_is_local_priced() -> None:
    """A proxied exchange is submitted as a **capture** with `origin: BROKER`, and its usage travels.

    `origin` says which door the bytes came through and `cost_source` says how the money
    was arrived at, and the two are orthogonal. What this side has to get right is the three things the
    service's pricing depends on:

    * the wrapped call is a **capture** (`responseBody`, `responseStatus`), not a summary — the service reads
      the provider's own bytes rather than taking this SDK's word, which is what makes a broker receipt worth
      more than a self-report, and a summary would produce no receipt at all;
    * the token counts are in it **at every capture setting**, because usage is not content: it is what the
      call cost, and it is the whole input to `LOCAL_PRICED`;
    * the submission says `origin: BROKER`, so the observation records who saw the exchange.

    The pricing itself is the service's, and the Hajer platform's own tests assert that a capture of
    exactly this shape becomes a `LOCAL_PRICED` receipt.
    """
    recorder = Recorder(responds(accepted()))
    settings = HajerSettings(
        api_key="key-for-tests",
        team_id="team-1",
        base_url="https://hajer.test",
        observe_flush_interval_ms=600_000,
    )
    exchange = Exchange(
        method="POST",
        path="/v1/messages",
        status=200,
        started_at="2026-09-19T12:00:00Z",
        duration_ms=412,
        request_headers={"content-type": "application/json"},
        response_headers={"content-type": "application/json"},
        response_body=json.dumps(ANSWER).encode("utf-8"),
        captured=True,
    )

    with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
        observer = ObserveRecorder(client=client)
        observer.record_exchange(exchange)
        client.flush()

    assert observer.submitted == 1
    assert observer.failures == []
    (submitted,) = [
        observation
        for body in recorder.bodies()
        if isinstance(body.get("observations"), list)
        for observation in cast("list[JsonValue]", body["observations"])
        if isinstance(observation, dict)
    ]
    assert submitted["origin"] == BROKER_ORIGIN
    assert submitted["verifier"] is None, "a proxy does not know what obligation a call was about"

    (capture,) = cast("list[JsonObject]", submitted["wrappedCalls"])
    assert capture["wire"] == "ANTHROPIC_MESSAGES"
    assert capture["responseStatus"] == 200
    assert "responseBody" in capture, "a capture, not a summary: the service reads the provider's own bytes"
    assert "provider" not in capture

    output = cast("JsonObject", submitted["output"])
    assert output["usage"] == {"input_tokens": 19, "output_tokens": 8}


def test_a_recording_failure_never_reaches_the_customer() -> None:
    """The provider call already succeeded; a failure to record it is this proxy's problem, not theirs."""

    class Broken:
        def observe_exchange(self, **kwargs: object) -> None:
            del kwargs
            raise RuntimeError("the queue is on fire")

    observer = ObserveRecorder(client=Broken())
    observer.record_exchange(
        Exchange(
            method="POST",
            path="/v1/messages",
            status=200,
            started_at="2026-09-19T12:00:00Z",
            duration_ms=1,
            request_headers={},
            response_headers={},
        )
    )

    assert observer.submitted == 0
    assert observer.failures == ["RuntimeError: the queue is on fire"]


def test_an_exchange_labels_the_wire_it_came_in_on() -> None:
    """The capture says which provider wire it is, by the path, and never guesses at a path it knows."""
    shapes = {
        "/v1/messages": "ANTHROPIC_MESSAGES",
        "/v1/chat/completions": "OPENAI_CHAT_COMPLETIONS",
        "/v1/responses": "OPENAI_RESPONSES",
    }
    for path, wire in shapes.items():
        exchange = Exchange(
            method="POST",
            path=path,
            status=200,
            started_at="2026-09-19T12:00:00Z",
            duration_ms=1,
            request_headers={},
            response_headers={},
        )
        assert exchange.wire == wire

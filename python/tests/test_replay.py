"""`hajer.replay` decisions, without installing anything: the sandbox is exercised through its own methods.

The end-to-end path (a real child process with the guards installed) is covered by the Hajer platform's own
tests; installing the guards here would patch
httpx and the socket layer for this whole test process.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from hajer.replay._case import UnserialisableError, as_json, provider_usage
from hajer.replay._entry import EntryUnavailable, prepare_call
from hajer.replay._guard import Endpoint, Recording, RecordingKey, Sandbox, SandboxRefused, recordings_from

# Paths only compared, never opened: the guard decides by path, the tunnel itself is the backend's.
PROVIDER_TUNNEL, HAJER_TUNNEL = "/run/hr-test/provider.sock", "/run/hr-test/hajer.sock"


def sandbox(tmp_path: Path, recordings: dict[RecordingKey, list[Recording]] | None = None) -> Sandbox:
    return Sandbox(
        provider=Endpoint(host="api.example.test", port=443, tunnel=PROVIDER_TUNNEL),
        hajer=Endpoint(host="127.0.0.1", port=8000, tunnel=HAJER_TUNNEL),
        recordings=recordings or {},
        egress_log=tmp_path / "egress.jsonl",
    )


def sent(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
    """What a real transport returns: an unread stream (the guard reads it raw, then re-wraps it)."""
    body = json.dumps({"model": "m", "usage": {"input_tokens": 3, "output_tokens": 4}}).encode()
    return httpx.Response(200, stream=httpx.ByteStream(body), request=request)


def log(tmp_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (tmp_path / "egress.jsonl").read_text().splitlines()]


def boundary(body: str, **changes: object) -> dict[str, object]:
    """One `hajerBoundaryResponses.responses` entry as `hajer.record_boundaries` writes it."""
    return {
        "method": "GET",
        "scheme": "https",
        "host": "crm.example.test",
        "port": None,
        "path": "/p",
        "query": "id=1",
        "status": 200,
        "contentType": "application/json",
        "streamed": False,
        "body": body,
        "bodyDigest": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        "bodyBytes": len(body),
        "truncated": False,
        **changes,
    }


def test_only_whole_recordings_are_served() -> None:
    whole = boundary('{"tier": "gold"}')
    recordings = recordings_from(
        [
            whole,
            {**whole, "body": "{}"},
            boundary("x", truncated=True),
            {**boundary(""), "streamed": True, "body": None},
        ]  # pyright: ignore[reportArgumentType]
    )
    assert recordings == {
        ("GET", "https", "crm.example.test", 443, "/p", "id=1"): [
            Recording(200, {"content-type": "application/json"}, '{"tier": "gold"}')
        ]
    }


def test_only_the_provider_is_sent_and_every_event_is_logged(tmp_path: Path) -> None:
    guard = sandbox(tmp_path, recordings_from([boundary('{"tier": "gold"}')]))  # pyright: ignore[reportArgumentType]
    through: list[str | None] = []

    def tunnelled(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        through.append(PROVIDER_TUNNEL if guard.tunnel_open(PROVIDER_TUNNEL) else None)
        return sent(transport, request)

    answered = guard.send_sync(httpx.Request("POST", "https://api.example.test/v1", content=b"abc"), tunnelled)
    served = guard.send_sync(httpx.Request("GET", "https://crm.example.test/p?id=1"), tunnelled)
    with pytest.raises(SandboxRefused):
        guard.send_sync(httpx.Request("GET", "https://crm.example.test/p?id=1"), tunnelled)
    with pytest.raises(SandboxRefused):
        guard.send_sync(httpx.Request("POST", "https://mail.example.test/send"), tunnelled)

    assert answered.status_code == 200
    assert served.json() == {"tier": "gold"}
    assert through == [PROVIDER_TUNNEL]
    assert guard.record.refusals == ["NETWORK_UNRECORDED", "BLOCKED_EFFECT"]
    assert guard.record.provider_statuses == [200]
    assert provider_usage(guard.record.provider_bodies[0]) == {"model": "m", "inputTokens": 3, "outputTokens": 4}
    assert log(tmp_path) == [
        {"host": "api.example.test", "port": 443, "allowed": True, "bytes": 3, "served": False},
        {"host": "crm.example.test", "port": 443, "allowed": False, "bytes": 0, "served": True},
        {"host": "crm.example.test", "port": 443, "allowed": False, "bytes": 0, "served": False},
        {"host": "mail.example.test", "port": 443, "allowed": False, "bytes": 0, "served": False},
    ]


def test_a_socket_reaches_only_an_endpoints_tunnel_and_only_while_the_guard_sends(tmp_path: Path) -> None:
    guard = sandbox(tmp_path)
    seen: list[tuple[bool, bool, bool, bool]] = []

    def inner(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                guard.tunnel_open(PROVIDER_TUNNEL),
                guard.tunnel_open(PROVIDER_TUNNEL.encode()),
                guard.tunnel_open("/var/run/postgresql/.s.PGSQL.5432"),
                guard.tunnel_open(("127.0.0.1", 443)),
            )
        )
        return sent(transport, request)

    assert not guard.tunnel_open(PROVIDER_TUNNEL)
    guard.send_sync(httpx.Request("POST", "https://api.example.test/v1", content=b"x"), inner)
    assert seen == [(True, True, False, False)]
    assert not guard.tunnel_open(PROVIDER_TUNNEL)
    with pytest.raises(SandboxRefused):
        guard.send_sync(httpx.Request("POST", "http://127.0.0.1:8000/v1/verify"), inner)
    guard.report()
    guard.send_sync(httpx.Request("POST", "http://127.0.0.1:8000/v1/verify"), inner)
    with pytest.raises(SandboxRefused):
        guard.send_sync(httpx.Request("POST", "https://api.example.test/v1"), inner)


class Owner:
    def __init__(self, prefix: str = ">") -> None:
        self.prefix = prefix

    def run(self, text: str) -> str:
        return self.prefix + text


class NeedsClient:
    def __init__(self, client: object) -> None:
        self.client = client

    def run(self, text: str) -> str:
        return text


def two(first: str, second: str = "") -> str:
    return first + second


# Reached by name through `importlib`, which is what the adapter does and what vulture cannot see.
ENTRY_POINTS = (Owner, NeedsClient, two)


def adapter(qualname: str, *, constructor: str = "DERIVE", mode: str = "DERIVE") -> dict[str, object]:
    assert qualname.partition(".")[0] in {item.__name__ for item in ENTRY_POINTS} | {"Missing"}
    return {"module": __name__, "qualname": qualname, "constructor": constructor, "input": mode, "fields": {}}


def test_derived_calls_are_accepted_only_when_unambiguous() -> None:
    method = prepare_call(adapter("Owner.run"), {"text": "a"})  # pyright: ignore[reportArgumentType]
    function = prepare_call(adapter("two"), {"first": "x", "second": "y"})  # pyright: ignore[reportArgumentType]

    assert method.target(*method.args, **method.kwargs) == ">a"
    assert function.target(*function.args, **function.kwargs) == "xy"
    for qualname, value in (("NeedsClient.run", {"text": "a"}), ("two", {"third": 1}), ("Missing.run", {})):
        with pytest.raises(EntryUnavailable) as refused:
            prepare_call(adapter(qualname), value)  # pyright: ignore[reportArgumentType]
        assert refused.value.reason == "ADAPTER_MISSING"


@dataclass
class Shape:
    label: str


class Model(BaseModel):
    score: float


def test_outputs_are_json_or_refused() -> None:
    model = Model(score=0.5)
    assert as_json({"a": (Shape("x"), model)}) == {"a": [{"label": "x"}, {"score": model.score}]}
    for value in (float("inf"), {1: "x"}, object()):
        with pytest.raises(UnserialisableError):
            as_json(value)

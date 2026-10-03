"""A live CI run is guarded too. The model provider is the one place a byte may go; a database driver, a
non-provider host and a process are refused before they happen, and the attempt fails closed naming what it tried.

The provider path is exercised at the guard (`Sandbox.send_sync` with a transport double that makes the connect the
audit hook would decide, through `Sandbox.connecting`, and answers naming the connection it came over, as httpcore's
`network_stream` does; and a resolver double for the guard's own lookup), never over a socket or a name server; the side effects are exercised end to end, in the plugin's own child, where the guard patches
the socket layer. The plugin's in-session adapter check runs the same guards first and refuses such an adapter before
any case runs (the last test here); the fixture's adapter double (`tests.test_pytest_plugin.ADAPTER_DOUBLE`) lets the
case reach the child, whose guard must still hold for a side effect the check's one call did not make.
"""

from __future__ import annotations

import itertools
import json
import socket
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from hajer._json import JsonObject
from hajer.replay._guard import UNCONNECTED, Endpoint, Resolver, Sandbox, SandboxRefused
from tests.test_pytest_plugin import ADAPTER_DOUBLE, build_fixture, run_plugin

#: What the guard's own lookup answers for the provider's name here (TEST-NET-1: documentation only, never contacted).
PROVIDER_ADDRESS = "192.0.2.10"
PROVIDER = Endpoint(host="api.example.test", port=443)


_DESCRIPTORS = itertools.count(1000)


class _Socket:
    """What the audit hook hands `Sandbox.connecting` as the connecting socket, and what an answer's `network_stream`
    reports it came over: a family and a descriptor, nothing more (no socket is opened)."""

    def __init__(self, family: int = socket.AF_INET) -> None:
        self.family = family
        self._descriptor = next(_DESCRIPTORS)

    def fileno(self) -> int:
        return self._descriptor


class _Stream:
    """A transport's `network_stream` response extension: the connection the answer came over."""

    def __init__(self, sock: _Socket) -> None:
        self._sock = sock

    def get_extra_info(self, info: str) -> object:
        return self._sock if info == "socket" else None


def answer(request: httpx.Request, over: _Socket | None) -> httpx.Response:
    """A 200 as the application's transport returns it: over the connection `over`, or naming none."""
    extensions = {} if over is None else {"network_stream": _Stream(over)}
    return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'), request=request, extensions=extensions)


def answers(*addresses: str, looked_up: list[str] | None = None) -> Resolver:
    """A resolver double: every name is `addresses`, and each lookup is noted in `looked_up`."""

    def resolve(host: str, _port: int) -> list[tuple[int, str]]:
        if looked_up is not None:
            looked_up.append(host)
        return [(socket.AF_INET6 if ":" in item else socket.AF_INET, item) for item in addresses]

    return resolve


def live_sandbox(tmp_path: Path, resolve: Resolver) -> Sandbox:
    return Sandbox(
        provider=None,
        hajer=None,
        recordings={},
        egress_log=tmp_path / "egress.jsonl",
        direct=(PROVIDER,),
        resolve=resolve,
    )


def egress(tmp_path: Path) -> list[JsonObject]:
    return [json.loads(line) for line in (tmp_path / "egress.jsonl").read_text().splitlines()]


def connecting_to(sandbox: Sandbox, sock: _Socket, address: object) -> Callable[..., httpx.Response]:
    """An application transport double that connects `sock` to `address` (as the audit hook would ask) and, if that is
    let through, answers 200 over it: what went out is decided at the connect, never by the request's URL."""

    def inner(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        refused = sandbox.connecting(sock, address)
        if refused is not None:
            raise ConnectionRefusedError(refused)
        return answer(request, sock)

    return inner


def test_the_declared_provider_is_reached_directly_and_nothing_else_is(tmp_path: Path) -> None:
    looked_up: list[str] = []
    sandbox = live_sandbox(tmp_path, answers(PROVIDER_ADDRESS, looked_up=looked_up))
    seen: list[tuple[bool, bool, bool, str | None]] = []

    def inner(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        # Inside the send a connection may be made, to the provider's own address; only its name is ever resolvable.
        sock = _Socket()
        connected = sandbox.connecting(sock, (PROVIDER_ADDRESS, 443))
        seen.append(
            (sandbox.direct_open(), sandbox.resolvable("API.example.test"), sandbox.resolvable("x.test"), connected)
        )
        return answer(request, sock)

    request = httpx.Request("POST", "https://api.example.test/v1/messages", content=b"{}")
    with httpx.HTTPTransport() as own:
        response = sandbox.send_sync(request, inner, own)
    assert (response.status_code, json.loads(response.read())) == (200, {"ok": True})
    assert seen == [(True, True, False, None)]
    assert looked_up == ["api.example.test"], "resolved once, for the send, by the guard's own lookup"
    assert not sandbox.direct_open()
    assert sandbox.record.provider_statuses == [200]
    with pytest.raises(SandboxRefused), httpx.HTTPTransport() as own:
        sandbox.send_sync(httpx.Request("POST", "https://crm.example.test/log"), inner, own)
    assert sandbox.record.refusals == ["BLOCKED_EFFECT"]
    # The allowed egress names the address the socket connected to, not the URL's host.
    assert egress(tmp_path) == [
        {"host": PROVIDER_ADDRESS, "port": 443, "allowed": True, "bytes": 2, "served": False},
        {"host": "crm.example.test", "port": 443, "allowed": False, "bytes": 0, "served": False},
    ]


DIVERSIONS = {
    # A proxy (the environment's or a client's own) at another address: what the transport really connects to.
    "proxy": (_Socket(), ("10.0.0.5", 3128), "10.0.0.5:3128"),
    "provider-address-other-port": (_Socket(), (PROVIDER_ADDRESS, 8443), f"{PROVIDER_ADDRESS}:8443"),
    "unix-socket": (_Socket(socket.AF_UNIX), "/run/elsewhere.sock", "'/run/elsewhere.sock':0"),
    # Python's `connect` resolves a name itself, without an audit event: only a numeric address is ever let through.
    "a-name": (_Socket(), ("api.example.test", 443), "api.example.test:443"),
    "ipv6-elsewhere": (_Socket(socket.AF_INET6), ("2001:db8::1", 443, 0, 0), "2001:db8::1:443"),
}


@pytest.mark.parametrize("diversion", sorted(DIVERSIONS))
def test_a_live_send_that_connects_anywhere_but_the_provider_is_refused_and_named(
    tmp_path: Path, diversion: str
) -> None:
    """A request addressed to the provider but carried elsewhere by its transport is refused at the connect, logged
    with where it tried to go, and neither its answer nor its bytes count as the provider's."""
    sock, address, named = DIVERSIONS[diversion]
    sandbox = live_sandbox(tmp_path, answers(PROVIDER_ADDRESS))
    request = httpx.Request("POST", "https://api.example.test/v1/messages", content=b"{}")
    with pytest.raises(SandboxRefused) as refused, httpx.HTTPTransport() as own:
        sandbox.send_sync(request, connecting_to(sandbox, sock, address), own)
    assert (
        str(refused.value)
        == f"HAJER_REPLAY: BLOCKED_EFFECT POST api.example.test reached {named}, not api.example.test:443"
    )
    assert sandbox.record.refusals == ["BLOCKED_EFFECT"]
    assert sandbox.record.provider_statuses == []
    host, port = named.rsplit(":", 1)
    assert egress(tmp_path) == [{"host": host, "port": int(port), "allowed": False, "bytes": 0, "served": False}]
    assert not sandbox.direct_open()


def test_a_name_that_answers_differently_by_the_connect_is_refused(tmp_path: Path) -> None:
    """DNS rebinding: the guard resolved the provider's name once, when the send began; the library's own lookup then
    answered another address. The connect is judged against the guard's answer."""
    sandbox = live_sandbox(tmp_path, answers(PROVIDER_ADDRESS))
    request = httpx.Request("POST", "https://api.example.test/v1/messages", content=b"{}")
    with pytest.raises(SandboxRefused), httpx.HTTPTransport() as own:
        sandbox.send_sync(request, connecting_to(sandbox, _Socket(), ("127.0.0.1", 443)), own)
    assert egress(tmp_path) == [{"host": "127.0.0.1", "port": 443, "allowed": False, "bytes": 0, "served": False}]
    assert sandbox.record.provider_statuses == []


def test_a_name_that_does_not_resolve_admits_no_connect(tmp_path: Path) -> None:
    sandbox = live_sandbox(tmp_path, answers())
    request = httpx.Request("POST", "https://api.example.test/v1/messages", content=b"{}")
    with pytest.raises(SandboxRefused), httpx.HTTPTransport() as own:
        sandbox.send_sync(request, connecting_to(sandbox, _Socket(), (PROVIDER_ADDRESS, 443)), own)
    assert sandbox.record.refusals == ["BLOCKED_EFFECT"]
    assert sandbox.record.provider_statuses == []


def test_a_transport_that_answers_without_connecting_is_not_the_provider(tmp_path: Path) -> None:
    """A transport whose own pool answers a provider request itself never reached the provider: refused, not counted."""
    sandbox = live_sandbox(tmp_path, answers(PROVIDER_ADDRESS))

    def made_up(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return answer(request, None)

    request = httpx.Request("POST", "https://api.example.test/v1/messages", content=b"{}")
    with pytest.raises(SandboxRefused) as refused, httpx.HTTPTransport() as own:
        sandbox.send_sync(request, made_up, own)
    assert str(refused.value).endswith(f"reached {UNCONNECTED}, not api.example.test:443")
    assert sandbox.record.refusals == ["BLOCKED_EFFECT"]
    assert sandbox.record.provider_statuses == []
    assert egress(tmp_path) == [{"host": UNCONNECTED, "port": 0, "allowed": False, "bytes": 0, "served": False}]


def test_a_pooled_connection_this_send_reused_is_credited_to_where_it_connected(tmp_path: Path) -> None:
    """The second send over a kept-alive connection makes no connect; its answer came over the first send's connection,
    so its bytes went where that connection went, and it counts."""
    sandbox = live_sandbox(tmp_path, answers(PROVIDER_ADDRESS))
    sock = _Socket()
    first = connecting_to(sandbox, sock, (PROVIDER_ADDRESS, 443))

    def pooled(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return answer(request, sock)

    with httpx.HTTPTransport() as own:
        for inner in (first, pooled):
            sandbox.send_sync(httpx.Request("POST", "https://api.example.test/v1", content=b"{}"), inner, own)
    assert sandbox.record.provider_statuses == [200, 200]
    assert [(entry["host"], entry["allowed"]) for entry in egress(tmp_path)] == [(PROVIDER_ADDRESS, True)] * 2


def test_a_descriptor_reused_by_a_refused_connect_loses_its_earlier_admission(tmp_path: Path) -> None:
    """A closed connection's descriptor, reused by a socket whose connect was refused, credits nothing."""
    sandbox = live_sandbox(tmp_path, answers(PROVIDER_ADDRESS))
    earlier = _Socket()
    reused = _Socket()
    reused.fileno = earlier.fileno  # the same descriptor number, on a new socket

    def later(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        refused = sandbox.connecting(reused, ("10.0.0.5", 3128))
        assert refused is not None
        return answer(request, reused)  # a transport that swallowed the refusal and answered anyway

    with httpx.HTTPTransport() as own:
        first = connecting_to(sandbox, earlier, (PROVIDER_ADDRESS, 443))
        sandbox.send_sync(httpx.Request("POST", "https://api.example.test/v1", content=b"{}"), first, own)
        with pytest.raises(SandboxRefused):
            sandbox.send_sync(httpx.Request("POST", "https://api.example.test/v1", content=b"{}"), later, own)
        with pytest.raises(SandboxRefused):  # and the next send over it, with no connect of its own, is not credited
            sandbox.send_sync(
                httpx.Request("POST", "https://api.example.test/v1", content=b"{}"),
                lambda _transport, request: answer(request, reused),
                own,
            )
    assert sandbox.record.provider_statuses == [200]


OTHER = Endpoint(host="api.other.test", port=443)
#: The second send, after a first that connected to the provider and was credited: every one is refused, uncounted.
SECOND_SENDS = ["no-connection", "a-connection-never-admitted", "diverted", "another-endpoints-connection"]


@pytest.mark.parametrize("second", SECOND_SENDS)
def test_a_send_is_credited_only_through_the_connection_its_own_answer_came_over(tmp_path: Path, second: str) -> None:
    """I-A: success is per send. An earlier send's connection to the provider credits no later send that did not use
    it: a send answered over no connection, over one the guard never admitted, after a refused connect, or over a
    connection admitted to another endpoint is refused and never counted."""
    sandbox = Sandbox(
        provider=None,
        hajer=None,
        recordings={},
        egress_log=tmp_path / "egress.jsonl",
        direct=(PROVIDER, OTHER),
        resolve=answers(PROVIDER_ADDRESS),
    )
    earlier = _Socket()
    url = "https://api.other.test/v1" if second == "another-endpoints-connection" else "https://api.example.test/v1"

    def later(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        if second == "diverted":
            refused = sandbox.connecting(_Socket(), ("10.0.0.5", 3128))
            raise ConnectionRefusedError(refused)
        over = {"no-connection": None, "a-connection-never-admitted": _Socket()}.get(second, earlier)
        return answer(request, over)

    with httpx.HTTPTransport() as own:
        first = connecting_to(sandbox, earlier, (PROVIDER_ADDRESS, 443))
        sandbox.send_sync(httpx.Request("POST", "https://api.example.test/v1", content=b"{}"), first, own)
        with pytest.raises(SandboxRefused):
            sandbox.send_sync(httpx.Request("POST", url, content=b"{}"), later, own)
    assert sandbox.record.provider_statuses == [200], "the first send only"
    assert sandbox.record.refusals == ["BLOCKED_EFFECT"]
    logged = egress(tmp_path)
    assert logged[0] == {"host": PROVIDER_ADDRESS, "port": 443, "allowed": True, "bytes": 2, "served": False}
    assert [entry["allowed"] for entry in logged] == [True, False]
    assert logged[1]["host"] == ("10.0.0.5" if second == "diverted" else UNCONNECTED)


async def test_an_async_live_send_is_bound_to_the_provider_the_same_way(tmp_path: Path) -> None:
    sandbox = live_sandbox(tmp_path, answers(PROVIDER_ADDRESS))

    async def elsewhere(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        refused = sandbox.connecting(_Socket(), ("10.0.0.5", 3128))
        if refused is not None:
            raise ConnectionRefusedError(refused)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"), request=request)  # pragma: no cover

    async def provider(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        sock = _Socket()
        assert sandbox.connecting(sock, (PROVIDER_ADDRESS, 443)) is None
        return answer(request, sock)

    async def made_up(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return answer(request, None)

    request = httpx.Request("POST", "https://api.example.test/v1/messages", content=b"{}")
    async with httpx.AsyncHTTPTransport() as own:
        with pytest.raises(SandboxRefused):
            await sandbox.send_async(request, elsewhere, own)
        response = await sandbox.send_async(request, provider, own)
        with pytest.raises(SandboxRefused):
            await sandbox.send_async(request, made_up, own)
    assert json.loads(await response.aread()) == {"ok": True}
    assert sandbox.record.provider_statuses == [200]
    assert [(entry["host"], entry["port"], entry["allowed"]) for entry in egress(tmp_path)] == [
        ("10.0.0.5", 3128, False),
        (PROVIDER_ADDRESS, 443, True),
        (UNCONNECTED, 0, False),
    ]


@pytest.mark.parametrize(
    "url",
    ["http://api.example.test:443/v1/messages", "https://api.example.test:80/v1", "http://api.example.test/v1"],
)
def test_the_provider_match_includes_the_scheme(tmp_path: Path, url: str) -> None:
    """`http://…:443` names the provider's host and port but would send it plaintext: not the provider."""
    sandbox = live_sandbox(tmp_path, answers(PROVIDER_ADDRESS))
    with pytest.raises(SandboxRefused), httpx.HTTPTransport() as own:
        sandbox.send_sync(httpx.Request("POST", url), connecting_to(sandbox, _Socket(), (PROVIDER_ADDRESS, 443)), own)
    assert sandbox.record.provider_statuses == []
    assert Endpoint(host="127.0.0.1", port=8765).reaches(httpx.URL("http://127.0.0.1:8765/v1"))
    assert not Endpoint(host="127.0.0.1", port=8765).reaches(httpx.URL("https://127.0.0.1:8765/v1"))
    assert Endpoint(host="127.0.0.1", port=8765, scheme="https").reaches(httpx.URL("https://127.0.0.1:8765/v1"))


SIDE_EFFECTS = {
    "database": ("def classify(value):\n    import psycopg\n    return {'priority': 'HIGH'}\n", "import:psycopg"),
    "network": (
        "import httpx\ndef classify(value):\n    try:\n        httpx.post('https://crm.example.test/log', content=value)\n"
        "    except Exception:\n        pass\n    return {'priority': 'HIGH'}\n",
        "crm.example.test",
    ),
    "process": (
        "import subprocess\ndef classify(value):\n    try:\n        subprocess.run(['touch', 'MARKER'], check=False)\n"
        "    except Exception:\n        pass\n    return {'priority': 'HIGH'}\n",
        "process",
    ),
}


@pytest.mark.parametrize("kind", sorted(SIDE_EFFECTS))
@pytest.mark.parametrize("live", [True, False])
def test_a_side_effect_fails_the_attempt_closed_in_live_and_replay_alike(tmp_path: Path, kind: str, live: bool) -> None:
    build_fixture(tmp_path, expected="HIGH")
    source, named = SIDE_EFFECTS[kind]
    (tmp_path / "customer.py").write_text(source)
    result = run_plugin(tmp_path, *(["--hajer-live"] if live else []))
    assert result.returncode == 1, result.stdout + result.stderr
    check = json.loads((tmp_path / "results.json").read_text())["suites"][0]["cases"][0]["checks"][0]
    assert check["verdict"] == "UNABLE_TO_VERIFY"
    assert check["reason"].startswith("APP_EXECUTION_UNAVAILABLE: RUNTIME_SIDE_EFFECT_REFUSED:"), check
    assert named in check["reason"]
    assert not (tmp_path / "MARKER").exists()


@pytest.mark.parametrize("kind", sorted(SIDE_EFFECTS))
def test_the_in_session_adapter_check_refuses_a_side_effect_before_any_case_runs(tmp_path: Path, kind: str) -> None:
    build_fixture(tmp_path, expected="HIGH")
    (tmp_path / f"{ADAPTER_DOUBLE}.py").unlink()
    source, named = SIDE_EFFECTS[kind]
    (tmp_path / "customer.py").write_text(source)
    result = run_plugin(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads((tmp_path / "results.json").read_text())
    # No case ran. Its declared checks are still retained, each UNABLE_TO_VERIFY under the refused adapter (never
    # dropped, never green), so the uploaded receipt keeps the suite instead of losing it whole.
    [suite] = payload["suites"]
    assert [
        (case["durationMs"], check["verdict"], check["reason"]) for case in suite["cases"] for check in case["checks"]
    ] == [(0, "UNABLE_TO_VERIFY", "APP_EXECUTION_UNAVAILABLE: ADAPTER_UNVERIFIED")]
    [adapter] = payload["adapters"]
    assert adapter["status"] == "RUNTIME_SIDE_EFFECT_REFUSED", adapter
    assert named in adapter["detail"]
    assert not (tmp_path / "MARKER").exists()

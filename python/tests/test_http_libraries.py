"""httpx2, the fork openai 3.x and anthropic 1.x send through, is guarded, captured and recorded exactly as httpx is.

Every test runs once per library: httpx, and httpx2 when this environment has it (a visible skip when it does not; it
is never vendored). The guard is exercised end to end, in the real children that install it — the pytest plugin's case
child (replay and live) and the adapter-reach child of `verify-adapters` — with sync and async clients: a recorded
boundary is served, a live provider is reached over a real socket, every other host is refused before a byte leaves (as
the library's own `ConnectError`), and an adapter that calls a model through the library is VERIFIED.

The tests marked `loopback` are the one place this package opens a socket, and only on 127.0.0.1 (and, for the
Unix-socket diversion, a socket file in a private temporary directory), to listeners these tests run: what the live
path claims is that the provider's bytes leave through the application's own transport and nowhere else, and only a
socket shows either half. Every fixture that opens one fails a test that does not carry the marker.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import json
import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path
from typing import cast
from wsgiref.simple_server import WSGIRequestHandler, make_server
from wsgiref.types import StartResponse, WSGIEnvironment

import httpx
import pytest

import hajer
import hajer.pytest_plugin._boot as plugin_boot
from hajer._boundary import taken_boundaries
from hajer._http_libraries import HTTPX, HttpLibrary, libraries, library_of
from hajer._json import JsonObject
from hajer._settings import HajerSettings, ci_child_environment
from hajer._verify_adapters import verify_adapters
from hajer.replay._guard import UNCONNECTED, Endpoint, Sandbox, recordings_from
from tests.test_embeddings_and_http import CHAT, HTTP_SETTINGS
from tests.test_pytest_plugin import build_fixture, run_plugin

FORK_MISSING = importlib.util.find_spec("httpx2") is None
LIBRARY = pytest.mark.parametrize(
    "name",
    [
        "httpx",
        pytest.param(
            "httpx2",
            marks=pytest.mark.skipif(FORK_MISSING, reason="httpx2 is not installed here, and it is never vendored"),
        ),
    ],
)
CLIENT = pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
#: `build_fixture`'s case input is the JSON string "ticket": its canonical digest, as each attempt records it.
TICKET_DIGEST = "sha256:" + hashlib.sha256(b'"ticket"').hexdigest()
#: The exception to "no network in tests" (`CLAUDE.md`): this test opens a socket, on loopback only.
LOOPBACK = pytest.mark.loopback


def library(name: str) -> HttpLibrary:
    (found,) = [item for item in libraries() if item.name == name]
    return found


# ── the guard, at its own methods ──────────────────────────────────────────────────────────────────


@LIBRARY
def test_the_guard_answers_and_refuses_in_the_requests_own_library(tmp_path: Path, name: str) -> None:
    lib = library(name)
    body = '{"tier": "gold"}'
    recorded: JsonObject = {
        "method": "GET",
        "scheme": "https",
        "host": "crm.example.test",
        "path": "/p",
        "status": 200,
        "body": body,
        "bodyDigest": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
    }
    sandbox = Sandbox(
        provider=Endpoint(host="api.example.test", port=443, tunnel="/run/hr-test/provider.sock"),
        hajer=None,
        recordings=recordings_from([recorded]),
        egress_log=tmp_path / "egress.jsonl",
        direct=(Endpoint(host="live.example.test", port=443),),
        resolve=lambda _host, _port: [(socket.AF_INET, "192.0.2.10")],
    )
    transports: list[type[object]] = []

    class Connecting:
        family = socket.AF_INET

        def fileno(self) -> int:
            return 7

    class Stream:
        def get_extra_info(self, info: str) -> object:
            return Connecting() if info == "socket" else None

    def inner(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        transports.append(type(transport))
        extensions: dict[str, object] = {}
        if sandbox.direct_open():  # the live send's socket, as the audit hook would decide it, and its answer over it
            assert sandbox.connecting(Connecting(), ("192.0.2.10", 443)) is None
            extensions["network_stream"] = Stream()
        return lib.response(200, stream=lib.byte_stream(b'{"ok": true}'), request=request, extensions=extensions)

    served = sandbox.send_sync(lib.request("GET", "https://crm.example.test/p"), inner, library=lib)
    tunnelled = sandbox.send_sync(lib.request("POST", "https://api.example.test/v1", content=b"x"), inner, library=lib)
    with lib.http_transport() as own:
        direct = sandbox.send_sync(
            lib.request("POST", "https://live.example.test/v1", content=b"y"), inner, own, library=lib
        )
    with pytest.raises(lib.connect_error) as refused:
        sandbox.send_sync(lib.request("POST", "https://mail.example.test/send"), inner, library=lib)

    assert [type(item) for item in (served, tunnelled, direct)] == [lib.response] * 3
    answers = [json.loads(item.read()) for item in (served, tunnelled, direct)]
    assert answers == [{"tier": "gold"}, {"ok": True}, {"ok": True}]
    assert transports == [lib.http_transport, lib.http_transport], "the tunnel is the library's own transport"
    assert str(refused.value) == "HAJER_REPLAY: BLOCKED_EFFECT POST mail.example.test"
    assert (library_of(served), library_of(refused.value.request)) == (lib, lib)
    assert sandbox.record.refusals == ["BLOCKED_EFFECT"]
    assert sandbox.record.provider_statuses == [200, 200]


CI_REQUEST = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "ticket"}],
}
CI_REQUEST_BYTES = json.dumps(CI_REQUEST, separators=(",", ":")).encode()
CI_REQUEST_EXPRESSION = repr(CI_REQUEST).replace("'ticket'", "value")


# ── the guard, installed: the pytest plugin's case child ─────────────────────────────────────────────


def app(name: str, url: str, *, asynchronous: bool, seen: Path | None = None, priced: bool = False) -> str:
    """`customer.classify`: post the ticket to `url` through `name`'s own client and read the answer. With `seen`, a
    refusal is caught as that library's `ConnectError` and its class written there, and the call returns anyway."""
    client = f"{name}.AsyncClient(trust_env=False)" if asynchronous else f"{name}.Client(trust_env=False)"
    payload = "json=" + CI_REQUEST_EXPRESSION if priced else "content=value"
    send = f"await client.post({url!r}, {payload})" if asynchronous else f"client.post({url!r}, {payload})"
    lines = [
        f"import {name}",
        ("async def" if asynchronous else "def") + " classify(value):",
        f"    {'async with' if asynchronous else 'with'} {client} as client:",
    ]
    if seen is None:
        lines += [f"        response = {send}", "    return {'priority': response.json()['answer'].upper()}"]
    else:
        lines += [
            "        try:",
            f"            {send}",
            f"        except {name}.ConnectError as error:",
            f"            open({str(seen)!r}, 'w').write(type(error).__module__ + '.' + type(error).__name__)",
            "    return {'priority': 'HIGH'}",
        ]
    return "\n".join(lines) + "\n"


def only_check(root: Path) -> tuple[JsonObject, JsonObject]:
    payload = json.loads((root / "results.json").read_text())
    case = payload["suites"][0]["cases"][0]
    return case["checks"][0], case


@LIBRARY
@CLIENT
def test_a_recorded_boundary_is_served_to_the_librarys_client_in_a_replay(
    tmp_path: Path, name: str, asynchronous: bool
) -> None:
    build_fixture(tmp_path, expected="HIGH")
    (tmp_path / "customer.py").write_text(app(name, "https://api.example.test/classify", asynchronous=asynchronous))
    result = run_plugin(tmp_path)
    check, case = only_check(tmp_path)
    assert (check["verdict"], check["reason"]) == ("PASS", "REPLAY_HAS_NO_VARIANCE"), result.stdout + result.stderr
    assert case["executionEvidence"] == [
        {
            "mode": "replay",
            "providerSuccesses": 0,
            "reason": None,
            "inputDigest": TICKET_DIGEST,
            "edited": [],
            "requestFingerprint": "sha256:"
            + hashlib.sha256(
                b"".join(
                    hashlib.sha256(value).digest()
                    for value in (b"POST", b"https://api.example.test/classify", b"ticket")
                )
            ).hexdigest(),
        }
    ], "served from the recording: nothing was sent"


class _Quiet(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        _ = (format, args)


def loopback_only(request: pytest.FixtureRequest) -> None:
    """A fixture that opens a socket serves only a test marked `loopback`: the exception is named where it is taken."""
    node = cast(pytest.Item, request.node)  # pytest leaves the fixture request's node untyped
    if node.get_closest_marker("loopback") is None:
        pytest.fail("a test that opens a socket is marked @pytest.mark.loopback (python/CLAUDE.md, No network)")


@pytest.fixture
def stand_in(request: pytest.FixtureRequest) -> Iterator[tuple[int, list[bytes]]]:
    """A model provider on loopback that answers every POST `{"answer": "high"}`, and every body it received."""
    loopback_only(request)
    received: list[bytes] = []

    def answer(environ: WSGIEnvironment, start: StartResponse) -> Iterable[bytes]:
        length = int(str(environ.get("CONTENT_LENGTH") or 0))
        received.append(environ["wsgi.input"].read(length))
        start("200 OK", [("content-type", "application/json")])
        return [json.dumps({"answer": "high", "usage": {"input_tokens": 10, "output_tokens": 20}}).encode()]

    server = make_server("127.0.0.1", 0, answer, handler_class=_Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, received
    finally:
        server.shutdown()
        server.server_close()


def declare_provider(root: Path, host: str, port: int) -> None:
    (root / ".hajer" / "replay.toml").write_text(f'[provider]\nhost = "{host}"\nport = {port}\n')


@LOOPBACK
@LIBRARY
@CLIENT
def test_a_live_run_reaches_the_declared_provider_over_a_real_socket(
    tmp_path: Path, name: str, asynchronous: bool, stand_in: tuple[int, list[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HAJER_CI_BUDGET_MICROUSD", "1000000")
    monkeypatch.setenv("HAJER_CI_BUDGET_FILE", str(tmp_path / "spend.sqlite3"))
    port, received = stand_in
    build_fixture(tmp_path, expected="HIGH")
    declare_provider(tmp_path, "127.0.0.1", port)
    url = f"http://127.0.0.1:{port}/v1/messages"
    (tmp_path / "customer.py").write_text(app(name, url, asynchronous=asynchronous, priced=True))
    result = run_plugin(tmp_path, "--hajer-live")
    check, case = only_check(tmp_path)
    assert check["verdict"] == "PASS", result.stdout + result.stderr
    attempts = case["executionEvidence"]
    assert isinstance(attempts, list)
    assert attempts
    # Each live attempt's provenance: one 2xx answer from the provider, and nothing refused.
    expected: JsonObject = {
        "mode": "live",
        "providerSuccesses": 1,
        "reason": None,
        "inputDigest": TICKET_DIGEST,
        "requestFingerprint": "sha256:"
        + hashlib.sha256(
            b"".join(hashlib.sha256(value).digest() for value in (b"POST", url.encode(), CI_REQUEST_BYTES))
        ).hexdigest(),
        "edited": [],
    }
    assert attempts == [expected] * len(attempts)
    assert received == [CI_REQUEST_BYTES] * len(attempts), "the bytes left through the application's own transport"
    spend = json.loads((tmp_path / "results.json").read_text())["spend"]
    assert spend["settledMicroUsd"] == 110 * len(attempts)
    assert spend["uncertainReservedMicroUsd"] == 0
    assert spend["calls"] == len(attempts)


@LOOPBACK
@pytest.mark.parametrize("ceiling", ["", "1"])
def test_live_without_sufficient_budget_never_reaches_provider(
    tmp_path: Path, stand_in: tuple[int, list[bytes]], monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    port, received = stand_in
    build_fixture(tmp_path, expected="HIGH")
    declare_provider(tmp_path, "127.0.0.1", port)
    monkeypatch.setenv("HAJER_CI_BUDGET_MICROUSD", ceiling)
    monkeypatch.setenv("HAJER_CI_BUDGET_FILE", str(tmp_path / "spend.sqlite3"))
    (tmp_path / "customer.py").write_text(
        app("httpx", f"http://127.0.0.1:{port}/v1/messages", asynchronous=False, priced=True)
    )
    result = run_plugin(tmp_path, "--hajer-live")
    check, _ = only_check(tmp_path)
    assert result.returncode != 0
    assert check["verdict"] == "UNABLE_TO_VERIFY"
    assert received == []


@LOOPBACK
@LIBRARY
@CLIENT
@pytest.mark.parametrize("funded", [False, True])
def test_omitted_openai_output_limit_preserves_bytes_and_reserves_before_dispatch(
    tmp_path: Path, name: str, asynchronous: bool, stand_in: tuple[int, list[bytes]], funded: bool
) -> None:
    port, received = stand_in
    body = b'{ "model": "gpt-5.1", "messages": [{"role":"user","content":"synthetic test"}] }'
    source = app(name, f"http://127.0.0.1:{port}/v1/chat/completions", asynchronous=asynchronous).replace(
        "content=value", f"content={body!r}"
    )
    receipt, _ = live_child(
        tmp_path,
        source,
        f'host = "127.0.0.1"\nport = {port}',
        environment={"HAJER_CI_BUDGET_MICROUSD": "1780000" if funded else "1779999"},
    )
    if funded:
        assert (receipt["provider_successes"], receipt["reason"]) == (1, None)
        assert received == [body], "the provider received the original body without an injected output cap"
    else:
        assert (receipt["provider_successes"], receipt["reason"]) == (0, "CI_BUDGET_EXHAUSTED")
        assert received == [], "the entire published maximum is reserved before any bytes leave"


@LOOPBACK
@LIBRARY
@CLIENT
@pytest.mark.parametrize("live", [False, True], ids=["replay", "live"])
def test_every_other_host_is_refused_before_a_byte_leaves(
    tmp_path: Path, name: str, asynchronous: bool, live: bool, stand_in: tuple[int, list[bytes]]
) -> None:
    """A listening host that is not the declared provider is refused in the guard, as the library's own error, in
    live and replay alike: the stand-in receives nothing, and the attempt fails closed naming it."""
    port, received = stand_in
    build_fixture(tmp_path, expected="HIGH")
    declare_provider(tmp_path, "api.example.test", 443)
    seen = tmp_path / "SEEN"
    url = f"http://127.0.0.1:{port}/log"
    (tmp_path / "customer.py").write_text(app(name, url, asynchronous=asynchronous, seen=seen))
    result = run_plugin(tmp_path, *(["--hajer-live"] if live else []))
    check, _ = only_check(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert check["verdict"] == "UNABLE_TO_VERIFY"
    assert check["reason"] == "APP_EXECUTION_UNAVAILABLE: RUNTIME_SIDE_EFFECT_REFUSED: 127.0.0.1", check
    assert seen.read_text() == "hajer.replay._guard.SandboxRefused", "caught as the library's own ConnectError"
    assert received == []


# ── the guard, installed: a live send carried anywhere but the provider ─────────────────────────────


class Sink:
    """A listener that answers nothing and keeps every connection it accepted, with the bytes sent on it: a connection
    reaching it at all is egress, so what a test asserts is that there were none."""

    def __init__(self, server: socket.socket) -> None:
        self._server = server
        self._server.settimeout(0.05)
        self._stopped = threading.Event()
        self._received: list[bytes] = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1)
                chunks: list[bytes] = []
                with contextlib.suppress(OSError):
                    while chunk := connection.recv(65536):
                        chunks.append(chunk)
                self._received.append(b"".join(chunks))

    def drained(self) -> list[bytes]:
        """Stop listening; every connection accepted, as the bytes sent on it."""
        self._stopped.set()
        self._thread.join(5)
        self._server.close()
        return self._received


@pytest.fixture
def sinks(request: pytest.FixtureRequest) -> Iterator[list[Sink]]:
    """Listeners on 127.0.0.1 a test opens with `tcp_sink`, closed after it."""
    loopback_only(request)
    opened: list[Sink] = []
    yield opened
    for sink in opened:
        sink.drained()


def tcp_sink(opened: list[Sink]) -> tuple[Sink, int]:
    server = socket.create_server(("127.0.0.1", 0))
    sink = Sink(server)
    opened.append(sink)
    return sink, cast(tuple[str, int], server.getsockname())[1]


@pytest.fixture
def unix_sink(request: pytest.FixtureRequest) -> Iterator[tuple[Sink, str]]:
    """A Unix-socket listener in a private temporary directory short enough for any platform's socket path."""
    loopback_only(request)
    directory = tempfile.mkdtemp(prefix="hj-", dir="/tmp")
    path = f"{directory}/sink.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen()
    sink = Sink(server)
    try:
        yield sink, path
    finally:
        sink.drained()
        shutil.rmtree(directory, ignore_errors=True)


#: The plugin's child boot, as `pytest_plugin._run.execute` starts it: `python -I -S -B _boot.py SPEC RECEIPT EGRESS`.
BOOT = Path(plugin_boot.__file__)


def live_child(
    root: Path, source: str, provider: str, *, environment: dict[str, str] | None = None, prelude: str = ""
) -> tuple[JsonObject, list[JsonObject]]:
    """One live attempt of `customer.classify` exactly as the plugin runs it — its child boot, isolated, `[provider]`
    declared — after `prelude` (standard library only, before the SDK is importable): its receipt and egress log.

    The child's environment is the plugin's (`ci_child_environment`) without any proxy of the developer's, plus
    `environment`."""
    (root / ".hajer").mkdir(exist_ok=True)
    (root / ".hajer" / "replay.toml").write_text(f"[provider]\n{provider}\n")
    (root / "customer.py").write_text(source)
    spec, receipt, egress = root / "input.json", root / "receipt.json", root / "egress.jsonl"
    adapter = {"module": "customer", "qualname": "classify", "constructor": "NONE", "input": "SINGLE"}
    spec.write_text(json.dumps({"adapter": adapter, "input": "ticket", "boundaries": []}))
    inherited = {name: value for name, value in ci_child_environment().items() if not name.lower().endswith("_proxy")}
    code = f"{prelude}\nimport runpy\nrunpy.run_path({str(BOOT)!r}, run_name='__main__')\n"
    result = subprocess.run(  # noqa: S603 - the current interpreter, on a script this test wrote
        [sys.executable, "-I", "-S", "-B", "-c", code, str(spec), str(receipt), str(egress), str(root), "live"],
        cwd=root,
        env={
            **inherited,
            "HAJER_CI_BUDGET_MICROUSD": "1000000",
            "HAJER_CI_BUDGET_FILE": str(root / "spend.sqlite3"),
            **(environment or {}),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert receipt.is_file(), result.stdout + result.stderr
    lines = egress.read_text().splitlines() if egress.is_file() else []
    return json.loads(receipt.read_text()), [json.loads(line) for line in lines]


def classify(name: str, url: str, *, asynchronous: bool, client: str, seen: Path, preamble: str = "") -> str:
    """`customer.classify` posting the ticket to `url` through `name`'s client built with `client`; a refusal is caught
    as that library's `ConnectError`, its class written to `seen`, and the call returns anyway."""
    constructor = f"{name}.AsyncClient" if asynchronous else f"{name}.Client"
    url = url.rsplit("/", 1)[0] + "/v1/messages"
    send = (
        f"await http.post({url!r}, json={CI_REQUEST_EXPRESSION})"
        if asynchronous
        else f"http.post({url!r}, json={CI_REQUEST_EXPRESSION})"
    )
    return "\n".join(
        [
            f"import {name}",
            preamble,
            ("async def" if asynchronous else "def") + " classify(value):",
            f"    {'async with' if asynchronous else 'with'} {constructor}({client}) as http:",
            "        try:",
            f"            {send}",
            f"        except {name}.ConnectError as error:",
            f"            open({str(seen)!r}, 'w').write(type(error).__module__ + '.' + type(error).__name__)",
            "    return {'priority': 'HIGH'}",
            "",
        ]
    )


def own_pool(name: str, sink_port: int | None, *, asynchronous: bool) -> str:
    """`Diverted`: `name`'s transport with its own `_pool`, which opens a socket of its own to the sink and answers; with
    no `sink_port`, which answers without connecting anywhere (a made-up answer)."""
    if asynchronous:
        return (
            "import socket\n"
            "class _Body:\n"
            "    async def __aiter__(self):\n"
            '        yield b\'{"answer": "high"}\'\n'
            "class _Answer:\n"
            "    status, headers, extensions = 200, [(b'content-type', b'application/json')], {}\n"
            "    stream = _Body()\n"
            "class _Pool:\n"
            "    async def handle_async_request(self, request):\n"
            "        sent = b''.join([part async for part in request.stream])\n"
            + (
                f"        with socket.create_connection(('127.0.0.1', {sink_port})) as elsewhere:\n"
                "            elsewhere.sendall(sent)\n"
                if sink_port is not None
                else ""
            )
            + "        return _Answer()\n"
            "    async def __aenter__(self):\n"
            "        return self\n"
            "    async def __aexit__(self, *_):\n"
            "        pass\n"
            "    async def aclose(self):\n"
            "        pass\n"
            f"class Diverted({name}.AsyncHTTPTransport):\n"
            "    def __init__(self):\n"
            "        super().__init__()\n"
            "        self._pool = _Pool()\n"
        )
    return (
        "import socket\n"
        "class _Answer:\n"
        "    status, headers, extensions = 200, [(b'content-type', b'application/json')], {}\n"
        '    stream = [b\'{"answer": "high"}\']\n'
        "class _Pool:\n"
        "    def handle_request(self, request):\n"
        "        sent = b''.join(request.stream)\n"
        + (
            f"        with socket.create_connection(('127.0.0.1', {sink_port})) as elsewhere:\n"
            "            elsewhere.sendall(sent)\n"
            if sink_port is not None
            else ""
        )
        + "        return _Answer()\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *_):\n"
        "        pass\n"
        "    def close(self):\n"
        "        pass\n"
        f"class Diverted({name}.HTTPTransport):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        self._pool = _Pool()\n"
    )


#: Before the SDK is importable: a name server whose answer for `provider.test` changes after the first lookup (the
#: guard's, `::1`, where nothing listens) — DNS rebinding — to the sink's address, 127.0.0.1. Both are loopback, so
#: even a guard that let the first answer through would connect nowhere else.
REBINDING = """
import _socket
_real, _asked = _socket.getaddrinfo, []
def _rebinding(host, port, *args, **kwargs):
    name = host.decode() if isinstance(host, bytes) else host
    if name != "provider.test":
        return _real(host, port, *args, **kwargs)
    _asked.append(name)
    if len(_asked) == 1:
        return [(_socket.AF_INET6, _socket.SOCK_STREAM, _socket.IPPROTO_TCP, "", ("::1", int(port), 0, 0))]
    return [(_socket.AF_INET, _socket.SOCK_STREAM, _socket.IPPROTO_TCP, "", ("127.0.0.1", int(port)))]
_socket.getaddrinfo = _rebinding
"""

DIVERSIONS = ["env-http-proxy", "env-https-proxy", "client-proxy", "uds", "own-pool", "dns-rebinding"]


@LOOPBACK
@LIBRARY
@CLIENT
@pytest.mark.parametrize("diversion", DIVERSIONS)
def test_a_live_send_to_the_provider_carried_anywhere_else_is_refused_before_a_byte_leaves(
    tmp_path: Path, name: str, asynchronous: bool, diversion: str, sinks: list[Sink], unix_sink: tuple[Sink, str]
) -> None:
    """A request addressed to the declared provider, which the application's own transport would carry somewhere
    else — a proxy from the environment or the client, a Unix socket, a pool with its own socket, a name that answers
    differently by the time the library connects — is refused at that connect. The sink receives nothing, the log
    names where the send tried to go, and no provider answer is counted."""
    sink, sink_port = tcp_sink(sinks)
    provider, provider_port = tcp_sink(sinks)
    unix, unix_path = unix_sink
    seen = tmp_path / "SEEN"
    declared = f'host = "127.0.0.1"\nport = {provider_port}'
    url = f"http://127.0.0.1:{provider_port}/classify"
    client, preamble, prelude = "trust_env=False", "", ""
    environment: dict[str, str] = {}
    refused_at = ("127.0.0.1", sink_port)
    prefix = "Async" if asynchronous else ""
    if diversion in {"env-http-proxy", "env-https-proxy"}:
        scheme = diversion.removeprefix("env-").removesuffix("-proxy")
        client = ""  # trust_env: the environment's proxy, as openai 3.x and anthropic 1.x read it
        proxy = f"http://127.0.0.1:{sink_port}"
        environment = {f"{scheme.upper()}_PROXY": proxy, f"{scheme}_proxy": proxy, "NO_PROXY": "", "no_proxy": ""}
        declared += f'\nscheme = "{scheme}"'
        url = f"{scheme}://127.0.0.1:{provider_port}/classify"
    elif diversion == "client-proxy":
        client = f"proxy='http://127.0.0.1:{sink_port}', trust_env=False"
    elif diversion == "uds":
        client = f"transport={name}.{prefix}HTTPTransport(uds={unix_path!r}), trust_env=False"
        refused_at = (repr(unix_path), 0)
    elif diversion == "own-pool":
        client, preamble = "transport=Diverted(), trust_env=False", own_pool(name, sink_port, asynchronous=asynchronous)
    else:
        declared, url, prelude = (
            f'host = "provider.test"\nport = {sink_port}',
            f"http://provider.test:{sink_port}/c",
            REBINDING,
        )
    source = classify(name, url, asynchronous=asynchronous, client=client, seen=seen, preamble=preamble)
    receipt, egress = live_child(tmp_path, source, declared, environment=environment, prelude=prelude)
    assert (sink.drained(), provider.drained(), unix.drained()) == ([], [], []), "nothing reached any listener"
    host, port = refused_at
    assert {"host": host, "port": port, "allowed": False, "bytes": 0, "served": False} in egress, egress
    assert not [entry for entry in egress if entry["allowed"]], egress
    assert (receipt["mode"], receipt["provider_successes"]) == ("live", 0)
    assert str(receipt["reason"]).startswith("RUNTIME_SIDE_EFFECT_REFUSED: "), receipt
    assert seen.read_text() == "hajer.replay._guard.SandboxRefused", "caught as the library's own ConnectError"


@LOOPBACK
@LIBRARY
@CLIENT
@pytest.mark.parametrize("through", ["base-url", "proxy-is-the-provider"])
def test_a_live_send_to_the_provider_is_logged_where_it_connected(
    tmp_path: Path, name: str, asynchronous: bool, through: str, stand_in: tuple[int, list[bytes]]
) -> None:
    """The positive half: a provider on loopback at its exact port is reached (a metered proxy declared as the provider
    is the provider, whether the client addresses it as its base URL or as its proxy), the allowed egress names the
    address and port the socket connected to, and the answer is counted once."""
    port, received = stand_in
    seen = tmp_path / "SEEN"
    client = (
        f"proxy='http://127.0.0.1:{port}', trust_env=False" if through == "proxy-is-the-provider" else "trust_env=False"
    )
    url = f"http://127.0.0.1:{port}/classify"
    source = classify(name, url, asynchronous=asynchronous, client=client, seen=seen)
    receipt, egress = live_child(tmp_path, source, f'host = "127.0.0.1"\nport = {port}')
    assert egress == [
        {"host": "127.0.0.1", "port": port, "allowed": True, "bytes": len(CI_REQUEST_BYTES), "served": False}
    ]
    assert (receipt["mode"], receipt["provider_successes"], receipt["reason"]) == ("live", 1, None)
    assert received == [CI_REQUEST_BYTES]
    assert not seen.exists()


@LOOPBACK
@LIBRARY
@CLIENT
def test_a_send_that_never_connected_is_not_credited_by_an_earlier_one(
    tmp_path: Path, name: str, asynchronous: bool, stand_in: tuple[int, list[bytes]]
) -> None:
    """I-A: success is per send. The first send reaches the provider and counts; the second, through a transport whose
    own pool makes its answer up without connecting, is refused and not counted — the first send's connection to the
    same provider credits nothing it did not carry."""
    port, received = stand_in
    seen = tmp_path / "SEEN"
    url = f"http://127.0.0.1:{port}/v1/messages"
    constructor = f"{name}.AsyncClient" if asynchronous else f"{name}.Client"
    wait, block = ("await ", "async with") if asynchronous else ("", "with")
    source = "\n".join(
        [
            f"import {name}",
            own_pool(name, None, asynchronous=asynchronous),
            ("async def" if asynchronous else "def") + " classify(value):",
            f"    {block} {constructor}(trust_env=False) as http:",
            f"        first = {wait}http.post({url!r}, json={CI_REQUEST_EXPRESSION})",
            f"    {block} {constructor}(transport=Diverted(), trust_env=False) as http:",
            "        try:",
            f"            {wait}http.post({url!r}, json={CI_REQUEST_EXPRESSION})",
            f"        except {name}.ConnectError as error:",
            f"            open({str(seen)!r}, 'w').write(type(error).__module__ + '.' + type(error).__name__)",
            "    return {'priority': first.json()['answer'].upper()}",
            "",
        ]
    )
    receipt, egress = live_child(tmp_path, source, f'host = "127.0.0.1"\nport = {port}')
    assert egress == [
        {"host": "127.0.0.1", "port": port, "allowed": True, "bytes": len(CI_REQUEST_BYTES), "served": False},
        {"host": UNCONNECTED, "port": 0, "allowed": False, "bytes": 0, "served": False},
    ]
    assert (receipt["provider_successes"], receipt["reason"]) == (1, f"RUNTIME_SIDE_EFFECT_REFUSED: {UNCONNECTED}")
    assert received == [CI_REQUEST_BYTES], "the provider answered the first send only"
    assert seen.read_text() == "hajer.replay._guard.SandboxRefused"


#: Before the SDK is importable: an audit hook of the test's own, noting whether httpx2 was already imported each time
#: another hook is added (the guard's is the only other), and whether it was by the time the child exits.
IMPORT_ORDER = """
import sys
_order = []
def _watch(event, args):
    if event == "sys.addaudithook":
        _order.append("hook after httpx2" if "httpx2" in sys.modules else "hook")
sys.addaudithook(_watch)
import atexit
atexit.register(lambda: open(ORDER, "w").write(",".join([*_order, "httpx2" if "httpx2" in sys.modules else ""])))
"""


@pytest.mark.skipif(FORK_MISSING, reason="httpx2 is not installed here, and it is never vendored")
def test_httpx2_is_first_imported_under_the_guards_audit_hook(tmp_path: Path) -> None:
    """Finding the libraries imports httpx2, a dependency outside the SDK; the guard adds its hook first."""
    order = tmp_path / "ORDER"
    source = "def classify(value):\n    return {'priority': 'HIGH'}\n"
    prelude = IMPORT_ORDER.replace("ORDER", repr(str(order)))
    receipt, egress = live_child(tmp_path, source, 'host = "127.0.0.1"\nport = 9', prelude=prelude)
    assert (receipt["reason"], egress) == (None, [])
    assert order.read_text() == "hook,httpx2", "the guard's audit hook is added before httpx2 is first imported"


def test_an_httpx2_that_fails_to_import_is_logged_once_and_left_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken httpx2 is not silently skipped: the `hajer` logger says so, once, and httpx is still guarded."""
    real_import, real_find = importlib.import_module, importlib.util.find_spec

    def find(name: str, package: str | None = None) -> object:
        return object() if name == "httpx2" else real_find(name, package)

    def broken(name: str, package: str | None = None) -> object:
        if name == "httpx2":
            raise ImportError("httpcore2 is missing")
        return real_import(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", find)
    monkeypatch.setattr(importlib, "import_module", broken)
    libraries.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="hajer"):
            assert libraries() == (HTTPX,)
            assert libraries() == (HTTPX,)
    finally:
        libraries.cache_clear()
    assert [(record.name, record.levelname) for record in caplog.records] == [("hajer", "WARNING")]
    assert "httpx2 is installed but could not be imported (ImportError)" in caplog.records[0].getMessage()


# ── the guard, installed: the adapter-reach child of `verify-adapters` ────────────────────────────────

SETTINGS = HajerSettings(ci_case_timeout_seconds=60, adapter_check_answers=4)


def reach_app(name: str, *, asynchronous: bool) -> str:
    """An adapter whose model call (line 6) goes through `name`'s own client to a model endpoint the SDK wraps."""
    if asynchronous:
        return (
            f"import {name}\n"
            "\n"
            "\n"
            "async def ask(prompt):\n"
            f"    async with {name}.AsyncClient(base_url='https://api.anthropic.com') as client:\n"
            "        reply = await client.post('/v1/messages', json={'model': 'm', 'max_tokens': 8, 'messages': [prompt]})\n"
            "    return reply.json()['content'][0]['text']\n"
        )
    return (
        f"import {name}\n"
        "\n"
        "\n"
        "def ask(prompt):\n"
        f"    with {name}.Client(base_url='https://api.anthropic.com') as client:\n"
        "        reply = client.post('/v1/messages', json={'model': 'm', 'max_tokens': 8, 'messages': [prompt]})\n"
        "    return reply.json()['content'][0]['text']\n"
    )


def adapter_repository(root: Path, source: str, site: str) -> Path:
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "llm.py").write_text(source)
    (root / ".hajer").mkdir()
    table = [
        'schema = "hajer-replay-v1"',
        "",
        '[adapters."app.llm:ask"]',
        'module = "app.llm"',
        'callable = "ask"',
        'constructor = "NONE"',
        'input = "SINGLE"',
        'verifier = "wf"',
        f'site = "{site}"',
    ]
    (root / ".hajer" / "replay.toml").write_text("\n".join(table) + "\n")
    return root


@LIBRARY
@CLIENT
def test_an_adapter_calling_a_model_through_the_library_is_verified(
    tmp_path: Path, name: str, asynchronous: bool
) -> None:
    repository = adapter_repository(tmp_path, reach_app(name, asynchronous=asynchronous), "app/llm.py:6")
    (check,) = verify_adapters(repository, SETTINGS)
    assert (check.status, check.detail) == ("VERIFIED", "a model call was made from app/llm.py:6")


PROVIDER_APPS = {
    "openai": (
        "from openai import {prefix}OpenAI\n"
        "\n"
        "client = {prefix}OpenAI(api_key='fake', max_retries=0)\n"
        "\n"
        "\n"
        "{define} ask(prompt):\n"
        "    completion = {wait}client.chat.completions.create(model='m', messages=[{{'role': 'user', 'content': prompt}}])\n"
        "    return completion.choices[0].message.content\n"
    ),
    "anthropic": (
        "from anthropic import {prefix}Anthropic\n"
        "\n"
        "client = {prefix}Anthropic(api_key='fake', max_retries=0)\n"
        "\n"
        "\n"
        "{define} ask(prompt):\n"
        "    message = {wait}client.messages.create(model='m', max_tokens=8, messages=[{{'role': 'user', 'content': prompt}}])\n"
        "    return message.content[0].text\n"
    ),
}


@pytest.mark.parametrize("provider", sorted(PROVIDER_APPS))
@CLIENT
def test_a_provider_client_is_verified_whichever_library_it_sends_through(
    tmp_path: Path, provider: str, asynchronous: bool
) -> None:
    """fix1-08: openai 3.x and anthropic 1.x send through httpx2, and their adapter was refused at the socket."""
    pytest.importorskip(provider)
    words = {"prefix": "Async", "define": "async def", "wait": "await "} if asynchronous else {}
    source = PROVIDER_APPS[provider].format(**{"prefix": "", "define": "def", "wait": "", **words})
    repository = adapter_repository(tmp_path, source, "app/llm.py:7")
    (check,) = verify_adapters(repository, SETTINGS)
    assert (check.status, check.detail) == ("VERIFIED", "a model call was made from app/llm.py:7")


# ── capture and boundary recording in this process ────────────────────────────────────────────────


CUSTOMER: JsonObject = {"customer": "c-1", "tier": "gold"}


def unread(lib: HttpLibrary, body: JsonObject) -> httpx.SyncByteStream:
    """`body` as a stream of `lib`'s own that nobody has read yet, sync and async: what a real transport returns, so
    capture has to tap it (a body already read is recorded at once, and would test nothing here)."""
    encoded = json.dumps(body).encode()

    class Unread(lib.sync_byte_stream, lib.async_byte_stream):
        def __iter__(self) -> Iterator[bytes]:
            yield encoded

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield encoded

    return Unread()


class _Answers(httpx.BaseTransport):
    """An in-memory transport answering in `lib`'s own classes (its client is handed it; nothing opens a socket)."""

    def __init__(self, lib: HttpLibrary) -> None:
        self.lib = lib

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = CHAT if request.url.path.endswith("/chat/completions") else CUSTOMER
        return self.lib.response(200, headers={"content-type": "application/json"}, stream=unread(self.lib, body))


@pytest.fixture
def default_transports(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[HttpLibrary]:
    """`lib`'s own default transports, answering in memory: what `HAJER_CAPTURE_HTTP` patches is theirs."""
    lib = library(cast(str, request.param))
    answers = _Answers(lib)

    def handle(_self: httpx.HTTPTransport, sent: httpx.Request) -> httpx.Response:
        return answers.handle_request(sent)

    async def handle_async(_self: httpx.AsyncHTTPTransport, sent: httpx.Request) -> httpx.Response:
        await sent.aread()
        return answers.handle_request(sent)

    monkeypatch.setattr(lib.http_transport, "handle_request", handle)
    monkeypatch.setattr(lib.async_http_transport, "handle_async_request", handle_async)
    hajer.instrument(settings=HTTP_SETTINGS)
    try:
        yield lib
    finally:
        hajer.uninstrument()


LIBRARY_TRANSPORTS = pytest.mark.parametrize(
    "default_transports",
    [
        "httpx",
        pytest.param(
            "httpx2",
            marks=pytest.mark.skipif(FORK_MISSING, reason="httpx2 is not installed here, and it is never vendored"),
        ),
    ],
    indirect=True,
)


@LIBRARY_TRANSPORTS
@CLIENT
async def test_http_capture_records_the_librarys_model_and_http_calls(
    default_transports: HttpLibrary, asynchronous: bool
) -> None:
    lib = default_transports
    with hajer.scope() as operation:
        if asynchronous:
            async with lib.async_client() as http:
                model = await http.post("http://models.invalid/v1/chat/completions", json={"model": "gpt-5.1"})
                other = await http.get("http://people.invalid/v1/people")
        else:
            with lib.client() as http:
                model = http.post("http://models.invalid/v1/chat/completions", json={"model": "gpt-5.1"})
                other = http.get("http://people.invalid/v1/people")
    assert (type(model), model.json(), other.json()) == (lib.response, CHAT, {"customer": "c-1", "tier": "gold"})
    assert [(call.provider, call.api) for call in operation.calls] == [("openai", "chat.completions"), ("http", "GET")]
    assert operation.calls[0].response_id == "chat-1"


@LIBRARY
def test_a_capture_transport_wraps_the_librarys_own_transport(name: str) -> None:
    lib = library(name)
    wrapped = hajer.CaptureTransport(
        _Answers(lib), settings=HajerSettings(capture_content=True, capture_call_site=False)
    )
    with lib.client(transport=wrapped) as http, hajer.scope() as operation:
        response = http.post("http://models.invalid/v1/chat/completions", json={"model": "gpt-5.1"})
    assert (type(response), response.json()) == (lib.response, CHAT)
    assert [call.response_id for call in operation.calls] == ["chat-1"]


@LIBRARY
def test_record_boundaries_records_the_librarys_calls(name: str) -> None:
    lib = library(name)
    settings = HajerSettings(boundary_body_max_bytes=64, boundary_responses_max=4)
    with lib.client(transport=_Answers(lib), base_url="https://crm.internal") as http:
        with hajer.record_boundaries(hosts=["crm.internal"], settings=settings):
            http.get("/customers/c-1", params={"expand": "tier"})
            recorded = taken_boundaries()
    assert isinstance(recorded, dict)
    (entry,) = cast(list[JsonObject], recorded["responses"])
    assert (entry["host"], entry["path"], entry["query"], entry["status"]) == (
        "crm.internal",
        "/customers/c-1",
        "expand=tier",
        200,
    )
    assert json.loads(str(entry["body"])) == {"customer": "c-1", "tier": "gold"}


def test_httpx_is_always_first_and_a_fork_is_named_once() -> None:
    found = libraries()
    assert found[0] is HTTPX
    assert [item.name for item in found] == (["httpx"] if FORK_MISSING else ["httpx", "httpx2"])


@LOOPBACK
def test_fixed_repeats_make_additional_fresh_customer_child_calls(
    tmp_path: Path, stand_in: tuple[int, list[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real child and transport, synthetic loopback provider; this is not paid model evidence."""
    monkeypatch.setenv("HAJER_CI_BUDGET_MICROUSD", "1000000")
    monkeypatch.setenv("HAJER_CI_BUDGET_FILE", str(tmp_path / "spend.sqlite3"))
    monkeypatch.setenv("HAJER_TEAM_ID", "team-1")
    monkeypatch.setenv("HAJER_CI_COMMIT_SHA", "b" * 40)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    port, received = stand_in
    build_fixture(tmp_path, expected="HIGH")
    declare_provider(tmp_path, "127.0.0.1", port)
    url = f"http://127.0.0.1:{port}/v1/messages"
    (tmp_path / "customer.py").write_text(app("httpx", url, asynchronous=False, priced=True))
    result = run_plugin(tmp_path, "--hajer-live", "--hajer-fixed-repeats", "5", "--hajer-project-id", "project-1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(received) == 8  # Three ordinary qualification calls plus five fixed attempts.
    path = next(tmp_path.glob("results-repeats-*/*.measurement.json"))
    measurement = json.loads(path.read_text())
    assert len(measurement["plan"]["planned_attempt_ids"]) == 5
    assert len(measurement["attempts"]) == 5
    assert {item["outcome"] for item in measurement["attempts"]} == {"SATISFIES"}
    assert len({item["request_fingerprint"] for item in measurement["attempts"]}) == 1
    assert measurement["plan"]["sampling_assumption"] == "NOT_ESTABLISHED"
    spent = json.loads((tmp_path / "results.json").read_text())["spend"]
    assert spent["calls"] == 8
    assert spent["settledMicroUsd"] == 8 * 110

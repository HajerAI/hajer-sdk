"""httpx2, the fork openai 3.x and anthropic 1.x send through, is captured and recorded exactly as httpx is.

Every test runs once per library: httpx, and httpx2 when this environment has it (a visible skip when it does not; it
is never vendored). What is asserted is the seam the SDK patches or wraps in each library — the default transports
under `HAJER_CAPTURE_HTTP`, a `CaptureTransport` around the library's own transport, and `record_boundaries` — answered
in memory, in the library's own classes, with no socket opened.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import cast

import httpx
import pytest

import hajer
from hajer._boundary import taken_boundaries
from hajer._http_libraries import HTTPX, HttpLibrary, libraries
from hajer._json import JsonObject
from hajer._settings import HajerSettings
from tests.test_embeddings_and_http import CHAT, HTTP_SETTINGS

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


def library(name: str) -> HttpLibrary:
    (found,) = [item for item in libraries() if item.name == name]
    return found


def test_an_httpx2_that_fails_to_import_is_logged_once_and_left_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken httpx2 is not silently skipped: the `hajer` logger says so, once, and httpx is still captured."""
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

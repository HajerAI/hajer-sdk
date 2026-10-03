"""Review regressions: telemetry faults, object lifetime, late ownership, and choice isolation."""

from __future__ import annotations

import gc
import sys
import weakref
from collections.abc import AsyncIterator, Iterator
from types import ModuleType
from typing import cast

import httpx
import pytest

import hajer
from tests.fakes import FakeOpenAI, FakeStream


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("preloaded", [False, True])
async def test_http_telemetry_buffer_fault_never_changes_response(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool, preloaded: bool
) -> None:
    """Buffer allocation/read bugs belong to telemetry, never to the provider or consumer."""
    chunks = (b'{"id":"chat-1",', b'"choices":[]}')

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield from chunks

    class AsyncStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk

    def broken_buffer(self: object, value: bytes) -> None:
        raise RuntimeError("telemetry buffer failed")

    def respond(request: httpx.Request) -> httpx.Response:
        if preloaded:
            return httpx.Response(200, content=b"".join(chunks))
        return httpx.Response(200, stream=AsyncStream() if asynchronous else Stream())

    monkeypatch.setattr("hajer._http_capture._Capture.chunk", broken_buffer)
    transport = httpx.MockTransport(respond)
    if asynchronous:
        async with httpx.AsyncClient(transport=hajer.AsyncCaptureTransport(transport)) as client:
            response = await client.post("https://provider.test/v1/chat/completions", json={"model": "fake"})
    else:
        with httpx.Client(transport=hajer.CaptureTransport(transport)) as client:
            response = client.post("https://provider.test/v1/chat/completions", json={"model": "fake"})
    assert response.content == b"".join(chunks)
    assert response.status_code == 200
    assert not any(call.error_type for call in hajer.wrapped_calls())


@pytest.fixture
def global_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeOpenAI]]:
    module = ModuleType("openai")

    class Client(FakeOpenAI):
        pass

    module.__dict__["OpenAI"] = Client
    monkeypatch.setitem(sys.modules, "openai", module)
    yield Client
    hajer.uninstrument()


@pytest.mark.parametrize("uninstall_first", [False, True])
def test_installation_receipt_does_not_retain_discarded_clients(
    global_client: type[FakeOpenAI], uninstall_first: bool
) -> None:
    receipt = hajer.instrument()
    client = global_client()
    resource_ref = weakref.ref(client.chat.completions)
    if uninstall_first:
        hajer.uninstrument()
    del client
    gc.collect()
    assert resource_ref() is None
    if uninstall_first:
        assert receipt.patches == []
    else:
        # A new instance also exercises pruning of bookkeeping for collected resources.
        successor = global_client()
        assert all(patch.owner() is not None for patch in receipt.patches)
        assert successor.chat.completions.create() == FakeOpenAI().chat.completions.create()


def test_a_tracing_module_imported_after_instrument_does_not_stop_client_patching(
    global_client: type[FakeOpenAI], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vendor module appearing later is not a source of spans Hajer reads."""
    receipt = hajer.instrument()
    monkeypatch.setitem(sys.modules, "langfuse.openai", ModuleType("langfuse.openai"))
    later = global_client()
    assert getattr(later.chat.completions.create, "__hajer_instrumented__", False)
    assert (receipt.mode, receipt.linkage) == ("wrap", "FRAMES")


def test_streamed_tool_calls_from_different_choices_keep_separate_arguments() -> None:
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call-A", "function": {"name": "alpha", "arguments": '{"a":'}}
                        ]
                    },
                },
                {
                    "index": 1,
                    "delta": {
                        "tool_calls": [{"index": 0, "id": "call-B", "function": {"name": "beta", "arguments": '{"b":'}}]
                    },
                },
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}},
                {"index": 1, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "2}"}}]}},
            ]
        },
    ]
    stream = FakeStream(cast(list[object], chunks))
    client = hajer.wrap(FakeOpenAI(chat_script=[stream]), settings=hajer.HajerSettings(capture_content=True))
    assert list(cast(Iterator[object], client.chat.completions.create(stream=True))) == chunks
    assert [(tool.id, tool.name, tool.arguments) for tool in hajer.wrapped_calls()[0].tool_calls] == [
        ("call-A", "alpha", '{"a":1}'),
        ("call-B", "beta", '{"b":2}'),
    ]

"""HTTP fallback must leave the application's bytes, order, and resource lifecycle intact."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import httpx
import pytest

import hajer
from hajer._json import JsonObject
from tests.test_install_surfaces import document


def capture(transport: httpx.MockTransport, *, asynchronous: bool = False) -> object:
    name = "AsyncCaptureTransport" if asynchronous else "CaptureTransport"
    factory: object = getattr(hajer, name, None)
    assert callable(factory), f"hajer.{name} is required"
    return factory(transport, settings=hajer.HajerSettings(capture_content=True, capture_call_site=False))


@pytest.mark.parametrize(
    ("provider", "path"),
    [("openai", "/v1/chat/completions"), ("openai", "/v1/responses"), ("anthropic", "/v1/messages")],
)
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http_capture_preserves_response_and_links_tool_calls(
    provider: str, path: str, asynchronous: bool
) -> None:
    body = document(provider, "responses" if path.endswith("responses") else "messages")
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    wrapped = capture(transport, asynchronous=asynchronous)
    payload: JsonObject = {"model": "test-model", "messages": []}
    if asynchronous:
        async with httpx.AsyncClient(transport=cast(httpx.AsyncBaseTransport, wrapped)) as client:
            response = await client.post("https://provider.test" + path, json=payload)
    else:
        with httpx.Client(transport=cast(httpx.BaseTransport, wrapped)) as client:
            response = client.post("https://provider.test" + path, json=payload)
    assert response.json() == body
    calls = hajer.wrapped_calls()
    assert len(calls) == 1
    assert calls[0].tool_calls[0].id == "call-1"


def test_early_close_does_not_drain_http_stream() -> None:
    consumed: list[bytes] = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for chunk in (b'data: {"text":"one"}\n\n', b'data: {"text":"two"}\n\n'):
                consumed.append(chunk)
                yield chunk

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"})
    )
    with httpx.Client(transport=cast(httpx.BaseTransport, capture(transport))) as client:
        with client.stream(
            "POST", "https://provider.test/v1/chat/completions", json={"model": "test-model"}
        ) as response:
            assert next(response.iter_bytes()) == b'data: {"text":"one"}\n\n'
    assert len(consumed) == 1
    assert hajer.wrapped_calls()[0].stream_complete is False

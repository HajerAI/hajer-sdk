"""Shared fixtures. No network: every HTTP request is answered by an `httpx.MockTransport`.

`Recorder` is the seam the upload tests use — it collects the requests the SDK made and answers them
from a script, so a test asserts on exactly what went on the wire.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol, cast

import httpx
import pytest

import hajer
from hajer._json import JsonObject, JsonValue

Responder = Callable[[httpx.Request], httpx.Response]


@dataclass
class Recorder:
    """Every request the SDK made, and the script that answered them."""

    responder: Responder
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def bodies(self) -> list[JsonObject]:
        decoded: list[JsonObject] = []
        for request in self.requests:
            parsed: JsonValue = json.loads(request.content)
            assert isinstance(parsed, dict)
            decoded.append(parsed)
        return decoded

    def keys(self) -> list[str]:
        return [request.headers["Idempotency-Key"] for request in self.requests]


class StreamLike(Protocol):
    """What `wrap` promises a streamed sync call returns: the provider's stream, proxied.

    An instrumented `create(stream=True)` is declared as returning `object`, because the provider's
    own type is the provider's business. These two protocols are the test suite's statement of the
    contract the proxy has to keep, and `as_stream` asserts the checkable half of it at runtime.
    """

    def __iter__(self) -> Iterator[object]: ...
    def __enter__(self) -> StreamLike: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> object: ...
    def close(self) -> None: ...


class AsyncStreamLike(Protocol):
    """The async twin."""

    def __aiter__(self) -> AsyncIterator[object]: ...
    async def aclose(self) -> None: ...


class MessageStreamLike(Protocol):
    """What the proxy over `messages.stream(...)` promises: the provider's own helper surface.

    `wrap` must not take anything away. A caller who reads `text_stream`, asks the stream for its HTTP
    `response`, drains it with `until_done()` or reads the answer with `get_final_message()` finds all
    four on the proxy, because the proxy delegates every name it does not define itself.
    """

    response: object

    def __iter__(self) -> Iterator[object]: ...
    def __enter__(self) -> MessageStreamLike: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> object: ...
    @property
    def text_stream(self) -> Iterator[str]: ...
    def until_done(self) -> None: ...
    def get_final_message(self) -> object: ...
    def close(self) -> None: ...


class AsyncMessageStreamLike(Protocol):
    """The async twin."""

    response: object

    def __aiter__(self) -> AsyncIterator[object]: ...
    async def __aenter__(self) -> AsyncMessageStreamLike: ...
    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> object: ...
    async def get_final_message(self) -> object: ...
    async def aclose(self) -> None: ...


def as_stream(value: object) -> StreamLike:
    assert isinstance(value, Iterable), "a streamed call must return something iterable"
    return cast(StreamLike, value)


def as_async_stream(value: object) -> AsyncStreamLike:
    assert hasattr(value, "__aiter__"), "a streamed async call must return something async-iterable"
    return cast(AsyncStreamLike, value)


def as_message_stream(value: object) -> MessageStreamLike:
    assert isinstance(value, Iterable), "a stream helper must return something iterable"
    return cast(MessageStreamLike, value)


def as_async_message_stream(value: object) -> AsyncMessageStreamLike:
    assert hasattr(value, "__aiter__"), "an async stream helper must return something async-iterable"
    return cast(AsyncMessageStreamLike, value)


@pytest.fixture
def settings() -> hajer.HajerSettings:
    """A configured, non-inert client configuration with fast, small bounds."""
    return hajer.HajerSettings(
        api_key="key-for-tests",
        team_id="team-1",
        base_url="https://hajer.test",
        eval_upload_backoff_initial_ms=1,
        eval_upload_backoff_max_ms=2,
    )


@pytest.fixture(autouse=True)
def _clean_context() -> Iterator[None]:
    """No wrapped call ever leaks from one test into the next."""
    hajer.clear_wrapped_calls()
    yield
    hajer.clear_wrapped_calls()


def pytest_addoption(parser: pytest.Parser) -> None:
    """The two opt-ins: the engine tests, and rewriting the golden engine fixture from a real run."""
    parser.addoption(
        "--engine",
        action="store_true",
        default=False,
        help="Run the tests marked `engine`: the pinned eval engine on Node, installed from npm on first use.",
    )
    parser.addoption(
        "--update-evals-golden",
        action="store_true",
        default=False,
        help="Rewrite tests/fixtures/evals/results.golden.json from a real engine run (implies --engine).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """`engine` tests run only on request: they need Node and, once, the npm registry (CLAUDE.md, "no network").

    Skipping on a missing Node would silently reach the network wherever Node happens to be installed — every
    CI runner — so the opt-in is explicit rather than detected.
    """
    if config.getoption("--engine") or config.getoption("--update-evals-golden"):
        return
    skip = pytest.mark.skip(reason="needs the eval engine; run with `pytest --engine`")
    for item in items:
        if "engine" in item.keywords:
            item.add_marker(skip)

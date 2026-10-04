"""Shared fixtures. No network: every client is built on `httpx.MockTransport`.

`recorder()` is the seam every HTTP test uses — it collects the requests the SDK made and answers
them from a script, so a test asserts on exactly what went on the wire.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
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


def assessment_json(**overrides: JsonValue) -> JsonObject:
    """A minimal satisfied assessment on the wire, in the backend's camelCase."""
    body: JsonObject = {
        "status": "satisfied",
        "shadow": False,
        "findings": [],
        "missingEvidence": [],
        "checkOutcomes": [{"check": "refund@1", "reason": "satisfied", "detail": "", "evidenceUsed": ["orderState"]}],
        "costMicrousd": 120,
        "latencyMs": 41,
        "verifier": "refund-policy@1",
        "observationId": "obs-1",
    }
    body.update(overrides)
    return body


def responds(body: JsonObject, *, status: int = 200) -> Responder:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return responder


def times_out(request: httpx.Request) -> httpx.Response:
    """Spend the caller's own per-request timeout, then fail the way a real socket does.

    `httpx.MockTransport` does not enforce timeouts, so the handler reads the budget the SDK derived
    from the deadline out of the request extensions and honours it itself. That keeps the test honest
    about which clock the SDK is spending.
    """
    extensions: Mapping[str, object] = request.extensions
    timeout = extensions.get("timeout")
    budget: object = cast(Mapping[str, object], timeout).get("read") if isinstance(timeout, dict) else None
    time.sleep(float(budget) if isinstance(budget, (int, float)) else 0.05)
    raise httpx.ReadTimeout("simulated read timeout", request=request)


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
        deadline_ms_default=500,
        observe_flush_interval_ms=50,
        observe_batch_max=8,
        observe_queue_max=64,
    )


@pytest.fixture(autouse=True)
def _clean_context() -> Iterator[None]:
    """No wrapped call ever leaks from one test into the next."""
    hajer.clear_wrapped_calls()
    yield
    hajer.clear_wrapped_calls()


def pytest_addoption(parser: pytest.Parser) -> None:
    """`--update-observed-export` rewrites the engine's fixture export from this wrapper's own output.

    A generated artifact, never a typed one: the committed export is what `wrap` records off the
    fixture application, and without this flag a drift between the two is a failure.
    """
    parser.addoption(
        "--update-observed-export",
        action="store_true",
        default=False,
        help="Rewrite contract/observed-app/export.json from the wrapper.",
    )
    parser.addoption(
        "--update-suite-reference-capture",
        action="store_true",
        default=False,
        help="Rewrite contract/sdk-capture/export.json from the owned outreach app.",
    )
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


@pytest.fixture
def update(request: pytest.FixtureRequest) -> bool:
    """Whether this run may rewrite the generated export."""
    return bool(request.config.getoption("--update-observed-export"))


@pytest.fixture
def update_suite_capture(request: pytest.FixtureRequest) -> bool:
    """Whether this run may rewrite the maintained-suite rehearsal's generated capture export."""
    return bool(request.config.getoption("--update-suite-reference-capture"))

"""The socket the SDK's own HTTP requests go through (the eval upload, `doctor`'s probe), and its four rules.

Traces never pass here: they leave through the OpenTelemetry exporter (`hajer._telemetry_otel`).

1. **The deadline covers the whole operation.** `Deadline` starts on a monotonic clock when the SDK
   call begins, so serialisation, connection, transfer and the server's own work all spend from the
   same budget, and the per-request httpx timeout is whatever is left of it at the moment of the send.
2. **No automatic inline retry.** `httpx.HTTPTransport(retries=0)` and nothing above it retries; the eval
   upload retries with bounded backoff of its own because it is off every response path.
3. **The body is bounded before the socket.** `encode_body` refuses at its limit. Refusing locally costs
   nothing, keeps the payload out of the network, and gives the developer the same answer the server
   would have given.
4. **The transport is injectable.** Passing `transport=httpx.MockTransport(handler)` replaces the
   socket with a function. Every test in this package uses that seam; none opens a connection.
"""

from __future__ import annotations

import json
import time
from typing import Final

import httpx

from hajer._errors import BodyOverBoundError
from hajer._json import JsonObject
from hajer._paths import VERSION

USER_AGENT: Final[str] = f"hajer-python/{VERSION}"
#: httpx wants seconds; the whole SDK speaks milliseconds. One conversion, one place.
_MS_PER_S: Final[float] = 1_000.0


class Deadline:
    """A monotonic budget in milliseconds. `remaining_ms()` is what is left, never negative."""

    __slots__ = ("_budget_ms", "_started_ns")

    def __init__(self, budget_ms: int) -> None:
        self._budget_ms = budget_ms
        self._started_ns = time.monotonic_ns()

    @property
    def budget_ms(self) -> int:
        return self._budget_ms

    def elapsed_ms(self) -> int:
        return (time.monotonic_ns() - self._started_ns) // 1_000_000

    def remaining_ms(self) -> int:
        return max(0, self._budget_ms - self.elapsed_ms())

    def expired(self) -> bool:
        return self.remaining_ms() <= 0

    def timeout_s(self) -> float:
        return self.remaining_ms() / _MS_PER_S


def encode_body(payload: JsonObject, *, limit: int) -> bytes:
    """Canonical JSON bytes, or `BodyOverBoundError` before anything is sent.

    Sorted keys and no whitespace: the same payload always produces the same bytes, which is what
    makes the derived idempotency key stable across processes and machines.
    """
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(raw) > limit:
        raise BodyOverBoundError(len(raw), limit)
    return raw


def probe(base_url: str, path: str, *, timeout_ms: int, transport: httpx.BaseTransport | None = None) -> int | None:
    """One keyless GET, for `python -m hajer doctor`: the status, or `None` when nothing answered.

    No credential, no retry, and no exception: the question is whether *something* is listening at
    `HAJER_BASE_URL` and speaking HTTP, which is the difference between a wrong URL and a wrong key —
    the two an operator confuses. A 401 is a perfectly good answer to it. It lives here because this is
    the one module that opens a socket, and it takes the same injectable transport everything else does.
    """
    try:
        with httpx.Client(
            base_url=base_url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
            timeout=httpx.Timeout(timeout_ms / _MS_PER_S),
        ) as client:
            return client.get(path).status_code
    except (httpx.HTTPError, httpx.InvalidURL, httpx.UnsupportedProtocol):
        return None


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


class SyncTransport:
    """One `httpx.Client`, one connection pool, no retries, a per-call timeout."""

    __slots__ = ("_client",)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers=_headers(api_key),
            transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
            # Every request carries its own timeout, derived from the deadline. A client-level
            # default would silently apply to a request whose budget had already run out.
            timeout=httpx.Timeout(None),
        )

    def post(self, path: str, body: bytes, *, idempotency_key: str, timeout_s: float) -> httpx.Response:
        return self._client.post(path, content=body, headers={"Idempotency-Key": idempotency_key}, timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

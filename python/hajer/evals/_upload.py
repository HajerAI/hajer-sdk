"""`upload_eval_run` — one eval run's payload to the platform, and the four rules a CI upload keeps.

1. **It never raises.** The upload is the last thing `hajer eval` does, after the run it reports on has
   already finished; a traceback here would throw away a result the developer waited minutes for. Every
   outcome is an `UploadReceipt`, and the CLI decides what to print and what exit code to give.
2. **It degrades before it refuses.** A run's spans are the bulk of its payload and the least of its
   value, then the outputs; both are dropped, in that order, before the body is refused locally at
   `HAJER_EVAL_UPLOAD_MAX_BYTES`. The receipt names what was dropped so the omission is never silent.
3. **It retries only what a retry can fix.** A 5xx or a transport failure is tried again with the same
   doubling backoff `observe` uses, up to `HAJER_EVAL_UPLOAD_ATTEMPTS`; a 4xx is the service reading the
   payload and saying no, and a second copy of the same payload would get the same answer. The run id is
   the idempotency key on every attempt, so a retry after an answer that was lost in transit is a 409,
   which is the run already stored: `uploaded`.
4. **It sends nothing it should not.** Inert settings send nothing: the pull request that installs the
   SDK has no credentials. The run names no project and no repository of its own — the platform places it
   by the git context the payload carries (`git.ci.repository`, `git.remoteUrl`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final, Literal

import httpx

from hajer._errors import BodyOverBoundError
from hajer._json import JsonObject, JsonValue
from hajer._paths import EVAL_RUNS_PATH
from hajer._settings import HajerSettings
from hajer._transport import Deadline, SyncTransport, encode_body

UploadStatus = Literal["uploaded", "skipped", "failed"]

#: httpx wants seconds; the whole SDK speaks milliseconds. The same one-line conversion `_transport` spells
#: for itself, because it does not export it.
_MS_PER_S: Final[float] = 1_000.0

#: What the receipt says, so the CLI and the tests compare against one spelling.
SKIPPED_INERT: Final[str] = "INERT"
FAILED_NO_RUN_ID: Final[str] = "NO_RUN_ID"
FAILED_BODY_OVER_BOUND: Final[str] = "BODY_OVER_BOUND"
FAILED_REFUSED: Final[str] = "REFUSED"
FAILED_UNREACHABLE: Final[str] = "UNREACHABLE"
FAILED_TIMEOUT: Final[str] = "TIMEOUT"

#: The payload field that is the idempotency key. The payload is the SDK's own versioned schema
#: (`hajer.evals._payload`), camelCase on the wire like everything the platform reads.
_RUN_ID_KEY: Final[str] = "runId"


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    """What became of one run's upload — the CLI's whole knowledge of it.

    `reason` is `None` for an upload; `INERT` for a skip; and for a failure
    `BODY_OVER_BOUND` (refused locally, nothing sent), `REFUSED` (a 4xx: the service read it and said no),
    `UNREACHABLE` (no usable answer after every attempt: a socket error or a 5xx), `TIMEOUT` (the last
    attempt ran out of its deadline) or `NO_RUN_ID` (the payload carried no run id — a programming error
    in the caller, reported rather than raised because the rule that nothing here raises has no exception
    for the SDK's own mistakes). `attempts` counts requests actually sent. `degraded` names what was dropped
    to fit the body bound, in the order it was dropped — and on `BODY_OVER_BOUND`, everything that was
    dropped without it fitting.
    """

    status: UploadStatus
    http_status: int | None
    reason: str | None
    attempts: int
    degraded: tuple[str, ...]


def upload_eval_run(
    payload: JsonObject,
    *,
    settings: HajerSettings,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> UploadReceipt:
    """POST one run to `EVAL_RUNS_PATH`, degraded to fit and retried within bounds. Never raises.

    `transport` and `sleep` are seams, so a test asserts on the wire and on the backoff without a socket or a
    clock.
    """
    api_key = settings.api_key
    team_id = settings.team_id
    if settings.inert or not api_key or not team_id:
        return _skipped(SKIPPED_INERT)
    run_id = payload.get(_RUN_ID_KEY)
    if not isinstance(run_id, str) or not run_id:
        return UploadReceipt(status="failed", http_status=None, reason=FAILED_NO_RUN_ID, attempts=0, degraded=())
    body, degraded = _encode_to_fit(payload, limit=settings.eval_upload_max_bytes)
    if body is None:
        return UploadReceipt(
            status="failed", http_status=None, reason=FAILED_BODY_OVER_BOUND, attempts=0, degraded=degraded
        )
    client = SyncTransport(base_url=settings.base_url, api_key=api_key, transport=transport)
    try:
        return _send(
            client,
            EVAL_RUNS_PATH.format(team_id=team_id),
            body,
            run_id=run_id,
            degraded=degraded,
            settings=settings,
            sleep=sleep,
        )
    finally:
        client.close()


def _skipped(reason: str) -> UploadReceipt:
    return UploadReceipt(status="skipped", http_status=None, reason=reason, attempts=0, degraded=())


# ── fitting the body ────────────────────────────────────────────────────────────────────────────


def _without_spans(payload: JsonObject) -> JsonObject:
    return _with_each_result(payload, "spans", lambda: [])


def _without_outputs(payload: JsonObject) -> JsonObject:
    return _with_each_result(payload, "output", lambda: None)


#: The degradations, in the order they are applied: the label the receipt reports, and the rewrite.
_DEGRADATIONS: Final[tuple[tuple[str, Callable[[JsonObject], JsonObject]], ...]] = (
    ("spans", _without_spans),
    ("outputs", _without_outputs),
)


def _with_each_result(payload: JsonObject, key: str, replacement: Callable[[], JsonValue]) -> JsonObject:
    """A copy of `payload` with `key` replaced on every result that has it. The caller's object is untouched.

    Shallow copies only — the result dicts are rebuilt, the values under them are shared — so dropping a
    run's spans costs the size of the result list, not a deep copy of the spans being dropped. The
    replacement is a factory so no two results share one mutable value.
    """
    results = payload.get("results")
    if not isinstance(results, list):
        return payload
    rewritten: list[JsonValue] = [
        {**result, key: replacement()} if isinstance(result, dict) and key in result else result for result in results
    ]
    return {**payload, "results": rewritten}


def _encode_to_fit(payload: JsonObject, *, limit: int) -> tuple[bytes | None, tuple[str, ...]]:
    """The body under `limit`, degraded as far as it had to be — or `None` with everything that was tried."""
    candidate = payload
    degraded: tuple[str, ...] = ()
    stages = iter(_DEGRADATIONS)
    while True:
        try:
            return encode_body(candidate, limit=limit), degraded
        except BodyOverBoundError:
            stage = next(stages, None)
            if stage is None:
                return None, degraded
            label, rewrite = stage
            candidate = rewrite(candidate)
            degraded = (*degraded, label)


# ── sending it ──────────────────────────────────────────────────────────────────────────────────


def _send(
    client: SyncTransport,
    path: str,
    body: bytes,
    *,
    run_id: str,
    degraded: tuple[str, ...],
    settings: HajerSettings,
    sleep: Callable[[float], None],
) -> UploadReceipt:
    """Up to `eval_upload_attempts` sends of the same bytes under the same key; the receipt is the last answer."""
    http_status: int | None = None
    reason = FAILED_UNREACHABLE
    attempts = 0
    for attempt in range(1, settings.eval_upload_attempts + 1):
        attempts = attempt
        try:
            response = client.post(
                path,
                body,
                idempotency_key=run_id,
                timeout_s=Deadline(settings.eval_upload_deadline_ms).timeout_s(),
            )
        except httpx.TimeoutException:
            http_status, reason = None, FAILED_TIMEOUT
        except httpx.HTTPError:
            http_status, reason = None, FAILED_UNREACHABLE
        else:
            http_status = response.status_code
            if _is_stored(http_status):
                return UploadReceipt(
                    status="uploaded", http_status=http_status, reason=None, attempts=attempt, degraded=degraded
                )
            if _is_refusal(http_status):
                return UploadReceipt(
                    status="failed", http_status=http_status, reason=FAILED_REFUSED, attempts=attempt, degraded=degraded
                )
            reason = FAILED_UNREACHABLE
        if attempt < settings.eval_upload_attempts:
            sleep(_backoff_s(settings, failures=attempt))
    return UploadReceipt(status="failed", http_status=http_status, reason=reason, attempts=attempts, degraded=degraded)


def _is_stored(status: int) -> bool:
    """Any 2xx, and 409: the run was already stored under this id, which is exactly what a retry hopes to hear."""
    return HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES or status == HTTPStatus.CONFLICT


def _is_refusal(status: int) -> bool:
    """A 4xx other than 409: the service read the payload and said no, so sending it again would not help."""
    return HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR


def _backoff_s(settings: HajerSettings, *, failures: int) -> float:
    """Seconds before the next attempt: doubling from `HAJER_EVAL_UPLOAD_BACKOFF_INITIAL_MS`, capped at its `_MAX_MS`."""
    waited_ms = min(
        settings.eval_upload_backoff_initial_ms * (2 ** (failures - 1)), settings.eval_upload_backoff_max_ms
    )
    return waited_ms / _MS_PER_S

"""`Hajer` and `AsyncHajer` — the two calls a customer's code makes.

    assessment = hajer_client.verify("refund-policy", request=…, output=reply, evidence={…})
    if assessment.status == "violated":
        ...                                   # the application decides; the SDK never does

**`verify` does not raise.** A transport failure, a timeout, a 500, a body over the bound, a missing
key: every one of them is an `Assessment` with `status="unavailable"` and a `reason` that says which.
`raise_on_unavailable=True` on the constructor is available for a caller who would rather have the
stack trace, and it is opt-in for one reason: the default must be the one that keeps a customer's
request path working when Hajer is down.

**There is no `on_unavailable="satisfied"`.** Continuing the application and satisfying an obligation
are different facts, and the second is never manufactured from the first What to
do with `unavailable` — continue, hold, review, reject — is the application's policy, chosen per
workflow at enforcement time, and it lives in the customer's own `if`.

**`observe` does not raise and does not wait.** It returns a receipt in one bounded in-memory queue;
`hajer/_queue.py` has the delivery contract.

Every request carries an idempotency key: the caller's when they have one (a request id is better
than any digest), otherwise derived from tenant + verifier + payload digest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Final

import httpx

from hajer import _payload
from hajer._boundary import BOUNDARY_EVIDENCE_KEY, taken_boundaries
from hajer._errors import AssessmentUnavailableError, BodyOverBoundError
from hajer._json import JsonObject, JsonValue
from hajer._models import Assessment, ObservationRow, UnavailableReason
from hajer._paths import ASSESSMENT_PATH, OBSERVATIONS_PATH, OBSERVE_PATH, VERIFY_PATH
from hajer._queue import AsyncObserveQueue, AsyncObserveReceipt, ObserveQueue, ObserveReceipt
from hajer._redact import ClientRedactionPolicy, build_policy
from hajer._settings import HajerSettings
from hajer._transport import AsyncTransport, Deadline, SyncTransport, encode_body
from hajer._wrap import WrappedCall, clear_wrapped_calls, wrapped_calls, wrapped_calls_dropped

#: The inert team id: `verify` needs a path even when there is nothing to send it to.
_NO_TEAM: Final[str] = "-"

#: What varies per call in an attached call's own request, and therefore says nothing about which case
#: it is an attempt at. One entry, and it is the clock.
_ATTACH_VOLATILE_PATHS: Final[tuple[str, ...]] = ("startedAt",)


def _page(since: str | None, limit: int | None) -> dict[str, str]:
    """The listing's two query parameters, with absent meaning absent rather than empty."""
    query: dict[str, str] = {}
    if since is not None:
        query["since"] = since
    if limit is not None:
        query["limit"] = str(limit)
    return query


def _taken_calls() -> tuple[tuple[WrappedCall, ...], int]:
    """Take the task's wrapped calls and reset the context — even when the client is inert.

    Resetting unconditionally matters: a process with no key would otherwise accumulate records
    forever, and a customer's test run would grow a slow leak the SDK put there.
    """
    calls = wrapped_calls()
    dropped = wrapped_calls_dropped()
    clear_wrapped_calls()
    return calls, dropped


class _Base:
    """What both clients share: the settings, the path builder, and the unavailable policy."""

    __slots__ = ("_policy", "_raise_on_unavailable", "_settings")

    def __init__(
        self,
        settings: HajerSettings,
        *,
        raise_on_unavailable: bool,
        policy: ClientRedactionPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._raise_on_unavailable = raise_on_unavailable
        # Built once per client, not once per call: the extra patterns are compiled here, so a policy with
        # a broken pattern is a construction error a developer sees at start-up rather than a degraded pass
        # they read about in an assessment.
        self._policy = policy if policy is not None else build_policy()

    @property
    def settings(self) -> HajerSettings:
        """The settings in force, already validated. Frozen: read it, do not mutate it."""
        return self._settings

    @property
    def inert(self) -> bool:
        """True when there is no key, no team, or `HAJER_DISABLED`: nothing is sent, nothing raises."""
        return self._settings.inert

    def _redacted(self, body: JsonObject, policy: ClientRedactionPolicy | None) -> JsonObject:
        """The body as it will be sent: redacted client-side unless the team turned that off.

        `HAJER_REDACT_CLIENT=0` returns the body untouched and attaches no report, which is the honest
        wire for it — an absent `clientRedaction` says *this client did not redact*, and a report with zero
        counts would say *it ran and found nothing*.

        A per-call `policy=` overrides the process default for this submission only (accepted DX row): one
        endpoint of an application may need a class the rest of it removes, and that is a decision about
        that call rather than about the process.
        """
        if not self._settings.redact_client:
            return body
        return _payload.redacted_body(body, policy=policy if policy is not None else self._policy)

    def _team(self) -> str:
        return self._settings.team_id or _NO_TEAM

    def _path(self, template: str, **parts: str) -> str:
        return template.format(team_id=self._team(), **parts)

    def mark_complete(self, assessment: Assessment) -> Assessment:
        if assessment.status == "unavailable" and self._raise_on_unavailable:
            raise AssessmentUnavailableError(assessment)
        return assessment

    def _unavailable(self, reason: UnavailableReason, deadline: Deadline) -> Assessment:
        return self.mark_complete(Assessment.unavailable_now(reason, latency_ms=deadline.elapsed_ms()))

    def _body(
        self,
        *,
        mode: _payload.IngestMode,
        verifier: str,
        request: JsonValue,
        output: JsonValue,
        evidence: Mapping[str, JsonValue] | None,
        idempotency_key: str | None,
        deadline: Deadline | None,
        case_key: str | None = None,
        policy: ClientRedactionPolicy | None = None,
    ) -> tuple[JsonObject, str]:
        calls, dropped = _taken_calls()
        boundaries = taken_boundaries()
        if boundaries is not None:
            # Recorded non-model responses ride as evidence, so a replay can serve them back (`_boundary`).
            evidence = {**(evidence or {}), BOUNDARY_EVIDENCE_KEY: boundaries}
        key = idempotency_key or _payload.derive_idempotency_key(
            team_id=self._team(), verifier=verifier, request=request, output=output, evidence=evidence
        )
        # Derived here and now, before any later pass rewrites the request: the case key is what makes
        # two attempts comparable, and a key derived after redaction moves whenever a rule fires.
        case, case_source = _payload.case_identity(case_key=case_key, request=request)
        body = _payload.observation_body(
            mode=mode,
            verifier=verifier,
            request=request,
            output=output,
            evidence=evidence,
            wrapped=calls,
            wrapped_dropped=dropped,
            content_captured=self._settings.capture_content,
            idempotency_key=key,
            deadline_ms=None if deadline is None else deadline.remaining_ms(),
            case_key=case,
            case_key_source=case_source,
            environment=self._settings.environment,
        )
        return self._redacted(body, policy), key

    def _call_submission(self, call: WrappedCall) -> tuple[JsonObject, str]:
        """One provider call as its own `OBSERVE` submission, with **no verifier** — attach mode's body.

        The tuple is the call: `attach_request` is what was sent and `attach_output` is what came back,
        neither of them carrying content at any setting, and the call itself rides beside them as the one
        wrapped call under the capture switches. No verifier is named because the process did not name
        one: the observation is recorded and nothing is verified, which is exactly what "logs on its own"
        can honestly mean.

        It deliberately does not go through `_body`: that one *takes* the task's wrapped calls, and an
        attached call must not consume the context a later `verify` in the same task still wants.
        """
        request = _payload.attach_request(call)
        output = _payload.attach_output(call)
        key = _payload.derive_idempotency_key(
            team_id=self._team(), verifier=None, request=request, output=output, evidence=None
        )
        # `startedAt` is the one field of an attached call's request that varies on every call, so it is
        # declared volatile here: without that, every attached observation would be its own case and the
        # column would hold one singleton group per provider call rather than a grouping.
        case, case_source = _payload.case_identity(
            case_key=None, request=request, volatile_paths=_ATTACH_VOLATILE_PATHS
        )
        body = _payload.observation_body(
            mode="OBSERVE",
            verifier=None,
            request=request,
            output=output,
            evidence=None,
            wrapped=(call,),
            wrapped_dropped=0,
            content_captured=self._settings.capture_content,
            idempotency_key=key,
            deadline_ms=None,
            case_key=case,
            case_key_source=case_source,
            environment=self._settings.environment,
        )
        return self._redacted(body, None), key

    def _read_assessment(self, response: httpx.Response, deadline: Deadline) -> Assessment:
        if response.status_code >= 400:
            return self._unavailable("HTTP_STATUS", deadline)
        parsed = _payload.parse_assessment(response.content)
        if parsed is None:
            return self._unavailable("MALFORMED_RESPONSE", deadline)
        # `latency_ms` is always the SDK's own whole-operation measurement: it is the number the
        # caller can act on, and it is the one the deadline was spent against.
        return self.mark_complete(parsed.model_copy(update={"latency_ms": deadline.elapsed_ms()}))


class Hajer(_Base):
    """The synchronous client. Use it as a context manager, or call `close()` when you are done."""

    __slots__ = ("_queue", "_transport")

    def __init__(
        self,
        *,
        settings: HajerSettings | None = None,
        transport: httpx.BaseTransport | None = None,
        raise_on_unavailable: bool = False,
        policy: ClientRedactionPolicy | None = None,
    ) -> None:
        resolved = settings if settings is not None else HajerSettings.from_env()
        super().__init__(resolved, raise_on_unavailable=raise_on_unavailable, policy=policy)
        self._transport = (
            None
            if resolved.inert
            else SyncTransport(base_url=resolved.base_url, api_key=resolved.api_key or "", transport=transport)
        )
        self._queue = ObserveQueue(settings=resolved, send=self._send_batch, overhead=_payload.batch_overhead_bytes())

    # ── verify ──────────────────────────────────────────────────────────────────────────────────

    def verify(
        self,
        verifier: str,
        request: JsonValue,
        output: JsonValue,
        evidence: Mapping[str, JsonValue] | None = None,
        *,
        deadline_ms: int | None = None,
        idempotency_key: str | None = None,
        case_key: str | None = None,
        policy: ClientRedactionPolicy | None = None,
    ) -> Assessment:
        """Apply `verifier` to this payload and answer inside the deadline, or say `unavailable`."""
        deadline = Deadline(deadline_ms if deadline_ms is not None else self._settings.deadline_ms_default)
        body, key = self._body(
            mode="VERIFY",
            verifier=verifier,
            request=request,
            output=output,
            evidence=evidence,
            idempotency_key=idempotency_key,
            deadline=deadline,
            case_key=case_key,
            policy=policy,
        )
        if self._transport is None:
            return self._unavailable("DISABLED", deadline)
        try:
            raw = encode_body(body, limit=self._settings.body_max_bytes)
        except BodyOverBoundError:
            return self._unavailable("BODY_OVER_BOUND", deadline)
        if deadline.expired():
            return self._unavailable("LOCAL_DEADLINE", deadline)
        try:
            response = self._transport.post(
                self._path(VERIFY_PATH), raw, idempotency_key=key, timeout_s=deadline.timeout_s()
            )
        except httpx.TimeoutException:
            return self._unavailable("LOCAL_DEADLINE", deadline)
        except httpx.HTTPError:
            return self._unavailable("TRANSPORT", deadline)
        return self._read_assessment(response, deadline)

    # ── observe ─────────────────────────────────────────────────────────────────────────────────

    def observe(
        self,
        verifier: str,
        request: JsonValue,
        output: JsonValue,
        evidence: Mapping[str, JsonValue] | None = None,
        *,
        idempotency_key: str | None = None,
        case_key: str | None = None,
        policy: ClientRedactionPolicy | None = None,
    ) -> ObserveReceipt:
        """Record this payload for verification off the response path. Returns immediately."""
        body, key = self._body(
            mode="OBSERVE",
            verifier=verifier,
            request=request,
            output=output,
            evidence=evidence,
            idempotency_key=idempotency_key,
            deadline=None,
            case_key=case_key,
            policy=policy,
        )
        return self._queued(body, key)

    def observe_exchange(
        self,
        *,
        request: JsonValue,
        output: JsonValue,
        capture: JsonObject,
        origin: str,
        case_key: str | None = None,
    ) -> ObserveReceipt:
        """Record one exchange **somebody else observed**, with the origin that says who.

        The wire proxy's one call into this client (`_proxy.py`): the bytes were read at the wire
        rather than reported by the process that made the call, and `origin: "BROKER"` is that difference on
        the record. No verifier is named — the proxy does not know what obligation the call was about — so the
        exchange is recorded and nothing is verified, exactly as attach mode does.

        `capture` is the wrapped call as the wire carries it, which for a proxied exchange is the provider's
        own request and response bytes: the service reads those itself into a `ModelCallReceipt` rather than
        taking a summary of them, which is what makes a broker receipt worth more than a self-report.
        """
        key = _payload.derive_idempotency_key(
            team_id=self._team(), verifier=None, request=request, output=output, evidence=None
        )
        case, case_source = _payload.case_identity(case_key=case_key, request=request)
        body = _payload.observation_body(
            mode="OBSERVE",
            verifier=None,
            request=request,
            output=output,
            evidence=None,
            wrapped=(),
            wrapped_dropped=0,
            content_captured=self._settings.capture_content,
            idempotency_key=key,
            deadline_ms=None,
            case_key=case,
            case_key_source=case_source,
            origin=origin,
            environment=self._settings.environment,
        )
        body["wrappedCalls"] = [capture]
        return self._queued(self._redacted(body, None), key)

    def observe_call(self, call: WrappedCall) -> ObserveReceipt:
        """Record one provider call as its own observation, with no verifier. What `attach()` calls.

        Public because it is the one thing attach mode does, and a customer who wants that behaviour for
        one call without attaching the whole process should be able to ask for it by name.
        """
        return self._queued(*self._call_submission(call))

    def _queued(self, body: JsonObject, key: str) -> ObserveReceipt:
        """Put one submission in the local queue, or say why it will never be sent."""
        if self._transport is None:
            return ObserveReceipt(idempotency_key=key, state="disabled")
        receipt = ObserveReceipt(idempotency_key=key, state="queued", poller=self._poll_assessment)
        try:
            raw = encode_body(body, limit=self._settings.body_max_bytes)
        except BodyOverBoundError:
            receipt.mark_dropped("BODY_OVER_BOUND")
            return receipt
        self._queue.submit(body, len(raw), receipt)
        return receipt

    def flush(self) -> None:
        """Send everything queued now, in this thread. One attempt per batch, no backoff."""
        self._queue.flush()

    def queue_depth(self) -> int:
        """Observations waiting in memory right now."""
        return self._queue.depth()

    # ── lifecycle ───────────────────────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Flush, stop the background thread, close the connection pool. Idempotent."""
        self._queue.close()
        if self._transport is not None:
            self._transport.close()

    def __enter__(self) -> Hajer:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # ── internals ───────────────────────────────────────────────────────────────────────────────

    def _send_batch(self, observations: Sequence[JsonObject]) -> tuple[str, ...]:
        if self._transport is None:
            return ()
        body = _payload.batch_body(list(observations))
        raw = encode_body(body, limit=self._settings.body_max_bytes)
        deadline = Deadline(self._settings.observe_flush_deadline_ms)
        response = self._transport.post(
            self._path(OBSERVE_PATH),
            raw,
            idempotency_key=_batch_key(observations),
            timeout_s=deadline.timeout_s(),
        )
        response.raise_for_status()
        return _payload.parse_observation_ids(response.content)

    def observations(self, *, since: str | None = None, limit: int | None = None) -> tuple[ObservationRow, ...]:
        """The observations this team has recorded, oldest first. What `python -m hajer tail` reads.

        `since` is the `created_at` of the last row you saw and is **inclusive**, because one `observe`
        flush writes every row in one transaction and they therefore share an instant: a strictly-after
        read would never show you the rest of a flush you had seen one row of. So a page can repeat the row
        you asked from, and a caller drops it by id — `tail` does exactly that.

        Empty on any failure, including an inert client: this is a read for a human watching a terminal,
        and it follows the same rule as everything else here — nothing raises into a caller's process.
        """
        if self._transport is None:
            return ()
        deadline = Deadline(self._settings.observe_flush_deadline_ms)
        try:
            response = self._transport.get(
                self._path(OBSERVATIONS_PATH), timeout_s=deadline.timeout_s(), params=_page(since, limit)
            )
        except httpx.HTTPError:
            return ()
        if response.status_code >= 400:
            return ()
        return _payload.parse_observations(response.content)

    def _poll_assessment(self, observation_id: str) -> Assessment | None:
        if self._transport is None:
            return None
        deadline = Deadline(self._settings.observe_flush_deadline_ms)
        try:
            response = self._transport.get(
                self._path(ASSESSMENT_PATH, observation_id=observation_id), timeout_s=deadline.timeout_s()
            )
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        return _payload.parse_assessment(response.content)


class AsyncHajer(_Base):
    """The asynchronous client. `async with hajer.AsyncHajer() as client:` — closing it matters."""

    __slots__ = ("_queue", "_transport")

    def __init__(
        self,
        *,
        settings: HajerSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        raise_on_unavailable: bool = False,
        policy: ClientRedactionPolicy | None = None,
    ) -> None:
        resolved = settings if settings is not None else HajerSettings.from_env()
        super().__init__(resolved, raise_on_unavailable=raise_on_unavailable, policy=policy)
        self._transport = (
            None
            if resolved.inert
            else AsyncTransport(base_url=resolved.base_url, api_key=resolved.api_key or "", transport=transport)
        )
        self._queue = AsyncObserveQueue(
            settings=resolved, send=self._send_batch, overhead=_payload.batch_overhead_bytes()
        )

    async def verify(
        self,
        verifier: str,
        request: JsonValue,
        output: JsonValue,
        evidence: Mapping[str, JsonValue] | None = None,
        *,
        deadline_ms: int | None = None,
        idempotency_key: str | None = None,
        case_key: str | None = None,
        policy: ClientRedactionPolicy | None = None,
    ) -> Assessment:
        """Apply `verifier` to this payload and answer inside the deadline, or say `unavailable`."""
        deadline = Deadline(deadline_ms if deadline_ms is not None else self._settings.deadline_ms_default)
        body, key = self._body(
            mode="VERIFY",
            verifier=verifier,
            request=request,
            output=output,
            evidence=evidence,
            idempotency_key=idempotency_key,
            deadline=deadline,
            case_key=case_key,
            policy=policy,
        )
        if self._transport is None:
            return self._unavailable("DISABLED", deadline)
        try:
            raw = encode_body(body, limit=self._settings.body_max_bytes)
        except BodyOverBoundError:
            return self._unavailable("BODY_OVER_BOUND", deadline)
        if deadline.expired():
            return self._unavailable("LOCAL_DEADLINE", deadline)
        try:
            response = await self._transport.post(
                self._path(VERIFY_PATH), raw, idempotency_key=key, timeout_s=deadline.timeout_s()
            )
        except httpx.TimeoutException:
            return self._unavailable("LOCAL_DEADLINE", deadline)
        except httpx.HTTPError:
            return self._unavailable("TRANSPORT", deadline)
        return self._read_assessment(response, deadline)

    def observe(
        self,
        verifier: str,
        request: JsonValue,
        output: JsonValue,
        evidence: Mapping[str, JsonValue] | None = None,
        *,
        idempotency_key: str | None = None,
        case_key: str | None = None,
        policy: ClientRedactionPolicy | None = None,
    ) -> AsyncObserveReceipt:
        """Record this payload for verification off the response path. Returns immediately.

        Not a coroutine on purpose: nothing is awaited, so there is nothing to await. Awaiting it
        would suggest the observation had been delivered, which is exactly what the receipt is for.
        """
        body, key = self._body(
            mode="OBSERVE",
            verifier=verifier,
            request=request,
            output=output,
            evidence=evidence,
            idempotency_key=idempotency_key,
            deadline=None,
            case_key=case_key,
            policy=policy,
        )
        if self._transport is None:
            return AsyncObserveReceipt(idempotency_key=key, state="disabled")
        receipt = AsyncObserveReceipt(idempotency_key=key, state="queued", poller=self._poll_assessment)
        try:
            raw = encode_body(body, limit=self._settings.body_max_bytes)
        except BodyOverBoundError:
            receipt.mark_dropped("BODY_OVER_BOUND")
            return receipt
        self._queue.submit(body, len(raw), receipt)
        return receipt

    async def flush(self) -> None:
        """Send everything queued now. One attempt per batch, no backoff."""
        await self._queue.flush()

    def queue_depth(self) -> int:
        """Observations waiting in memory right now."""
        return self._queue.depth()

    async def close(self) -> None:
        """Flush, stop the background task, close the connection pool. Idempotent."""
        await self._queue.close()
        if self._transport is not None:
            await self._transport.close()

    async def __aenter__(self) -> AsyncHajer:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    def __del__(self) -> None:
        # There is no loop to await on here, so this cannot flush. It says what was lost instead of
        # losing it quietly; `close()` is the thing that actually delivers. Nothing in a finaliser may
        # raise — during interpreter shutdown even `warnings` may already be gone.
        try:
            self._queue.abandon_at_exit()
        except Exception:  # noqa: BLE001, S110 — a finaliser that raises replaces the real problem with itself
            pass

    async def _send_batch(self, observations: Sequence[JsonObject]) -> tuple[str, ...]:
        if self._transport is None:
            return ()
        body = _payload.batch_body(list(observations))
        raw = encode_body(body, limit=self._settings.body_max_bytes)
        deadline = Deadline(self._settings.observe_flush_deadline_ms)
        response = await self._transport.post(
            self._path(OBSERVE_PATH),
            raw,
            idempotency_key=_batch_key(observations),
            timeout_s=deadline.timeout_s(),
        )
        response.raise_for_status()
        return _payload.parse_observation_ids(response.content)

    async def observations(self, *, since: str | None = None, limit: int | None = None) -> tuple[ObservationRow, ...]:
        """The synchronous client's `observations`, awaited. `since` is inclusive for the same reason."""
        if self._transport is None:
            return ()
        deadline = Deadline(self._settings.observe_flush_deadline_ms)
        try:
            response = await self._transport.get(
                self._path(OBSERVATIONS_PATH), timeout_s=deadline.timeout_s(), params=_page(since, limit)
            )
        except httpx.HTTPError:
            return ()
        if response.status_code >= 400:
            return ()
        return _payload.parse_observations(response.content)

    async def _poll_assessment(self, observation_id: str) -> Assessment | None:
        if self._transport is None:
            return None
        deadline = Deadline(self._settings.observe_flush_deadline_ms)
        try:
            response = await self._transport.get(
                self._path(ASSESSMENT_PATH, observation_id=observation_id), timeout_s=deadline.timeout_s()
            )
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        return _payload.parse_assessment(response.content)


def _batch_key(observations: Sequence[JsonObject]) -> str:
    """One flush's own key: a digest over the keys it carries, in order.

    A digest rather than the keys themselves because this goes in a header, and 32 observation keys
    do not fit in one. A replayed flush produces the same key, which is the point.
    """
    keys: list[str] = []
    for observation in observations:
        found = observation.get("idempotencyKey")
        keys.append(found if isinstance(found, str) else "")
    digest = hashlib.sha256("+".join(keys).encode("utf-8")).hexdigest()
    return f"{_payload.IDEMPOTENCY_SCHEME}batch_{digest}"

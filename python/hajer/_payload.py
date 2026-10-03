"""The bodies the SDK sends and the assessment it reads back, mapped onto the generated wire.

The routes exist: `POST /api/teams/{team_id}/verify`, `/observe` and the assessment poll are in
`contract/openapi.json`, so `hajer/_wire.py` carries the platform's own models for
them and **that file is the authority for every name below**. `IngestMode` is the generated alias, and
`tests/test_payload.py` validates a real body through the generated `VerifyIn` and against the
snapshot's own JSON schema — so a field this module spells differently from the platform fails in the
SDK's tests rather than in a customer's request.

What this module still does is build the body, and it does it as a plain dict on purpose: it runs
inside somebody else's request path, and validating every submission against a pydantic model there
would spend the caller's latency proving something the test suite already proved. `hajer.wire_source()`
reports which of the two regimes is in force, so a developer can always tell.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel

from hajer import _wire
from hajer._case_key import CaseKeySource, derive_case_key
from hajer._json import JsonObject, JsonValue
from hajer._models import Assessment, ObservationRow
from hajer._paths import VERSION
from hajer._redact import ClientRedactionPolicy, redact_submission
from hajer._transport import encode_body
from hajer._wrap import WrappedCall

#: One page of the observation listing: the route answers with a bare list.
_PAGE: Final[TypeAdapter[tuple[ObservationRow, ...]]] = TypeAdapter(tuple[ObservationRow, ...])

#: `hj1` names the derivation, not the key: a change to what the digest covers is `hj2`, so two SDK
#: versions can never disagree silently about whether two payloads are the same submission.
IDEMPOTENCY_SCHEME: Final[str] = "hj1"

#: The two modes, from the backend's own schema rather than restated here.
IngestMode = _wire.IngestMode

_SDK: Final[JsonObject] = {"name": "hajer-python", "version": VERSION}


def wire_source() -> str:
    """Whether the wire shapes in force are generated from the backend snapshot or hand-written."""
    if _wire.WIRE_READY:
        return f"generated from {_wire.SNAPSHOT_PATH} at {_wire.SNAPSHOT_DIGEST}"
    return (
        "hand-written (hajer/_payload.py): the backend snapshot at "
        f"{_wire.SNAPSHOT_DIGEST} declares none of the operations the SDK calls"
    )  # pragma: no cover - the snapshot declares all three today; kept for a backend that predates them


def observation_body(
    *,
    mode: IngestMode,
    verifier: str | None,
    request: JsonValue,
    output: JsonValue,
    evidence: Mapping[str, JsonValue] | None,
    wrapped: Sequence[WrappedCall],
    wrapped_dropped: int,
    content_captured: bool,
    idempotency_key: str,
    deadline_ms: int | None,
    case_key: str | None = None,
    case_key_source: CaseKeySource | None = None,
    origin: str | None = None,
    environment: str | None = None,
) -> JsonObject:
    """One `IngestObservation` as the ingest route will receive it.

    `deadlineMs` is what is **left** of the caller's budget at the moment of the send, not what the
    caller asked for: the server is told the remaining budget so it can refuse before reserving
    rather than answer late. An `OBSERVE` submission carries none — it is off the response path.

    `verifier` is `None` for an attach-mode observation: the process did not name a verifier, so the
    tuple is recorded and nothing is verified. `null` goes on the wire rather than the key being
    omitted, because "no verifier" is a statement and an absent field is a silence.
    """
    body: JsonObject = {
        "mode": mode,
        "verifier": verifier,
        "request": request,
        "output": output,
        "evidence": dict(evidence) if evidence is not None else {},
        "wrappedCalls": [call.to_wire() for call in wrapped],
        "wrappedCallsDropped": wrapped_dropped,
        "contentCaptured": content_captured,
        "idempotencyKey": idempotency_key,
        "sdk": dict(_SDK),
    }
    if deadline_ms is not None:
        body["deadlineMs"] = deadline_ms
    if case_key is not None:
        # Both keys or neither: a source naming no key is a provenance for something that is not there,
        # and the server refuses it rather than recording it (`VerifyIn.a_case_key_says_where_it_came_from`).
        body["caseKey"] = case_key
        body["caseKeySource"] = case_key_source or "CALLER"
    if origin is not None:
        # Who *saw* this exchange. Absent means the ordinary case — the application reported itself — and
        # `BROKER` is the wire proxy's: read at the wire rather than reported by the process that made it.
        # A claim about evidence, so it is on the wire explicitly rather than inferred from the shape.
        body["origin"] = origin
    if environment is not None:
        # Where the call was made (`HAJER_ENVIRONMENT`): the team decides which environments' calls may become test
        # inputs. Absent rather than null when the process named none — a silence, which the service never reads as
        # production.
        body["environment"] = environment
    return body


def batch_body(observations: Sequence[JsonObject]) -> JsonObject:
    """What one `observe` flush sends: a list of observations, nothing else."""
    return {"observations": list(observations)}


def batch_overhead_bytes() -> int:
    """The bytes a flush envelope costs before any observation is in it. Measured, not assumed."""
    return len(encode_body(batch_body([]), limit=1 << 30))


def attach_request(call: WrappedCall) -> JsonObject:
    """What one provider call *was*, as the request half of its own observation.

    Attach mode records a model call with nothing else around it, so the tuple is the call: this is the
    request side of it, and `attach_output` is the answer side. **Neither carries content at any
    setting.** The message text, the tool arguments and the tool results travel in exactly one place —
    the wrapped call beside this, under `HAJER_CAPTURE_CONTENT` and `HAJER_CAPTURE_RAW` — so what an
    attached process discloses by default is shapes, names, counts, timing and usage, and turning
    content capture on widens one field of one object rather than three places at once.
    """
    return {
        "provider": call.provider,
        "api": call.api,
        "model": call.model,
        "startedAt": call.started_at,
        "messageCount": call.message_count,
        "messageRoles": list(call.message_roles),
        "declaredTools": list(call.declared_tools),
        "requestSettings": dict(call.request_settings),
    }


def attach_output(call: WrappedCall) -> JsonObject:
    """The answer half: what came back, how long it took, and what it cost in tokens.

    `toolCalls` is the names the model asked for and never their arguments — a name is the shape of the
    answer and an argument is the customer's data.
    """
    output: JsonObject = {
        "responseId": call.response_id,
        "finishReason": call.finish_reason,
        "usage": dict(call.usage),
        "durationMs": call.duration_ms,
        "streamed": call.streamed,
        "streamComplete": call.stream_complete,
        "streamChunks": call.stream_chunks,
        "toolCalls": [tool.name for tool in call.tool_calls],
        "error": call.error,
        "errorType": call.error_type,
    }
    if call.embedding is not None:
        # An embedding's answer is its shape: how many vectors, of how many dimensions. Never a vector.
        vectors, dimensions = call.embedding
        output["embedding"] = {"vectors": vectors, "dimensions": dimensions}
    return output


#: The keys of one submission body that carry a customer's own values, and are therefore what client
#: redaction walks. Everything else in the body is Hajer's own metadata — the mode, the verifier, the
#: idempotency key, the SDK identity, the case key — and a pass over those would be a pass that could
#: rewrite the keys the routing depends on.
_REDACTED_KEYS: Final[tuple[str, ...]] = ("request", "output", "evidence", "wrappedCalls")


def redacted_body(body: JsonObject, *, policy: ClientRedactionPolicy) -> JsonObject:
    """One submission body with every customer value in it redacted, and the report attached.

    **The wrapped calls are in it.** A raw capture is the provider's own request and response bytes, which
    is where a card number pasted into a support ticket actually ends up, and `HAJER_CAPTURE_RAW` is how a
    team opts into shipping them. Redacting the tuple and leaving the capture untouched would be the whole
    protection with a hole in the middle of it — so the capture is redacted **before the body is queued**,
    which is what `test_raw_capture_is_redacted_before_queue` asserts.

    The metadata keys are deliberately not walked (`_REDACTED_KEYS`): a pass that could rewrite
    `idempotencyKey` or `caseKey` would be a pass that can break routing to protect nothing.
    """
    values: JsonObject = {key: body[key] for key in _REDACTED_KEYS if key in body}
    redacted, report = redact_submission(values, policy=policy)
    if not isinstance(redacted, dict):  # pragma: no cover - the walk returns what it was given
        return body
    updated: JsonObject = {**body, **redacted}
    if report is not None:
        updated["clientRedaction"] = report.to_wire()
    return updated


def case_identity(
    *, case_key: str | None, request: JsonValue, volatile_paths: tuple[str, ...] = ()
) -> tuple[str, CaseKeySource]:
    """The caller's case key, or one derived from the request here — with the label that says which.

    Called before anything rewrites the request, which is the whole reason it is called on this side at
    all (`_case_key.py`). `volatile_paths` is what the *caller* says varies per attempt; the verifier's
    own declaration lives in its compiled document, which this process has never seen, and that
    asymmetry is why `CALLER` is the strongest source of the three.
    """
    if case_key is not None:
        return case_key, "CALLER"
    return derive_case_key(request, volatile_paths=volatile_paths), "DERIVED_CLIENT"


def derive_idempotency_key(
    *,
    team_id: str,
    verifier: str | None,
    request: JsonValue,
    output: JsonValue,
    evidence: Mapping[str, JsonValue] | None,
) -> str:
    """Bind the submission to tenant + verifier + payload, and to nothing that varies by attempt.

    Timing, usage and the wrapped calls are deliberately **not** in the digest: the same logical
    submission retried from a different process, or replayed from a queue, must produce the same key,
    and a stream that took 40 ms longer the second time is not a different submission. A caller who
    has a request id of their own should pass `idempotency_key=` instead — theirs is better than any
    digest, because it survives a payload the application itself regenerated.
    """
    payload: JsonObject = {
        "request": request,
        "output": output,
        "evidence": dict(evidence) if evidence is not None else {},
    }
    payload_digest = hashlib.sha256(encode_body(payload, limit=1 << 30)).hexdigest()
    seed = "\n".join((IDEMPOTENCY_SCHEME, team_id, verifier or "", payload_digest)).encode("utf-8")
    return f"{IDEMPOTENCY_SCHEME}_{hashlib.sha256(seed).hexdigest()}"


def parse_assessment(raw: bytes) -> Assessment | None:
    """The assessment, or `None` when the body is not one — never an exception into a request path."""
    try:
        return Assessment.model_validate_json(raw)
    except ValidationError:
        return None


def parse_observations(raw: bytes) -> tuple[ObservationRow, ...]:
    """One page of the tail, or an empty tuple when the body is not one — never an exception."""
    try:
        return _PAGE.validate_json(raw)
    except ValidationError:
        return ()


def parse_observation_ids(raw: bytes) -> tuple[str, ...]:
    """The ids the server assigned to an accepted batch, in order; empty when it named none."""
    try:
        accepted = _AcceptedBatch.model_validate_json(raw)
    except ValidationError:
        return ()
    return accepted.observation_ids


class _AcceptedBatch(BaseModel):
    """What the server answers a flush with: one id per observation it took, in the order sent."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore")

    observation_ids: tuple[str, ...] = ()

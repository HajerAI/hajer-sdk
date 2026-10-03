"""One case, once: guard, call the entry, classify fail-closed, then `verify` what the model produced.

The outcome is decided from what the sandbox saw before what the application said: an application that
catches a refused send and returns something anyway is still UNABLE_TO_VERIFY, because its output was
computed without the effect or the state it asked for. The order is DB_REQUIRED, BLOCKED_EFFECT,
NETWORK_UNRECORDED, then the entry's own refusal (MISSING_DEPENDENCY, ADAPTER_MISSING, PARSE), then an
application exception (APP_ERROR, naming only the exception's class — its message may quote customer
data), then the provider: a provider answer outside 2xx, or no provider answer at all, is
MISSING_DEPENDENCY — an output computed without the model (a key the replay does not have, a graceful
fallback) is not the model's behaviour. Only then REPLAYED, and only a REPLAYED attempt is sent to
`verify` — the reply's check subject and the capture evidence the generated verifier's contract reads
(`_capture.py`), marked `evidence.hajerReplay` so production reads can leave it out. When Hajer is configured
and no observation comes back, the capture is unbound and the attempt is FAILED with the output kept; a
verifier in SHADOW answers `unavailable{SHADOW}` to the application, but its observation was captured and
assessed, so that observation id is kept. Every other `unavailable` (no verifier, an error, a timeout) is
unbound. A recorded boundary served on method and URL alone (a recording older than request digests) marks
the receipt `limitations: [WEAK_BOUNDARY_MATCH]`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import json
import math
from collections.abc import Coroutine, Mapping
from typing import TypeAlias, cast

from pydantic import BaseModel, JsonValue

from hajer._client import Hajer
from hajer._replay_marker import REPLAY_MARKER
from hajer._settings import CI_ENVIRONMENT, HajerSettings
from hajer.replay._capture import captured
from hajer.replay._entry import EntryUnavailable, prepare_call
from hajer.replay._guard import Sandbox

Receipt: TypeAlias = dict[str, JsonValue]
_ORDER = ("DB_REQUIRED", "BLOCKED_EFFECT", "NETWORK_UNRECORDED")


class UnserialisableError(ValueError):
    pass


def as_json(value: object) -> JsonValue:
    """The application's return value as JSON, or `UnserialisableError` — never `str()` of an object."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise UnserialisableError("a non-finite number")
        return value
    if isinstance(value, BaseModel):
        return cast(JsonValue, value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return as_json(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in items):
            raise UnserialisableError("a mapping with non-string keys")
        return {str(key): as_json(item) for key, item in items.items()}
    if isinstance(value, list | tuple):
        return [as_json(item) for item in cast(list[object] | tuple[object, ...], value)]
    raise UnserialisableError(f"a {type(value).__name__}")


def provider_usage(body: bytes) -> JsonValue:
    """`{model, inputTokens, outputTokens}` from an Anthropic- or OpenAI-shaped response, else None (unpriced)."""
    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    payload = cast(dict[str, object], document)
    usage, model = payload.get("usage"), payload.get("model")
    if not isinstance(usage, dict) or not isinstance(model, str):
        return None
    counts = cast(dict[str, object], usage)
    prompt = counts.get("input_tokens", counts.get("prompt_tokens"))
    completion = counts.get("output_tokens", counts.get("completion_tokens"))
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    return {"model": model, "inputTokens": prompt, "outputTokens": completion}


def _call(spec: Mapping[str, JsonValue]) -> tuple[object, str | None]:
    """(the result, or the qualified class of the exception the application raised instead)."""
    adapter = cast(Mapping[str, JsonValue], spec["adapter"])
    prepared = prepare_call(adapter, spec["input"])
    try:
        result = prepared.target(*prepared.args, **prepared.kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(cast(Coroutine[object, object, object], result))
    except Exception as error:  # noqa: BLE001 - the application's own failure is an outcome, recorded by class
        kind = type(error)
        return None, f"{kind.__module__}.{kind.__qualname__}"
    return result, None


def replay_marker(spec: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The evidence every replay `verify` carries, so a production read can tell a replay from traffic."""
    return {REPLAY_MARKER: {key: spec[key] for key in ("runId", "planId", "caseId", "attempt")}}


def _provider_missing(statuses: list[int]) -> bool:
    return not statuses or any(not 200 <= status < 300 for status in statuses)


def _verify(
    spec: Mapping[str, JsonValue], output: JsonValue, api_key: str | None, bodies: list[bytes]
) -> tuple[str | None, str, bool]:
    """(observation id, what happened, whether a verify was sent). Only a pinned verifier is sent."""
    hajer = spec.get("hajer")
    verifier = str(cast(Mapping[str, JsonValue], spec["adapter"])["verifier"])
    if not isinstance(hajer, dict) or api_key is None:
        return None, "Hajer is not configured for this replay; nothing was sent to verify", False
    if "@" not in verifier:
        return None, f"verifier {verifier!r} is not pinned (name@version); the output is kept, not verified", False
    identity = {key: spec[key] for key in ("runId", "planId", "caseId", "attempt", "workflowId", "revisionLabel")}
    key = "replay-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    # A replay is CI traffic whatever the shell around the child was tagged: `ci` is never a test input (D7).
    settings = HajerSettings(
        api_key=api_key, team_id=str(hajer["teamId"]), base_url=str(hajer["baseUrl"]), environment=CI_ENVIRONMENT
    )
    subject, evidence = captured(bodies, output)
    with Hajer(settings=settings) as client:
        assessment = client.verify(
            verifier, spec["input"], subject, {**evidence, **replay_marker(spec)}, idempotency_key=key
        )
    shadow = assessment.status == "unavailable" and assessment.reason == "SHADOW"
    if (assessment.status == "unavailable" and not shadow) or not assessment.observation_id:
        return None, "verify returned no observation: the capture is unbound", True
    if shadow:
        return assessment.observation_id, "verify unavailable to the application (SHADOW); observation kept", True
    return assessment.observation_id, f"verify {assessment.status}", True


def run_case(spec: Mapping[str, JsonValue], sandbox: Sandbox, api_key: str | None) -> Receipt:
    receipt: Receipt = {"outcome": "FAILED", "reason": None, "rawOutput": None, "observationId": None}
    entry_reason: str | None = None
    output: JsonValue = None
    failure: str | None = None
    raised: str | None = None
    try:
        result, raised = _call(spec)
        if raised is None:
            output = as_json(result)
    except EntryUnavailable as error:
        entry_reason, failure = error.reason, str(error)
    except UnserialisableError as error:
        entry_reason, failure = "PARSE", f"the entry returned {error}, which is not JSON"
    record = sandbox.record
    receipt["providerUsage"] = [provider_usage(body) for body in record.provider_bodies]
    receipt["providerStatuses"] = list(record.provider_statuses)
    if record.weak_matches:
        receipt["limitations"] = ["WEAK_BOUNDARY_MATCH"]
    refused = next((reason for reason in _ORDER if reason in record.refusals), None)
    if refused is not None or entry_reason is not None:
        receipt.update(outcome="UNABLE_TO_VERIFY", reason=refused or entry_reason, detail=failure)
        return receipt
    if raised is not None:
        receipt.update(outcome="UNABLE_TO_VERIFY", reason="APP_ERROR", exceptionClass=raised)
        return receipt
    if _provider_missing(record.provider_statuses):
        detail = "no provider call answered 2xx: the output was computed without the model"
        receipt.update(outcome="UNABLE_TO_VERIFY", reason="MISSING_DEPENDENCY", detail=detail)
        return receipt
    sandbox.report()
    observation_id, detail, sent = _verify(spec, output, api_key, record.provider_bodies)
    unbound = sent and observation_id is None
    receipt.update(
        outcome="FAILED" if unbound else "REPLAYED", rawOutput=output, observationId=observation_id, detail=detail
    )
    return receipt

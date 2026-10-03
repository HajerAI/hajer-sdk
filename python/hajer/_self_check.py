"""Local synthetic capture, with explicit single-attempt delivery only for doctor --emit."""

from __future__ import annotations

import httpx

from hajer._client import Hajer
from hajer._errors import BodyOverBoundError
from hajer._json import JsonObject
from hajer._redact import build_policy, redact_document
from hajer._settings import HajerSettings
from hajer._wrap import WrappedCall, instrument, scope


class _Messages:
    def create(self, **kwargs: object) -> JsonObject:
        del kwargs
        return {
            "id": "doctor",
            "model": "hajer-doctor-fake",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class _Client:
    def __init__(self) -> None:
        self.messages = _Messages()


def _synthetic_call(settings: HajerSettings) -> WrappedCall | None:
    client = _Client()
    # The self-check deliberately uses no external exporter and never proves hosted ingestion.
    with scope() as operation:
        instrument(
            client,
            settings.model_copy(update={"capture_raw": False, "capture_call_site": False}),
        )
        client.messages.create(model="hajer-doctor-fake", messages=[{"role": "user", "content": "doctor@example.com"}])
        calls = operation.calls
    return calls[0] if calls else None


def self_check(settings: HajerSettings) -> JsonObject:
    call = _synthetic_call(settings)
    redacted, _ = redact_document(call.to_wire() if call is not None else {}, policy=build_policy())
    return {
        "recorded": call is not None,
        "wouldEmit": call is not None and not settings.inert,
        "evidence": "local synthetic call; hosted delivery unverified",
        "trace": redacted,
    }


class _DoctorClient(Hajer):
    def send_once(self, call: WrappedCall) -> tuple[str, ...]:
        """Reuse the authenticated, bounded, redacted wire path without queue retries."""
        body, _ = self._call_submission(call)
        return self._send_batch((body,))


def emit_self_check(settings: HajerSettings, *, transport: httpx.BaseTransport | None) -> JsonObject:
    """No permalink exists in the current observe receipt: never turn acceptance into verification."""
    report: JsonObject = {
        "status": "configured but unverified",
        "observationId": None,
        "permalink": None,
        "detail": "Backend observe receipt has no permalink contract; delivery cannot be verified by link.",
    }
    if settings.inert:
        report["detail"] = "Set HAJER_API_KEY and HAJER_TEAM_ID and unset HAJER_DISABLED to emit. Nothing sent."
        return report
    call = _synthetic_call(settings)
    if call is None:
        report["detail"] = "Local synthetic capture failed. Nothing sent."
        return report
    try:
        client = _DoctorClient(settings=settings.model_copy(update={"redact_client": True}), transport=transport)
        with client:
            ids = client.send_once(call)
        if len(ids) == 1 and ids[0]:
            report["observationId"] = ids[0]
        else:
            report["detail"] = "Backend did not return one accepted observation ID; receipt unverified."
    except (httpx.HTTPError, httpx.InvalidURL, BodyOverBoundError, ValueError):
        report["detail"] = "Synthetic delivery failed; receipt unverified."
    return report

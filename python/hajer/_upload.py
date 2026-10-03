"""Upload only compact receipts, on explicit request, using the existing bounded SDK transport."""

from uuid import uuid4

import httpx
from pydantic import JsonValue

from hajer._errors import BodyOverBoundError
from hajer._settings import HajerSettings
from hajer._transport import Deadline, SyncTransport, encode_body


def _wire_receipt(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Attempt provenance stays local until the API explicitly versions that evidence contract."""
    suites = payload.get("suites")
    if not isinstance(suites, list):
        return payload
    result: list[JsonValue] = []
    for suite in suites:
        if not isinstance(suite, dict) or not isinstance(cases := suite.get("cases"), list):
            result.append(suite)
            continue
        cleaned: list[JsonValue] = [
            {key: value for key, value in case.items() if key not in {"executionEvidence", "adapterCheck"}}
            if isinstance(case, dict)
            else case
            for case in cases
        ]
        result.append({**suite, "cases": cleaned})
    return {**payload, "suites": result}


def upload(
    payload: dict[str, JsonValue],
    project_id: str,
    settings: HajerSettings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    if not settings.api_key or not settings.team_id:
        return False
    deadline = Deadline(settings.observe_flush_deadline_ms)
    client = SyncTransport(base_url=settings.base_url, api_key=settings.api_key, transport=transport)
    try:
        body = encode_body(_wire_receipt(payload), limit=settings.body_max_bytes)
        response = client.post(
            f"/api/teams/{settings.team_id}/projects/{project_id}/suite-runs",
            body,
            idempotency_key=str(uuid4()),
            timeout_s=deadline.timeout_s(),
        )
        return response.status_code == 201
    except (httpx.HTTPError, BodyOverBoundError):
        return False
    finally:
        client.close()

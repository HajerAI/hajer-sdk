"""Fetch approved traffic bindings into the CI parent's memory, never a repository or artifact.

Only live suites opt in. The backend rechecks source, current suite and promotion custody;
this reader checks exact suite bytes and rejects extra cases or recorded responses. Existing
per-attempt private files carry an admitted input to the application and are then removed.
"""

import hashlib
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

from hajer._errors import BodyOverBoundError
from hajer._paths import PRIVATE_INPUTS_PATH
from hajer._settings import HajerSettings
from hajer._transport import Deadline, SyncTransport, encode_body
from hajer.pytest_plugin._models import Binding, Suite

_DOCUMENT = TypeAdapter(dict[str, JsonValue])
REFUSED = "PRIVATE_CI_INPUTS_UNAVAILABLE: no private case may run without its exact approved binding"


def hydrate(
    suite: Suite,
    path: Path,
    project_id: str | None,
    settings: HajerSettings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    raw = path.read_bytes()
    document = _DOCUMENT.validate_json(raw)
    members = document.get("members")
    wanted: set[str] = set()
    for member in members if isinstance(members, list) else []:
        case = member.get("case") if isinstance(member, dict) else None
        export = case.get("inputExport") if isinstance(case, dict) else None
        if isinstance(case, dict) and isinstance(export, dict) and export.get("sensitivity") == "TRAFFIC_DERIVED":
            case_id = case.get("id")
            if isinstance(case_id, str):
                wanted.add(case_id)
    if not wanted:
        return
    if not project_id or not settings.api_key or not settings.team_id or not settings.ci_commit_sha:
        raise ValueError(REFUSED)  # CI must not silently fall back to an unapproved traffic binding.
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    request: dict[str, JsonValue] = {
        "suiteId": suite.suite_id,
        "suiteVersion": suite.version,
        "suiteDigest": digest,
        "revision": settings.ci_commit_sha,
    }
    client = SyncTransport(base_url=settings.base_url, api_key=settings.api_key, transport=transport)
    deadline = Deadline(settings.ci_input_deadline_ms)
    try:
        response = client.post_bounded(
            PRIVATE_INPUTS_PATH.format(team_id=settings.team_id, project_id=project_id),
            encode_body(request, limit=settings.body_max_bytes),
            idempotency_key=str(uuid4()),
            timeout_s=deadline.timeout_s(),
            max_bytes=settings.ci_input_max_bytes,
        )
        if response.status_code != 200 or len(response.content) > settings.ci_input_max_bytes:
            raise ValueError(REFUSED)
        result = _DOCUMENT.validate_json(response.content)
        encoded, unavailable = result.get("executionDocument"), result.get("unavailableCaseIds")
        if (
            not isinstance(encoded, str)
            or not isinstance(unavailable, list)
            or result.get("revision") != settings.ci_commit_sha
        ):
            raise ValueError(REFUSED)
        binding = Binding.model_validate_json(encoded)
        if (binding.suite_id, binding.suite_version, binding.suite_digest) != (suite.suite_id, suite.version, digest):
            raise ValueError(REFUSED)
        if unavailable or set(binding.cases) != wanted:
            raise ValueError(REFUSED)
        if set(binding.cases) & set(unavailable) or any(case.boundaries for case in binding.cases.values()):
            raise ValueError(REFUSED)
        for case in suite.cases:
            if case.case_id in wanted:
                case.execution = binding.cases.get(case.case_id)
        suite.private_inputs = bool(binding.cases)
    except (httpx.HTTPError, BodyOverBoundError, ValueError, ValidationError):
        raise ValueError(REFUSED) from None
    finally:
        client.close()

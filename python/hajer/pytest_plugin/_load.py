"""A committed suite is executable only with an exact, explicit execution binding."""

import hashlib
import json
from pathlib import Path
from typing import cast

from pydantic import JsonValue, ValidationError

from hajer._temporary import read_temporary_binding
from hajer.pytest_plugin._models import Binding, Case, Suite
from hajer.pytest_plugin._situations import frame_choices, keyed_choices


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")  # CI configuration errors must fail collection.
    return value


def _binding(path: Path, raw: bytes, suite_id: str, version: int, temporary: Path | None) -> Binding | None:
    sidecar = path.parent.parent / "executions" / path.name
    if temporary is None and not sidecar.is_file():
        return None
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    payload = sidecar.read_bytes() if temporary is None else read_temporary_binding(temporary, path, digest)
    try:
        binding = Binding.model_validate_json(payload)
    except (ValueError, ValidationError):
        return None
    if (binding.suite_id, binding.suite_version, binding.suite_digest) != (suite_id, version, digest):
        return None
    return binding


def load_suite(path: Path, *, temporary: Path | None = None) -> Suite:
    raw = path.read_bytes()
    document = object_value(cast(JsonValue, json.loads(raw)))
    suite_id, version, members = document.get("suiteId"), document.get("version"), document.get("members")
    if not isinstance(suite_id, str) or not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Suite identity/version is missing")
    if not isinstance(members, list) or not members:
        raise ValueError("Suite has no cases")
    binding = _binding(path, raw, suite_id, version, temporary)
    keyed = keyed_choices(document)
    cases: dict[str, Case] = {}
    for raw_member in members:
        member = object_value(raw_member)
        case_id = object_value(member.get("case"))["id"]
        check = object_value(member.get("check"))
        if not isinstance(case_id, str) or not isinstance(object_value(check.get("check")).get("id"), str):
            raise ValueError("A member has no case/check identity")
        if case_id not in cases:
            execution = binding.cases.get(case_id) if binding else None
            case = object_value(member.get("case"))
            origin = case.get("origin")
            situations = frame_choices(case, keyed)
            cases[case_id] = Case(case_id, [], execution, situations, origin if isinstance(origin, str) else None)
        cases[case_id].checks.append(check)
    site = document.get("site")
    if temporary is not None and (binding is None or set(binding.cases) != set(cases)):
        # Exact membership is mandatory for the explicit local exception; no extra case can borrow its scope.
        raise ValueError("TEMPORARY_LOCAL_INPUT_BINDING_MISMATCH")
    shadow = {
        str(identity["id"])
        for raw_member in members
        if isinstance(raw_member, dict)
        and isinstance(check := raw_member.get("check"), dict)
        and check.get("mode") == "SHADOW"
        and isinstance(identity := check.get("check"), dict)
    }
    return Suite(
        suite_id,
        version,
        path.parent.parent.parent,
        list(cases.values()),
        site=site if isinstance(site, str) else None,
        shadow=shadow,
        temporary=temporary is not None,
        workflow_id=str(document["workflowKey"]) if isinstance(document.get("workflowKey"), str) else None,
    )

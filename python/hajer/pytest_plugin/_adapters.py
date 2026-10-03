"""A suite's cases run only through an adapter shown to reach the suite's site; an unverified one leaves them neutral.

Before a suite's first case runs, its adapter is checked once this session the way `python -m hajer verify-adapters`
checks it (`hajer._verify_adapters.check_adapter`: one call under the full replay guards, a fake model, no network),
against the site the adapter declares, else the suite's own `site`. Only VERIFIED runs the cases. Any other status
leaves each case neutral: it is recorded as skipped (ADAPTER_UNVERIFIED, with the status and why), never a PASS and
never a failed check, because a verdict computed through an adapter that does not drive the site would be a verdict
about other code. With no site known at all there is nothing to check against, and the case is neutral with the named
reason ADAPTER_SITE_UNKNOWN. Persisted `verify-adapters` reports are informational: they do not establish reachability
for the current checkout and are never used to authorize execution. A suite bound from the explicit temporary local
proof bundle has no committed binding for the check to read an input from, so each of its cases is checked with its
own authored input (`Authored`), and a case whose input differs is checked again. The session's `adapters` list keeps
one result per adapter and site, the last one, so each such case also carries its own check (`case_adapter`).
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Final

from pydantic import JsonValue

from hajer._settings import HajerSettings, clipped
from hajer._verify_adapters import REPLAY_CONFIG, Authored, check_adapter, declared_adapters, upload_identity
from hajer.pytest_plugin._models import Case, State, Suite

ADAPTER_UNVERIFIED: Final = "ADAPTER_UNVERIFIED"
SITE_UNKNOWN: Final = (
    "ADAPTER_SITE_UNKNOWN: neither the adapter nor the suite names the model call site it drives, so a call of it "
    "cannot be shown to reach it"
)


def _environment(suite: Suite) -> dict[str, str]:
    if not (suite.root / REPLAY_CONFIG).is_file():
        return {}
    try:
        return declared_adapters(suite.root)[1]
    except (OSError, ValueError):
        return {}


def adapter_status(state: State, suite: Suite, case: Case, settings: HajerSettings) -> tuple[str, str] | None:
    """(status, why) of the adapter `case` runs through, checked once per run against its site; None only for a case
    with no execution binding, which is refused on its own. With no site known at all (the adapter names none and the
    suite names none), there is nothing to check against: the case is neutral, ADAPTER_SITE_UNKNOWN, never green."""
    if case.execution is None:
        return None
    adapter: dict[str, JsonValue] = dict(case.execution.adapter)
    declared = adapter.get("site")
    key = _key(suite, case)
    site = key[1]
    environment = _environment(suite)
    bound: list[object] = [str(suite.root.resolve()), adapter, environment]
    if suite.temporary or suite.private_inputs:
        bound.append(case.execution.input)
    binding = sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
    if isinstance(declared, str) and suite.site is not None and declared != suite.site:
        result = ("UNVERIFIED", "ADAPTER_SITE_MISMATCH: the adapter and suite name different model call sites")
        state.adapters[key] = result
        state.adapter_bindings.pop(key, None)
        return result
    if site is None:
        state.adapters[key] = ("UNVERIFIED", SITE_UNKNOWN)
        return state.adapters[key]
    if key not in state.adapters or state.adapter_bindings.get(key) != binding:
        document: dict[str, JsonValue] = {**adapter, "site": site}
        if suite.temporary or suite.private_inputs:
            authored = Authored(case.execution.input)
            check = check_adapter(suite.root, document, environment=environment, settings=settings, authored=authored)
        else:
            check = check_adapter(suite.root, document, environment=environment, settings=settings)
        state.adapters[key] = (check.status, check.detail)
        state.adapter_bindings[key] = binding
    return state.adapters[key]


def _key(suite: Suite, case: Case) -> tuple[str, str | None]:
    """(adapter id, site) the case's adapter is checked under: the site it declares, else the suite's."""
    adapter: dict[str, JsonValue] = {} if case.execution is None else dict(case.execution.adapter)
    declared, named = adapter.get("site"), adapter.get("adapterId")
    site = declared if isinstance(declared, str) else suite.site
    return (named if isinstance(named, str) else f"{adapter.get('module')}:{adapter.get('qualname')}", site)


def case_adapter(suite: Suite, case: Case, checked: tuple[str, str]) -> dict[str, JsonValue]:
    """The adapter check this one case ran under, as its own receipt entry (review N4): in temporary mode each case is
    checked with its own input, while the session's `adapters` list keeps only the last check per adapter and site."""
    adapter_id, site = _key(suite, case)
    status, detail = checked
    return {
        "adapterId": upload_identity(adapter_id),
        "site": None if site is None else upload_identity(site),
        "status": status,
        "detail": clipped(detail),
    }


def neutral(case: Case, status: str, detail: str) -> dict[str, JsonValue]:
    """The case's result when its adapter is not VERIFIED: no check read, never green, never a failed check."""
    checks: list[JsonValue] = [
        {
            "checkId": identity.get("id"),
            "verdict": "UNABLE_TO_VERIFY",
            "label": "not_yet",
            "attempts": ["UNABLE_TO_VERIFY"],
            "reason": "APP_EXECUTION_UNAVAILABLE: ADAPTER_UNVERIFIED",
        }
        for check in case.checks
        if isinstance(identity := check.get("check"), dict)
    ]
    return {
        "caseId": case.case_id,
        "checks": checks,
        "skipped": ADAPTER_UNVERIFIED,
        "adapter": {"status": status, "detail": clipped(detail)},
        "durationMs": 0,
    }


def adapter_documents(state: State) -> list[JsonValue]:
    """Every adapter checked this session, as the upload carries them."""
    return [
        {
            "adapterId": upload_identity(adapter_id),
            "site": None if site is None else upload_identity(site),
            "status": status,
            "detail": clipped(detail),
        }
        for (adapter_id, site), (status, detail) in sorted(state.adapters.items(), key=lambda item: str(item[0]))
    ]

"""Blocking decisions use published evaluator identities; raw evidence is never rewritten."""

from typing import Literal, cast

from pydantic import JsonValue

from hajer.pytest_plugin._models import Case, Suite


def evaluator(case: Case, check_id: JsonValue) -> str:
    found = {
        str(identity.get("evaluator", "UNKNOWN"))
        for definition in case.checks
        if isinstance(identity := definition.get("check"), dict) and identity.get("id") == check_id
    }
    if found == {"SCOPED_SEMANTIC_JUDGE"}:
        return "JUDGE"
    return "PREDICATE" if found == {"REVIEWED_PREDICATE"} else "UNKNOWN"


def disposition(suite: Suite, result: dict[str, JsonValue]) -> Literal["FAIL", "NEUTRAL", "PASS"]:
    case = next((case for case in suite.cases if case.case_id == result.get("caseId")), None)
    if case is None or result.get("skipped") == "ADAPTER_UNVERIFIED":
        return "FAIL"
    if result.get("skipped"):
        return "NEUTRAL"
    checks = cast(list[dict[str, JsonValue]], result.get("checks") or [])
    if not checks:
        return "NEUTRAL" if result.get("liveOnly") else "FAIL"
    waiting = cast(list[dict[str, JsonValue]], result.get("liveOnly") or [])
    reported = [check.get("checkId") for check in [*checks, *waiting]]
    if any(
        not isinstance(identity := definition.get("check"), dict) or identity.get("id") not in reported
        for definition in case.checks
    ):
        return "FAIL"
    neutral = bool(waiting)
    for check in checks:
        kind = evaluator(case, check.get("checkId"))
        reason = check.get("reason")
        if reason == "NOT_EXECUTED" or (isinstance(reason, str) and reason.startswith("APP_EXECUTION_UNAVAILABLE:")):
            return "FAIL"
        if kind == "UNKNOWN":
            neutral = True
        elif check.get("verdict") != "PASS":
            if kind == "PREDICATE" and check.get("checkId") not in suite.shadow:
                return "FAIL"
            neutral = True
    return "NEUTRAL" if neutral else "PASS"

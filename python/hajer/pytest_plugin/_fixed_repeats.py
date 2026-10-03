"""Explicit extra measurements never enter adaptive qualification or the CI blocking decision.

Collection writes the prospective case/attempt manifest. Each case freezes its exact request configuration
from the ordinary run before any extra attempt; per-check plans and empty measurements exist before dispatch.
Atomic snapshots keep completed observations on interruption, while absent attempts remain in each plan.
"""

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest
from pydantic import JsonValue

from hajer._settings import HajerSettings
from hajer._temporary import write_private
from hajer.pytest_plugin._evaluate import evaluate, reads_tool_calls
from hajer.pytest_plugin._models import Attempt, Case, RepeatAttempt, RepeatMeasurement, RepeatPlan, RepeatScope, Suite
from hajer.pytest_plugin._run import execute, live_only


def _digest(value: JsonValue) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _atomic(path: Path, text: str) -> None:
    temporary = path.with_name(uuid4().hex + ".tmp")
    try:
        write_private(temporary, text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Pending:
    suite: Suite
    case: Case
    attempt_ids: tuple[str, ...]
    scopes: tuple[RepeatScope, ...]
    key: str
    finished: bool = False
    measurements: list[tuple[Path, RepeatMeasurement]] = field(default_factory=list)


class FixedRepeats:
    def __init__(self, root: Path, count: int, team: str, project: str, revision: str, settings: HajerSettings) -> None:
        self.root, self.count, self.team, self.project, self.revision = root, count, team, project, revision
        self.settings = settings
        self.pending: list[Pending] = []
        self.stopped = False
        root.mkdir(parents=True, mode=0o700, exist_ok=False)

    def register(self, suite: Suite) -> None:
        planned = sum(len(pending.scopes) for pending in self.pending)
        if planned + sum(len(case.checks) for case in suite.cases) > 512:
            raise pytest.UsageError("Fixed-repeat plan exceeds the receipt limit of 512 input/check measurements")
        for case in suite.cases:
            key = uuid4().hex
            declared = _digest(cast(JsonValue, case.execution.model_dump()) if case.execution else None)
            scopes = tuple(
                RepeatScope(
                    team_id=self.team,
                    project_id=self.project,
                    workflow_id=suite.workflow_id or "",
                    revision=self.revision,
                    suite_id=suite.suite_id,
                    suite_version=suite.version,
                    case_id=case.case_id,
                    check_id=str(cast(dict[str, JsonValue], check["check"])["id"]),
                    configuration_digest=declared,
                )
                for check in case.checks
            )
            pending = Pending(suite, case, tuple(uuid4().hex for _ in range(self.count)), scopes, key)
            manifest = {
                "caseId": case.case_id,
                "suiteId": suite.suite_id,
                "suiteVersion": suite.version,
                "plannedAttemptIds": pending.attempt_ids,
                "checkIds": [scope.check_id for scope in scopes],
                "createdAt": datetime.now(UTC).isoformat(),
                "configuration": "FROZEN_BEFORE_EXTRA_ATTEMPTS",
            }
            write_private(self.root / f"{key}.manifest.json", json.dumps(manifest))
            self.pending.append(pending)

    def _freeze(self, pending: Pending, reference: str | None) -> list[tuple[Path, RepeatMeasurement]]:
        if pending.measurements:
            return pending.measurements
        measurements: list[tuple[Path, RepeatMeasurement]] = []
        for scope in pending.scopes:
            plan_id = uuid4().hex
            config = _digest({"execution": scope.configuration_digest, "request": reference, "revision": self.revision})
            plan = RepeatPlan(
                plan_id=plan_id,
                scope=scope.model_copy(update={"configuration_digest": config}),
                planned_attempt_ids=pending.attempt_ids,
                planned_at=datetime.now(UTC),
            )
            measurement = RepeatMeasurement(plan=plan)
            write_private(self.root / f"{plan_id}.plan.json", measurement.model_dump_json())
            path = self.root / f"{plan_id}.measurement.json"
            write_private(path, measurement.model_dump_json())
            measurements.append((path, measurement))
        pending.measurements = measurements
        return measurements

    def prepare(self) -> None:
        for pending in self.pending:
            empty: dict[str, JsonValue] = {}
            result = next(
                (value for value in pending.suite.results if value.get("caseId") == pending.case.case_id), empty
            )
            self._freeze(pending, _reference(result))

    def collect(self, suite: Suite, case: Case, result: dict[str, JsonValue]) -> None:
        pending = next(item for item in self.pending if item.suite is suite and item.case is case)
        if pending.finished:
            return
        reference = _reference(result)
        try:
            measurements = self._freeze(pending, reference)
            pending.finished = True
            if reference is None or self.stopped or all(live_only(check) for check in case.checks):
                return
            for attempt_id in pending.attempt_ids:
                attempt = execute(suite, case, live=True, settings=self.settings)
                observed = datetime.now(UTC)
                for index, ((path, measurement), check) in enumerate(zip(measurements, case.checks, strict=True)):
                    outcome, reason = _outcome(check, case, attempt, reference)
                    reading = RepeatAttempt(
                        attempt_id=attempt_id,
                        configuration_digest=measurement.plan.scope.configuration_digest,
                        outcome=outcome,
                        reason=reason,
                        request_fingerprint=attempt.request_fingerprint,
                        evidence_id=attempt_id,
                        observed_at=observed,
                    )
                    updated = measurement.model_copy(update={"attempts": (*measurement.attempts, reading)})
                    _atomic(path, updated.model_dump_json())
                    measurements[index] = (path, updated)
        except OSError:
            # Never make another paid attempt after its evidence could not be retained. Original CI policy is unchanged.
            self.stopped = True
            # A warning filter must not promote this advisory collection failure into a failed CI job.
            sys.stderr.write(
                "HAJER_FIXED_REPEAT_EVIDENCE_UNAVAILABLE: collection stopped; planned attempts may be missing\n"
            )

    def summary(self) -> str:
        measurements = [value for pending in self.pending for _, value in pending.measurements]
        attempts = [attempt for value in measurements for attempt in value.attempts]
        planned = sum(len(pending.scopes) * len(pending.attempt_ids) for pending in self.pending)
        passed = sum(attempt.outcome == "SATISFIES" for attempt in attempts)
        failed = sum(attempt.outcome == "VIOLATES" for attempt in attempts)
        unknown = len(attempts) - passed - failed
        return (
            f"Fixed-repeat measurements: {passed} passed, {failed} failed, {unknown} unknown, "
            f"{planned - len(attempts)} missing / {planned} planned check-attempts; advisory only."
        )

    def documents(self) -> list[JsonValue]:
        """The same frozen measurements retained locally, included in the ordinary CI receipt."""
        return [
            cast(JsonValue, measurement.model_dump(mode="json"))
            for pending in self.pending
            for _, measurement in pending.measurements
        ]

    def finish(self) -> None:
        for pending in self.pending:
            if not pending.finished:
                self._freeze(pending, None)
                pending.finished = True


def _reference(result: dict[str, JsonValue]) -> str | None:
    evidence = result.get("executionEvidence")
    if not isinstance(evidence, list):
        return None
    for item in reversed(evidence):
        if isinstance(item, dict) and item.get("mode") == "live" and not item.get("reason"):
            fingerprint = item.get("requestFingerprint")
            if isinstance(fingerprint, str):
                return fingerprint
    return None


def _outcome(
    check: dict[str, JsonValue], case: Case, attempt: Attempt, reference: str
) -> tuple[Literal["SATISFIES", "VIOLATES", "UNKNOWN"], str | None]:
    if attempt.reason:
        reason = (
            attempt.reason
            if attempt.reason
            in {"TIMEOUT", "CHILD_FAILED", "CI_BUDGET_EXHAUSTED", "CI_SPEND_NOT_CONFIGURED", "CI_USAGE_UNAVAILABLE"}
            else "EXECUTION_UNAVAILABLE"
        )
        return "UNKNOWN", reason
    if attempt.mode != "live" or not attempt.provider_successes:
        return "UNKNOWN", "NO_LIVE_PROVIDER_EVIDENCE"
    if attempt.request_fingerprint != reference:
        return "UNKNOWN", "REQUEST_CONFIGURATION_CHANGED"
    if live_only(check):
        return "UNKNOWN", "EXTRA_JUDGE_OR_METAMORPHIC_CALL_NOT_AUTHORIZED"
    value = attempt.reply if reads_tool_calls(check) else attempt.output
    verdict = evaluate(check, value, request=None if case.execution is None else case.execution.input)
    outcome: Literal["SATISFIES", "VIOLATES", "UNKNOWN"] = (
        "SATISFIES" if verdict == "PASS" else "VIOLATES" if verdict == "FAIL" else "UNKNOWN"
    )
    return outcome, ("CHECK_UNAVAILABLE" if verdict == "UNABLE_TO_VERIFY" else None)


def start_collection(config: pytest.Config, run_id: str) -> FixedRepeats | None:
    count = cast(int, config.getoption("hajer_fixed_repeats"))
    if count == 0:
        return None
    settings = HajerSettings.from_env()
    if count < 0 or count > settings.ci_fixed_repeat_limit:
        raise pytest.UsageError("Fixed repeat count exceeds HAJER_CI_FIXED_REPEAT_LIMIT or is negative")
    if not config.getoption("hajer_live") or not settings.ci_budget_file or not settings.ci_budget_microusd:
        raise pytest.UsageError("Fixed repeats require --hajer-live and an explicit nonzero CI budget and ledger")
    project = cast(str | None, config.getoption("hajer_project_id"))
    if not settings.team_id or not project or not settings.ci_commit_sha:
        raise pytest.UsageError("Fixed repeats require team, project, and exact commit identity")
    result = Path(str(config.getoption("hajer_results")))
    root = result.parent / (result.stem + "-repeats-" + run_id)
    return FixedRepeats(root, count, settings.team_id, project, settings.ci_commit_sha, settings)


def complete_collection(fixed: FixedRepeats, status: int) -> None:
    # Measurements run after ordinary tests so they cannot consume a later case's qualification budget.
    # An interrupt is never treated as permission to dispatch more calls during teardown.
    try:
        fixed.prepare()
    except OSError:
        fixed.stopped = True
        sys.stderr.write("HAJER_FIXED_REPEAT_EVIDENCE_UNAVAILABLE: no extra calls started\n")
        return
    if status not in {pytest.ExitCode.INTERRUPTED, pytest.ExitCode.INTERNAL_ERROR, pytest.ExitCode.USAGE_ERROR}:
        for pending in fixed.pending:
            result = next(
                (value for value in pending.suite.results if value.get("caseId") == pending.case.case_id), None
            )
            if result is not None:
                fixed.collect(pending.suite, pending.case, result)
    fixed.finish()

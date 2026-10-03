"""Fresh child per attempt; live attempts repeat by the shared qualification rule, never past its cap."""

import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from hajer._settings import HajerSettings, ci_execution_environment, clipped
from hajer._temporary import write_private
from hajer._verify_adapters import REPLAY_CONFIG, declared_adapters
from hajer.pytest_plugin._evaluate import evaluate, reads_tool_calls
from hajer.pytest_plugin._judge import JUDGE_UNFUNDED, SKIPPED, Judge, JudgeAnswer
from hajer.pytest_plugin._metamorphic import edited_input, relation_verdict
from hajer.pytest_plugin._models import Attempt, Case, Execution, Suite, Verdict
from hajer.pytest_plugin._qualification import next_repeat, read
from hajer.pytest_plugin._situations import DERIVED_MUTATION, NEEDS_LIVE_RUN, witness


def input_digest(spec: str) -> str:
    """`hajer-canonical-request-v1` over the input a child is handed: the `input` of the exact spec bytes it reads,
    as canonical JSON (sorted keys, no insignificant whitespace, UTF-8), the namespace the backend's custody records use
    (`suite_materialization.schemas.request_digest`). A proof compares it with the digest the product computes from the
    custodian it re-reads; only the digest is kept."""
    value = cast(dict[str, JsonValue], json.loads(spec))["input"]
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def execute(suite: Suite, case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
    if case.execution is None:
        return Attempt(reason="MISSING_OR_STALE_EXECUTION_BINDING")
    spec_text = case.execution.model_dump_json()
    digest = input_digest(spec_text)
    return _attempt(suite, spec_text, live=live, settings=settings).model_copy(update={"input_digest": digest})


def _stderr_tail(stderr: bytes) -> str | None:
    """The last non-empty line a failed child wrote to stderr, clipped: for a traceback, the exception it ended with.

    One line and not the stream, because the stream is the application's and may say anything, while one line is what
    names the failure a bare `CHILD_FAILED` hides. It stays local (`Attempt.stderr_tail`)."""
    lines = [line.strip() for line in stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return clipped(lines[-1]) if lines else None


def _attempt(suite: Suite, spec_text: str, *, live: bool, settings: HajerSettings) -> Attempt:
    try:
        declared = declared_adapters(suite.root)[1] if (suite.root / REPLAY_CONFIG).is_file() else {}
    except (OSError, ValueError):
        return Attempt(reason="REPLAY_CONFIGURATION_UNREADABLE")
    with tempfile.TemporaryDirectory(prefix="hajer-suite-") as directory:
        root = Path(directory)
        spec, receipt, egress = root / "input.json", root / "receipt.json", root / "egress.jsonl"
        write_private(spec, spec_text)
        try:
            result = subprocess.run(  # noqa: S603 - fixed interpreter/boot; customer entry is explicitly declared
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    str(Path(__file__).with_name("_boot.py")),
                    str(spec),
                    str(receipt),
                    str(egress),
                    str(suite.root),
                    "live" if live else "replay",
                ],
                cwd=suite.root,
                env=ci_execution_environment(declared, live=live, settings=settings),
                capture_output=True,
                check=False,
                timeout=settings.ci_case_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return Attempt(reason="TIMEOUT")
        if result.returncode or not receipt.is_file():
            return Attempt(reason="CHILD_FAILED", stderr_tail=_stderr_tail(result.stderr))
        return Attempt.model_validate_json(receipt.read_bytes())


#: A judge check is judged live on Hajer's JUDGE route and a metamorphic relation needs a second live run, so
#: a replay reports them apart with these reasons: never a pass, never a failure of the case's other checks.
JUDGE_NEEDS_LIVE = "JUDGE_NEEDS_LIVE"
METAMORPHIC_NEEDS_LIVE = "METAMORPHIC_NEEDS_LIVE"


def live_only(check: dict[str, JsonValue]) -> str | None:
    if isinstance(check.get("metamorphic"), dict):
        return METAMORPHIC_NEEDS_LIVE
    return JUDGE_NEEDS_LIVE if isinstance(check.get("judge"), dict) else None


def _check_id(check: dict[str, JsonValue]) -> JsonValue:
    identity = check["check"]
    return identity.get("id") if isinstance(identity, dict) else None


def _edits(checks: list[dict[str, JsonValue]]) -> dict[str, dict[str, JsonValue]]:
    found: dict[str, dict[str, JsonValue]] = {}
    for check in checks:
        relation = check.get("metamorphic")
        edit = relation.get("edit") if isinstance(relation, dict) else None
        if isinstance(edit, dict):
            found[json.dumps(edit, sort_keys=True)] = edit
    return found


def _edited(case: Case, edit: dict[str, JsonValue]) -> tuple[Case, JsonValue] | None:
    if case.execution is None:
        return None
    changed = edited_input(case.execution.input, edit)
    if changed is None:
        return None
    execution = Execution(adapter=case.execution.adapter, input=changed, boundaries=case.execution.boundaries)
    return Case(case.case_id, case.checks, execution, case.situations, case.origin), changed


def run(
    suite: Suite, case: Case, *, live: bool, settings: HajerSettings, judge: Judge | None = None
) -> dict[str, JsonValue]:
    """Execute a case once in replay, or in live mode until no check's verdict is still due another attempt.

    One execution reads every check, so the case repeats while any of its checks is `not_yet` under the cap, and
    each check keeps every attempt it was read on: an extra attempt a sibling needed is evidence, never dropped.
    A replay lists its live-only checks (judge, metamorphic) apart under `liveOnly`, never as a pass.
    """
    started = time.monotonic()
    if not live and case.origin == DERIVED_MUTATION:
        # A situation-derived input has no recorded answer to replay; a live run runs it. Counted apart, never
        # an UNABLE_TO_VERIFY check.
        return {"caseId": case.case_id, "checks": [], "skipped": NEEDS_LIVE_RUN, "durationMs": 0}
    waiting: list[JsonValue] = []
    if not live:
        waiting = [{"checkId": _check_id(item), "skipped": why} for item in case.checks if (why := live_only(item))]
        kept = [item for item in case.checks if not live_only(item)]
        case = Case(case.case_id, kept, case.execution, case.situations, case.origin)
        if not kept:
            return {
                "caseId": case.case_id,
                "checks": [],
                "skipped": NEEDS_LIVE_RUN,
                "liveOnly": waiting,
                "durationMs": 0,
            }
    histories: list[list[Verdict]] = [[] for _ in case.checks]
    unfunded: set[int] = set()
    # The case's input is the document a published check's applicability reads as `request`, as on the backend.
    request = None if case.execution is None else case.execution.input
    edits = _edits(case.checks) if live else {}
    reasons: list[str | None] = []
    executions: list[JsonValue] = []
    while not reasons or (
        live and any(next_repeat(history) for index, history in enumerate(histories) if index not in unfunded)
    ):
        attempt = execute(suite, case, live=live, settings=settings)
        edited: dict[str, tuple[Attempt, JsonValue]] = {}
        for key, edit in edits.items():
            found = _edited(case, edit)
            edited[key] = (
                (Attempt(reason="METAMORPHIC_EDIT_NOT_APPLICABLE"), None)
                if found is None
                else (execute(suite, found[0], live=True, settings=settings), found[1])
            )
        for index, (history, check) in enumerate(zip(histories, case.checks, strict=True)):
            if index in unfunded:
                continue
            answer = _read(check, (attempt, len(reasons)), request, edited, judge)
            if answer == SKIPPED:
                # A soft stop: the team funds no judge; listed apart, never a failure.
                unfunded.add(index)
                continue
            history.append(answer)
        executions.append(
            {
                **_evidence(attempt),
                "requestFingerprint": attempt.request_fingerprint,
                "edited": [_evidence(other) for other, _ in edited.values()],
            }
        )
        reasons.append(attempt.reason or next((other.reason for other, _ in edited.values() if other.reason), None))
    checks: list[JsonValue] = []
    waiting += [{"checkId": _check_id(case.checks[index]), "skipped": JUDGE_UNFUNDED} for index in sorted(unfunded)]
    for index, (outcomes, check) in enumerate(zip(histories, case.checks, strict=True)):
        if index in unfunded:
            continue
        # Live: the settled verdict (8A's ci_verdict), never the latest attempt. Replay: its one deterministic read.
        # Every attempt is still listed.
        label, verdict = read(outcomes, replayed=not live)
        checks.append(
            {
                "checkId": _check_id(check),
                "verdict": verdict,
                "label": label,
                "attempts": [str(outcome) for outcome in outcomes],
                "reason": _clipped(_reason(outcomes, reasons, label=label, live=live)),
            }
        )
    result: dict[str, JsonValue] = {
        "caseId": case.case_id,
        "checks": checks,
        "durationMs": int((time.monotonic() - started) * 1000),
        "executionEvidence": executions,
    }
    if waiting:
        result["liveOnly"] = waiting
    if case.situations:
        # Each KEYED choice the case's frame intends, witnessed on the case's input as the platform does.
        result["situations"] = [
            {"choiceId": choice, "holds": witness(predicate, request)} for choice, predicate in case.situations
        ]
    return result


def _evidence(attempt: Attempt) -> dict[str, JsonValue]:
    """One attempt as `executionEvidence` records it. A failed child's stderr line is there only when there is one, so an
    attempt that ran reads exactly as it always has."""
    evidence: dict[str, JsonValue] = {
        "mode": attempt.mode,
        "providerSuccesses": attempt.provider_successes,
        "reason": _clipped(attempt.reason),
        "inputDigest": attempt.input_digest,
    }
    if attempt.stderr_tail is not None:
        evidence["stderrTail"] = attempt.stderr_tail
    return evidence


def _base(
    check: dict[str, JsonValue], attempt: tuple[Attempt, int], request: JsonValue, judge: Judge | None
) -> JudgeAnswer:
    """The base check's verdict on one attempt: a judge check judged live (its attempt number names the judging),
    any other check evaluated."""
    found, number = attempt
    if isinstance(check.get("judge"), dict):
        if found.reason or judge is None:
            return "UNABLE_TO_VERIFY"
        return judge(check, found.output, request, number)
    return _verdict(check, found, request)


def _read(
    check: dict[str, JsonValue],
    attempt: tuple[Attempt, int],
    request: JsonValue,
    edited: dict[str, tuple[Attempt, JsonValue]],
    judge: Judge | None,
) -> JudgeAnswer:
    """One attempt's reading. A keep relation compares one field of two outputs and judges nothing; a DIR relation
    reads only the edited reply; an INV relation reads both (review I7: no judging whose verdict goes unused)."""
    relation = check.get("metamorphic")
    if not isinstance(relation, dict):
        return _base(check, attempt, request, judge)
    found, number = attempt
    other, changed = edited.get(json.dumps(relation.get("edit"), sort_keys=True), (Attempt(reason="NO_EDIT"), None))
    if isinstance(relation.get("keep"), list):
        if found.reason or other.reason:
            return "UNABLE_TO_VERIFY"
        return relation_verdict(relation, ("PASS", found.output), ("PASS", other.output))
    after = _base(check, (other, number), changed, judge)
    if after == SKIPPED or relation.get("relation") == "DIR":
        return after
    before = _base(check, attempt, request, judge)
    if before == SKIPPED:
        return before
    return relation_verdict(relation, (before, found.output), (after, other.output))


def _verdict(check: dict[str, JsonValue], attempt: Attempt, request: JsonValue) -> Verdict:
    """A check on the model's tool calls reads the recorded reply (as the platform's verifier does); any other check
    reads the application's return value."""
    if attempt.reason:
        return "UNABLE_TO_VERIFY"
    if not reads_tool_calls(check):
        return evaluate(check, attempt.output, request=request)
    # A call the recorded replies do not carry (none captured, a text answer, a duplicate) is UNABLE_TO_VERIFY.
    return evaluate(check, attempt.reply, request=request)


def _clipped(reason: str | None) -> str | None:
    return None if reason is None else clipped(reason)


def _reason(outcomes: list[Verdict], reasons: list[str | None], *, label: str, live: bool) -> str | None:
    """Execution failure anywhere in the attempts takes precedence over a judge's descriptive unknown.

    Later agreement can settle the check's verdict, but cannot erase a failed application/edited-child attempt.
    """
    execution_failure = next((reason for reason in reasons if reason), None)
    if execution_failure is not None:
        return f"APP_EXECUTION_UNAVAILABLE: {execution_failure}"
    if "UNABLE_TO_VERIFY" in outcomes:
        return "CHECK_UNSUPPORTED_OR_MISSING_EVIDENCE"
    if not live:
        return "REPLAY_HAS_NO_VARIANCE"
    return "FLAKY" if label == "flaky" else None

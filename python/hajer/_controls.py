"""Run a published check's retained controls in CI, without importing the customer application.

This is evidence about the checks, never an application test result. Missing controls,
unsupported predicates and empty suites cannot produce a successful control report. Model
calibration is not replayed offline: it stays NOT_REEXECUTED and cannot block code checks.
Each suite's report, and the line printed for it, names exactly which code checks were verified
against their retained controls, which were not, and which model-graded checks still need a live
evaluation; a count alone let "verified" read as covering the model-graded checks too.

A check's control identity is its draft digest (or, for a strong check, its id). One predicate can be prepared for two
obligations, so two members may carry the same identity with different provenance (obligation, producer, timestamps);
only a difference in what is evaluated (`draft`, `judge`, `metamorphic`, `liveOnly`, the evaluator) is a conflict.
Controls are read from each requirement's `controls` and from the suite's `checkControls` (a seed's, a carried check's
or a strong check's retained controls), in the same positive / negative gate shape.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from hajer.pytest_plugin._evaluate import evaluate

_OBJECT = TypeAdapter(dict[str, JsonValue])
_EVALUATED = ("draft", "judge", "metamorphic", "liveOnly")


def _objects(value: JsonValue) -> list[dict[str, JsonValue]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _evaluated(check: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """What a check's verdict depends on; provenance (obligation, producer, timestamps) is not part of it."""
    metadata = check.get("check")
    evaluator = metadata.get("evaluator") if isinstance(metadata, dict) else None
    return {"evaluator": evaluator, **{key: check.get(key) for key in _EVALUATED}}


def _published(document: dict[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    checks: dict[str, dict[str, JsonValue]] = {}
    members = document.get("members")
    if not isinstance(members, list):
        raise ValueError("Published members are missing")
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("Published member is unreadable")
        check = member.get("check")
        if not isinstance(check, dict):
            raise ValueError("Published check has no control identity")
        digest = check.get("checkDraftDigest")
        metadata = check.get("check")
        if digest is None and isinstance(metadata, dict):
            digest = metadata.get("id")
        if not isinstance(digest, str) or not digest:
            raise ValueError("Published check has no control identity")
        if digest in checks and _evaluated(checks[digest]) != _evaluated(check):
            raise ValueError("Conflicting published check definitions")
        checks[digest] = check
    return checks


def _row(identity: str, check: dict[str, JsonValue], controls: dict[str, JsonValue]) -> dict[str, JsonValue]:
    metadata = check.get("check")
    if isinstance(metadata, dict) and metadata.get("evaluator") == "SCOPED_SEMANTIC_JUDGE":
        if isinstance(check.get("judge"), dict) and check.get("liveOnly") is True:
            return {
                "checkDigest": identity,
                "status": "NOT_REEXECUTED",
                "reason": "MODEL_CALIBRATION_REQUIRES_LIVE_JUDGE",
                "advisory": True,
            }
        return {
            "checkDigest": identity,
            "status": "UNVERIFIED",
            "reason": "ADVISORY_CHECK_UNREADABLE",
            "advisory": True,
        }
    return {"checkDigest": identity, **_run(check, controls)}


def _output(document: dict[str, JsonValue]) -> JsonValue:
    """The application output a retained control document holds: its `output`, or — a control kept as the observation a
    check reading `evidence.application_result` evaluates (a declared-schema seed) — that result. `evaluate` projects
    the output to both places, so either form is the same reply."""
    if "output" in document:
        return document["output"]
    evidence = document.get("evidence")
    return evidence.get("application_result") if isinstance(evidence, dict) else None


def _run(check: dict[str, JsonValue], controls: dict[str, JsonValue]) -> dict[str, JsonValue]:
    counts = {"positive": 0, "negative": 0}
    mismatches = 0
    for polarity, expected in (("positive", "PASS"), ("negative", "FAIL")):
        for gate in _objects(controls.get(polarity)):
            for control in _objects(gate.get("controls")):
                raw = control.get("documentJson")
                if not isinstance(raw, str):
                    mismatches += 1
                    continue
                document = _OBJECT.validate_json(raw)
                observed = evaluate(check, _output(document), request=document.get("request"))
                counts[polarity] += 1
                mismatches += observed != expected
    complete = all(counts.values()) and controls.get("discriminates") is True
    return {
        "status": "VERIFIED" if complete and not mismatches else "UNVERIFIED",
        "positiveControls": counts["positive"],
        "negativeControls": counts["negative"],
        "mismatches": mismatches,
    }


def verify_controls(path: Path) -> dict[str, JsonValue]:
    raw = path.read_bytes()
    document = _OBJECT.validate_json(raw)
    checks = _published(document)
    controls: dict[str, dict[str, JsonValue]] = {}
    held = [
        (
            declaration.get("digest") if isinstance(declaration := item.get("check"), dict) else None,
            item.get("controls"),
        )
        for item in _objects(document.get("requirements"))
    ] + [(item.get("digest"), item.get("controls")) for item in _objects(document.get("checkControls"))]
    for digest, examples in held:
        if isinstance(digest, str) and isinstance(examples, dict):
            if digest in controls and controls[digest] != examples:
                raise ValueError("Conflicting retained controls")
            controls[digest] = examples
    rows: list[JsonValue] = [_row(digest, check, controls.get(digest, {})) for digest, check in checks.items()]
    verified = _named(rows, "VERIFIED", advisory=False)
    advisory = _named(rows, "NOT_REEXECUTED", advisory=True)
    status = "UNVERIFIED"
    if rows and len(verified) + len(advisory) == len(rows):
        status = "VERIFIED" if not advisory else "CODE_CONTROLS_VERIFIED" if verified else "NOT_REEXECUTED"
    return {
        "suiteId": document.get("suiteId"),
        "suiteDigest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "status": status,
        "checks": rows,
        "verifiedChecks": len(verified),
        "advisoryChecksNotReexecuted": len(advisory),
        "totalChecks": len(rows),
        "codeChecksVerified": verified,
        "codeChecksUnverified": _named(rows, "UNVERIFIED", advisory=False),
        "modelChecksNeedingLiveEvaluation": advisory,
        "modelChecksUnreadable": _named(rows, "UNVERIFIED", advisory=True),
        "applicationExecuted": False,
    }


def _named(rows: list[JsonValue], status: str, *, advisory: bool) -> list[JsonValue]:
    """The identities of the code (or, with `advisory`, model-graded) checks in `status`."""
    return [
        row.get("checkDigest")
        for row in rows
        if isinstance(row, dict) and row.get("status") == status and (row.get("advisory") is True) is advisory
    ]


def _listed(row: JsonValue, key: str) -> str:
    names = row.get(key) if isinstance(row, dict) else None
    named = [str(item) for item in names] if isinstance(names, list) else []
    return f"{len(named)} ({', '.join(named)})" if named else "0"


def _suite_line(row: JsonValue) -> str:
    """One suite's control result: which code checks were verified, which were not, which need a live model."""
    if not isinstance(row, dict) or "codeChecksVerified" not in row:
        named = row.get("file") if isinstance(row, dict) else None
        return f"- {named or 'a suite file'}: unverified (CONTROL_DOCUMENT_UNREADABLE).\n"
    unreadable = f"; model-graded checks unreadable: {_listed(row, 'modelChecksUnreadable')}"
    return (
        f"- {row.get('suiteId')}: {row.get('status')}. Code checks verified against their retained controls: "
        f"{_listed(row, 'codeChecksVerified')}; code checks not verified: {_listed(row, 'codeChecksUnverified')}; "
        f"model-graded checks that still need a live evaluation: {_listed(row, 'modelChecksNeedingLiveEvaluation')}"
        f"{unreadable if row.get('modelChecksUnreadable') else ''}.\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify retained good/bad controls; does not test application behavior"
    )
    parser.add_argument("--suites", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    options = parser.parse_args(argv)
    paths = sorted(options.suites.glob("*.json"))
    rows: list[JsonValue] = []
    for path in paths:
        try:
            rows.append(verify_controls(path))
        except (OSError, ValueError):
            # File contents can contain customer data; report only the category.
            rows.append({"status": "UNVERIFIED", "reason": "CONTROL_DOCUMENT_UNREADABLE", "file": path.name})
    ready = bool(rows) and all(
        isinstance(row, dict) and row.get("status") in {"VERIFIED", "CODE_CONTROLS_VERIFIED", "NOT_REEXECUTED"}
        for row in rows
    )
    report = {"schema": "hajer-control-results-v1", "applicationExecuted": False, "suites": rows}
    options.results.parent.mkdir(parents=True, exist_ok=True)
    options.results.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for row in rows:
        sys.stdout.write(_suite_line(row))
    sys.stdout.write(
        f"Hajer code controls: {'no failures found' if ready else 'unverified'}. "
        "Model-graded checks are not re-executed offline and are not verified here. Application behavior not tested.\n"
    )
    # Success permits execution to continue, not a claim that a model check passed.
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""In customer CI, judge checks are judged live and never pass in replay; metamorphic relations run live
with the platform's input edits; the shared vectors pin both sides' edits."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import JsonValue

from hajer._settings import HajerSettings
from hajer.pytest_plugin import _run
from hajer.pytest_plugin._judge import JudgeAnswer, remote_judge
from hajer.pytest_plugin._metamorphic import edited_input
from hajer.pytest_plugin._models import Attempt, Case, Execution, Suite
from tests.repo import ACTION, CONTRACT

VECTORS = CONTRACT / "check-evaluation-vectors.json"
EDITS = cast(list[dict[str, JsonValue]], json.loads(VECTORS.read_text(encoding="utf-8"))["metamorphicEdits"])
SCHEMA: JsonValue = {
    "type": "object",
    "properties": {"priority": {"enum": ["P1", "P2", "P3"]}},
    "required": ["priority"],
}
DRAFT: JsonValue = {
    "observationSchema": "source-response-json-v1",
    "expression": {"op": "schema_valid", "path": ["output"], "valueJson": json.dumps(SCHEMA)},
    "requiredPaths": [],
    "applicability": None,
}
RUBRIC: JsonValue = {"quote": "Set the priority from the customer impact, never from the tone.", "promptVersion": "v"}


def _check(ident: str, **extra: JsonValue) -> dict[str, JsonValue]:
    return {"check": {"id": ident}, **extra}


def _case(*checks: dict[str, JsonValue]) -> Case:
    execution = Execution(adapter={}, input={"body": "Checkout is down"}, boundaries=[])
    return Case("case-1", list(checks), execution)


def _suite() -> Suite:
    return Suite("suite", 1, Path("."), [])


def _executes(monkeypatch: pytest.MonkeyPatch, answer: dict[str, str]) -> list[JsonValue]:
    """The app under test: P3 when the ticket is angry, else P1 (a tone-dependent priority, the bug INV catches)."""
    seen: list[JsonValue] = []

    def execute(suite: Suite, case: Case, *, live: bool, settings: HajerSettings) -> Attempt:
        del suite, live, settings
        assert case.execution is not None
        seen.append(case.execution.input)
        body = str(cast(dict[str, JsonValue], case.execution.input)["body"])
        return Attempt(output={"priority": answer["angry"] if "angry" in body else "P1"})

    monkeypatch.setattr(_run, "execute", execute)
    return seen


@pytest.mark.parametrize("row", EDITS, ids=[str(row["name"]) for row in EDITS])
def test_the_plugin_edits_every_metamorphic_vector_as_the_backend_does(row: dict[str, JsonValue]) -> None:
    assert edited_input(row["input"], cast(dict[str, JsonValue], row["edit"])) == row["edited"]


def test_a_replay_lists_judge_and_metamorphic_checks_apart_and_never_passes_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _executes(monkeypatch, {"angry": "P1"})
    relation: JsonValue = {
        "relation": "INV",
        "edit": {"kind": "TONE", "field": ["body"]},
        "keep": ["output", "priority"],
    }
    case = _case(
        _check("hajer-predicate:schema:a", draft=DRAFT),
        _check("hajer-judge:judge:b", judge=RUBRIC),
        _check("hajer-judge:inv:c", judge=RUBRIC, metamorphic=relation),
    )
    result = _run.run(_suite(), case, live=False, settings=HajerSettings())
    assert [check["checkId"] for check in cast(list[dict[str, JsonValue]], result["checks"])] == [
        "hajer-predicate:schema:a"
    ]
    assert result["liveOnly"] == [
        {"checkId": "hajer-judge:judge:b", "skipped": "JUDGE_NEEDS_LIVE"},
        {"checkId": "hajer-judge:inv:c", "skipped": "METAMORPHIC_NEEDS_LIVE"},
    ]


def test_a_live_judge_check_is_judged_on_every_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _executes(monkeypatch, {"angry": "P1"})
    judged: list[tuple[JsonValue, int]] = []

    def judge(check: dict[str, JsonValue], output: JsonValue, request: JsonValue, attempt: int) -> JudgeAnswer:
        del check, request
        judged.append((output, attempt))
        return "PASS"

    result = _run.run(_suite(), _case(_check("j", judge=RUBRIC)), live=True, settings=HajerSettings(), judge=judge)
    (check,) = cast(list[dict[str, JsonValue]], result["checks"])
    # Repeated judgings are the judge's noise reading: three agreeing attempts settle a qualified PASS.
    assert (check["verdict"], check["label"], len(judged)) == ("PASS", "qualified", 3)
    # Each attempt is its own judging: the attempt number travels with it.
    assert [attempt for _, attempt in judged] == [0, 1, 2]


def test_a_live_inv_relation_fails_an_app_whose_priority_follows_the_tone(monkeypatch: pytest.MonkeyPatch) -> None:
    relation: JsonValue = {
        "relation": "INV",
        "edit": {"kind": "TONE", "field": ["body"]},
        "keep": ["output", "priority"],
    }
    seen = _executes(monkeypatch, {"angry": "P3"})
    result = _run.run(
        _suite(), _case(_check("m", draft=DRAFT, metamorphic=relation)), live=True, settings=HajerSettings()
    )
    (check,) = cast(list[dict[str, JsonValue]], result["checks"])
    assert check["verdict"] == "FAIL"
    assert {"body": "I am extremely angry and fed up, fix this now!!! Checkout is down"} in seen
    _executes(monkeypatch, {"angry": "P1"})
    result = _run.run(
        _suite(), _case(_check("m", draft=DRAFT, metamorphic=relation)), live=True, settings=HajerSettings()
    )
    assert cast(list[dict[str, JsonValue]], result["checks"])[0]["verdict"] == "PASS"


def test_the_remote_judge_reads_the_route_and_refuses_without_a_key() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"verdict": "VIOLATES", "promptVersion": "suite-judge-v1"})

    settings = HajerSettings(api_key="hj_test_key", team_id="team-1")
    judge = remote_judge(settings, "project-1", "run1", suite_id="suite-7", transport=httpx.MockTransport(answer))
    assert judge(_check("j", judge=RUBRIC), {"priority": "P3"}, {"body": "x"}, 2) == "FAIL"
    assert sent[0].url.path == "/api/teams/team-1/projects/project-1/suite-runs/judgings"
    body = json.loads(sent[0].content)
    # The route looks the check up in this suite only.
    assert (body["checkId"], body["suiteId"], body["runId"], body["attempt"]) == ("j", "suite-7", "run1", 2)
    assert "rubric" not in body
    assert (
        remote_judge(HajerSettings(), "project-1", "run1")(_check("j", judge=RUBRIC), {}, {}, 0) == "UNABLE_TO_VERIFY"
    )


def test_an_unfunded_judge_is_skipped_never_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A soft stop: 409 JUDGE_UNFUNDED lists the check apart, it never fails the case."""
    _executes(monkeypatch, {"angry": "P1"})

    def refuse(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(409, json={"error": "JUDGE_UNFUNDED: no JUDGE spend is consented", "code": "CONFLICT"})

    settings = HajerSettings(api_key="hj_test_key", team_id="team-1")
    judge = remote_judge(settings, "project-1", "run1", transport=httpx.MockTransport(refuse))
    case = _case(_check("s", draft=DRAFT), _check("j", judge=RUBRIC))
    result = _run.run(_suite(), case, live=True, settings=HajerSettings(), judge=judge)
    assert [check["checkId"] for check in cast(list[dict[str, JsonValue]], result["checks"])] == ["s"]
    assert result["liveOnly"] == [{"checkId": "j", "skipped": "JUDGE_UNFUNDED"}]


def test_a_keep_relation_judges_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keep relation compares one field of two outputs; no judging is spent on it."""
    _executes(monkeypatch, {"angry": "P1"})
    calls: list[int] = []

    def judge(check: dict[str, JsonValue], output: JsonValue, request: JsonValue, attempt: int) -> JudgeAnswer:
        del check, output, request
        calls.append(attempt)
        return "PASS"

    relation: JsonValue = {
        "relation": "INV",
        "edit": {"kind": "CASING", "field": ["body"]},
        "keep": ["output", "priority"],
    }
    result = _run.run(
        _suite(), _case(_check("m", metamorphic=relation)), live=True, settings=HajerSettings(), judge=judge
    )
    assert cast(list[dict[str, JsonValue]], result["checks"])[0]["verdict"] == "PASS"
    assert calls == []


def test_a_shadow_check_is_reported_and_never_fails_the_job(tmp_path: Path) -> None:
    """A generated check ships in SHADOW; its FAIL is in the receipt, the job still passes, and a judge
    check in replay is listed apart (JUDGE_NEEDS_LIVE)."""
    fixture = tmp_path / "support"
    shutil.copytree(ACTION / "fixtures" / "support", fixture)
    suite_path = fixture / ".hajer/suites/support.json"
    suite = json.loads(suite_path.read_text())
    wrong: JsonValue = {"op": "eq", "path": ["output", "priority"], "valueJson": '"LOW"'}
    suite["members"] += [
        {
            "case": {"id": "fixture-ticket"},
            "check": {"check": {"id": "shadow-wrong"}, "mode": "SHADOW", "draft": {"expression": wrong}},
        },
        {"case": {"id": "fixture-ticket"}, "check": {"check": {"id": "judge-j"}, "mode": "SHADOW", "judge": RUBRIC}},
    ]
    raw = json.dumps(suite).encode()
    suite_path.write_bytes(raw)
    sidecar = fixture / ".hajer/executions/support.json"
    binding = json.loads(sidecar.read_text())
    binding["suiteDigest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    sidecar.write_text(json.dumps(binding))
    result = subprocess.run(  # noqa: S603 - a copy of the committed fixture, replayed with egress blocked
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "hajer.pytest_plugin",
            "--hajer-suites",
            str(fixture / ".hajer/suites"),
            "--hajer-results",
            str(tmp_path / "results.json"),
            "-q",
        ],
        cwd=fixture,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    case = json.loads((tmp_path / "results.json").read_text())["suites"][0]["cases"][0]
    verdicts = {check["checkId"]: check["verdict"] for check in case["checks"]}
    assert verdicts == {"fixture-priority": "PASS", "shadow-wrong": "FAIL"}
    assert case["liveOnly"] == [{"checkId": "judge-j", "skipped": "JUDGE_NEEDS_LIVE"}]
    assert "SHADOW" in result.stdout

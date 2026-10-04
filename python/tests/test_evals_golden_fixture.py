"""The translation, against a results document a real engine wrote — on every matrix job, with no Node.

`tests/fixtures/evals/results.golden.json` is recorded from the golden run (`pytest --update-evals-golden`) and
is the example suite exactly as promptfoo reported it: four rows, four traces, the engine's own grader spans
beside the application's. `test_evals_payload.py` proves the rules on a hand-written document; this file proves
them on the real one, so a pin bump that changes the engine's output shape is caught before the engine job runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from hajer._json import JsonValue
from hajer._settings import HajerSettings
from hajer.evals._payload import EngineInfo, EvalRunPayload, Filters
from hajer.evals._results import read_results, translate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "evals" / "results.golden.json"


@pytest.fixture(scope="module")
def payload() -> EvalRunPayload:
    if not FIXTURE.exists():
        pytest.skip("no recorded engine document yet: run `pytest --update-evals-golden -m engine` once")
    document = read_results(FIXTURE)
    assert document is not None
    return translate(
        document,
        run_id="evalrun_fixture",
        created_at="2026-10-04T00:00:00Z",
        engine=EngineInfo(name="promptfoo"),
        exit_code=0,
        git=None,
        config_path="promptfooconfig.yaml",
        hook_report=None,
        filters=Filters(),
        settings=HajerSettings(),
    )


def test_the_recorded_document_holds_no_path_under_a_home_directory() -> None:
    if not FIXTURE.exists():
        pytest.skip("no recorded engine document yet")
    text = FIXTURE.read_text(encoding="utf-8")
    assert "/Users/" not in text
    assert "/home/" not in text


def test_the_example_suite_translates_as_the_golden_run_saw_it(payload: EvalRunPayload) -> None:
    assert payload.status == "passed"
    assert payload.stats.total == 4
    assert payload.stats.passed == 4
    assert payload.engine.version is not None, "filled from the engine's own metadata when the caller gave none"
    rows = {row.test_case_id: row for row in payload.results}
    assert {"eval_refund_pending", "eval_refund_completed", "eval_greeting"} <= set(rows)
    assert all(row.correlation == "platform" for row in payload.results), "defaultTest names the workflow"
    assert all(row.workflow_id == "wf_support" for row in payload.results)
    assert rows["eval_refund_pending"].obligation_ids == ("obl_refund_status_disclosed",)
    assert rows["eval_greeting"].obligation_ids == ()


def test_the_traces_are_linked_and_the_spans_are_the_applications_and_the_engines(payload: EvalRunPayload) -> None:
    pending = next(row for row in payload.results if row.test_case_id == "eval_refund_pending")
    assert pending.trace_id is not None
    assert len(pending.trace_id) == 32
    names = [span.name for span in pending.spans]
    assert "workflow wf_support" in names
    assert "component cmp_refund_agent" in names
    assert "execute_tool get_refund_status" in names
    assert any(name.startswith("grader ") for name in names), "the engine's grader spans share the trace"
    tool = next(span for span in pending.spans if span.name == "execute_tool get_refund_status")
    assert tool.attributes["hajer.tool.id"] == "tool_refund_status"
    assert tool.attributes["gen_ai.tool.name"] == "get_refund_status"
    assert tool.attributes["hajer.eval.test_case.id"] == "eval_refund_pending"
    assert tool.attributes["deployment.environment"] == "eval"
    assert all(span.status in {"unset", "ok", "error", None} for span in pending.spans), "numeric codes became words"
    assert pending.span_summary.tool_names == ("get_refund_status",)
    assert pending.span_summary.component_ids == ("cmp_refund_agent",)
    assert pending.span_summary.error_count == 0


def test_the_assertions_were_all_graded_on_the_trace(payload: EvalRunPayload) -> None:
    pending = next(row for row in payload.results if row.test_case_id == "eval_refund_pending")
    graded = {assertion.type: assertion for assertion in pending.assertions}
    assert set(graded) == {"contains", "trajectory:tool-used", "trace-span-count", "llm-rubric"}
    assert all(assertion.passed for assertion in graded.values())


def test_the_wire_round_trips(payload: EvalRunPayload) -> None:
    wire = payload.to_wire()
    again = EvalRunPayload.model_validate(json.loads(json.dumps(cast(JsonValue, wire))))
    assert again == payload
    assert isinstance(wire["results"], list)

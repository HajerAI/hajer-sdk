"""The eval upload schema and the translation into it, against a hand-written engine document.

`tests/fixtures/evals/results.sample.json` is the shape of promptfoo's `--output results.json`, written by hand
from its documented structure: three rows (platform-correlated and passing, with a trace; failing on an
`llm-rubric`; errored, with no trace), the trace for the first, and the engine's `metadata`. A golden document
from a real engine run sits beside it once the engine suite has written one; this file is the suite that runs
without Node, so every claim here is about the translation, not the engine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from hajer._json import JsonObject, JsonValue
from hajer._paths import VERSION
from hajer._settings import HajerSettings
from hajer._wrap import TRUNCATION_MARK
from hajer.evals import _results
from hajer.evals._payload import (
    PAYLOAD_SCHEMA_VERSION,
    EngineInfo,
    EvalRunPayload,
    Filters,
    payload_json_schema,
)
from hajer.evals._results import read_results, translate

FIXTURE = Path(__file__).parent / "fixtures" / "evals" / "results.sample.json"
ENGINE = EngineInfo(name="promptfoo", version="0.123.1", lockfile_digest="sha256:0123abcd")
CREATED_AT = "2026-10-04T10:00:05+00:00"
EMAIL = "dana.okafor@example.com"


def sample() -> JsonObject:
    """A fresh copy every time, so a test that mutates its document cannot leak into the next."""
    return cast(JsonObject, json.loads(FIXTURE.read_text(encoding="utf-8")))


def rows(document: JsonObject) -> list[JsonObject]:
    return cast(list[JsonObject], cast(JsonObject, document["results"])["results"])


def translated(
    document: JsonObject,
    *,
    exit_code: int | None = 100,
    settings: HajerSettings | None = None,
    engine: EngineInfo = ENGINE,
    hook_report: JsonObject | None = None,
    git: JsonObject | None = None,
    filters: Filters | None = None,
) -> EvalRunPayload:
    return translate(
        document,
        run_id="run-1",
        created_at=CREATED_AT,
        engine=engine,
        exit_code=exit_code,
        git=git,
        config_path="evals/promptfooconfig.yaml",
        hook_report=hook_report,
        filters=filters if filters is not None else Filters(),
        settings=settings if settings is not None else HajerSettings(),
    )


def test_the_run_is_described_from_the_document_and_the_caller() -> None:
    payload = translated(sample(), hook_report={"warnings": ["two tests share a test case id"]})

    assert payload.schema_version == PAYLOAD_SCHEMA_VERSION == 1
    assert payload.run_id == "run-1"
    assert payload.created_at == CREATED_AT
    assert payload.status == "failed"
    assert payload.engine_exit_code == 100
    assert payload.engine.name == "promptfoo"
    assert payload.engine.version == "0.123.1"
    assert payload.engine.lockfile_digest == "sha256:0123abcd"
    assert payload.engine.node_version == "v22.22.0"
    assert payload.engine.eval_id == "eval-sample-0001"
    assert payload.sdk.version == VERSION
    assert payload.git is None
    assert payload.config.path == "evals/promptfooconfig.yaml"
    assert payload.config.description == "Refund support suite"
    assert payload.config.provider_ids == ("python:provider.py",)
    assert payload.filters.workflow_id is None
    assert payload.filters.obligation_ids == ()
    assert payload.warnings == ("two tests share a test case id",)
    stats = payload.stats
    assert (stats.total, stats.passed, stats.failed, stats.errored) == (3, 1, 1, 1)
    assert stats.duration_ms == 2450
    assert stats.token_usage is not None
    assert (stats.token_usage.prompt, stats.token_usage.completion) == (120, 60)
    assert (stats.token_usage.total, stats.token_usage.cached) == (180, 0)
    assert stats.cost == pytest.approx(0.0012)


def test_a_platform_correlated_passing_row_with_its_trace() -> None:
    result = translated(sample()).results[0]

    assert result.test_case_id == "case-refund-window-01"  # from the trace, not the row
    assert (result.test_idx, result.prompt_idx) == (0, 0)
    assert result.correlation == "platform"
    assert result.workflow_id == "refund-flow"
    assert result.obligation_ids == ("refund-window",)
    assert result.component_ids == ("refund-agent",)
    assert result.source_trace_ids == ("trace-prod-9f1",)
    assert result.generated_by == "platform"
    assert result.provenance == {"kind": "recorded", "observationId": "obs-77"}
    assert result.description == "Refund window is stated"
    assert (result.provider.id, result.provider.label) == ("python:provider.py", "support-agent")
    assert result.outcome == "passed"
    assert result.score == 1.0
    assert result.error is None
    assert len(result.assertions) == 1
    assertion = result.assertions[0]
    assert (assertion.type, assertion.metric, assertion.weight) == ("contains", None, None)
    assert (assertion.passed, assertion.score) == (True, 1.0)
    assert assertion.reason == 'Output contains "business days"'
    assert result.latency_ms == 812
    assert result.token_usage is not None
    assert (result.token_usage.prompt, result.token_usage.completion) == (40, 20)
    assert (result.token_usage.total, result.token_usage.cached) == (60, 0)
    assert result.cost == pytest.approx(0.0012)
    assert (result.trace_id, result.evaluation_id) == ("trace-0001", "eval-sample-0001")
    assert result.output == "Your refund of $42.00 will arrive in 3-5 business days."

    # The document lists s4 before s3; the payload is in start-time order, times rounded to whole milliseconds.
    assert [span.span_id for span in result.spans] == ["s1", "s2", "s3", "s4"]
    root, agent, lookup, refund = result.spans
    assert (root.parent_span_id, agent.parent_span_id, lookup.parent_span_id) == (None, "s1", "s2")
    assert (root.name, root.start_time, root.end_time, root.status) == ("refund-flow", 1000, 1801, "ok")
    assert root.attributes == {
        "hajer.workflow.id": "refund-flow",
        "hajer.eval.run_id": "run-sample",
        "deployment.environment": "eval",
    }  # `http.method` is not in an allow-listed namespace
    assert agent.attributes == {
        "hajer.component.id": "refund-agent",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o-mini",
    }
    assert refund.status == "error"
    assert refund.attributes["tool.call.args"] == '{"orderId":"o-1"}'

    summary = result.span_summary
    assert (summary.count, summary.error_count) == (4, 1)
    assert summary.tool_names == ("lookup_order", "issue_refund")  # `gen_ai.tool.name` before `tool.name`, deduplicated
    assert summary.component_ids == ("refund-agent",)
    assert summary.workflow_ids == ("refund-flow",)


def test_a_failing_row_carries_the_assertion_that_failed() -> None:
    result = translated(sample()).results[1]

    assert result.test_case_id == "case-scope-01"  # no trace: the suite's own `metadata.testCaseId`
    assert result.outcome == "failed"
    assert result.correlation == "platform"
    assert result.obligation_ids == ("stay-in-scope",)
    assert result.error == "Assertion failed: Response does not stay in scope"
    assert result.score == 0.25
    assert len(result.assertions) == 1
    assertion = result.assertions[0]
    assert (assertion.type, assertion.metric, assertion.weight) == ("llm-rubric", "scope", 2.0)
    assert (assertion.passed, assertion.score) == (False, 0.25)
    assert assertion.reason == "The response declines but then adds a joke"
    assert result.trace_id is None
    assert result.spans == ()
    assert result.span_summary.count == 0
    assert result.token_usage is None
    assert result.cost is None


def test_an_errored_row_has_no_output_and_no_verdict() -> None:
    result = translated(sample()).results[2]

    assert result.test_case_id == "2-0"  # neither a trace nor a written id: the engine's own fallback
    assert result.correlation == "none"
    assert result.workflow_id is None
    assert result.obligation_ids == ()
    assert result.provenance is None
    assert result.outcome == "errored"
    assert result.error == "Provider timed out after 30000ms"
    assert result.assertions == ()
    assert result.output is None
    assert result.description is None
    assert result.provider.label is None
    assert result.latency_ms == 30000


def test_the_test_case_id_comes_from_the_trace_then_the_metadata_then_the_indices() -> None:
    document = sample()
    row = rows(document)[0]
    cast(JsonObject, row["metadata"])["testCaseId"] = "case-written"
    assert translated(document).results[0].test_case_id == "case-refund-window-01"

    cast(list[JsonObject], document["traces"])[0].pop("testCaseId")
    assert translated(document).results[0].test_case_id == "case-written"

    del row["traceId"]
    assert translated(document).results[0].test_case_id == "case-written"

    cast(JsonObject, row["metadata"])["testCaseId"] = 42  # not a string: not an id
    assert translated(document).results[0].test_case_id == "0-0"


@pytest.mark.parametrize(
    ("success", "failure_reason", "error", "outcome"),
    [
        (True, 0, None, "passed"),
        (True, 2, "ignored when the engine says success", "passed"),
        (False, 1, "Assertion failed", "failed"),
        (False, 1, None, "failed"),
        (False, 0, None, "failed"),
        (False, 2, None, "errored"),
        (False, 2, "boom", "errored"),
        (False, 0, "boom", "errored"),  # an error with no assertion verdict is the provider's failure
    ],
)
def test_the_outcome_rule(success: bool, failure_reason: int, error: str | None, outcome: str) -> None:
    document = sample()
    row = rows(document)[2]
    row["success"] = success
    row["failureReason"] = failure_reason
    if error is None:
        del row["error"]
    else:
        row["error"] = error
    assert translated(document).results[2].outcome == outcome


def test_the_output_is_redacted_when_client_redaction_is_on_and_kept_when_off() -> None:
    document = sample()
    rows(document)[0]["response"] = {"output": f"Contact {EMAIL} about the refund."}

    redacted = translated(document, settings=HajerSettings(redact_client=True)).results[0].output
    assert redacted is not None
    assert EMAIL not in redacted
    assert "[redacted:" in redacted
    assert redacted.endswith(" about the refund.")

    kept = translated(document, settings=HajerSettings(redact_client=False)).results[0].output
    assert kept == f"Contact {EMAIL} about the refund."


def test_the_output_is_clipped_in_the_middle_to_the_setting() -> None:
    document = sample()
    text = "".join(f"{index % 10}" for index in range(200))
    rows(document)[0]["response"] = {"output": text}

    output = translated(document, settings=HajerSettings(eval_output_max_chars=40)).results[0].output
    assert output is not None
    assert output.startswith(text[:20])
    assert output.endswith(text[-20:])
    assert "[hajer: 160 of 200 characters omitted here; sha256:" in output
    assert TRUNCATION_MARK.split("{")[0] in output

    whole = translated(document, settings=HajerSettings(eval_output_max_chars=200)).results[0].output
    assert whole == text


def test_a_structured_output_is_json_encoded_before_it_is_redacted_and_clipped() -> None:
    document = sample()
    rows(document)[0]["response"] = {"output": {"answer": "yes", "n": 1, "to": EMAIL}}

    output = translated(document).results[0].output
    assert output is not None
    assert output.startswith('{"answer":"yes","n":1,"to":"')
    assert EMAIL not in output

    rows(document)[0]["response"] = {"output": None}
    assert translated(document).results[0].output is None


def test_spans_are_capped_at_the_setting_but_the_summary_counts_them_all() -> None:
    result = translated(sample(), settings=HajerSettings(eval_spans_max=2)).results[0]

    assert [span.span_id for span in result.spans] == ["s1", "s2"]  # the two earliest
    assert result.span_summary.count == 4
    assert result.span_summary.error_count == 1
    assert result.span_summary.tool_names == ("lookup_order", "issue_refund")


def only(document: JsonObject, *indices: int) -> JsonObject:
    """The sample reduced to the given rows, with the traces kept."""
    kept = rows(document)
    cast(JsonObject, document["results"])["results"] = [kept[index] for index in indices]
    return document


@pytest.mark.parametrize(
    ("document", "exit_code", "status"),
    [
        (sample(), 100, "failed"),
        (only(sample(), 0), 0, "passed"),
        (only(sample(), 0, 2), 1, "errored"),
        (only(sample(), 1, 2), 100, "failed"),  # a failure outranks an error
        (only(sample(), 0), 1, "passed"),  # rows present: never aborted, whatever the exit code
        (only(sample()), 0, "passed"),  # an empty suite that finished
        (only(sample()), 1, "aborted"),
        (only(sample()), None, "aborted"),
        ({}, 1, "aborted"),  # the engine wrote nothing at all
    ],
)
def test_the_status_rule(document: JsonObject, exit_code: int | None, status: str) -> None:
    payload = translated(document, exit_code=exit_code)
    assert payload.status == status
    assert payload.engine_exit_code == exit_code


def test_the_wire_is_camel_case_json_and_round_trips_by_alias() -> None:
    payload = translated(
        sample(),
        git={"sha": "0123abcd", "branch": "feat/evals", "dirty": False},
        filters=Filters(workflow_id="refund-flow", obligation_ids=("refund-window",)),
        hook_report={"warnings": ["one"]},
    )
    wire = payload.to_wire()

    assert json.dumps(wire)  # JSON-safe: nothing but str, int, float, bool, None, dict, list
    assert wire["schemaVersion"] == 1
    assert set(wire) == {
        "schemaVersion",
        "runId",
        "createdAt",
        "status",
        "engineExitCode",
        "engine",
        "sdk",
        "git",
        "config",
        "filters",
        "stats",
        "warnings",
        "results",
    }
    assert wire["git"] == {"sha": "0123abcd", "branch": "feat/evals", "dirty": False}
    assert wire["filters"] == {"workflowId": "refund-flow", "obligationIds": ["refund-window"]}
    first = cast(list[JsonObject], wire["results"])[0]
    assert {"testCaseId", "testIdx", "promptIdx", "workflowId", "obligationIds", "spanSummary", "latencyMs"} <= set(
        first
    )
    assert cast(JsonObject, first["spanSummary"])["toolNames"] == ["lookup_order", "issue_refund"]
    # Every model key is camelCase; the three opaque documents (`git`, `provenance`, span `attributes`) are not models.
    for key in model_keys(wire):
        assert "_" not in key, key

    assert EvalRunPayload.model_validate(wire) == payload
    assert EvalRunPayload.model_validate(wire).to_wire() == wire


def model_keys(
    value: JsonValue, *, opaque: frozenset[str] = frozenset({"git", "provenance", "attributes"})
) -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            found.append(key)
            if key not in opaque:
                found.extend(model_keys(item, opaque=opaque))
        return found
    if isinstance(value, list):
        return [key for item in value for key in model_keys(item, opaque=opaque)]
    return []


def test_the_schema_forbids_what_the_translation_did_not_write() -> None:
    wire = translated(sample()).to_wire()
    wire["unknownKey"] = 1
    with pytest.raises(ValidationError):
        EvalRunPayload.model_validate(wire)


def test_read_results_answers_none_for_anything_but_a_json_object(tmp_path: Path) -> None:
    assert read_results(tmp_path / "missing.json") is None

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json", encoding="utf-8")
    assert read_results(invalid) is None

    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_results(not_an_object) is None

    assert read_results(tmp_path) is None  # a directory is unreadable, not an error

    valid = tmp_path / "results.json"
    valid.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    document = read_results(valid)
    assert document is not None
    assert document["evalId"] == "eval-sample-0001"
    assert translated(document).status == "failed"


def test_the_json_schema_requires_the_version_and_names_the_wire_keys() -> None:
    schema = payload_json_schema()
    required = cast(list[str], schema["required"])
    assert "schemaVersion" in required
    assert {"runId", "createdAt", "status", "engine", "sdk", "config", "filters", "stats", "results"} <= set(required)
    properties = cast(JsonObject, schema["properties"])
    assert cast(JsonObject, properties["schemaVersion"])["const"] == 1
    assert "schema_version" not in properties
    assert "EvalResult" in cast(JsonObject, schema["$defs"])


def with_unknown_keys(value: JsonValue, *, under: str | None = None) -> JsonValue:
    """The document with a key no engine version has at every object, except inside the opaque `provenance`."""
    if isinstance(value, dict):
        rebuilt: JsonObject = {key: with_unknown_keys(item, under=key) for key, item in value.items()}
        if under != "provenance":
            rebuilt["xUnknownKey"] = {"nested": [1, "two", None]}
        return rebuilt
    if isinstance(value, list):
        return [with_unknown_keys(item, under=under) for item in value]
    return value


def test_a_newer_engine_document_with_unknown_keys_everywhere_still_translates() -> None:
    baseline = translated(sample()).to_wire()
    newer = with_unknown_keys(sample())
    assert isinstance(newer, dict)
    assert "xUnknownKey" in newer  # the walk reached the top...
    assert "xUnknownKey" in rows(newer)[0]  # ...and the rows

    assert translated(newer).to_wire() == baseline


def test_a_document_that_is_not_the_engines_raises_rather_than_guessing() -> None:
    with pytest.raises(ValidationError):
        translated({"results": {"results": [{"success": True}]}})  # a row without its indices or provider


def test_the_engine_record_keeps_the_pin_and_fills_only_its_blanks() -> None:
    bare = translated(sample(), engine=EngineInfo(name="promptfoo")).engine
    assert (bare.version, bare.node_version, bare.eval_id) == ("0.123.1", "v22.22.0", "eval-sample-0001")
    assert bare.lockfile_digest is None

    pinned = EngineInfo(name="promptfoo", version="0.200.0", node_version="v24.0.0", eval_id="eval-x")
    kept = translated(sample(), engine=pinned).engine
    assert (kept.version, kept.node_version, kept.eval_id) == ("0.200.0", "v24.0.0", "eval-x")

    document = sample()
    del document["metadata"]
    del document["evalId"]
    unfilled = translated(document, engine=EngineInfo(name="promptfoo")).engine
    assert (unfilled.version, unfilled.node_version, unfilled.eval_id) == (None, None, None)


def test_provider_ids_fall_back_to_the_config_only_when_nothing_ran() -> None:
    document = only(sample())
    cast(JsonObject, document["config"])["providers"] = [
        "file://provider.py",
        {"id": "openai:gpt-4o-mini", "config": {"temperature": 0}},
        3,
        "file://provider.py",
    ]
    assert translated(document).config.provider_ids == ("file://provider.py", "openai:gpt-4o-mini")

    cast(JsonObject, document["config"])["providers"] = "openai:gpt-4o-mini"
    assert translated(document).config.provider_ids == ("openai:gpt-4o-mini",)

    del document["config"]
    payload = translated(document)
    assert payload.config.provider_ids == ()
    assert payload.config.description is None


def test_hook_warnings_travel_verbatim_and_only_the_strings() -> None:
    assert translated(sample(), hook_report=None).warnings == ()
    assert translated(sample(), hook_report={"classified": 3}).warnings == ()
    assert translated(sample(), hook_report={"warnings": ["a", 3, "b"]}).warnings == ("a", "b")
    assert translated(sample(), hook_report={"warnings": "not a list"}).warnings == ()


@pytest.mark.parametrize(
    ("raw", "word"),
    [(0, "unset"), (1, "ok"), (2, "error"), ("error", "error"), ("OK", "ok"), (None, None), (7, None)],
)
def test_a_span_status_is_read_as_the_engine_writes_it_and_carried_as_the_word(raw: object, word: str | None) -> None:
    """`results.json` carries OpenTelemetry's numeric code; the engine's own API carries the word. Both are read."""
    assert _results._span_status(cast("str | int | None", raw)) == word  # pyright: ignore[reportPrivateUsage]

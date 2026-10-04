"""Translate the engine's own output document into the upload schema, and nothing else.

promptfoo writes `--output results.json` in its own shape, which moves with its version; `_payload.py` is the
SDK's shape, which moves with `schemaVersion`. This module is the one place that knows both, so an engine bump
is a change here and nowhere downstream.

The input models are `extra="ignore"` with a default on every key the engine may omit: a newer engine adding a
field, or dropping an optional one, must not stop a run that has already finished from being uploaded. A document
that is not the engine's at all — a row without its indices, a span without its times — raises
`pydantic.ValidationError` from `translate`: `hajer eval` is a command, not a customer's request path, and a
stack trace naming the key is the right answer there. `read_results` is the lenient half: a missing or unreadable
file is `None`, because *the engine wrote nothing* is the normal end of an aborted run and the status rule covers it.

Three things the translation decides, because the engine does not say them:

* **A row's test case id.** Rows carry none. The trace the row links to (`traceId`) carries the id the engine
  minted for the test case, so that is first; a `metadata.testCaseId` the suite wrote is second; and
  `"{testIdx}-{promptIdx}"`, the engine's own fallback, is last.
* **A row's outcome.** `passed` when the engine says `success`; otherwise `errored` when the provider never
  produced an output to grade (`failureReason` 2, or an `error` with no assertion verdict), else `failed`.
* **What of the output and the trace travels.** The output is redacted with the same catalog `verify` uses
  (`HAJER_REDACT_CLIENT`) and then clipped to `HAJER_EVAL_OUTPUT_MAX_CHARS` — in that order, so a cut can never
  leave half of a value the scan would have caught. Span attributes are allow-listed to the namespaces the platform
  reads (`_ATTRIBUTE_PREFIXES`), and the span list is capped at `HAJER_EVAL_SPANS_MAX`, earliest first, with the
  summary counted over all of them so a capped trace still says how large it was.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from hajer._json import JsonObject, JsonValue
from hajer._paths import VERSION
from hajer._redact import ClientRedactionPolicy, build_policy, redact_document
from hajer._settings import HajerSettings
from hajer._wrap import TRUNCATION_MARK
from hajer.evals._payload import (
    PAYLOAD_SCHEMA_VERSION,
    AssertionResult,
    ConfigInfo,
    EngineInfo,
    EvalResult,
    EvalRunPayload,
    Filters,
    Outcome,
    ProviderInfo,
    RunStatus,
    SdkInfo,
    SpanRecord,
    SpanSummary,
    Stats,
    TokenUsage,
)

#: The span attribute namespaces that travel. Everything else a span carries — `http.*`, `db.*`, a framework's
#: own keys — stays in the customer's CI: the platform reads these four and nothing it does not read is sent.
_ATTRIBUTE_PREFIXES: Final[tuple[str, ...]] = ("hajer.", "gen_ai.", "deployment.", "tool.")
#: Where a span names the tool it called, in the order tried: the GenAI semantic convention first, then the SDK's own.
_TOOL_NAME_KEYS: Final[tuple[str, ...]] = ("gen_ai.tool.name", "tool.name")
_COMPONENT_ID_KEY: Final[str] = "hajer.component.id"
_WORKFLOW_ID_KEY: Final[str] = "hajer.workflow.id"
#: The reserved key under a test's `metadata` where the platform (or the suite author) writes the row's linkage.
_HAJER_METADATA_KEY: Final[str] = "hajer"
#: The engine's `failureReason` vocabulary: an assertion failed, or the provider errored. Not bounds, the engine's
#: own enum, spelled here because the engine spells it as bare integers.
_FAILURE_ASSERTION: Final[int] = 1
_FAILURE_ERROR: Final[int] = 2
#: The two exit codes that mean the engine finished and graded: all passed, or some failed. Any other code with no
#: rows is a run that never reached a verdict. The engine's vocabulary again, not a bound.
_COMPLETED_EXIT_CODES: Final[frozenset[int]] = frozenset({0, 100})


class _Engine(BaseModel):
    """Base for everything read out of the engine's document: lenient on keys, strict on the types of what is there."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore")


class _TokenUsage(_Engine):
    prompt: float | None = None
    completion: float | None = None
    total: float | None = None
    cached: float | None = None


class _Stats(_Engine):
    token_usage: _TokenUsage | None = None
    duration_ms: float | None = None


class _Provider(_Engine):
    id: str
    label: str | None = None


class _Response(_Engine):
    output: JsonValue = None


class _TestCase(_Engine):
    description: str | None = None


class _Assertion(_Engine):
    type: str | None = None
    metric: str | None = None
    weight: float | None = None


class _ComponentResult(_Engine):
    #: `pass` is a keyword, so the attribute carries a trailing underscore and the alias is written out.
    pass_: bool = Field(default=False, alias="pass")
    score: float = 0.0
    reason: str = ""
    assertion: _Assertion | None = None


class _GradingResult(_Engine):
    pass_: bool = Field(default=False, alias="pass")
    score: float = 0.0
    reason: str = ""
    component_results: list[_ComponentResult] = []


class _Row(_Engine):
    success: bool
    score: float = 0.0
    failure_reason: int = 0
    error: str | None = None
    latency_ms: float | None = None
    cost: float | None = None
    token_usage: _TokenUsage | None = None
    test_idx: int
    prompt_idx: int
    provider: _Provider
    response: _Response | None = None
    metadata: JsonObject = {}
    test_case: _TestCase | None = None
    grading_result: _GradingResult | None = None
    trace_id: str | None = None
    evaluation_id: str | None = None


class _Results(_Engine):
    stats: _Stats | None = None
    results: list[_Row] = []


class _Span(_Engine):
    span_id: str
    parent_span_id: str | None = None
    name: str = ""
    start_time: float
    end_time: float
    attributes: JsonObject = {}
    #: The engine writes OpenTelemetry's numeric code (0 unset, 1 ok, 2 error) in the results document and the
    #: word in its own API; both are read, and the payload always carries the word.
    status_code: str | int | None = None


class _Trace(_Engine):
    trace_id: str
    test_case_id: str | None = None
    spans: list[_Span] = []


class _Config(_Engine):
    description: str | None = None
    providers: JsonValue = None


class _Metadata(_Engine):
    promptfoo_version: str | None = None
    node_version: str | None = None


class _Document(_Engine):
    """The output file. Every section defaults to empty so a document the engine never finished still translates."""

    eval_id: str | None = None
    results: _Results = _Results()
    config: _Config | None = None
    metadata: _Metadata | None = None
    traces: list[_Trace] = []


def read_results(path: Path) -> JsonObject | None:
    """The engine's output document, or `None` when there is not one to read. Never raises.

    A missing file is what an aborted run leaves behind, so it is an answer, not an error; a file that is not JSON,
    or is JSON but not an object, is treated the same way because the caller's next question (*did the engine
    finish?*) has the same answer for all three, and the exit code says the rest.
    """
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _json_object(loaded)


def _json_object(value: object) -> JsonObject | None:
    """A parsed JSON value as the object it is, or `None`. The one `cast` here: `json.loads` yields nothing else."""
    return cast(JsonObject, value) if isinstance(value, dict) else None


def translate(
    document: JsonObject,
    *,
    run_id: str,
    created_at: str,
    engine: EngineInfo,
    exit_code: int | None,
    git: JsonObject | None,
    config_path: str | None,
    hook_report: JsonObject | None,
    filters: Filters,
    settings: HajerSettings,
) -> EvalRunPayload:
    """One engine document as the upload payload. Raises `pydantic.ValidationError` for a document that is not one."""
    parsed = _Document.model_validate(document)
    traces = {trace.trace_id: trace for trace in parsed.traces}
    policy = build_policy() if settings.redact_client else None
    results = tuple(_result(row, traces=traces, settings=settings, policy=policy) for row in parsed.results.results)
    return EvalRunPayload(
        schema_version=PAYLOAD_SCHEMA_VERSION,
        run_id=run_id,
        created_at=created_at,
        status=_status(results, exit_code),
        engine_exit_code=exit_code,
        engine=_engine(engine, parsed),
        sdk=SdkInfo(version=VERSION),
        git=git,
        config=ConfigInfo(
            path=config_path,
            description=parsed.config.description if parsed.config is not None else None,
            provider_ids=_provider_ids(parsed),
        ),
        filters=filters,
        stats=_stats(parsed, results),
        warnings=_warnings(hook_report),
        results=results,
    )


def _engine(engine: EngineInfo, parsed: _Document) -> EngineInfo:
    """The caller's engine record with the blanks the document can fill: what ran, on which Node, as which eval.

    The caller knows the pin (version, lockfile digest) before the run; the document knows what actually ran. The
    caller's word stands where it has one, so a pinned version is never overwritten by the engine's own report.
    """
    metadata = parsed.metadata
    return engine.model_copy(
        update={
            "version": engine.version if engine.version is not None or metadata is None else metadata.promptfoo_version,
            "node_version": (
                engine.node_version if engine.node_version is not None or metadata is None else metadata.node_version
            ),
            "eval_id": engine.eval_id if engine.eval_id is not None else parsed.eval_id,
        }
    )


def _status(results: Sequence[EvalResult], exit_code: int | None) -> RunStatus:
    if not results and exit_code not in _COMPLETED_EXIT_CODES:
        return "aborted"
    outcomes = {result.outcome for result in results}
    if "failed" in outcomes:
        return "failed"
    if "errored" in outcomes:
        return "errored"
    return "passed"


def _stats(parsed: _Document, results: Sequence[EvalResult]) -> Stats:
    """Totals counted from the translated rows, so `stats` and `results` can never disagree about an outcome.

    Duration and token usage are the engine's own totals (it timed the run; the SDK did not). Cost is summed from
    the rows because the engine reports it per row only; `None` when no row priced itself, never a zero it did not say.
    """
    engine_stats = parsed.results.stats
    costs = [row.cost for row in parsed.results.results if row.cost is not None]
    return Stats(
        total=len(results),
        passed=sum(1 for result in results if result.outcome == "passed"),
        failed=sum(1 for result in results if result.outcome == "failed"),
        errored=sum(1 for result in results if result.outcome == "errored"),
        duration_ms=_count(engine_stats.duration_ms) if engine_stats is not None else None,
        token_usage=_token_usage(engine_stats.token_usage) if engine_stats is not None else None,
        cost=sum(costs) if costs else None,
    )


def _provider_ids(parsed: _Document) -> tuple[str, ...]:
    """The providers that ran, by the ids the engine resolved; the configured ones only when nothing ran.

    The rows carry the resolved id (`python:provider.py`) and the config the written one (`file://provider.py`);
    listing both would show one provider twice under two names, so the config is read only for a run with no rows,
    where it is the only record of what the suite meant to drive.
    """
    from_rows = _unique(row.provider.id for row in parsed.results.results)
    if from_rows or parsed.config is None:
        return from_rows
    configured = parsed.config.providers
    if isinstance(configured, str):
        return (configured,)
    if not isinstance(configured, list):
        return ()
    named: list[str] = []
    for item in configured:
        found = item if isinstance(item, str) else _string(item, "id") if isinstance(item, dict) else None
        if found is not None:
            named.append(found)
    return _unique(named)


def _warnings(hook_report: JsonObject | None) -> tuple[str, ...]:
    """What the hook warned about, verbatim: the suite author's problems, carried to where the run is reviewed."""
    if hook_report is None:
        return ()
    return _strings(hook_report, "warnings")


def _result(
    row: _Row, *, traces: Mapping[str, _Trace], settings: HajerSettings, policy: ClientRedactionPolicy | None
) -> EvalResult:
    trace = traces.get(row.trace_id) if row.trace_id is not None else None
    spans = sorted(trace.spans, key=lambda span: span.start_time) if trace is not None else []
    linkage = _hajer(row.metadata)
    workflow_id = _string(linkage, "workflowId")
    return EvalResult(
        test_case_id=_test_case_id(row, trace),
        test_idx=row.test_idx,
        prompt_idx=row.prompt_idx,
        correlation="platform" if workflow_id is not None else "none",
        workflow_id=workflow_id,
        obligation_ids=_strings(linkage, "obligationIds"),
        component_ids=_strings(linkage, "componentIds"),
        source_trace_ids=_strings(linkage, "sourceTraceIds"),
        generated_by=_string(linkage, "generatedBy"),
        provenance=_object(linkage, "provenance"),
        description=row.test_case.description if row.test_case is not None else None,
        provider=ProviderInfo(id=row.provider.id, label=row.provider.label),
        outcome=_outcome(row),
        score=row.score,
        error=row.error,
        assertions=_assertions(row.grading_result),
        latency_ms=_count(row.latency_ms),
        token_usage=_token_usage(row.token_usage),
        cost=row.cost,
        trace_id=row.trace_id,
        evaluation_id=row.evaluation_id,
        output=_output(row.response, settings=settings, policy=policy),
        spans=_spans(spans[: settings.eval_spans_max]),
        span_summary=_summary(spans),
    )


def _test_case_id(row: _Row, trace: _Trace | None) -> str:
    if trace is not None and trace.test_case_id is not None:
        return trace.test_case_id
    written = _string(row.metadata, "testCaseId")
    if written is not None:
        return written
    return f"{row.test_idx}-{row.prompt_idx}"


def _outcome(row: _Row) -> Outcome:
    if row.success:
        return "passed"
    if row.failure_reason == _FAILURE_ERROR or (row.error is not None and row.failure_reason != _FAILURE_ASSERTION):
        return "errored"
    return "failed"


def _assertions(grading: _GradingResult | None) -> tuple[AssertionResult, ...]:
    """Each component's verdict; the overall verdict alone when the engine graded without naming components."""
    if grading is None:
        return ()
    if not grading.component_results:
        return (AssertionResult(passed=grading.pass_, score=grading.score, reason=grading.reason),)
    return tuple(
        AssertionResult(
            type=component.assertion.type if component.assertion is not None else None,
            metric=component.assertion.metric if component.assertion is not None else None,
            passed=component.pass_,
            score=component.score,
            reason=component.reason,
            weight=component.assertion.weight if component.assertion is not None else None,
        )
        for component in grading.component_results
    )


def _output(response: _Response | None, *, settings: HajerSettings, policy: ClientRedactionPolicy | None) -> str | None:
    """The row's output as text — redacted first, then clipped, so a cut cannot leave half of a value unscanned."""
    if response is None or response.output is None:
        return None
    text = response.output if isinstance(response.output, str) else _encoded(response.output)
    if policy is not None:
        redacted, _entries = redact_document(text, policy=policy)
        text = redacted if isinstance(redacted, str) else _encoded(redacted)
    return _clip(text, settings.eval_output_max_chars)


def _encoded(value: JsonValue) -> str:
    """A structured output as one JSON text, compact, so it is clipped and redacted like any other output."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _clip(text: str, keep: int) -> str:
    """Head and tail around `TRUNCATION_MARK`, or the text itself when it fits.

    The same mark `wrap` leaves in a clipped prompt, so one spelling means *the SDK cut this here* wherever it is
    read, and the digest lets a reader with the full output confirm it is the same one.
    """
    if len(text) <= keep:
        return text
    head = keep - keep // 2
    tail = keep // 2
    digest = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()
    mark = TRUNCATION_MARK.format(omitted=len(text) - keep, length=len(text), digest=digest)
    return text[:head] + mark + text[len(text) - tail :]


#: OpenTelemetry's `StatusCode` enum as the engine serialises it in `results.json`.
_SPAN_STATUS_WORDS: Final[dict[int, str]] = {0: "unset", 1: "ok", 2: "error"}


def _span_status(value: str | int | None) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return _SPAN_STATUS_WORDS.get(value)
    return value.lower() or None


def _spans(spans: Sequence[_Span]) -> tuple[SpanRecord, ...]:
    return tuple(
        SpanRecord(
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            name=span.name,
            start_time=round(span.start_time),
            end_time=round(span.end_time),
            status=_span_status(span.status_code),
            attributes={key: value for key, value in span.attributes.items() if key.startswith(_ATTRIBUTE_PREFIXES)},
        )
        for span in spans
    )


def _summary(spans: Sequence[_Span]) -> SpanSummary:
    tool_names: list[str] = []
    for span in spans:
        for key in _TOOL_NAME_KEYS:
            name = _string(span.attributes, key)
            if name is not None:
                tool_names.append(name)
                break
    return SpanSummary(
        count=len(spans),
        error_count=sum(1 for span in spans if _span_status(span.status_code) == "error"),
        tool_names=_unique(tool_names),
        component_ids=_unique(
            found for span in spans if (found := _string(span.attributes, _COMPONENT_ID_KEY)) is not None
        ),
        workflow_ids=_unique(
            found for span in spans if (found := _string(span.attributes, _WORKFLOW_ID_KEY)) is not None
        ),
    )


def _token_usage(usage: _TokenUsage | None) -> TokenUsage | None:
    if usage is None:
        return None
    return TokenUsage(
        prompt=_count(usage.prompt),
        completion=_count(usage.completion),
        total=_count(usage.total),
        cached=_count(usage.cached),
    )


def _count(value: float | None) -> int | None:
    """A count or a duration the engine wrote as a JSON number, as the integer it means."""
    return None if value is None else round(value)


def _hajer(metadata: JsonObject) -> JsonObject | None:
    """The row's reserved linkage block, when the suite wrote one and wrote it as an object."""
    return _object(metadata, _HAJER_METADATA_KEY)


def _string(source: JsonObject | None, key: str) -> str | None:
    value = source.get(key) if source is not None else None
    return value if isinstance(value, str) else None


def _strings(source: JsonObject | None, key: str) -> tuple[str, ...]:
    value = source.get(key) if source is not None else None
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _object(source: JsonObject | None, key: str) -> JsonObject | None:
    value = source.get(key) if source is not None else None
    return value if isinstance(value, dict) else None


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    """Distinct strings in first-seen order; `dict.fromkeys` is the ordered set the stdlib has."""
    return tuple(dict.fromkeys(items))

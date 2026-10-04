"""The eval run upload: one versioned schema, owned by the SDK, that the platform reads by its number.

`hajer eval --upload` posts this document to `EVAL_RUNS_PATH` and nothing else. It is the SDK's own schema rather
than a generated wire type because the engine's output moves with the engine's version (`_results.py` translates
it, in the customer's CI) while the platform must keep accepting what an older SDK produced after the engine moves:
`schemaVersion` is the contract, the translation behind it is private, and a change a reader of version 1 could
not ignore is version 2.

Every model here is `extra="forbid"` and frozen because this is the *output* side. The SDK's response models are
`extra="ignore"` so a newer server cannot raise inside a customer's request; here an unknown key is a bug in the
translation, and the round trip `EvalRunPayload.model_validate(payload.to_wire())` is what the tests assert.
Wire keys are camelCase, attributes snake_case, as everywhere in the SDK.
"""

from __future__ import annotations

from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from hajer._json import JsonObject

#: The one number the platform branches on. Bumped when a reader of the previous version could not ignore the change.
PAYLOAD_SCHEMA_VERSION: Final = 1

#: What one run ended as. `aborted` is the engine not finishing (no rows, an exit code that is not a verdict);
#: the other three are the worst row's outcome, in that order of badness.
RunStatus: TypeAlias = Literal["passed", "failed", "errored", "aborted"]
#: What one row ended as: the assertions held, an assertion failed, or the provider never produced an output.
Outcome: TypeAlias = Literal["passed", "failed", "errored"]
#: Whether the row names a platform workflow (`metadata.hajer.workflowId`), which is what lets the platform
#: place it next to production traces of the same workflow.
Correlation: TypeAlias = Literal["platform", "none"]


class _Payload(BaseModel):
    """Base for the whole document: camelCase on the wire, snake_case in code, and nothing undeclared."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid")


class EngineInfo(_Payload):
    """Which engine ran, pinned by the lockfile digest the install is keyed on, and the eval it minted."""

    name: Literal["promptfoo"]
    version: str | None = None
    lockfile_digest: str | None = None
    node_version: str | None = None
    eval_id: str | None = None


class SdkInfo(_Payload):
    """The SDK that translated the run: the only thing that can explain a schema-version mismatch."""

    version: str


class ConfigInfo(_Payload):
    """The suite as configured: where it lives, what it says about itself, and which providers it drove."""

    path: str | None = None
    description: str | None = None
    provider_ids: tuple[str, ...] = ()


class Filters(_Payload):
    """What `hajer eval --workflow` / `--obligation` narrowed the suite to; empty means the whole suite ran."""

    workflow_id: str | None = None
    obligation_ids: tuple[str, ...] = ()


class TokenUsage(_Payload):
    """Token counts as the engine reported them; `None` where it reported nothing, never a zero it did not say."""

    prompt: int | None = None
    completion: int | None = None
    total: int | None = None
    cached: int | None = None


class Stats(_Payload):
    """The run's totals, counted from the rows by the SDK's own outcome rule so they agree with `results`."""

    total: int
    passed: int
    failed: int
    errored: int
    duration_ms: int | None = None
    token_usage: TokenUsage | None = None
    cost: float | None = None


class ProviderInfo(_Payload):
    """The provider a row ran against, by the engine's id and the suite's label for it."""

    id: str
    label: str | None = None


class AssertionResult(_Payload):
    """One assertion's verdict, with the engine's own reason sentence; the suite's `metric` and `weight` travel."""

    type: str | None = None
    metric: str | None = None
    passed: bool
    score: float
    reason: str
    weight: float | None = None


class SpanRecord(_Payload):
    """One span of the row's trace, with only the attribute namespaces the platform reads (`_results.py`)."""

    span_id: str
    parent_span_id: str | None = None
    name: str
    start_time: int
    end_time: int
    status: str | None = None
    attributes: JsonObject = {}


class SpanSummary(_Payload):
    """The whole trace in a few numbers, counted before the span cap so a capped trace still says its size."""

    count: int
    error_count: int
    tool_names: tuple[str, ...] = ()
    component_ids: tuple[str, ...] = ()
    workflow_ids: tuple[str, ...] = ()


class EvalResult(_Payload):
    """One row: its identity, its platform linkage, its verdict, and what of its output and trace travels."""

    test_case_id: str
    test_idx: int
    prompt_idx: int
    correlation: Correlation
    workflow_id: str | None = None
    obligation_ids: tuple[str, ...] = ()
    component_ids: tuple[str, ...] = ()
    source_trace_ids: tuple[str, ...] = ()
    generated_by: str | None = None
    provenance: JsonObject | None = None
    description: str | None = None
    provider: ProviderInfo
    outcome: Outcome
    score: float
    error: str | None = None
    assertions: tuple[AssertionResult, ...] = ()
    latency_ms: int | None = None
    token_usage: TokenUsage | None = None
    cost: float | None = None
    trace_id: str | None = None
    evaluation_id: str | None = None
    output: str | None = None
    spans: tuple[SpanRecord, ...] = ()
    span_summary: SpanSummary


class EvalRunPayload(_Payload):
    """The document `hajer eval --upload` sends. `schema_version` has no default: a payload says its version."""

    schema_version: Literal[1]
    run_id: str
    created_at: str
    status: RunStatus
    engine_exit_code: int | None = None
    engine: EngineInfo
    sdk: SdkInfo
    git: JsonObject | None = None
    config: ConfigInfo
    filters: Filters
    stats: Stats
    warnings: tuple[str, ...] = ()
    results: tuple[EvalResult, ...]

    def to_wire(self) -> JsonObject:
        """The document as the request body carries it: every key present, camelCase, JSON-safe values only."""
        return self.model_dump(by_alias=True, mode="json")


def payload_json_schema() -> JsonObject:
    """The schema as JSON Schema, by wire key, so the platform's reader can be generated from it rather than typed."""
    return EvalRunPayload.model_json_schema(by_alias=True)

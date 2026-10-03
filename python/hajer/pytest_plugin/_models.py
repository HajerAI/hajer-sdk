"""CI receipts contain verdicts and identities, never recorded customer content."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

Verdict = Literal["PASS", "FAIL", "UNABLE_TO_VERIFY"]
Label = Literal["qualified", "not_yet", "flaky"]


class Execution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: dict[str, JsonValue]
    input: JsonValue
    boundaries: list[dict[str, JsonValue]]


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel)
    suite_id: str
    suite_version: int
    suite_digest: str
    cases: dict[str, Execution]


@dataclass
class Case:
    case_id: str
    checks: list[dict[str, JsonValue]]
    execution: Execution | None
    #: The KEYED situation choices the case's frame intends, witnessed on its request in CI.
    situations: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list[tuple[str, dict[str, JsonValue]]])
    #: The case's origin in the suite file; a DERIVED_MUTATION input has no recorded answer.
    origin: str | None = None


@dataclass
class Suite:
    suite_id: str
    version: int
    root: Path
    cases: list[Case]
    results: list[dict[str, JsonValue]] = field(default_factory=list)
    #: The model call site the suite's workflow is (`path:line`), which its adapter must be shown to reach.
    site: str | None = None
    #: Check ids the suite publishes in SHADOW mode; a non-PASS is reported and never fails the job.
    shadow: set[str] = field(default_factory=set[str])
    #: Bound from the explicit temporary local proof bundle: each case's adapter is checked with its own input.
    temporary: bool = False
    #: Approved traffic inputs held only in this process, checked with their own input.
    private_inputs: bool = False
    workflow_id: str | None = None


@dataclass
class State:
    suites: list[Suite] = field(default_factory=list)
    #: (suite, version, case) already executed this session; a second run of one is refused, never recorded.
    executed: set[tuple[str, int, str]] = field(default_factory=set[tuple[str, int, str]])
    refused_reruns: int = 0
    #: Each adapter's `verify-adapters` status this session, by (adapter id, site): checked once, before its cases.
    adapters: dict[tuple[str, str | None], tuple[str, str]] = field(
        default_factory=dict[tuple[str, str | None], tuple[str, str]]
    )
    adapter_bindings: dict[tuple[str, str | None], str] = field(default_factory=dict)
    #: One id per session: a live judging names its run and attempt, so a repeat is never a refused replay.
    run_id: str = field(default_factory=lambda: uuid4().hex)


class Attempt(BaseModel):
    output: JsonValue = None
    reason: str | None = None
    #: The attempt's replies as the backend's verifier reads them (`replay._capture.subject_of`), its tool calls included.
    reply: JsonValue = None
    #: Local provenance is absent for legacy children; never store provider requests or response content here.
    mode: Literal["live", "replay"] | None = None
    provider_successes: int | None = None
    #: The digest of the input this attempt's child was handed (`_run.input_digest`), set by the parent from the exact
    #: bytes it wrote, never read from the child's receipt. Only the digest is kept, never the input.
    input_digest: str | None = None
    request_fingerprint: str | None = None
    #: The last line of a failed child's stderr (`CHILD_FAILED`), clipped — for a Python traceback, the exception it ends
    #: with. It is the application's own stderr and stays with its owner: the local results and the failure message carry
    #: it, and `hajer._upload._wire_receipt` strips `executionEvidence` before anything is sent.
    stderr_tail: str | None = None


# Offline measurement records; their JSON is checked against the engine contract in tests.
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class RepeatValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepeatScope(RepeatValue):
    team_id: Identifier
    project_id: Identifier
    workflow_id: Identifier
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]
    suite_id: Identifier
    suite_version: Annotated[int, Field(ge=1)]
    case_id: Identifier
    check_id: Identifier
    configuration_digest: Digest


class RepeatPlan(RepeatValue):
    plan_id: Identifier
    scope: RepeatScope
    sampling_policy: Literal["FIXED_REPEATS"] = "FIXED_REPEATS"
    planned_attempt_ids: tuple[Identifier, ...]
    sampling_assumption: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"
    k: Annotated[int, Field(ge=1)] = 1
    planned_at: AwareDatetime


class RepeatAttempt(RepeatValue):
    attempt_id: Identifier
    configuration_digest: Digest
    outcome: Literal["SATISFIES", "VIOLATES", "UNKNOWN"]
    evidence_id: Identifier
    observed_at: datetime
    reason: Identifier | None = None
    request_fingerprint: Digest | None = None


class RepeatMeasurement(RepeatValue):
    evidence_origin: Literal["SUPPLIED_EXECUTION_RECORDS"] = "SUPPLIED_EXECUTION_RECORDS"
    plan: RepeatPlan
    attempts: tuple[RepeatAttempt, ...] = ()

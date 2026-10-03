"""What the SDK returns to application code: the `Assessment` and the three records under it.

These names and these statuses are the customer's own source: their `if` compares against
`"violated"`, so the four statuses and the per-check reasons are spelled exactly as the platform's
verification contract spells them (lower-case for the statuses and the check reasons, upper-case for every enum the application does
not branch on). Field names are the platform's snake_case; the wire keys are its camelCase.

The claim an assessment makes is exactly *"this supplied payload satisfies these checks against this
supplied evidence"*. It does not prove delivery, a later outcome, or that omitted evidence does not
exist. `extra="ignore"` is deliberate: a newer server may add fields, and an older SDK must keep
working rather than raise in the middle of a customer's request.
"""

from __future__ import annotations

from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from hajer._vocabulary import Mode

#: What one `verify` answered. `unavailable` is never reported as `satisfied`, by anything, ever.
VerificationStatus: TypeAlias = Literal["satisfied", "violated", "insufficient_evidence", "unavailable"]

#: Why one check inside the verifier ended where it did, preserved under the four statuses.
CheckReason: TypeAlias = Literal[
    "satisfied", "violated", "unsupported", "invalid", "not_applicable", "missing_evidence"
]

#: Absent, redacted, truncated, stale and invalid are five different facts about one missing field.
MissingEvidenceReason: TypeAlias = Literal["ABSENT", "REDACTED", "TRUNCATED", "STALE", "INVALID"]

#: Why an assessment is `unavailable`.
#:
#: The first six are the server's (contract v0): `SHADOW` is the ordinary one — a verifier that has
#: not qualified records its assessment and tells the caller nothing it could gate on. The last five
#: are the SDK's own, decided locally, and they are deliberately distinct from the server's so a
#: reader can tell "we never asked" from "the service answered that it could not". In particular
#: `LOCAL_DEADLINE` (the caller's clock ran out here) is not `DEADLINE` (the server refused before
#: reserving because the route's measured p95 exceeds the budget).
UnavailableReason: TypeAlias = Literal[
    "SHADOW",
    "DEADLINE",
    "VERIFIER_UNKNOWN",
    "JUDGE_UNAVAILABLE",
    "BUDGET_EXHAUSTED",
    "INTERNAL",
    "DISABLED",
    "LOCAL_DEADLINE",
    "TRANSPORT",
    "BODY_OVER_BOUND",
    "HTTP_STATUS",
    "MALFORMED_RESPONSE",
]

#: The reasons above that the SDK decides by itself, without the server having answered.
LOCAL_REASONS: Final[frozenset[str]] = frozenset(
    {"DISABLED", "LOCAL_DEADLINE", "TRANSPORT", "BODY_OVER_BOUND", "HTTP_STATUS", "MALFORMED_RESPONSE"}
)


class _Wire(BaseModel):
    """Base for everything read off the wire: camelCase keys in, snake_case attributes out."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        frozen=True,
        extra="ignore",
    )


class Finding(_Wire):
    """A substantiated statement about the supplied payload, naming what it rests on."""

    obligation: str
    check: str
    statement: str
    evidence_used: tuple[str, ...] = ()


class MissingEvidence(_Wire):
    """One evidence-contract field the assessment needed and did not get, and why."""

    field: str
    reason: MissingEvidenceReason
    detail: str = ""


class CheckOutcome(_Wire):
    """One check's own answer, kept underneath the aggregate status."""

    check: str
    reason: CheckReason
    detail: str = ""
    evidence_used: tuple[str, ...] = ()


class ObservationRow(_Wire):
    """One recorded observation, as the tail lists it: what arrived, not what it said.

    The backend's own row (`ObservationRowOut` in the generated wire), read through the same
    camelCase-in/snake_case-out base as everything else. `created_at` stays the wire's string: the tail
    prints it, and parsing an instant only to format it again would be the SDK deciding how somebody's
    terminal shows a timestamp.

    `verifier` is `None` for an attach-mode observation — one that named none, so nothing will assess it.
    `provider` and `model` are `None` when the wrapped calls arrived as summaries rather than captures,
    because then no model-call receipt exists and the summary's word for it is not a reading.

    `case_key` is which case this attempt is an attempt at, and `case_key_source` is who decided that —
    `CALLER`, `DERIVED_CLIENT` or `DERIVED_SERVER` (`python/docs/reference.md`, case identity). Both are
    plain strings rather than a closed `Literal`: a newer server adding a fourth source must not raise
    inside somebody's tail.

    `mode` is a closed vocabulary read the forward-compatible way (`_vocabulary.py`): a member this client
    knows, or `UNKNOWN` **with the raw string kept**. Two vocabularies grew in one plan — `origin` gained
    `BROKER` and `cost_source` gained `LOCAL_PRICED` — and a client that raised on the third would be
    discovering a server upgrade inside somebody's request path.
    """

    id: str
    created_at: str
    mode: Mode
    verifier: str | None = None
    provider: str | None = None
    model: str | None = None
    case_key: str | None = None
    case_key_source: str | None = None
    redactions: int = 0
    redacted_classes: tuple[str, ...] = ()
    disposition: str = ""
    shadow: bool = False
    assessed: bool = False


class Assessment(_Wire):
    """What one `verify` answered.

    `shadow` is a fact about trust, not about the answer: a shadow assessment is computed and
    recorded in full, and what reaches the application is `unavailable{reason: SHADOW}` rather than
    anything it could gate on.
    """

    status: VerificationStatus
    reason: UnavailableReason | None = Field(
        default=None, validation_alias="unavailableReason", serialization_alias="unavailableReason"
    )
    findings: tuple[Finding, ...] = ()
    missing_evidence: tuple[MissingEvidence, ...] = ()
    shadow: bool = False
    per_check: tuple[CheckOutcome, ...] = Field(
        default=(), validation_alias="checkOutcomes", serialization_alias="checkOutcomes"
    )
    cost_microusd: int = 0
    latency_ms: int = 0
    #: What this answer does not cover, in sentences: the client redacted before sending, its pass degraded,
    #: a declared path is a hash (the case-key recipe and the service's own
    #: limitation sentences). Empty from a server that predates them, which is why it has a default:
    #: a newer field must not raise inside a customer's request path.
    limitations: tuple[str, ...] = ()
    #: The verifier version the server applied (`policy@3`), when it got as far as choosing one.
    verifier: str | None = None
    #: The recorded observation, and what `ObserveReceipt.poll()` asks about.
    observation_id: str | None = None

    @classmethod
    def unavailable_now(cls, reason: UnavailableReason, *, latency_ms: int = 0) -> Assessment:
        """The assessment the SDK returns when it decided, locally, that there is no answer."""
        return cls(status="unavailable", reason=reason, latency_ms=latency_ms)

    @property
    def decided_locally(self) -> bool:
        """True when this assessment is the SDK's own verdict and no server answered."""
        return self.reason is not None and self.reason in LOCAL_REASONS

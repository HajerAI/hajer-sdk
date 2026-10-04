"""Hajer — the verifier in your own code.

At the point where a model output crosses into a side effect, hand Hajer the request, the output and
the evidence you chose. Hajer applies a named, versioned **verifier** and returns an **assessment**.
Your application decides what to do with it.

    import hajer

    hajer_client = hajer.Hajer()                  # HAJER_API_KEY, HAJER_TEAM_ID from the environment
    openai_client = hajer.wrap(OpenAI())          # the model calls are recorded beside the answer

    reply = openai_client.chat.completions.create(model="…", messages=…)
    assessment = hajer_client.verify(
        "refund-policy",                                               # the verifier
        {"orderId": order.id, "question": question},                   # the request
        reply.choices[0].message.content,                              # the output, as it will be sent
        {"orderState": order.state, "approvedPolicy": policy.text},    # the evidence you chose
    )
    if assessment.status == "violated":
        ...                                       # your policy, your decision

Without `HAJER_API_KEY` and `HAJER_TEAM_ID` the client is inert: `verify` returns
`unavailable{reason: DISABLED}`, `observe` is a no-op, nothing opens a socket and nothing raises — so
a test suite with no Hajer credentials runs exactly as it did before the SDK was added.

`hajer.scope()` is the unit an obligation is about: every provider call made inside the block — including
the ones a framework makes in child tasks — belongs to it, and the `verify` inside the block carries them.

`hajer.workflow(id)`, `hajer.component(id)` and `hajer.tool(id)` declare the shape around those calls as spans — a
decorator or a `with` block each — for the application's own OpenTelemetry provider or, under `hajer eval`, the
engine's receiver. `workflow` also enters `scope(workflow=id)`, so the span and the recorded calls carry one id.
Without the `otel` extra they run the code and emit nothing (`hajer/_telemetry.py`).

`hajer.attach()` (or `HAJER_ATTACH=1` with the import-time hook) is the other way round: no verifier, no
scope, one `observe` observation per provider call, for a process nobody has instrumented yet.
`hajer/_attach.py` states exactly what leaves the process at each capture setting.

`README.md` has every setting, the delivery contract of `observe`, and what `wrap` can and cannot
capture. The module docstrings say the same things where the code is.
"""

from __future__ import annotations

from hajer._attach import Attachment, attach, attachment, detach
from hajer._boundary import record_boundaries
from hajer._client import AsyncHajer, Hajer
from hajer._errors import (
    AssessmentUnavailableError,
    BodyOverBoundError,
    HajerConfigError,
    HajerError,
    UnsupportedClientError,
)
from hajer._http_capture import AsyncCaptureTransport, CaptureTransport
from hajer._instrumentation import Instrumentation, instrument, uninstrument
from hajer._json import JsonObject, JsonValue
from hajer._models import (
    Assessment,
    CheckOutcome,
    CheckReason,
    Finding,
    MissingEvidence,
    MissingEvidenceReason,
    ObservationRow,
    UnavailableReason,
    VerificationStatus,
)
from hajer._observe_sink import ObservationFileSink
from hajer._observe_sink import install_from_env as _install_observe_sink
from hajer._observe_sink import installed as observe_sink
from hajer._paths import VERSION
from hajer._payload import wire_source
from hajer._queue import AsyncObserveReceipt, DropReason, ObserveReceipt, ReceiptState
from hajer._redact import (
    ClientRedactionPolicy,
    RedactionEntry,
    build_policy,
    redact_document,
)
from hajer._settings import HajerSettings
from hajer._telemetry import component, tool, workflow
from hajer._wrap import (
    Operation,
    ToolCall,
    ToolResult,
    WrappedCall,
    clear_wrapped_calls,
    scope,
    wrap,
    wrapped_calls,
    wrapped_calls_dropped,
)

__version__ = VERSION

#: The file sink is armed at import, because the process that needs it is the one nobody instrumented:
#: a test suite whose transports are patched imports the SDK through its own application and never
#: calls `attach()`. It is a no-op — no directory, no file, no hook — unless `HAJER_OBSERVE_SINK` names
#: a directory, and even then nothing is written until a call settles.
_SINK = _install_observe_sink()

#: The public API. Everything else in this package is private and starts with an underscore.
__all__ = [
    "Assessment",
    "AssessmentUnavailableError",
    "AsyncCaptureTransport",
    "AsyncHajer",
    "AsyncObserveReceipt",
    "Attachment",
    "BodyOverBoundError",
    "CaptureTransport",
    "CheckOutcome",
    "CheckReason",
    "ClientRedactionPolicy",
    "DropReason",
    "Finding",
    "Hajer",
    "HajerConfigError",
    "HajerError",
    "HajerSettings",
    "Instrumentation",
    "JsonObject",
    "JsonValue",
    "MissingEvidence",
    "MissingEvidenceReason",
    "ObservationFileSink",
    "ObservationRow",
    "ObserveReceipt",
    "Operation",
    "ReceiptState",
    "RedactionEntry",
    "ToolCall",
    "ToolResult",
    "UnavailableReason",
    "UnsupportedClientError",
    "VerificationStatus",
    "WrappedCall",
    "__version__",
    "attach",
    "attachment",
    "build_policy",
    "clear_wrapped_calls",
    "component",
    "detach",
    "instrument",
    "observe_sink",
    "record_boundaries",
    "redact_document",
    "scope",
    "tool",
    "uninstrument",
    "wire_source",
    "workflow",
    "wrap",
    "wrapped_calls",
    "wrapped_calls_dropped",
]

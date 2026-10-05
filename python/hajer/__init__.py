"""Hajer — the traces of your application's model calls, in your own code.

    import hajer
    from openai import OpenAI

    hajer.instrument()                              # or hajer.wrap(client): every model call is recorded
    client = OpenAI()

    @hajer.workflow("answer-support-question")      # the unit the platform shows a trace as
    def answer(ticket):
        with hajer.session(ticket.conversation_id): # the conversation the turn belongs to
            return client.chat.completions.create(model="…", messages=…)

With `HAJER_API_KEY` and `HAJER_TEAM_ID` in the environment the spans leave for the platform over
OpenTelemetry (`hajer/_telemetry.py`); without them the SDK is inert: everything still runs, nothing
opens a socket and nothing raises, so a test suite with no Hajer credentials runs exactly as it did
before the SDK was added.

`hajer.workflow(id)`, `hajer.component(id)` and `hajer.tool(id)` declare the shape around the model calls
as spans — a decorator or a `with` block each. `workflow` also enters `hajer.scope(workflow=id)`, so the
span and the recorded calls carry one id. Without the `otel` extra they run the code and emit nothing.

`hajer.attach()` (or `HAJER_ATTACH=1` with the import-time hook) instruments a process nobody has touched:
every provider client it builds records its calls. `hajer/_attach.py` states exactly what leaves the
process at each capture setting.

docs.hajer.ai has every setting and what `wrap` can and cannot capture. The module docstrings say the
same things where the code is.
"""

from __future__ import annotations

from hajer._attach import Attachment, attach, attachment, detach
from hajer._context import TraceContextScope, context, session, user
from hajer._errors import (
    BodyOverBoundError,
    HajerConfigError,
    HajerError,
    UnsupportedClientError,
)
from hajer._http_capture import AsyncCaptureTransport, CaptureTransport
from hajer._instrumentation import Instrumentation, instrument, uninstrument
from hajer._json import JsonObject, JsonValue
from hajer._paths import VERSION
from hajer._redact import (
    ClientRedactionPolicy,
    RedactionEntry,
    build_policy,
    redact_document,
)
from hajer._settings import HajerSettings
from hajer._telemetry import component, configure, flush, install, tool, workflow
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

#: The span emitter listens to every recorded call from the moment the package is imported: a process that only
#: ever calls `wrap()` (or is attached) gets its model spans without a second line.
install()

#: The public API. Everything else in this package is private and starts with an underscore.
__all__ = [
    "AsyncCaptureTransport",
    "Attachment",
    "BodyOverBoundError",
    "CaptureTransport",
    "ClientRedactionPolicy",
    "HajerConfigError",
    "HajerError",
    "HajerSettings",
    "Instrumentation",
    "JsonObject",
    "JsonValue",
    "Operation",
    "RedactionEntry",
    "ToolCall",
    "ToolResult",
    "TraceContextScope",
    "UnsupportedClientError",
    "WrappedCall",
    "__version__",
    "attach",
    "attachment",
    "build_policy",
    "clear_wrapped_calls",
    "component",
    "configure",
    "context",
    "detach",
    "flush",
    "instrument",
    "redact_document",
    "scope",
    "session",
    "tool",
    "uninstrument",
    "user",
    "workflow",
    "wrap",
    "wrapped_calls",
    "wrapped_calls_dropped",
]

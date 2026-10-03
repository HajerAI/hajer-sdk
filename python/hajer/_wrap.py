"""`hajer.wrap(client)` — what the model call actually was, recorded beside the answer.

One line at client construction, and the next `verify` or `observe` **in the same task** carries the
model calls that produced the output it is asking about. That is the whole of it. `wrap` returns the
object it was handed, instrumented in place, so nothing downstream changes: the same client, the same
methods, the same return values, the same exceptions, re-raised unchanged.

**What it records** (`WrappedCall`): provider and api, the model, the request settings the caller
passed, the declared tool names, the tool calls the model asked for and the tool results the caller
sent back, wall-clock timing, token usage, the response id and finish reason, the error class and
message when the call raised, retries performed at this seam, whether the call streamed and whether
that stream ran to exhaustion.

**Content is captured by default**: message text, tool-call arguments and tool-result bodies.
`HAJER_CAPTURE_CONTENT=0` opts out. Client-side Layer 1 redaction runs before hosted export.

**What it records only with `HAJER_CAPTURE_RAW=1` as well** (`RawCapture`): the provider's own request
and response documents, so the service reads the call itself into a `ModelCallReceipt` instead of
taking the summary's word for it. A summary is a description and is counted as one; a capture is the
bytes and becomes a receipt. Raw is strictly more disclosing than content, so it requires content
capture and is refused without it. One capture is bounded by `HAJER_WRAPPED_CALL_MAX_BYTES`: past it the
longest texts are clipped in the middle, keeping their head and tail around their full length and SHA-256
(`TRUNCATION_MARK`) — a prompt is never dropped whole; a clipped response also sets `truncated`. A streamed
call never carries a
capture — the SSE framing is gone below this seam, so there are no bytes here to capture.

**What it can never record**, at any setting: what happened to the output after the call (a later
transformation, a template, a redaction), database truth, authorization state, whether the side
effect completed, and the provider SDK's own internal retries — those happen below this seam and are
invisible to it, so `retries` counts retries *Hajer* performed, which is always zero. A streamed call
is returned as a proxy that forwards attribute access and iteration: the chunks are the provider's own
objects, the container is not. A stream that is neither consumed nor closed is recorded as
`stream_complete=False` and never becomes complete.

No provider library is imported here, at module import time or ever: `wrap` finds the surfaces by
attribute and replaces the leaf `create` on the object it was given.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import functools
import hashlib
import inspect
import itertools
import json
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Generator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, TypeAlias, TypeVar, cast
from unittest.mock import NonCallableMock
from urllib.parse import urlsplit

from hajer._claims import OPEN_CALL, claim, remember
from hajer._errors import UnsupportedClientError
from hajer._frame_context import frames_for_call, install_task_frames
from hajer._frames import CALLER_FRAMES_UNRESOLVED, FILE_DIGESTS_CACHED, CallerFrame, file_digest
from hajer._json import JsonObject, JsonValue
from hajer._raw_response import http_response, observe_body
from hajer._reads import ReadSink, recording
from hajer._settings import HajerSettings
from hajer._stream import ACTIVE_CALL, AsyncRecordingStream, DualRecordingStream, RecordingStream, StreamRecorder

#: Who answered. The provider's own name, lower case, as a receipt spells it — `openai`, `anthropic`,
#: `google`, and for a router the upstream it chose (`litellm` names it in the model: `anthropic/claude-…`).
#: A string rather than a closed vocabulary because a router's upstream list is not ours to enumerate, and
#: the wire types it as a string for the same reason (`WrappedCallSummaryIn.provider`).
Provider: TypeAlias = str
#: Which door was knocked on. `chat.completions` and `/v1/responses` are two contracts from one company,
#: so the api is recorded beside the provider and never folded into it.
Api: TypeAlias = str
#: A provider method as this module has to treat it: something callable whose signature is the provider's
#: business, not ours. Every replacement forwards `*args, **kwargs` untouched.
_Callable: TypeAlias = Callable[..., object]
#: The provider wire a raw capture was made on, spelled the way the service's own ingest spells it
#: (its `Wire` vocabulary). Three, because these are the three
#: request shapes `wrap` recognises.
Wire: TypeAlias = Literal["OPENAI_CHAT_COMPLETIONS", "OPENAI_RESPONSES", "ANTHROPIC_MESSAGES"]

_MARK: Final[str] = "__hajer_instrumented__"
#: Where a framework chat model keeps the provider client it was built with, in the order they are
#: tried. `langchain_anthropic.ChatAnthropic` keeps `anthropic.Anthropic` on `_client` and
#: `anthropic.AsyncAnthropic` on `_async_client`; `langchain_openai.ChatOpenAI` (and `AzureChatOpenAI`)
#: keeps `openai.OpenAI` on `root_client` and `openai.AsyncOpenAI` on `root_async_client`, and its
#: `client` / `async_client` are those clients' own `chat.completions`. Each calls exactly those from
#: `_generate` and `_agenerate` — so those objects are the seam, and the chat model itself is not. Read
#: by `getattr`: nothing about LangChain is imported to find them.
_INNER_CLIENT_ATTRIBUTES: Final[tuple[str, ...]] = ("_client", "_async_client", "root_client", "root_async_client")
_NS_PER_MS: Final[int] = 1_000_000
#: Bytes of one float32 in a base64-encoded embedding vector.
_FLOAT32_BYTES: Final[int] = 4
#: How deep `_jsonable` walks a value the provider handed us before it names the type instead.
_JSON_MAX_DEPTH: Final[int] = 4
#: How deep a **raw** capture walks. A provider's request and response documents are a handful of levels
#: deep; this is a loop guard rather than a shaping decision, so it is far above what either shape
#: reaches and far below anything that would exhaust the stack.
_RAW_MAX_DEPTH: Final[int] = 24

#: Request keys worth recording as settings. Everything else the caller passes is left alone: a key
#: not on this list is not evidence about the call, it is the caller's business.
_SETTING_KEYS: Final[tuple[str, ...]] = (
    "model",
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "max_output_tokens",
    "max_completion_tokens",
    "n",
    "seed",
    "stop",
    "stop_sequences",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "reasoning_effort",
    "service_tier",
    "tool_choice",
    "parallel_tool_calls",
    "stream",
    "timeout",
    "max_retries",
    # An embedding request's shape: how many dimensions were asked for, and in which encoding.
    "dimensions",
    "encoding_format",
)

#: Which wire one (provider, api) pair is, for a raw capture. The same three pairs `_surfaces` finds.
_WIRES: Final[dict[tuple[str, str], Wire]] = {
    ("openai", "chat.completions"): "OPENAI_CHAT_COMPLETIONS",
    ("openai", "responses"): "OPENAI_RESPONSES",
    ("anthropic", "messages"): "ANTHROPIC_MESSAGES",
}
#: The status a raw capture records for a call the provider answered. The SDK sits above the transport:
#: a parsed response object is the provider having answered 2xx, and that is the only thing it can
#: honestly say about the status line it never saw.
_ANSWERED_STATUS: Final[int] = 200
#: Where the status of a failed call is read from, if the provider's exception carries one.
_STATUS_ATTRIBUTES: Final[tuple[str, ...]] = ("status_code", "status")
_STATUS_MIN: Final[int] = 100
_STATUS_MAX: Final[int] = 599

#: Usage counters, in every provider's spelling, mapped onto one vocabulary. First match wins, so the
#: order is the order of specificity rather than of arrival.
_USAGE_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("prompt_tokens", "input_tokens"),
    ("completion_tokens", "output_tokens"),
    ("total_tokens", "total_tokens"),
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_input_tokens"),
    ("cache_creation_input_tokens", "cache_write_input_tokens"),
    # google-genai counts the same three things under its own names, on `usage_metadata`.
    ("prompt_token_count", "input_tokens"),
    ("candidates_token_count", "output_tokens"),
    ("total_token_count", "total_tokens"),
    ("cached_content_token_count", "cache_read_input_tokens"),
)
#: Where a provider keeps its token counts. Two names, because google-genai calls it `usage_metadata`.
_USAGE_HOLDERS: Final[tuple[str, ...]] = ("usage", "usage_metadata")
#: What the record says when a streamed call named no tokens. OpenAI sends usage on the final chunk only
#: when the request asked for it, so the absence is a property of the request and worth stating once.
STREAM_USAGE_UNOBSERVED: Final[str] = (
    "The stream named no token usage. OpenAI sends it on the final chunk only when the request carried "
    'stream_options={"include_usage": True}; without that, the usage of a streamed call is not '
    "observable at this seam."
)
#: What the record says when raw capture was asked for and this library is not one of the three provider
#: wires the service can read back into a receipt.
#: What stands in the middle of a text clipped to fit its bound: how much is missing, how long the whole was,
#: and the SHA-256 of the whole, so the two ends kept are never mistaken for the text that was sent.
TRUNCATION_MARK: Final[str] = "\n[hajer: {omitted} of {length} characters omitted here; sha256:{digest} of the whole]\n"
#: What a summary says when its content was clipped to fit `HAJER_WRAPPED_CALL_MAX_BYTES`.
CONTENT_TRUNCATED: Final[str] = (
    "CONTENT_TRUNCATED: a text of this call was longer than HAJER_WRAPPED_CALL_MAX_BYTES allows; its head and "
    "tail are kept with its full length and SHA-256 marked between them."
)
#: The fewest characters a clipped text keeps, head and tail together. Below it a text says nothing a reader
#: could use, so a document that does not fit even then is not clipped further.
_CLIP_FLOOR: Final[int] = 256
#: What a call observed from another tool's OpenTelemetry spans says, and what the installation receipt says
#: once any call is (`hajer.instrument().linkage`): the span has no caller frames to link it by.
LINKAGE_DEGRADED: Final[str] = (
    "LINKAGE_DEGRADED: observed from another tool's OpenTelemetry gen_ai spans, which carry no caller frames, "
    "so this call cannot link to a call site by frame."
)
#: What an embedding record says about the vectors it was handed back and did not keep.
EMBEDDING_VECTORS_NOT_RECORDED: Final[str] = (
    "EMBEDDING_VECTORS_NOT_RECORDED: {vectors} vector(s) of {dimensions} dimension(s) came back; only "
    "their count and size are recorded, never their values."
)
RAW_WIRE_UNKNOWN: Final[str] = (
    "HAJER_CAPTURE_RAW is on, but this call is not one of the three provider wires the service reads "
    "into a model-call receipt, so it ships as a summary and no bytes were captured."
)


# ── the record ───────────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ToolCall:
    """A tool the model asked to run. `arguments` only with `HAJER_CAPTURE_CONTENT`."""

    id: str | None
    name: str | None
    arguments: JsonValue | None = None


@dataclass(slots=True)
class ToolResult:
    """A tool result the caller sent back into the conversation. `content` only with capture."""

    tool_use_id: str | None
    is_error: bool | None = None
    content: JsonValue | None = None


@dataclass(slots=True)
class RawCapture:
    """The provider's own request and response document for one call, as the service can read them.

    This is the shape that becomes a `ModelCallReceipt`: the service parses these two documents itself
    instead of taking the summary beside them, so what the receipt says about the model, the tokens and
    the finish reason comes from the provider rather than from this SDK's reading of it.

    `truncated` is the SDK saying it clipped `response_body` to stay inside `HAJER_WRAPPED_CALL_MAX_BYTES`;
    the service then refuses to read a receipt from it. A text of `request` over the bound is clipped in its
    middle instead and marked in-band: its head and tail stay around `TRUNCATION_MARK`, which states its
    full length and SHA-256, so the request is still the request's own shape and says where it was cut.
    """

    wire: Wire
    request: JsonObject
    response_body: str = ""
    response_status: int = _ANSWERED_STATUS
    truncated: bool = False


@dataclass(slots=True)
class WrappedCall:
    """One instrumented provider call. Filled in as the call proceeds; read it after it returns."""

    provider: Provider
    api: Api
    started_at: str
    model: str | None = None
    request_settings: JsonObject = field(default_factory=dict)
    declared_tools: tuple[str, ...] = ()
    message_count: int = 0
    message_roles: tuple[str, ...] = ()
    duration_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    stream_tools: dict[tuple[int, int], ToolCall] = field(default_factory=dict, repr=False)
    response_id: str | None = None
    finish_reason: str | None = None
    error: str | None = None
    error_type: str | None = None
    retries: int = 0
    streamed: bool = False
    stream_complete: bool | None = None
    stream_chunks: int = 0
    content: JsonObject | None = None
    raw: RawCapture | None = None
    #: Caller-declared workflow, not a static callable or a unique execution id. Taken at call start.
    workflow_hint: str | None = None
    #: The application's own frames above this call, innermost first (`_frames.py`). Recorded with
    #: the raw capture and under the same consent: a frame is a location in the customer's own source,
    #: which is strictly less than the prompt bytes beside it, and without it a stored provider request
    #: is a prompt with no address.
    caller_frames: tuple[CallerFrame, ...] = ()
    #: `host:port` of the provider endpoint the wrapped client talks to, read off its `base_url`, or
    #: `None` when the client names none. The service keys an integration failure on it.
    provider_host: str | None = None
    #: Where the paths the application read off this reply accumulate while it is reading them
    #: (`_reads.py`). `None` unless `HAJER_RECORD_REPLY_READS` is on, and `reply_reads` is empty with
    #: `REPLY_READS_NOT_RECORDED` on the limitations rather than looking like "nothing was read".
    read_sink: ReadSink | None = None
    #: What this record could not observe, in prose, one sentence each. Read by the caller
    #: (`hajer.wrapped_calls()[0].limitations`), by the SDK's own tests, and — since the wire lane added
    #: `WrappedCallSummaryIn.limitations` — by the service: a streamed call with no usage says so rather
    #: than looking like a call that spent no tokens. Summaries only; a raw capture carries the provider's
    #: own bytes and `WrappedCallCaptureIn` is closed without this field.
    limitations: tuple[str, ...] = ()
    #: An embedding call's answer, as (vectors, dimensions per vector — None when unreadable): what came
    #: back, without the vectors. Not on the summary's wire; the attach output carries it.
    embedding: tuple[int, int | None] | None = field(default=None, repr=False)

    def note(self, limitation: str) -> None:
        """Record one thing this call could not observe, once."""
        if limitation not in self.limitations:
            self.limitations = (*self.limitations, limitation)

    @property
    def reply_reads(self) -> tuple[str, ...]:
        """The paths the application read off this reply, in first-read order. Empty when unrecorded.

        Read at submission rather than at settlement on purpose: the application reads the answer
        *after* the provider call returns, so a set taken when the record settled would be the set
        of reads the SDK itself made.
        """
        return () if self.read_sink is None else self.read_sink.paths

    def to_wire(self) -> JsonObject:
        """The camelCase shape the ingest envelope carries as one element of `wrappedCalls`.

        Two shapes, and which one goes out is which one this record actually holds. A `raw` capture is
        the provider's own bytes and becomes a receipt on the other side; without one the summary goes,
        and the service counts it as a call it was told about and shown no bytes of. The two shapes are
        disjoint on the wire — the service's own schemas forbid unknown fields either way — so a
        summary can never be mistaken for a capture with pieces missing.
        """
        if self.raw is not None:
            capture: JsonObject = {
                "wire": self.raw.wire,
                "request": dict(self.raw.request),
                "responseBody": self.raw.response_body,
                "responseStatus": self.raw.response_status,
                "startedAt": self.started_at,
                "durationMs": self.duration_ms,
                "streamed": self.streamed,
                "truncated": self.raw.truncated,
                "callerFrames": [frame.to_wire() for frame in self.caller_frames],
                # `null` when no sink was opened and a list when one was: an empty list is "the
                # application read nothing", `null` is "which fields it read was not observed", and a
                # reader that could not tell the two apart would count unrecorded calls as sinkless.
                "replyReads": None if self.read_sink is None else list(self.reply_reads),
            }
            if self.workflow_hint is not None:
                capture["workflowHint"] = self.workflow_hint
            return capture
        body: JsonObject = {
            "provider": self.provider,
            "api": self.api,
            "startedAt": self.started_at,
            "model": self.model,
            "requestSettings": dict(self.request_settings),
            "declaredTools": list(self.declared_tools),
            "messageCount": self.message_count,
            "messageRoles": list(self.message_roles),
            "durationMs": self.duration_ms,
            "usage": dict(self.usage),
            "toolCalls": [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in self.tool_calls],
            "toolResults": [
                {"toolUseId": result.tool_use_id, "isError": result.is_error, "content": result.content}
                for result in self.tool_results
            ],
            "responseId": self.response_id,
            "finishReason": self.finish_reason,
            "error": self.error,
            "errorType": self.error_type,
            "retries": self.retries,
            "streamed": self.streamed,
            "streamComplete": self.stream_complete,
            "streamChunks": self.stream_chunks,
            "limitations": list(self.limitations),
        }
        if self.content is not None:
            body["content"] = dict(self.content)
        if self.workflow_hint is not None:
            body["workflowHint"] = self.workflow_hint
        if self.caller_frames:
            body["callerFrames"] = [frame.to_wire() for frame in self.caller_frames]
        if self.provider_host is not None:
            body["providerHost"] = self.provider_host
        return body


# ── the per-task context, and the per-operation scope above it ───────────────────────────────────

_CALLS: contextvars.ContextVar[tuple[WrappedCall, ...]] = contextvars.ContextVar("hajer_wrapped_calls", default=())
_DROPPED: contextvars.ContextVar[int] = contextvars.ContextVar("hajer_wrapped_calls_dropped", default=0)
#: One number per operation opened in this process, so every scope has a name of its own.
_SCOPE_SEQUENCE: Final[Iterator[int]] = itertools.count(1)


@dataclass(slots=True)
class _Sink:
    """One operation's calls, as a **mutable object** every context descended from the scope shares.

    This is the whole of the fix for the task boundary. `_CALLS` holds an immutable tuple, so a child
    task's `set()` writes into its own copy of the context and the parent never sees it. A sink is a
    reference: `contextvars.copy_context()` copies the *pointer*, so a call appended inside
    `asyncio.gather`'s child task is appended to the list the parent is holding.

    The lock is not decoration. Two children of one `gather` run in one thread and cannot interleave
    mid-append, but `asyncio.to_thread` and `ThreadPoolExecutor` also copy the context, so two real
    threads can reach one sink — and a list that lost an element to a race would be a capture hole
    nobody could see.
    """

    calls: list[WrappedCall] = field(default_factory=list)
    dropped: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: This operation's name in a recorder's output. A counter and not `id(self)`, because an address
    #: is reused the moment the sink is collected and two operations would share a label.
    label: str = field(default_factory=lambda: f"scope-{next(_SCOPE_SEQUENCE)}")
    workflow_hint: str | None = None

    def record(self, call: WrappedCall, *, maximum: int) -> None:
        with self.lock:
            if len(self.calls) >= maximum:
                self.dropped += 1
                return
            self.calls.append(call)

    def taken(self) -> tuple[tuple[WrappedCall, ...], int]:
        """Everything recorded so far, and the sink emptied — what `verify` does when it attaches."""
        with self.lock:
            calls, dropped = tuple(self.calls), self.dropped
            self.calls.clear()
            self.dropped = 0
        return calls, dropped


#: The operation in force, or `None` when the caller opened none. A scope is entered in one context
#: and read from every context descended from it, which is why the value is a mutable sink.
_SCOPE: contextvars.ContextVar[_Sink | None] = contextvars.ContextVar("hajer_scope", default=None)


class Operation:
    """What one `with hajer.scope()` block recorded — read it, or let `verify` attach it for you.

    `calls` is a snapshot, so reading it never removes anything: the next `verify` or `observe` inside
    the block still carries them. Outside the block the operation is closed and holds nothing.
    """

    __slots__ = ("_sink",)

    def __init__(self, sink: _Sink) -> None:
        self._sink = sink

    @property
    def calls(self) -> tuple[WrappedCall, ...]:
        """The calls made anywhere inside this operation, including in its child tasks."""
        with self._sink.lock:
            return tuple(self._sink.calls)

    @property
    def dropped(self) -> int:
        """How many calls `HAJER_WRAPPED_CALLS_MAX` refused to keep in this operation."""
        with self._sink.lock:
            return self._sink.dropped


@contextlib.contextmanager
def scope(*, workflow: str | None = None) -> Generator[Operation]:
    """One operation: every provider call inside this block belongs to it, whichever task made it.

        with hajer.scope(workflow="answer-support-question"):
            reply = await agent.ainvoke(...)          # the framework may fan this out
            assessment = hajer_client.verify(...)     # and the calls are still here

    A scope is the unit an obligation is about — one request, one job, one turn — and it is explicit
    because the alternative is not. One sink installed at `wrap()` time would be shared by every
    context descended from the process that constructed the client, which is *every* request task in a
    server: one request's `verify` would attach another request's model calls. Under-capture is safe
    and over-attachment is not, so the sink is opened and closed around the
    operation by the code that knows where the operation begins.

    Nesting is allowed and means what it says: the inner scope takes the calls made inside it, and the
    outer one resumes when it closes. Outside every scope nothing changes — the calls go into the
    per-task context exactly as they did before, and `verify` in the same task attaches them.

    `workflow` is an optional stable, non-secret name for the operation. It guides evidence grouping;
    it does not claim a static match, prove a side effect, or author an obligation. Repeated names
    still get separate execution identities. Use a non-blank name within the service's label bound.
    An unnamed inner scope does not inherit its outer scope's declaration. A workflow key copied from
    Hajer (`source-call-…`) is also a declaration the service links by: a call in the scope that carries
    no call-site frames is linked to that workflow when the indexed source still holds the key.
    """
    sink = _Sink(workflow_hint=workflow)
    install_task_frames()  # the operation's framework tasks carry its caller's frames, on any loop
    token = _SCOPE.set(sink)
    try:
        yield Operation(sink)
    finally:
        _SCOPE.reset(token)


def wrapped_calls() -> tuple[WrappedCall, ...]:
    """The calls the current operation holds, or the ones this task recorded outside any scope."""
    sink = _SCOPE.get()
    if sink is not None:
        with sink.lock:
            return tuple(sink.calls)
    return _CALLS.get()


def wrapped_calls_dropped() -> int:
    """How many calls `HAJER_WRAPPED_CALLS_MAX` refused to keep, in the same place."""
    sink = _SCOPE.get()
    if sink is not None:
        with sink.lock:
            return sink.dropped
    return _DROPPED.get()


def clear_wrapped_calls() -> None:
    """Forget them. `verify` and `observe` call this once they have attached what they found."""
    sink = _SCOPE.get()
    if sink is not None:
        sink.taken()
        return
    _CALLS.set(())
    _DROPPED.set(0)


def _record(call: WrappedCall, settings: HajerSettings) -> WrappedCall:
    """Keep the first `wrapped_calls_max` calls of the operation — or of the task — and count the rest.

    The first calls of a workflow are the ones the obligation is usually about, so the cap drops the
    newest rather than the oldest — and says how many it dropped, instead of quietly truncating.
    """
    sink = _SCOPE.get()
    if sink is not None:
        sink.record(call, maximum=settings.wrapped_calls_max)
        return call
    existing = _CALLS.get()
    if len(existing) >= settings.wrapped_calls_max:
        _DROPPED.set(_DROPPED.get() + 1)
        return call
    _CALLS.set((*existing, call))
    return call


# ── the settled hook: what attach mode listens to ────────────────────────────────────────────────

#: Called with one finished call, after the record is complete, when the process has attached
#: (`hajer.attach()`). `_attach.py` is the only thing that ever sets it, and it is a hook rather than
#: an import because this module imports nothing of the SDK above `_settings` and never will.
SettledHook: TypeAlias = Callable[[WrappedCall], None]


@dataclass(slots=True)
class _Attachment:
    """The process-wide hook, in a holder rather than a rebound module global.

    One object whose field is written is the same fact as a `global` statement and reads as what it is:
    there is at most one attachment in a process, and `hajer.attach()` / `hajer.detach()` are the only
    two things that ever write it.
    """

    hook: SettledHook | None = None


_ATTACHMENT = _Attachment()


def set_settled_hook(hook: SettledHook | None) -> None:
    """Install (or remove) the hook every settled call is handed to. `hajer.attach()`'s seam."""
    _ATTACHMENT.hook = hook


def settled_hook() -> SettledHook | None:
    return _ATTACHMENT.hook


def accept_call(call: WrappedCall, settings: HajerSettings) -> None:
    """Alternate capture seams enter the same bounded observation and exporter path."""
    _record(call, settings)
    _settled(call)


def record_http_exchange(
    *,
    path: str,
    request: JsonObject,
    body: bytes,
    status: int,
    streamed: bool,
    complete: bool,
    truncated: bool,
    settings: HajerSettings,
    started_ns: int,
    error: BaseException | None,
) -> WrappedCall:
    """HTTP fallback produces the same records; headers and provider credentials are never copied."""
    provider = "anthropic" if path.endswith("/messages") else "openai"
    api = "messages" if provider == "anthropic" else "responses" if path.endswith("/responses") else "chat.completions"
    call = _begin(provider, api, dict(request), settings, streams=streamed)
    call.stream_complete = complete if streamed else None
    call.duration_ms = (time.monotonic_ns() - started_ns) // _NS_PER_MS
    if error is not None:
        _fail(call, error, started_ns)
    elif not streamed and not truncated:
        parsed: JsonValue = json.loads(body)
        read = {"messages": read_message, "responses": read_responses_answer}.get(api, read_chat_completion)
        read(call, parsed, settings)
    if streamed and not _fold_events(call, body, settings):
        call.note("HTTP stream bytes observed; semantic completion and token usage require provider decoding.")
    if truncated:
        call.note("HTTP capture exceeded wrapped_call_max_bytes; response content is incomplete.")
    if settings.capture_content:
        content = call.content or {}
        content["responseBody"] = body.decode("utf-8", errors="replace")
        call.content = content
    if call.raw is not None:
        call.raw.response_body = body.decode("utf-8", errors="replace")
        call.raw.response_status = status
        call.raw.truncated = truncated or (streamed and not complete)
    accept_call(call, settings)
    return call


def _fold_events(call: WrappedCall, body: bytes, settings: HajerSettings) -> bool:
    """Fold a streamed body's server-sent events into the record, as a wrapped stream is: the response id, the
    text, the usage and the finish. False when no event could be read."""
    folded = False
    for line in body.split(b"\n"):
        data = line.strip()
        if not data.startswith(b"data:") or data[len(b"data:") :].strip() == b"[DONE]":
            continue
        try:
            _observe_chunk(call, json.loads(data[len(b"data:") :]), settings)
        except Exception:  # noqa: BLE001, S112 - an event that cannot be read is skipped, never the record
            continue
        folded = True
    return folded


#: What an HTTP exchange's record says about the bytes it holds.
HTTP_EXCHANGE_OBSERVED: Final[str] = (
    "HTTP_EXCHANGE: an outbound request observed at the transport (HAJER_CAPTURE_HTTP). Its URL is a path "
    "template; its headers, query string and bodies are never recorded."
)
#: The first HTTP status that is a failed exchange.
_HTTP_FAILURE: Final[int] = 400


def open_http_call(*, method: str, url: str, host: str | None, settings: HajerSettings) -> WrappedCall:
    """The record of one outbound HTTP request, begun where it was made: its frames are the caller's now.

    `url` is already stripped of its user info, query string and fragment; the caller of this seam owns that.
    """
    call = WrappedCall(
        provider="http",
        api=method.upper(),
        started_at=datetime.now(UTC).isoformat(),
        request_settings={"method": method.upper(), "url": url},
    )
    scope_sink = _SCOPE.get()
    call.workflow_hint = None if scope_sink is None else scope_sink.workflow_hint
    call.note(HTTP_EXCHANGE_OBSERVED)
    if settings.capture_call_site:
        _call_site(call, settings)
        call.provider_host = host
    return _record(call, settings)


def settle_http_call(
    call: WrappedCall,
    *,
    status: int,
    streamed: bool,
    complete: bool,
    started_ns: int,
    error: BaseException | None,
) -> None:
    """Close one HTTP exchange's record with its status, then settle. No body is recorded, request or response:
    a third party's API body is not the prompt and answer content capture is consent for."""
    call.duration_ms = (time.monotonic_ns() - started_ns) // _NS_PER_MS
    call.streamed = streamed
    call.stream_complete = complete if streamed else None
    if error is not None:
        _fail(call, error, started_ns)
    elif status >= _HTTP_FAILURE:
        call.error = f"HTTP {status}"
        call.error_type = "HTTPStatus"
    call.request_settings["status"] = status
    _settled(call)


#: Run once per `wrap`/`attach`/`instrument` with the settings in force: what else those install. A hook
#: rather than an import for the reason `SettledHook` is one — `_http_capture` patches httpx's transports
#: and registers itself here; this module imports nothing of the SDK above `_settings`.
_INSTALLERS: Final[list[Callable[[HajerSettings], None]]] = []


def add_installer(installer: Callable[[HajerSettings], None]) -> None:
    if installer not in _INSTALLERS:
        _INSTALLERS.append(installer)


def run_installers(settings: HajerSettings) -> None:
    """What `wrap`, `attach` and `instrument` install beside the provider patch. Never raises."""
    for installer in _INSTALLERS:
        try:
            installer(settings)
        except Exception:  # noqa: BLE001, S110 - an optional capture seam may never break instrumentation
            pass


#: Called with every settled call and the label of the operation it settled in — inside a scope as
#: well as outside one, because a recorder that skipped the calls an operation claimed would hold
#: exactly the traffic an instrumented application declares. `_observe_sink.py` is the only thing that
#: sets it, and it is a hook for the same reason `SettledHook` is: this module imports nothing above
#: `_settings`.
RecordedHook: TypeAlias = Callable[[WrappedCall, str], None]


@dataclass(slots=True)
class _Recording:
    """The process-wide recorder, in a holder rather than a rebound module global."""

    hook: RecordedHook | None = None


_RECORDING = _Recording()


def set_recorded_hook(hook: RecordedHook | None) -> None:
    """Install (or remove) the recorder every settled call is handed to. The file sink's seam."""
    _RECORDING.hook = hook


def _recorded(call: WrappedCall) -> None:
    """Hand one finished call to the recorder, whether or not an operation claimed it.

    The label is the operation's, so a reader can tell one declared operation's calls from another's;
    a call settled outside every scope is labelled `process`, which is all the SDK knows about it.

    Nothing this does may reach the caller, for the same reason `_settled` may not: it runs inside
    somebody's provider call.
    """
    hook = _RECORDING.hook
    if hook is None:
        return
    sink = _SCOPE.get()
    try:
        hook(call, "process" if sink is None else sink.label)
    except Exception:  # noqa: BLE001, S110 - recording a call may never break the call
        pass


def _settled(call: WrappedCall) -> None:
    """Hand one finished call to the attachment, unless an operation has claimed it.

    Inside a `scope()` the call belongs to the operation and its `verify` carries it; emitting an
    observation of the same call here as well would report it twice. Outside every scope there is
    nobody to attach it to, and that is exactly what attach mode is for.

    Nothing this hook does may reach the caller: it runs inside somebody's provider call, and a
    monitoring path that raises into a request path is worse than a missing observation.
    """
    remember(call)
    _recorded(call)
    hook = _ATTACHMENT.hook
    if hook is None or _SCOPE.get() is not None:
        return
    try:
        hook(call)
    except Exception:  # noqa: BLE001, S110 - observing a call may never break the call
        pass


# ── reading opaque provider objects ──────────────────────────────────────────────────────────────


def _attr(target: object, name: str) -> object:
    return getattr(target, name, None)


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_seq(value: object) -> tuple[object, ...]:
    if isinstance(value, list):
        return tuple(cast(list[object], value))
    if isinstance(value, tuple):
        return cast(tuple[object, ...], value)
    return ()


def _as_mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in cast(dict[object, object], value).items()}
    return {}


def _jsonable(value: object, *, depth: int = 0, limit: int = _JSON_MAX_DEPTH) -> JsonValue:
    """A JSON value for anything, without ever raising: an unknown object becomes its type name.

    `limit` is how deep to walk. The default is the shallow one a summary wants — a summary describes a
    call, so naming the type of the fifth level down loses nothing it claimed to carry. A raw capture
    passes `_RAW_MAX_DEPTH` instead, because there the document *is* the claim.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if depth >= limit:
        return type(value).__name__
    # Both readers are called before any narrowing, so neither is handed a partially-known type.
    sequence = _as_seq(value)
    mapping = _as_mapping(value)
    if sequence or isinstance(value, (list, tuple)):
        return [_jsonable(item, depth=depth + 1, limit=limit) for item in sequence]
    if mapping or isinstance(value, dict):
        return {key: _jsonable(item, depth=depth + 1, limit=limit) for key, item in mapping.items()}
    dump = _attr(value, "model_dump")
    if callable(dump):
        try:
            return _jsonable(_dumped(dump), depth=depth + 1, limit=limit)
        except (TypeError, ValueError):
            return type(value).__name__
    return type(value).__name__


def _dumped(dump: _Callable) -> object:
    """A pydantic model's JSON document, without the serializer warnings pydantic prints for a field it did not
    expect (`ParsedChatCompletion.parsed` holds the caller's own model): reading an answer writes to nobody's
    stderr. A `model_dump` that takes no `warnings` is asked plainly."""
    try:
        return dump(mode="json", warnings=False)
    except TypeError:
        return dump(mode="json")


def _member(target: object, *names: str) -> object:
    """The first attribute or mapping key that is present, so dicts and objects read the same."""
    mapping = _as_mapping(target)
    for name in names:
        if name in mapping:
            return mapping[name]
        found = _attr(target, name)
        if found is not None:
            return found
    return None


# ── request side ─────────────────────────────────────────────────────────────────────────────────


def _tool_names(tools: object) -> tuple[str, ...]:
    names: list[str] = []
    for tool in _as_seq(tools):
        direct = _as_str(_member(tool, "name"))
        if direct is not None:
            names.append(direct)
            continue
        function = _member(tool, "function")
        nested = _as_str(_member(function, "name")) if function is not None else None
        if nested is not None:
            names.append(nested)
    return tuple(names)


def _tool_results(messages: tuple[object, ...], *, capture: bool) -> tuple[ToolResult, ...]:
    results: list[ToolResult] = []
    for message in messages:
        if _member(message, "type") == "function_call_output":
            results.append(
                ToolResult(
                    tool_use_id=_as_str(_member(message, "call_id")),
                    content=_jsonable(_member(message, "output")) if capture else None,
                )
            )
            continue
        role = _as_str(_member(message, "role"))
        if role == "tool":  # openai chat: one message per result
            results.append(
                ToolResult(
                    tool_use_id=_as_str(_member(message, "tool_call_id")),
                    content=_jsonable(_member(message, "content")) if capture else None,
                )
            )
            continue
        for block in _as_seq(_member(message, "content")):
            if _as_str(_member(block, "type")) != "tool_result":  # anthropic: a block per result
                continue
            results.append(
                ToolResult(
                    tool_use_id=_as_str(_member(block, "tool_use_id")),
                    is_error=_as_bool(_member(block, "is_error")),
                    content=_jsonable(_member(block, "content")) if capture else None,
                )
            )
    return tuple(results)


def _embedding_inputs(given: object) -> tuple[object, ...]:
    """An embedding request's inputs: a string, a list of strings, one token list or a list of token lists."""
    whole: tuple[object, ...] = (given,)
    items = _as_seq(given)
    if items:
        # A list of integers is one tokenised input, not one input per token.
        return whole if all(_as_int(item) is not None for item in items) else items
    return () if given is None or isinstance(given, (list, tuple)) else whole


def _begin(
    provider: Provider, api: Api, kwargs: dict[str, object], settings: HajerSettings, *, streams: bool = False
) -> WrappedCall:
    """The record of one call, as it looked when it left. `streams` is for a surface that always streams.

    `create(stream=True)` says so in the request; `messages.stream(...)` and
    `models.generate_content_stream(...)` are separate methods and say it by being themselves, which is
    what `streams` carries.
    """
    capture = settings.capture_content
    scope_sink = _SCOPE.get()
    embedding = api == "embeddings"
    messages = (
        _embedding_inputs(kwargs.get("input"))
        if embedding
        else _as_seq(kwargs.get("messages") or kwargs.get("input") or kwargs.get("contents"))
    )
    call = WrappedCall(
        provider=provider,
        api=api,
        started_at=datetime.now(UTC).isoformat(),
        workflow_hint=None if scope_sink is None else scope_sink.workflow_hint,
        model=_as_str(kwargs.get("model")),
        request_settings={
            key: _jsonable(kwargs[key]) for key in _SETTING_KEYS if key in kwargs and kwargs[key] is not None
        },
        declared_tools=_tool_names(kwargs.get("tools")),
        message_count=len(messages),
        message_roles=tuple(role for role in (_as_str(_member(item, "role")) for item in messages) if role),
        tool_results=_tool_results(messages, capture=capture),
        streamed=streams or kwargs.get("stream") is True,
    )
    if embedding:
        pass  # metadata only: the embedded text is not captured, and `read_embeddings` adds the answer's shape
    elif capture:
        call.content = {
            "system": _jsonable(kwargs.get("system") or kwargs.get("instructions")),
            "messages": [_jsonable(item) for item in messages],
        }
    _bound_content(call, settings)
    if settings.capture_call_site:
        _call_site(call, settings)
    if settings.capture_raw:
        wire = _WIRES.get((provider, api))
        if wire is None:
            call.note(RAW_WIRE_UNKNOWN)
        else:
            call.raw = RawCapture(wire=wire, request=_request_document(kwargs))
        # The frames a raw capture carries are the call-site frames above: relative to the project root,
        # never a home directory, and none at all with `HAJER_CAPTURE_CALL_SITE=0`.
    if settings.record_reply_reads:
        call.read_sink = ReadSink()
    return call


#: What a frame's file becomes when it lies outside the project root: this marker and its basename only.
OUTSIDE_ROOT: Final[str] = "<outside>"
#: A home directory at the front of a path, POSIX or Windows (separators already `/`): never sent. macOS
#: resolves `/home` to `/System/Volumes/Data/home`, so that spelling of a home directory is one too.
_HOME: Final[re.Pattern[str]] = re.compile(
    r"^(?:/System/Volumes/Data)?(?:/home/[^/]+|/Users/[^/]+|/root|[A-Za-z]:/Users/[^/]+)(?=/|$)"
)
#: A root that is no root: the filesystem root, or a bare drive. Nothing is "under" it for disclosure.
_NO_ROOT: Final[re.Pattern[str]] = re.compile(r"^(?:[A-Za-z]:)?/?$")
_ANCHORED: Final[re.Pattern[str]] = re.compile(r"^(?:/|~|[A-Za-z]:)")
_HOSTNAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]{1,253}$")


@functools.lru_cache(maxsize=FILE_DIGESTS_CACHED)
def _resolved(path: str) -> str | None:
    """The path with every symlink resolved, or None when it is not an absolute path of this system."""
    if not os.path.isabs(path):
        return None
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _under(file: str, root: str) -> str | None:
    """`file` relative to `root` when it lies under it, both with any home directory already replaced."""
    path = _HOME.sub("~", file.replace("\\", "/"))
    base = _HOME.sub("~", root.replace("\\", "/")).rstrip("/")
    if not _NO_ROOT.fullmatch(base) and path.startswith(f"{base}/"):
        return path[len(base) + 1 :]
    return None


def located_file(file: str, root: str) -> str:
    """A frame's file as it may leave the process: relative to the project root, or `<outside>/<basename>`.

    Never an absolute path and never a home directory, whatever the root: both are replaced by `~` before
    anything is compared, so a root above a home directory (`/`, `/home`) puts every file under it outside.
    The two are compared as spelled, then with their symlinks resolved: on macOS `Path.cwd()` answers
    `/private/tmp/…` for a checkout a module was imported from as `/tmp/…`, and both name one file.
    """
    inside = _under(file, root)
    if inside is None:
        real_file, real_root = _resolved(file), _resolved(root)
        if real_file is not None and real_root is not None:
            inside = _under(real_file, real_root)
    if inside is not None:
        return inside
    path = _HOME.sub("~", file.replace("\\", "/"))
    if not _ANCHORED.match(path) and ".." not in path.split("/"):
        return path.removeprefix("./")
    return f"{OUTSIDE_ROOT}/{path.rsplit('/', 1)[-1]}"


def _call_site(call: WrappedCall, settings: HajerSettings) -> None:
    """Where the call was made, and a digest of each frame's file: what call-site linkage reads to name the call
    site that made it, in the revision that ran it. The path leaves relative; the digest is of the bytes."""
    root = settings.project_root or str(Path.cwd())
    call.caller_frames = tuple(
        CallerFrame(
            module=frame.module,
            qualname=frame.qualname,
            file=located,
            line=frame.line,
            # Only a file under the project root is the application's own: nothing outside it is digested.
            file_digest=None if located.startswith(f"{OUTSIDE_ROOT}/") else file_digest(frame.file),
        )
        for frame in frames_for_call()
        for located in (located_file(frame.file, root),)
    )
    if not call.caller_frames:
        call.note(CALLER_FRAMES_UNRESOLVED)


def _provider_host(client: object) -> str | None:
    """`host:port` of the client's `base_url` (IDNA-encoded, the scheme's default port), or None when unreadable."""
    base = _attr(client, "base_url")
    return None if base is None else host_of(base)


def host_of(base: object) -> str | None:
    """`host:port` of one URL (IDNA-encoded, the scheme's default port), or None when unreadable."""
    try:
        parts = urlsplit(str(base))
        port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
        hostname = parts.hostname
        if not hostname or port is None:
            return None
        host = f"[{hostname}]" if ":" in hostname else hostname.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return None
    return f"{host}:{port}" if host.startswith("[") or _HOSTNAME.fullmatch(host) else None


def _request_document(kwargs: dict[str, object]) -> JsonObject:
    """Everything the caller passed, as a JSON document: the provider request, not a selection from it.

    `request_settings` records the keys that are evidence about the call; this is the other thing, and
    the difference is the point of raw capture. A value that will not survive a JSON round trip becomes
    its type name rather than raising inside somebody's request path.
    """
    return {key: _jsonable(value, limit=_RAW_MAX_DEPTH) for key, value in kwargs.items()}


def _encoded(value: JsonValue) -> str:
    """One document as the bytes that will go on the wire, so the bound is measured and not guessed."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _clip(text: str, keep: int) -> str:
    """The text's head and tail around `TRUNCATION_MARK`, or the text itself when it is `keep` or shorter."""
    if len(text) <= keep:
        return text
    head = keep - keep // 2
    tail = keep // 2
    digest = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()
    mark = TRUNCATION_MARK.format(omitted=len(text) - keep, length=len(text), digest=digest)
    return text[:head] + mark + text[len(text) - tail :]


def _clipped(value: JsonValue, keep: int) -> JsonValue:
    """Every string in the document clipped to `keep` characters; its shape, keys and other values untouched."""
    if isinstance(value, str):
        return _clip(value, keep)
    if isinstance(value, list):
        return [_clipped(item, keep) for item in value]
    if isinstance(value, dict):
        return {key: _clipped(item, keep) for key, item in value.items()}
    return value


def _longest(value: JsonValue) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return max((_longest(item) for item in value), default=0)
    if isinstance(value, dict):
        return max((_longest(item) for item in value.values()), default=0)
    return 0


def fitted(value: JsonValue, budget: int) -> tuple[JsonValue, bool] | None:
    """The document inside `budget` encoded bytes: itself, or with its long strings clipped in the middle.

    The clip is the longest one that fits, found by bisection, and applies only to strings longer than it,
    so short fields — roles, ids, names — are never touched. `(document, clipped)`, or None when even the
    `_CLIP_FLOOR` clip does not fit: then it is the document's shape, not its text, that is too large.
    """
    if len(_encoded(value).encode("utf-8")) <= budget:
        return value, False
    low, high = _CLIP_FLOOR, _longest(value) - 1
    best: JsonValue | None = None
    found = False
    while low <= high:
        keep = (low + high) // 2
        candidate = _clipped(value, keep)
        if len(_encoded(candidate).encode("utf-8")) <= budget:
            best, found, low = candidate, True, keep + 1
        else:
            high = keep - 1
    return (best, True) if found else None


def _bound_content(call: WrappedCall, settings: HajerSettings) -> None:
    """Fit the summary's content inside `HAJER_WRAPPED_CALL_MAX_BYTES`, clipping texts rather than dropping them."""
    content = call.content
    if content is None:
        return
    result = fitted(content, settings.wrapped_call_max_bytes)
    document, clipped = result if result is not None else (_clipped(content, _CLIP_FLOOR), True)
    if clipped and isinstance(document, dict):
        call.content = document
        call.note(CONTENT_TRUNCATED)


def _bound_raw(call: WrappedCall, settings: HajerSettings) -> None:
    """Fit the capture inside `HAJER_WRAPPED_CALL_MAX_BYTES`, clipping texts in the middle rather than dropping them.

    The request keeps at least half the bound, or whatever the answer leaves; an over-long text in it keeps its
    head and tail around its full length and digest (`TRUNCATION_MARK`), so the document is still the request's
    own shape and says in its own text where it was cut. The response document is fitted the same way into what
    is left, and only then is the capture `truncated`: the service reads that flag as "the response was clipped"
    and refuses to read a receipt from it. Only a document whose shape alone does not fit ships as a summary.
    """
    raw = call.raw
    if raw is None:
        return
    maximum = settings.wrapped_call_max_bytes
    answer = raw.response_body.encode("utf-8")
    request = fitted(raw.request, max(maximum - len(answer), maximum // 2))
    if request is None or not isinstance(request[0], dict):
        call.raw = None
        return
    raw.request = request[0]
    if request[1]:
        # Marked in the text itself: `truncated` is the service's word for a clipped *response*, which it
        # then refuses to read, and this response is whole.
        call.note(CONTENT_TRUNCATED)
    budget = maximum - len(_encoded(raw.request).encode("utf-8"))
    if budget <= 0:
        call.raw = None
        return
    if len(answer) > budget:
        raw.truncated = True
        try:
            document: JsonValue = json.loads(answer)
        except ValueError:
            document = None
        response = None if document is None else fitted(document, budget)
        raw.response_body = (
            _encoded(response[0]) if response is not None else answer[:budget].decode("utf-8", errors="ignore")
        )


# ── response side ────────────────────────────────────────────────────────────────────────────────


def _usage(source: object) -> dict[str, int]:
    usage = _member(source, *_USAGE_HOLDERS)
    if usage is None:
        return {}
    counted: dict[str, int] = {}
    for wire_name, our_name in _USAGE_KEYS:
        value = _as_int(_member(usage, wire_name))
        if value is not None:
            counted.setdefault(our_name, value)
    details = _member(usage, "input_tokens_details", "prompt_tokens_details")
    cached = _as_int(_member(details, "cached_tokens")) if details is not None else None
    if cached is not None:
        counted.setdefault("cache_read_input_tokens", cached)
    return counted


def _openai_chat_tool_calls(response: object, *, capture: bool) -> tuple[tuple[ToolCall, ...], str | None, JsonValue]:
    calls: list[ToolCall] = []
    finish: str | None = None
    texts: list[JsonValue] = []
    for choice in _as_seq(_member(response, "choices")):
        finish = finish or _as_str(_member(choice, "finish_reason"))
        message = _member(choice, "message")
        if message is None:
            continue
        if capture:
            texts.append(_jsonable(_member(message, "content")))
        for raw in _as_seq(_member(message, "tool_calls")):
            function = _member(raw, "function")
            calls.append(
                ToolCall(
                    id=_as_str(_member(raw, "id")),
                    name=_as_str(_member(function, "name")) if function is not None else None,
                    arguments=_jsonable(_member(function, "arguments")) if capture and function is not None else None,
                )
            )
    return tuple(calls), finish, texts


def _openai_responses_tool_calls(
    response: object, *, capture: bool
) -> tuple[tuple[ToolCall, ...], str | None, JsonValue]:
    calls: list[ToolCall] = []
    texts: list[JsonValue] = []
    for item in _as_seq(_member(response, "output")):
        kind = _as_str(_member(item, "type"))
        if kind == "function_call":
            calls.append(
                ToolCall(
                    id=_as_str(_member(item, "call_id")) or _as_str(_member(item, "id")),
                    name=_as_str(_member(item, "name")),
                    arguments=_jsonable(_member(item, "arguments")) if capture else None,
                )
            )
        elif capture and kind == "message":
            texts.append(_jsonable(_member(item, "content")))
    return tuple(calls), _as_str(_member(response, "status")), texts


def _anthropic_tool_calls(response: object, *, capture: bool) -> tuple[tuple[ToolCall, ...], str | None, JsonValue]:
    calls: list[ToolCall] = []
    texts: list[JsonValue] = []
    for block in _as_seq(_member(response, "content")):
        kind = _as_str(_member(block, "type"))
        if kind == "tool_use":
            calls.append(
                ToolCall(
                    id=_as_str(_member(block, "id")),
                    name=_as_str(_member(block, "name")),
                    arguments=_jsonable(_member(block, "input")) if capture else None,
                )
            )
        elif capture and kind == "text":
            texts.append(_jsonable(_member(block, "text")))
    return tuple(calls), _as_str(_member(response, "stop_reason")), texts


#: How one library's answer is read into the record. Each surface names its own, so a new library is a
#: reader and a row in the candidate table rather than a branch in the middle of the instrumentation.
Reader: TypeAlias = Callable[["WrappedCall", object, HajerSettings], None]


def record_answer(
    call: WrappedCall,
    response: object,
    settings: HajerSettings,
    *,
    tool_calls: tuple[ToolCall, ...],
    finish_reason: str | None,
    texts: JsonValue,
) -> None:
    """Everything every answer has, once: the identity, the model, the usage, the capture.

    The per-library readers differ only in how the tool calls, the finish reason and the text are found;
    what is done with them is the same, and doing it in one place is what keeps a new library from
    quietly recording a different shape of the same call.
    """
    call.tool_calls = tool_calls
    call.finish_reason = finish_reason
    call.response_id = _as_str(_member(response, "id", "response_id"))
    call.model = _as_str(_member(response, "model", "model_version")) or call.model
    call.usage = _usage(response) or call.usage
    if settings.capture_content:
        content = call.content if call.content is not None else {}
        content["output"] = texts
        call.content = content
        _bound_content(call, settings)
    if call.raw is not None:
        call.raw.response_body = _encoded(_jsonable(response, limit=_RAW_MAX_DEPTH))
        call.raw.response_status = _ANSWERED_STATUS
        _bound_raw(call, settings)


def read_chat_completion(call: WrappedCall, response: object, settings: HajerSettings) -> None:
    """An OpenAI chat completion — and anything shaped like one, a router's dict included."""
    calls, finish, texts = _openai_chat_tool_calls(response, capture=settings.capture_content)
    record_answer(call, response, settings, tool_calls=calls, finish_reason=finish, texts=texts)


def read_responses_answer(call: WrappedCall, response: object, settings: HajerSettings) -> None:
    """An OpenAI `/v1/responses` answer."""
    calls, finish, texts = _openai_responses_tool_calls(response, capture=settings.capture_content)
    record_answer(call, response, settings, tool_calls=calls, finish_reason=finish, texts=texts)


def read_message(call: WrappedCall, response: object, settings: HajerSettings) -> None:
    """An Anthropic message."""
    calls, finish, texts = _anthropic_tool_calls(response, capture=settings.capture_content)
    record_answer(call, response, settings, tool_calls=calls, finish_reason=finish, texts=texts)


def _dimensions(item: object) -> int | None:
    """How many dimensions one returned vector has: a float list's length, or a base64 float32 string's."""
    vector = _member(item, "embedding")
    values = _as_seq(vector)
    if values:
        return len(values)
    if isinstance(vector, str):
        try:
            return len(base64.b64decode(vector, validate=True)) // _FLOAT32_BYTES
        except (ValueError, TypeError):
            return None
    return None


def read_embeddings(call: WrappedCall, response: object, settings: HajerSettings) -> None:
    """An OpenAI embeddings answer, as metadata: the model, the usage, how many vectors and of what size.

    The vectors themselves are never read into the record. They are not evidence about the call a person can
    check — a float list — and they are the bulk of the answer, so a record that carried them would carry
    little else inside any bound.
    """
    vectors = _as_seq(_member(response, "data"))
    dimensions = _dimensions(vectors[0]) if vectors else None
    call.model = _as_str(_member(response, "model")) or call.model
    call.usage = _usage(response) or call.usage
    call.embedding = (len(vectors), dimensions)
    call.note(EMBEDDING_VECTORS_NOT_RECORDED.format(vectors=len(vectors), dimensions=dimensions))
    if settings.capture_content:
        content = call.content if call.content is not None else {}
        content["output"] = {"vectors": len(vectors), "dimensions": dimensions}
        call.content = content


def _as_label(value: object) -> str | None:
    """A closed-vocabulary value as text: a string, or the name of the enum member it is.

    google-genai spells its finish reasons as enum members (`FinishReason.STOP`), and a receipt wants the
    word rather than the repr of a class the SDK must not import to recognise.
    """
    if isinstance(value, str):
        return value
    name = _attr(value, "name")
    return name if isinstance(name, str) else None


def read_genai_answer(call: WrappedCall, response: object, settings: HajerSettings) -> None:
    """A google-genai `GenerateContentResponse`: its parts, its function calls, its `usage_metadata`.

    The shape is a candidate list whose content is a part list, and a part is either text or a function
    call. Read by attribute like every other provider object, so nothing about `google.genai` is imported
    here either — `record_answer` finds the model under `model_version` and the tokens under
    `usage_metadata` because both names are in the tables it reads.
    """
    capture = settings.capture_content
    calls: list[ToolCall] = []
    texts: list[JsonValue] = []
    finish: str | None = None
    for candidate in _as_seq(_member(response, "candidates")):
        finish = finish or _as_label(_member(candidate, "finish_reason"))
        for part in _as_seq(_member(_member(candidate, "content"), "parts")):
            function = _member(part, "function_call")
            if function is not None:
                calls.append(
                    ToolCall(
                        id=_as_str(_member(function, "id")),
                        name=_as_str(_member(function, "name")),
                        arguments=_jsonable(_member(function, "args")) if capture else None,
                    )
                )
                continue
            text = _as_str(_member(part, "text"))
            if capture and text is not None:
                texts.append(text)
    record_answer(call, response, settings, tool_calls=tuple(calls), finish_reason=finish, texts=texts)


def _observe_chunk(call: WrappedCall, chunk: object, settings: HajerSettings) -> None:
    _observe_stream_tools(call, chunk, settings)
    call.stream_chunks += 1
    call.response_id = call.response_id or _as_str(_member(chunk, "id"))
    usage = _usage(chunk)
    if usage:
        call.usage.update(usage)
    message = _member(chunk, "message")
    if message is not None:
        call.model = _as_str(_member(message, "model")) or call.model
        call.usage.update(_usage(message))
    stop = _as_str(_member(chunk, "stop_reason"))
    if stop is not None:
        call.finish_reason = stop
    for choice in _as_seq(_member(chunk, "choices")):
        finish = _as_str(_member(choice, "finish_reason"))
        if finish is not None:
            call.finish_reason = finish
    if settings.capture_content:
        _append_delta(call, chunk)


def _observe_stream_tools(call: WrappedCall, chunk: object, settings: HajerSettings) -> None:
    kind = _member(chunk, "type")
    if kind == "response.completed":
        read_responses_answer(call, _member(chunk, "response"), settings)
        return
    index = _as_int(_member(chunk, "index"))
    block = _member(chunk, "content_block")
    if kind == "content_block_start" and _member(block, "type") == "tool_use" and index is not None:
        call.stream_tools[(0, index)] = ToolCall(id=_as_str(_member(block, "id")), name=_as_str(_member(block, "name")))
    if (
        kind == "content_block_delta"
        and index is not None
        and (0, index) in call.stream_tools
        and settings.capture_content
    ):
        fragment = _as_str(_member(_member(chunk, "delta"), "partial_json"))
        if fragment is not None:
            tool = call.stream_tools[(0, index)]
            tool.arguments = (tool.arguments if isinstance(tool.arguments, str) else "") + fragment
    for choice in _as_seq(_member(chunk, "choices")):
        choice_index = _as_int(_member(choice, "index"))
        if choice_index is None:
            continue
        for delta in _as_seq(_member(_member(choice, "delta"), "tool_calls")):
            slot = _as_int(_member(delta, "index"))
            if slot is None:
                continue
            tool = call.stream_tools.setdefault((choice_index, slot), ToolCall(id=None, name=None))
            function = _member(delta, "function")
            tool.id = _as_str(_member(delta, "id")) or tool.id
            tool.name = _as_str(_member(function, "name")) or tool.name
            fragment = _as_str(_member(function, "arguments"))
            if settings.capture_content and fragment is not None:
                tool.arguments = (tool.arguments if isinstance(tool.arguments, str) else "") + fragment
    if call.stream_tools:
        call.tool_calls = tuple(call.stream_tools.values())


def _append_delta(call: WrappedCall, chunk: object) -> None:
    fragment = _as_str(_member(_member(chunk, "delta"), "text", "content"))
    if fragment is None:
        for choice in _as_seq(_member(chunk, "choices")):
            fragment = _as_str(_member(_member(choice, "delta"), "content"))
            if fragment is not None:
                break
    if fragment is None:
        return
    content = call.content if call.content is not None else {}
    previous = content.get("streamedText")
    content["streamedText"] = (previous if isinstance(previous, str) else "") + fragment
    call.content = content


def _status_of(exc: BaseException) -> int | None:
    """The HTTP status the provider's exception carries, when it carries one inside the real range."""
    for name in _STATUS_ATTRIBUTES:
        found = _as_int(_attr(exc, name))
        if found is not None and _STATUS_MIN <= found <= _STATUS_MAX:
            return found
    return None


def _fail(call: WrappedCall, exc: BaseException, started_ns: int) -> None:
    call.duration_ms = (time.monotonic_ns() - started_ns) // _NS_PER_MS
    call.error = str(exc)
    call.error_type = type(exc).__qualname__
    if call.raw is None:
        return
    # A capture needs a status line and a body, and a call that raised may have neither. When the
    # provider's exception carries a status the capture records it with whatever body the exception
    # names; when it does not — a connection that never opened — there is nothing to capture, and the
    # summary already says exactly that (`error`, `errorType`), so the capture is dropped rather than
    # invented. `errors` is not on the capture's wire shape; it is in the body the service reads.
    status = _status_of(exc)
    if status is None:
        call.raw = None
        return
    call.raw.response_status = status
    call.raw.response_body = _encoded({"error": {"type": call.error_type, "message": call.error}})


# ── the streamed-response proxies ────────────────────────────────────────────────────────────────


def stream_recorder(call: WrappedCall, settings: HajerSettings, started_ns: int, *, read: Reader) -> StreamRecorder:
    """What a `RecordingStream` reports one streamed call's events to.

    The four callbacks are the whole seam between `_stream.py` and the record: a chunk crossed, a provider
    helper returned the whole answer, the stream raised, the call is over. `_stream.py` calls each one
    inside `try/except Exception`, so nothing here can reach the caller's stream.
    """

    def chunk(value: object) -> None:
        _observe_chunk(call, value, settings)

    def final(answer: object) -> None:
        read(call, answer, settings)

    def failed(error: BaseException) -> None:
        _fail(call, error, started_ns)

    def settled(exhausted: bool) -> None:
        call.duration_ms = (time.monotonic_ns() - started_ns) // _NS_PER_MS
        call.stream_complete = exhausted
        if not call.usage:
            call.note(STREAM_USAGE_UNOBSERVED)
        _bound_content(call, settings)
        _settled(call)

    return StreamRecorder(chunk=chunk, final=final, failed=failed, settled=settled)


# ── instrumentation ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Surface:
    """One leaf method to replace, and everything needed to record what goes through it."""

    provider: Provider
    api: Api
    holder: object
    attribute: str
    read: Reader
    #: True for a method that is *itself* the streaming one (`messages.stream`), as opposed to one that
    #: streams because the request said `stream=True`.
    streams: bool = False
    #: The client's endpoint as `host:port` (`_provider_host`), or `None` when it names none.
    host: str | None = None
    #: The objects from the client down to `holder`: where a raw-response proxy (`with_raw_response`,
    #: `with_streaming_response`) may already be cached around the unwrapped leaf.
    owners: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class _Candidate:
    """Where a surface lives on a client, as a path of attribute names and the leaf to replace."""

    provider: Provider
    api: Api
    path: tuple[str, ...]
    attribute: str
    read: Reader
    streams: bool = False


#: Every surface `wrap` knows how to find and read, by attribute. The `stream` rows are the providers'
#: separate streaming helpers — `messages.stream(...)` and `chat.completions.stream(...)` return a context
#: manager rather than a response, and a client that uses them would otherwise record nothing at all.
_CANDIDATES: Final[tuple[_Candidate, ...]] = (
    _Candidate("openai", "chat.completions", ("chat", "completions"), "parse", read_chat_completion),
    _Candidate("openai", "chat.completions", ("beta", "chat", "completions"), "parse", read_chat_completion),
    _Candidate("openai", "responses", ("responses",), "parse", read_responses_answer),
    _Candidate("openai", "chat.completions", ("chat", "completions"), "create", read_chat_completion),
    _Candidate("openai", "chat.completions", ("chat", "completions"), "stream", read_chat_completion, streams=True),
    _Candidate("openai", "responses", ("responses",), "create", read_responses_answer),
    _Candidate("openai", "responses", ("responses",), "stream", read_responses_answer, streams=True),
    # Metadata only: `read_embeddings` never reads a vector, and there is no raw wire for it.
    _Candidate("openai", "embeddings", ("embeddings",), "create", read_embeddings),
    _Candidate("anthropic", "messages", ("messages",), "create", read_message),
    _Candidate("anthropic", "messages", ("messages",), "stream", read_message, streams=True),
    # The same messages contract behind a beta header. It is a separate function on a separate
    # object, and a client that sends `betas` sends it here instead — measured on
    # `langchain_anthropic 1.4.6`, whose `_create` picks `beta.messages.create` whenever the payload
    # carries `betas`. Leaving it out would be a capture hole nobody could see.
    _Candidate("anthropic", "messages", ("beta", "messages"), "create", read_message),
    _Candidate("anthropic", "messages", ("beta", "messages"), "stream", read_message, streams=True),
)


def _direct_surfaces(client: object) -> list[_Surface]:
    """Every leaf method on the object itself that the SDK knows how to read, found by attribute."""
    found: list[_Surface] = []
    for candidate in _CANDIDATES:
        holder: object = client
        owners: list[object] = [client]
        for step in candidate.path:
            holder = _attr(holder, step)
            if holder is None:
                break
            owners.append(holder)
        if holder is None or not callable(_attr(holder, candidate.attribute)):
            continue
        found.append(
            _Surface(
                provider=candidate.provider,
                api=candidate.api,
                holder=holder,
                attribute=candidate.attribute,
                read=candidate.read,
                streams=candidate.streams,
                host=_provider_host(client),
                owners=tuple(owners),
            )
        )
    return found


#: The provider proxies that bind a leaf method when they are first built (`functools.cached_property`) and
#: then call it with a raw-response header: `client.with_raw_response.chat.completions.create`,
#: `completions.with_streaming_response.create`. One built before `wrap` holds the unwrapped leaf.
_RAW_PROXIES: Final[tuple[str, ...]] = ("with_raw_response", "with_streaming_response")


def _rebind_raw_proxies(owners: tuple[object, ...]) -> None:
    """Forget any raw-response proxy already cached on the way to a leaf, so the next use builds it around the
    instrumented leaf. The response it returns is then observed by the leaf's own replacement, and the
    application keeps the provider's own response object. Never raises."""
    for owner in owners:
        cached = getattr(owner, "__dict__", None)
        if not isinstance(cached, dict):
            continue
        for name in _RAW_PROXIES:
            try:
                cast(dict[str, object], cached).pop(name, None)
            except Exception:  # noqa: BLE001, S110 - a proxy that cannot be forgotten is a path not observed
                pass


def _surfaces(client: object) -> list[_Surface]:
    """The surfaces to instrument: the object's own, or the ones its inner provider clients carry.

    A framework chat model — `langchain_anthropic.ChatAnthropic` is the one this was measured against —
    is not a provider client and has no `messages.create` of its own. It *holds* one: the provider
    client it builds in its own constructor and calls from `_generate` / `_agenerate`. Instrumenting
    that inner client is instrumenting the only place the request actually leaves the process, which is
    what makes the record a record of the call rather than of the framework's idea of it. The two
    attribute names in `_INNER_CLIENT_ATTRIBUTES` are read by `getattr`, so no provider library and no
    framework is imported here either.

    Inner clients are looked at **only** when the object itself carries no surface, so a real
    `openai.OpenAI` or `anthropic.Anthropic` — both of which keep an httpx client under `_client` —
    behaves exactly as it did before.
    """
    direct = _direct_surfaces(client)
    if direct:
        return direct
    found: list[_Surface] = []
    for name in _INNER_CLIENT_ATTRIBUTES:
        inner = _attr(client, name)
        if inner is None or inner is client:
            continue
        found.extend(_direct_surfaces(inner))
    return found


def _no_raw_for_a_stream(call: WrappedCall) -> None:
    """A streamed answer has no raw capture, because this seam never sees the bytes it would be.

    The service reads a streamed capture as the server-sent-event stream it was, folding the frames back
    into one reading. What crosses `wrap` is the provider's *parsed* chunk objects — the SSE framing is
    gone below this seam — so a capture built here would be a reassembly wearing the bytes' clothes. The
    summary already carries what this seam did observe: `streamed`, `streamComplete`, `streamChunks`,
    the usage the final chunk named and, with content capture, the accumulated text.
    """
    call.raw = None


def _proxy(result: object, recorder: StreamRecorder) -> object:
    """The proxy that matches the stream the provider handed back, sync or async.

    Which one it is cannot be read off the *call*: `AsyncAnthropic().messages.stream(...)` is an ordinary
    method that returns an async context manager, and `create(stream=True)` awaited returns an async
    iterator. So the object is asked what it is, which is the same way `wrap` finds everything else.
    """
    if _attr(result, "__iter__") is not None and _attr(result, "__aiter__") is not None:
        return DualRecordingStream(result, recorder)
    if _attr(result, "__aiter__") is not None or _attr(result, "__aenter__") is not None:
        return AsyncRecordingStream(result, recorder)
    return RecordingStream(result, recorder)


#: Why every recording step below is wrapped: the seam runs inside somebody else's request path, so a bug
#: in Hajer's own reading of a provider object must cost an observation and never the call. The customer's
#: return value and the customer's exception cross these functions untouched, whatever goes wrong here.
def _open(
    provider: Provider,
    api: Api,
    kwargs: dict[str, object],
    settings: HajerSettings,
    *,
    streams: bool,
    provider_host: str | None = None,
) -> WrappedCall | None:
    """Begin the record, or `None` — in which case the call is forwarded and simply not recorded."""
    try:
        if settings.capture_call_site:
            install_task_frames()  # the tasks this call's framework creates next carry its frames, on any loop
        call = _begin(provider, api, kwargs, settings, streams=streams)
        call.provider_host = provider_host if settings.capture_call_site else None
        claim(kwargs.get("model"), call)
        return _record(call, settings)
    except Exception:  # noqa: BLE001 - beginning a record may never break the call it is about
        return None


def _note_failure(call: WrappedCall, exc: BaseException, started_ns: int) -> None:
    """Record that the call raised, then settle it. The exception continues to the caller unchanged."""
    try:
        _fail(call, exc, started_ns)
    except Exception:  # noqa: BLE001, S110 - recording a failure may never replace it
        pass
    _settled(call)


def _stream_proxy(
    result: object, call: WrappedCall, settings: HajerSettings, started_ns: int, read: Reader
) -> object | None:
    """The proxy over a streamed answer, or `None` when one could not be built."""
    try:
        _no_raw_for_a_stream(call)
        return _proxy(result, stream_recorder(call, settings, started_ns, read=read))
    except Exception:  # noqa: BLE001 - a proxy that cannot be built is a missing record, not a failed call
        return None


def _close(result: object, call: WrappedCall, settings: HajerSettings, started_ns: int, read: Reader) -> None:
    """Read the answer into the record and settle it. Reading may fail; the record still settles."""
    try:
        call.duration_ms = (time.monotonic_ns() - started_ns) // _NS_PER_MS
        read(call, result, settings)
    except Exception:  # noqa: BLE001, S110 - reading an answer may never replace it
        pass
    _settled(call)


def _settle_sync(
    result: object, call: WrappedCall, settings: HajerSettings, started_ns: int, *, read: Reader
) -> object:
    """One synchronous answer: the record closed, or a proxy that closes it when the stream ends."""
    if call.streamed:
        proxy = _stream_proxy(result, call, settings, started_ns, read)
        return result if proxy is None else proxy
    _close(result, call, settings, started_ns, read)
    return _viewed(result, call)


def _viewed(result: object, call: WrappedCall) -> object:
    """The answer, or a delegating view of it that records which of its fields the caller reads.

    Only when `HAJER_RECORD_REPLY_READS` opened a sink. A view that cannot be built is a missing
    observation and never a failed call, which is why the caller's own object is what comes back
    from the `except`.
    """
    sink = call.read_sink
    if sink is None:
        return result
    try:
        return recording(result, sink)
    except Exception:  # noqa: BLE001 - a view that cannot be built may never replace the answer
        return result


async def _settle_async(
    pending: Awaitable[object],
    call: WrappedCall,
    settings: HajerSettings,
    started_ns: int,
    *,
    read: Reader,
) -> object:
    """The same, for a coroutine: awaited here, so the record closes when the answer actually arrives."""
    token, opened = ACTIVE_CALL.set(True), OPEN_CALL.set(call)
    try:
        result = await pending
    except BaseException as exc:
        _note_failure(call, exc, started_ns)
        raise
    finally:
        OPEN_CALL.reset(opened)
        ACTIVE_CALL.reset(token)
    return _settle_result(result, call, settings, started_ns, read)


#: Said on a raw response closed before anything read its body.
RAW_RESPONSE_NOT_READ: Final[str] = (
    "RAW_RESPONSE_NOT_READ: the application closed the raw response without reading its body, so the answer "
    "was not observed."
)
#: Said on a raw response whose whole body was not a JSON document this SDK could read into the answer.
RAW_RESPONSE_UNREADABLE: Final[str] = (
    "RAW_RESPONSE_UNREADABLE: the provider answered 2xx with a body that is not a JSON answer (a proxy's page, "
    "plain text), so no output, usage or finish reason was read from it."
)
#: Said on a raw response whose body stopped before its end.
RAW_RESPONSE_INCOMPLETE: Final[str] = (
    "RAW_RESPONSE_INCOMPLETE: the application stopped reading the raw response before its end; the answer "
    "is what had arrived by then."
)


#: The event a streamed answer ends with, besides OpenAI chat's `data: [DONE]`: Anthropic's and the Responses API's.
_TERMINAL_EVENTS: Final[frozenset[str]] = frozenset(
    {"message_stop", "response.completed", "response.failed", "response.incomplete"}
)


@dataclass(slots=True)
class _RawBody:
    """One raw response's body as the application reads it: server-sent events folded in as they arrive, a JSON
    document buffered up to `HAJER_BODY_MAX_BYTES`, and the record settled once when the body ends."""

    call: WrappedCall
    settings: HajerSettings
    started_ns: int
    read: Reader
    buffer: bytearray = field(default_factory=bytearray)
    line: bytearray = field(default_factory=bytearray)
    received: int = 0
    overflowed: bool = False
    done: bool = False

    def chunk(self, piece: bytes) -> None:
        if self.done:
            return
        self.received += len(piece)
        if self.call.streamed:
            self.line.extend(piece)
            *lines, rest = bytes(self.line).split(b"\n")
            self.line = bytearray(rest)
            for line in lines:
                if self._event(line):
                    # The stream's own terminal event: a provider client stops reading here and closes the
                    # response rather than draining it, so the stream is complete now.
                    self.end(True, None)
                    return
            return
        if len(self.buffer) + len(piece) > self.settings.body_max_bytes:
            self.overflowed = True
            return
        self.buffer.extend(piece)

    def _event(self, line: bytes) -> bool:
        """Fold one server-sent-event line into the record; True when it is the stream's terminal event."""
        data = line.strip()
        if not data.startswith(b"data:"):
            return False
        payload = data[len(b"data:") :].strip()
        if payload == b"[DONE]":
            return True
        try:
            chunk: JsonValue = json.loads(payload)
        except ValueError:
            return False
        _observe_chunk(self.call, chunk, self.settings)
        return _member(chunk, "type") in _TERMINAL_EVENTS

    def _answer(self, body: bytes) -> None:
        """Read a whole body into the answer. A body that is not a JSON document the reader understands (a proxy's
        HTML page, a plain-text reply) is recorded as such and never lost."""
        try:
            self.read(self.call, json.loads(body), self.settings)
        except Exception:  # noqa: BLE001 - an unreadable answer is a limitation of the record, not its end
            self.call.raw = None
            self.call.note(RAW_RESPONSE_UNREADABLE)

    def end(self, complete: bool, error: BaseException | None) -> None:
        if self.done:
            return
        self.done = True
        try:
            self._close(complete, error)
        except Exception:  # noqa: BLE001, S110 - reading the answer may fail; the record still settles
            pass
        _settled(self.call)

    def _close(self, complete: bool, error: BaseException | None) -> None:
        call = self.call
        call.duration_ms = (time.monotonic_ns() - self.started_ns) // _NS_PER_MS
        if error is not None:
            _fail(call, error, self.started_ns)
        if call.streamed:
            self._event(bytes(self.line))
            _no_raw_for_a_stream(call)
            call.stream_complete = complete
            if not call.usage:
                call.note(STREAM_USAGE_UNOBSERVED)
        elif complete and self.buffer and not self.overflowed:
            self._answer(bytes(self.buffer))
        else:
            call.raw = None  # no whole body, so no provider document to read into a receipt
        if not self.received:
            call.note(RAW_RESPONSE_NOT_READ)
        elif not complete or self.overflowed:
            call.note(RAW_RESPONSE_INCOMPLETE)
        _bound_content(call, self.settings)


def _observe_raw(http: object, call: WrappedCall, settings: HajerSettings, started_ns: int, read: Reader) -> None:
    """Settle `call` from the raw response's body when the application reads it, and never before."""
    body = _RawBody(call, settings, started_ns, read)
    observe_body(http, body.chunk, body.end)


def _settle_result(result: object, call: WrappedCall, settings: HajerSettings, started_ns: int, read: Reader) -> object:
    """A raw response (the provider's `LegacyAPIResponse` / `APIResponse`) is handed back as it is and recorded
    on the first read of its body, by whichever accessor — `parse()`, `read()`, `json()`, `iter_lines()`,
    `http_response` — exactly once. Anything else is settled now, or proxied if it streams."""
    http = http_response(result)
    if http is not None:
        _observe_raw(http, call, settings, started_ns, read)
        return result
    return _settle_sync(result, call, settings, started_ns, read=read)


def instrument_function(
    original: _Callable,
    *,
    provider: Provider,
    api: Api,
    settings: HajerSettings,
    read: Reader,
    streams: bool = False,
    provider_host: str | None = None,
) -> _Callable | None:
    """A replacement for one synchronous callable that records the call it forwards.

    `None` when there is nothing to do: the callable is already instrumented, and instrumenting it twice
    would record the same call twice. The replacement returns whatever the original returned — including a
    coroutine, which the caller awaits and which is why `inspect.isawaitable` is checked here rather than
    assumed away.
    """
    if _attr(original, _MARK) is True:
        return None

    @functools.wraps(original)
    def replacement(*args: object, **kwargs: object) -> object:
        if ACTIVE_CALL.get():
            return original(*args, **kwargs)
        call = _open(provider, api, kwargs, settings, streams=streams, provider_host=provider_host)
        if call is None:  # the record could not be opened; the call is the caller's and goes through
            return original(*args, **kwargs)
        started_ns = time.monotonic_ns()
        token, opened = ACTIVE_CALL.set(True), OPEN_CALL.set(call)
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:
            _note_failure(call, exc, started_ns)
            raise
        finally:
            OPEN_CALL.reset(opened)
            ACTIVE_CALL.reset(token)
        if inspect.isawaitable(result):
            return _settle_async(cast(Awaitable[object], result), call, settings, started_ns, read=read)
        return _settle_result(result, call, settings, started_ns, read)

    setattr(replacement, _MARK, True)
    return replacement


def instrument_coroutine(
    original: _Callable,
    *,
    provider: Provider,
    api: Api,
    settings: HajerSettings,
    read: Reader,
    streams: bool = False,
    provider_host: str | None = None,
) -> _Callable | None:
    """The same for a coroutine function — `litellm.acompletion`, `aio.models.generate_content`.

    An `async def` replacement rather than a sync one that returns the coroutine, so
    `inspect.iscoroutinefunction` still answers yes: a framework that introspects the function it is about
    to call (and several do, to decide whether to await it or to hand it to a thread) must see what it saw
    before Hajer was in the process.
    """
    if _attr(original, _MARK) is True:
        return None

    @functools.wraps(original)
    async def replacement(*args: object, **kwargs: object) -> object:
        if ACTIVE_CALL.get():
            return await cast(Awaitable[object], original(*args, **kwargs))
        call = _open(provider, api, kwargs, settings, streams=streams, provider_host=provider_host)
        if call is None:  # the record could not be opened; the call is the caller's and goes through
            return await cast(Awaitable[object], original(*args, **kwargs))
        started_ns = time.monotonic_ns()
        try:
            pending = original(*args, **kwargs)
        except BaseException as exc:
            _note_failure(call, exc, started_ns)
            raise
        if not inspect.isawaitable(pending):
            return _settle_result(pending, call, settings, started_ns, read)
        return await _settle_async(cast(Awaitable[object], pending), call, settings, started_ns, read=read)

    setattr(replacement, _MARK, True)
    return replacement


def _instrument(
    surface: _Surface,
    settings: HajerSettings,
    on_patch: Callable[[object, str, object, object], None] | None = None,
) -> None:
    original = _attr(surface.holder, surface.attribute)
    if not callable(original):
        return
    build = instrument_coroutine if inspect.iscoroutinefunction(original) else instrument_function
    replacement = build(
        cast(_Callable, original),
        provider=surface.provider,
        api=surface.api,
        settings=settings,
        read=surface.read,
        streams=surface.streams,
        provider_host=surface.host,
    )
    if replacement is None:
        return
    if on_patch is not None:
        on_patch(surface.holder, surface.attribute, original, replacement)
    setattr(surface.holder, surface.attribute, replacement)


_ClientT = TypeVar("_ClientT")

#: Libraries `wrap` and attach mode must instrument but whose shape does not belong in the candidate
#: table: a router whose entry point is a module function, a client whose methods are not called `create`.
#: A target module registers itself here at import time rather than being imported from below, for the
#: same reason attach mode installs a hook instead of an import — this module imports nothing of the SDK
#: above `_settings`, and the seam that runs inside every provider call stays free of everything else.
_INSTRUMENTERS: Final[list[Callable[[object, HajerSettings], int]]] = []


def add_instrumenter(instrumenter: Callable[[object, HajerSettings], int]) -> None:
    """Register one library's own instrumenter. It is handed every object `wrap`/attach is given."""
    if instrumenter not in _INSTRUMENTERS:
        _INSTRUMENTERS.append(instrumenter)


def instrument(
    client: object,
    settings: HajerSettings,
    *,
    on_patch: Callable[[object, str, object, object], None] | None = None,
) -> int:
    """Instrument every surface `client` carries, and say how many there were. Never refuses.

    `wrap` is the customer's call and refuses an object it cannot read, because a line the developer
    wrote that records nothing is worse than an error. Attach mode is handed *every* client the process
    constructs and most of them carry no surface at all, so it needs the count rather than an exception
    — which is the whole difference between these two functions.
    """
    surfaces = _surfaces(client)
    for surface in surfaces:
        _instrument(surface, settings, on_patch)
    for surface in surfaces:
        _rebind_raw_proxies(surface.owners)
    if surfaces and settings.capture_call_site:
        # A framework's `ainvoke` runs the provider call in a child task; the application frame that
        # awaited it is on the stack only where that task is created (`_frame_context`).
        install_task_frames()
    if surfaces:
        run_installers(settings)
    count = len(surfaces)
    for instrumenter in _INSTRUMENTERS:
        try:
            count += instrumenter(client, settings)
        except Exception:  # noqa: BLE001, S110 - one library's instrumenter may never break another's
            pass
    return count


def wrap(client: _ClientT, *, settings: HajerSettings | None = None) -> _ClientT:
    """Instrument `client` in place and return it — the same object, behaving the same way.

    Recognises `openai.OpenAI` / `AsyncOpenAI` (`chat.completions.create`, `responses.create`) and
    `anthropic.Anthropic` / `AsyncAnthropic` (`messages.create`), sync and async, streamed and not,
    by the attributes they carry rather than by importing either library. A framework chat model that
    holds one of those clients — `langchain_openai.ChatOpenAI`, `langchain_anthropic.ChatAnthropic` — is
    instrumented at the inner client, so the record is of the request that left the process, and the
    application frame that awaited the framework is carried into the child tasks it fans out to. Calling it twice on the same client
    is a no-op. `settings` is a test seam; production reads the environment.

    A `unittest.mock` object (`NonCallableMock` and every subclass: `Mock`, `MagicMock`, `AsyncMock`, the objects
    `mock.patch("openai.OpenAI")` hands out) is returned unchanged and records nothing: it is a test's own double, and
    instrumenting it would replace the mocked methods the test asserts on (`assert_called_once`). The install PR
    wraps a client where the application constructs it, so a test that patches the provider class reaches `wrap`
    with that mock.
    """
    if isinstance(client, NonCallableMock):
        return client
    resolved = settings if settings is not None else HajerSettings.from_env()
    if instrument(client, resolved) == 0:
        raise UnsupportedClientError(client)
    return client

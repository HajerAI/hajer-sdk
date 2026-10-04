"""One recorded model call as the span it becomes: its name, its attributes, its status and its two instants.

`wrap` records; this module translates. A `WrappedCall` is the SDK's own reading of a provider call, and a
`gen_ai` span is the GenAI semantic conventions' reading of one; `model_span` is the whole mapping between them,
written once and OpenTelemetry-free, so the OTel half (`_telemetry_otel`) only has to start a span with what it is
handed and end it. `tests/test_model_spans.py` holds the mapping to the conventions' names.

Content — the messages, the answer, the system instructions — is the one part with rules of its own: it is carried
only with `HAJER_CAPTURE_CONTENT`, it passes the client-side redaction first, and the three attributes together fit
inside `HAJER_WRAPPED_CALL_MAX_BYTES` (the input first, then the output, then the instructions; what does not fit is
clipped in the middle the way `wrap` clips, and a document whose shape alone does not fit is left out and said so in
`hajer.limitations`). An HTTP exchange `HAJER_CAPTURE_HTTP` recorded becomes an HTTP client span instead: the method,
the path template, the host and the status, and never a body.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, cast

from hajer import _genai_messages, _semconv
from hajer._json import JsonObject, JsonValue
from hajer._redact import ClientRedactionPolicy, redact_document
from hajer._settings import HajerSettings
from hajer._wrap import CONTENT_TRUNCATED, WrappedCall, fitted

#: What one span attribute may hold: the OpenTelemetry scalar types and a sequence of strings.
AttributeValue = str | bool | int | float | tuple[str, ...]
Attributes = dict[str, AttributeValue]

#: The record's `provider` for an HTTP exchange recorded at the transport (`HAJER_CAPTURE_HTTP`).
HTTP_PROVIDER: Final[str] = "http"
#: `gen_ai.operation.name` by the record's `api`: the conventions' well-known values. A router's completion is a chat.
_OPERATIONS: Final[dict[str, str]] = {
    "chat.completions": _semconv.OPERATION_CHAT,
    "responses": _semconv.OPERATION_CHAT,
    "messages": _semconv.OPERATION_CHAT,
    "litellm.completion": _semconv.OPERATION_CHAT,
    "embeddings": _semconv.OPERATION_EMBEDDINGS,
    "genai.generate_content": _semconv.OPERATION_GENERATE_CONTENT,
    "genai.generate_content_stream": _semconv.OPERATION_GENERATE_CONTENT,
}
#: `gen_ai.provider.name` by the record's `provider`, where the conventions spell it differently.
_PROVIDERS: Final[dict[str, str]] = {"google": "gcp.gen_ai"}
#: Request settings that have a conventions name, by the key the caller passed. First match per attribute wins,
#: which is what lets the three spellings of "max tokens" land on one key.
_REQUEST_SETTINGS: Final[tuple[tuple[str, str], ...]] = (
    ("temperature", _semconv.REQUEST_TEMPERATURE),
    ("top_p", _semconv.REQUEST_TOP_P),
    ("top_k", _semconv.REQUEST_TOP_K),
    ("max_tokens", _semconv.REQUEST_MAX_TOKENS),
    ("max_output_tokens", _semconv.REQUEST_MAX_TOKENS),
    ("max_completion_tokens", _semconv.REQUEST_MAX_TOKENS),
    ("seed", _semconv.REQUEST_SEED),
    ("frequency_penalty", _semconv.REQUEST_FREQUENCY_PENALTY),
    ("presence_penalty", _semconv.REQUEST_PRESENCE_PENALTY),
    ("n", _semconv.REQUEST_CHOICE_COUNT),
)
_STOP_SETTINGS: Final[tuple[str, ...]] = ("stop", "stop_sequences")
#: The usage counters, from the record's one vocabulary to the attribute each lands on.
_USAGE: Final[tuple[tuple[str, str], ...]] = (
    ("input_tokens", _semconv.USAGE_INPUT_TOKENS),
    ("output_tokens", _semconv.USAGE_OUTPUT_TOKENS),
    ("total_tokens", _semconv.USAGE_TOTAL_TOKENS),
    ("cache_read_input_tokens", _semconv.USAGE_CACHE_READ_INPUT_TOKENS),
    ("cache_write_input_tokens", _semconv.USAGE_CACHE_WRITE_INPUT_TOKENS),
)
_NS_PER_MS: Final[int] = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelSpan:
    """Everything the OTel half needs to emit one span: nothing in here names an OpenTelemetry type."""

    name: str
    attributes: Attributes
    #: The qualified exception class when the call raised, else None: what `error.type` and the status say.
    error_type: str | None
    start_ns: int
    end_ns: int


def model_span(call: WrappedCall, settings: HajerSettings, policy: ClientRedactionPolicy) -> ModelSpan:
    """The span for one settled record, under the settings and the redaction policy in force."""
    end_ns = call.started_ns + max(call.duration_ms, 0) * _NS_PER_MS
    if call.provider == HTTP_PROVIDER:
        return ModelSpan(
            name=call.api,
            attributes=_http_attributes(call),
            error_type=call.error_type,
            start_ns=call.started_ns,
            end_ns=end_ns,
        )
    attributes = _model_attributes(call)
    if settings.capture_content:
        _content(call, settings, policy, attributes)
    if call.limitations:
        attributes[_semconv.LIMITATIONS] = call.limitations
    operation = _OPERATIONS.get(call.api, call.api)
    name = f"{operation} {call.model}" if call.model else operation
    return ModelSpan(
        name=name, attributes=attributes, error_type=call.error_type, start_ns=call.started_ns, end_ns=end_ns
    )


def _model_attributes(call: WrappedCall) -> Attributes:
    attributes: Attributes = {
        _semconv.OPERATION_NAME: _OPERATIONS.get(call.api, call.api),
        _semconv.PROVIDER_NAME: _PROVIDERS.get(call.provider, call.provider),
        _semconv.API: call.api,
    }
    requested = call.request_settings.get("model")
    if isinstance(requested, str):
        attributes[_semconv.REQUEST_MODEL] = requested
    elif call.model is not None:
        attributes[_semconv.REQUEST_MODEL] = call.model
    if call.model is not None:
        attributes[_semconv.RESPONSE_MODEL] = call.model
    for key, attribute in _REQUEST_SETTINGS:
        value = call.request_settings.get(key)
        if attribute not in attributes and isinstance(value, (int, float)) and not isinstance(value, bool):
            attributes[attribute] = value
    stops = _stop_sequences(call)
    if stops:
        attributes[_semconv.REQUEST_STOP_SEQUENCES] = stops
    if call.declared_tools:
        attributes[_semconv.REQUEST_TOOL_NAMES] = call.declared_tools
    if call.response_id is not None:
        attributes[_semconv.RESPONSE_ID] = call.response_id
    if call.finish_reason is not None:
        attributes[_semconv.RESPONSE_FINISH_REASONS] = (call.finish_reason,)
    for key, attribute in _USAGE:
        count = call.usage.get(key)
        if count is not None:
            attributes[attribute] = count
    if call.embedding is not None:
        vectors, dimensions = call.embedding
        attributes[_semconv.EMBEDDING_VECTORS] = vectors
        if dimensions is not None:
            attributes[_semconv.EMBEDDINGS_DIMENSION_COUNT] = dimensions
    _server(call, attributes)
    _code(call, attributes)
    if call.streamed:
        attributes[_semconv.STREAM] = True
        if call.stream_complete is not None:
            attributes[_semconv.STREAM_COMPLETE] = call.stream_complete
        attributes[_semconv.STREAM_CHUNKS] = call.stream_chunks
    if call.error_type is not None:
        attributes[_semconv.ERROR_TYPE] = call.error_type
    if call.error_status is not None:
        attributes[_semconv.HTTP_RESPONSE_STATUS_CODE] = call.error_status
    return attributes


def _stop_sequences(call: WrappedCall) -> tuple[str, ...]:
    for key in _STOP_SETTINGS:
        value = call.request_settings.get(key)
        if isinstance(value, str):
            return (value,)
        if isinstance(value, list):
            return tuple(item for item in value if isinstance(item, str))
    return ()


def _server(call: WrappedCall, attributes: Attributes) -> None:
    """`host:port` as the record spells it → the two conventions attributes. A bracketed IPv6 host keeps its brackets."""
    if call.provider_host is None:
        return
    host, separator, port = call.provider_host.rpartition(":")
    if separator and port.isdigit():
        attributes[_semconv.SERVER_ADDRESS] = host
        attributes[_semconv.SERVER_PORT] = int(port)
    else:
        attributes[_semconv.SERVER_ADDRESS] = call.provider_host


def _code(call: WrappedCall, attributes: Attributes) -> None:
    """The innermost application frame: where the call was made. A location, never a value, like the record's."""
    if not call.caller_frames:
        return
    frame = call.caller_frames[0]
    attributes[_semconv.CODE_FUNCTION_NAME] = f"{frame.module}.{frame.qualname}" if frame.module else frame.qualname
    attributes[_semconv.CODE_FILE_PATH] = frame.file
    attributes[_semconv.CODE_LINE_NUMBER] = frame.line


def _http_attributes(call: WrappedCall) -> Attributes:
    attributes: Attributes = {_semconv.HTTP_REQUEST_METHOD: call.api}
    url = call.request_settings.get("url")
    if isinstance(url, str):
        attributes[_semconv.URL_TEMPLATE] = url
    status = call.request_settings.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        attributes[_semconv.HTTP_RESPONSE_STATUS_CODE] = status
    _server(call, attributes)
    _code(call, attributes)
    if call.error_type is not None:
        attributes[_semconv.ERROR_TYPE] = call.error_type
    if call.limitations:
        attributes[_semconv.LIMITATIONS] = call.limitations
    return attributes


# ── content ──


def _document(messages: list[JsonObject]) -> JsonValue:
    """A list of messages as the JSON value the redaction walk and the fit take (a list is invariant to the checker)."""
    return cast(JsonValue, messages)


def _content(call: WrappedCall, settings: HajerSettings, policy: ClientRedactionPolicy, attributes: Attributes) -> None:
    """The three content attributes, redacted, then fitted together inside the content bound, input first."""
    content = call.content or {}
    documents: list[tuple[str, JsonValue]] = []
    if call.api != "embeddings" and "messages" in content:
        documents.append(
            (_semconv.INPUT_MESSAGES, _document(_genai_messages.input_messages(call.api, content.get("messages"))))
        )
    if call.api != "embeddings":
        output = _genai_messages.output_messages(
            call.api,
            content.get("output"),
            content.get("streamedText"),
            [(tool.id, tool.name, tool.arguments) for tool in call.tool_calls],
            call.finish_reason,
        )
        if output:
            documents.append((_semconv.OUTPUT_MESSAGES, _document(output)))
    instructions = _genai_messages.system_instructions(content.get("system"))
    if instructions is not None:
        documents.append((_semconv.SYSTEM_INSTRUCTIONS, _document(instructions)))
    budget = settings.wrapped_call_max_bytes
    clipped = False
    for attribute, document in documents:
        redacted = redact_document(document, policy=policy)[0] if settings.redact_client else document
        fit = fitted(redacted, budget)
        if fit is None:
            clipped = True
            continue
        encoded = json.dumps(fit[0], ensure_ascii=False, separators=(",", ":"), default=str)
        clipped = clipped or fit[1]
        attributes[attribute] = encoded
        budget = max(budget - len(encoded.encode("utf-8")), 0)
    if clipped:
        call.note(CONTENT_TRUNCATED)

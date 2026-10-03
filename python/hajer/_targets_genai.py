"""google-genai: four methods on a constructed client, one of which streams by being itself.

`google.genai.Client` keeps its calls on sub-objects rather than on itself:
`client.models.generate_content(...)`, its async twin `client.aio.models.generate_content(...)`, and
`generate_content_stream` beside each — a **separate method** rather than a `stream=True` flag, which is
why it is declared here as a surface that always streams. Without that, a client that streams would be
recorded as one that answered in one piece and never named its tokens.

Attach mode patches `Client.__init__` (the generic constructor patch in `_attach.py`), and the instance's
four methods are replaced here, on the instance: the sub-objects are built in that constructor, so by the
time it returns they exist and this is the only moment attach mode is guaranteed to see them. A client
built **before** `attach()` is not instrumented — `attach()` cannot reach an object it was never handed —
and `wrap(client)` is the line for that case; `detach()` documents the mirror of it.

The answer's shape is read in `_wrap.read_genai_answer`, with the model under `model_version` and the
tokens under `usage_metadata`. Nothing about `google.genai` is imported anywhere in this package.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from hajer import _wrap
from hajer._json import JsonValue
from hajer._settings import HajerSettings
from hajer._targets import Support, Target
from hajer._wrap import instrument_coroutine, instrument_function, read_genai_answer

#: Who answered. Google's own name, not the library's: the receipt is about the call.
PROVIDER: Final[str] = "google"

#: Where the four calls live on a constructed client, and whether the method is the streaming one.
#: `(path, attribute, api, streams)`, walked by `getattr` exactly as `wrap` walks everything else.
_SURFACES: Final[tuple[tuple[tuple[str, ...], str, str, bool], ...]] = (
    (("models",), "generate_content", "genai.generate_content", False),
    (("models",), "generate_content_stream", "genai.generate_content_stream", True),
    (("aio", "models"), "generate_content", "genai.generate_content", False),
    (("aio", "models"), "generate_content_stream", "genai.generate_content_stream", True),
)

TARGET: Final[Target] = Target(
    module="google.genai",
    classes=("Client",),
    support=Support(
        label="google-genai",
        sync=True,
        asynchronous=True,
        streaming=True,
        usage=True,
        content=True,
        caveat=(
            "`generate_content_stream` is a separate method and is recorded as a stream; usage arrives "
            "in the final chunk's `usage_metadata`. A client constructed before `attach()` is reached "
            "with `hajer.wrap(client)`."
        ),
    ),
)


def instrument(candidate: object, settings: HajerSettings) -> int:
    """Replace the four methods on one genai client. Returns how many were replaced.

    Registered with `_wrap.add_instrumenter`, so `wrap(client)` and the constructor patch both arrive
    here. The client is recognised by what it carries — a `models` object with a callable
    `generate_content` — which is the same rule the rest of `wrap` follows and the reason no provider
    library is imported to identify one.
    """
    if not _is_genai_client(candidate):
        return 0
    patched = 0
    for path, attribute, api, streams in _SURFACES:
        holder = _walk(candidate, path)
        original = getattr(holder, attribute, None) if holder is not None else None
        if not callable(original):
            continue
        build = instrument_coroutine if inspect.iscoroutinefunction(original) else instrument_function
        replacement = build(
            original, provider=PROVIDER, api=api, settings=settings, read=read_genai_answer, streams=streams
        )
        if replacement is None:  # already instrumented; patching twice would record the call twice
            continue
        try:
            setattr(holder, attribute, replacement)
        except (AttributeError, TypeError):  # pragma: no cover - a sub-client that refuses assignment
            continue
        patched += 1
    return patched


def _walk(client: object, path: tuple[str, ...]) -> object | None:
    holder: object = client
    for step in path:
        holder = getattr(holder, step, None)
        if holder is None:
            return None
    return holder


def _is_genai_client(candidate: object) -> bool:
    """A genai client is the object that carries `models.generate_content`. Nothing else does."""
    models = getattr(candidate, "models", None)
    return models is not None and callable(getattr(models, "generate_content", None))


_wrap.add_instrumenter(instrument)


#: The semconv `gen_ai.operation.name` values that are a model call.
MODEL_OPERATIONS: Final[frozenset[str]] = frozenset({"chat", "text_completion", "embeddings", "generate_content"})


def observation_from_gen_ai(
    attributes: Mapping[str, object], *, start_ns: int, end_ns: int, capture_content: bool = False
) -> _wrap.WrappedCall | None:
    """The semconv 1.37+ mapping, shared as a contract with server ingest. No vendor dialects.

    Attributes must identify a provider and a **model** operation (`MODEL_OPERATIONS`): a framework's
    `invoke_agent`, `execute_task`, `execute_tool` or `create_agent` span is not a call (Traceloop
    0.60 puts `gen_ai` attributes on every runnable, task, tool and graph span). Unknown counters
    stay absent. JSON message attributes carry parts with tool_call/tool_call_response and their provider-issued
    IDs.
    """
    provider = attributes.get("gen_ai.provider.name") or attributes.get("gen_ai.system")
    operation = attributes.get("gen_ai.operation.name")
    if not isinstance(provider, str) or not isinstance(operation, str) or operation not in MODEL_OPERATIONS:
        return None
    api = {
        ("openai", "chat"): "chat.completions",
        ("anthropic", "chat"): "messages",
        ("openai", "responses"): "responses",
    }.get((provider, operation), operation)
    call = _wrap.WrappedCall(
        provider=provider,
        api=api,
        started_at=datetime.fromtimestamp(start_ns / 1_000_000_000, UTC).isoformat(),
        duration_ms=max(0, (end_ns - start_ns) // 1_000_000),
    )
    model = attributes.get("gen_ai.response.model") or attributes.get("gen_ai.request.model")
    call.model = model if isinstance(model, str) else None
    response_id = attributes.get("gen_ai.response.id")
    call.response_id = response_id if isinstance(response_id, str) else None
    error_type = attributes.get("error.type")
    call.error_type = error_type if isinstance(error_type, str) else None
    for key in ("input_tokens", "output_tokens"):
        value = attributes.get(f"gen_ai.usage.{key}")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            call.usage[key] = value
    content: dict[str, JsonValue] = {}
    tools: list[_wrap.ToolCall] = []
    results: list[_wrap.ToolResult] = []
    for direction in ("input", "output"):
        messages = _span_messages(attributes.get(f"gen_ai.{direction}.messages"))
        if capture_content:
            content[f"{direction}Messages"] = messages
        for message in messages:
            if not isinstance(message, dict):
                continue
            parts = message.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                identifier, name = part.get("id"), part.get("name")
                tool_id = identifier if isinstance(identifier, str) else None
                if part.get("type") == "tool_call":
                    tools.append(
                        _wrap.ToolCall(
                            id=tool_id,
                            name=name if isinstance(name, str) else None,
                            arguments=part.get("arguments") if capture_content else None,
                        )
                    )
                if part.get("type") == "tool_call_response":
                    result = part.get("result") if "result" in part else part.get("response")
                    results.append(_wrap.ToolResult(tool_use_id=tool_id, content=result if capture_content else None))
    call.tool_calls, call.tool_results = tuple(tools), tuple(results)
    call.content = content if capture_content else None
    call.note("Observed from OpenTelemetry gen_ai spans; provider bytes and application outcomes are unverified.")
    return call


def _span_messages(value: object) -> list[JsonValue]:
    if not isinstance(value, (str, list)):
        return []
    try:
        parsed: JsonValue = json.loads(value if isinstance(value, str) else json.dumps(value, allow_nan=False))
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []

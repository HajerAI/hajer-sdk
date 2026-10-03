"""The (output, evidence) a replay `verify` sends: the reply's check subject, and what the application made of it.

The generated verifier's evidence contract (`source-response-json-v1`) reads the model's own reply, not the
application's return value: `output` is the reply's one text block decoded as strict JSON (no fence stripping;
only an object is a subject, else null), and the evidence says how it decoded (`provider_decode`), what the
application returned (`application_result`, kept apart and never the check subject), which layer the reply was
read from (`capture_layer`) and — only when it decoded to an object — `subject_digest`. Without these the
assessment is incomplete and a replay could never be read as a reproduction. The evidence goes through the
service's ordinary ingest, so its redaction runs over it like any production capture.

The reply read is the attempt's last provider answer as the child's transport saw it (Anthropic Messages or an
OpenAI chat completion); a reply cut at `max_tokens` is TRUNCATED, never a shorter subject.

The attempt's tool calls (the model's structured form: `tool_use` blocks, OpenAI `tool_calls`) join the subject as
`{"tool_calls": {<name>: {"arguments": <decoded arguments>}}}`, each taken from the reply that carries it — not the
attempt's last reply, so a tool loop ending in a text answer keeps its call — beside the last reply's text subject
(DECODED_TOOL_CALLS when there is no text subject). A rule on the structured form is checked at
`output.tool_calls.<name>.arguments.<field>`. A name called more than once in the attempt, arguments that
are not an object and a reply cut at its token limit give that name no entry; a check on it is then unknown, never
absent. The shared vectors (`contract/check-evaluation-vectors.json`, `toolCallSubjects`) pin the projection.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Final, Literal, TypeAlias, cast

from pydantic import JsonValue

ProviderDecode: TypeAlias = Literal[
    "DECODED",
    "DECODED_TOOL_CALLS",
    "NOT_JSON",
    "NOT_OBJECT",
    "DUPLICATE_KEYS",
    "NON_FINITE",
    "NON_TEXT_CONTENT",
    "TRUNCATED",
    "NO_CAPTURE",
]
TOOL_CALLS: Final = "tool_calls"
CAPTURE_LAYER = "HTTP_TRANSPORT_DECODED_BODY"


class _DecodeRefusedError(ValueError):
    def __init__(self, decode: ProviderDecode) -> None:
        super().__init__(decode)
        self.decode: ProviderDecode = decode


def _pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise _DecodeRefusedError("DUPLICATE_KEYS")
    return dict(pairs)


def _constant(_: str) -> JsonValue:
    raise _DecodeRefusedError("NON_FINITE")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _DecodeRefusedError("NON_FINITE")
    return value


def _strict(text: str) -> tuple[ProviderDecode, dict[str, JsonValue] | None]:
    try:
        decoded = cast(
            JsonValue, json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_finite)
        )
    except _DecodeRefusedError as refused:
        return refused.decode, None
    except ValueError:
        return "NOT_JSON", None
    return ("DECODED", decoded) if isinstance(decoded, dict) else ("NOT_OBJECT", None)


def _text(body: bytes) -> tuple[ProviderDecode | None, str | None]:
    try:
        document = cast(object, json.loads(body))
    except ValueError:
        return "NO_CAPTURE", None
    if not isinstance(document, dict):
        return "NO_CAPTURE", None
    reply = cast(dict[str, object], document)
    choices = reply.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = cast(dict[str, object], choices[0])
        message = choice.get("message")
        content = cast(dict[str, object], message).get("content") if isinstance(message, dict) else None
        cut = choice.get("finish_reason") == "length"
        return ("TRUNCATED" if cut else None), content if isinstance(content, str) else None
    blocks = reply.get("content")
    texts = [
        cast(dict[str, object], block).get("text")
        for block in (cast(list[object], blocks) if isinstance(blocks, list) else [])
        if isinstance(block, dict) and cast(dict[str, object], block).get("type") == "text"
    ]
    if not isinstance(blocks, list) or len(cast(list[object], blocks)) != 1 or len(texts) != 1:
        return "NON_TEXT_CONTENT", None
    text = texts[0]
    return ("TRUNCATED" if reply.get("stop_reason") == "max_tokens" else None), text if isinstance(text, str) else None


def _calls(reply: dict[str, object]) -> list[tuple[str, object]]:
    """(name, arguments) of every tool call the reply makes: Anthropic `tool_use` blocks or OpenAI chat `tool_calls`
    (arguments a JSON string there, decoded strictly)."""
    blocks = reply.get("content")
    found: list[tuple[str, object]] = [
        (str(cast(dict[str, object], block).get("name")), cast(dict[str, object], block).get("input"))
        for block in (cast(list[object], blocks) if isinstance(blocks, list) else [])
        if isinstance(block, dict) and cast(dict[str, object], block).get("type") == "tool_use"
    ]
    choices = reply.get("choices")
    first = cast(list[object], choices)[0] if isinstance(choices, list) and choices else None
    message = cast(dict[str, object], first).get("message") if isinstance(first, dict) else None
    calls = cast(dict[str, object], message).get("tool_calls") if isinstance(message, dict) else None
    for call in cast(list[object], calls) if isinstance(calls, list) else []:
        function = cast(dict[str, object], call).get("function") if isinstance(call, dict) else None
        if isinstance(function, dict):
            named = cast(dict[str, object], function)
            found.append((str(named.get("name")), named.get("arguments")))
    return found


def _document(body: bytes) -> dict[str, object] | None:
    try:
        document = cast(object, json.loads(body))
    except ValueError:
        return None
    return cast(dict[str, object], document) if isinstance(document, dict) else None


def _cut(reply: dict[str, object]) -> bool:
    choices = reply.get("choices")
    first = cast(list[object], choices)[0] if isinstance(choices, list) and choices else None
    return reply.get("stop_reason") == "max_tokens" or (
        isinstance(first, dict) and cast(dict[str, object], first).get("finish_reason") == "length"
    )


def _arguments(arguments: object) -> dict[str, JsonValue] | None:
    if isinstance(arguments, str):
        return _strict(arguments)[1]
    return cast(dict[str, JsonValue], arguments) if isinstance(arguments, dict) else None


def tool_calls(bodies: Sequence[bytes]) -> dict[str, JsonValue]:
    """Every tool call of the attempt, keyed by name, from the reply that carries it: a name
    called more than once, arguments that are not an object, and a reply cut at its token limit give no entry."""
    seen: dict[str, int] = {}
    projected: dict[str, JsonValue] = {}
    for body in bodies:
        reply = _document(body)
        if reply is None:
            continue
        for name, raw in _calls(reply):
            seen[name] = seen.get(name, 0) + 1
            arguments = None if _cut(reply) else _arguments(raw)
            if arguments is not None:
                projected[name] = {"arguments": arguments}
    return {name: item for name, item in projected.items() if seen[name] == 1}


def subject_of(bodies: Sequence[bytes]) -> tuple[ProviderDecode, dict[str, JsonValue] | None]:
    """The check subject of the attempt: its last provider reply's text decoded as a JSON object, with the attempt's
    tool calls beside it under `tool_calls` (each from the reply that carries it); or why there is none."""
    if not bodies:
        return "NO_CAPTURE", None
    calls = tool_calls(bodies)
    failure, text = _text(bodies[-1])
    decode, subject = (
        (failure, None) if failure is not None else ("NON_TEXT_CONTENT", None) if text is None else _strict(text)
    )
    if not calls or (subject is not None and TOOL_CALLS in subject):
        return decode, subject
    if subject is None:
        return "DECODED_TOOL_CALLS", {TOOL_CALLS: calls}
    return decode, {**subject, TOOL_CALLS: calls}


def captured(
    bodies: Sequence[bytes], application: JsonValue
) -> tuple[dict[str, JsonValue] | None, dict[str, JsonValue]]:
    """(output, evidence) for one replayed attempt, in the generated verifier's evidence contract."""
    decode, subject = subject_of(bodies)
    result: dict[str, JsonValue] = application if isinstance(application, dict) else {"value": application}
    evidence: dict[str, JsonValue] = {
        "provider_decode": decode,
        "application_result": result,
        "capture_layer": CAPTURE_LAYER,
    }
    if subject is not None:
        canonical = json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        evidence["subject_digest"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return subject, evidence

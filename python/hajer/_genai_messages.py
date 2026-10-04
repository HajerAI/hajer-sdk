"""The messages of one recorded call, in the shape the GenAI semantic conventions spell them.

`wrap` records what the caller passed and what came back in each provider's own shape (`WrappedCall.content`, the tool
calls and results beside it). A span carries one shape for all of them: a list of messages, each a `role` and its
`parts`, where a part is text, a tool call or a tool call's response —

    {"role": "user", "parts": [{"type": "text", "content": "where is my order"}]}
    {"role": "assistant", "parts": [{"type": "tool_call", "id": "call-1", "name": "lookup", "arguments": {...}}]}
    {"role": "tool", "parts": [{"type": "tool_call_response", "id": "call-1", "result": "shipped"}]}

— so a reader never has to know which library made the call. Each dialect is one private function; a message in a
shape none of them recognises is kept as the text of its JSON rather than dropped, because a transcript with a hole
in it is worse than one with an odd line.

Pure functions over JSON: nothing here imports a provider, reads a setting or touches a span.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Final

from hajer._json import JsonObject, JsonValue

#: The semconv message roles. Every provider's spelling is folded onto these four.
USER: Final[str] = "user"
ASSISTANT: Final[str] = "assistant"
SYSTEM: Final[str] = "system"
TOOL: Final[str] = "tool"
#: The three part types a message may carry, as the conventions spell them.
TEXT: Final[str] = "text"
TOOL_CALL: Final[str] = "tool_call"
TOOL_CALL_RESPONSE: Final[str] = "tool_call_response"

_ROLES: Final[dict[str, str]] = {
    "user": USER,
    "human": USER,
    "assistant": ASSISTANT,
    "model": ASSISTANT,
    "ai": ASSISTANT,
    "system": SYSTEM,
    "developer": SYSTEM,
    "tool": TOOL,
    "function": TOOL,
}


def text_part(content: str) -> JsonObject:
    return {"type": TEXT, "content": content}


def tool_call_part(identifier: JsonValue, name: JsonValue, arguments: JsonValue) -> JsonObject:
    return {"type": TOOL_CALL, "id": identifier, "name": name, "arguments": _parsed(arguments)}


def tool_response_part(identifier: JsonValue, result: JsonValue) -> JsonObject:
    return {"type": TOOL_CALL_RESPONSE, "id": identifier, "result": result}


def message(role: str, parts: list[JsonObject]) -> JsonObject:
    return {"role": role, "parts": list(parts)}


def _role(value: JsonValue) -> str:
    return _ROLES.get(value.lower(), value) if isinstance(value, str) and value else USER


def _parsed(arguments: JsonValue) -> JsonValue:
    """A provider hands tool arguments back as a JSON string; the part carries the document when it is one."""
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError:
            return arguments
    return arguments


def _as_text(value: JsonValue) -> str:
    """Anything that is not already text, as the text of its JSON — the last resort that loses nothing."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text_parts(content: JsonValue) -> list[JsonObject]:
    """A provider's `content`: a string, or a list of typed blocks whose text blocks become text parts.

    A block that is not text — an image, a document, an audio clip — becomes a part naming its type and nothing
    else: the span says the message carried one without carrying it.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [text_part(content)]
    if not isinstance(content, list):
        return [text_part(_as_text(content))]
    parts: list[JsonObject] = []
    for block in content:
        if isinstance(block, str):
            parts.append(text_part(block))
            continue
        if not isinstance(block, dict):
            parts.append(text_part(_as_text(block)))
            continue
        kind = block.get("type")
        text = block.get("text")
        if isinstance(text, str) and (kind is None or (isinstance(kind, str) and kind.endswith("text"))):
            parts.append(text_part(text))
        elif isinstance(kind, str):
            parts.append({"type": kind})
        else:
            parts.append(text_part(_as_text(block)))
    return parts


# ── OpenAI chat completions (and everything shaped like it: litellm, a router) ──


def _openai_chat_message(item: JsonObject) -> JsonObject:
    role = _role(item.get("role"))
    if role == TOOL:
        return message(TOOL, [tool_response_part(item.get("tool_call_id"), item.get("content"))])
    parts = _text_parts(item.get("content"))
    calls = item.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            inner = function if isinstance(function, dict) else {}
            parts.append(tool_call_part(call.get("id"), inner.get("name", call.get("name")), inner.get("arguments")))
    return message(role, parts)


# ── OpenAI responses ──


def _openai_responses_item(item: JsonObject) -> JsonObject:
    kind = item.get("type")
    if kind == "function_call":
        return message(ASSISTANT, [tool_call_part(item.get("call_id"), item.get("name"), item.get("arguments"))])
    if kind == "function_call_output":
        return message(TOOL, [tool_response_part(item.get("call_id"), item.get("output"))])
    return message(_role(item.get("role")), _text_parts(item.get("content")))


# ── Anthropic messages ──


def _anthropic_message(item: JsonObject) -> JsonObject:
    role = _role(item.get("role"))
    content = item.get("content")
    if not isinstance(content, list):
        return message(role, _text_parts(content))
    parts: list[JsonObject] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(text_part(_as_text(block)))
            continue
        kind = block.get("type")
        if kind == "tool_use":
            parts.append(tool_call_part(block.get("id"), block.get("name"), block.get("input")))
        elif kind == "tool_result":
            parts.append(tool_response_part(block.get("tool_use_id"), block.get("content")))
        else:
            parts.extend(_text_parts([block]))
    return message(role, parts)


# ── google-genai ──


def _genai_content(item: JsonObject) -> JsonObject:
    role = _role(item.get("role") or "user")
    given = item.get("parts")
    parts: list[JsonObject] = []
    for part in given if isinstance(given, list) else []:
        if not isinstance(part, dict):
            parts.append(text_part(_as_text(part)))
            continue
        call = part.get("function_call")
        response = part.get("function_response")
        text = part.get("text")
        if isinstance(call, dict):
            parts.append(tool_call_part(call.get("id"), call.get("name"), call.get("args")))
        elif isinstance(response, dict):
            parts.append(tool_response_part(response.get("id"), response.get("response")))
        elif isinstance(text, str):
            parts.append(text_part(text))
        else:
            parts.append(text_part(_as_text(part)))
    return message(role, parts)


Dialect = Callable[[JsonObject], JsonObject]

#: Which reader a recorded call's `api` names. A router's answer is OpenAI-shaped, and so is its request.
_DIALECTS: Final[dict[str, Dialect]] = {
    "chat.completions": _openai_chat_message,
    "litellm.completion": _openai_chat_message,
    "responses": _openai_responses_item,
    "messages": _anthropic_message,
    "genai.generate_content": _genai_content,
    "genai.generate_content_stream": _genai_content,
}


def _dialect(api: str) -> Dialect:
    return _DIALECTS.get(api, _openai_chat_message)


def input_messages(api: str, messages: JsonValue) -> list[JsonObject]:
    """The request's messages as the span carries them: `content["messages"]` read in the call's own dialect."""
    if isinstance(messages, str):
        return [message(USER, [text_part(messages)])]
    if not isinstance(messages, list):
        return [] if messages is None else [message(USER, [text_part(_as_text(messages))])]
    read = _dialect(api)
    return [read(item) if isinstance(item, dict) else message(USER, [text_part(_as_text(item))]) for item in messages]


def output_messages(
    api: str,
    texts: JsonValue,
    streamed_text: JsonValue,
    tool_calls: list[tuple[JsonValue, JsonValue, JsonValue]],
    finish_reason: str | None,
) -> list[JsonObject]:
    """The answer as the span carries it: one assistant message per choice, the tool calls on the first.

    `texts` is what the reader kept per choice (`content["output"]`): a string, a list of content blocks, or
    `None` for a choice that was all tool calls. A streamed call kept its text as it arrived (`streamed_text`)
    instead. The finish reason is the conventions' own field on the message, when the provider named one.
    """
    choices: list[list[JsonObject]] = []
    if isinstance(streamed_text, str) and streamed_text:
        choices.append([text_part(streamed_text)])
    elif isinstance(texts, list) and api != "embeddings":
        choices.extend(_text_parts(item) for item in texts)
    elif isinstance(texts, str):
        choices.append([text_part(texts)])
    calls = [tool_call_part(identifier, name, arguments) for identifier, name, arguments in tool_calls]
    if not choices and calls:
        choices.append([])
    if choices and calls:
        choices[0].extend(calls)
    answers: list[JsonObject] = []
    for parts in choices:
        answer = message(ASSISTANT, parts)
        if finish_reason is not None:
            answer["finish_reason"] = finish_reason
        answers.append(answer)
    return answers


def system_instructions(system: JsonValue) -> list[JsonObject] | None:
    """`content["system"]` — the out-of-band `system` / `instructions` / `system_instruction` argument — as parts."""
    if system is None:
        return None
    parts = _text_parts(system)
    return parts or None

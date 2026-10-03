"""A fake model: canned provider answers the sandbox serves in place of any model API call, so nothing leaves.

`verify-adapters` calls each declared adapter once to see which model call site it reaches; it needs the call to
complete the way a provider's client expects, and it must spend nothing. So each model endpoint the SDK wraps is
given `count` recorded answers with no request digest: the sandbox serves one to any request for that endpoint,
whatever its body (a WEAK_BOUNDARY_MATCH it counts), and every other request is refused as it always is. The answers
are minimal and well formed (an empty JSON object as the text, a one-dimensional embedding), because what the
application does with the answer is not what is being checked.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

from pydantic import JsonValue

_TEXT: Final = "{}"
#: (host, path, the answer's JSON body): the model endpoints the SDK wraps.
_ANSWERS: Final[tuple[tuple[str, str, dict[str, JsonValue]], ...]] = (
    (
        "api.anthropic.com",
        "/v1/messages",
        {
            "id": "msg_hajer_fake",
            "type": "message",
            "role": "assistant",
            "model": "hajer-fake",
            "content": [{"type": "text", "text": _TEXT}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    ),
    (
        "api.openai.com",
        "/v1/chat/completions",
        {
            "id": "chatcmpl-hajer-fake",
            "object": "chat.completion",
            "created": 0,
            "model": "hajer-fake",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": _TEXT}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ),
    (
        "api.openai.com",
        "/v1/responses",
        {
            "id": "resp_hajer_fake",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "hajer-fake",
            "output": [
                {
                    "type": "message",
                    "id": "msg_hajer_fake",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": _TEXT, "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    ),
    (
        "api.openai.com",
        "/v1/embeddings",
        {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.0]}],
            "model": "hajer-fake",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    ),
)


def fake_answers(count: int, provider: dict[str, JsonValue] | None = None) -> list[JsonValue]:
    """`count` served answers per model endpoint, as the boundary entries `recordings_from` reads."""
    entries: list[JsonValue] = []
    for host, path, answer in _ANSWERS:
        body = json.dumps(answer)
        entry: dict[str, JsonValue] = {
            "method": "POST",
            "scheme": "https",
            "host": host,
            "port": None,
            "path": path,
            "query": "",
            "status": 200,
            "contentType": "application/json",
            "streamed": False,
            "body": body,
            "bodyDigest": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            "truncated": False,
        }
        entries += [dict(entry) for _ in range(count)]
    if provider is not None:
        # Only canned model routes are mirrored. Sandbox.provider remains None: no tunnel or socket is allowed.
        entries.extend({**entry, **provider} for entry in list(entries) if isinstance(entry, dict))
    return entries

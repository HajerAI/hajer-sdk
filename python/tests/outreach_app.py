"""The owned opt-out outreach application: the SDK half of the maintained-suite REFERENCE_REHEARSAL.

`draft_outreach` is a drafting workflow in the shape a customer's code has it: one model call drafts the
turn's proposed messages, and before anything is sent it hands Hajer the request, the whole output and the
evidence it chose. The Hajer platform's maintained-suite rehearsal replays exactly the bytes this
function's `verify` put on the wire.

It is an owned fixture, not a customer: the rule it is judged against is the platform fixture's opt-out rule
(`reference_flow.RULE`), and nothing here claims any application implements it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import hajer
from hajer._json import JsonObject, JsonValue

# The backend fixture's verifier, pinned: the compiled version the suite's capture source admits.
VERIFIER = "outreach-opt-out@1"
# What the caller says it consulted; the verifier's evidence contract requires it and no check reads it.
EVIDENCE: dict[str, JsonValue] = {"suppression_list_version": "2026-09-01"}


def draft_outreach(
    request: JsonObject, *, model_client: object, verifier_client: hajer.Hajer, idempotency_key: str
) -> tuple[JsonObject, hajer.Assessment]:
    """Draft one turn's messages, then verify the final output before any of it is sent."""
    create = _chat(model_client)
    drafted = create(
        model="outreach-writer",
        messages=[{"role": "user", "content": json.dumps(request, sort_keys=True)}],
        temperature=0.2,
    )
    parsed: JsonValue = json.loads(_text(drafted))
    if not isinstance(parsed, dict):
        raise TypeError("the drafting model answers one JSON object per turn")
    output: JsonObject = parsed
    assessment = verifier_client.verify(VERIFIER, request, output, EVIDENCE, idempotency_key=idempotency_key)
    return output, assessment


def _chat(model_client: object) -> Callable[..., object]:
    completions = getattr(getattr(model_client, "chat", None), "completions", None)
    create = getattr(completions, "create", None)
    if not callable(create):
        raise TypeError("the outreach app needs a client with chat.completions.create")
    return create


def _text(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise TypeError("the drafting model returned no choice")
    content = getattr(getattr(cast(list[object], choices)[0], "message", None), "content", None)
    if not isinstance(content, str):
        raise TypeError("the drafting model returned no text")
    return content

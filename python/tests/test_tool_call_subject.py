"""The model's tool calls as a check subject: `output.tool_calls.<name>.arguments`.

A reply's tool calls join the attempt's subject, keyed by tool name, each with its decoded arguments, taken from the
reply that carries the call (a tool loop ending in text keeps it). The shared vectors (`contract/check-evaluation-vectors.json`, `toolCallSubjects`) pin
the projection for the provider shapes a recorded reply takes.
"""

import json
from typing import cast

import pytest
from pydantic import JsonValue

from hajer.replay._capture import subject_of
from tests.repo import CONTRACT

VECTORS = CONTRACT / "check-evaluation-vectors.json"
ROWS = cast(list[dict[str, JsonValue]], json.loads(VECTORS.read_text(encoding="utf-8"))["toolCallSubjects"])


@pytest.mark.parametrize("row", ROWS, ids=[str(row["name"]) for row in ROWS])
def test_a_recorded_reply_projects_to_its_tool_call_subject(row: dict[str, JsonValue]) -> None:
    bodies = row["bodies"]
    assert isinstance(bodies, list)
    decode, subject = subject_of([json.dumps(body).encode() for body in bodies])
    assert (decode, subject) == (row["decode"], row["subject"])

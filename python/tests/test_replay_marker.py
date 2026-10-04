"""Hajer's own replay marker ids travel exactly as minted; anything else under `hajerReplay` is redacted as today.

About one sha256-hex marker id in a few hundred holds a Luhn-valid digit run. The client pass reported it as a phantom
CARD and sent the id mangled, so the capture no longer named its run, plan or case. A string at exactly
`evidence.hajerReplay.<runId | planId | caseId>` that whole matches the grammar of the id Hajer mints there is left
alone (`_replay_marker.py`). Asserted on the redaction pass, with the document in the shape the marker is minted into.
"""

from __future__ import annotations

import json
from typing import cast

import pytest

from hajer._json import JsonObject, JsonValue
from hajer._redact import build_policy, redact_document
from hajer._replay_marker import REPLAY_MARKER

CARD = "4111111111111111"  # Luhn-valid
RUN_ID = f"replay-run-{CARD}a57af5ce6ab17372"
PLAN_ID = f"suite-replay-{CARD}a90bbde2bfbcf0f1"
CASE_ID = f"case-{CARD}a{'b' * 47}"
EMAIL = "jane.doe@example.com"


def redacted(marker: dict[str, JsonValue]) -> tuple[JsonObject, set[str]]:
    """A document with `marker` as its `evidence.hajerReplay`, after the pass, and the classes it found."""
    document: JsonObject = {"request": {"body": "No thanks."}, "evidence": {REPLAY_MARKER: marker}}
    rebuilt, entries = redact_document(document, policy=build_policy())
    return cast("JsonObject", rebuilt), {entry.category for entry in entries}


def test_the_minted_marker_ids_travel_exactly_as_minted() -> None:
    """The diagnoser's regression, client side: no phantom CARD, no mangled join id."""
    marker: dict[str, JsonValue] = {"runId": RUN_ID, "planId": PLAN_ID, "caseId": CASE_ID, "attempt": 1}
    document, classes = redacted(marker)
    evidence = cast("JsonObject", document["evidence"])
    assert evidence[REPLAY_MARKER] == marker
    assert classes == set()


@pytest.mark.parametrize(
    ("marker", "removed", "category"),
    [
        ({"runId": CARD}, CARD, "CARD"),
        ({"runId": RUN_ID + "\n"}, CARD, "CARD"),
        ({"runId": RUN_ID.upper()}, CARD, "CARD"),
        ({"caseId": RUN_ID}, CARD, "CARD"),
        ({"planId": f"{PLAN_ID} {EMAIL}"}, EMAIL, "EMAIL"),
        ({"planId": f"my-intent-{CARD}-replay-observed"}, CARD, "CARD"),
        ({"runId": RUN_ID, "note": EMAIL}, EMAIL, "EMAIL"),
        ({"runId": {"id": RUN_ID}}, CARD, "CARD"),
    ],
    ids=["card", "newline", "uppercase", "wrong-key", "email-beside", "typed-intent", "other-key", "nested"],
)
def test_anything_else_under_the_marker_is_redacted_and_reported(
    marker: dict[str, JsonValue], removed: str, category: str
) -> None:
    """A spoof: only a whole minted id at its own key is left alone; a value in any other shape is scanned."""
    document, classes = redacted(marker)
    assert removed not in json.dumps(document["evidence"])
    assert category in classes


def test_the_exemption_is_only_at_the_markers_own_paths() -> None:
    """The same minted id anywhere else in a document is a string like any other."""
    rebuilt, entries = redact_document(
        {"request": {"runId": RUN_ID}, "evidence": {"hajerReplay": {"runId": RUN_ID}, "runId": RUN_ID}},
        policy=build_policy(),
    )
    document = cast("JsonObject", rebuilt)
    assert cast("JsonObject", cast("JsonObject", document["evidence"])[REPLAY_MARKER])["runId"] == RUN_ID
    assert CARD not in json.dumps(document["request"])
    assert CARD not in json.dumps(cast("JsonObject", document["evidence"])["runId"])
    assert sum(entry.count for entry in entries if entry.category == "CARD") == 2

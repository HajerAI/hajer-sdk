"""The shared vectors for the qualification rule, answered by the pytest plugin.

`contract/qualification-vectors.json` is generated from the platform's rule
and read by its test there. The plugin, which repeats
checks in the customer's CI, must give every sequence of one to five attempts the same label and the same
repeat decision, so the two sides cannot disagree about when a verdict has settled. A live check's verdict is the
one its label settled on (`ci_verdict`): PASS or FAIL when qualified, UNABLE_TO_VERIFY otherwise. A replay reads
fixed recorded answers once, so its single read is its verdict while its label stays not_yet (labels describe live
repeated behaviour). Every attempt stays in the receipt either way.
"""

import json
from dataclasses import dataclass
from typing import cast, get_args

import pytest

from hajer.pytest_plugin._models import Label, Verdict
from hajer.pytest_plugin._qualification import MAX_REPEATS, MIN_AGREEING, label, next_repeat, read
from tests.repo import CONTRACT

VECTORS = CONTRACT / "qualification-vectors.json"
# The plugin's verdicts spelled in the vectors' symbols: UNABLE_TO_VERIFY is the rule's UNKNOWN.
SYMBOLS: dict[str, Verdict] = {"S": "PASS", "V": "FAIL", "U": "UNABLE_TO_VERIFY"}


@dataclass(frozen=True, slots=True)
class Vector:
    """One shared vector: the attempts spelled in S/V/U, the label they earn and whether another is due."""

    attempts: str
    label: str
    repeat: bool


DOCUMENT = cast(dict[str, object], json.loads(VECTORS.read_text(encoding="utf-8")))
ROWS = tuple(
    Vector(attempts=str(row["attempts"]), label=str(row["label"]), repeat=row["repeat"] is True)
    for row in cast(list[dict[str, object]], DOCUMENT["vectors"])
)


def test_the_vectors_are_the_rule_this_plugin_carries() -> None:
    assert DOCUMENT["rule"] == "qualification-rule-v1"
    assert (DOCUMENT["maxRepeats"], DOCUMENT["minAgreeing"]) == (MAX_REPEATS, MIN_AGREEING) == (5, 3)
    assert DOCUMENT["count"] == len(ROWS) == 363
    assert set(cast(dict[str, str], DOCUMENT["symbols"])) == set(SYMBOLS)
    assert set(SYMBOLS.values()) == set(get_args(Verdict))
    assert set(cast(dict[str, str], DOCUMENT["labels"])) == set(get_args(Label))
    assert {row.label for row in ROWS} == set(get_args(Label))
    assert {row.repeat for row in ROWS} == {True, False}


@pytest.mark.parametrize("vector", ROWS, ids=[row.attempts for row in ROWS])
def test_the_plugin_answers_the_vector(vector: Vector) -> None:
    outcomes: list[Verdict] = [SYMBOLS[symbol] for symbol in vector.attempts]
    assert label(outcomes) == vector.label
    assert next_repeat(outcomes) is vector.repeat


@pytest.mark.parametrize("vector", ROWS, ids=[row.attempts for row in ROWS])
def test_the_check_verdict_is_the_settled_verdict(vector: Vector) -> None:
    outcomes: list[Verdict] = [SYMBOLS[symbol] for symbol in vector.attempts]
    counted: set[Verdict] = {outcome for outcome in outcomes if outcome != "UNABLE_TO_VERIFY"}
    settled: Verdict = counted.pop() if vector.label == "qualified" else "UNABLE_TO_VERIFY"
    assert read(outcomes, replayed=False) == (vector.label, settled)
    # A replay is one deterministic read; anything else is not a replay the plugin produces and settles nothing.
    assert read(outcomes, replayed=True) == ("not_yet", outcomes[0] if len(outcomes) == 1 else "UNABLE_TO_VERIFY")


def test_an_unknown_attempt_does_not_unsettle_a_qualified_pass() -> None:
    # U,S,S,S settles on S; the U stays among the attempts the receipt lists.
    assert read(["UNABLE_TO_VERIFY", "PASS", "PASS", "PASS"], replayed=False) == ("qualified", "PASS")
    assert read(["FAIL", "UNABLE_TO_VERIFY", "FAIL", "FAIL"], replayed=False) == ("qualified", "FAIL")
    assert read(["PASS", "PASS", "FAIL"], replayed=False) == ("flaky", "UNABLE_TO_VERIFY")

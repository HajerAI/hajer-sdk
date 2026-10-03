"""The shared check-evaluation vectors, answered by the plugin's CI evaluator.

`contract/check-evaluation-vectors.json` pins how a published check is evaluated; the platform's interpreter answers
the same vectors. Every operation, unit and type generation may publish is one the plugin evaluates, so a published
check never reaches CI as UNABLE_TO_VERIFY for want of an operation.
"""

import json
from typing import cast

import pytest
from pydantic import JsonValue

from hajer.pytest_plugin._evaluate import RANGE_TYPES, SUPPORTED_OPS, TEXT_UNITS, evaluate
from tests.repo import CONTRACT

VECTORS = CONTRACT / "check-evaluation-vectors.json"
DOCUMENT = cast(dict[str, JsonValue], json.loads(VECTORS.read_text(encoding="utf-8")))
ROWS = cast(list[dict[str, JsonValue]], DOCUMENT["vectors"])


@pytest.mark.parametrize("row", ROWS, ids=[str(row["name"]) for row in ROWS])
def test_the_plugin_answers_every_vector(row: dict[str, JsonValue]) -> None:
    check: dict[str, JsonValue] = {"draft": row["draft"]}
    assert evaluate(check, row["output"], request=row["request"]) == row["verdict"]


def test_the_plugin_evaluates_everything_generation_may_publish() -> None:
    assert set(cast(list[str], DOCUMENT["publishableOps"])) <= SUPPORTED_OPS
    assert set(cast(list[str], DOCUMENT["lengthUnits"])) <= TEXT_UNITS
    assert set(cast(list[str], DOCUMENT["rangeTypes"])) <= RANGE_TYPES

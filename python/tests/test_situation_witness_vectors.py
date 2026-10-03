"""The shared situation-witness vectors, answered by the plugin's CI evaluator.

`contract/situation-witness-vectors.json` pins how a KEYED input-situation choice is witnessed on a case's
request; the platform answers the same vectors.
"""

import json
from typing import cast

import pytest
from pydantic import JsonValue

from hajer.pytest_plugin._situations import WITNESS_OPS, witness
from tests.repo import CONTRACT

VECTORS = CONTRACT / "situation-witness-vectors.json"
DOCUMENT = cast(dict[str, JsonValue], json.loads(VECTORS.read_text(encoding="utf-8")))
ROWS = cast(list[dict[str, JsonValue]], DOCUMENT["vectors"])


@pytest.mark.parametrize("row", ROWS, ids=[str(row["name"]) for row in ROWS])
def test_the_plugin_witnesses_every_vector(row: dict[str, JsonValue]) -> None:
    predicate = cast(dict[str, JsonValue], row["predicate"])
    assert witness(predicate, row["request"]) is row["holds"]


def test_the_plugin_knows_every_keyed_operation() -> None:
    assert set(cast(list[str], DOCUMENT["ops"])) == WITNESS_OPS


def test_an_unknown_operation_is_not_witnessed() -> None:
    assert witness({"op": "MATCHES", "field": ["body"]}, {"body": "x"}) is None

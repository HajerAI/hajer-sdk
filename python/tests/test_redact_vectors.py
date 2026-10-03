"""The shared redaction vectors, read by this client and by the TypeScript one.

`contract/redaction-vectors.json` holds thirty-one positives and fifteen negatives with the class
each is expected to report and the exact text it becomes. `typescript/test/redact.test.ts` reads
the same file and asserts the same two things, so the two clients cannot disagree about what a
submission contains without a red test on both sides — which is the whole claim behind
the TypeScript SDK sharing this catalog rather than reimplementing it.

The two provider-key classes are deliberately not in the file: a data file carrying a literal
provider-key prefix trips every credential scan this repository runs over its artifacts, and each
client asserts those two against its own inline sample instead (`test_redact.py`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from hajer._redact import build_policy, redact_submission
from hajer._rules import CATALOG_ID
from tests.repo import CONTRACT

VECTORS = CONTRACT / "redaction-vectors.json"


@dataclass(frozen=True, slots=True)
class Vector:
    """One shared vector, read into a record so both assertions are about typed fields."""

    name: str
    value: str
    classes: tuple[str, ...]
    redacted: str


def _document() -> tuple[str, tuple[Vector, ...]]:
    raw = json.loads(VECTORS.read_text(encoding="utf-8"))
    rows = tuple(
        Vector(
            name=str(row["name"]),
            value=str(row["value"]),
            classes=tuple(str(item) for item in row["classes"]),
            redacted=str(row["redacted"]),
        )
        for group in ("positives", "negatives")
        for row in raw[group]
    )
    return str(raw["catalog"]), rows


CATALOG, ROWS = _document()
POSITIVES = tuple(row for row in ROWS if row.classes)
NEGATIVES = tuple(row for row in ROWS if not row.classes)


def test_the_vectors_name_the_catalog_this_client_carries() -> None:
    assert CATALOG == CATALOG_ID
    assert len(POSITIVES) >= 10
    assert len(NEGATIVES) >= 10


@pytest.mark.parametrize("row", ROWS, ids=[row.name for row in ROWS])
def test_every_shared_vector_reproduces(row: Vector) -> None:
    value, report = redact_submission({"value": row.value}, policy=build_policy())
    classes = () if report is None else tuple(sorted(name for name, _ in report.counts_by_class))
    assert classes == row.classes, row.name
    assert isinstance(value, dict)
    assert value["value"] == row.redacted, row.name

"""A vocabulary the server may extend without this client raising in somebody's request path.

Forward compatibility. Two vocabularies grew in one release —
`origin` gained `BROKER` and `cost_source` gained `LOCAL_PRICED` — so a client that
raised on the next member would be discovering a server upgrade the worst possible way: inside a customer's
own `verify`, which is the one call this SDK promises never to raise from.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from hajer._models import ObservationRow
from hajer._vocabulary import COST_SOURCES, MODES, OBSERVATION_ORIGINS, UNKNOWN, Mode, Vocabulary, read


def row(**overrides: object) -> ObservationRow:
    body: dict[str, object] = {
        "id": "ing-1",
        "createdAt": "2026-09-19T12:00:00Z",
        "mode": "OBSERVE",
        **overrides,
    }
    return ObservationRow.model_validate(body)


def test_unknown_literal_is_unknown_with_raw_value() -> None:
    """A member this client does not know reads as `UNKNOWN`, and the server's own string is kept.

    Both halves matter. `UNKNOWN` is what a reader branches on — and it is a member rather than `None`,
    because *absent* and *unrecognised* are different facts. `raw` is what a log line, a receipt or a support
    conversation needs, and dropping it would turn "the server sent something we do not know" into "the
    server sent nothing".
    """
    known = row(mode="VERIFY").mode
    assert (known.value, known.raw, known.known) == ("VERIFY", "VERIFY", True)

    newer = row(mode="REPLAY").mode
    assert newer.value == UNKNOWN
    assert newer.raw == "REPLAY"
    assert newer.known is False
    # And it prints as what arrived, so a tail's column still says `REPLAY`.
    assert str(newer) == "REPLAY"
    assert f"{newer:<8}|" == "REPLAY  |"


@pytest.mark.parametrize(
    ("members", "value"),
    [
        (OBSERVATION_ORIGINS, "BROKER"),
        (OBSERVATION_ORIGINS, "TARGET_SELF_REPORT"),
        (COST_SOURCES, "LOCAL_PRICED"),
        (COST_SOURCES, "PROVIDER"),
    ],
)
def test_the_two_vocabularies_this_plan_extended_are_known_members(members: tuple[str, ...], value: str) -> None:
    """`BROKER` and `LOCAL_PRICED` are members this client knows, not `UNKNOWN` with a raw string.

    The point of the helper is the *next* member; the point of this test is that the two that were added are
    not already being read as unknown, which would make every receipt from this build's own server look like
    a receipt from a newer one.
    """
    assert read(value, members).value == value


def test_a_value_that_is_not_a_string_is_unknown_rather_than_an_exception() -> None:
    """A malformed answer is still not worth failing a customer's request over.

    `verify` does not raise (`python/CLAUDE.md`), and that contract does not have an exception for a server that
    sent a number where a vocabulary belongs: the honest reading is `UNKNOWN` with the `repr` kept, so the
    fact reaches a log rather than a stack trace.
    """
    unreadable = read(7, ("VERIFY",))

    assert unreadable.value == UNKNOWN
    assert unreadable.raw == "7"


def test_an_already_read_vocabulary_survives_a_second_validation() -> None:
    """`model_validate` over a model's own dump must not double-wrap: the value is idempotent."""
    once = row(mode="VERIFY")
    twice = ObservationRow.model_validate(once.model_dump(by_alias=True))

    assert twice.mode == once.mode


class _Row(BaseModel):
    """One field annotated the way a model field is, so the alias itself is exercised and not only `read`."""

    mode: Mode


def test_the_annotated_alias_reads_and_dumps_as_the_string_that_arrived() -> None:
    """`Mode` is the pair of halves a field needs: a validator that never throws and a dump that round-trips.

    Without the serializer the dataclass would dump as an object and a second `model_validate` would find no
    member in it — which is exactly how a cached response, a replayed body or a stored row would lose its
    vocabulary between two reads.
    """
    assert set(MODES) == {"VERIFY", "OBSERVE"}

    parsed = _Row.model_validate({"mode": "OBSERVE"})
    assert isinstance(parsed.mode, Vocabulary)
    assert parsed.mode.value == "OBSERVE"

    assert _Row.model_validate({"mode": "REPLAY"}).mode.value == UNKNOWN
    assert _Row.model_validate(parsed.model_dump()).mode == parsed.mode
    assert parsed.model_dump() == {"mode": "OBSERVE"}

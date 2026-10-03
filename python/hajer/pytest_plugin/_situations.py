"""A KEYED input-situation choice witnessed on a case's request in customer CI, as the platform witnesses it.

A suite case carries its test frame (`case.frame`) and the suite's `situations` block names each KEYED choice's
predicate: an operation over a field of the case's request. This evaluator answers the shared vectors in
`contract/situation-witness-vectors.json` exactly as the platform's own witness does:

- a field path is read whole, never a suffix: when its parent is not an object of the request the witness is UNKNOWN
  (None); a missing leaf under a present parent is ABSENT; `[]` is the whole request;
- empty: ABSENT, null, a whitespace-only string, an empty list or object;
- length: a string's characters, a list's items, an object's keys; ABSENT and null 0; another scalar its JSON text;
- text: a string itself; a list's or object's string leaves joined by a newline, in order; anything else "";
- CONTAINS_ANY / CONTAINS_NONE: case-folded substrings of that text.

An unknown operation is not witnessed (None), never taken as holding.
"""

import json
from typing import Final, cast

from pydantic import JsonValue

DERIVED_MUTATION: Final = "DERIVED_MUTATION"
NEEDS_LIVE_RUN: Final = "NEEDS_LIVE_RUN"
WITNESS_OPS: Final = frozenset(
    {"EMPTY", "NON_EMPTY", "LENGTH_AT_MOST", "LENGTH_EQUALS", "LENGTH_ABOVE", "CONTAINS_ANY", "CONTAINS_NONE"}
)


class _Absent:
    pass


_ABSENT: Final = _Absent()


def _walk(request: JsonValue, path: list[str]) -> JsonValue | _Absent:
    current: JsonValue = request
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return _ABSENT
        current = current[key]
    return current


class _Unknown:
    pass


def _resolve(request: JsonValue, path: list[str]) -> JsonValue | _Absent | _Unknown:
    if not path:
        return request
    parent = _walk(request, path[:-1])
    if not isinstance(parent, dict):
        return _Unknown()
    return parent[path[-1]] if path[-1] in parent else _ABSENT


def _empty(value: JsonValue | _Absent) -> bool:
    if isinstance(value, _Absent) or value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return isinstance(value, list | dict) and not value


def _length(value: JsonValue | _Absent) -> int:
    if isinstance(value, _Absent) or value is None:
        return 0
    if isinstance(value, str | list | dict):
        return len(value)
    return len(json.dumps(value))


def _text(value: JsonValue | _Absent) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text for item in value if (text := _text(item)))
    if isinstance(value, dict):
        return "\n".join(text for item in value.values() if (text := _text(item)))
    return ""


def witness(predicate: dict[str, JsonValue], request: JsonValue) -> bool | None:
    """Whether the request is in the choice's block; None for an operation this evaluator does not know, or when the
    request does not carry the path's parent (the path was not derived from it)."""
    op = predicate.get("op")
    field = predicate.get("field", [])
    limit = predicate.get("limit", 0)
    words = predicate.get("words", [])
    if (
        op not in WITNESS_OPS
        or not isinstance(field, list)
        or not isinstance(limit, int)
        or not isinstance(words, list)
    ):
        return None
    value = _resolve(request, [str(item) for item in cast("list[JsonValue]", field)])
    if isinstance(value, _Unknown):
        return None
    found = any(str(word).casefold() in _text(value).casefold() for word in cast("list[JsonValue]", words))
    answers: dict[str, bool] = {
        "EMPTY": _empty(value),
        "NON_EMPTY": not _empty(value),
        "LENGTH_AT_MOST": _length(value) <= limit,
        "LENGTH_EQUALS": _length(value) == limit,
        "LENGTH_ABOVE": _length(value) > limit,
        "CONTAINS_ANY": found,
        "CONTAINS_NONE": not found,
    }
    return answers[str(op)]


def keyed_choices(document: dict[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    """The suite's KEYED choices by choice id, each with its predicate (the `situations` block's `keyed`)."""
    block = document.get("situations")
    rows = block.get("choices") if isinstance(block, dict) else None
    found: dict[str, dict[str, JsonValue]] = {}
    for row in rows if isinstance(rows, list) else []:
        if (
            isinstance(row, dict)
            and isinstance(choice := row.get("choiceId"), str)
            and isinstance(row.get("keyed"), dict)
        ):
            found[choice] = cast("dict[str, JsonValue]", row["keyed"])
    return found


def frame_choices(
    case: dict[str, JsonValue], keyed: dict[str, dict[str, JsonValue]]
) -> list[tuple[str, dict[str, JsonValue]]]:
    """The KEYED choices a case's frame intends, each with the predicate CI witnesses on its request."""
    frame = case.get("frame")
    entries = frame.get("frame") if isinstance(frame, dict) else None
    chosen = [item.get("choiceId") for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []
    return [(choice, keyed[choice]) for choice in chosen if isinstance(choice, str) and choice in keyed]

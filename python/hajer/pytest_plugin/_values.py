"""Match the backend predicate_values JSON equality and lookup semantics without a backend dependency."""

from enum import Enum

from pydantic import JsonValue


class Missing(Enum):
    FIELD = "absent"


def lookup(document: JsonValue, path: list[str]) -> JsonValue | Missing:
    value = document
    for part in path:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdecimal() and str(int(part)) == part and int(part) < len(value):
            value = value[int(part)]
        else:
            return Missing.FIELD
    return value


def equal(left: JsonValue, right: JsonValue) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right, strict=True))
    return left == right

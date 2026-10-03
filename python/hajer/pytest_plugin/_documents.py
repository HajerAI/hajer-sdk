"""The four document operations, evaluated in customer CI exactly as the Hajer platform does.

This is the plugin's copy of the platform's document predicates: `schema_valid`
(a JSON-schema subset), `grounded` (every URL, number of two or more digits and named entity in the subject is in a
named source or the allow-list), `excludes` (no listed term as a whole word, case-insensitively) and
`fields_compare` (EQ/NE by typed equality, LT/LE/GT/GE between finite numbers). Both answer the shared vectors in
`contract/check-evaluation-vectors.json` alike; any difference between the two copies is a vector failure.
"""

import json
import math
from typing import Final, cast
from uuid import UUID

from pydantic import JsonValue

from hajer.pytest_plugin._grounding import forbidden, ungrounded
from hajer.pytest_plugin._values import Missing, equal, lookup

DOCUMENT_OPERATIONS: Final = frozenset({"schema_valid", "grounded", "excludes", "fields_compare"})
SCHEMA_KEYWORDS: Final = frozenset(
    {
        "type",
        "format",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "anyOf",
    }
)
SCHEMA_ANNOTATIONS: Final = frozenset({"title", "description", "default", "examples"})
SCHEMA_TYPES: Final = frozenset({"string", "integer", "number", "boolean", "null", "object", "array"})
RELATIONS: Final = frozenset({"EQ", "NE", "LT", "LE", "GT", "GE"})
_NUMERIC: Final = ("minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum")
#: The backend's shape rule for a term list, a rule of the language and not a tunable bound.
_TERMS: Final = 256


def _number(value: JsonValue) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_path(value: JsonValue) -> bool:
    return isinstance(value, list) and all(isinstance(part, str) for part in value)


def schema_shape(schema: JsonValue) -> None:
    """Refuse a schema this subset does not evaluate exactly."""
    if not isinstance(schema, dict):
        raise ValueError("a schema is an object")
    unknown = set(schema) - SCHEMA_KEYWORDS - SCHEMA_ANNOTATIONS
    if unknown:
        raise ValueError(f"schema keywords outside the evaluated subset: {sorted(unknown)}")
    if "format" in schema and schema["format"] != "uuid":
        raise ValueError("schema format must be uuid")
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind] if kind is not None else []
    if any(not isinstance(item, str) or item not in SCHEMA_TYPES for item in kinds):
        raise ValueError("a schema type names JSON types")
    if any(key in schema and not _number(schema[key]) for key in _NUMERIC):
        raise ValueError("schema bounds are finite numbers")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ValueError("enum is a list")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ValueError("required names properties")
    if not isinstance(schema.get("additionalProperties", True), bool):
        raise ValueError("additionalProperties is true or false")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("properties is an object")
    children = [*properties.values(), *([schema["items"]] if "items" in schema else [])]
    alternatives = schema.get("anyOf", [None])
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError("anyOf is a non-empty list")
    for child in [*children, *(item for item in alternatives if item is not None)]:
        schema_shape(child)


def _typed(kind: str, value: JsonValue) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return _number(value) and float(cast(float, value)).is_integer()
    if kind == "number":
        return _number(value)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return isinstance(value, dict) if kind == "object" else isinstance(value, list)


def _bounded(value: float, schema: dict[str, JsonValue], low: str, high: str) -> bool:
    lower, upper = schema.get(low), schema.get(high)
    return (not _number(lower) or value >= cast(float, lower)) and (not _number(upper) or value <= cast(float, upper))


def _object_holds(schema: dict[str, JsonValue], value: dict[str, JsonValue]) -> bool:
    properties = cast(dict[str, JsonValue], schema.get("properties", {}))
    if any(key not in value for key in cast(list[str], schema.get("required", []))):
        return False
    if schema.get("additionalProperties", True) is False and any(key not in properties for key in value):
        return False
    return all(schema_holds(properties[key], item) for key, item in value.items() if key in properties)


def _format_holds(rules: dict[str, JsonValue], value: str) -> bool:
    if rules.get("format") != "uuid":
        return True
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def schema_holds(schema: JsonValue, value: JsonValue) -> bool:
    """Whether the value validates against a schema `schema_shape` admitted."""
    rules = cast(dict[str, JsonValue], schema)
    kind = rules.get("type")
    kinds = cast(list[str], kind if isinstance(kind, list) else [kind] if kind is not None else [])
    if kinds and not any(_typed(item, value) for item in kinds):
        return False
    if "enum" in rules and not any(equal(value, item) for item in cast(list[JsonValue], rules["enum"])):
        return False
    if "const" in rules and not equal(value, rules["const"]):
        return False
    if "anyOf" in rules and not any(schema_holds(item, value) for item in cast(list[JsonValue], rules["anyOf"])):
        return False
    if isinstance(value, dict) and not _object_holds(rules, value):
        return False
    if isinstance(value, list):
        items = rules.get("items")
        if items is not None and not all(schema_holds(items, item) for item in value):
            return False
        return _bounded(len(value), rules, "minItems", "maxItems")
    if isinstance(value, str):
        return _format_holds(rules, value) and _bounded(len(value), rules, "minLength", "maxLength")
    return not _number(value) or _bounded(cast(float, value), rules, "minimum", "maximum")


def related(subject: JsonValue, other: JsonValue | Missing, relation: str) -> bool:
    if isinstance(other, Missing):
        return False
    if relation in {"EQ", "NE"}:
        return equal(subject, other) == (relation == "EQ")
    if not _number(subject) or not _number(other):
        return False
    left, right = cast(float, subject), cast(float, other)
    return {"LT": left < right, "LE": left <= right, "GT": left > right, "GE": left >= right}[relation]


def document_shape(op: str, value_json: str | None) -> None:
    """The parameters each document operation requires, refused as a shape when missing or malformed."""
    if value_json is None:
        raise ValueError(f"{op} carries its parameters in value_json")
    value = cast(JsonValue, json.loads(value_json))
    if op == "schema_valid":
        schema_shape(value)
        return
    if op == "excludes":
        if not isinstance(value, list) or not value or len(value) > _TERMS:
            raise ValueError("excludes names a bounded, non-empty term list")
        if not all(isinstance(term, str) and term.strip() for term in value):
            raise ValueError("every forbidden term is non-empty text")
        return
    if not isinstance(value, dict):
        raise ValueError(f"{op} parameters are an object")
    if op == "grounded":
        sources, allow = value.get("sources", []), value.get("allow", [])
        if not isinstance(sources, list) or not sources or not all(_is_path(item) for item in sources):
            raise ValueError("grounded names at least one source path")
        if not isinstance(allow, list) or not all(isinstance(item, str) for item in allow):
            raise ValueError("grounded's allow-list is text")
        return
    if not _is_path(value.get("other")) or value.get("relation") not in RELATIONS:
        raise ValueError("fields_compare names another path and one of EQ NE LT LE GT GE")


def document_truth(op: str, value_json: str, subject: JsonValue, document: JsonValue) -> bool:
    """The operation's answer over a present subject and the root document (after `document_shape`)."""
    parameters = cast(JsonValue, json.loads(value_json))
    if op == "schema_valid":
        return schema_holds(parameters, subject)
    if op == "excludes":
        return not forbidden(subject, cast(list[str], parameters))
    named = cast(dict[str, JsonValue], parameters)
    if op == "grounded":
        return not ungrounded(subject, document, named)
    other = lookup(document, cast(list[str], named["other"]))
    return related(subject, other, cast(str, named["relation"]))

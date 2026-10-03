"""A published check, evaluated in customer CI exactly as the platform's interpreter evaluates it.

Generation publishes only the operations below, and the platform and this evaluator answer the shared vectors in
`contract/check-evaluation-vectors.json` alike: `exists`, `eq`, `in`, `range` (optionally naming INTEGER),
`length` (optionally in WORDS, SENTENCES, LINES or PARAGRAPHS of a text), `all` over a list, `and`, `not`, the
literal searches `contains` / `not_contains`, and the document operations (`_documents`: `schema_valid`,
`grounded`, `excludes`, `fields_compare`), with an optional applicability over the reply (`output`) and the case's
input (`request`). An absent field fails every operation but `not_contains` (an absent field holds no prohibited
text); `null` is present and is not text. Anything else — another operation, a relation field, an unknown unit, a
malformed node — is UNABLE_TO_VERIFY: metadata is not an oracle.

A check that reads the model's tool calls (`output.tool_calls.<name>.arguments…`, a structured-output rule) is
evaluated on the case's recorded reply, projected as the platform's verifier reads it (`replay._capture.subject_of`);
every other check on the application's return value (`_run._verdict`).

Verdicts: the applicability does not hold → PASS (the reply is outside the requirement's scope, so it holds); the
expression is false → FAIL; the expression holds with every required path present → PASS; otherwise UNABLE_TO_VERIFY.
"""

import json
import math
import re
from typing import Final, TypeAlias, TypeGuard, cast

from pydantic import JsonValue

from hajer.pytest_plugin._documents import DOCUMENT_OPERATIONS, document_shape, document_truth
from hajer.pytest_plugin._models import Verdict
from hajer.pytest_plugin._values import Missing, equal, lookup

SUPPORTED_OPS: Final = (
    frozenset({"exists", "eq", "in", "range", "length", "all", "and", "not", "contains", "not_contains"})
    | DOCUMENT_OPERATIONS
)
TEXT_UNITS: Final = frozenset({"WORDS", "SENTENCES", "LINES", "PARAGRAPHS"})
RANGE_TYPES: Final = frozenset({"INTEGER"})
_SENTENCE: Final = re.compile(r"[^.!?]*[.!?]+|[^.!?]+$")
_PARAGRAPH: Final = re.compile(r"\n\s*\n")
_RELATIONS: Final = ("otherPath", "keyPath", "before", "after", "partnerScope", "normaliser", "normaliserDigest")
_VALUED: Final = frozenset({"eq", "in", "contains", "not_contains"})
_BOUNDED: Final = frozenset({"range", "length"})
#: The backend's shape rule for a membership set (`predicate_shape`), a rule of the language and not a tunable bound.
_MEMBERS: Final = 256

Node: TypeAlias = dict[str, JsonValue]


class _UnsupportedError(ValueError):
    """The check is not one this evaluator answers exactly as the backend does."""


def _path(value: JsonValue) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(part, str) for part in value)


def _reject(constant: str) -> JsonValue:
    raise _UnsupportedError(f"non-finite JSON number {constant}")


def _decoded(node: Node) -> JsonValue:
    raw = node.get("valueJson")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _UnsupportedError("valueJson is JSON text")
    return cast(JsonValue, json.loads(raw, parse_constant=_reject))


def _number(value: JsonValue) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _children(node: Node) -> list[Node]:
    children = node.get("children") or []
    if not isinstance(children, list) or not all(isinstance(child, dict) for child in children):
        raise _UnsupportedError("children are expressions")
    return cast(list[Node], children)


def _check_value(op: str, value: JsonValue, present: bool) -> None:
    if op in _VALUED and not present:
        raise _UnsupportedError(f"{op} needs a valueJson")
    if op == "in" and (not isinstance(value, list) or len(value) > _MEMBERS):
        raise _UnsupportedError("membership needs a bounded list")
    if op in {"contains", "not_contains"} and (not isinstance(value, str) or not value):
        raise _UnsupportedError("a literal search names one non-empty text")
    allowed = TEXT_UNITS if op == "length" else RANGE_TYPES if op == "range" else None
    if op not in _VALUED and present and (allowed is None or not isinstance(value, str) or value not in allowed):
        raise _UnsupportedError(f"{op} names no known unit or type")


def _check_bounds(op: str, node: Node) -> None:
    lower, upper = node.get("lower"), node.get("upper")
    if any(side is not None and not _number(side) for side in (lower, upper)):
        raise _UnsupportedError("bounds are finite numbers")
    if op not in _BOUNDED:
        if lower is not None or upper is not None:
            raise _UnsupportedError(f"{op} takes no bounds")
        return
    if lower is None and upper is None:
        raise _UnsupportedError(f"{op} needs a bound")
    if _number(lower) and _number(upper) and lower > upper:
        raise _UnsupportedError("bounds are reversed")


def _validate(node: JsonValue) -> None:
    """The backend's shape rules (`predicate_shape`, `predicate_constraints`) for the supported operations."""
    if not isinstance(node, dict):
        raise _UnsupportedError("an expression is an object")
    op = node.get("op")
    if not isinstance(op, str) or op not in SUPPORTED_OPS:
        raise _UnsupportedError(f"operation {op!r} is not evaluated in CI")
    if any(node.get(key) not in (None, []) for key in _RELATIONS):
        raise _UnsupportedError("relation fields are not evaluated in CI")
    children = _children(node)
    if op in {"and", "not"}:
        if not children or (op == "not" and len(children) != 1):
            raise _UnsupportedError("composition needs children; not needs exactly one")
    elif op == "all":
        if len(children) != 1:
            raise _UnsupportedError("all needs exactly one child")
    elif children or not _path(node.get("path", [])):
        raise _UnsupportedError("a scalar operation reads one path and hides no children")
    if op in DOCUMENT_OPERATIONS:
        raw = node.get("valueJson")
        document_shape(op, raw if isinstance(raw, str) else None)
        _check_bounds(op, node)
        return
    _check_value(op, _decoded(node), node.get("valueJson") is not None)
    _check_bounds(op, node)
    for child in children:
        _validate(child)


def _measured(value: str, unit: str) -> int:
    if unit == "WORDS":
        return len(value.split())
    if unit == "SENTENCES":
        return sum(1 for item in _SENTENCE.findall(value.strip()) if item.strip())
    if unit == "LINES":
        return len(value.splitlines())
    return sum(1 for block in _PARAGRAPH.split(value.strip()) if block.strip())


def _bounded(value: float, node: Node) -> bool:
    lower, upper = node.get("lower"), node.get("upper")
    return (not _number(lower) or value >= lower) and (not _number(upper) or value <= upper)


def _length(node: Node, value: JsonValue) -> bool:
    unit = _decoded(node)
    if isinstance(unit, str):
        return isinstance(value, str) and _bounded(_measured(value, unit), node)
    return isinstance(value, str | list | dict) and _bounded(len(value), node)


def _scalar(op: str, node: Node, value: JsonValue) -> bool:
    if op == "exists":
        return True
    if op == "eq":
        return equal(value, _decoded(node))
    if op == "in":
        allowed = _decoded(node)
        return isinstance(allowed, list) and any(equal(value, item) for item in allowed)
    if op == "range":
        integral = isinstance(value, int) or (isinstance(value, float) and value.is_integer())
        return _number(value) and (node.get("valueJson") is None or integral) and _bounded(value, node)
    if op == "length":
        return _length(node, value)
    literal = _decoded(node)
    if not isinstance(value, str) or not isinstance(literal, str):
        return False
    return (literal in value) == (op == "contains")


#: Three-valued, as the backend's `combine`: True, False, or None (unknown).
Truth: TypeAlias = bool | None
TOOL_CALLS: Final = ["output", "tool_calls"]


def _conjunction(values: list[Truth]) -> Truth:
    if any(value is False for value in values):
        return False
    return None if any(value is None for value in values) else True


def _truth(node: Node, document: JsonValue, *, root: bool = True) -> Truth:
    op = cast(str, node["op"])
    children = _children(node)
    if op == "and":
        return _conjunction([_truth(child, document, root=root) for child in children])
    if op == "not":
        inner = _truth(children[0], document, root=root)
        return None if inner is None else not inner
    path = cast(list[str], node.get("path", []))
    value = lookup(document, path)
    if isinstance(value, Missing):
        # A tool call the reply does not carry is unknown, not absent (the backend's `predicate_walk.walk`).
        if root and path[:2] == TOOL_CALLS and isinstance(lookup(document, path[:3]), Missing):
            return None
        return op in {"not_contains", "excludes"}
    if op in DOCUMENT_OPERATIONS:
        return document_truth(op, cast(str, node["valueJson"]), value, document)
    if op == "all":
        if not isinstance(value, list):
            return False
        return _conjunction([_truth(children[0], item, root=False) for item in value])
    return _scalar(op, node, value)


def evaluate(check: dict[str, JsonValue], output: JsonValue, *, request: JsonValue = None) -> Verdict:
    """The CI verdict of one published check on one reply, `request` being the case's input."""
    draft = check.get("draft")
    if not isinstance(draft, dict) or draft.get("requiredProcessObservations", []) != []:
        return "UNABLE_TO_VERIFY"
    # The application result is a separate evidence boundary. In particular a model's
    # wire schema can use aliases while the returned model_dump uses Python field names.
    # Do not require raw model telemetry to masquerade as an application return value.
    document: dict[str, JsonValue] = {"output": output}
    if not reads_tool_calls(check):
        document["evidence"] = {"application_result": output}
    if request is not None:
        document["request"] = request
    expression, applicability = draft.get("expression"), draft.get("applicability")
    paths = draft.get("requiredPaths", [])
    try:
        if not isinstance(paths, list) or not all(_path(path) and path for path in paths):
            raise _UnsupportedError("required paths are non-empty paths")
        _validate(expression)
        if applicability is not None:
            _validate(applicability)
            applies = _truth(cast(Node, applicability), document)
            if applies is None:
                return "UNABLE_TO_VERIFY"
            if not applies:
                return "PASS"
        holds = _truth(cast(Node, expression), document)
        if holds is None:
            return "UNABLE_TO_VERIFY"
        if not holds:
            return "FAIL"
    except (ValueError, RecursionError):
        return "UNABLE_TO_VERIFY"
    present = all(not isinstance(lookup(document, cast(list[str], path)), Missing) for path in paths)
    return "PASS" if present else "UNABLE_TO_VERIFY"


def _paths(node: JsonValue) -> list[JsonValue]:
    if not isinstance(node, dict):
        return []
    children = node.get("children")
    nested = [path for child in (children if isinstance(children, list) else []) for path in _paths(child)]
    return [node.get("path"), *nested]


def reads_tool_calls(check: dict[str, JsonValue]) -> bool:
    """The check reads the model's tool calls (`output.tool_calls…`), not the application's return value."""
    draft = check.get("draft")
    if not isinstance(draft, dict):
        return False
    required = draft.get("requiredPaths")
    paths = [*_paths(draft.get("expression")), *_paths(draft.get("applicability"))]
    paths += required if isinstance(required, list) else []
    return any(isinstance(path, list) and path[:2] == ["output", "tool_calls"] for path in paths)

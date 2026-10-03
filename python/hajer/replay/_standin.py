"""A stand-in input for an adapter, read from the callable's own signature when no authored input is at hand.

`verify-adapters` calls each declared adapter once. The input it prefers is one a model authored for that adapter
(a committed suite's case); with none, it builds one value per parameter a case would carry, from the declared type:
a short fixed string, `1`, `1.0`, `False`, the first member of an enum or a `Literal`, a one-element list, and for a
pydantic model, a dataclass or a TypedDict, its required fields, each built the same way. Nothing in it is customer
data, and it is never sent anywhere: the only question it answers is which model call the adapter reaches.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import inspect
import typing
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Final, cast, is_typeddict

from pydantic import BaseModel, JsonValue

from hajer.replay._coerce import hints

TEXT: Final = "hajer adapter check"
#: How deep a stand-in nests models inside models before it stops at a string.
_DEPTH: Final[int] = 4
_UNIONS: Final = (typing.Union, getattr(__import__("types"), "UnionType", typing.Union))
_SCALARS: Final[dict[object, JsonValue]] = {
    str: TEXT,
    bool: False,
    int: 1,
    float: 1.0,
    type(None): None,
    uuid.UUID: "00000000-0000-4000-8000-000000000000",
    datetime.datetime: "2026-01-01T00:00:00+00:00",
    datetime.date: "2026-01-01",
}


def _declared(hint: type[object]) -> dict[str, object]:
    """A class's field types evaluated (a module with `from __future__ import annotations` keeps them as strings)."""
    try:
        return dict(typing.get_type_hints(hint))
    except Exception:  # noqa: BLE001 - an unresolvable annotation leaves the field's written type
        return {}


def _fields(hint: type[object]) -> dict[str, object]:
    """The required fields a structured type is built from."""
    if issubclass(hint, BaseModel):
        return {name: field.annotation for name, field in hint.model_fields.items() if field.is_required()}
    if dataclasses.is_dataclass(hint):
        declared = _declared(hint)
        return {
            item.name: declared.get(item.name, item.type)
            for item in dataclasses.fields(hint)
            if item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING
        }
    if is_typeddict(hint):
        required = cast(frozenset[str], getattr(hint, "__required_keys__", frozenset[str]()))
        return {name: value for name, value in typing.get_type_hints(hint).items() if name in required}
    return {}


def _container(hint: object, depth: int) -> JsonValue:
    origin, arguments = typing.get_origin(hint), typing.get_args(hint)
    if origin in _UNIONS:
        return value(next((item for item in arguments if item is not type(None)), str), depth)
    if origin is typing.Literal:
        return cast(JsonValue, arguments[0])
    if origin in (dict, Mapping):
        return {}
    return [value(arguments[0] if arguments else str, depth + 1)]


def value(hint: object, depth: int = 0) -> JsonValue:
    """One stand-in value of the declared type."""
    if hint in _SCALARS:
        return _SCALARS[hint]
    if depth >= _DEPTH:
        return TEXT
    if typing.get_origin(hint) is not None:
        return _container(hint, depth)
    if not isinstance(hint, type):
        return TEXT
    klass = cast(type[object], hint)
    if issubclass(klass, enum.Enum):
        return cast(JsonValue, next(iter(klass)).value)
    if issubclass(klass, str):
        return TEXT
    if issubclass(klass, dict | Mapping):
        return {}
    if issubclass(klass, list | tuple | set | Sequence):
        return [TEXT]
    structured = issubclass(klass, BaseModel) or dataclasses.is_dataclass(klass) or is_typeddict(klass)
    return {name: value(item, depth + 1) for name, item in _fields(klass).items()} if structured else TEXT


def standin(adapter: Mapping[str, JsonValue], target: Callable[..., object]) -> JsonValue:
    """The input a call of `target` through `adapter` is checked with, built from the parameters a case carries."""
    bound = adapter.get("arguments")
    supplied: set[str] = set(bound) if isinstance(bound, dict) else set()
    try:
        parameters = [
            item
            for item in inspect.signature(target).parameters.values()
            if item.kind not in (item.VAR_POSITIONAL, item.VAR_KEYWORD) and item.name not in supplied
        ]
    except (TypeError, ValueError):
        return TEXT
    declared = hints(target)
    by_name = {item.name: value(declared.get(item.name, str)) for item in parameters}
    mode = adapter.get("input")
    if mode == "SINGLE":
        return next(iter(by_name.values()), TEXT)
    fields = adapter.get("fields")
    if mode == "FIELDS" and isinstance(fields, dict):
        return {str(field): by_name.get(name, TEXT) for name, field in fields.items()}
    required = [item.name for item in parameters if item.default is inspect.Parameter.empty] or list(by_name)[:1]
    return {name: by_name[name] for name in required}

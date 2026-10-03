"""A case input read as the types the entry declares, where the input is JSON and the parameter is not.

A case input is JSON (an authored input, a recorded request), and an entry often takes a typed object: a pydantic
model, a dataclass, an enum, a UUID or a datetime, or a list of them. Passing the dict where the model is expected is
an `AttributeError` inside the application, which says nothing about the application. So a value is validated into
its parameter's declared type (pydantic's `TypeAdapter`) exactly when that type is one of those and the value is not
already an instance of it; a value that does not read as the type is PARSE, named with the parameter. Every other
parameter is passed the value as given, so a plain `str` or `dict` entry behaves as it always has.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import inspect
import typing
import uuid
from collections.abc import Callable
from typing import Final, cast

from pydantic import BaseModel, TypeAdapter, ValidationError

from hajer.replay._entry_errors import EntryUnavailable

_SCALARS: Final = (uuid.UUID, datetime.datetime, datetime.date)
_UNIONS: Final = (typing.Union, getattr(__import__("types"), "UnionType", typing.Union))


def _typed(hint: object) -> bool:
    """Whether a value of this type is built from JSON: a model, a dataclass, an enum, a UUID or a date, or a list,
    tuple or set of those, or an optional one."""
    origin = typing.get_origin(hint)
    if origin in _UNIONS or origin in (list, tuple, set, frozenset):
        return any(_typed(item) for item in typing.get_args(hint) if item is not type(None))
    if not isinstance(hint, type):
        return False
    klass = cast(type[object], hint)
    return issubclass(klass, (BaseModel, enum.Enum, *_SCALARS)) or dataclasses.is_dataclass(klass)


def hints(target: Callable[..., object]) -> dict[str, object]:
    """The parameter types `target` declares, or none when they cannot be evaluated."""
    try:
        found = typing.get_type_hints(target)
    except Exception:  # noqa: BLE001 - an unresolvable forward reference means: pass values as given
        return {}
    return {name: hint for name, hint in found.items() if name != "return"}


def coerced(target: Callable[..., object], name: str, value: object) -> object:
    """`value` read as parameter `name`'s declared type when that type is built from JSON, else `value`."""
    hint = hints(target).get(name)
    if hint is None or not _typed(hint) or (isinstance(hint, type) and isinstance(value, hint)):
        return value
    try:
        return cast(object, TypeAdapter(hint).validate_python(value))
    except (ValidationError, TypeError) as error:
        raise EntryUnavailable("PARSE", f"input `{name}` does not read as {hint!r}") from error


def positional_name(target: Callable[..., object]) -> str | None:
    """The first parameter a SINGLE input is passed as."""
    try:
        parameters = list(inspect.signature(target).parameters.values())
    except (TypeError, ValueError):
        return None
    return parameters[0].name if parameters else None

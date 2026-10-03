"""A declared recipe turned into the one value it names: what a FACTORY is called with, and a bound parameter.

The schema is the Hajer platform's, carried in the adapter document the
parent froze (`factoryArguments`, `arguments`), each value one of:

- `{"name": "module:dotted.path"}` — import the module, then read the attributes;
- `{"call": "module:dotted.path"}` — the same, then call it with no argument;
- `{"value": <literal>}` — the literal;
- `{"construct": "module:dotted.path", "arguments": {...}}` — call it with keyword arguments, each a recipe.

`"model_client": true` marks the application's model client: what the recipe built is handed to `hajer.wrap`, so the
calls it makes are recorded with the application frame that made them (and, in a replay, answered through the
sandbox). An object `wrap` does not recognise is kept as built. A parameter whose annotation names an external
resource (a database session or engine, an HTTP client, a queue, a cache, a request) is refused by name, whatever
the declaration supplies for it: a replay never builds one.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Mapping
from typing import Final, cast

from pydantic import JsonValue

from hajer._errors import UnsupportedClientError
from hajer._wrap import wrap
from hajer.replay._entry_errors import EntryUnavailable

#: The annotation names a replay never builds a value for (the backend's `adapter_signature.EXTERNAL_TYPES`).
EXTERNAL_TYPES: Final = frozenset(
    {
        "AsyncSession",
        "Session",
        "sessionmaker",
        "async_sessionmaker",
        "scoped_session",
        "Engine",
        "AsyncEngine",
        "Connection",
        "AsyncConnection",
        "Cursor",
        "Pool",
        "AsyncClient",
        "ClientSession",
        "Redis",
        "StrictRedis",
        "Queue",
        "Broker",
        "AsyncIOMotorClient",
        "MongoClient",
        "Request",
        "WebSocket",
        "BackgroundTasks",
        "Channel",
    }
)
_KINDS: Final = ("name", "call", "value", "construct")


def import_module(module: str) -> object:
    try:
        return importlib.import_module(module)
    except ImportError as error:
        # The class, never the message: an import error's message quotes the interpreter's absolute paths.
        raise EntryUnavailable("MISSING_DEPENDENCY", f"import {module}: {type(error).__name__}") from error
    except Exception as error:
        raise EntryUnavailable("MISSING_DEPENDENCY", f"import {module} raised {type(error).__name__}") from error


def _named(target: str) -> object:
    module, _, path = target.partition(":")
    found = import_module(module)
    for part in (item for item in path.split(".") if item):
        try:
            found = cast(object, getattr(found, part))
        except AttributeError as error:
            raise EntryUnavailable("ADAPTER_MISSING", f"{target!r} does not exist where the recipe says") from error
    return found


def _called(target: str, arguments: dict[str, object]) -> object:
    found = _named(target)
    if not callable(found):
        raise EntryUnavailable("ADAPTER_MISSING", f"{target!r} is not callable")
    try:
        return found(**arguments)
    except Exception as error:
        raise EntryUnavailable("ADAPTER_MISSING", f"building {target!r} raised {type(error).__name__}") from error


def build(recipe: JsonValue) -> object:
    """The value one recipe names, built as the application's own code builds it."""
    if not isinstance(recipe, dict) or sum(key in recipe for key in _KINDS) != 1:
        raise EntryUnavailable("PARSE", "a recipe holds exactly one of name, call, value, construct")
    if "value" in recipe:
        return recipe["value"]
    kind = next(key for key in _KINDS if key in recipe)
    target = str(recipe[kind])
    nested = recipe.get("arguments")
    built = (
        _named(target)
        if kind == "name"
        else _called(target, arguments(nested) if kind == "construct" and isinstance(nested, dict) else {})
    )
    if recipe.get("model_client") is True:
        try:
            return wrap(built)
        except UnsupportedClientError:
            return built
    return built


def arguments(document: JsonValue) -> dict[str, object]:
    """A table of keyword recipes, each built."""
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise EntryUnavailable("PARSE", "declared arguments are a table of recipes")
    return {name: build(recipe) for name, recipe in document.items()}


def _external(parameter: inspect.Parameter) -> bool:
    annotation = parameter.annotation
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", repr(annotation))
    names = {part.strip(" []|'\"").rpartition(".")[2] for part in str(text).replace(",", "|").split("|")}
    return bool(names & EXTERNAL_TYPES)


def checked_call(target: Callable[..., object], supplied: Mapping[str, object], label: str) -> None:
    """Refuse, by name, a parameter of `target` that is an external resource, and a required one nobody supplies."""
    try:
        parameters = inspect.signature(target).parameters.values()
    except (TypeError, ValueError):
        return
    for parameter in parameters:
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if _external(parameter) and (parameter.name in supplied or parameter.default is inspect.Parameter.empty):
            raise EntryUnavailable(
                "ADAPTER_MISSING", f"{label}: `{parameter.name}` is an external resource, which a replay never builds"
            )

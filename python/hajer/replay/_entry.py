"""The adapter the parent froze into the case spec, turned into one call. Declared or derived, never guessed.

A declared adapter (`.hajer/replay.toml`) says how to construct the owner and how to map the input. A
derived one (`constructor` and `input` both `DERIVE`) is accepted only when the call is unambiguous: a
module-level function, or a method of a class whose constructor needs no argument; the input spread as
keywords when it is an object whose keys cover exactly the required parameters, or passed whole when
the callable takes exactly one. Every other shape is ADAPTER_MISSING.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue

from hajer.replay._coerce import coerced, positional_name
from hajer.replay._entry_errors import EntryReason, EntryUnavailable
from hajer.replay._recipes import arguments, build, checked_call, import_module

__all__ = ["EntryReason", "EntryUnavailable", "PreparedCall", "prepare_call", "target_of"]


@dataclass(frozen=True, slots=True)
class PreparedCall:
    target: Callable[..., object]
    args: tuple[object, ...]
    kwargs: dict[str, object]


def _import(module: str) -> object:
    return import_module(module)


def _attribute(owner: object, name: str) -> object:
    try:
        return cast(object, getattr(owner, name))
    except AttributeError as error:
        raise EntryUnavailable("ADAPTER_MISSING", f"{name!r} does not exist where the adapter says") from error


def _no_arg_constructible(cls: type) -> bool:
    try:
        parameters = inspect.signature(cls).parameters.values()
    except (TypeError, ValueError):
        return False
    return all(
        item.default is not inspect.Parameter.empty or item.kind in (item.VAR_POSITIONAL, item.VAR_KEYWORD)
        for item in parameters
    )


def _construct(adapter: Mapping[str, JsonValue], module: object, owner_name: str) -> object:
    constructor = adapter["constructor"]
    if constructor == "FACTORY":
        factory_module, _, factory_name = str(adapter["factory"]).partition(":")
        factory: object = _import(factory_module)
        for part in factory_name.split("."):
            factory = _attribute(factory, part)
        if not callable(factory):
            raise EntryUnavailable("ADAPTER_MISSING", f"factory {factory_name!r} is not callable")
        supplied = arguments(adapter.get("factoryArguments"))
        checked_call(factory, supplied, f"factory {factory_name}")
        return factory(**supplied)
    owner = _attribute(module, owner_name)
    if not isinstance(owner, type):
        raise EntryUnavailable("ADAPTER_MISSING", f"{owner_name!r} is not a class")
    if constructor == "DERIVE" and not _no_arg_constructible(owner):
        raise EntryUnavailable("ADAPTER_MISSING", f"{owner_name}() needs arguments; declare a constructor recipe")
    return cast(object, owner())


def _setup(adapter: Mapping[str, JsonValue]) -> None:
    """The declared `[setup] calls`, run once, in order, before the adapter is built: the state the application's
    startup installs (a client registry, a configured singleton), which a replay does not run."""
    calls = adapter.get("setup")
    for recipe in calls if isinstance(calls, list) else []:
        build(recipe)


def _target(adapter: Mapping[str, JsonValue]) -> Callable[..., object]:
    _setup(adapter)
    module = _import(str(adapter["module"]))
    owner_name, _, name = str(adapter["qualname"]).rpartition(".")
    if owner_name:
        try:
            instance = _construct(adapter, module, owner_name)
        except EntryUnavailable:
            raise
        except Exception as error:
            raise EntryUnavailable(
                "ADAPTER_MISSING", f"constructing {owner_name} raised {type(error).__name__}"
            ) from error
        target = _attribute(instance, name)
    else:
        target = _attribute(module, name)
    if not callable(target) or isinstance(target, type):
        raise EntryUnavailable("ADAPTER_MISSING", f"{adapter['qualname']!r} is not a function or method")
    return target


def _derived_arguments(target: Callable[..., object], value: JsonValue) -> PreparedCall:
    try:
        parameters = [
            item
            for item in inspect.signature(target).parameters.values()
            if item.kind not in (item.VAR_POSITIONAL, item.VAR_KEYWORD)
        ]
    except (TypeError, ValueError) as error:
        raise EntryUnavailable("ADAPTER_MISSING", "the callable's signature cannot be read") from error
    required = {item.name for item in parameters if item.default is inspect.Parameter.empty}
    names = {item.name for item in parameters}
    if isinstance(value, dict) and value and required <= set(value) <= names:
        return PreparedCall(target, (), dict(value))
    if len(parameters) == 1:
        return PreparedCall(target, (value,), {})
    raise EntryUnavailable("ADAPTER_MISSING", "the input does not map onto the callable's parameters unambiguously")


def _inputs(adapter: Mapping[str, JsonValue], target: Callable[..., object], value: JsonValue) -> PreparedCall:
    mode = adapter["input"]
    if mode == "DERIVE":
        return _derived_arguments(target, value)
    if mode == "SINGLE":
        return PreparedCall(target, (value,), {})
    if not isinstance(value, dict):
        raise EntryUnavailable("PARSE", f"input mode {mode} needs a JSON object input")
    if mode == "KWARGS":
        return PreparedCall(target, (), dict(value))
    fields = adapter["fields"]
    mapping = cast(dict[str, str], fields) if isinstance(fields, dict) else {}
    missing = sorted(field for field in mapping.values() if field not in value)
    if missing:
        raise EntryUnavailable("PARSE", f"the input has no field(s) {missing}")
    return PreparedCall(target, (), {parameter: value[field] for parameter, field in mapping.items()})


def target_of(adapter: Mapping[str, JsonValue]) -> Callable[..., object]:
    """The callable the adapter names, its owner constructed as the adapter says (a factory runs here)."""
    return _target(adapter)


def prepare_call(
    adapter: Mapping[str, JsonValue], value: JsonValue, *, target: Callable[..., object] | None = None
) -> PreparedCall:
    """The call a case makes: the input mapped as the adapter says, each value read as its parameter's declared type
    when that type is built from JSON (`_coerce`), and the declared `arguments` (the bound model client) beside it.
    `target` is the callable `target_of` already built, so a factory runs once."""
    target = _target(adapter) if target is None else target
    prepared = _inputs(adapter, target, value)
    bound = arguments(adapter.get("arguments"))
    checked_call(target, bound, str(adapter["qualname"]))
    first = positional_name(target)
    args = tuple(coerced(target, first, item) if first else item for item in prepared.args)
    kwargs = {name: coerced(target, name, item) for name, item in prepared.kwargs.items()}
    return PreparedCall(target, args, kwargs | bound)

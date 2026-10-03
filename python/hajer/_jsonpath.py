"""One path grammar, the same subset the backend declares, for every place that names a value.

The Hajer platform's path grammar is the authority and this is the second
implementation of it, held together by shared test data rather than by an import: the SDK's runtime
dependencies are httpx and pydantic and a shared package would be a third. Two things
point at a value from this side — the case-key normaliser's volatile paths and client redaction's
exempt paths — and both are declarations a customer writes, so a grammar that silently missed what they
meant would leave them believing a field was protected or ignored when it was not.

    path    := [ "$" ] [ "." ] step { ( "." step ) | index }
    step    := name
    index   := "[" ( digits | "*" ) "]"
    name    := [A-Za-z0-9_-]+

A path that matches nothing is not an error — absence is the normal case — and nothing here mutates
its input.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from hajer._json import JsonValue

_NAME = re.compile(r"[A-Za-z0-9_-]+")


class PathSyntaxError(ValueError):
    """This text is not a path in the subset above. Raised where the path is written, never in `verify`."""


@dataclass(frozen=True, slots=True)
class Name:
    """One object key."""

    key: str


@dataclass(frozen=True, slots=True)
class Index:
    """One array position, counted from zero."""

    position: int


@dataclass(frozen=True, slots=True)
class Wildcard:
    """`[*]` — every element of an array."""


Segment = Name | Index | Wildcard


@dataclass(frozen=True, slots=True)
class Hit:
    """One concrete place a path matched, and the value that was there."""

    path: str
    value: JsonValue


def parse_path(text: str) -> tuple[Segment, ...]:
    """The segments of one declared path, or `PathSyntaxError` naming what is wrong with it."""
    body = text.strip()
    if not body:
        raise PathSyntaxError("a path names at least one field; this one is empty")
    if body.startswith("$"):
        body = body[1:]
    if body.startswith("."):
        body = body[1:]
    segments: list[Segment] = []
    position = 0
    while position < len(body):
        character = body[position]
        if character == "[":
            close = body.find("]", position)
            if close == -1:
                raise PathSyntaxError(f"{text!r}: an index opens with '[' and closes with ']'")
            inside = body[position + 1 : close]
            if inside == "*":
                segments.append(Wildcard())
            elif inside.isdigit():
                segments.append(Index(int(inside)))
            else:
                raise PathSyntaxError(f"{text!r}: an index is a number or '*', not {inside!r}")
            position = close + 1
            continue
        if character == ".":
            position += 1
            continue
        found = _NAME.match(body, position)
        if found is None:
            raise PathSyntaxError(
                f"{text!r}: {character!r} is not part of a path in this subset "
                "($.a[0].b, a[*].b, a.b — no filters, no recursive descent, no quoted keys)"
            )
        segments.append(Name(found.group(0)))
        position = found.end()
    if not segments:
        raise PathSyntaxError(f"{text!r}: a path names at least one field")
    return tuple(segments)


def parse_paths(texts: Iterable[str]) -> tuple[tuple[Segment, ...], ...]:
    """Every declared path, parsed. One bad path refuses the whole declaration."""
    return tuple(parse_path(text) for text in texts)


def _step(path: str, segment: Segment) -> str:
    if isinstance(segment, Name):
        return segment.key if not path else f"{path}.{segment.key}"
    if isinstance(segment, Index):
        return f"{path}[{segment.position}]"
    return f"{path}[*]"  # pragma: no cover - expanded to an Index before this is reached


def hits(document: JsonValue, path: Sequence[Segment], *, prefix: str = "") -> tuple[Hit, ...]:
    """Every concrete place this path reaches in this document. Empty when it reaches none."""
    if not path:
        return (Hit(path=prefix, value=document),)
    head, rest = path[0], path[1:]
    if isinstance(head, Name):
        if not isinstance(document, dict) or head.key not in document:
            return ()
        return hits(document[head.key], rest, prefix=_step(prefix, head))
    if isinstance(head, Index):
        if not isinstance(document, list) or not 0 <= head.position < len(document):
            return ()
        return hits(document[head.position], rest, prefix=_step(prefix, head))
    if not isinstance(document, list):
        return ()
    return tuple(
        hit for position, item in enumerate(document) for hit in hits(item, rest, prefix=_step(prefix, Index(position)))
    )


def matched(document: JsonValue, paths: Sequence[Sequence[Segment]]) -> frozenset[str]:
    """The concrete paths these declarations reach — what a rebuild or an exemption tests against."""
    return frozenset(hit.path for path in paths for hit in hits(document, path))


def without(document: JsonValue, paths: Sequence[Sequence[Segment]]) -> tuple[JsonValue, tuple[str, ...]]:
    """The document with every value these paths reach removed, and the concrete paths removed."""
    targets = matched(document, paths)
    return _rebuild(document, targets, "", None), tuple(sorted(targets))


def _rebuild(
    value: JsonValue, targets: frozenset[str], prefix: str, transform: Callable[[str, JsonValue], JsonValue] | None
) -> JsonValue:
    if isinstance(value, dict):
        rebuilt: dict[str, JsonValue] = {}
        for key, item in value.items():
            child = _step(prefix, Name(key))
            if child in targets:
                if transform is not None:
                    rebuilt[key] = transform(child, item)
                continue
            rebuilt[key] = _rebuild(item, targets, child, transform)
        return rebuilt
    if isinstance(value, list):
        kept: list[JsonValue] = []
        for position, item in enumerate(value):
            child = _step(prefix, Index(position))
            if child in targets:
                if transform is not None:
                    kept.append(transform(child, item))
                continue
            kept.append(_rebuild(item, targets, child, transform))
        return kept
    return value

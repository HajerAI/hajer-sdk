"""Generate `hajer/_wire.py` from the platform's OpenAPI snapshot.

Nothing about the wire is hand-written: `contract/openapi.json` (at the repository root, copied from the
Hajer platform — `contract/README.md`) is the one serialization of the API, and this script turns the
slice of it the SDK calls into pydantic models.

    just contract-refresh    # rewrite hajer/_wire.py from the snapshot (this script, --write)
    just contract-check      # fail if hajer/_wire.py is not what the snapshot produces (--check)

Which operations it reads is not a list kept here: it is `hajer._paths.SDK_OPERATIONS`, the calls the
SDK makes. A snapshot that declares none of them emits a placeholder that says so and carries
`WIRE_READY = False`.

Run from `python/`. `--snapshot` points at another copy; the environment is never read (the SDK reads
it in exactly one place, `hajer/_settings.py`).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hajer._paths import SDK_OPERATIONS

SDK_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = SDK_DIR.parent / "contract" / "openapi.json"
#: How the generated module names its source: relative to the repository root, never this machine.
SNAPSHOT_NAME = "contract/openapi.json"
TARGET = SDK_DIR / "hajer" / "_wire.py"
REMEDY = "run `just contract-refresh` in python/ and commit hajer/_wire.py with the change"


# ── the slice of OpenAPI 3.1 this generator understands ──────────────────────────────────────────
# Everything outside it becomes `JsonValue`, openly, rather than a guess. `extra="ignore"` is what
# lets these five models read a document that carries far more than they name.


class _Node(BaseModel):
    """One schema node: a `$ref`, a primitive, an array, a union, or an object with properties."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ref: str | None = Field(default=None, alias="$ref")
    type_: str | list[str] | None = Field(default=None, alias="type")
    enum: list[str | int] | None = None
    const: str | int | None = None
    any_of: list[_Node] | None = Field(default=None, alias="anyOf")
    one_of: list[_Node] | None = Field(default=None, alias="oneOf")
    all_of: list[_Node] | None = Field(default=None, alias="allOf")
    items: _Node | None = None
    properties: dict[str, _Node] | None = None
    required: list[str] | None = None
    additional_properties: bool | _Node | None = Field(default=None, alias="additionalProperties")
    description: str | None = None


class _Media(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_: _Node | None = Field(default=None, alias="schema")


class _Carrier(BaseModel):
    """A request body or a response: content keyed by media type."""

    model_config = ConfigDict(extra="ignore")

    content: dict[str, _Media] = Field(default_factory=dict)


class _Operation(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    request_body: _Carrier | None = Field(default=None, alias="requestBody")
    responses: dict[str, _Carrier] = Field(default_factory=dict)


class _Spec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    openapi: str
    paths: dict[str, dict[str, _Operation]] = Field(default_factory=dict)
    components: dict[str, dict[str, _Node]] = Field(default_factory=dict)


_Node.model_rebuild()


# ── reachability: which component schemas the SDK's operations actually touch ─────────────────────


def _ref_name(ref: str) -> str:
    raw = ref.rsplit("/", 1)[-1]
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in raw)
    return cleaned if cleaned[:1].isalpha() or cleaned[:1] == "_" else f"S_{cleaned}"


def _child_nodes(node: _Node) -> list[_Node]:
    children: list[_Node] = []
    for group in (node.any_of, node.one_of, node.all_of):
        if group is not None:
            children.extend(group)
    if node.items is not None:
        children.append(node.items)
    if node.properties is not None:
        children.extend(node.properties.values())
    if isinstance(node.additional_properties, _Node):
        children.append(node.additional_properties)
    return children


def _reachable(roots: list[_Node], schemas: dict[str, _Node]) -> dict[str, _Node]:
    """Every component schema the roots reach, by name. Cycles terminate: a name is visited once."""
    found: dict[str, _Node] = {}
    queue = list(roots)
    while queue:
        node = queue.pop()
        if node.ref is not None:
            name = _ref_name(node.ref)
            if name not in found:
                target = schemas.get(name)
                if target is None:
                    continue
                found[name] = target
                queue.append(target)
            continue
        queue.extend(_child_nodes(node))
    return found


def _operation_roots(spec: _Spec) -> tuple[list[_Node], list[tuple[str, str]]]:
    roots: list[_Node] = []
    resolved: list[tuple[str, str]] = []
    for verb, path in SDK_OPERATIONS:
        operation = spec.paths.get(path, {}).get(verb)
        if operation is None:
            continue
        resolved.append((verb, path))
        carriers: list[_Carrier] = [] if operation.request_body is None else [operation.request_body]
        carriers.extend(carrier for status, carrier in sorted(operation.responses.items()) if status.startswith("2"))
        for carrier in carriers:
            for media in carrier.content.values():
                if media.schema_ is not None:
                    roots.append(media.schema_)
    return roots, resolved


# ── rendering ────────────────────────────────────────────────────────────────────────────────────

_PRIMITIVES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "null": "None"}


def _literal(value: str | int) -> str:
    """A literal member, spelled the way ruff format spells one: double quotes for a string."""
    return f'"{value}"' if isinstance(value, str) else str(value)


def _snake(name: str) -> str:
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index > 0:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _render_type(node: _Node, uses: set[str]) -> str:
    if node.ref is not None:
        return _ref_name(node.ref)
    if node.const is not None:
        uses.add("Literal")
        return f"Literal[{_literal(node.const)}]"
    if node.enum is not None:
        uses.add("Literal")
        return f"Literal[{', '.join(_literal(value) for value in node.enum)}]"
    for group in (node.any_of, node.one_of):
        if group:
            members = [_render_type(member, uses) for member in group]
            unique = list(dict.fromkeys(members))
            return " | ".join(unique)
    if node.all_of and len(node.all_of) == 1:
        return _render_type(node.all_of[0], uses)
    kind = node.type_
    if isinstance(kind, list):
        members = [_PRIMITIVES.get(single, "JsonValue") for single in kind]
        uses.update({"JsonValue"} if "JsonValue" in members else set())
        return " | ".join(dict.fromkeys(members))
    if kind == "array":
        inner = "JsonValue" if node.items is None else _render_type(node.items, uses)
        if inner == "JsonValue":
            uses.add("JsonValue")
        return f"tuple[{inner}, ...]"
    if kind == "object":
        if isinstance(node.additional_properties, _Node):
            inner = _render_type(node.additional_properties, uses)
            return f"dict[str, {inner}]"
        uses.add("JsonValue")
        return "dict[str, JsonValue]"
    if kind in _PRIMITIVES:
        return _PRIMITIVES[str(kind)]
    uses.add("JsonValue")
    return "JsonValue"


def _is_model(node: _Node) -> bool:
    """A component is a model when it declares properties. Everything else is a type alias.

    The distinction is not cosmetic: the server's `type CheckReason = Literal[...]` and its
    `JsonValue` both arrive as named component schemas, and rendering either as an empty `BaseModel`
    would turn a closed vocabulary into an open object that accepts anything.
    """
    return bool(node.properties)


def _render_alias(name: str, node: _Node, uses: set[str]) -> str:
    """One component that is a vocabulary or a shape rather than an object, as a type alias."""
    rendered = _render_type(node, uses)
    if rendered == name:
        # A schema with nothing in it is "any JSON value", which is the name this module already
        # imports — the alias would read `X: TypeAlias = X`. The import is the declaration.
        return f"# components.schemas.{name} declares no shape of its own: it is the imported JsonValue."
    uses.add("TypeAlias")
    return f"{name}: TypeAlias = {rendered}"


def _render_model(name: str, node: _Node, uses: set[str]) -> str:
    lines = [f"class {name}(BaseModel):"]
    description = (node.description or "").strip().splitlines()
    lines.append(f'    """{description[0] if description else f"Generated from components.schemas.{name}."}"""')
    lines.append("")
    lines.append('    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)')
    lines.append("")
    properties = node.properties or {}
    if not properties:
        uses.add("JsonValue")
        lines.append("    # No declared properties: the schema is an open object on the wire.")
        lines.append("    extra: dict[str, JsonValue] = Field(default_factory=dict)")
        return "\n".join(lines)
    required = set(node.required or ())
    for wire_name, prop in properties.items():
        python_name = _snake(wire_name)
        rendered = _render_type(prop, uses)
        if wire_name in required:
            declaration = rendered
            default = ""
        else:
            declaration = rendered if "None" in rendered.split(" | ") else f"{rendered} | None"
            default = " = None"
        if python_name == wire_name:
            lines.append(f"    {python_name}: {declaration}{default}")
        else:
            uses.add("Field")
            prefix = "None, " if default else ""
            lines.append(f'    {python_name}: {declaration} = Field({prefix}alias="{wire_name}")')
    return "\n".join(lines)


def _header(digest: str, *, ready: bool) -> list[str]:
    lines = [
        '"""GENERATED by scripts/generate_wire.py — do not edit; run `just contract-refresh`.',
        "",
        f"Source: {SNAPSHOT_NAME} at {digest}.",
        "",
    ]
    if ready:
        lines += [
            "Every model below is the platform's own schema for one of the operations the SDK calls,",
            "generated from the guarded snapshot. Hand-editing this file puts it permanently at odds",
            "with its generator; `just contract-check` fails on any drift.",
        ]
    else:
        lines += [
            "PLACEHOLDER. The snapshot declares none of the operations the SDK calls, so there is",
            "nothing to generate. Until it does, the request bodies and the assessment parsing live in the hand-written",
            "hajer/_payload.py, which says so, and `hajer.wire_source()` reports which of the two is",
            "in force. Nothing else in the SDK waits on this file.",
        ]
    lines += ['"""', ""]
    return lines


def render(snapshot_bytes: bytes) -> str:
    digest = f"sha256:{hashlib.sha256(snapshot_bytes).hexdigest()}"
    spec = _Spec.model_validate_json(snapshot_bytes)
    if not spec.openapi.startswith("3."):
        raise ValueError(f"the snapshot declares openapi={spec.openapi!r}; this generator reads 3.x")
    schemas = spec.components.get("schemas", {})
    roots, resolved = _operation_roots(spec)
    reachable = _reachable(roots, schemas)

    uses: set[str] = set()
    models = sorted(name for name in reachable if _is_model(reachable[name]))
    aliases = sorted(name for name in reachable if not _is_model(reachable[name]))
    rendered_models = [_render_model(name, reachable[name], uses) for name in models]
    rendered_aliases = [_render_alias(name, reachable[name], uses) for name in aliases]
    # An alias that names a model has to follow it: unlike a class body, the right-hand side of an
    # assignment is evaluated when the module is imported.
    forward = [text for text in rendered_aliases if any(model in text.split("=", 1)[-1] for model in models)]
    bodies = [text for text in rendered_aliases if text not in forward] + rendered_models + forward
    ready = bool(bodies)

    lines = _header(digest, ready=ready)
    lines.append("from __future__ import annotations")
    lines.append("")
    typing_imports = ["Final", *sorted({name for name in ("Literal", "TypeAlias") if name in uses})]
    lines.append(f"from typing import {', '.join(typing_imports)}")
    if ready:
        pydantic_imports = ["BaseModel", "ConfigDict"] + (["Field"] if "Field" in uses else [])
        lines.append("")
        lines.append(f"from pydantic import {', '.join(pydantic_imports)}")
        if "JsonValue" in uses:
            lines.append("")
            lines.append("from hajer._json import JsonValue")
    lines.append("")
    lines.append(f'SNAPSHOT_DIGEST: Final[str] = "{digest}"')
    lines.append(f'SNAPSHOT_PATH: Final[str] = "{SNAPSHOT_NAME}"')
    operations = ", ".join(f'("{verb}", "{path}")' for verb, path in resolved)
    lines.append(f"OPERATIONS: Final[tuple[tuple[str, str], ...]] = ({operations}{',' if resolved else ''})")
    lines.append(f"WIRE_READY: Final[bool] = {ready}")
    for body in bodies:
        lines.append("")
        lines.append("")
        lines.append(body)
    for name in models:
        lines.append("")
        lines.append(f"{name}.model_rebuild()")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite hajer/_wire.py")
    mode.add_argument("--check", action="store_true", help="fail if hajer/_wire.py is stale or hand-edited")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT, help="the OpenAPI snapshot to read")
    args = parser.parse_args(argv)

    snapshot = Path(args.snapshot)
    if not snapshot.is_file():
        print(
            f"generate_wire: missing {snapshot} (point --snapshot at the platform's OpenAPI snapshot)", file=sys.stderr
        )
        return 1
    rendered = render(snapshot.read_bytes())

    if args.write:
        TARGET.write_text(rendered, encoding="utf-8")
        print(f"generate_wire: wrote {TARGET.relative_to(SDK_DIR)} from {snapshot}", file=sys.stderr)
        return 0

    if not TARGET.is_file():
        print(f"generate_wire: {TARGET} does not exist; {REMEDY}", file=sys.stderr)
        return 1
    current = TARGET.read_text(encoding="utf-8")
    if current == rendered:
        print("generate_wire: OK (hajer/_wire.py is what the snapshot produces)", file=sys.stderr)
        return 0
    diff = difflib.unified_diff(
        current.splitlines(keepends=True), rendered.splitlines(keepends=True), "committed", "generated"
    )
    sys.stderr.writelines(list(diff)[:40])
    print(f"generate_wire: hajer/_wire.py is stale or was hand-edited; {REMEDY}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

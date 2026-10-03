"""Explicit local proof input override; suite roots, binding digests and adapter verification stay authoritative.

`--hajer-temporary-local-executions DIR` (the pytest plugin) and `verify-adapters --temporary-local-executions DIR`
read a case's model-authored input from a private bundle the local proof owns, never from a committed sidecar: a
0700 directory outside the checkout and every Git ancestor, holding 0600 single-link files, scoped to one checkout
root and one suite's exact bytes. Every refusal is a fixed code and never quotes the input.

Each attempt that hands an input to a child (a case's `input.json`, an adapter check's `spec.json`) writes it with
`write_private`: 0600, never over an existing path, inside the attempt's own 0700 `TemporaryDirectory`, which an
ordinary interrupt (SIGINT: `KeyboardInterrupt`) unwinds.
"""

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Final, cast

from pydantic import JsonValue, TypeAdapter

_SCOPE = TypeAdapter[dict[str, JsonValue]](dict[str, JsonValue])
BINDING_MISMATCH: Final = "TEMPORARY_LOCAL_INPUT_BINDING_MISMATCH"


def _read(path: Path, directory: Path) -> bytes:
    # A refused proof configuration must fail collection, never fall back to an unrelated public sidecar.
    if any(item.is_symlink() for item in (directory, *directory.parents)):
        raise ValueError("TEMPORARY_LOCAL_INPUT_DIRECTORY_REFUSED")
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("TEMPORARY_LOCAL_INPUT_DIRECTORY_REFUSED")
    parent = os.open(directory.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in directory.parts[1:]:
            if part in {".", ".."}:
                raise ValueError("TEMPORARY_LOCAL_INPUT_DIRECTORY_REFUSED")
            next_parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = next_parent
        current = os.fstat(parent)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("TEMPORARY_LOCAL_INPUT_DIRECTORY_REFUSED")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("TEMPORARY_LOCAL_INPUT_FILE_REFUSED")
        return handle.read()


def read_temporary_binding(directory: Path, suite: Path, digest: str) -> bytes:
    directory = directory.absolute()
    root = suite.parent.parent.parent.resolve()
    if directory.is_relative_to(root) or any((parent / ".git").exists() for parent in directory.parents):
        raise ValueError("TEMPORARY_LOCAL_INPUT_PUBLICATION_PATH_REFUSED")
    scope = _SCOPE.validate_json(_read(directory / "scope.json", directory))
    if scope != {"root": str(root), "suiteDigest": digest}:
        raise ValueError("TEMPORARY_LOCAL_INPUT_SCOPE_MISMATCH")
    return _read(directory / suite.name, directory)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(BINDING_MISMATCH)
    return value


def temporary_case_inputs(directory: Path, suite: Path) -> dict[str, tuple[str, JsonValue]]:
    """Each case of `suite` from its exact temporary binding: (the adapter id its execution names, its own input).

    The binding must name this suite's id, version and byte digest and exactly its cases, as the plugin requires
    (`_load.load_suite`). A malformed binding is one fixed code, never a message that could quote the input."""
    raw = suite.read_bytes()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    payload = read_temporary_binding(directory, suite, digest)
    try:
        document = _object(cast(JsonValue, json.loads(raw)))
        binding = _object(cast(JsonValue, json.loads(payload)))
        members = document.get("members")
        cases = _object(binding.get("cases"))
        wanted = {
            str(_object(_object(member).get("case")).get("id"))
            for member in (members if isinstance(members, list) else [])
        }
        identity = (binding.get("suiteId"), binding.get("suiteVersion"), binding.get("suiteDigest"))
        if identity != (document.get("suiteId"), document.get("version"), digest) or set(cases) != wanted:
            raise ValueError(BINDING_MISMATCH)
        found: dict[str, tuple[str, JsonValue]] = {}
        for case_id, value in cases.items():
            execution = _object(value)
            named = _object(execution.get("adapter")).get("adapterId")
            if not isinstance(named, str) or "input" not in execution:
                raise ValueError(BINDING_MISMATCH)
            found[case_id] = (named, execution["input"])
    except ValueError:
        raise ValueError(BINDING_MISMATCH) from None
    return found


def write_private(path: Path, text: str) -> None:
    """One attempt's copy of a case input: created 0600, never over an existing path or through a link."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)

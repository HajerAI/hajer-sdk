"""`platform` answers from values the parent recorded, so asking it never starts a process.

Model SDK clients build their platform headers from the `platform` module before their first request (openai and
anthropic both call `platform.system()`, `platform.platform()`, `platform.machine()`, `python_implementation()` and
`python_version()`), and on macOS and Linux `platform.processor()` runs `uname -p` and `platform.platform()` runs
`file` on the interpreter. The guard refuses every process, so the first model call of a replay was BLOCKED_EFFECT
before it reached the recorded-response matcher.

The parent, which may start processes, records this host's answers in the case spec (`platform`). Before the guard is
armed, `prime` seeds `platform`'s own caches with them: the cached `uname()` result (with its lazily computed
`processor` already filled in) and the cached `platform()` strings. `processor()`, `uname()`, `platform()`,
`machine()`, `system()`, `release()`, `version()` and `node()` then answer from memory. Nothing else changes: the audit
hook still refuses every process, so an application that runs `uname -p` itself is still BLOCKED_EFFECT. A spec
without recorded values (a replay run in the customer's own CI) is primed with this host's answers, computed without
a process before the guard is armed; the receipt says which values the child answered with.

The worker's hostname never reaches the child: the parent records none, and `node()` (and `uname().node`) is always
`REPLAY_NODE`, whatever a spec says, so it cannot surface in an attempt's output or in the stored run.
"""

from __future__ import annotations

import platform
from pathlib import Path

from pydantic import JsonValue

_UNAME = ("system", "node", "release", "version", "machine")
# What `platform.node()` answers in every primed replay child, in place of the worker's hostname.
REPLAY_NODE = "hajer-replay"


def _binary_description(target: str, default: str = "") -> str:
    """Recognize Mach-O without `file`: CPython 3.13 includes its linkage in platform().

    Older Python versions ignore this description's Mach-O marker and retain their older spelling.
    The magic is the four-byte file-format signature, including universal binaries, not a scan budget.
    """
    try:
        with Path(target).open("rb") as stream:
            magic = stream.read(4)
    except OSError:
        return default
    if magic in {
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }:
        return "Mach-O executable"
    return default


def _recorded(value: JsonValue) -> dict[str, str] | None:
    """The spec's recorded answers, or None unless every one is a string."""
    if not isinstance(value, dict):
        return None
    names = ("system", "release", "version", "machine", "processor", "platform", "platformTerse")
    found = {name: value.get(name) for name in names}
    strings = {name: item for name, item in found.items() if isinstance(item, str)}
    return {**strings, "node": REPLAY_NODE} if len(strings) == len(names) else None


def _seed_uname(values: dict[str, str]) -> None:
    """The cached `uname()` result, its lazily computed `processor` (a `functools.cached_property`, stored in the
    instance's `__dict__`) already filled in."""
    result = platform.uname_result(*(values[name] for name in _UNAME))
    vars(result)["processor"] = values["processor"]
    setattr(platform, "_uname_cache", result)  # noqa: B010 - the standard library's own cache, private by name only


def _processor(system: str, machine: str) -> str:
    """What `uname -p` prints, from the machine name instead of the process: macOS names the CPU family (`arm`,
    `i386`); elsewhere it is the machine, as the coreutils Ubuntu and Fedora ship print it."""
    if system == "Darwin":
        return {"arm64": "arm", "x86_64": "i386"}.get(machine, machine)
    return machine


def _derived() -> dict[str, str] | None:
    """This host's answers computed in-process, for a spec that records none (a replay in the customer's own CI).

    `uname()` itself reads `os.uname()`; only its `processor` would run `uname -p`, so that is derived and seeded
    first. `platform()` then reads it from the cache, and its one other process — `file` on the interpreter, for the
    bits and linkage `architecture()` reports on macOS — is replaced with a direct read of its format signature.
    Unknown formats keep the defaults `architecture()` already supplies."""
    syscmd: object = getattr(platform, "_syscmd_file", None)
    if syscmd is None:
        return None
    # By attribute only: indexing, slicing or unpacking a `uname_result` evaluates its `processor`, i.e. `uname -p`.
    current = platform.uname()
    values = {name: getattr(current, name) for name in ("system", "release", "version", "machine")}
    values |= {"processor": _processor(current.system, current.machine), "node": REPLAY_NODE}
    _seed_uname(values)

    setattr(platform, "_syscmd_file", _binary_description)  # noqa: B010 - restored below, before the guard is armed
    try:
        values |= {"platform": platform.platform(), "platformTerse": platform.platform(terse=True)}
    finally:
        setattr(platform, "_syscmd_file", syscmd)  # noqa: B010
    return values


def prime(value: JsonValue) -> dict[str, JsonValue] | None:
    """Seed `platform`'s caches before the guard is armed; the answers seeded, or None when they cannot be.

    The parent's recorded answers when the spec carries them; otherwise this host's own, computed in-process
    (`_derived`), because a replay in the customer's CI has no parent to record them.
    `node` is `REPLAY_NODE` either way. Both caches are private to the standard library, so a Python without
    them is left unseeded rather than guessed at.
    """
    cache: object = getattr(platform, "_platform_cache", None)
    if not isinstance(cache, dict) or not hasattr(platform, "_uname_cache"):
        return None
    values = _recorded(value) or _derived()
    if values is None:
        return None
    _seed_uname(values)
    for aliased in (False, True):
        cache[(aliased, False)] = values["platform"]
        cache[(aliased, True)] = values["platformTerse"]
    return dict(values)

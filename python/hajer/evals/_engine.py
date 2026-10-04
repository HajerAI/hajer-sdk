"""The pinned eval engine: where it is installed, whether it is ready, and how it gets there.

`hajer eval` drives upstream promptfoo at one exact version. The pin is **not** a string in this module: it is
`engine/package.json` and `engine/package-lock.json`, shipped inside the wheel as package data and read back
through `importlib.resources`. One source of truth, so bumping the engine is an edit to two JSON files that
`tests/test_evals_engine.py` holds to each other, and never a Python constant that could drift from them.

The engine is installed once per lockfile, under `<cache_dir>/engine/<digest of the lockfile>/`, with
`npm ci` against the shipped lockfile — deterministic, and keyed by the lockfile so a bump lands in a fresh
directory rather than mutating the one a running eval may be using. A directory is *ready* when its `.ready`
marker names the pinned version and the entrypoint exists; a stale marker is a reinstall, never a guess.

Nothing here raises to its caller. Every way the engine can fail to be ready — no Node, old Node, no npm, not
installed, an install that failed, hung or produced the wrong version — comes back as an `EngineUnavailable`
whose `detail` is one sentence naming what to install, because the person reading it is at a terminal with a
failing CI job, not reading a traceback. The one exception is `pinned()`, which raises when the package data
itself is missing: that is a broken installation of this package, and the fix is to reinstall it.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.resources
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from hajer._json import JsonObject, JsonValue
from hajer._settings import HajerSettings

#: The npm package the pin names, under `dependencies` in `package.json`.
ENGINE_PACKAGE: Final[str] = "promptfoo"
#: The engine's CLI entry inside an installed copy, relative to the install directory. promptfoo's own layout;
#: `node <entrypoint> --version` prints the version, which is the smoke test an install has to pass.
ENTRYPOINT_PARTS: Final[tuple[str, ...]] = ("node_modules", ENGINE_PACKAGE, "dist", "src", "entrypoint.js")
#: The two files that are the pin, copied verbatim into the install directory before `npm ci` runs there.
PIN_FILES: Final[tuple[str, str]] = ("package.json", "package-lock.json")
#: The marker a finished install writes, holding the version the smoke test reported. Its presence with the right
#: text is what lets every later run skip npm entirely.
READY_MARKER: Final[str] = ".ready"
#: How many hex characters of the lockfile digest name the install directory. A naming width, not a bound on
#: anything the SDK does: 16 hex characters is 64 bits, which is enough that two lockfiles this package ever
#: ships will not share a directory, and short enough to read in a path.
ENGINE_DIR_DIGEST_CHARS: Final[int] = 16
#: `npm ci` as the engine needs it: the lockfile's exact tree, production dependencies only, and none of the
#: chatter (audit, funding, the update notice) that would land in a CI log and mean nothing to the person reading it.
NPM_CI_FLAGS: Final[tuple[str, ...]] = ("ci", "--omit=dev", "--no-audit", "--no-fund", "--loglevel=error")

#: `engines.node` as `package.json` spells a floor: `>=X.Y.Z`, optional whitespace after the operator.
_NODE_FLOOR = re.compile(r"^\s*>=\s*(\d+)\.(\d+)\.(\d+)\s*$")
#: What `node --version` prints: `v22.23.2`, possibly with a prerelease suffix we do not need.
_NODE_VERSION = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)")

Reason = Literal[
    "NODE_MISSING",
    "NODE_TOO_OLD",
    "NPM_MISSING",
    "NOT_INSTALLED",
    "INSTALL_FAILED",
    "INSTALL_TIMEOUT",
    "SMOKE_FAILED",
]
#: The subset of `subprocess.run` this module calls, as a seam: tests hand in a fake that never starts a process.
Run = Callable[..., subprocess.CompletedProcess[str]]
#: `shutil.which`, as a seam for the same reason.
Which = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class Pin:
    """What the package data says the engine is: the version, the Node floor, and the lockfile that fixes the tree."""

    version: str
    node_floor: tuple[int, int, int]
    lockfile_digest: str
    package_json: Path
    lockfile: Path


@dataclass(frozen=True, slots=True)
class EngineReady:
    """An engine `hajer eval` can start right now: `node entrypoint …` is the command."""

    entrypoint: Path
    node: str
    node_version: str
    version: str
    directory: Path
    installed_now: bool


@dataclass(frozen=True, slots=True)
class EngineUnavailable:
    """Why the engine cannot be started, and the one sentence that fixes it."""

    reason: Reason
    detail: str


def _dotted(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


@functools.cache
def pinned() -> Pin:
    """Read the pin out of the package data, once per process.

    Raises `FileNotFoundError` when either file is missing and `ValueError` when it does not carry the fields the
    pin needs: both mean this installation of the package is broken (a wheel built without its package data), and
    the fix is to reinstall it. `ensure_engine` and `engine_status` translate either into an `EngineUnavailable`
    rather than letting it surface, so a caller of theirs never sees a traceback.
    """
    root = importlib.resources.files("hajer.evals") / "engine"
    if not isinstance(root, Path):
        # `uv_build` wheels install as directories, so this is the one shape the package data ever has; a zipped
        # install could not host an `npm ci` anyway.
        raise FileNotFoundError(f"the engine pin is not on the file system ({root!r}); reinstall the hajer package")
    package_json = root / PIN_FILES[0]
    lockfile = root / PIN_FILES[1]
    manifest: JsonValue = json.loads(package_json.read_bytes())
    if not isinstance(manifest, dict):
        raise ValueError(f"{package_json} is not a JSON object")
    dependencies = manifest.get("dependencies")
    engines = manifest.get("engines")
    version = dependencies.get(ENGINE_PACKAGE) if isinstance(dependencies, dict) else None
    floor = engines.get("node") if isinstance(engines, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError(f"{package_json} pins no `dependencies.{ENGINE_PACKAGE}` version")
    matched = _NODE_FLOOR.match(floor) if isinstance(floor, str) else None
    if matched is None:
        raise ValueError(f"{package_json} has no `engines.node` floor of the form >=X.Y.Z")
    return Pin(
        version=version,
        node_floor=(int(matched.group(1)), int(matched.group(2)), int(matched.group(3))),
        lockfile_digest=hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        package_json=package_json,
        lockfile=lockfile,
    )


def engine_dir(settings: HajerSettings, pin: Pin | None = None) -> Path:
    """`<cache_dir>/engine/<lockfile digest prefix>`: one directory per lockfile, so a bump never mutates an install in use."""
    resolved = pinned() if pin is None else pin
    return Path(settings.cache_dir) / "engine" / resolved.lockfile_digest[:ENGINE_DIR_DIGEST_CHARS]


def entrypoint(directory: Path) -> Path:
    """The engine's CLI entry inside an install directory."""
    return directory.joinpath(*ENTRYPOINT_PARTS)


def node_version(node: str, *, run: Run = subprocess.run) -> tuple[int, int, int] | None:
    """What `node --version` reports, as a triple — or None when it reports nothing a floor can be compared to.

    None rather than a raise, for a `node` on PATH that is not Node at all, cannot start, or prints something
    else: the caller turns that into the sentence, and a doctor that crashed on a broken Node would be no doctor.
    """
    try:
        completed = run([node, "--version"], capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    matched = _NODE_VERSION.match(completed.stdout)
    if matched is None:
        return None
    return (int(matched.group(1)), int(matched.group(2)), int(matched.group(3)))


def _locate_node(*, which: Which, run: Run) -> tuple[str | None, tuple[int, int, int] | None]:
    """The `node` on PATH and the version it reports: the two facts the floor check and `doctor` both need."""
    node = which("node")
    return node, None if node is None else node_version(node, run=run)


def _node_refusal(node: str | None, found: tuple[int, int, int] | None, pin: Pin) -> EngineUnavailable:
    """The sentence for a Node that cannot run the engine.

    Called only once the caller has established one of the three: no `node` on PATH, a `node` that reports no
    version, or a version below the floor — so the last case needs no comparison of its own.
    """
    floor = _dotted(pin.node_floor)
    if node is None:
        return EngineUnavailable(
            "NODE_MISSING",
            f"Node.js was not found on PATH; install Node.js {floor} or newer (https://nodejs.org) to run the eval engine.",
        )
    if found is None:
        return EngineUnavailable(
            "NODE_MISSING",
            f"{node} did not report a Node.js version; install Node.js {floor} or newer (https://nodejs.org) "
            "and make sure it is the `node` on PATH.",
        )
    return EngineUnavailable(
        "NODE_TOO_OLD",
        f"Node.js {_dotted(found)} at {node} is older than the {floor} the eval engine needs; "
        f"install Node.js {floor} or newer (https://nodejs.org).",
    )


def _pin_or_unavailable() -> Pin | EngineUnavailable:
    """The pin, or the sentence for an installation that lost its package data."""
    try:
        return pinned()
    except (OSError, ValueError) as error:
        return EngineUnavailable(
            "INSTALL_FAILED",
            f"the eval engine pin shipped inside the hajer package is missing or unreadable ({error}); "
            "reinstall the hajer package.",
        )


def _not_installed(directory: Path, pin: Pin) -> EngineUnavailable:
    return EngineUnavailable(
        "NOT_INSTALLED",
        f"the eval engine ({ENGINE_PACKAGE} {pin.version}) is not installed under {directory}; "
        "run `hajer eval` once with access to the npm registry to install it.",
    )


def _is_ready(directory: Path, pin: Pin) -> bool:
    """A finished install of exactly this version: the marker names it and the entrypoint is there."""
    try:
        marker = (directory / READY_MARKER).read_text(encoding="utf-8")
        return marker.strip() == pin.version and entrypoint(directory).is_file()
    except OSError:
        return False


def _ready(node: str, found: tuple[int, int, int], directory: Path, pin: Pin, *, installed_now: bool) -> EngineReady:
    return EngineReady(
        entrypoint=entrypoint(directory),
        node=node,
        node_version=_dotted(found),
        version=pin.version,
        directory=directory,
        installed_now=installed_now,
    )


@contextmanager
def _exclusive(lock_path: Path) -> Generator[None, None, None]:
    """One installer per engine directory.

    Two `hajer eval` processes starting cold on the same machine (a test matrix, say) must not both run `npm ci`
    into one directory; the second waits, then finds `.ready` and skips npm. An advisory `flock` on a sibling file
    does that on POSIX; a platform without `fcntl` proceeds unlocked, which is the behaviour before locking rather
    than a failure.
    """
    try:
        import fcntl  # noqa: PLC0415 - POSIX-only; imported where it is used so this module loads on a platform without it
    except ImportError:
        yield
        return
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _last_line(text: str) -> str:
    """The last non-empty line of a process's stderr: npm puts the sentence that matters there."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _stage(directory: Path, pin: Pin) -> None:
    """Create the install directory holding exactly the two files `npm ci` needs."""
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pin.package_json, directory / PIN_FILES[0])
    shutil.copyfile(pin.lockfile, directory / PIN_FILES[1])


def _install(
    node: str,
    found: tuple[int, int, int],
    npm: str,
    directory: Path,
    pin: Pin,
    settings: HajerSettings,
    *,
    run: Run,
    environment: Mapping[str, str] | None,
) -> EngineReady | EngineUnavailable:
    """`npm ci`, then the smoke test, then the marker — with the lock held, so a concurrent installer waits."""
    env = None if environment is None else {**environment, "NO_UPDATE_NOTIFIER": "1"}
    with _exclusive(directory.with_name(directory.name + ".lock")):
        if _is_ready(directory, pin):
            return _ready(node, found, directory, pin, installed_now=False)
        try:
            completed = run(
                [npm, *NPM_CI_FLAGS],
                cwd=directory,
                env=env,
                timeout=settings.eval_install_timeout_s,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return EngineUnavailable(
                "INSTALL_TIMEOUT",
                f"`npm ci` of the eval engine ({ENGINE_PACKAGE} {pin.version}) did not finish within "
                f"{settings.eval_install_timeout_s} s; check access to the npm registry, "
                "or raise HAJER_EVAL_INSTALL_TIMEOUT_S on a slow connection.",
            )
        except OSError as error:
            return EngineUnavailable("INSTALL_FAILED", f"`npm ci` of the eval engine could not be started: {error}.")
        if completed.returncode != 0:
            said = _last_line(completed.stderr) or f"exit status {completed.returncode}"
            return EngineUnavailable(
                "INSTALL_FAILED",
                f"`npm ci` of the eval engine ({ENGINE_PACKAGE} {pin.version}) failed in {directory}: {said}",
            )
        entry = entrypoint(directory)
        try:
            smoke = run(
                [node, str(entry), "--version"],
                cwd=directory,
                env=env,
                timeout=settings.eval_install_timeout_s,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return EngineUnavailable(
                "SMOKE_FAILED", f"the installed eval engine at {entry} could not be started: {error}."
            )
        reported = smoke.stdout.strip().removeprefix("v")
        if smoke.returncode != 0 or reported != pin.version:
            said = reported or _last_line(smoke.stderr) or f"exit status {smoke.returncode}"
            return EngineUnavailable(
                "SMOKE_FAILED",
                f"the installed eval engine at {entry} reported `{said}` instead of {pin.version}; "
                f"remove {directory} and run again.",
            )
        (directory / READY_MARKER).write_text(pin.version + "\n", encoding="utf-8")
    return _ready(node, found, directory, pin, installed_now=True)


def ensure_engine(
    settings: HajerSettings,
    *,
    install: bool = True,
    which: Which = shutil.which,
    run: Run = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> EngineReady | EngineUnavailable:
    """An engine that can be started, installing it if `install` allows — or the one sentence that says why not.

    `environment` is what `npm ci` and the smoke test run in; the caller owns it because this module may not read
    the process environment (`hajer eval` hands in `eval_engine_environment`, which already drops the Hajer key).
    None runs them in this process's environment untouched.
    """
    pin = _pin_or_unavailable()
    if isinstance(pin, EngineUnavailable):
        return pin
    node, found = _locate_node(which=which, run=run)
    if node is None or found is None or found < pin.node_floor:
        return _node_refusal(node, found, pin)
    directory = engine_dir(settings, pin)
    if _is_ready(directory, pin):
        return _ready(node, found, directory, pin, installed_now=False)
    if not install:
        return _not_installed(directory, pin)
    npm = which("npm")
    if npm is None:
        return EngineUnavailable(
            "NPM_MISSING",
            f"npm was not found on PATH; it ships with Node.js {_dotted(pin.node_floor)} or newer "
            "(https://nodejs.org), which the eval engine is installed with.",
        )
    try:
        _stage(directory, pin)
        return _install(node, found, npm, directory, pin, settings, run=run, environment=environment)
    except OSError as error:
        return EngineUnavailable(
            "INSTALL_FAILED", f"the eval engine could not be installed under {directory}: {error}."
        )


def engine_status(settings: HajerSettings, *, which: Which = shutil.which, run: Run = subprocess.run) -> JsonObject:
    """What `hajer doctor` prints about the engine: every fact `ensure_engine` would act on, none of its actions.

    Read-only — it never installs. `ready` is whether `hajer eval` would start the engine right now without
    touching npm; `installed` is only whether the entrypoint exists, which a half-finished install also satisfies.
    """
    pin = _pin_or_unavailable()
    if isinstance(pin, EngineUnavailable):
        return {
            "node": which("node"),
            "nodeVersion": None,
            "nodeFloor": None,
            "nodeMeetsFloor": False,
            "npm": which("npm"),
            "pinnedVersion": None,
            "lockfileDigest": None,
            "engineDir": None,
            "installed": False,
            "ready": False,
            "reason": pin.reason,
            "detail": pin.detail,
        }
    node, found = _locate_node(which=which, run=run)
    meets_floor = node is not None and found is not None and found >= pin.node_floor
    directory = engine_dir(settings, pin)
    unavailable: EngineUnavailable | None
    if not meets_floor:
        unavailable = _node_refusal(node, found, pin)
    elif _is_ready(directory, pin):
        unavailable = None
    else:
        unavailable = _not_installed(directory, pin)
    return {
        "node": node,
        "nodeVersion": None if found is None else _dotted(found),
        "nodeFloor": _dotted(pin.node_floor),
        "nodeMeetsFloor": meets_floor,
        "npm": which("npm"),
        "pinnedVersion": pin.version,
        "lockfileDigest": pin.lockfile_digest,
        "engineDir": str(directory),
        "installed": entrypoint(directory).is_file(),
        "ready": unavailable is None,
        "reason": None if unavailable is None else unavailable.reason,
        "detail": None if unavailable is None else unavailable.detail,
    }

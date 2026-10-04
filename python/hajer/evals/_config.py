"""Where a run lives and how the engine is told about it: the run directory, the overlay config and the receiver port.

`hajer eval` never edits the user's configuration. It writes one small JSON config of its own — tracing on,
the Hajer hook attached — into the run directory under the cache and passes it to the engine *after* the user's
files, so promptfoo's base path (the first config's directory) stays the user's and every relative `file://`
in their config keeps resolving. Nothing lands in the user's tree.
"""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path
from typing import Final

from hajer._json import JsonObject
from hajer._settings import HajerSettings

#: promptfoo's own default configuration names, in the order it looks for them, so `hajer eval` with no `-c`
#: runs the same file `promptfoo eval` would. Passing any `-c` turns that auto-discovery off in promptfoo,
#: which is why the discovered file is passed explicitly.
CONFIG_NAMES: Final[tuple[str, ...]] = (
    "promptfooconfig.yaml",
    "promptfooconfig.yml",
    "promptfooconfig.json",
    "promptfooconfig.js",
    "promptfooconfig.mjs",
    "promptfooconfig.cjs",
    "promptfooconfig.ts",
)
OVERLAY_NAME: Final[str] = "overlay.json"
RESULTS_NAME: Final[str] = "results.json"
PAYLOAD_NAME: Final[str] = "payload.json"
HOOK_REPORT_NAME: Final[str] = "hook-report.json"
#: The hook function's name inside `hajer/evals/_hook_entry.py`; promptfoo runs a function named like a hook
#: only for that hook.
HOOK_FUNCTION: Final[str] = "beforeAll"


def discover_configs(explicit: list[str], cwd: Path) -> tuple[Path, ...]:
    """The user's config files, resolved: the ones named with `-c`, else promptfoo's default in `cwd`, else none."""
    if explicit:
        return tuple((cwd / item).resolve() if not Path(item).is_absolute() else Path(item) for item in explicit)
    for name in CONFIG_NAMES:
        candidate = cwd / name
        if candidate.is_file():
            return (candidate.resolve(),)
    return ()


def runs_directory(settings: HajerSettings) -> Path:
    return Path(settings.cache_dir) / "runs"


def run_directory(settings: HajerSettings, run_id: str) -> Path:
    """`<cache>/runs/<run id>/`, created, after older runs past `HAJER_EVAL_RUNS_KEEP` are removed."""
    runs = runs_directory(settings)
    runs.mkdir(parents=True, exist_ok=True)
    _prune(runs, keep=settings.eval_runs_keep - 1)
    directory = runs / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _prune(runs: Path, *, keep: int) -> None:
    """Remove the oldest run directories beyond `keep`; a directory that cannot be removed is left alone."""
    try:
        existing = sorted((item for item in runs.iterdir() if item.is_dir()), key=lambda item: item.stat().st_mtime)
    except OSError:
        return
    for stale in existing[: max(0, len(existing) - keep)]:
        shutil.rmtree(stale, ignore_errors=True)


def free_port(preferred: int) -> int:
    """`preferred` when it can be bound on loopback, else a port the kernel hands out: never a silent clash."""
    bound = _bindable(preferred)
    if bound is not None:
        return bound
    handed_out = _bindable(0)
    return handed_out if handed_out is not None else preferred


def _bindable(port: int) -> int | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
            return int(probe.getsockname()[1])
    except OSError:
        return None


def overlay_document(*, port: int, hook_entry: Path) -> JsonObject:
    """The config `hajer eval` adds: tracing to the loopback receiver, and the Hajer `beforeAll` hook.

    `prompts: []` is load-bearing: promptfoo gives every config file that has **no** `prompts` key the default
    prompt `{{prompt}}` before merging, which would run the whole suite a second time against it. An empty list
    is a value, so the overlay contributes no prompt of its own and the user's prompts are the only ones.
    """
    return {
        "prompts": [],
        "tracing": {"enabled": True, "otlp": {"http": {"enabled": True, "host": "127.0.0.1", "port": port}}},
        "extensions": [f"file://{hook_entry}:{HOOK_FUNCTION}"],
    }


def write_overlay(directory: Path, *, port: int, hook_entry: Path) -> Path:
    target = directory / OVERLAY_NAME
    target.write_text(json.dumps(overlay_document(port=port, hook_entry=hook_entry), indent=2) + "\n", encoding="utf-8")
    return target

"""One adapter called once under the full replay guards with a fake model: which model call does it reach?

Entered from `_reach_boot.py` (`python -I -S -B …/hajer/replay/_reach_boot.py SPEC RECEIPT EGRESS_LOG`) by
`python -m hajer verify-adapters`. The guards are the replay's own (`_guard.Sandbox`, `_hooks.install`,
`_database.install_database_guard`) and nothing is allowed out: no provider endpoint, no Hajer endpoint, no tunnel.
The only answers are the fake model's (`_fake`), served in-process to any request for a model endpoint the SDK
wraps. While each one is served, the application frames that made it are recorded (`_frame_context.frames_for_call`,
which carries the frames across the asyncio tasks a framework fans out to): that is the site the call came from.

The input is the spec's (an authored case input), else a stand-in built from the callable's signature
(`_standin`). The receipt names what was reached, what was refused (by kind and host), the class of an exception the
application raised (never its message) and an entry refusal; the parent judges it (`hajer._verify_adapters`).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import site
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import cast

import httpx
from pydantic import JsonValue

from hajer._frame_context import frames_for_call, install_task_frames
from hajer.replay._boot import site_directories
from hajer.replay._database import install_database_guard
from hajer.replay._entry import EntryUnavailable, prepare_call, target_of
from hajer.replay._fake import fake_answers
from hajer.replay._guard import Sandbox, recordings_from
from hajer.replay._hooks import install
from hajer.replay._platform import prime
from hajer.replay._standin import standin

RECEIPT_SCHEMA = "hajer-adapter-reach-v1"


class _Frames:
    """Every model request's application frames, relative to the repository root, innermost first."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.calls: list[JsonValue] = []

    def observe(self, request: httpx.Request) -> None:
        frames: list[JsonValue] = []
        for frame in frames_for_call():
            try:
                relative = Path(frame.file).resolve().relative_to(self._root)
            except ValueError:
                continue
            frames.append([relative.as_posix(), frame.line])
        self.calls.append({"host": request.url.host, "path": request.url.path, "frames": frames})


def _admit(project_root: Path) -> None:
    """After the guard: the interpreter's `.pth` hooks and the project's own directory on the path."""
    for directory in site_directories():
        site.addsitedir(directory)
    sys.path.insert(0, str(project_root))


async def _driven(coroutine: Coroutine[object, object, object]) -> object:
    install_task_frames()
    return await coroutine


def _exception_classes(error: BaseException) -> str:
    """Keep bounded causal types so a wrapper error does not hide setup failures; no messages or data."""
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(names) < 3:
        seen.add(id(current))
        kind = type(current)
        names.append(f"{kind.__module__}.{kind.__qualname__}")
        current = current.__cause__ or current.__context__
    return " caused by ".join(names)


def _call(adapter: dict[str, JsonValue], value: JsonValue | None) -> tuple[str | None, JsonValue]:
    """(the class of the exception the application raised, or None; the entry refusal, or None)."""
    try:
        target = target_of(adapter)
        prepared = prepare_call(adapter, standin(adapter, target) if value is None else value, target=target)
    except EntryUnavailable as error:
        return None, {"reason": error.reason, "detail": str(error)}
    except Exception as error:  # noqa: BLE001 - building the call ran the application's code (a factory, a setup)
        return _exception_classes(error), None
    try:
        result = prepared.target(*prepared.args, **prepared.kwargs)
        if inspect.isawaitable(result):
            asyncio.run(_driven(cast(Coroutine[object, object, object], result)))
    except Exception as error:  # noqa: BLE001 - the application's failure is an outcome, recorded by class only
        return _exception_classes(error), None
    return None, None


def _refused(egress: Path) -> list[JsonValue]:
    """Every refused egress event (kind by host): what the application tried that a replay never lets out."""
    lines = egress.read_text(encoding="utf-8").splitlines() if egress.is_file() else []
    events = [cast(dict[str, JsonValue], json.loads(line)) for line in lines if line.strip()]
    return [str(item.get("host")) for item in events if not item.get("allowed") and not item.get("served")]


def main(argv: list[str]) -> int:
    spec_path, receipt_path, egress_path = (Path(item) for item in argv)
    spec = cast(dict[str, JsonValue], json.loads(spec_path.read_text(encoding="utf-8")))
    root = Path(str(spec["repositoryRoot"])).resolve()
    frames = _Frames(root)
    answers = spec.get("answers")
    # Before the guard: `platform` answers from memory, so a provider client's platform headers start no process.
    prime(spec.get("platform"))
    provider = spec.get("provider")
    sandbox = Sandbox(
        provider=None,
        hajer=None,
        recordings=recordings_from(
            fake_answers(answers if isinstance(answers, int) else 1, provider if isinstance(provider, dict) else None)
        ),
        egress_log=egress_path,
        observer=frames.observe,
    )
    install(sandbox)
    install_database_guard(sandbox)
    _admit(Path(str(spec["projectRoot"])))
    adapter = cast(dict[str, JsonValue], spec["adapter"])
    raised, entry = _call(adapter, spec.get("input"))
    receipt: dict[str, JsonValue] = {
        "schema": RECEIPT_SCHEMA,
        "calls": frames.calls,
        "refusals": list(sandbox.record.refusals),
        "refusedHosts": _refused(egress_path),
        "exceptionClass": raised,
        "entry": entry,
    }
    descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True))
    return 0

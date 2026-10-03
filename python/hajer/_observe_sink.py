"""`HAJER_OBSERVE_SINK` — the same wrapped calls, written to a directory instead of to a socket.

**The failure this removes.** A customer's test suite may patch `httpx`'s transports for
the whole session, so that no test can reach a network. That patch is about *their* provider calls,
but it catches the SDK's flush as well: the wrapper records every call and the observations never
leave the process. The suite is the richest source of observed traffic a repository has — it exercises
the workflows with no spend and no side effects — and it was the one source the SDK could not export.

With `HAJER_OBSERVE_SINK=<dir>` set, every settled `WrappedCall` is appended to **one file per
process** under that directory and nothing is sent anywhere: no client is constructed, no socket is
opened, no credential is read. The document is the shape Hajer's observation export
already reads — `{"runId": …, "runs": [{"label": …, "wrappedCalls": [...]}]}` — so the export path from
a sunk suite is the export path from a runner receipt. Layer 1 redaction runs on a telemetry copy
before persistence, including raw captures; the application's response and in-memory call stay untouched.

**What a run entry is.** One per `hajer.scope()` that was open when the call settled, and one for
everything settled outside every scope (`process`). That is the honest grain: a scope is the operation
the application itself declared, and where an application declares none the SDK will not invent case
boundaries it cannot see. A reader that needs a finer unit has the call's own `startedAt`, caller
frames and request bytes in the entry.

**Durability.** The document is rewritten in full after every settled call, through a temporary file
and `os.replace`, so a reader never sees a half-written document and a killed process leaves behind
everything that had settled. An `atexit` hook writes once more at interpreter exit.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Final

from hajer._json import JsonObject, JsonValue
from hajer._paths import VERSION
from hajer._redact import build_policy, redact_document
from hajer._settings import HajerSettings
from hajer._wrap import WrappedCall, set_recorded_hook

#: The name of the document this writes. Not a new record: `observation_export.py --receipt` reads it.
SINK_SCHEMA: Final[str] = "observe-sink-v1"


class ObservationFileSink:
    """One process's wrapped calls, grouped by the operation that was open, on disk after each one."""

    __slots__ = ("_calls", "_document_path", "_lock", "_policy", "_run_id", "_temporary_path")

    def __init__(self, directory: Path) -> None:
        self._run_id = f"sink-{uuid.uuid4().hex}"
        directory.mkdir(parents=True, exist_ok=True)
        self._document_path = directory / f"hajer-observe-{os.getpid()}-{self._run_id[5:17]}.json"
        self._temporary_path = self._document_path.with_suffix(".writing")
        self._calls: dict[str, list[JsonObject]] = {}
        self._lock = threading.Lock()
        self._policy = build_policy()

    @property
    def path(self) -> Path:
        """Where this process writes. Read by tests and by whoever exports the directory afterwards."""
        return self._document_path

    @property
    def run_id(self) -> str:
        """This process's run id, which the exporter mints observation ids from."""
        return self._run_id

    def record(self, call: WrappedCall, label: str) -> None:
        """Append one settled call to its run entry and write the document."""
        redacted, _ = redact_document(call.to_wire(), policy=self._policy)
        if not isinstance(redacted, dict):  # pragma: no cover - a document keeps its container shape
            return
        with self._lock:
            self._calls.setdefault(label, []).append(redacted)
            document = self._document()
        self._write(document)

    def flush(self) -> None:
        """Write the document as it stands. The `atexit` hook, and a seam for a test."""
        with self._lock:
            document = self._document()
        self._write(document)

    def _document(self) -> JsonObject:
        runs: list[JsonValue] = [{"label": label, "wrappedCalls": list(calls)} for label, calls in self._calls.items()]
        return {"schemaVersion": SINK_SCHEMA, "sdkVersion": VERSION, "runId": self._run_id, "runs": runs}

    def _write(self, document: JsonObject) -> None:
        """Whole document, temporary file, `os.replace`: a reader never sees a partial write."""
        self._temporary_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(self._temporary_path, self._document_path)


class _Installation:
    """The one sink this process has, in a holder rather than a rebound module global."""

    __slots__ = ("sink",)

    def __init__(self) -> None:
        self.sink: ObservationFileSink | None = None


_INSTALLED = _Installation()


def installed() -> ObservationFileSink | None:
    """The sink this process installed, or `None` when `HAJER_OBSERVE_SINK` was unset."""
    return _INSTALLED.sink


def install_from_env(settings: HajerSettings | None = None) -> ObservationFileSink | None:
    """Install the file sink when the variable names a directory. Idempotent; no-op when it does not.

    Nothing is written until the first call settles: a process that imports `hajer` and never calls a
    model leaves an empty document behind at exit, and never a partial one.
    """
    resolved = HajerSettings.from_env() if settings is None else settings
    if not resolved.observe_sink:
        return None
    if _INSTALLED.sink is not None:
        return _INSTALLED.sink
    sink = ObservationFileSink(Path(resolved.observe_sink))
    _INSTALLED.sink = sink
    set_recorded_hook(sink.record)
    atexit.register(sink.flush)
    return sink


def uninstall() -> None:
    """Forget the sink and the hook. A test seam; a process never needs it."""
    _INSTALLED.sink = None
    set_recorded_hook(None)

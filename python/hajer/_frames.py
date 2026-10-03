"""Which of the customer's own callables was making this provider call.

A captured provider request says what was sent; it does not say **who sent it**. Without that, a
stored observation is a prompt with no address: the engine can read every sentence of it and cannot
say which declaration in the repository it belongs to, which is exactly the join the platform's OBSERVED rule
needs ("the HOT SPOT is the application frame the SDK recorded on the call").

The rule for "the application's own frames" is deliberately one a reader can check and one that
names no library. A frame is the application's when its file is **not** inside this SDK's own
package, **not** inside the interpreter's standard library, and **not** inside an installed-package
directory — the three locations `sysconfig` and `site` define for every Python installation there is.
Everything left is code somebody in this process wrote, and the innermost of those are the ones
nearest the call.

Two limitations, both stated rather than worked around:

* an application installed into site-packages non-editably has no frame outside those prefixes, and
  the record then carries none with `CALLER_FRAMES_UNRESOLVED`. An editable install points back at
  the source tree and is the ordinary case.
* the walk is bounded — `MAX_FRAMES_SCANNED` frames looked at, `MAX_CALLER_FRAMES` kept — because a
  deep framework stack is not a reason to spend unbounded time inside somebody's request path.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import site
import stat
import struct
import sys
import sysconfig
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Final

from hajer._json import JsonObject

#: How many stack frames are looked at before the walk stops. A framework that fans a call out
#: through a dozen layers is ordinary; one that needs more than this is one whose application frame
#: this SDK will honestly say it did not find.
MAX_FRAMES_SCANNED: Final[int] = 64
#: How many application frames one call records, innermost first. Eight is a call path a person
#: reads; past it the record is a stack trace, and a stack trace is not evidence about a hot spot.
MAX_CALLER_FRAMES: Final[int] = 8
#: How long one recorded frame's module or qualified name may be.
MAX_FRAME_LABEL: Final[int] = 256

#: Said on a record whose walk found no frame outside the SDK, the standard library and the
#: installed packages — an application that is itself an installed package, or a call made from a
#: framework's own thread with no customer frame above it.
CALLER_FRAMES_UNRESOLVED: Final[str] = (
    "CALLER_FRAMES_UNRESOLVED: no frame above this SDK lay outside the interpreter's standard "
    "library and its installed-package directories, so this call carries no application frame. An "
    "application installed non-editably into site-packages looks exactly like this."
)


def _library_prefixes() -> tuple[str, ...]:
    """Every directory this interpreter keeps code somebody else installed in, plus this package.

    Each one in both spellings, as `sysconfig` names it and as the file system resolves it, because the one a
    frame carries depends on how its module was found: Homebrew's `sysconfig` names the standard library behind
    `opt/python@3.x/…` while its modules run from `Cellar/python@3.x/…`, and this package imported through a
    symlinked path (`PYTHONPATH=/tmp/…` on macOS) carries the symlink. The standard library is also taken from a
    module of it (`inspect`), which is exactly the spelling every frame run from it has.
    """
    paths = sysconfig.get_paths()
    found = {paths[key] for key in ("stdlib", "platstdlib", "purelib", "platlib") if key in paths}
    try:
        found.update(site.getsitepackages())
    except AttributeError:  # pragma: no cover - a virtual environment without the hook
        pass
    found.add(site.getusersitepackages())
    stdlib_module = getattr(inspect, "__file__", None)
    if isinstance(stdlib_module, str):
        found.add(os.path.dirname(stdlib_module))
    found.add(os.path.dirname(os.path.abspath(__file__)))
    found.update({os.path.realpath(item) for item in list(found) if item})
    return tuple(sorted(item for item in found if item))


_PREFIXES: Final[tuple[str, ...]] = _library_prefixes()


@dataclass(frozen=True, slots=True)
class CallerFrame:
    """One application frame above the SDK: where it is, what it is called, and which line called."""

    module: str
    qualname: str
    file: str
    line: int
    #: `sha256:` of the frame's source file bytes (`file_digest`), or None when it could not be read. A
    #: digest of the customer's own file, never its text: the service binds the frame to the indexed
    #: revision with those bytes, so a line is never read against code that did not run.
    file_digest: str | None = None

    @property
    def label(self) -> str:
        """`module:qualname:line` — the spelling the OBSERVED rule names, and what a reader compares."""
        return f"{self.module}:{self.qualname}:{self.line}"

    def to_wire(self) -> JsonObject:
        """The camelCase shape one `callerFrames` entry carries."""
        wire: JsonObject = {"module": self.module, "qualname": self.qualname, "file": self.file, "line": self.line}
        if self.file_digest is not None:
            wire["fileDigest"] = self.file_digest
        return wire


#: How many distinct source files one process remembers the digest of. A process's own source is far fewer; a
#: file first seen after the cache is full gets no digest, so the first-sight check never lapses.
FILE_DIGESTS_CACHED: Final[int] = 4096
#: A file whose timestamps come within this much of the process start counts as written after it: filesystem
#: timestamps can be coarse (FAT's 2 s, some network filesystems' 1 s), so an edit made just after the start
#: can read as made at or before it.
TIMESTAMP_MARGIN_NS: Final[int] = 2 * 10**9
#: Windows `FILETIME` counts 100 ns ticks from 1601-01-01; the Unix epoch is this many ticks later.
_FILETIME_UNIX_EPOCH: Final[int] = 116_444_736_000_000_000


#: Linux `PF_FORKNOEXEC` (the flags field of `/proc/<pid>/stat`): forked, and has not exec'd since.
_PF_FORKNOEXEC: Final[int] = 0x40
#: macOS `P_EXEC` (`proc_bsdinfo.pbi_flags`): has exec'd. A fork clears it, and an `exec` sets it.
_P_EXEC: Final[int] = 0x4000
#: How many forking ancestors one start lookup walks through; deeper than that, the start is unknown.
ANCESTORS_WALKED: Final[int] = 8


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """What the operating system says about one process: when it started, its parent, and whether it was forked
    without an `exec` since (so the modules it runs may have been loaded by an ancestor)."""

    started_ns: int
    parent: int
    forked: bool


def _linux_process(pid: int) -> ProcessInfo | None:
    """`/proc/<pid>/stat`: field 4 (parent), field 9 (flags) and field 22 (start, in clock ticks after boot), plus
    `/proc/stat`'s `btime`."""
    if sys.platform != "linux":
        return None
    try:
        own = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        boot = Path("/proc/stat").read_text(encoding="ascii")
        # Fields after the parenthesised command name start at field 3.
        fields = own.rsplit(")", 1)[1].split()
        parent, flags, ticks = int(fields[1]), int(fields[6]), int(fields[19])
        btime = next(int(line.split()[1]) for line in boot.splitlines() if line.startswith("btime "))
        hertz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return ProcessInfo(btime * 10**9 + ticks * 10**9 // hertz, parent, bool(flags & _PF_FORKNOEXEC))


def _darwin_process(pid: int) -> ProcessInfo | None:
    """`proc_pidinfo(pid, PROC_PIDTBSDINFO)`: `pbi_flags` (`P_EXEC`), `pbi_ppid` and `pbi_start_tvsec/usec`."""
    if sys.platform != "darwin":
        return None
    try:
        # Imported here, not at the top: an interpreter built without `_ctypes` must still import this SDK.
        import ctypes  # noqa: PLC0415

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        pidinfo = libproc.proc_pidinfo
        pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        pidinfo.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(136)  # sizeof(struct proc_bsdinfo)
        size = pidinfo(pid, 3, 0, buffer, 136)  # PROC_PIDTBSDINFO
    except (ImportError, OSError, AttributeError):
        return None
    if size != 136:
        return None
    flags, _, _, _, parent = struct.unpack_from("=5I", buffer.raw, 0)
    seconds, micros = struct.unpack_from("=QQ", buffer.raw, 120)
    if seconds <= 0:
        return None
    return ProcessInfo(seconds * 10**9 + micros * 1_000, parent, not flags & _P_EXEC)


def _windows_process() -> ProcessInfo | None:
    """`GetProcessTimes(GetCurrentProcess())`'s creation time, with the handle typed as the 64-bit `HANDLE` it is.
    Windows has no fork: every process loaded its own modules."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes  # noqa: PLC0415 - see `_darwin_process`
        from ctypes import wintypes  # noqa: PLC0415

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        current = kernel32.GetCurrentProcess
        current.argtypes = []
        current.restype = wintypes.HANDLE
        times = kernel32.GetProcessTimes
        times.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
        times.restype = wintypes.BOOL
        creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        succeeded = times(
            current(), ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        )
    except (ImportError, OSError, AttributeError):
        return None
    ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    if not succeeded or ticks <= _FILETIME_UNIX_EPOCH:
        return None
    return ProcessInfo((ticks - _FILETIME_UNIX_EPOCH) * 100, os.getppid(), forked=False)


def process_info(pid: int) -> ProcessInfo | None:
    """What the operating system says about process `pid`; None when it cannot be asked (on Windows, only about
    this process)."""
    if sys.platform == "win32":
        return _windows_process() if pid == os.getpid() else None
    return _linux_process(pid) if sys.platform == "linux" else _darwin_process(pid)


def own_started_ns() -> int | None:
    """When this process itself started (a fork child: when it was forked)."""
    process = process_info(os.getpid())
    return None if process is None else process.started_ns


def process_started_ns() -> int | None:
    """The start a file's timestamps are held to: the earliest start of this process and of every ancestor it was
    forked from without an `exec` in between (`PF_FORKNOEXEC` on Linux, `P_EXEC` on macOS).

    A fork child — a gunicorn `preload_app` or celery prefork worker, a `multiprocessing` fork, a fork of a fork —
    runs the modules its forking ancestors loaded, and its own start is only its fork time. The walk stops at the
    first process that exec'd: that one loaded its modules itself, after its own start. It is unknown (None) when a
    forked process's parent is PID 1 (orphaned: the ancestor that loaded its modules is gone), when an ancestor
    cannot be read, past `ANCESTORS_WALKED` forks, and where the operating system cannot be asked at all; the caller
    then trusts no module loaded before this SDK.
    """
    pid, earliest = os.getpid(), None
    for _ in range(ANCESTORS_WALKED):
        process = process_info(pid)
        if process is None:
            return None
        earliest = process.started_ns if earliest is None else min(earliest, process.started_ns)
        if not process.forked:
            return earliest
        if process.parent <= 1:
            return None
        pid = process.parent
    return None


def normalised_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def loaded_module_files() -> frozenset[str]:
    """The source file of every module loaded so far."""
    files = (getattr(module, "__file__", None) for module in list(sys.modules.values()))
    return frozenset(normalised_path(file) for file in files if isinstance(file, str))


_STARTED_NS: Final[int | None] = process_started_ns()
#: When this process started: the operating system's answer, or — where it has none — when this SDK was
#: imported. A file written after it (by `max(mtime, ctime)`, less the margin) may not hold the bytes that are
#: running, so it never gets a digest.
_PROCESS_STARTED_NS: int = time.time_ns() if _STARTED_NS is None else _STARTED_NS
#: Where the process start is not known: the files of the modules already loaded when this SDK was imported.
#: One edited between its own import and this one would pass the import-time check, so none gets a digest.
_LOADED_BEFORE: frozenset[str] = frozenset() if _STARTED_NS is not None else loaded_module_files()
#: Each file's (mtime, ctime, size) when first seen, and its digest then.
_SEEN: dict[str, tuple[int, int, int, str | None]] = {}


def started_ns() -> int:
    """The start a file's timestamps are compared against (`_PROCESS_STARTED_NS`)."""
    return _PROCESS_STARTED_NS


_INHERITED_START_NS: Final[int] = _PROCESS_STARTED_NS


def _keep_the_parent_s_start() -> None:
    """In a child forked after this SDK was imported: it runs the bytes the parent loaded, so it keeps the
    parent's start (and its first sights), never its own later fork time."""
    global _PROCESS_STARTED_NS
    # The one module-level start, pinned across the fork (tests pin it too, through monkeypatch).
    _PROCESS_STARTED_NS = min(_PROCESS_STARTED_NS, _INHERITED_START_NS)  # pyright: ignore[reportConstantRedefinition]


if hasattr(os, "register_at_fork"):  # absent where the interpreter has no fork (Windows, WASI, Emscripten)
    os.register_at_fork(after_in_child=_keep_the_parent_s_start)


def file_digest(path: str) -> str | None:
    """`sha256:` of one source file's bytes, sent only while the file provably holds the code that runs.

    That is: a regular file (a FIFO, a device or `/dev/fd/N` is never opened) whose `max(mtime, ctime)` is at
    or before the process start less `TIMESTAMP_MARGIN_NS`, with the same (mtime, ctime, size) as when first
    seen. The ctime matters: the kernel sets it on every write, rename or `utime`, and nothing backdates it,
    so a file replaced by an mtime-preserving writer (`rsync -a`, `cp -p`, `tar -x`) fails the check. The
    start is the process's own, so a module edited between its import and the SDK's fails it too. An edit, a
    reload, a checkout during the process — any of them and the frame carries no digest, and a frame without
    one never links. Read once per file per process; None otherwise.
    """
    try:
        status = os.stat(path)
    except (OSError, ValueError):
        return None
    changed = max(status.st_mtime_ns, status.st_ctime_ns)
    if not stat.S_ISREG(status.st_mode) or changed > _PROCESS_STARTED_NS - TIMESTAMP_MARGIN_NS:
        return None
    if _LOADED_BEFORE and normalised_path(path) in _LOADED_BEFORE:
        return None
    sight = (status.st_mtime_ns, status.st_ctime_ns, status.st_size)
    seen = _SEEN.get(path)
    if seen is not None:
        return seen[3] if seen[:3] == sight else None
    if len(_SEEN) >= FILE_DIGESTS_CACHED:
        return None
    try:
        with Path(path).open("rb") as handle:
            digest: str | None = "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
        after = os.stat(path)
    except (OSError, ValueError):
        return None
    if (after.st_mtime_ns, after.st_ctime_ns, after.st_size) != sight:
        digest = None  # changed while it was read: these bytes are nobody's
    _SEEN[path] = (*sight, digest)
    return digest


@dataclass(slots=True)
class _Classified:
    """Which files are library code, remembered per file for the prefixes it was decided against: the walk runs
    on every task a wrapped process creates, and a prefix scan per frame is most of its cost."""

    prefixes: tuple[str, ...] = ()
    files: dict[str, bool] = field(default_factory=dict)


_CLASSIFIED = _Classified()


def _is_library(filename: str) -> bool:
    if _CLASSIFIED.prefixes is not _PREFIXES:
        _CLASSIFIED.prefixes, _CLASSIFIED.files = _PREFIXES, {}
    known = _CLASSIFIED.files.get(filename)
    if known is None:
        # CPython freezes standard-library modules such as runpy. Their synthetic filenames
        # have no installation prefix, but they are still interpreter frames, not app code.
        # uv's temporary runner environment can add another venv through .pth after
        # this module loads. Its dependency frames must not consume the application's
        # bounded frame slots merely because they live outside the initial prefixes.
        # A file reached through a symlink the prefixes do not name is resolved once, here,
        # and the answer remembered: the walk never pays for it again.
        parts = filename.replace("\\", "/").split("/")
        known = (
            filename.startswith("<frozen ")
            or "site-packages" in parts
            or "dist-packages" in parts
            or any(filename.startswith(prefix) for prefix in _PREFIXES)
            or any(os.path.realpath(filename).startswith(prefix) for prefix in _PREFIXES)
        )
        if len(_CLASSIFIED.files) < FILE_DIGESTS_CACHED:
            _CLASSIFIED.files[filename] = known
    return known


#: One application frame before it is a `CallerFrame`: (module, qualified name, file, line). A tuple, because the
#: walk runs on every task a wrapped process creates and a tuple is the cheapest record to build and hash.
RawFrame = tuple[str, str, str, int]


def raw_caller_frames(*, last: FrameType | None = None) -> tuple[RawFrame, ...]:
    """The application's own frames above this call, innermost first, bounded.

    Called from inside the wrapper, so the first frames walked are the SDK's own and are dropped by
    the same prefix rule that drops the standard library: this package's directory is one of the
    prefixes, which is why `wrap`'s own replacement functions never appear in a record.

    `last` is the outermost frame that belongs to the walk: inside an asyncio task, the task's own
    coroutine. Below it are the event loop and whatever started the loop — the same frames under every
    task in the process, and never the code that caused this one.
    """
    found: list[RawFrame] = []
    frame = inspect.currentframe()
    scanned = 0
    while frame is not None and scanned < MAX_FRAMES_SCANNED and len(found) < MAX_CALLER_FRAMES:
        scanned += 1
        code = frame.f_code
        filename = code.co_filename
        if not _is_library(filename):
            module = str(frame.f_globals.get("__name__", ""))[:MAX_FRAME_LABEL]
            found.append((module, code.co_qualname[:MAX_FRAME_LABEL], filename[:1024], frame.f_lineno))
        if frame is last:
            break
        frame = frame.f_back
    return tuple(found)


def caller_frames(*, last: FrameType | None = None) -> tuple[CallerFrame, ...]:
    """`raw_caller_frames`, as records."""
    return tuple(CallerFrame(*raw) for raw in raw_caller_frames(last=last))

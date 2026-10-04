"""Trace linkage: a wrapped call says where it was made, by default.

Frames are locations, so they travel with content capture off; they carry no value the application held, and
nothing derived from the prompt travels with them. `HAJER_CAPTURE_CALL_SITE=0` turns them off.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import runpy
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import hajer
from hajer import _frames
from hajer._frames import file_digest
from hajer._json import JsonObject
from hajer._wrap import located_file
from tests.fakes import FakeOpenAI


class _HostedOpenAI(FakeOpenAI):
    """A fake whose client names its endpoint, the way `openai.OpenAI().base_url` does."""

    base_url = "https://api.openai.com/v1"


class _ComposeOpenAI(FakeOpenAI):
    base_url = "http://llm_proxy:8000/v1"


class _IdnOpenAI(FakeOpenAI):
    base_url = "https://bücher.example/v1"


def _as_document(call: hajer.WrappedCall) -> JsonObject:
    """The record's locations as a document: its frames, its host and its content, each only when recorded."""
    document: JsonObject = {}
    if call.caller_frames:
        document["callerFrames"] = [frame.to_wire() for frame in call.caller_frames]
    if call.provider_host is not None:
        document["providerHost"] = call.provider_host
    if call.content is not None:
        document["content"] = dict(call.content)
    return document


def _posted_call(settings: hajer.HajerSettings, *, client: FakeOpenAI | None = None) -> JsonObject:
    provider = hajer.wrap(client if client is not None else FakeOpenAI(), settings=settings)
    with hajer.scope() as operation:
        provider.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You are terse."}, {"role": "user", "content": "hi"}],
        )
    (call,) = operation.calls
    return _as_document(call)


def test_frames_are_recorded_by_default(settings: hajer.HajerSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    # This file predates the (pretended) process start however recently it was saved.
    _process_starts_now(monkeypatch)
    call = _posted_call(settings)
    frames = cast("list[JsonObject]", call["callerFrames"])
    assert frames[0]["qualname"] == "_posted_call"
    assert not str(frames[0]["file"]).startswith("/")
    assert frames[0]["file"] == "tests/test_call_site.py"
    assert frames[0]["fileDigest"] == "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    assert "You are terse." in str(call["content"]), "message content is captured by default"
    assert "providerHost" not in call, "a client that names no base URL names no host"


def _older_than_the_process(path: Path, *, hours: int = 1) -> None:
    """Backdate the file's mtime, the way `rsync -a`, `cp -p`, `tar -x` or `git` can; its ctime stays now."""
    past = time.time_ns() - hours * 3_600 * 10**9
    os.utime(path, ns=(past, past))


def _process_starts_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the process started now, with no margin: a file written before this call predates it."""
    time.sleep(0.01)
    monkeypatch.setattr(_frames, "_PROCESS_STARTED_NS", time.time_ns())
    monkeypatch.setattr(_frames, "TIMESTAMP_MARGIN_NS", 0, raising=False)
    time.sleep(0.01)


R1 = b'def which():\n    return "R1 draft"\n'
R2 = b'def which():\n    return "R2 moderation, not the same call at all"\n'


def _sha(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def test_a_file_digest_is_sent_only_while_the_file_holds_the_code_that_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "app.py"
    source.write_bytes(b"print('hi')\n")
    _process_starts_now(monkeypatch)
    assert file_digest(str(source)) == _sha(b"print('hi')\n")
    # Edited while the process runs (a reload, a checkout): the running code may be the old bytes.
    source.write_bytes(b"changed\n")
    assert file_digest(str(source)) is None
    # Even with an old mtime put back, it is not the file first seen.
    _older_than_the_process(source, hours=2)
    assert file_digest(str(source)) is None
    fresh = tmp_path / "fresh.py"
    fresh.write_bytes(b"x = 1\n")
    assert file_digest(str(fresh)) is None, "modified after the process started"
    assert file_digest(str(tmp_path / "missing.py")) is None
    assert file_digest("<stdin>") is None


def test_a_file_replaced_with_its_mtime_preserved_before_its_first_call_gets_no_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept, replaced = tmp_path / "kept.py", tmp_path / "replaced.py"
    for path in (kept, replaced):
        path.write_bytes(R1)
        _older_than_the_process(path)
    _process_starts_now(monkeypatch)
    # `rsync -a` / `cp -p` / `tar -x` / `docker cp`: new bytes, the old mtime, and a ctime the kernel sets now.
    replaced.write_bytes(R2)
    _older_than_the_process(replaced)
    assert replaced.stat().st_mtime_ns < _frames.started_ns() < replaced.stat().st_ctime_ns
    assert file_digest(str(kept)) == _sha(R1)
    assert file_digest(str(replaced)) is None


def test_a_module_edited_after_the_process_started_but_before_hajer_was_imported_gets_no_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "probe_mod.py"
    source.write_bytes(R1)
    _older_than_the_process(source)
    script = "\n".join(
        [
            "import sys, time",
            f"sys.path.insert(0, {str(tmp_path)!r})",
            "sys.dont_write_bytecode = True",
            "import probe_mod  # a notebook cell, a lazily initialised SDK",
            "time.sleep(0.05)",
            f"open({str(source)!r}, 'wb').write({R2!r})  # an editor save, a `git pull`",
            "time.sleep(0.05)",
            "from hajer._frames import file_digest",
            "print(probe_mod.which(), file_digest(" + repr(str(source)) + "))",
        ]
    )
    ran = subprocess.run(  # noqa: S603 - this interpreter, running a script this test wrote
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert ran.stdout.split() == ["R1", "draft", "None"], ran.stdout + ran.stderr


def test_the_start_is_the_process_s_own_and_not_when_hajer_was_imported() -> None:
    started = _frames.process_started_ns()
    if sys.platform in ("linux", "darwin", "win32"):  # each asks the operating system: /proc, sysctl, GetProcessTimes
        assert started is not None
        assert time.time_ns() - 7 * 86_400 * 10**9 < started <= time.time_ns()
        assert started == _frames.started_ns()
        ran = subprocess.run(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(0.3); from hajer import _frames; import time as t; "
                "print(t.time_ns() - _frames.started_ns())",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        assert int(ran.stdout) >= 300_000_000, "the start precedes the sleep before `import hajer`"


def test_on_a_platform_with_no_known_start_a_module_loaded_before_hajer_gets_no_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "early.py"
    source.write_bytes(R1)
    _process_starts_now(monkeypatch)
    assert file_digest(str(source)) == _sha(R1)
    monkeypatch.setattr(_frames, "_SEEN", {})
    monkeypatch.setattr(_frames, "_LOADED_BEFORE", frozenset({_frames.normalised_path(str(source))}), raising=False)
    assert file_digest(str(source)) is None
    assert _frames.normalised_path(__file__) in _frames.loaded_module_files(), "this module was loaded already"


def test_the_first_sight_check_never_lapses_when_the_cache_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "late.py"
    source.write_bytes(R1)
    _process_starts_now(monkeypatch)
    monkeypatch.setattr(_frames, "_SEEN", {f"/f{i}": (0, 0, 0, None) for i in range(_frames.FILE_DIGESTS_CACHED)})
    assert file_digest(str(source)) is None


def test_a_file_that_is_not_regular_is_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    _process_starts_now(monkeypatch)
    assert file_digest(str(fifo)) is None, "opening a FIFO would block the provider call"
    assert file_digest(str(tmp_path)) is None


def test_a_frame_outside_the_project_root_carries_no_digest(settings: hajer.HajerSettings) -> None:
    call = _posted_call(settings.model_copy(update={"project_root": "/nonexistent-project-root"}))
    frames = cast("list[JsonObject]", call["callerFrames"])
    assert frames
    assert all(str(frame["file"]).startswith("<outside>/") for frame in frames)
    assert all("fileDigest" not in frame for frame in frames)


def test_frozen_interpreter_frames_are_not_recorded_as_application_code(
    settings: hajer.HajerSettings, tmp_path: Path
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("call = _posted_call(settings)\n", encoding="utf-8")
    namespace = runpy.run_path(str(probe), init_globals={"_posted_call": _posted_call, "settings": settings})
    call = cast(JsonObject, namespace["call"])
    frames = cast("list[JsonObject]", call["callerFrames"])
    assert frames
    assert all(not str(frame["file"]).startswith("<frozen ") for frame in frames)


@pytest.mark.parametrize(
    ("client", "host"),
    [
        (_HostedOpenAI(), "api.openai.com:443"),
        (_ComposeOpenAI(), "llm_proxy:8000"),
        (_IdnOpenAI(), "xn--bcher-kva.example:443"),
    ],
)
def test_the_provider_host_is_the_clients_base_url(
    settings: hajer.HajerSettings, client: FakeOpenAI, host: str
) -> None:
    assert _posted_call(settings, client=client)["providerHost"] == host


@pytest.mark.parametrize(
    ("file", "root", "sent"),
    [
        ("/srv/app/app/llm.py", "/srv/app", "app/llm.py"),
        ("/srv/app/a/b/c/d/e/f/g/h.py", "/srv/app", "a/b/c/d/e/f/g/h.py"),
        ("/home/alice/proj/app/llm.py", "/", "<outside>/llm.py"),
        ("/home/alice/bot.py", "/opt/service", "<outside>/bot.py"),
        ("/Users/alice/work/app/llm.py", "/opt/service", "<outside>/llm.py"),
        ("/Users/alice/code/proj/app/llm.py", "/Users/alice/code/proj/notebooks", "<outside>/llm.py"),
        ("/home/alice/proj/app/llm.py", "/home", "<outside>/llm.py"),
        ("/home/alice/proj/app/llm.py", "/home/alice", "proj/app/llm.py"),
        ("/usr/lib/python3.12/site-packages/pkg/mod.py", "/srv/app", "<outside>/mod.py"),
        ("C:\\Users\\bob\\proj\\app\\llm.py", "C:\\Users\\bob\\proj", "app/llm.py"),
        ("C:\\Users\\bob\\proj\\app\\llm.py", "C:\\", "<outside>/llm.py"),
    ],
)
def test_a_frame_carries_no_absolute_path_and_no_home_directory(file: str, root: str, sent: str) -> None:
    assert located_file(file, root) == sent


def test_a_configured_project_root_wins_over_the_working_directory() -> None:
    settings = hajer.HajerSettings.from_env({"HAJER_PROJECT_ROOT": "/srv/app"})
    assert settings.project_root == "/srv/app"


def test_call_site_capture_is_opted_out_by_one_variable(settings: hajer.HajerSettings) -> None:
    quiet = hajer.HajerSettings.from_env(
        {"HAJER_API_KEY": "key-for-tests", "HAJER_TEAM_ID": "team-1", "HAJER_CAPTURE_CALL_SITE": "0"}
    )
    assert quiet.capture_call_site is False
    call = _posted_call(settings.model_copy(update={"capture_call_site": False}), client=_HostedOpenAI())
    assert "callerFrames" not in call
    assert "providerHost" not in call, "the opt-out covers the endpoint too"


def _python(script: str) -> str:
    """Run `script` in a fresh interpreter of this environment and return what it printed."""
    ran = subprocess.run(  # noqa: S603 - this interpreter, running a script this test wrote
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    return ran.stdout.strip()


@pytest.mark.skipif(sys.platform == "win32", reason="no fork on Windows")
def test_a_forked_worker_that_imports_hajer_after_its_parent_loaded_an_edited_module_sends_no_digest(
    tmp_path: Path,
) -> None:
    # The fork probe: a prefork master (gunicorn `preload_app`, celery prefork) imports the application,
    # the file is edited, and a worker forked more than the margin later initialises the SDK lazily.
    source = tmp_path / "probe_mod.py"
    source.write_bytes(R1)
    _older_than_the_process(source)
    script = "\n".join(
        [
            "import os, sys, time",
            f"sys.path.insert(0, {str(tmp_path)!r})",
            "sys.dont_write_bytecode = True",
            "import probe_mod",
            f"open({str(source)!r}, 'wb').write({R2!r})",
            "time.sleep(2.5)",
            "pid = os.fork()",
            "if pid == 0:",
            "    from hajer._frames import file_digest",
            "    print(probe_mod.which(), file_digest(" + repr(str(source)) + "), flush=True)",
            "    os._exit(0)",
            "os.waitpid(pid, 0)",
        ]
    )
    assert _python(script).split() == ["R1", "draft", "None"]


@pytest.mark.skipif(sys.platform == "win32", reason="no fork on Windows")
def test_a_child_forked_after_hajer_was_imported_keeps_the_parent_s_start() -> None:
    script = "\n".join(
        [
            "import os, time",
            "from hajer import _frames",
            "time.sleep(0.2)",
            "pid = os.fork()",
            "if pid == 0:",
            "    print('child', _frames.started_ns(), flush=True)",
            "    os._exit(0)",
            "os.waitpid(pid, 0)",
            "print('parent', _frames.started_ns(), flush=True)",
        ]
    )
    said = dict(line.split() for line in _python(script).splitlines())
    assert said["child"] == said["parent"]


@pytest.mark.skipif(sys.platform == "win32", reason="no fork on Windows")
def test_a_process_that_exec_d_is_held_to_its_own_start_and_a_fork_child_to_its_forking_ancestor_s() -> None:
    # This interpreter exec'd: its own start. So does a child interpreter it starts (it loads its own modules).
    assert _frames.process_started_ns() == _frames.own_started_ns()
    said = _python("from hajer import _frames; print(_frames.process_started_ns() == _frames.own_started_ns())")
    assert said == "True"
    script = "\n".join(
        [
            "import os",
            "from hajer import _frames",
            "print('parent', _frames.process_info(os.getpid()).forked, _frames.own_started_ns(), flush=True)",
            "pid = os.fork()",
            "if pid == 0:",
            "    print('child', _frames.process_info(os.getpid()).forked, _frames.process_started_ns(), flush=True)",
            "    os._exit(0)",
            "os.waitpid(pid, 0)",
        ]
    )
    lines = {line.split()[0]: line.split()[1:] for line in _python(script).splitlines()}
    assert lines["parent"][0] == "False"
    assert lines["child"][0] == "True"
    assert lines["child"][1] == lines["parent"][1], "a fork child is held to the start of the process that forked it"


_EDITED_THEN_FORKED = [
    "import os, sys, time",
    "sys.dont_write_bytecode = True",
    "import probe_mod  # the master loads the application; hajer is not imported yet",
    "open(probe_mod.__file__, 'wb').write({r2!r})  # an in-place `git pull`",
    "time.sleep(2.5)  # forks happen more than the margin later",
]


def _report(tag: str) -> list[str]:
    return [
        "    from hajer import _frames",
        f"    print({tag!r}, probe_mod.which(), _frames.file_digest(probe_mod.__file__), flush=True)",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="no fork on Windows")
def test_a_grandchild_that_imports_hajer_after_its_grandparent_loaded_an_edited_module_sends_no_digest(
    tmp_path: Path,
) -> None:
    (tmp_path / "probe_mod.py").write_bytes(R1)
    _older_than_the_process(tmp_path / "probe_mod.py")
    script = "\n".join(
        [
            f"import sys; sys.path.insert(0, {str(tmp_path)!r})",
            *(line.format(r2=R2) for line in _EDITED_THEN_FORKED),
            "b = os.fork()",
            "if b == 0:",
            "  c = os.fork()",
            "  if c == 0:",
            *("  " + line for line in _report("grandchild")),
            "      os._exit(0)",
            "  os.waitpid(c, 0)",
            "  os._exit(0)",
            "os.waitpid(b, 0)",
        ]
    )
    assert _python(script).split() == ["grandchild", "R1", "draft", "None"]


@pytest.mark.skipif(sys.platform == "win32", reason="no fork on Windows")
def test_an_orphaned_child_cannot_prove_its_start_and_sends_no_digest_for_what_it_inherited(
    tmp_path: Path,
) -> None:
    (tmp_path / "probe_mod.py").write_bytes(R1)
    _older_than_the_process(tmp_path / "probe_mod.py")
    out = tmp_path / "orphan.txt"
    script = "\n".join(
        [
            f"import sys; sys.path.insert(0, {str(tmp_path)!r})",
            *(line.format(r2=R2) for line in _EDITED_THEN_FORKED),
            "r, w = os.pipe()",
            "b = os.fork()",
            "if b == 0:",
            "    os.close(w); os.read(r, 1); time.sleep(0.3)  # the parent has exited: reparented",
            "    from hajer import _frames",
            f"    open({str(out)!r}, 'w').write(' '.join(map(str, [probe_mod.which(), _frames.process_started_ns(),"
            " _frames.file_digest(probe_mod.__file__)])))",
            "    os._exit(0)",
            "os.close(r)",
        ]
    )
    _python(script)
    for _ in range(100):
        if out.exists() and out.read_text():
            break
        time.sleep(0.05)
    assert out.read_text().split() == ["R1", "draft", "None", "None"]


def test_hajer_imports_where_os_has_no_register_at_fork() -> None:
    said = _python("import os; del os.fork, os.register_at_fork; import hajer; from hajer import _frames; print('ok')")
    assert said == "ok"


def test_hajer_imports_without_ctypes_and_then_trusts_no_module_loaded_before_it() -> None:
    script = "\n".join(
        [
            "import sys",
            "class _Block:",
            "    def find_spec(self, name, path=None, target=None):",
            "        if name in ('_ctypes', 'ctypes') or name.startswith('ctypes.'):",
            "            raise ModuleNotFoundError(f'import of {name} halted')",
            "sys.meta_path.insert(0, _Block())",
            "import hajer",
            "from hajer import _frames",
            "print(_frames.process_started_ns() is None, bool(_frames._LOADED_BEFORE), 'ctypes' in sys.modules)",
        ]
    )
    unknown, refused, loaded = _python(script).split()
    assert loaded == "False"
    # Linux reads /proc without ctypes; elsewhere the start is unknown, and every module loaded before gets none.
    expected = "False" if sys.platform == "linux" else "True"
    assert (unknown, refused) == (expected, expected)


def test_a_symlinked_path_and_its_real_path_are_one_root(tmp_path: Path) -> None:
    """macOS: `Path.cwd()` is `/private/tmp/…` while a module imported through `PYTHONPATH=/tmp/…` reports
    `/tmp/…`. Both spell one directory, so neither may put a file outside."""
    real = tmp_path / "real"
    (real / "app").mkdir(parents=True)
    (real / "app" / "llm.py").write_text("x = 1\n")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert located_file(str(link / "app" / "llm.py"), str(real)) == "app/llm.py"
    assert located_file(str(real / "app" / "llm.py"), str(link)) == "app/llm.py"
    assert located_file(str(link / "app" / "llm.py"), str(tmp_path / "elsewhere")) == "<outside>/llm.py"


def test_a_module_imported_through_a_symlink_gets_its_relative_file_and_digest(
    tmp_path: Path, settings: hajer.HajerSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    (real / "app").mkdir(parents=True)
    source = b"def ask(client):\n    return client.chat.completions.create(model='m', messages=[])\n"
    (real / "app" / "caller.py").write_bytes(source)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    namespace: dict[str, object] = {"__name__": "app.caller"}
    exec(compile(source, str(link / "app" / "caller.py"), "exec"), namespace)  # noqa: S102 - the module under test
    _process_starts_now(monkeypatch)
    provider = hajer.wrap(FakeOpenAI(), settings=settings.model_copy(update={"project_root": str(real)}))
    with hajer.scope() as operation:
        cast("Callable[[object], object]", namespace["ask"])(provider)
    frame = operation.calls[0].caller_frames[0]
    assert (frame.file, frame.qualname) == ("app/caller.py", "ask")
    assert frame.file_digest == _sha(source)


@pytest.mark.parametrize(
    ("file", "root", "sent"),
    [
        ("/home/alice/proj/app/llm.py", "/home", "<outside>/llm.py"),
        ("/System/Volumes/Data/home/alice/proj/app/llm.py", "/home", "<outside>/llm.py"),
        ("/home/alice/proj/app/llm.py", "/System/Volumes/Data/home", "<outside>/llm.py"),
        ("/System/Volumes/Data/home/alice/proj/app/llm.py", "/home/alice", "proj/app/llm.py"),
    ],
)
def test_resolving_a_path_never_reveals_a_home_directory(file: str, root: str, sent: str) -> None:
    """On macOS `/home` resolves to `/System/Volumes/Data/home`: the resolved spelling is a home directory too."""
    assert located_file(file, root) == sent


def test_the_sdk_s_own_directory_is_library_code_however_it_was_imported() -> None:
    """Imported through a symlinked path, the SDK's frames carry the unresolved spelling."""
    here = os.path.dirname(os.path.abspath(_frames.__file__))
    assert here in _frames._PREFIXES  # pyright: ignore[reportPrivateUsage]
    assert str(Path(_frames.__file__).resolve().parent) in _frames._PREFIXES  # pyright: ignore[reportPrivateUsage]


@pytest.mark.skipif(sys.platform == "win32", reason="a directory symlink needs a privilege on Windows")
def test_the_standard_library_is_library_code_however_the_interpreter_spells_its_directory(tmp_path: Path) -> None:
    """Homebrew's `sysconfig` names the standard library behind `opt/…` while its modules run from `Cellar/…`, so an
    asyncio frame carried the resolved spelling and counted as the application's — the loop's own `create_task` was
    recorded in place of the caller. Either spelling is the interpreter's, and a module reached through a symlink the
    prefixes never named is resolved before it is taken for application code."""
    for module_file in (asyncio.__file__, inspect.__file__):
        assert _frames._is_library(module_file)  # pyright: ignore[reportPrivateUsage]
        assert _frames._is_library(os.path.realpath(module_file))  # pyright: ignore[reportPrivateUsage]
    link = tmp_path / "interpreter"
    link.symlink_to(os.path.dirname(inspect.__file__), target_is_directory=True)
    assert _frames._is_library(str(link / "asyncio" / "base_events.py"))  # pyright: ignore[reportPrivateUsage]
    assert not _frames._is_library(str(tmp_path / "app" / "service.py"))  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("directory", ["site-packages", "dist-packages"])
def test_overlay_dependency_frames_do_not_hide_the_application_caller(tmp_path: Path, directory: str) -> None:
    def dependency(depth: int) -> tuple[_frames.CallerFrame, ...]:
        return dependency(depth - 1) if depth else _frames.caller_frames()

    dependency.__code__ = dependency.__code__.replace(
        co_filename=str(tmp_path / "overlay" / directory / "framework.py")
    )
    frames = dependency(12)
    assert any(
        frame.qualname.endswith("test_overlay_dependency_frames_do_not_hide_the_application_caller") for frame in frames
    )
    assert all(frame.file != dependency.__code__.co_filename for frame in frames)

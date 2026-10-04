"""The pinned eval engine: the pin is the package data, the install is deterministic, and nothing raises.

No Node and no npm anywhere in here: `which` and `run` are injected, and the `run` fake answers by the shape of the
command it is handed — `node --version`, `npm ci`, the smoke test — and creates the entrypoint file where a real
`npm ci` would. What is asserted is the exact command line, the environment it ran in, and what was written.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hajer import HajerSettings
from hajer._json import JsonValue
from hajer.evals._engine import (
    ENGINE_DIR_DIGEST_CHARS,
    ENGINE_PACKAGE,
    NPM_CI_FLAGS,
    PIN_FILES,
    READY_MARKER,
    EngineReady,
    EngineUnavailable,
    Pin,
    Run,
    Which,
    engine_dir,
    engine_status,
    ensure_engine,
    entrypoint,
    node_version,
    pinned,
)

NODE = "/fake/bin/node"
NPM = "/fake/bin/npm"
NODE_VERSION = "v22.23.2"


def both(name: str) -> str | None:
    return {"node": NODE, "npm": NPM}.get(name)


def node_only(name: str) -> str | None:
    return NODE if name == "node" else None


def neither(name: str) -> str | None:
    del name
    return None


@dataclass
class ScriptedRuns:
    """Every command `ensure_engine` ran, and the answers it got — without a process ever starting."""

    pin: Pin
    node_prints: str = NODE_VERSION + "\n"
    node_exit: int = 0
    npm_exit: int = 0
    npm_stderr: str = ""
    npm_hangs: bool = False
    smoke_prints: str | None = None
    argvs: list[list[str]] = field(default_factory=list)
    kwargs: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.argvs.append(list(argv))
        self.kwargs.append(dict(kwargs))
        if list(argv) == [NODE, "--version"]:
            return subprocess.CompletedProcess(list(argv), self.node_exit, stdout=self.node_prints, stderr="")
        if argv[0] == NPM:
            if self.npm_hangs:
                raise subprocess.TimeoutExpired(list(argv), float(str(kwargs.get("timeout"))))
            if self.npm_exit == 0:
                cwd = kwargs.get("cwd")
                assert isinstance(cwd, Path)
                entry = entrypoint(cwd)
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.write_text("// the engine\n", encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), self.npm_exit, stdout="", stderr=self.npm_stderr)
        if argv[0] == NODE and argv[-1] == "--version":
            printed = self.pin.version if self.smoke_prints is None else self.smoke_prints
            return subprocess.CompletedProcess(list(argv), 0, stdout=printed + "\n", stderr="")
        raise AssertionError(f"unexpected command: {argv!r}")

    @property
    def npm_calls(self) -> list[list[str]]:
        return [argv for argv in self.argvs if argv[0] == NPM]


@pytest.fixture
def pin() -> Pin:
    return pinned()


@pytest.fixture
def settings(tmp_path: Path) -> HajerSettings:
    return HajerSettings(cache_dir=str(tmp_path))


@pytest.fixture
def runs(pin: Pin) -> ScriptedRuns:
    return ScriptedRuns(pin)


def ready(
    settings: HajerSettings,
    runs: Run,
    which: Which = both,
    *,
    install: bool = True,
    environment: Mapping[str, str] | None = None,
) -> EngineReady:
    outcome = ensure_engine(settings, install=install, which=which, run=runs, environment=environment)
    assert isinstance(outcome, EngineReady), outcome
    return outcome


def unavailable(settings: HajerSettings, runs: Run, which: Which = both, *, install: bool = True) -> EngineUnavailable:
    outcome = ensure_engine(settings, install=install, which=which, run=runs)
    assert isinstance(outcome, EngineUnavailable), outcome
    return outcome


class TestThePin:
    def test_the_package_data_is_the_pin(self, pin: Pin) -> None:
        assert pin.version
        assert pin.node_floor == (22, 22, 0)
        assert len(pin.lockfile_digest) == 64
        assert int(pin.lockfile_digest, 16) >= 0
        assert pin.package_json.name == PIN_FILES[0]
        assert pin.lockfile.name == PIN_FILES[1]
        assert pin.package_json.parent == pin.lockfile.parent

    def test_the_lockfile_resolves_the_version_the_manifest_pins(self, pin: Pin) -> None:
        """The drift test: a bump that edits one file and not the other fails here, before any npm run would."""
        lock: JsonValue = json.loads(pin.lockfile.read_bytes())
        assert isinstance(lock, dict)
        packages = lock["packages"]
        assert isinstance(packages, dict)
        engine = packages[f"node_modules/{ENGINE_PACKAGE}"]
        assert isinstance(engine, dict)
        assert engine["version"] == pin.version
        root = packages[""]
        assert isinstance(root, dict)
        assert root["dependencies"] == {ENGINE_PACKAGE: pin.version}

    def test_pinned_is_read_once(self) -> None:
        assert pinned() is pinned()

    def test_the_engine_dir_is_keyed_by_the_lockfile(self, settings: HajerSettings, pin: Pin, tmp_path: Path) -> None:
        directory = engine_dir(settings)
        assert directory == tmp_path / "engine" / pin.lockfile_digest[:ENGINE_DIR_DIGEST_CHARS]
        assert engine_dir(settings, pin) == directory
        assert entrypoint(directory) == directory / "node_modules" / ENGINE_PACKAGE / "dist" / "src" / "entrypoint.js"


class TestNodeVersion:
    def test_parses_what_node_prints(self, runs: ScriptedRuns) -> None:
        assert node_version(NODE, run=runs) == (22, 23, 2)
        assert runs.argvs == [[NODE, "--version"]]

    @pytest.mark.parametrize("printed", ["", "node: command not found\n", "twenty-two\n", "v22\n"])
    def test_garbage_is_none(self, pin: Pin, printed: str) -> None:
        assert node_version(NODE, run=ScriptedRuns(pin, node_prints=printed)) is None

    def test_a_failing_run_is_none(self, pin: Pin) -> None:
        assert node_version(NODE, run=ScriptedRuns(pin, node_exit=1)) is None

    def test_a_node_that_cannot_start_is_none(self) -> None:
        def cannot_start(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise OSError(f"cannot start {argv[0]} {sorted(kwargs)}")

        assert node_version(NODE, run=cannot_start) is None


class TestEnsureEngineRefuses:
    def test_without_node(self, settings: HajerSettings, runs: ScriptedRuns, pin: Pin) -> None:
        outcome = unavailable(settings, runs, which=neither)
        assert outcome.reason == "NODE_MISSING"
        assert "22.22.0" in outcome.detail
        assert runs.argvs == []

    def test_with_a_node_that_reports_no_version(self, settings: HajerSettings, pin: Pin) -> None:
        outcome = unavailable(settings, ScriptedRuns(pin, node_prints="segmentation fault\n"))
        assert outcome.reason == "NODE_MISSING"
        assert NODE in outcome.detail

    def test_with_an_old_node(self, settings: HajerSettings, pin: Pin) -> None:
        outcome = unavailable(settings, ScriptedRuns(pin, node_prints="v20.0.0\n"))
        assert outcome.reason == "NODE_TOO_OLD"
        assert "20.0.0" in outcome.detail
        assert "22.22.0" in outcome.detail
        assert not engine_dir(settings).exists(), "an old Node stops before anything is written"

    def test_a_node_exactly_at_the_floor_is_enough(self, settings: HajerSettings, pin: Pin) -> None:
        ready(settings, ScriptedRuns(pin, node_prints="v22.22.0\n"))

    def test_without_npm(self, settings: HajerSettings, runs: ScriptedRuns) -> None:
        outcome = unavailable(settings, runs, which=node_only)
        assert outcome.reason == "NPM_MISSING"
        assert "Node.js" in outcome.detail
        assert runs.npm_calls == []

    def test_when_not_allowed_to_install(self, settings: HajerSettings, runs: ScriptedRuns, pin: Pin) -> None:
        outcome = unavailable(settings, runs, install=False)
        assert outcome.reason == "NOT_INSTALLED"
        assert pin.version in outcome.detail
        assert str(engine_dir(settings)) in outcome.detail
        assert runs.npm_calls == []
        assert not engine_dir(settings).exists()


class TestEnsureEngineInstalls:
    def test_a_cold_cache_installs_and_smokes(
        self, settings: HajerSettings, runs: ScriptedRuns, pin: Pin, tmp_path: Path
    ) -> None:
        outcome = ready(settings, runs, environment={"PATH": "/fake/bin", "HOME": str(tmp_path)})
        directory = engine_dir(settings)
        assert outcome == EngineReady(
            entrypoint=entrypoint(directory),
            node=NODE,
            node_version="22.23.2",
            version=pin.version,
            directory=directory,
            installed_now=True,
        )
        assert outcome.entrypoint.is_file()
        assert (directory / READY_MARKER).read_text(encoding="utf-8").strip() == pin.version
        for name in PIN_FILES:
            assert (directory / name).read_bytes() == (pin.package_json.parent / name).read_bytes()
        assert directory.with_name(directory.name + ".lock").exists()

        assert runs.argvs == [
            [NODE, "--version"],
            [NPM, *NPM_CI_FLAGS],
            [NODE, str(entrypoint(directory)), "--version"],
        ]
        npm_kwargs = runs.kwargs[1]
        assert npm_kwargs["cwd"] == directory
        assert npm_kwargs["timeout"] == settings.eval_install_timeout_s
        assert npm_kwargs["capture_output"] is True
        assert npm_kwargs["text"] is True
        assert npm_kwargs["check"] is False
        assert npm_kwargs["env"] == {"PATH": "/fake/bin", "HOME": str(tmp_path), "NO_UPDATE_NOTIFIER": "1"}
        assert runs.kwargs[2]["env"] == npm_kwargs["env"]
        assert runs.kwargs[2]["cwd"] == directory

    def test_no_environment_means_the_process_environment(self, settings: HajerSettings, runs: ScriptedRuns) -> None:
        ready(settings, runs)
        assert runs.kwargs[1]["env"] is None
        assert runs.kwargs[2]["env"] is None

    def test_a_ready_install_never_touches_npm(self, settings: HajerSettings, runs: ScriptedRuns) -> None:
        first = ready(settings, runs)
        again = ScriptedRuns(runs.pin)
        second = ready(settings, again)
        assert second == EngineReady(
            entrypoint=first.entrypoint,
            node=first.node,
            node_version=first.node_version,
            version=first.version,
            directory=first.directory,
            installed_now=False,
        )
        assert again.argvs == [[NODE, "--version"]]
        # `install=False` is also enough once the install is there.
        assert ready(settings, ScriptedRuns(runs.pin), install=False).installed_now is False

    def test_a_stale_marker_reinstalls(self, settings: HajerSettings, runs: ScriptedRuns, pin: Pin) -> None:
        ready(settings, runs)
        directory = engine_dir(settings)
        (directory / READY_MARKER).write_text("0.0.1\n", encoding="utf-8")
        again = ScriptedRuns(pin)
        outcome = ready(settings, again)
        assert outcome.installed_now is True
        assert len(again.npm_calls) == 1
        assert (directory / READY_MARKER).read_text(encoding="utf-8").strip() == pin.version

    def test_a_marker_without_an_entrypoint_reinstalls(self, settings: HajerSettings, runs: ScriptedRuns) -> None:
        ready(settings, runs)
        entrypoint(engine_dir(settings)).unlink()
        again = ScriptedRuns(runs.pin)
        assert ready(settings, again).installed_now is True
        assert len(again.npm_calls) == 1

    def test_the_smoke_test_accepts_a_leading_v(self, settings: HajerSettings, pin: Pin) -> None:
        ready(settings, ScriptedRuns(pin, smoke_prints="v" + pin.version))


class TestEnsureEngineInstallFails:
    def test_a_failing_npm(self, settings: HajerSettings, pin: Pin) -> None:
        stderr = "npm warn something harmless\nnpm error code E403\nnpm error 403 Forbidden - GET https://registry.example/x\n\n"
        outcome = unavailable(settings, ScriptedRuns(pin, npm_exit=1, npm_stderr=stderr))
        assert outcome.reason == "INSTALL_FAILED"
        assert outcome.detail.endswith("npm error 403 Forbidden - GET https://registry.example/x")
        assert not (engine_dir(settings) / READY_MARKER).exists()

    def test_a_failing_npm_that_said_nothing(self, settings: HajerSettings, pin: Pin) -> None:
        outcome = unavailable(settings, ScriptedRuns(pin, npm_exit=7))
        assert outcome.reason == "INSTALL_FAILED"
        assert "exit status 7" in outcome.detail

    def test_a_hanging_npm(self, settings: HajerSettings, pin: Pin) -> None:
        outcome = unavailable(settings, ScriptedRuns(pin, npm_hangs=True))
        assert outcome.reason == "INSTALL_TIMEOUT"
        assert str(settings.eval_install_timeout_s) in outcome.detail
        assert "HAJER_EVAL_INSTALL_TIMEOUT_S" in outcome.detail

    def test_an_npm_that_cannot_start(self, settings: HajerSettings, runs: ScriptedRuns) -> None:
        def flaky(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[0] == NPM:
                raise PermissionError(f"{argv[0]}: not executable")
            return runs(argv, **kwargs)

        outcome = unavailable(settings, flaky)
        assert outcome.reason == "INSTALL_FAILED"
        assert "not executable" in outcome.detail

    def test_a_wrong_version_after_install(self, settings: HajerSettings, pin: Pin) -> None:
        outcome = unavailable(settings, ScriptedRuns(pin, smoke_prints="0.0.1"))
        assert outcome.reason == "SMOKE_FAILED"
        assert "0.0.1" in outcome.detail
        assert pin.version in outcome.detail
        assert not (engine_dir(settings) / READY_MARKER).exists(), "a failed smoke test leaves no marker"

    def test_an_unwritable_cache_is_a_sentence_not_a_traceback(self, tmp_path: Path, runs: ScriptedRuns) -> None:
        blocker = tmp_path / "cache"
        blocker.write_text("not a directory\n", encoding="utf-8")
        outcome = unavailable(HajerSettings(cache_dir=str(blocker)), runs)
        assert outcome.reason == "INSTALL_FAILED"
        assert str(blocker) in outcome.detail
        assert runs.npm_calls == []


class TestEngineStatus:
    KEYS = frozenset(
        {
            "node",
            "nodeVersion",
            "nodeFloor",
            "nodeMeetsFloor",
            "npm",
            "pinnedVersion",
            "lockfileDigest",
            "engineDir",
            "installed",
            "ready",
            "reason",
            "detail",
        }
    )

    def test_before_an_install(self, settings: HajerSettings, runs: ScriptedRuns, pin: Pin) -> None:
        status = engine_status(settings, which=both, run=runs)
        assert frozenset(status) == self.KEYS
        assert status["node"] == NODE
        assert status["nodeVersion"] == "22.23.2"
        assert status["nodeFloor"] == "22.22.0"
        assert status["nodeMeetsFloor"] is True
        assert status["npm"] == NPM
        assert status["pinnedVersion"] == pin.version
        assert status["lockfileDigest"] == pin.lockfile_digest
        assert status["engineDir"] == str(engine_dir(settings))
        assert status["installed"] is False
        assert status["ready"] is False
        assert status["reason"] == "NOT_INSTALLED"
        assert runs.npm_calls == [], "status never installs"
        assert not engine_dir(settings).exists()

    def test_after_an_install(self, settings: HajerSettings, runs: ScriptedRuns) -> None:
        ready(settings, runs)
        status = engine_status(settings, which=both, run=ScriptedRuns(runs.pin))
        assert frozenset(status) == self.KEYS
        assert status["installed"] is True
        assert status["ready"] is True
        assert status["reason"] is None
        assert status["detail"] is None

    def test_an_install_the_marker_disowns_is_installed_but_not_ready(
        self, settings: HajerSettings, runs: ScriptedRuns
    ) -> None:
        ready(settings, runs)
        (engine_dir(settings) / READY_MARKER).write_text("0.0.1\n", encoding="utf-8")
        status = engine_status(settings, which=both, run=ScriptedRuns(runs.pin))
        assert status["installed"] is True
        assert status["ready"] is False
        assert status["reason"] == "NOT_INSTALLED"

    def test_without_node(self, settings: HajerSettings, runs: ScriptedRuns, pin: Pin) -> None:
        status = engine_status(settings, which=neither, run=runs)
        assert status["node"] is None
        assert status["nodeVersion"] is None
        assert status["nodeMeetsFloor"] is False
        assert status["npm"] is None
        assert status["pinnedVersion"] == pin.version
        assert status["ready"] is False
        assert status["reason"] == "NODE_MISSING"

    def test_with_an_old_node(self, settings: HajerSettings, pin: Pin) -> None:
        status = engine_status(settings, which=both, run=ScriptedRuns(pin, node_prints="v20.0.0\n"))
        assert status["nodeVersion"] == "20.0.0"
        assert status["nodeMeetsFloor"] is False
        assert status["reason"] == "NODE_TOO_OLD"

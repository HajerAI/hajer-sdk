"""The fixture application under a transport-blocking suite still yields the export.

A customer's `conftest.py` may patch every `httpx` transport for the whole session so that no test can
reach a network. The patch is about their provider calls; it catches Hajer's flush as well, and before
`HAJER_OBSERVE_SINK` a suite instrumented that way produced no observations at all — the one source of
observed traffic that costs nothing was the one source the SDK could not export.

This test reproduces that suite: `httpx.HTTPTransport.handle_request` and its async twin raise for the
duration, the fixture application runs through `wrap`, and the sink's document is fed to the real
platform's observation exporter (`--receipt`), which is the exporter the trace already reads. What it
proves is that the export exists and carries the same provider bytes the wrapper recorded; what it does
not prove is that anything reached a backend, because in this test nothing may.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast

import httpx
import pytest

import hajer
from hajer._json import JsonObject, JsonValue
from hajer._observe_sink import install_from_env, uninstall
from hajer._settings import HajerSettings
from hajer._wrap import clear_wrapped_calls
from tests.fakes import FixtureAnswer, FixtureClient, Planner
from tests.repo import platform_path, requires_platform
from tests.test_observed_capture import FILES, FIXTURE, PACKAGE


def _materialise(root: Path) -> None:
    """The engine's own observed fixture, written out as an importable package. One source, two tests."""
    package = root / PACKAGE
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, path in FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((FIXTURE / name).read_text(encoding="utf-8"), encoding="utf-8")


def _payload() -> dict[str, JsonValue]:
    return {"depot": "north", "stops": ["a", "b", "c", "d", "e", "f"], "grade": "express", "distance_km": 120}


def _settings() -> HajerSettings:
    """Content and raw capture: the consent under which a wrapped call carries provider bytes."""
    return HajerSettings(capture_content=True, capture_raw=True, record_reply_reads=True)


class _NoNetworkError(RuntimeError):
    """What a session-wide transport patch raises. The suite's rule, not the SDK's."""


@pytest.fixture
def blocked_transports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every httpx transport, patched for the whole test — the customer conftest, in one fixture."""

    def refuse(*_: object, **__: object) -> object:
        raise _NoNetworkError("this suite reaches no network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.fixture
def sink_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path]:
    """`HAJER_OBSERVE_SINK` pointed at a fresh directory, and the hook removed afterwards."""
    directory = tmp_path / "sink"
    monkeypatch.setenv("HAJER_OBSERVE_SINK", str(directory))
    try:
        yield directory
    finally:
        uninstall()


def _planner(root: Path) -> Callable[[object], Planner]:
    _materialise(root)
    sys.path.insert(0, str(root))
    agent = importlib.reload(importlib.import_module(f"{PACKAGE}.agent"))
    return cast(Callable[[object], Planner], agent.RoutePlanner)


def _forget(root: Path) -> None:
    sys.path.remove(str(root))
    for name in [item for item in sys.modules if item.startswith(PACKAGE)]:
        del sys.modules[name]


@pytest.mark.usefixtures("sink_directory", "blocked_transports")
@requires_platform
def test_a_blocked_suite_still_exports_what_its_workflows_asked_for(tmp_path: Path) -> None:
    """The fixture runs, no transport answers, and the sink's document exports as `observed-calls-v1`."""
    sink = install_from_env()
    assert sink is not None, "HAJER_OBSERVE_SINK names a directory, so a sink is installed"

    application = tmp_path / "app"
    planner = _planner(application)
    try:
        for mode in ("initial", "reflect"):
            clear_wrapped_calls()
            client = FixtureClient(FixtureAnswer("propose_route", _payload()))
            with hajer.scope(workflow="route-planning"):
                planner(hajer.wrap(client, settings=_settings())).plan("north", mode, 6)
    finally:
        _forget(application)

    # Nothing left the process: the client that would have flushed cannot open a socket at all.
    with pytest.raises(_NoNetworkError):
        httpx.Client().get("http://127.0.0.1:1/observations")

    sink.flush()
    document = cast(JsonObject, json.loads(sink.path.read_text(encoding="utf-8")))
    runs = cast("list[JsonObject]", document["runs"])
    assert document["runId"] == sink.run_id
    # One run entry per declared operation, in the order the scopes were opened, and each labelled by
    # this process's own counter: two scopes, two entries, one call each.
    labels = [cast(str, run["label"]) for run in runs]
    assert len(labels) == 2, labels
    assert labels == sorted(labels, key=lambda label: int(label.removeprefix("scope-")))
    assert all(label.startswith("scope-") for label in labels)
    assert [len(cast("list[JsonValue]", run["wrappedCalls"])) for run in runs] == [1, 1]
    assert [cast("list[JsonObject]", run["wrappedCalls"])[0]["workflowHint"] for run in runs] == [
        "route-planning",
        "route-planning",
    ]

    out = tmp_path / "wire.json"
    result = subprocess.run(  # noqa: S603 - the exporter is this repository's own file
        [
            sys.executable,
            str(platform_path("tooling/dev/observation_export.py")),
            "--receipt",
            str(sink.path),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    export = cast(JsonObject, json.loads(out.read_text(encoding="utf-8")))
    observations = cast("list[JsonObject]", export["observations"])
    assert all(cast("list[JsonObject]", item["calls"])[0]["workflowHint"] == "route-planning" for item in observations)
    assert export["schemaVersion"] == "observed-calls-v1"
    assert [entry["observationId"] for entry in observations] == [f"run:{sink.run_id}:{run['label']}" for run in runs]
    calls = [call for entry in observations for call in cast("list[JsonObject]", entry["calls"])]
    assert len(calls) == 2, result.stdout
    assert all(isinstance(call["request"], dict) for call in calls), "every entry carries provider bytes"
    assert {cast(str, cast(JsonObject, call["request"])["model"]) for call in calls} == {"fixture-model"}


def test_the_sink_is_inert_when_the_variable_is_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No variable, no sink, no file, no hook: the default is the behaviour that was there before."""
    monkeypatch.delenv("HAJER_OBSERVE_SINK", raising=False)
    try:
        assert install_from_env() is None
        assert hajer.observe_sink() is None
        assert HajerSettings.from_env().observe_sink is None
        assert list(tmp_path.iterdir()) == []
    finally:
        uninstall()


@requires_platform
def test_the_fixture_sources_are_the_ones_the_engine_reads() -> None:
    """The materialised application is the committed fixture, so this test measures nothing of its own."""
    assert FILES, "the fixture labels name the sources"
    assert platform_path("tooling/dev/observation_export.py").is_file(), (
        "the exporter this test runs is the one the trace runs"
    )

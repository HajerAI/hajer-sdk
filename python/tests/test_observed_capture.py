"""The fixture application, run through `wrap` against a fake provider, produces the wire evidence.

This is the SDK half of the OBSERVED evidence path. The platform's half reads an `observed-calls-v1`
export and extracts warrants from it; this
test is what *produces* that export, by materialising the same fixture sources the platform test reads
and running them through the real wrapper. Neither side fakes the other: the bytes the engine
extracts from are the bytes this wrapper recorded off a call the fixture application actually made.

The fixture lives under `contract/observed-app/`, copied from the platform, and not here on
purpose. The engine needs it as a *repository* — files at paths, so a snapshot can hold them and a
resolver can search them — and duplicating it would be two fixtures drifting apart.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import hajer
from hajer._json import JsonObject, JsonValue
from hajer._reads import ReadSink, ReplyView
from hajer._settings import HajerSettings
from hajer._wrap import clear_wrapped_calls, wrapped_calls
from tests.fakes import FixtureAnswer, FixtureClient, Planner
from tests.repo import CONTRACT, SDK_REPOSITORY

REPOSITORY = SDK_REPOSITORY
FIXTURE = CONTRACT / "observed-app"
EXPORT = FIXTURE / "export.json"
LABELS = cast(dict[str, JsonValue], json.loads((FIXTURE / "labels.json").read_text(encoding="utf-8")))
PACKAGE = cast(str, LABELS["package"])
FILES = cast(dict[str, str], LABELS["files"])


def _materialise(root: Path) -> None:
    """The fixture's `.py.txt` sources, written out as an importable package."""
    package = root / PACKAGE
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, path in FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((FIXTURE / name).read_text(encoding="utf-8"), encoding="utf-8")


def _payload() -> dict[str, JsonValue]:
    return {"depot": "north", "stops": ["a", "b", "c", "d", "e", "f"], "grade": "express", "distance_km": 120}


def _settings(*, reads: bool = True) -> HajerSettings:
    """Content capture, raw capture and (by default) reply-read recording: the observed consent."""
    return HajerSettings(capture_content=True, capture_raw=True, record_reply_reads=reads)


def _imported() -> Callable[[object], Planner]:
    """The fixture's `RoutePlanner`, imported out of a materialised copy of its sources."""
    agent = importlib.reload(importlib.import_module(f"{PACKAGE}.agent"))
    return cast(Callable[[object], Planner], agent.RoutePlanner)


def _forget() -> None:
    for name in [item for item in sys.modules if item.startswith(PACKAGE)]:
        del sys.modules[name]


def _run(
    tmp_path: Path, *, modes: tuple[str, ...] = ("initial", "initial", "reflect"), reads: bool = True
) -> list[JsonObject]:
    """Run the fixture workflow once per mode through `wrap`, and return the wire records."""
    _materialise(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        planner = _imported()
        records: list[JsonObject] = []
        for mode in modes:
            clear_wrapped_calls()
            client = FixtureClient(FixtureAnswer("propose_route", _payload()))
            # The materialised app is the project: frames are relative to it, as `HAJER_PROJECT_ROOT` makes them.
            settings = _settings(reads=reads).model_copy(update={"project_root": str(tmp_path)})
            planner(hajer.wrap(client, settings=settings)).plan("north", mode, 6)
            records.extend(call.to_wire() for call in wrapped_calls())
        return records
    finally:
        sys.path.remove(str(tmp_path))
        _forget()


def _request(record: JsonObject) -> JsonObject:
    return cast(JsonObject, record["request"])


def _tool_names(record: JsonObject) -> tuple[str, ...]:
    tools = cast("list[JsonObject]", _request(record)["tools"])
    return tuple(sorted(cast(str, tool["name"]) for tool in tools))


def _rooted(record: JsonObject, root: Path) -> JsonObject:
    """One record reduced to what the FIXTURE REPOSITORY produced, and nothing about this machine.

    A committed fixture has to be a pure function of the committed bytes: a
    re-run of this suite in a fresh detached worktree failed five times out of five, and every
    differing byte was one of these:

    * a frame of the *harness* — this test file and the interpreter that ran it — whose `file` is an
      absolute path into whichever checkout ran and whose `line` moves whenever this file is edited.
      Those frames are real and they are recorded (`test_a_frame_is_recorded_for_the_application_and_
      not_for_this_sdk` asserts on them off the live records), but they are not the fixture's, so the
      export carries the fixture's own frames and a COUNT of the rest rather than their paths.
    * `startedAt` and `durationMs`, which are what the clock said. They are dropped here rather than
      only at comparison, so the committed artifact is itself reproducible rather than merely
      comparable after filtering.
    """
    held = {key: value for key, value in record.items() if key not in ("startedAt", "durationMs")}
    inside: list[JsonValue] = []
    outside = 0
    for frame in cast("list[JsonObject]", record["callerFrames"]):
        item: JsonObject = dict(frame)
        # The export is where the calls were made, for the OBSERVED join; a file's digest is linkage's, not its.
        item.pop("fileDigest", None)
        file = cast(str, item["file"])
        # The SDK already sends frames relative to the project root and the harness's own as `<outside>/...`.
        if file.startswith(str(root)):
            file = file[len(str(root)) + 1 :]
        elif file.startswith(("<outside>/", "/")):
            outside += 1
            continue
        item["file"] = file
        inside.append(item)
    held["callerFrames"] = inside
    held["harnessFramesOmitted"] = outside
    return held


def _export(records: list[JsonObject], root: Path) -> JsonObject:
    """The `observed-calls-v1` document, one observation per run, in run order."""
    return {
        "schemaVersion": "observed-calls-v1",
        "observations": [
            {"observationId": f"ing-fixture-{index:02d}", "calls": [_rooted(record, root)]}
            for index, record in enumerate(records)
        ],
    }


#: Fields of a captured call that are about THIS RUN and not about the call.
#:
#: `startedAt` and `durationMs` are the clock. `harnessFramesOmitted` is the third and it was found
#: the same way: the number of frames the prefix rule dropped is a function of how deep the process
#: that made the call already was, so the committed export said 3 when this file ran alone and 4 when
#: it ran inside the whole suite - a fixture that passed or failed on the order pytest collected it
#: in. The COUNT is reported on the wire because a reader of one observation wants to know frames
#: were dropped; it is not part of what the capture found, and nothing in the engine reads it.
_ABOUT_THIS_RUN = frozenset({"startedAt", "durationMs", "harnessFramesOmitted"})


def _comparable(document: JsonValue) -> JsonValue:
    """The export as it is compared. `_rooted` has already removed everything about this machine.

    Kept as a function rather than inlined because it is the one place that says what a committed
    fixture may not depend on, and because a future field that is about the clock or the checkout
    belongs here beside the two that already are.
    """
    if isinstance(document, dict):
        return {key: _comparable(value) for key, value in document.items() if key not in _ABOUT_THIS_RUN}
    if isinstance(document, list):
        return [_comparable(item) for item in document]
    return document


def test_the_export_carries_no_path_of_the_checkout_that_produced_it(tmp_path: Path) -> None:
    """A committed fixture is a pure function of the committed bytes, or it is a test of one machine.

    Two claims, both checkable from inside: no value anywhere in the export is an absolute path, and
    every frame it carries is one of the fixture repository's own.
    """
    produced = _export(_run(tmp_path), tmp_path)
    flat = json.dumps(produced)
    assert str(tmp_path) not in flat, "the temporary directory the fixture was materialised into leaked"
    assert str(REPOSITORY) not in flat, "the checkout that ran this suite leaked into the export"
    assert '"/' not in flat.replace('"/"', ""), "an absolute path leaked into the export"
    for observation in cast("list[JsonObject]", produced["observations"]):
        for call in cast("list[JsonObject]", observation["calls"]):
            for frame in cast("list[JsonObject]", call["callerFrames"]):
                assert cast(str, frame["file"]).startswith(PACKAGE), frame


def test_the_wrapper_records_the_prompt_the_tools_the_frame_and_the_reads(tmp_path: Path) -> None:
    """One call of the fixture carries all four OBSERVED facts, off the real wrapper."""
    records = _run(tmp_path)
    assert len(records) == 3, "three runs of the fixture make three captured calls"
    first = records[0]
    assert first["wire"] == "ANTHROPIC_MESSAGES"
    assert "Return AT MOST 5 stops on any one route." in cast(str, _request(first)["system"])
    assert _tool_names(first) == ("check_capacity", "propose_route"), "the initial phase binds both tools"
    frames = cast("list[JsonObject]", first["callerFrames"])
    assert any(frame["qualname"] == "RoutePlanner.plan" for frame in frames), (
        "the application frame that made the call is recorded, so a stored prompt has an address"
    )
    reads = cast("list[str]", first["replyReads"])
    assert "content[0].input" in reads, "the consumer reached the tool-use block's arguments"
    assert any(path.endswith("stops[:5]") for path in reads), "the consumer's slice is a recorded read"


def test_the_reflect_phase_binds_its_own_tool_set(tmp_path: Path) -> None:
    """The registry's second key binds one tool, so the wire sees two phases and not one."""
    assert [_tool_names(record) for record in _run(tmp_path)] == [
        ("check_capacity", "propose_route"),
        ("check_capacity", "propose_route"),
        ("propose_route",),
    ]


def test_reply_reads_are_null_when_the_recorder_is_off(tmp_path: Path) -> None:
    """`null` is "not observed" and `[]` is "read nothing"; the two are never collapsed."""
    assert _run(tmp_path, modes=("initial",), reads=False)[0]["replyReads"] is None


def test_a_frame_is_recorded_for_the_application_and_not_for_this_sdk(tmp_path: Path) -> None:
    """The prefix rule drops this package's own frames: `wrap`'s replacement is never a caller."""
    frames = cast("list[JsonObject]", _run(tmp_path, modes=("initial",))[0]["callerFrames"])
    modules = [cast(str, frame["module"]) for frame in frames]
    assert modules[0] == f"{PACKAGE}.agent", "the innermost application frame is the one that called"
    assert not any(module.startswith("hajer") for module in modules)


def test_the_committed_export_is_what_the_wrapper_produces(tmp_path: Path, update: bool) -> None:
    """The engine's fixture export is regenerated here and may not drift from the wrapper.

    Run with `--update-observed-export` to rewrite it. Without the flag a drift is a failure, which
    is what stops the engine's own test from being a test of a file somebody typed.
    """
    produced = _export(_run(tmp_path), tmp_path)
    if update:
        EXPORT.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    if not EXPORT.exists():  # pragma: no cover - only before the first write
        pytest.fail(f"{EXPORT} is missing; rerun with --update-observed-export")
    committed = cast(JsonValue, json.loads(EXPORT.read_text(encoding="utf-8")))
    assert _comparable(produced) == _comparable(committed), (
        "the committed observed-calls export is not what `wrap` produces from the fixture; "
        "rerun with --update-observed-export and commit it"
    )


# ── what a live run of a real framework forced the recorder to concede ───────────────────────────
#
# Both of these were found by running the recorder against a real application on a real framework and
# watching it raise inside that application's own code. Neither is a nicety: a recorder that breaks
# the call it is observing is not an observer.


def test_a_view_never_wraps_a_callable() -> None:
    """`dict(answer.input)` asks for `keys` and calls it; a view around a bound method is not callable.

    Measured on a framework's own tool-call reading. The rule it forced is the honest one:
    this recorder observes the VALUES an application reads, never the machinery it reads them with.
    """
    view = ReplyView({"job": "x", "n": 2}, ReadSink())
    assert dict(cast("dict[str, JsonValue]", view)) == {"job": "x", "n": 2}
    assert callable(view.keys)


def test_isinstance_about_a_view_answers_about_the_answer_it_holds() -> None:
    """A framework branches on the type of a provider value; a view that lied sent it down the wrong arm.

    `type(view)` is still `ReplyView` — the view does not claim to be the answer — but
    `isinstance` answers about what it holds, which is what every such branch actually asks.
    """

    class Held:
        def dumped(self) -> str:
            return "held"

    view = ReplyView(Held(), ReadSink())
    assert isinstance(view, Held)
    assert type(view).__name__ == "ReplyView"
    assert cast(Held, view).dumped() == "held"

"""The owned outreach app, run through the real `wrap` and `verify`, produces the rehearsal's capture bytes.

The SDK half of the maintained-suite REFERENCE_REHEARSAL join. Evidence label: IN_PROCESS — the real
wrapper over a fake provider (`tests/fakes.py`), the real client over `httpx.MockTransport`, and a scripted
SDK-side answer that is not evidence of anything the platform decided. The exact request body of each
`verify` is written to `contract/sdk-capture/export.json`; the Hajer platform's own tests replay
those bytes unchanged against its real `/verify` route over an
in-process ASGI client. No socket, no server, no real model: a recorded-byte join, never a live network run.

The clocks are pinned, and stack capture retains the real application frame while excluding the test
harness and interpreter entry point. The latter differ between `pytest` and `python -m pytest` and are
not application evidence. The export otherwise preserves exactly what the SDK sends.
Run with `--update-suite-reference-capture` to rewrite it; without the flag a drift is a failure.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, tzinfo
from typing import cast

import pytest

import hajer
from hajer._frame_context import frames_for_call
from hajer._frames import CallerFrame
from hajer._json import JsonObject, JsonValue
from hajer._paths import VERIFY_PATH
from tests.conftest import Recorder, assessment_json, responds
from tests.fakes import FakeChatCompletion, FakeChoice, FakeMessage, FakeOpenAI
from tests.outreach_app import EVIDENCE, VERIFIER, draft_outreach
from tests.repo import CONTRACT

EXPORT = CONTRACT / "sdk-capture" / "export.json"

OPTED_OUT: JsonObject = {"recipient": {"id": "r-1042", "optedOut": True}, "campaign": "renewal-reminder"}
SUBSCRIBED: JsonObject = {"recipient": {"id": "r-2001", "optedOut": False}, "campaign": "renewal-reminder"}
# A branch the producer proposed no case for: another campaign the application gained later.
WIN_BACK: JsonObject = {"recipient": {"id": "r-5005", "optedOut": True}, "campaign": "win-back"}

# (name, request, what the drafting model answers). The same opted-out request answered four ways.
TURNS: tuple[tuple[str, JsonObject, JsonObject], ...] = (
    ("baseline-suppressed", OPTED_OUT, {"proposedMessages": []}),
    (
        "candidate-violating",
        OPTED_OUT,
        {"proposedMessages": [{"recipientId": "r-1042", "body": "Your renewal is due next week."}]},
    ),
    (
        "accepted-alternative",
        OPTED_OUT,
        {"proposedMessages": [], "handoff": {"reason": "recipient opted out of contact"}},
    ),
    ("other-recipient", OPTED_OUT, {"proposedMessages": [{"recipientId": "r-2001", "body": "Renewal next week."}]}),
    ("subscribed", SUBSCRIBED, {"proposedMessages": [{"recipientId": "r-2001", "body": "Renewal next week."}]}),
    ("uncovered-branch", WIN_BACK, {"proposedMessages": []}),
)

_STARTED = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class _PinnedClock:
    """What `_wrap` reads the wall clock through (`datetime.now(UTC)`), pinned to one instant."""

    @staticmethod
    def now(tz: tzinfo) -> datetime:
        return _STARTED.astimezone(tz)


def _settings() -> hajer.HajerSettings:
    """A configured client that captures content: the owned app consents; the key never leaves this process."""
    return hajer.HajerSettings(
        api_key="key-for-tests",
        team_id="team-1",
        base_url="https://hajer.test",
        deadline_ms_default=20_000,
        capture_content=True,
    )


def _no_digest(_path: str) -> str | None:
    return None


def _application_frames() -> tuple[CallerFrame, ...]:
    return tuple(frame for frame in frames_for_call() if frame.module == "tests.outreach_app")


def _produced(monkeypatch: pytest.MonkeyPatch) -> JsonObject:
    monkeypatch.setattr(time, "monotonic_ns", lambda: 0)
    monkeypatch.setattr("hajer._wrap.datetime", _PinnedClock)
    # The harness's own frames include the virtualenv's `pytest` script, whose bytes differ per machine: the
    # export stays a pure function of the committed code by carrying no file digests.
    monkeypatch.setattr("hajer._wrap.file_digest", _no_digest)
    monkeypatch.setattr("hajer._wrap.frames_for_call", _application_frames)
    settings = _settings()
    captures: list[JsonValue] = []
    for name, request, answer in TURNS:
        recorder = Recorder(responds(assessment_json(verifier=VERIFIER)))
        model = FakeOpenAI(
            chat_script=[
                FakeChatCompletion(
                    id=f"chatcmpl-{name}",
                    model="outreach-writer",
                    choices=[FakeChoice(message=FakeMessage(content=json.dumps(answer, sort_keys=True)))],
                )
            ]
        )
        with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
            output, _ = draft_outreach(
                request,
                model_client=hajer.wrap(model, settings=settings),
                verifier_client=client,
                idempotency_key=f"rehearsal-{name}",
            )
        assert output == answer
        (sent,) = recorder.requests
        # Only the path template, the key and the body leave this test: never a header, never the api key.
        assert b"key-for-tests" not in sent.content
        captures.append({"name": name, "idempotencyKey": recorder.keys()[0], "body": sent.content.decode("utf-8")})
    return {
        "label": "REFERENCE_REHEARSAL",
        "evidence": (
            "IN_PROCESS: real hajer.wrap over a fake provider and real Hajer.verify over httpx.MockTransport; "
            "the SDK-side answer is scripted and is not evidence. Not a live network run."
        ),
        "producer": "python/tests/test_suite_reference_capture.py",
        "sdkVersion": hajer.__version__,
        "verifyPath": VERIFY_PATH,
        "captures": captures,
    }


def test_each_turn_verifies_the_whole_output_before_it_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    produced = _produced(monkeypatch)
    captures = cast(list[JsonObject], produced["captures"])
    bodies = [cast(JsonObject, json.loads(cast(str, item["body"]))) for item in captures]
    assert [body["verifier"] for body in bodies] == [VERIFIER] * len(TURNS)
    assert [body["request"] for body in bodies] == [request for _, request, _ in TURNS]
    assert [body["output"] for body in bodies] == [answer for _, _, answer in TURNS]
    assert {json.dumps(body["evidence"]) for body in bodies} == {json.dumps(EVIDENCE)}
    # One wrapped model call rides with each verify, and the pinned clocks make the bytes repeatable.
    assert [len(cast(list[JsonValue], body["wrappedCalls"])) for body in bodies] == [1] * len(TURNS)
    assert [body["deadlineMs"] for body in bodies] == [20_000] * len(TURNS)


def test_the_committed_capture_export_is_what_the_sdk_sends(
    monkeypatch: pytest.MonkeyPatch, update_suite_capture: bool
) -> None:
    produced = _produced(monkeypatch)
    if update_suite_capture:
        EXPORT.parent.mkdir(parents=True, exist_ok=True)
        EXPORT.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    if not EXPORT.exists():  # pragma: no cover - only before the first write
        pytest.fail(f"{EXPORT} is missing; rerun with --update-suite-reference-capture")
    committed = cast(JsonValue, json.loads(EXPORT.read_text(encoding="utf-8")))
    assert produced == committed, (
        "the committed suite reference capture is not what `verify` sends from the owned outreach app; "
        "rerun with --update-suite-reference-capture and commit it"
    )

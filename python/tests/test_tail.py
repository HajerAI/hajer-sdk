"""`observations()` and `python -m hajer tail` — what an operator watching an attached process sees.

Every test answers the listing from a `httpx.MockTransport`, so the rows are exactly what the route's own
schema says they are (`ObservationRowOut` in the generated wire) and nothing opens a socket.
"""

from __future__ import annotations

import httpx
import pytest

import hajer
from hajer.__main__ import main
from hajer._client import Hajer
from hajer._json import JsonObject, JsonValue
from tests.conftest import Recorder, Responder

TAIL = hajer.HajerSettings(
    api_key="key-for-tests",
    team_id="team-1",
    base_url="https://hajer.test",
    tail_interval_ms=1,
    tail_limit=7,
)


def row(**overrides: JsonValue) -> JsonObject:
    """One row of the listing, in the backend's camelCase, with the fields the route always sends."""
    body: JsonObject = {
        "id": "ing-0000000000000000000000000000000a",
        "createdAt": "2026-09-19T07:21:55.174187Z",
        "mode": "OBSERVE",
        "verifier": None,
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "redactions": 0,
        "redactedClasses": [],
        "disposition": "OUTPUT_OBSERVED",
        "shadow": True,
        "assessed": False,
    }
    body.update(overrides)
    return body


def answers(*pages: list[JsonObject]) -> Responder:
    """One response per request, in order; the last page repeats once the pages run out."""
    remaining = list(pages)

    def responder(request: httpx.Request) -> httpx.Response:
        page = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(200, json=page)

    return responder


class TestReadingTheListing:
    def test_the_rows_are_the_routes_own_shape(self) -> None:
        recorder = Recorder(answers([row(), row(id="ing-b", mode="VERIFY", verifier="ticket-reply@1")]))
        with hajer.Hajer(settings=TAIL, transport=recorder.transport()) as client:
            rows = client.observations()
        assert [entry.id for entry in rows] == ["ing-0000000000000000000000000000000a", "ing-b"]
        assert rows[0].verifier is None
        assert rows[0].provider == "anthropic"
        assert rows[0].model == "claude-sonnet-5"
        assert rows[0].disposition == "OUTPUT_OBSERVED"
        # `mode` is a `Vocabulary`: the member this client knows, and the raw string the server sent.
        assert rows[1].mode.value == "VERIFY"
        assert rows[1].mode.raw == "VERIFY"
        assert rows[1].mode.known is True
        assert rows[1].verifier == "ticket-reply@1"
        assert recorder.requests[0].url.path == "/api/teams/team-1/observations"

    def test_since_and_limit_go_on_the_query(self) -> None:
        recorder = Recorder(answers([]))
        with hajer.Hajer(settings=TAIL, transport=recorder.transport()) as client:
            assert client.observations(since="2026-09-19T07:00:00Z", limit=3) == ()
        query = recorder.requests[0].url.params
        assert query["since"] == "2026-09-19T07:00:00Z"
        assert query["limit"] == "3"

    def test_no_query_when_nothing_was_asked_for(self) -> None:
        recorder = Recorder(answers([row()]))
        with hajer.Hajer(settings=TAIL, transport=recorder.transport()) as client:
            client.observations()
        assert not recorder.requests[0].url.params

    def test_an_error_status_is_an_empty_page_and_never_an_exception(self) -> None:
        def refuses(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        recorder = Recorder(refuses)
        with hajer.Hajer(settings=TAIL, transport=recorder.transport()) as client:
            assert client.observations() == ()

    def test_a_malformed_body_is_an_empty_page(self) -> None:
        recorder = Recorder(answers([{"nothing": "like a row"}]))
        with hajer.Hajer(settings=TAIL, transport=recorder.transport()) as client:
            assert client.observations() == ()

    def test_an_inert_client_reads_nothing_and_opens_nothing(self) -> None:
        recorder = Recorder(answers([row()]))
        with hajer.Hajer(settings=hajer.HajerSettings(), transport=recorder.transport()) as client:
            assert client.observations() == ()
        assert recorder.requests == []

    async def test_the_async_client_reads_the_same_rows(self) -> None:
        recorder = Recorder(answers([row()]))
        async with hajer.AsyncHajer(settings=TAIL, transport=httpx.MockTransport(recorder.handle)) as client:
            rows = await client.observations(since="2026-09-19T07:00:00Z")
        assert len(rows) == 1
        assert recorder.requests[0].url.params["since"] == "2026-09-19T07:00:00Z"


class TestTheTailCommand:
    def test_one_line_per_observation_with_the_facts_and_nothing_else(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = Recorder(answers([row(), row(id="ing-b", verifier="ticket-reply@1", redactions=4, assessed=True)]))
        _serve(monkeypatch, recorder)

        assert main(["tail"]) == 0

        printed = capsys.readouterr().out.splitlines()
        assert len(printed) == 2
        assert "ing-0000000000000000000000000000000a" in printed[0]
        assert "OBSERVE" in printed[0]
        assert "anthropic/claude-sonnet-5" in printed[0]
        assert "redactions=0" in printed[0]
        assert "OUTPUT_OBSERVED" in printed[0]
        assert "assessed=no" in printed[0]
        assert "-" in printed[0], "a row that named no verifier prints the absent marker"
        assert "ticket-reply@1" in printed[1]
        assert "redactions=4" in printed[1]
        assert "assessed=yes" in printed[1]

    def test_a_summary_only_row_prints_no_provider(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve(monkeypatch, Recorder(answers([row(provider=None, model=None)])))

        assert main(["tail"]) == 0

        assert "anthropic" not in capsys.readouterr().out

    def test_since_is_passed_through_and_the_boundary_row_is_not_printed_twice(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inclusive boundary: the row it continued from comes back, and is dropped by id.

        Both rows share an instant — which is what one `observe` flush looks like — so the second page
        carries the first row again. A tail that printed it twice would be unreadable; one that asked
        strictly-after would never have been shown the second row at all.
        """
        first = row(id="ing-a")
        second = row(id="ing-b")
        recorder = Recorder(answers([first, second], [first, second]))
        _serve(monkeypatch, recorder)
        _stop_after(monkeypatch, sleeps=2)

        assert main(["tail", "--follow", "--limit", "2"]) == 0

        printed = capsys.readouterr().out.splitlines()
        assert [line.split()[1] for line in printed] == ["ing-a", "ing-b"], "each row once, in order"
        assert recorder.requests[1].url.params["since"] == "2026-09-19T07:21:55.174187Z"
        assert recorder.requests[1].url.params["limit"] == "2"

    def test_follow_polls_until_it_is_interrupted(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--follow` is a poll loop, and Ctrl-C ends it with a zero exit: stopping is not an error."""
        recorder = Recorder(answers([row(id="ing-a")], [row(id="ing-late", createdAt="2026-09-19T07:22:00Z")]))
        _serve(monkeypatch, recorder)
        sleeps = _stop_after(monkeypatch, sleeps=2)

        assert main(["tail", "--follow"]) == 0

        printed = capsys.readouterr().out.splitlines()
        assert [line.split()[1] for line in printed] == ["ing-a", "ing-late"]
        assert sleeps == [0.001, 0.001], "HAJER_TAIL_INTERVAL_MS, in seconds"

    def test_the_page_size_comes_from_the_setting_when_no_limit_is_given(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = Recorder(answers([row()]))
        _serve(monkeypatch, recorder)

        assert main(["tail"]) == 0

        assert recorder.requests[0].url.params["limit"] == "7", "HAJER_TAIL_LIMIT"
        assert capsys.readouterr().out

    def test_with_no_credential_it_says_so_and_exits_two(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _reads(monkeypatch, hajer.HajerSettings())

        assert main(["tail"]) == 2

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "HAJER_API_KEY" in captured.err


def _stop_after(monkeypatch: pytest.MonkeyPatch, *, sleeps: int) -> list[float]:
    """End a `--follow` the way an operator does: a `KeyboardInterrupt` out of the poll's own sleep.

    Returns the list the intervals are recorded in, so a test can assert what the loop waited.
    """
    waited: list[float] = []

    def interrupt(seconds: float) -> None:
        waited.append(seconds)
        if len(waited) >= sleeps:
            raise KeyboardInterrupt

    monkeypatch.setattr("hajer.__main__.time.sleep", interrupt)
    return waited


def _reads(monkeypatch: pytest.MonkeyPatch, settings: hajer.HajerSettings) -> None:
    """What `HajerSettings.from_env()` answers inside the CLI. The environment itself is never written."""

    def from_env(_cls: type[hajer.HajerSettings], _env: object = None) -> hajer.HajerSettings:
        return settings

    monkeypatch.setattr(hajer.HajerSettings, "from_env", classmethod(from_env))


def _serve(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    """Point the CLI at a mock transport: the settings it reads, and the socket it does not open."""
    _reads(monkeypatch, TAIL)
    original = Hajer.__init__

    def patched(self: Hajer, **kwargs: object) -> None:
        original(self, **{**kwargs, "transport": recorder.transport()})  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(Hajer, "__init__", patched)

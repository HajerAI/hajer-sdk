"""`attach()` — what a process that nobody instrumented records, and what it sends.

Every test here builds its own fake provider *module*, puts it in `sys.modules` (or lets the import hook
find it on disk), and asserts on the bodies that reached a `httpx.MockTransport`. No provider library is
installed in this environment and none is imported: that is the same constraint `wrap` lives under, and
the reason attach mode can be tested at all.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import httpx
import pytest

import hajer
from hajer._json import JsonObject
from tests.conftest import Recorder, assessment_json
from tests.fakes import FakeAnthropic, FakeAnthropicMessage, FakeAsyncAnthropic, FakeChatAnthropic

ATTACHABLE = hajer.HajerSettings(
    api_key="key-for-tests",
    team_id="team-1",
    base_url="https://hajer.test",
    observe_flush_interval_ms=600_000,
)
INERT = hajer.HajerSettings(base_url="https://hajer.test")


def accepts(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/observe"):
        return httpx.Response(202, json={"observationIds": ["obs-1"]})
    return httpx.Response(200, json=assessment_json())


def unpatch(*classes: type[object]) -> None:
    """Undo `_instrument_class` on a class, which only a test ever needs.

    A real process never does this — `detach()` deliberately leaves a patched constructor patched and
    inert, because another thread may be inside it — but these tests reuse the same fake classes, and a
    constructor that stayed wrapped from the previous test would record the next one's call twice.
    """
    for owner in classes:
        if owner.__dict__.get("__hajer_attached__") is not True:
            continue
        delattr(owner, "__hajer_attached__")
        owner.__init__ = owner.__init__.__wrapped__  # pyright: ignore[reportFunctionMemberAccess]


@pytest.fixture(autouse=True)
def _detached() -> Iterator[None]:
    """No attachment survives a test. `detach()` removes the hook and the finder; the marks are undone."""
    yield
    hajer.detach()
    unpatch(FakeAnthropic, FakeAsyncAnthropic, FakeChatAnthropic)


def provider_module(name: str) -> ModuleType:
    """A module shaped like `anthropic`: the client classes, under the names attach mode looks for."""
    module = ModuleType(name)
    module.Anthropic = FakeAnthropic  # pyright: ignore[reportAttributeAccessIssue] - a fake module
    module.AsyncAnthropic = FakeAsyncAnthropic  # pyright: ignore[reportAttributeAccessIssue]
    return module


@pytest.fixture
def anthropic_module() -> Iterator[ModuleType]:
    """`anthropic` in `sys.modules`, as a process that imported it before attaching would have it."""
    module = provider_module("anthropic")
    previous = sys.modules.get("anthropic")
    sys.modules["anthropic"] = module
    try:
        yield module
    finally:
        if previous is None:
            del sys.modules["anthropic"]
        else:  # pragma: no cover - this environment installs no provider library
            sys.modules["anthropic"] = previous


class TestWhatItInstruments:
    def test_a_client_built_after_attaching_records_its_calls(self, anthropic_module: ModuleType) -> None:
        attachment = hajer.attach(settings=INERT)
        assert attachment.classes == ("anthropic.Anthropic", "anthropic.AsyncAnthropic")
        assert attachment.modules == ("anthropic",)
        client = anthropic_module.Anthropic()
        client.messages.create(model="claude-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 1

    async def test_the_async_client_of_the_same_library_is_instrumented_too(self, anthropic_module: ModuleType) -> None:
        hajer.attach(settings=INERT)
        client = anthropic_module.AsyncAnthropic()
        await client.messages.create(model="claude-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 1

    def test_attaching_twice_changes_nothing(self, anthropic_module: ModuleType) -> None:
        first = hajer.attach(settings=INERT)
        second = hajer.attach(settings=INERT)
        assert first == second
        assert hajer.attachment() == first
        client = anthropic_module.Anthropic()
        client.messages.create(model="claude-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 1, "a doubly-patched constructor would record the call twice"

    def test_a_class_that_carries_no_surface_is_left_alone(self) -> None:
        module = ModuleType("anthropic")
        module.Anthropic = type("NotAClient", (), {})  # pyright: ignore[reportAttributeAccessIssue]
        sys.modules["anthropic"] = module
        try:
            attachment = hajer.attach(settings=INERT)
            instance = module.Anthropic()
        finally:
            del sys.modules["anthropic"]
        assert attachment.classes == ("anthropic.Anthropic",), "patched, because patching costs nothing"
        assert instance is not None
        assert hajer.wrapped_calls() == (), "and recorded nothing, because it carries no surface"

    def test_a_framework_chat_model_is_instrumented_at_its_inner_client(self) -> None:
        module = ModuleType("langchain_anthropic")
        module.ChatAnthropic = FakeChatAnthropic  # pyright: ignore[reportAttributeAccessIssue]
        sys.modules["langchain_anthropic"] = module
        try:
            attachment = hajer.attach(settings=INERT)
            assert "langchain_anthropic.ChatAnthropic" in attachment.classes
            model = module.ChatAnthropic(script=[FakeAnthropicMessage(id="msg-inner")])
            model.invoke([{"role": "user", "content": "hi"}])
        finally:
            del sys.modules["langchain_anthropic"]
        recorded = hajer.wrapped_calls()
        assert len(recorded) == 1
        assert recorded[0].response_id == "msg-inner"

    def test_a_library_imported_after_attaching_is_instrumented_by_the_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The import hook: a provider library that is not imported yet is patched when it is."""
        (tmp_path / "anthropic.py").write_text(
            textwrap.dedent("""
                class Anthropic:
                    def __init__(self):
                        self.messages = _Messages()

                class _Messages:
                    def create(self, **kwargs):
                        return _Answer()

                class _Answer:
                    id = "msg-late"
                    model = "claude-fake-1"
                """),
            encoding="utf-8",
        )
        monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
        monkeypatch.delitem(sys.modules, "anthropic", raising=False)
        attachment = hajer.attach(settings=INERT)
        assert attachment.classes == ()

        # `importlib` rather than `import anthropic`: the ban in pyproject.toml is the right ban — the SDK
        # never imports a provider library — and what this test needs is to import a module of that *name*
        # that it just wrote to a temporary directory, after `attach()`, which is the thing under test.
        late = importlib.import_module("anthropic")
        try:
            late.Anthropic().messages.create(model="claude-fake-1", messages=[])
        finally:
            monkeypatch.delitem(sys.modules, "anthropic", raising=False)
        recorded = hajer.wrapped_calls()
        assert len(recorded) == 1
        assert recorded[0].response_id == "msg-late"
        standing = hajer.attachment()
        assert standing is not None
        assert standing.modules == ("anthropic",), "the attachment is recomputed, so the late import shows"
        assert standing.classes == ("anthropic.Anthropic",)


class TestWhatItSends:
    def _bodies(self, recorder: Recorder) -> list[JsonObject]:
        sent: list[JsonObject] = []
        for body in recorder.bodies():
            observations = body.get("observations")
            assert isinstance(observations, list)
            for observation in observations:
                assert isinstance(observation, dict)
                sent.append(observation)
        return sent

    def _attached(self, recorder: Recorder, settings: hajer.HajerSettings) -> None:
        """Attach with a client whose transport is the recorder. The queue and the client are the real ones."""
        hajer.attach(settings=settings, transport=recorder.transport())

    def test_one_observation_per_call_with_no_verifier(self, anthropic_module: ModuleType) -> None:
        recorder = Recorder(accepts)
        self._attached(recorder, ATTACHABLE)
        client = anthropic_module.Anthropic(script=[FakeAnthropicMessage() for _ in range(3)])
        for _ in range(3):
            client.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": "hi"}])
        hajer.detach()  # closes the client, which flushes
        sent = self._bodies(recorder)
        assert len(sent) == 3
        for observation in sent:
            assert observation["mode"] == "OBSERVE"
            assert observation["verifier"] is None
            assert "deadlineMs" not in observation, "an OBSERVE submission is off the response path"
            assert len(observation["wrappedCalls"]) == 1  # pyright: ignore[reportArgumentType]

    def test_content_opt_out_carries_no_message_text(self, anthropic_module: ModuleType) -> None:
        """Opt-out retains shapes, names, counts, timing and usage, without message text."""
        recorder = Recorder(accepts)
        self._attached(recorder, ATTACHABLE.model_copy(update={"capture_content": False}))
        client = anthropic_module.Anthropic()
        client.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": "a secret"}])
        hajer.detach()
        observation = self._bodies(recorder)[0]
        assert "a secret" not in str(observation)
        request = observation["request"]
        assert isinstance(request, dict)
        assert request["provider"] == "anthropic"
        assert request["api"] == "messages"
        assert request["messageCount"] == 1
        assert request["messageRoles"] == ["user"]
        output = observation["output"]
        assert isinstance(output, dict)
        assert output["finishReason"] == "end_turn"
        assert output["usage"] == {"input_tokens": 13, "output_tokens": 5}
        assert observation["contentCaptured"] is False

    def test_default_content_capture_widens_the_wrapped_call_and_nothing_else(
        self, anthropic_module: ModuleType
    ) -> None:
        recorder = Recorder(accepts)
        self._attached(recorder, ATTACHABLE)
        client = anthropic_module.Anthropic()
        client.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": "a secret"}])
        hajer.detach()
        observation = self._bodies(recorder)[0]
        assert observation["contentCaptured"] is True
        assert "a secret" not in str(observation["request"]), "the tuple never carries content"
        assert "a secret" not in str(observation["output"])
        calls = observation["wrappedCalls"]
        assert isinstance(calls, list)
        assert "a secret" in str(calls[0]), "the wrapped call is where content capture shows up"

    def test_a_call_inside_a_scope_is_not_observed_on_its_own(self, anthropic_module: ModuleType) -> None:
        """Inside a scope the operation owns the call, and its `verify` is what carries it."""
        recorder = Recorder(accepts)
        self._attached(recorder, ATTACHABLE)
        client = anthropic_module.Anthropic()
        with hajer.scope() as operation:
            client.messages.create(model="claude-fake-1", messages=[])
            assert len(operation.calls) == 1
        hajer.detach()
        assert self._bodies(recorder) == []

    def test_a_failed_call_is_observed_with_its_error(self, anthropic_module: ModuleType) -> None:
        recorder = Recorder(accepts)
        self._attached(recorder, ATTACHABLE)
        from tests.fakes import ProviderError  # noqa: PLC0415 - only this test needs the error type

        client = anthropic_module.Anthropic(error=ProviderError("upstream is down"))
        with pytest.raises(ProviderError):
            client.messages.create(model="claude-fake-1", messages=[])
        hajer.detach()
        output = self._bodies(recorder)[0]["output"]
        assert isinstance(output, dict)
        assert output["errorType"] == "ProviderError"
        assert output["error"] == "upstream is down"

    def test_two_identical_calls_are_two_observations(self, anthropic_module: ModuleType) -> None:
        """`startedAt` and the response id are in the tuple, so one call is never two the same."""
        recorder = Recorder(accepts)
        self._attached(recorder, ATTACHABLE)
        client = anthropic_module.Anthropic(script=[FakeAnthropicMessage() for _ in range(2)])
        client.messages.create(model="claude-fake-1", messages=[])
        client.messages.create(model="claude-fake-1", messages=[])
        hajer.detach()
        keys = [str(observation["idempotencyKey"]) for observation in self._bodies(recorder)]
        assert len(set(keys)) == 2


class TestWithNoKey:
    def test_nothing_is_sent_and_the_attachment_says_so(self, anthropic_module: ModuleType) -> None:
        attachment = hajer.attach(settings=INERT)
        assert attachment.inert is True
        assert "inert" in attachment.describe()
        assert "anthropic" in attachment.describe()
        client = anthropic_module.Anthropic()
        client.messages.create(model="claude-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 1, "the records still exist; only the sending is off"

    def test_an_attached_process_still_says_what_it_attached(self, anthropic_module: ModuleType) -> None:
        attachment = hajer.attach(settings=ATTACHABLE)
        assert attachment.inert is False
        assert "observing outside every scope" in attachment.describe()


class TestDetach:
    def test_a_client_built_before_detaching_still_records_and_sends_nothing(
        self, anthropic_module: ModuleType
    ) -> None:
        """The instrumented surface stays instrumented; what stops is the observing."""
        recorder = Recorder(accepts)
        hajer.attach(settings=ATTACHABLE)
        client = anthropic_module.Anthropic(script=[FakeAnthropicMessage() for _ in range(2)])
        hajer.detach()
        assert hajer.attachment() is None
        client.messages.create(model="claude-fake-1", messages=[])
        assert len(hajer.wrapped_calls()) == 1, "wrap()'s own behaviour, which detaching does not undo"
        assert recorder.requests == []

    def test_a_client_built_after_detaching_is_not_instrumented(self, anthropic_module: ModuleType) -> None:
        """The patched constructor is still there and deliberately does nothing while detached."""
        hajer.attach(settings=ATTACHABLE)
        hajer.detach()
        client = anthropic_module.Anthropic()
        client.messages.create(model="claude-fake-1", messages=[])
        assert hajer.wrapped_calls() == ()

    def test_detaching_twice_is_a_no_op(self) -> None:
        hajer.detach()
        hajer.detach()
        assert hajer.attachment() is None


class TestTheImportTimeHook:
    def test_the_shim_directory_holds_a_sitecustomize(self) -> None:
        """`python -m hajer attach-path` prints this directory; the interpreter imports what is in it."""
        from hajer import _bootstrap  # noqa: PLC0415 - the module under test is the shim's own package

        shim = Path(_bootstrap.__file__).resolve().parent / "sitecustomize.py"
        assert shim.is_file()
        assert "import hajer.autoattach" in shim.read_text(encoding="utf-8")

    def test_attach_path_prints_that_directory(self, capsys: pytest.CaptureFixture[str]) -> None:
        from hajer.__main__ import main  # noqa: PLC0415 - the CLI is only reached from a shell and here

        assert main(["attach-path"]) == 0
        printed = capsys.readouterr().out.strip()
        assert printed.endswith("hajer/_bootstrap")
        assert (Path(printed) / "sitecustomize.py").is_file()

    def test_the_switch_is_a_setting_read_from_the_environment(self) -> None:
        assert hajer.HajerSettings.from_env({}).attach is False
        assert hajer.HajerSettings.from_env({"HAJER_ATTACH": "1"}).attach is True
        assert hajer.HajerSettings.from_env({"HAJER_ATTACH": "off"}).attach is False

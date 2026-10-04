"""`attach()` — what a process that nobody instrumented records.

Every test here builds its own fake provider *module*, puts it in `sys.modules` (or lets the import hook
find it on disk), and asserts on the calls that were recorded. No provider library is installed in this
environment and none is imported: that is the same constraint `wrap` lives under, and the reason attach
mode can be tested at all.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

import hajer
from tests.fakes import FakeAnthropic, FakeAnthropicMessage, FakeAsyncAnthropic, FakeChatAnthropic, ProviderError

ATTACHABLE = hajer.HajerSettings(api_key="key-for-tests", team_id="team-1", base_url="https://hajer.test")
INERT = hajer.HajerSettings(base_url="https://hajer.test")


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


class TestWhatItRecords:
    def test_one_record_per_call(self, anthropic_module: ModuleType) -> None:
        attachment = hajer.attach(settings=ATTACHABLE)
        assert attachment.inert is False
        client = anthropic_module.Anthropic(script=[FakeAnthropicMessage() for _ in range(3)])
        for _ in range(3):
            client.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": "hi"}])
        assert [call.provider for call in hajer.wrapped_calls()] == ["anthropic"] * 3

    def test_content_opt_out_carries_no_message_text(self, anthropic_module: ModuleType) -> None:
        """Opt-out retains shapes, names, counts, timing and usage, without message text."""
        hajer.attach(settings=ATTACHABLE.model_copy(update={"capture_content": False}))
        client = anthropic_module.Anthropic()
        client.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": "a secret"}])
        (call,) = hajer.wrapped_calls()
        assert "a secret" not in repr(call)
        assert call.content is None
        assert (call.provider, call.api, call.message_count, call.message_roles) == (
            "anthropic",
            "messages",
            1,
            ("user",),
        )
        assert call.finish_reason == "end_turn"
        assert call.usage == {"input_tokens": 13, "output_tokens": 5}

    def test_default_content_capture_is_on(self, anthropic_module: ModuleType) -> None:
        hajer.attach(settings=ATTACHABLE)
        client = anthropic_module.Anthropic()
        client.messages.create(model="claude-fake-1", messages=[{"role": "user", "content": "a secret"}])
        (call,) = hajer.wrapped_calls()
        assert call.content is not None
        assert "a secret" in repr(call.content["messages"])

    def test_a_call_inside_a_scope_belongs_to_the_operation(self, anthropic_module: ModuleType) -> None:
        hajer.attach(settings=ATTACHABLE)
        client = anthropic_module.Anthropic()
        with hajer.scope() as operation:
            client.messages.create(model="claude-fake-1", messages=[])
            assert len(operation.calls) == 1
        assert hajer.wrapped_calls() == (), "the operation took it; the task context holds nothing"

    def test_a_failed_call_is_recorded_with_its_error(self, anthropic_module: ModuleType) -> None:
        hajer.attach(settings=ATTACHABLE)
        client = anthropic_module.Anthropic(error=ProviderError("upstream is down"))
        with pytest.raises(ProviderError):
            client.messages.create(model="claude-fake-1", messages=[])
        (call,) = hajer.wrapped_calls()
        assert call.error_type == "ProviderError"

    def test_detach_stops_instrumenting_new_clients(self, anthropic_module: ModuleType) -> None:
        hajer.attach(settings=ATTACHABLE)
        hajer.detach()
        assert hajer.attachment() is None
        anthropic_module.Anthropic().messages.create(model="claude-fake-1", messages=[])
        assert hajer.wrapped_calls() == ()

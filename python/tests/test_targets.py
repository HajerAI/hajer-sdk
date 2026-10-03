"""litellm and google-genai, instrumented by `attach()` and by `wrap()` — with fakes, never a network.

Neither library is installed here and neither is imported: `sys.modules` is given a module object and a
client object shaped the way the real ones are, which is exactly the path a real process takes, because
`wrap` and `attach` find surfaces by attribute and import nothing.

The five claims:

- `acompletion` is **awaited** and recorded — and the replacement is still a coroutine function, so a
  framework that introspects it before calling sees what it saw before Hajer was in the process;
- usage priced from an **object or a dict**, because litellm returns both shapes in the field;
- a name bound by `from litellm import completion` **before** `attach()` is patched, so import order
  does not decide whether a call is captured;
- the genai **client and its stream** are recorded, including the usage in the final chunk;
- a recording that fails **never reaches the caller**.
"""

from __future__ import annotations

import inspect
import sys
import types
from typing import Final

import pytest

import hajer
from hajer import _targets_genai, _targets_litellm
from hajer._targets import rebind
from tests.conftest import as_async_stream, as_stream
from tests.fakes import (
    LITELLM_ANSWER,
    FakeGenaiCandidate,
    FakeGenaiClient,
    FakeGenaiContent,
    FakeGenaiFunctionCall,
    FakeGenaiPart,
    FakeGenaiResponse,
    FakeGenaiUsage,
    FakeLitellmResponse,
    fake_litellm,
)

QUIET: Final[hajer.HajerSettings] = hajer.HajerSettings(api_key="k", team_id="t", capture_content=False)
COUNTS: Final[dict[str, int]] = {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14}


class TestLitellm:
    def test_the_upstream_provider_comes_out_of_the_model_prefix(self) -> None:
        """A receipt about the call, not about the router: `anthropic` / `claude-sonnet-5`."""
        module = fake_litellm(answer=LITELLM_ANSWER)
        assert _targets_litellm.instrument(module, QUIET) == 2
        module.completion(model="anthropic/claude-sonnet-5", messages=[])
        (call,) = hajer.wrapped_calls()
        assert call.provider == "anthropic"
        assert call.model == "claude-sonnet-5"
        assert call.api == "litellm.completion"
        assert call.finish_reason == "stop"
        assert call.response_id == "chatcmpl-litellm-1"

    def test_an_unprefixed_model_stays_with_the_router(self) -> None:
        """litellm resolves `gpt-4o` itself; this seam was told nothing else, so it says litellm."""
        assert _targets_litellm.upstream("gpt-4o") == ("litellm", "gpt-4o")
        assert _targets_litellm.upstream(None) == ("litellm", None)
        assert _targets_litellm.upstream("anthropic/claude-sonnet-5") == ("anthropic", "claude-sonnet-5")

    async def test_acompletion_is_awaited_and_recorded(self) -> None:
        """The coroutine entry point: awaited here, recorded when the answer arrives, still a coroutine."""
        module = fake_litellm(answer=LITELLM_ANSWER)
        assert _targets_litellm.instrument(module, QUIET) == 2
        assert inspect.iscoroutinefunction(module.acompletion), (
            "a framework that introspects the function before calling it must see what it saw before"
        )
        answer = await module.acompletion(model="anthropic/claude-sonnet-5", messages=[])
        assert answer is LITELLM_ANSWER, "the caller's value is the provider's, untouched"
        (call,) = hajer.wrapped_calls()
        assert call.provider == "anthropic"
        assert call.usage == COUNTS
        assert call.duration_ms >= 0

    def test_usage_object_and_dict_both_priced(self) -> None:
        """Both shapes arrive in the field, and both have to produce the same three counts."""
        for answer in (LITELLM_ANSWER, FakeLitellmResponse()):
            hajer.clear_wrapped_calls()
            module = fake_litellm(answer=answer)
            _targets_litellm.instrument(module, QUIET)
            module.completion(model="anthropic/claude-sonnet-5", messages=[])
            (call,) = hajer.wrapped_calls()
            assert call.usage == COUNTS, f"usage was not read off {type(answer).__name__}"
            assert call.model == "claude-sonnet-5"

    def test_from_import_bound_before_attach_is_patched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`from litellm import completion` binds the function object. Attaching has to move that too.

        Without this, capture would depend on whether the customer's module was imported before or after
        `hajer.attach()` — which is the one thing a monitoring tool must never make somebody reason about.
        """
        module = fake_litellm(answer=LITELLM_ANSWER)
        monkeypatch.setitem(sys.modules, "litellm", module)
        caller = types.ModuleType("customer_app")
        # The `from litellm import completion` binding: the function object, in somebody else's namespace.
        caller.completion = module.completion  # pyright: ignore[reportAttributeAccessIssue] - a namespace
        monkeypatch.setitem(sys.modules, "customer_app", caller)
        original = caller.completion

        attachment = hajer.attach(settings=QUIET.model_copy(update={"api_key": None}))
        try:
            assert "litellm" in attachment.modules
            assert caller.completion is not original, "the alias still points at the original"
            caller.completion(model="anthropic/claude-sonnet-5", messages=[])
            (call,) = hajer.wrapped_calls()
            assert call.provider == "anthropic"
        finally:
            hajer.detach()

    def test_rebind_moves_only_what_is_still_the_original(self) -> None:
        """By identity, so an unrelated function of the same name in another module is left alone."""

        def original() -> None: ...

        def replacement() -> None: ...

        def namesake() -> None: ...

        holder = types.ModuleType("holder_module")
        holder.completion = original  # pyright: ignore[reportAttributeAccessIssue] - a namespace
        other = types.ModuleType("other_module")
        other.completion = namesake  # pyright: ignore[reportAttributeAccessIssue] - a namespace
        sys.modules["holder_module"] = holder
        sys.modules["other_module"] = other
        try:
            assert rebind(original, replacement) == 1
            assert holder.completion is replacement
            assert other.completion is namesake
            assert rebind(original, replacement) == 0, "running twice moves nothing the second time"
        finally:
            del sys.modules["holder_module"]
            del sys.modules["other_module"]

    def test_instrumenting_twice_records_one_call(self) -> None:
        module = fake_litellm(answer=LITELLM_ANSWER)
        assert _targets_litellm.instrument(module, QUIET) == 2
        assert _targets_litellm.instrument(module, QUIET) == 0
        module.completion(model="anthropic/claude-sonnet-5", messages=[])
        assert len(hajer.wrapped_calls()) == 1

    def test_a_module_that_is_not_litellm_is_left_alone(self) -> None:
        """A customer's own `completion()` helper is not a router, and is not instrumented as one."""
        impostor = types.ModuleType("my_helpers")

        def completion(**_: object) -> None: ...

        impostor.completion = completion  # pyright: ignore[reportAttributeAccessIssue] - a namespace
        assert _targets_litellm.instrument(impostor, QUIET) == 0

    def test_the_provider_exception_is_re_raised_unchanged(self) -> None:
        boom = RuntimeError("upstream refused")
        module = fake_litellm(answer=LITELLM_ANSWER, error=boom)
        _targets_litellm.instrument(module, QUIET)
        with pytest.raises(RuntimeError) as raised:
            module.completion(model="anthropic/claude-sonnet-5", messages=[])
        assert raised.value is boom
        (call,) = hajer.wrapped_calls()
        assert call.error == "upstream refused"
        assert call.error_type == "RuntimeError"

    def test_wrap_takes_the_module_too(self) -> None:
        """A developer who would rather name the library than attach the process gets the same capture."""
        module = fake_litellm(answer=LITELLM_ANSWER)
        assert hajer.wrap(module, settings=QUIET) is module
        module.completion(model="anthropic/claude-sonnet-5", messages=[])
        assert len(hajer.wrapped_calls()) == 1


class TestGoogleGenai:
    def test_genai_client_and_stream_are_recorded(self) -> None:
        """Both methods: the one-piece answer, and `generate_content_stream`, which streams by itself.

        The stream's usage is in its last frame, and the record has it the moment the caller stops reading
        — which for a genai stream is exhaustion, since there is no context manager to close.
        """
        chunks: list[object] = [
            FakeGenaiResponse(candidates=[FakeGenaiCandidate(content=FakeGenaiContent(parts=[FakeGenaiPart("he")]))]),
            FakeGenaiResponse(usage_metadata=FakeGenaiUsage(prompt_token_count=7, candidates_token_count=2)),
        ]
        client = hajer.wrap(FakeGenaiClient(chunks=chunks), settings=QUIET)

        answered = client.models.generate_content(model="gemini-2.5-pro", contents="hi")
        assert isinstance(answered, FakeGenaiResponse)
        streamed = as_stream(client.models.generate_content_stream(model="gemini-2.5-pro", contents="hi"))
        assert len(list(streamed)) == 2

        one, two = hajer.wrapped_calls()
        assert one.provider == "google"
        assert one.model == "gemini-2.5-pro", "read from `model_version`"
        assert one.response_id == "genai-1"
        assert one.finish_reason == "STOP", "an enum member is recorded by its name"
        assert one.usage == {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}
        assert one.streamed is False
        assert two.api == "genai.generate_content_stream"
        assert two.streamed is True
        assert two.stream_complete is True
        assert two.stream_chunks == 2
        assert two.usage == {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}
        assert two.limitations == ()

    async def test_the_async_client_is_recorded_too(self) -> None:
        chunks: list[object] = [FakeGenaiResponse(usage_metadata=FakeGenaiUsage())]
        client = hajer.wrap(FakeGenaiClient(chunks=chunks), settings=QUIET)
        answered = await client.aio.models.generate_content(model="gemini-2.5-pro", contents="hi")
        assert isinstance(answered, FakeGenaiResponse)
        streamed = as_async_stream(
            await client.aio.models.generate_content_stream(model="gemini-2.5-pro", contents="hi")
        )
        assert len([chunk async for chunk in streamed]) == 1
        one, two = hajer.wrapped_calls()
        assert one.provider == "google"
        assert one.usage["input_tokens"] == 7
        assert two.streamed is True
        assert two.stream_complete is True

    def test_tool_calls_are_read_from_the_parts(self) -> None:
        part = FakeGenaiPart(function_call=FakeGenaiFunctionCall(name="lookup", args={"q": "1"}))
        answer = FakeGenaiResponse(candidates=[FakeGenaiCandidate(content=FakeGenaiContent(parts=[part]))])
        client = hajer.wrap(FakeGenaiClient(answer=answer), settings=QUIET)
        client.models.generate_content(model="gemini-2.5-pro", contents="hi")
        (call,) = hajer.wrapped_calls()
        assert [tool.name for tool in call.tool_calls] == ["lookup"]
        assert call.tool_calls[0].arguments is None, "tool arguments are content, and this test opts out"

    def test_an_object_that_is_not_a_genai_client_is_left_alone(self) -> None:
        assert _targets_genai.instrument(types.SimpleNamespace(models=object()), QUIET) == 0

    def test_a_raw_capture_is_refused_for_a_library_the_service_cannot_read(self) -> None:
        """`HAJER_CAPTURE_RAW` names three provider wires. This is not one, and the record says so."""
        loud = QUIET.model_copy(update={"capture_content": True, "capture_raw": True})
        client = hajer.wrap(FakeGenaiClient(), settings=loud)
        client.models.generate_content(model="gemini-2.5-pro", contents="hi")
        (call,) = hajer.wrapped_calls()
        assert call.raw is None
        assert len(call.limitations) == 1
        assert "not one of the three provider wires" in call.limitations[0]


class TestRecordingNeverReachesTheCaller:
    def test_recording_failure_never_reaches_the_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The record is broken on purpose. The caller's answer arrives anyway, unchanged.

        `_begin` is the first thing the replacement does and the one thing no later `try` could cover, so
        breaking it is the strongest form of the claim: a Hajer bug at the very start of the instrumented
        path is still not the customer's exception.
        """
        module = fake_litellm(answer=LITELLM_ANSWER)
        _targets_litellm.instrument(module, QUIET)
        monkeypatch.setattr("hajer._wrap._begin", _explode)
        assert module.completion(model="anthropic/claude-sonnet-5", messages=[]) is LITELLM_ANSWER
        assert hajer.wrapped_calls() == (), "nothing was recorded, and nothing was raised"

    async def test_a_broken_record_does_not_break_an_awaited_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = fake_litellm(answer=LITELLM_ANSWER)
        _targets_litellm.instrument(module, QUIET)
        monkeypatch.setattr("hajer._wrap._begin", _explode)
        assert await module.acompletion(model="anthropic/claude-sonnet-5", messages=[]) is LITELLM_ANSWER

    def test_a_broken_reader_does_not_break_the_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = fake_litellm(answer=LITELLM_ANSWER)
        _targets_litellm.instrument(module, QUIET)
        monkeypatch.setattr("hajer._targets_litellm.read_chat_completion", _explode)
        assert module.completion(model="anthropic/claude-sonnet-5", messages=[]) is LITELLM_ANSWER
        (call,) = hajer.wrapped_calls()
        assert call.provider == "litellm", "the record exists; only the reading of the answer was lost"


def _explode(*_: object, **__: object) -> object:
    raise RuntimeError("the record is broken")

"""`wrap()` returns the very object it is handed, instrumented in place, so the install PR can
wrap any supported client at its construction expression wherever the value goes next: `isinstance` still holds,
attribute access is the object's own, and LangChain Runnable composition (`|`, `.bind_tools`,
`.with_structured_output`) keeps working on the wrapped chat model and still records every call.

The provider is `httpx.MockTransport`; `langchain-openai` is a dev dependency only (imported by name, never by the SDK).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from typing import Protocol, cast
from unittest import mock

import httpx

import hajer
from tests.fakes import FakeAnthropic, FakeChatAnthropic, FakeOpenAI

QUIET = hajer.HajerSettings(capture_content=False)


def _completion(request: httpx.Request) -> httpx.Response:
    body = cast(dict[str, object], json.loads(request.content))
    tools = cast(list[dict[str, dict[str, str]]], body.get("tools") or [])
    message: dict[str, object] = {"role": "assistant", "content": "hello"}
    if tools:
        name = tools[0]["function"]["name"]
        call = {"id": "call_1", "type": "function", "function": {"name": name, "arguments": '{"answer": "ok"}'}}
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
    choice = {"index": 0, "message": message, "finish_reason": "tool_calls" if tools else "stop"}
    usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [choice],
        "usage": usage,
    }
    return httpx.Response(200, json=payload)


class _Message(Protocol):
    content: object
    tool_calls: list[dict[str, object]]


class _Completions(Protocol):
    def create(self, **kwargs: object) -> object: ...


class _Chat(Protocol):
    @property
    def completions(self) -> _Completions: ...


class _OpenAIClient(Protocol):
    @property
    def chat(self) -> _Chat: ...


def _calls(operation: hajer.Operation) -> tuple[hajer.WrappedCall, ...]:
    return operation.calls


def _count(operation: hajer.Operation) -> int:
    """Read through a call, so the checker does not narrow one `len(...) == n` against the next."""
    return len(_calls(operation))


class _Runnable(Protocol):
    def invoke(self, value: object, /) -> object: ...


def _chat_openai() -> tuple[object, type[object]]:
    module = importlib.import_module("langchain_openai")
    kind = cast(type[object], module.ChatOpenAI)
    make = cast(Callable[..., object], kind)
    http = httpx.Client(transport=httpx.MockTransport(_completion))
    return make(model="gpt-fake-1", api_key="fake", max_retries=0, http_client=http), kind


class TestWrapReturnsTheSameObject:
    def test_isinstance_and_attributes_hold_for_every_fake_shape(self) -> None:
        for shape in (FakeOpenAI, FakeAnthropic, FakeChatAnthropic):
            client = shape()
            wrapped = hajer.wrap(client, settings=QUIET)
            assert wrapped is client
            assert isinstance(wrapped, shape)
            assert vars(wrapped) is vars(client)  # every attribute read or set is the client's own

    def test_the_real_openai_client_keeps_its_type_attributes_and_records(self) -> None:
        openai = importlib.import_module("openai")
        kind = cast(type[object], openai.OpenAI)
        http = httpx.Client(transport=httpx.MockTransport(_completion))
        client = cast(Callable[..., object], kind)(api_key="fake", max_retries=0, http_client=http)
        wrapped = hajer.wrap(client, settings=QUIET)
        assert wrapped is client
        assert isinstance(wrapped, kind)
        assert cast(str, wrapped.api_key) == "fake"  # pyright: ignore[reportAttributeAccessIssue]
        cast(_OpenAIClient, wrapped).chat.completions.create(
            model="gpt-fake-1", messages=[{"role": "user", "content": "hi"}]
        )
        assert len(hajer.wrapped_calls()) == 1


class TestLangChainCompositionOnTheWrappedModel:
    def test_pipe_bind_tools_and_structured_output_keep_working_and_record(self) -> None:
        model, kind = _chat_openai()
        wrapped = hajer.wrap(model, settings=QUIET)
        assert wrapped is model
        assert isinstance(wrapped, kind)
        assert cast(str, wrapped.model_name) == "gpt-fake-1"  # pyright: ignore[reportAttributeAccessIssue]
        # LangChain runs each step in a copied context, so the calls are read the way an application reads them:
        # through one `hajer.scope()` operation, which every call made inside it reaches whichever task made it.
        with hajer.scope(workflow="compose") as operation:
            prompts = importlib.import_module("langchain_core.prompts")
            template = cast(Callable[[str], object], prompts.ChatPromptTemplate.from_template)("Say {word}")
            chain = cast(_Runnable, template | wrapped)  # pyright: ignore[reportOperatorIssue]
            reply = cast(_Message, chain.invoke({"word": "hello"}))
            assert reply.content == "hello"
            assert _count(operation) == 1

            def lookup(query: str) -> str:
                """Look a thing up."""
                return query

            bind = cast(Callable[[list[object]], _Runnable], wrapped.bind_tools)  # pyright: ignore[reportAttributeAccessIssue]
            called = cast(_Message, bind([lookup]).invoke("find it"))
            assert called.tool_calls[0]["name"] == "lookup"
            assert _count(operation) == 2

            schema = {
                "title": "Answer",
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            }
            structured = cast(Callable[..., _Runnable], wrapped.with_structured_output)  # pyright: ignore[reportAttributeAccessIssue]
            assert structured(schema, method="function_calling").invoke("answer me") == {"answer": "ok"}
            assert _count(operation) == 3
            assert {call.model for call in _calls(operation)} == {"gpt-fake-1"}


class TestMocksAreLeftAlone:
    def test_a_patched_provider_class_mock_keeps_its_assertions(self) -> None:
        """A team's test patches `openai.OpenAI`; the install PR's `hajer.wrap(OpenAI())` then wraps a MagicMock."""
        for double in (mock.MagicMock(), mock.Mock(), mock.NonCallableMagicMock(), mock.AsyncMock()):
            assert hajer.wrap(double, settings=QUIET) is double
        client = mock.MagicMock()
        wrapped = hajer.wrap(client, settings=QUIET)
        wrapped.chat.completions.create(model="gpt-fake-1", messages=[])
        client.chat.completions.create.assert_called_once_with(model="gpt-fake-1", messages=[])
        assert hajer.wrapped_calls() == ()

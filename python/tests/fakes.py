"""Fake OpenAI and Anthropic clients, shaped like the real ones and reached by nothing.

`wrap()` reads provider objects by attribute and imports neither library, so these fakes exercise the
same code path the real clients do. No test in this package imports `openai` or `anthropic`, and none
opens a socket to a provider.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from types import ModuleType, TracebackType
from typing import Protocol

from hajer._json import JsonValue


@dataclass
class ProviderObject:
    """What every provider response object has that a plain dataclass does not: `model_dump`.

    The real `openai` and `anthropic` response types are pydantic models, and `wrap` reads a whole
    response document through `model_dump(mode="json")` for a raw capture. A fake without one would
    make raw capture look broken here and work in production, which is the wrong way round for a
    test seam."""

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        return dataclasses.asdict(self)


# ── OpenAI chat completions ──────────────────────────────────────────────────────────────────────
@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass
class FakeMessage:
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[FakeToolCall] = field(default_factory=list)


@dataclass
class FakeChoice:
    message: FakeMessage
    finish_reason: str = "stop"
    index: int = 0


@dataclass
class FakeOpenAIUsage:
    prompt_tokens: int = 11
    completion_tokens: int = 7
    total_tokens: int = 18


@dataclass
class FakeChatCompletion(ProviderObject):
    id: str = "chatcmpl-1"
    model: str = "gpt-fake-1"
    choices: list[FakeChoice] = field(default_factory=lambda: [FakeChoice(message=FakeMessage(content="ok"))])
    usage: FakeOpenAIUsage = field(default_factory=FakeOpenAIUsage)


@dataclass
class FakeChatDelta:
    content: str | None = None


@dataclass
class FakeChatStreamChoice:
    delta: FakeChatDelta
    finish_reason: str | None = None


@dataclass
class FakeChatChunk(ProviderObject):
    choices: list[FakeChatStreamChoice]
    id: str = "chatcmpl-1"
    usage: FakeOpenAIUsage | None = None


# ── OpenAI responses ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FakeResponseItem:
    type: str
    content: str | None = None
    name: str | None = None
    call_id: str | None = None
    arguments: str | None = None


@dataclass
class FakeResponse(ProviderObject):
    id: str = "resp-1"
    model: str = "gpt-fake-1"
    status: str = "completed"
    output: list[FakeResponseItem] = field(default_factory=lambda: [FakeResponseItem(type="message", content="ok")])
    usage: FakeOpenAIUsage = field(default_factory=FakeOpenAIUsage)


# ── Anthropic messages ───────────────────────────────────────────────────────────────────────────
@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, str]
    type: str = "tool_use"


@dataclass
class FakeAnthropicUsage:
    input_tokens: int = 13
    output_tokens: int = 5


@dataclass
class FakeAnthropicMessage(ProviderObject):
    id: str = "msg-1"
    model: str = "claude-fake-1"
    stop_reason: str = "end_turn"
    content: list[FakeTextBlock | FakeToolUseBlock] = field(default_factory=lambda: [FakeTextBlock(text="ok")])
    usage: FakeAnthropicUsage = field(default_factory=FakeAnthropicUsage)


@dataclass
class FakeAnthropicDelta:
    text: str | None = None


@dataclass
class FakeAnthropicEvent(ProviderObject):
    type: str = "content_block_delta"
    delta: FakeAnthropicDelta = field(default_factory=FakeAnthropicDelta)
    usage: FakeAnthropicUsage | None = None
    stop_reason: str | None = None


# ── the stream containers the providers return ───────────────────────────────────────────────────
class FakeStream:
    """A provider stream: iterable once, a context manager, closeable."""

    def __init__(self, chunks: list[object]) -> None:
        self.chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[object]:
        yield from self.chunks

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True


class FakeAsyncStream:
    """The async twin."""

    def __init__(self, chunks: list[object]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[object]:
        for chunk in self.chunks:
            yield chunk

    async def __aenter__(self) -> FakeAsyncStream:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed = True


class FakeMessageStream:
    """`anthropic.MessageStream` as `wrap` sees it: the events, plus the provider's own helpers.

    The helpers are the point. A caller who reads `text_stream` never touches the event iterator, so the
    chunks do not cross the proxy — and `get_final_message()` is how that caller reads the answer, which
    is why the proxy records what it returns.
    """

    def __init__(self, events: list[object], final: object) -> None:
        self.events = events
        self.final = final
        self.response: object = object()
        self.closed = False
        self.drained = False

    def __iter__(self) -> Iterator[object]:
        yield from self.events

    @property
    def text_stream(self) -> Iterator[str]:
        for event in self.events:
            text = getattr(getattr(event, "delta", None), "text", None)
            if isinstance(text, str):
                yield text

    def until_done(self) -> None:
        self.drained = True

    def get_final_message(self) -> object:
        return self.final

    def close(self) -> None:
        self.closed = True


class FakeMessageStreamManager:
    """What `messages.stream(...)` returns: a context manager whose `__enter__` is the stream."""

    def __init__(self, stream: FakeMessageStream) -> None:
        self.stream = stream
        self.entered = False

    def __enter__(self) -> FakeMessageStream:
        self.entered = True
        return self.stream

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.stream.close()


class FakeAsyncMessageStream:
    """The async twin: `__aiter__`, an async `get_final_message()`, `close()` as a coroutine."""

    def __init__(self, events: list[object], final: object) -> None:
        self.events = events
        self.final = final
        self.response: object = object()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[object]:
        for event in self.events:
            yield event

    async def get_final_message(self) -> object:
        return self.final

    async def close(self) -> None:
        self.closed = True


class FakeAsyncMessageStreamManager:
    """What `AsyncAnthropic().messages.stream(...)` returns: an async context manager."""

    def __init__(self, stream: FakeAsyncMessageStream) -> None:
        self.stream = stream
        self.entered = False

    async def __aenter__(self) -> FakeAsyncMessageStream:
        self.entered = True
        return self.stream

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stream.close()


class ProviderError(RuntimeError):
    """What a provider raises. The SDK must re-raise this exact class, unchanged.

    `status_code` is what the real provider exceptions carry when the answer came back with a status
    line; `None` is a call that never got one (a connection that never opened), which is a different
    fact and the one that leaves a raw capture with nothing to capture."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ── the clients ──────────────────────────────────────────────────────────────────────────────────
class _Endpoint:
    """One `create`, scripted: a list of things to return, or an exception to raise."""

    def __init__(self, script: list[object] | None = None, error: BaseException | None = None) -> None:
        self.script: list[object] = script if script is not None else []
        self.error = error
        self.calls: list[dict[str, object]] = []

    def _next(self, kwargs: dict[str, object]) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.script.pop(0) if self.script else FakeChatCompletion()


class SyncEndpoint(_Endpoint):
    def create(self, **kwargs: object) -> object:
        return self._next(kwargs)


class AsyncEndpoint(_Endpoint):
    async def create(self, **kwargs: object) -> object:
        return self._next(kwargs)


class SyncStreamingEndpoint(SyncEndpoint):
    """`messages.create` and the provider's own `messages.stream` beside it, scripted separately."""

    def __init__(
        self,
        script: list[object] | None = None,
        error: BaseException | None = None,
        stream_script: list[object] | None = None,
    ) -> None:
        super().__init__(script, error)
        self.stream_script: list[object] = stream_script if stream_script is not None else []
        self.stream_calls: list[dict[str, object]] = []

    def stream(self, **kwargs: object) -> object:
        self.stream_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.stream_script.pop(0)


class AsyncStreamingEndpoint(AsyncEndpoint):
    """The async twin. `stream` is **not** a coroutine on the real client: it returns the manager."""

    def __init__(
        self,
        script: list[object] | None = None,
        error: BaseException | None = None,
        stream_script: list[object] | None = None,
    ) -> None:
        super().__init__(script, error)
        self.stream_script: list[object] = stream_script if stream_script is not None else []
        self.stream_calls: list[dict[str, object]] = []

    def stream(self, **kwargs: object) -> object:
        self.stream_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.stream_script.pop(0)


class _SyncChat:
    def __init__(self, completions: SyncEndpoint) -> None:
        self.completions = completions


class _AsyncChat:
    def __init__(self, completions: AsyncEndpoint) -> None:
        self.completions = completions


class FakeOpenAI:
    """`openai.OpenAI` as `wrap` sees it: `chat.completions.create` and `responses.create`."""

    def __init__(
        self,
        *,
        chat_script: list[object] | None = None,
        responses_script: list[object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.completions = SyncEndpoint(chat_script, error)
        self.chat = _SyncChat(self.completions)
        self.responses = SyncEndpoint(responses_script, error)


class FakeAsyncOpenAI:
    """`openai.AsyncOpenAI`: the same surfaces, `create` is a coroutine function."""

    def __init__(
        self,
        *,
        chat_script: list[object] | None = None,
        responses_script: list[object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.completions = AsyncEndpoint(chat_script, error)
        self.chat = _AsyncChat(self.completions)
        self.responses = AsyncEndpoint(responses_script, error)


class _Beta:
    """`anthropic.Anthropic().beta`: the same messages contract behind a beta header."""

    def __init__(self, messages: SyncEndpoint | AsyncEndpoint) -> None:
        self.messages = messages


class FakeAnthropic:
    """`anthropic.Anthropic`: `messages.create`, and the transport it keeps on `_client`.

    `_client` is there because the real client has it: it is the httpx client, it carries no provider
    surface, and `wrap` must not follow it in preference to the `messages.create` this object itself
    has. Every anthropic test in this package therefore exercises that ordering for free.
    """

    def __init__(
        self,
        *,
        script: list[object] | None = None,
        error: BaseException | None = None,
        stream_script: list[object] | None = None,
    ) -> None:
        answers: list[object] = script if script is not None else [FakeAnthropicMessage()]
        self.messages = SyncStreamingEndpoint(answers, error, stream_script)
        self.beta = _Beta(SyncEndpoint(list(answers), error))
        self._client: object = object()


class FakeAsyncAnthropic:
    """`anthropic.AsyncAnthropic`: `messages.create` as a coroutine function."""

    def __init__(
        self,
        *,
        script: list[object] | None = None,
        error: BaseException | None = None,
        stream_script: list[object] | None = None,
    ) -> None:
        answers: list[object] = script if script is not None else [FakeAnthropicMessage()]
        self.messages = AsyncStreamingEndpoint(answers, error, stream_script)
        self.beta = _Beta(AsyncEndpoint(list(answers), error))
        self._client: object = object()


# ── litellm: a module, not a client ──────────────────────────────────────────────────────────────
#: A litellm answer as a plain dict, which is what a cache layer or a customer's middleware hands back.
LITELLM_ANSWER: dict[str, object] = {
    "id": "chatcmpl-litellm-1",
    "model": "anthropic/claude-sonnet-5",
    "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
}


@dataclass
class FakeLitellmUsage:
    """`ModelResponse.usage`: the same three counts as attributes rather than as keys."""

    prompt_tokens: int = 11
    completion_tokens: int = 3
    total_tokens: int = 14


@dataclass
class FakeLitellmResponse:
    """A litellm answer as an object, which is what `litellm.completion` returns by default."""

    model: str = "anthropic/claude-sonnet-5"
    id: str = "chatcmpl-litellm-2"
    usage: FakeLitellmUsage = field(default_factory=FakeLitellmUsage)
    choices: list[object] = field(default_factory=list)


def fake_litellm(*, answer: object, error: BaseException | None = None) -> ModuleType:
    """A stand-in for the litellm module: the two entry points and nothing else.

    A module object rather than a class, because that is what litellm is: `completion` is a module
    attribute, which is why attach mode patches the module and rebinds the aliases of it.
    """
    module = ModuleType("litellm")

    def completion(**kwargs: object) -> object:
        if error is not None:
            raise error
        assert "model" in kwargs
        return answer

    async def acompletion(**kwargs: object) -> object:
        if error is not None:
            raise error
        assert "model" in kwargs
        return answer

    module.completion = completion  # pyright: ignore[reportAttributeAccessIssue] - a module is a namespace
    module.acompletion = acompletion  # pyright: ignore[reportAttributeAccessIssue] - likewise
    return module


# ── google-genai: a client whose calls live on sub-objects ───────────────────────────────────────
@dataclass
class FakeGenaiUsage:
    prompt_token_count: int = 7
    candidates_token_count: int = 2
    total_token_count: int = 9


@dataclass
class FakeGenaiFunctionCall:
    name: str = "lookup"
    args: dict[str, str] = field(default_factory=dict)
    id: str | None = "fc-1"


@dataclass
class FakeGenaiPart:
    text: str | None = None
    function_call: FakeGenaiFunctionCall | None = None


@dataclass
class FakeGenaiContent:
    parts: list[FakeGenaiPart]


@dataclass
class FakeGenaiFinishReason:
    """A genai finish reason is an enum member, so the record reads its `name` rather than its repr."""

    name: str = "STOP"


@dataclass
class FakeGenaiCandidate:
    content: FakeGenaiContent
    finish_reason: FakeGenaiFinishReason = field(default_factory=FakeGenaiFinishReason)


@dataclass
class FakeGenaiResponse:
    """`GenerateContentResponse`: candidates, `usage_metadata`, and the model under `model_version`."""

    model_version: str = "gemini-2.5-pro"
    response_id: str = "genai-1"
    usage_metadata: FakeGenaiUsage = field(default_factory=FakeGenaiUsage)
    candidates: list[FakeGenaiCandidate] = field(
        default_factory=lambda: [FakeGenaiCandidate(content=FakeGenaiContent(parts=[FakeGenaiPart(text="ok")]))]
    )


class _GenaiModels:
    """`client.models`: the call, and the streaming one beside it."""

    def __init__(self, answer: FakeGenaiResponse, chunks: list[object]) -> None:
        self.answer = answer
        self.chunks = chunks
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> FakeGenaiResponse:
        self.calls.append(kwargs)
        return self.answer

    def generate_content_stream(self, **kwargs: object) -> Iterator[object]:
        self.calls.append(kwargs)
        return iter(self.chunks)


class _GenaiAsyncModels:
    """`client.aio.models`: the same two, as coroutines."""

    def __init__(self, answer: FakeGenaiResponse, chunks: list[object]) -> None:
        self.answer = answer
        self.chunks = chunks

    async def generate_content(self, **kwargs: object) -> FakeGenaiResponse:
        assert "model" in kwargs
        return self.answer

    async def generate_content_stream(self, **kwargs: object) -> AsyncIterator[object]:
        assert "model" in kwargs
        return _aiter(self.chunks)


async def _aiter(chunks: list[object]) -> AsyncIterator[object]:
    for chunk in chunks:
        yield chunk


class _GenaiAio:
    def __init__(self, models: _GenaiAsyncModels) -> None:
        self.models = models


class FakeGenaiClient:
    """`google.genai.Client` as `wrap` sees it: `models` and `aio.models`, built in the constructor."""

    def __init__(self, *, answer: FakeGenaiResponse | None = None, chunks: list[object] | None = None) -> None:
        settled = answer if answer is not None else FakeGenaiResponse()
        frames = chunks if chunks is not None else []
        self.models = _GenaiModels(settled, frames)
        self.aio = _GenaiAio(_GenaiAsyncModels(settled, frames))


class NotAProviderClient:
    """An object with no surface `wrap` recognises."""


class FakeChatAnthropic:
    """`langchain_anthropic.ChatAnthropic` as `wrap` sees it: not a provider client, but holding two.

    The real class is a pydantic model that builds `anthropic.Anthropic` and `anthropic.AsyncAnthropic`
    in its own constructor, keeps them on `_client` and `_async_client`, and calls `messages.create` on
    them from `_generate` / `_agenerate`. It carries no `messages` of its own, which is exactly why the
    chat model is the wrong object to instrument and the two inner clients are the right ones.
    """

    def __init__(self, *, script: list[object] | None = None, error: BaseException | None = None) -> None:
        self._client = FakeAnthropic(script=script, error=error)
        self._async_client = FakeAsyncAnthropic(script=script, error=error)
        self.bound: object | None = None

    def bind_tools(self, tools: object, **kwargs: object) -> FakeChatAnthropic:
        """Present because the subject calls it; it returns the same model, with the tools recorded."""
        self.bound = tools
        return self

    def invoke(self, messages: list[object], **kwargs: object) -> object:
        """What `_generate` does, reduced to the one line that matters: it calls the inner client."""
        return self._client.messages.create(model="claude-fake-1", messages=messages, **kwargs)

    async def ainvoke(self, messages: list[object], **kwargs: object) -> object:
        return await self._async_client.messages.create(model="claude-fake-1", messages=messages, **kwargs)


# ── the observed-capture fixture's provider (`test_observed_capture.py`) ─────────────────────────
#
# The smallest object `wrap` recognises as an Anthropic client, answering with a tool-use block
# shaped the way the fixture application's consumer reads it. Here rather than in the test because
# every field of a fake response exists to be read by `getattr` at runtime, which is the reason this
# module is excluded from the dead-code scan in the first place.


class Planner(Protocol):
    """What the fixture's workflow offers the test: one phase of one route plan."""

    def plan(self, depot: str, mode: str, requested_stops: int) -> object: ...


class FixtureBlock:
    """One tool-use content block, as a provider's own response object has it."""

    def __init__(self, name: str, payload: dict[str, JsonValue]) -> None:
        self.type = "tool_use"
        self.id = f"toolu_{name}"
        self.name = name
        self.input = payload


class FixtureUsage:
    def __init__(self) -> None:
        self.input_tokens = 11
        self.output_tokens = 7


class FixtureAnswer:
    """A provider answer with the shape the fixture's consumer reads."""

    def __init__(self, name: str, payload: dict[str, JsonValue]) -> None:
        self.id = "msg_fixture"
        self.model = "fixture-model"
        self.stop_reason = "tool_use"
        self.content = [FixtureBlock(name, payload)]
        self.usage = FixtureUsage()

    def model_dump(self, *, mode: str = "python") -> dict[str, JsonValue]:
        return {"id": self.id, "model": self.model, "stopReason": self.stop_reason, "mode": mode}


class FixtureMessages:
    def __init__(self, answer: FixtureAnswer) -> None:
        self._answer = answer
        self.seen: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> FixtureAnswer:
        self.seen.append(dict(kwargs))
        return self._answer


class FixtureClient:
    """The smallest object `wrap` recognises as an Anthropic client: `messages.create`."""

    def __init__(self, answer: FixtureAnswer) -> None:
        self.messages = FixtureMessages(answer)

"""Global installation must be reversible, and existing OTel must keep ownership."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import TYPE_CHECKING, cast

import httpx
import pytest

import hajer
from hajer import _http_capture
from tests.fakes import FakeOpenAI

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import Tracer
    from opentelemetry.util.types import AttributeValue


def api(name: str) -> Callable[..., object]:
    value: object = getattr(hajer, name, None)
    assert callable(value), f"hajer.{name} is required"
    return value


@pytest.fixture
def installed() -> Iterator[None]:
    yield
    uninstall = getattr(hajer, "uninstrument", None)
    if callable(uninstall):
        uninstall()


@pytest.mark.usefixtures("installed")
def test_global_install_is_idempotent_and_restores_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("openai")

    class Client(FakeOpenAI):
        pass

    module.__dict__["OpenAI"] = Client
    monkeypatch.setitem(sys.modules, "openai", module)
    original = Client.__init__
    first = api("instrument")()
    assert api("instrument")() is first
    client = Client()
    expected = FakeOpenAI().chat.completions.create()
    assert client.chat.completions.create() == expected
    assert len(hajer.wrapped_calls()) == 1
    api("uninstrument")()
    assert Client.__init__ is original
    hajer.clear_wrapped_calls()
    assert client.chat.completions.create() == expected
    assert hajer.wrapped_calls() == ()


def _instrumentor(module_name: str, class_name: str, *, active: bool | None) -> ModuleType:
    """An instrumentor module shaped like OpenTelemetry's `BaseInstrumentor`: a per-class singleton `_instance`,
    set when the instrumentor was built, whose `_is_instrumented_by_opentelemetry` says whether it is on."""
    module = ModuleType(module_name)
    instrumentor = type(class_name, (), {"_instance": None, "_is_instrumented_by_opentelemetry": False})
    if active is not None:
        instance = object.__new__(instrumentor)
        for owner, name, value in (
            (instance, "_is_instrumented_by_opentelemetry", active),
            (instrumentor, "_instance", instance),
        ):
            setattr(owner, name, value)
    module.__dict__[class_name] = instrumentor
    return module


#: A LangChain chat model as `wrap` recognises one without importing LangChain: a subclass of a class whose
#: module is `langchain_core`'s, holding its provider client.
_LangChainBase = type("BaseChatModel", (), {"__module__": "langchain_core.language_models.chat_models"})


class _ChatModel(_LangChainBase):
    def __init__(self) -> None:
        self.root_client = FakeOpenAI()


@pytest.mark.parametrize(
    ("module_name", "class_name", "active"),
    [
        ("langfuse.openai", "OpenAI", None),  # Langfuse's drop-in: langfuse.* attributes, no gen_ai
        ("opentelemetry.instrumentation.langchain", "LangchainInstrumentor", None),  # imported, never built
        ("opentelemetry.instrumentation.langchain", "LangchainInstrumentor", False),  # built, never instrumented
        ("openinference.instrumentation.langchain", "LangChainInstrumentor", True),  # llm.* attributes, not read
        ("opentelemetry.instrumentation.openai_v2", "OpenAIInstrumentor", True),  # no emitter verified here
    ],
)
@pytest.mark.usefixtures("installed")
def test_a_tracing_module_that_is_merely_present_never_stops_wrap(
    monkeypatch: pytest.MonkeyPatch, module_name: str, class_name: str, active: bool | None
) -> None:
    """Deferring to a module that was only imported captured 0 calls."""
    monkeypatch.setitem(sys.modules, module_name, _instrumentor(module_name, class_name, active=active))
    model = _ChatModel()
    hajer.wrap(model)
    assert getattr(model.root_client.chat.completions.create, "__hajer_instrumented__", False)
    model.root_client.chat.completions.create(model="m", messages=[])
    assert len(hajer.wrapped_calls()) == 1


_SCOPE = "opentelemetry.instrumentation.langchain"
_MODEL = "gpt-5.1"
_HTTP = hajer.HajerSettings(capture_http=True, disabled=True)

#: What Traceloop 0.60 puts on the spans it makes current around a runnable, a sequence step, a tool and a LangGraph
#: node (`callback_handler._create_task_span`, `patch.py`): `gen_ai` attributes, and no model call.
_FRAMEWORK_SPANS: dict[str, dict[str, str]] = {
    "runnable": {"gen_ai.provider.name": "langchain", "gen_ai.operation.name": "invoke_agent"},
    "sequence step": {"gen_ai.provider.name": "langchain", "gen_ai.operation.name": "execute_task"},
    "tool": {"gen_ai.provider.name": "langchain", "gen_ai.operation.name": "execute_tool"},
    "graph node": {"gen_ai.provider.name": "langgraph", "gen_ai.operation.name": "execute_task"},
    "graph": {"gen_ai.provider.name": "langgraph", "gen_ai.operation.name": "invoke_agent"},
}


def _emitting_to(provider: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Traceloop's instrumentor, active, emitting to `provider` — where 0.60 keeps it."""
    monkeypatch.setitem(sys.modules, _SCOPE, _instrumentor(_SCOPE, "LangchainInstrumentor", active=True))
    tracer = type("Tracer", (), {"_tracer_provider": provider})()
    handler = type("TraceloopCallbackHandler", (), {"tracer": tracer})()
    wrapper = type("_BaseCallbackManagerInitWrapper", (), {"_callback_handler": handler})()
    init = type("FunctionWrapper", (), {"_self_wrapper": wrapper})()
    callbacks = ModuleType("langchain_core.callbacks")
    callbacks.__dict__["BaseCallbackManager"] = type("BaseCallbackManager", (), {"__init__": init})
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", callbacks)


@pytest.fixture
def provider() -> Iterator[TracerProvider]:
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    made = cast("TracerProvider", sdk.TracerProvider())
    yield made
    made.shutdown()


def _tracer(provider: TracerProvider, scope: str = _SCOPE) -> Tracer:
    return provider.get_tracer(scope)


def _model_span(provider: TracerProvider, **attributes: object) -> None:
    """A model span, begun and ended with nothing inside it."""
    span = _tracer(provider).start_span("ChatOpenAI.chat", attributes=_chat(**attributes))
    span.end()


def _chat(**attributes: object) -> dict[str, AttributeValue]:
    given: dict[str, object] = {
        "gen_ai.provider.name": "openai",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": _MODEL,
    }
    given.update({key.replace("__", "."): value for key, value in attributes.items() if value is not None})
    return cast("dict[str, AttributeValue]", {key: value for key, value in given.items() if value != ""})


def _answer_messages(text: str) -> str:
    return json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": text}]}])


@pytest.mark.usefixtures("installed")
def test_an_active_instrumentor_never_stops_wrap(monkeypatch: pytest.MonkeyPatch, provider: TracerProvider) -> None:
    """Deferral is removed. Repeated attempts showed it cannot be made loss-free across
    emitters, so a traced model is wrapped like any other, with its frames."""
    _emitting_to(provider, monkeypatch)
    model = _ChatModel()
    hajer.wrap(model)
    assert getattr(model.root_client.chat.completions.create, "__hajer_instrumented__", False)
    model.root_client.chat.completions.create(model=_MODEL, messages=[])
    (call,) = hajer.wrapped_calls()
    assert call.caller_frames
    assert cast(hajer.Instrumentation, api("instrument")()).linkage == "FRAMES"


@pytest.mark.usefixtures("installed")
def test_an_explicit_receiver_keeps_wrapping_and_says_when_linkage_degrades(
    monkeypatch: pytest.MonkeyPatch, provider: TracerProvider
) -> None:
    """Hajer always captures through its own wrap; `tracer_provider=` adds the receiver for
    calls it cannot wrap. The receipt says FRAMES until a call is recorded from a span."""
    module = ModuleType("openai")

    class Client(FakeOpenAI):
        pass

    module.__dict__["OpenAI"] = Client
    monkeypatch.setitem(sys.modules, "openai", module)
    receipt = cast(hajer.Instrumentation, api("instrument")(tracer_provider=provider))
    assert (receipt.mode, receipt.connected, receipt.linkage) == ("wrap", True, "FRAMES")
    Client().chat.completions.create(model=_MODEL, messages=[])
    (wrapped,) = hajer.wrapped_calls()
    assert wrapped.caller_frames, "built after instrument(): wrapped, with its frames"
    _model_span(provider, gen_ai__response__id="bedrock-1", gen_ai__provider__name="aws.bedrock")
    assert [call.response_id for call in hajer.wrapped_calls()] == ["chatcmpl-1", "bedrock-1"]
    assert receipt.linkage.startswith("DEGRADED")


@pytest.mark.usefixtures("installed")
def test_the_receiver_takes_model_operations_only(provider: TracerProvider) -> None:
    """Every Traceloop chain, tool, agent and graph span became a phantom model call."""
    api("instrument")(tracer_provider=provider)
    for attributes in _FRAMEWORK_SPANS.values():
        with _tracer(provider).start_as_current_span("framework", attributes=attributes):
            pass
    operations = ("chat", "text_completion", "embeddings", "generate_content")
    for number, operation in enumerate(operations):
        _model_span(provider, gen_ai__operation__name=operation, gen_ai__response__id=f"model-{number}")
    assert [call.response_id for call in hajer.wrapped_calls()] == [f"model-{n}" for n in range(len(operations))]


_ANSWER = {
    "id": "chatcmpl-direct",
    "object": "chat.completion",
    "created": 0,
    "model": _MODEL,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_CHAT_URL = "http://models.invalid/v1/chat/completions"


@pytest.fixture
def transports(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """httpx's default transport, answering a chat completion in memory: what `HAJER_CAPTURE_HTTP` patches."""
    mock = httpx.MockTransport(lambda _request: httpx.Response(200, json=_ANSWER))

    def handle(_self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return mock.handle_request(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    yield
    _http_capture.uninstall_http_capture()


def _post_chat() -> None:
    with httpx.Client() as http:
        http.post(_CHAT_URL, json={"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]})


@pytest.mark.parametrize("inside", ["no span", *_FRAMEWORK_SPANS])
@pytest.mark.parametrize("receiver", [False, True])
@pytest.mark.usefixtures("installed", "transports")
def test_a_direct_request_is_captured_inside_any_framework_span(
    monkeypatch: pytest.MonkeyPatch, provider: TracerProvider, inside: str, *, receiver: bool
) -> None:
    """With a traced LangChain model wrapped, a direct model request made inside a runnable,
    a sequence step, a tool or a LangGraph node — spans Traceloop makes current, with `gen_ai.operation.name` —
    was left to a span that never came. Now it is always captured, and the framework span is no call."""
    _emitting_to(provider, monkeypatch)
    api("instrument")(settings=_HTTP, **({"tracer_provider": provider} if receiver else {}))
    model = _ChatModel()
    hajer.wrap(model, settings=_HTTP)
    assert getattr(model.root_client.chat.completions.create, "__hajer_instrumented__", False), "never deferred"
    if inside == "no span":
        _post_chat()
    else:
        with _tracer(provider).start_as_current_span(inside, attributes=_FRAMEWORK_SPANS[inside]):
            _post_chat()
    (call,) = hajer.wrapped_calls()
    assert (call.provider, call.response_id, bool(call.caller_frames)) == ("openai", "chatcmpl-direct", True)


def _traced_call(provider: TracerProvider, client: FakeOpenAI, **attributes: object) -> None:
    """A framework instrumentor's model span, as Traceloop makes it: begun before the provider request, never current,
    ended after the answer."""
    span = _tracer(provider).start_span("ChatOpenAI.chat", attributes=_chat())
    client.chat.completions.create(model=_MODEL, messages=[])
    for key, value in _chat(**attributes).items():
        span.set_attribute(key, value)
    span.end()


@pytest.mark.parametrize(
    "span",
    [
        {"gen_ai__response__id": "chatcmpl-1"},  # a call's span with its response id
        {"gen_ai__output__messages": _answer_messages("ok")},  # a streamed call's span: no id, the same answer
        {"gen_ai__usage__input_tokens": 11, "gen_ai__usage__output_tokens": 7},  # no id, the same usage
    ],
)
@pytest.mark.usefixtures("installed")
def test_a_wrapped_call_and_its_span_are_one_record(provider: TracerProvider, span: dict[str, object]) -> None:
    api("instrument")(tracer_provider=provider)
    _traced_call(provider, hajer.wrap(FakeOpenAI()), **span)
    (call,) = hajer.wrapped_calls()
    assert call.caller_frames, "the wrap's record wins"


@pytest.mark.usefixtures("installed")
def test_a_span_begun_inside_a_wrapped_call_is_that_call(provider: TracerProvider) -> None:
    """A client-level instrumentor's span begins inside Hajer's (outer, instance-level) patch and ends before Hajer's
    record settles, so no response id is known yet when it ends."""
    api("instrument")(tracer_provider=provider)

    def create(**_: object) -> object:
        _model_span(provider, gen_ai__response__id="chatcmpl-1")
        return FakeOpenAI().chat.completions.create()

    client = FakeOpenAI()
    client.chat.completions.create = create
    hajer.wrap(client)
    client.chat.completions.create(model=_MODEL, messages=[])
    (call,) = hajer.wrapped_calls()
    assert call.caller_frames


@pytest.mark.parametrize(
    ("span", "flagged"),
    [
        ({"gen_ai__usage__input_tokens": 50, "gen_ai__usage__output_tokens": 9}, False),  # another call's usage
        ({"gen_ai__output__messages": _answer_messages("something else")}, False),  # another call's answer
        ({}, True),  # nothing to tell them apart
        ({"gen_ai__request__model": ""}, True),  # nor a model
    ],
)
@pytest.mark.usefixtures("installed")
def test_a_distinct_call_inside_a_span_is_never_dropped(
    provider: TracerProvider, span: dict[str, object], *, flagged: bool
) -> None:
    """A call recorded inside the span of a call Hajer did not record — the loop body of a streamed
    call — claimed that span, and the streamed call was lost without a word."""
    api("instrument")(tracer_provider=provider)
    tracer = _tracer(provider)
    traced = tracer.start_span("ChatOpenAI.chat", attributes=_chat(**span))  # its own call is not Hajer's
    hajer.wrap(FakeOpenAI()).chat.completions.create(model=_MODEL, messages=[])  # the loop body's call
    traced.end()
    direct, from_span = hajer.wrapped_calls()
    assert direct.caller_frames
    assert not from_span.caller_frames
    assert any(note.startswith("SPAN_MAY_DUPLICATE") for note in from_span.limitations) is flagged


@pytest.mark.usefixtures("installed")
def test_instrument_then_wrap_records_a_traced_call_once(
    monkeypatch: pytest.MonkeyPatch, provider: TracerProvider
) -> None:
    """The root client was instrumented at construction, and its span was received as well."""
    _emitting_to(provider, monkeypatch)
    module = ModuleType("openai")

    class Client(FakeOpenAI):
        pass

    module.__dict__["OpenAI"] = Client
    monkeypatch.setitem(sys.modules, "openai", module)
    api("instrument")(tracer_provider=provider)
    model = _ChatModel()
    model.root_client = Client()
    hajer.wrap(model)
    _traced_call(provider, model.root_client, gen_ai__response__id="chatcmpl-1")
    assert len(hajer.wrapped_calls()) == 1


@pytest.mark.usefixtures("installed")
def test_uninstrument_does_not_undo_a_later_vendor_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("openai")

    class Client(FakeOpenAI):
        pass

    module.__dict__["OpenAI"] = Client
    monkeypatch.setitem(sys.modules, "openai", module)
    api("instrument")()

    def later(self: object) -> None:
        del self

    initializer = "__init__"
    setattr(Client, initializer, later)
    api("uninstrument")()
    assert cast(object, Client.__init__) is later


@pytest.mark.parametrize("vendor", ["sentry_sdk", "ddtrace", "langfuse", "logfire", "openinference", "opentelemetry"])
@pytest.mark.usefixtures("installed")
def test_a_loaded_vendor_package_that_instruments_no_model_call_does_not_disable_wrap(
    monkeypatch: pytest.MonkeyPatch, vendor: str
) -> None:
    """An application that imports `sentry_sdk` for error reporting and never initialises it locally emits no
    `gen_ai` span, so yielding to it would capture nothing at all."""
    monkeypatch.setitem(sys.modules, vendor, ModuleType(vendor))
    client = FakeOpenAI()
    hajer.wrap(client)
    assert getattr(client.chat.completions.create, "__hajer_instrumented__", False)
    client.chat.completions.create(model="m", messages=[])
    assert len(hajer.wrapped_calls()) == 1

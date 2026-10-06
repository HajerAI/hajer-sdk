"""`hajer.workflow` / `component` / `tool` — the spans an application declares about itself — and the model spans.

A model call is recorded by `wrap`; this module is where it becomes a span. The call observer installed in `_wrap`
(`install`) is told when a call opens — inside the caller's own context, which is the span the model span belongs
under — and when it settles, which is when the span is emitted, backdated to the call's start (`_model_spans`).
What this module also records is the *shape around* the calls: which workflow a request is, which component of it
is running, which tool it executed and with what. Those three spans are what an obligation is written against and
what an eval's trajectory assertions read, so their names and attribute keys are fixed here and nowhere else:
`hajer.workflow.id`, `hajer.component.id`, `hajer.tool.id`, and on a tool span the GenAI semantic-convention pair
`gen_ai.tool.name` / `gen_ai.tool.call.arguments` that an eval engine's trajectory checks look for by exactly those
keys.

The ids are a stack in a `contextvars.ContextVar`, so a component inside a workflow carries the workflow's id and a
tool inside both carries both — across `await`, and into a thread that copied the context the way `asyncio.to_thread`
does. A workflow span also enters `hajer.scope(workflow=…)`, so the model calls `wrap` records inside it group
under the same identity the span carries; one declaration, two records that agree.

**Where the spans go** is decided here, OTel-free, by `export_target`: a collector the process named
(`HAJER_OTLP_ENDPOINT`), else the platform's receiver for the team when there is a key, else nowhere. The OTel half
(`hajer._telemetry_otel`) adds an exporter for that target to the provider in use — the application's own, so it
keeps its exporters, or an isolated one the SDK builds and never installs globally.

**Nothing here imports OpenTelemetry at module import.** The OTel half is loaded through `importlib` on first use;
without `hajer[otel]` every span still runs the code it wraps, still keeps the id stack, still enters the scope,
emits nothing, and says so once on the `hajer` logger. **Nothing here raises into the
application**: a provider that cannot start a span, an exporter that cannot flush, a document that cannot be
encoded — each degrades to "no span" or "no attribute", because an instrumentation path that raised would turn a
declaration about a workflow into the reason the workflow failed.

An exception the wrapped code raises propagates unchanged; the span records its *type* and an error status, never
its message or stack trace — a message is content and a stack trace is a list of absolute paths, and the SDK sends
neither without the consent each has its own switch for.
"""

from __future__ import annotations

import atexit
import contextvars
import functools
import importlib
import inspect
import json
import logging
import threading
from collections.abc import Awaitable, Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from typing import Final, Literal, ParamSpec, Protocol, TypeAlias, TypeVar, cast

from hajer import _context, _semconv
from hajer._errors import HajerConfigError
from hajer._json import JsonObject
from hajer._model_spans import Attributes, AttributeValue, ModelSpan, model_span
from hajer._paths import OTEL_TRACES_PATH
from hajer._redact import ClientRedactionPolicy, build_policy, redact_document
from hajer._settings import HajerSettings
from hajer._wrap import Operation, WrappedCall, fitted, scope, set_call_observer

P = ParamSpec("P")
R = TypeVar("R")

#: The three kinds of span this module emits, and the word each span's name begins with.
Kind: TypeAlias = Literal["workflow", "component", "tool"]

#: The operation name a tool span carries, spelled as the GenAI semantic conventions spell it: it is the value an
#: eval engine's trajectory assertions select tool spans by.
EXECUTE_TOOL: Final[str] = _semconv.OPERATION_EXECUTE_TOOL
#: What the `hajer` logger says, once per configuration, when the OTel half cannot be imported.
OTEL_MISSING: Final[str] = (
    "hajer emits no spans: the OpenTelemetry SDK is not installed. "
    "Install the `otel` extra (`pip install 'hajer[otel]'`) to export them."
)
#: What it says instead when the OTel half imported but building the exporter raised: a broken provider or exporter
#: is not a missing package, and telling its owner to install what they have sends them the wrong way. The class name
#: only — an exception's message can carry an endpoint's credentials.
OTEL_BUILD_FAILED: Final[str] = "hajer emits no spans: its OpenTelemetry exporter could not be built ({error_type})."
#: OTLP/HTTP's traces path, appended to a collector endpoint (an endpoint, not a traces URL — the same
#: convention `OTEL_EXPORTER_OTLP_ENDPOINT` follows). The platform's route carries it already.
OTLP_TRACES_SUFFIX: Final[str] = "/v1/traces"
#: What the `hajer` logger says when `HajerSettings.from_env()` refuses the environment: the emitter runs on defaults.
SETTINGS_INVALID: Final[str] = (
    "hajer telemetry is running on default settings because the environment could not be read: {error}"
)
_LOG: Final = logging.getLogger("hajer")
_NO_ERROR: Final[tuple[None, None, None]] = (None, None, None)
#: The catalog's default policy, built once: tool arguments and model content are redacted under the same rules as
#: every other document the SDK sends, and `build_policy()` with no extras cannot refuse.
_DEFAULT_POLICY: Final = build_policy()

__all__ = ["AttributeValue", "Attributes"]


# ── where the spans go ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExportTarget:
    """One place spans are exported to: the traces URL, and the headers every request carries."""

    url: str
    headers: dict[str, str]
    #: True when the target is the platform and the key rides along; False for a collector of the process's own.
    authenticated: bool


def export_target(settings: HajerSettings) -> ExportTarget | None:
    """Where this process's spans leave for, or `None` when they leave for nowhere.

    A collector the process named wins: `HAJER_OTLP_ENDPOINT` (or the standard variable) is an operator saying
    where their spans go, and `hajer eval` relies on it to reach the engine's receiver without a key. Otherwise the
    platform, for the team the key names, when there is a key, the process is not disabled and
    `HAJER_TRACES_ENABLED` is on. Headers from `HAJER_OTLP_HEADERS` ride on either.
    """
    extra = settings.otlp_header_values
    if settings.otlp_endpoint is not None:
        return ExportTarget(settings.otlp_endpoint.rstrip("/") + OTLP_TRACES_SUFFIX, extra, authenticated=False)
    if settings.inert or not settings.traces_enabled or settings.api_key is None or settings.team_id is None:
        return None
    url = settings.base_url.rstrip("/") + OTEL_TRACES_PATH.format(team_id=settings.team_id)
    return ExportTarget(url, {**extra, "Authorization": f"Bearer {settings.api_key}"}, authenticated=True)


# ── the two seams the OTel half fills ───────────────────────────────────────────────────────────


class SpanHandle(Protocol):
    """One open span as this module holds it: something that can be ended, with or without an error."""

    def end(self, error: BaseException | None) -> None: ...


class Backend(Protocol):
    """What `hajer._telemetry_otel.build` returns. Structural, so this module never names an OTel type."""

    def start(self, name: str, attributes: Attributes, *, traceparent: str | None) -> SpanHandle: ...
    def opened(self, call: WrappedCall, stamp: Attributes, *, traceparent: str | None) -> object: ...
    def settled(self, handle: object, span: ModelSpan) -> None: ...
    def flush(self, timeout_ms: int) -> bool: ...
    def shutdown(self) -> None: ...


# ── process-wide configuration ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Configuration:
    """The process-wide state, in a holder rather than rebound module globals.

    `settings` and `tracer_provider` are what `configure()` was handed; `backend` is built from them on first use and
    dropped by the next `configure()`, which is how a test swaps providers and how the no-op fallback is reached again
    after the OTel half was made unimportable. `warned` is per configuration too: one warning per process would hide
    the second test's claim, one per span would flood a log.
    """

    settings: HajerSettings | None = None
    tracer_provider: object | None = None
    policy: ClientRedactionPolicy | None = None
    backend: Backend | None = None
    backend_loaded: bool = False
    warned: bool = False
    exit_hook_registered: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_CONFIGURATION = _Configuration()


def configure(
    settings: HajerSettings | None = None,
    *,
    tracer_provider: object | None = None,
    policy: ClientRedactionPolicy | None = None,
) -> None:
    """Choose the settings, the tracer provider and the redaction policy every later span uses.

    `None` means read the environment lazily, use the provider ownership decides, redact under the catalog's default
    policy. The explicit override and the test seam in one: an application with its own OpenTelemetry setup passes
    the provider it wants the spans on (the platform's exporter is added to it, beside the application's own), a
    team with a redaction policy of its own (`hajer.build_policy(...)`) passes it, a test passes an isolated SDK
    provider with an in-memory exporter, and `configure()` with nothing resets to the defaults. Takes effect for the
    next span; an open span is unaffected. The previous backend is shut down, so a provider the SDK built for the
    previous configuration stops exporting.
    """
    with _CONFIGURATION.lock:
        previous = _CONFIGURATION.backend
        _CONFIGURATION.settings = settings
        _CONFIGURATION.tracer_provider = tracer_provider
        _CONFIGURATION.policy = policy
        _CONFIGURATION.backend = None
        _CONFIGURATION.backend_loaded = False
        _CONFIGURATION.warned = False
    if previous is not None:
        try:
            previous.shutdown()
        except Exception:  # noqa: BLE001, S110 - a backend that cannot stop is not the caller's problem
            pass


def _settings() -> HajerSettings:
    """The settings in force, read from the environment once when `configure()` was given none.

    A `HAJER_*` variable the settings refuse is not this module's to raise about: `Hajer()` raises it where the
    developer wrote the client. Here it is logged and the emitter runs on defaults, because a decorator on a
    request handler is not a place a configuration error may surface as a failed request.
    """
    with _CONFIGURATION.lock:
        if _CONFIGURATION.settings is None:
            try:
                _CONFIGURATION.settings = HajerSettings.from_env()
            except HajerConfigError as error:
                _LOG.warning(SETTINGS_INVALID.format(error=error))
                _CONFIGURATION.settings = HajerSettings()
        return _CONFIGURATION.settings


def _policy() -> ClientRedactionPolicy:
    with _CONFIGURATION.lock:
        return _CONFIGURATION.policy if _CONFIGURATION.policy is not None else _DEFAULT_POLICY


def _backend() -> Backend | None:
    """The OTel half, imported on first use, or `None` — once, with one warning — when it cannot be.

    The warning says which: `OTEL_MISSING` when the package is not importable, `OTEL_BUILD_FAILED` when it is and
    building the exporter raised.

    `importlib.import_module` rather than an `import` statement, so that a process without `hajer[otel]` (or a test
    that puts `None` in `sys.modules` for the module) reaches the no-op path through the same code it would reach the
    real one, and so that the import happens here, inside the try, at the first span rather than at `import hajer`.
    """
    with _CONFIGURATION.lock:
        if _CONFIGURATION.backend_loaded:
            return _CONFIGURATION.backend
        _CONFIGURATION.backend_loaded = True
        settings = _CONFIGURATION.settings
    if settings is None:
        settings = _settings()
    backend: Backend | None = None
    failure = OTEL_MISSING
    try:
        module = importlib.import_module("hajer._telemetry_otel")
        build = cast(Callable[[HajerSettings, object | None], Backend], module.build)
        backend = build(settings, _CONFIGURATION.tracer_provider)
    except ImportError:
        pass
    except Exception as error:  # noqa: BLE001 - a broken provider or exporter may never break the application
        failure = OTEL_BUILD_FAILED.format(error_type=f"{type(error).__module__}.{type(error).__qualname__}")
    with _CONFIGURATION.lock:
        _CONFIGURATION.backend = backend
        if backend is None and not _CONFIGURATION.warned:
            _CONFIGURATION.warned = True
            _LOG.warning(failure)
        if backend is not None and not _CONFIGURATION.exit_hook_registered:
            _CONFIGURATION.exit_hook_registered = True
            atexit.register(_flush_at_exit)
    return backend


def flush(timeout_ms: int | None = None) -> bool:
    """Wait for the spans emitted so far to leave the process. True when the provider says they did.

    An eval provider calls this before it answers, so the trace is at the receiver when the row is graded; a
    short-lived process calls it before exit. `timeout_ms` defaults to `HAJER_TRACE_FLUSH_TIMEOUT_MS`. False when
    there is no provider to ask, when it says no, and when it raises — a flush that failed is reported, not raised.
    """
    backend = _backend()
    if backend is None:
        return False
    try:
        return backend.flush(timeout_ms if timeout_ms is not None else _settings().trace_flush_timeout_ms)
    except Exception:  # noqa: BLE001 - an exporter that cannot flush may never break the application
        return False


def _flush_at_exit() -> None:
    """The `atexit` hook: whatever is still queued gets its one chance to leave, then the SDK's own exporter stops.

    Never raises, at exit least of all. The shutdown is what ends the batch processor's thread for a provider the
    SDK built; an application's own provider is left to its own shutdown. Only the backend already loaded: after a
    `configure()` there may be none, and building one at interpreter exit has nothing queued to send and, with
    half the modules torn down, every chance of failing and logging about it.
    """
    try:
        with _CONFIGURATION.lock:
            backend = _CONFIGURATION.backend
        if backend is None:
            return
        try:
            backend.flush(_settings().trace_flush_timeout_ms)
        finally:
            backend.shutdown()
    except Exception:  # noqa: BLE001, S110 - nothing may raise out of an atexit hook
        pass


# ── the id stack and the eval binding ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Frame:
    """The ids in force where a span is opened: what a component or tool span copies onto itself."""

    workflow: str | None = None
    component: str | None = None


@dataclass(frozen=True, slots=True)
class _Binding:
    """One eval row's identity, bound around the provider call that produces it (`eval_binding`)."""

    run_id: str | None
    test_case_id: str | None
    workflow_id: str | None
    obligation_ids: tuple[str, ...]
    traceparent: str | None


#: Immutable values, so a child context that sets its own frame never writes into its parent's.
_ROOT_FRAME: Final[_Frame] = _Frame()
_FRAME: contextvars.ContextVar[_Frame] = contextvars.ContextVar("hajer_telemetry_frame", default=_ROOT_FRAME)
_BINDING: contextvars.ContextVar[_Binding | None] = contextvars.ContextVar("hajer_eval_binding", default=None)
#: The spans entered with `with` in this context, innermost last. A `with` block exits in the context it was entered
#: in, so the top of this stack is always the block that is closing — which a list on the `Span` object could not
#: promise once two tasks or two threads entered the same object.
_ENTERED: contextvars.ContextVar[tuple[_Entry, ...]] = contextvars.ContextVar("hajer_telemetry_entered", default=())


@contextmanager
def eval_binding(
    *,
    run_id: str | None,
    test_case_id: str | None,
    workflow_id: str | None,
    obligation_ids: tuple[str, ...],
    traceparent: str | None,
) -> Generator[None]:
    """Mark every span opened inside the block as belonging to one eval row.

    The spans get `hajer.eval.run.id`, `hajer.eval.test_case.id`, `hajer.eval.workflow.id` and
    `hajer.eval.obligation.ids` — whichever are given — and a span opened with no span already active becomes a
    child of `traceparent`, the W3C header the engine handed the provider, so the trace the engine grades is the
    one the application emitted. `run_id=None` leaves the run id to `HAJER_EVAL_RUN_ID`, which `hajer eval` sets.
    """
    token = _BINDING.set(
        _Binding(
            run_id=run_id,
            test_case_id=test_case_id,
            workflow_id=workflow_id,
            obligation_ids=obligation_ids,
            traceparent=traceparent,
        )
    )
    try:
        yield
    finally:
        _BINDING.reset(token)


def context_attributes(settings: HajerSettings, frame: _Frame, binding: _Binding | None) -> Attributes:
    """What every span opened here carries from its surroundings: the conversation, the enclosing ids, the
    environment, the eval row.

    The same stamp goes on a declared span and on a model span, so a generation nested in a workflow says which
    workflow, which session — and, under `hajer eval`, which row — exactly as the workflow span does.
    """
    attributes: Attributes = dict(_context.attributes(_context.current()))
    if frame.workflow is not None:
        attributes[_semconv.WORKFLOW_ID] = frame.workflow
    if frame.component is not None:
        attributes[_semconv.COMPONENT_ID] = frame.component
    if settings.environment is not None:
        attributes[_semconv.DEPLOYMENT_ENVIRONMENT] = settings.environment
        attributes[_semconv.DEPLOYMENT_ENVIRONMENT_NAME] = settings.environment
    run_id = settings.eval_run_id if binding is None or binding.run_id is None else binding.run_id
    if run_id is not None:
        attributes[_semconv.EVAL_RUN_ID] = run_id
    if binding is not None:
        if binding.test_case_id is not None:
            attributes[_semconv.EVAL_TEST_CASE_ID] = binding.test_case_id
        if binding.workflow_id is not None:
            attributes[_semconv.EVAL_WORKFLOW_ID] = binding.workflow_id
        if binding.obligation_ids:
            attributes[_semconv.EVAL_OBLIGATION_IDS] = binding.obligation_ids
    return attributes


def surrounding_attributes() -> Attributes:
    """The stamp for a span something else is starting here: what `HajerContextProcessor` puts on it.

    The same facts a span of the SDK's own gets at creation, read from the same places, so a third-party
    instrumentation's span inside a workflow and a session says both exactly as the SDK's own would.
    """
    return context_attributes(_settings(), _FRAME.get(), _BINDING.get())


# ── the model spans: what the call observer does ───────────────────────────────────────────────


class _CallObserver:
    """The one observer `_wrap` hands every recorded call to: a span at settle, parented where the call opened.

    `opened` runs inside the caller's context and asks the backend for a handle that remembers it; `settled` may run
    anywhere — a stream is consumed wherever the application consumes it — and emits the span from the record and
    that handle. Both are already wrapped in `try` at the seam, and both guard again here: nothing in the emission
    path may cost the call.
    """

    def opened(self, call: WrappedCall) -> object | None:
        settings = _settings()
        if not settings.model_spans:
            return None
        backend = _backend()
        if backend is None:
            return None
        binding = _BINDING.get()
        stamp = context_attributes(settings, _FRAME.get(), binding)
        try:
            return backend.opened(call, stamp, traceparent=None if binding is None else binding.traceparent)
        except Exception:  # noqa: BLE001 - a backend that cannot open costs the span, never the call
            return None

    def settled(self, call: WrappedCall, handle: object | None) -> None:
        if handle is None:
            return
        backend = _backend()
        if backend is None:
            return
        try:
            backend.settled(handle, model_span(call, _settings(), _policy()))
        except Exception:  # noqa: BLE001, S110 - a span that cannot be emitted costs the span, never the call
            pass


_OBSERVER = _CallObserver()


def install() -> None:
    """Make this module the observer of every call `_wrap` records. `import hajer` does it once."""
    set_call_observer(_OBSERVER)


# ── the span ────────────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Entry:
    """Everything one opened span holds, so that closing it is one call whatever happens in between."""

    token: contextvars.Token[_Frame]
    operation: AbstractContextManager[Operation] | None
    handle: SpanHandle | None

    def close(self, error: BaseException | None) -> None:
        """End the span, leave the scope, pop the frame. Each step is guarded: closing may never raise."""
        if self.handle is not None:
            try:
                self.handle.end(error)
            except Exception:  # noqa: BLE001, S110 - an exporter bug never escapes into the span owner's code
                pass
        if self.operation is not None:
            self.operation.__exit__(*_NO_ERROR)
        try:
            _FRAME.reset(self.token)
        except ValueError:  # pragma: no cover - a token from another context; the frame is that context's
            pass


class Span:
    """One declared span, usable as a decorator (sync or async), as `with`, and as `async with`.

    One object, three spellings, because the point where a workflow begins is sometimes a function and sometimes a
    block. The decorator keeps the wrapped function's signature and return type; an `async def` is awaited inside
    the span rather than returning a coroutine that outlives it.
    """

    __slots__ = ("_arguments", "_identity", "_kind", "_name")

    def __init__(self, kind: Kind, identity: str, *, name: str | None = None, arguments: JsonObject | None = None):
        self._kind: Kind = kind
        self._identity = identity
        self._name = name
        self._arguments = arguments

    def __call__(self, function: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(function):
            awaitable = cast(Callable[P, Awaitable[object]], function)

            @functools.wraps(function)
            async def awaited(*args: P.args, **kwargs: P.kwargs) -> object:
                entry = self._open()
                try:
                    result = await awaitable(*args, **kwargs)
                except BaseException as error:
                    entry.close(error)
                    raise
                entry.close(None)
                return result

            return cast(Callable[P, R], awaited)

        @functools.wraps(function)
        def called(*args: P.args, **kwargs: P.kwargs) -> R:
            entry = self._open()
            try:
                result = function(*args, **kwargs)
            except BaseException as error:
                entry.close(error)
                raise
            entry.close(None)
            return result

        return called

    def __enter__(self) -> Span:
        _ENTERED.set((*_ENTERED.get(), self._open()))
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        del exc_type, tb
        entered = _ENTERED.get()
        if not entered:  # pragma: no cover - an exit with no entry; nothing to close
            return
        _ENTERED.set(entered[:-1])
        entered[-1].close(exc)

    async def __aenter__(self) -> Span:
        return self.__enter__()

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        self.__exit__(exc_type, exc, tb)

    def _open(self) -> _Entry:
        """Begin: compute the attributes from the enclosing ids, push this span's own, enter the scope, start the span.

        The attributes are read from the frame *before* it is pushed, because what a component copies is the
        workflow around it, not itself. The backend is the one step that may fail and is the one step guarded here;
        the frame and the scope are this module's own and always happen, so the no-op path and the real one keep the
        same ids and the same `wrapped_calls()` grouping.
        """
        settings = _settings()
        enclosing = _FRAME.get()
        binding = _BINDING.get()
        attributes = self._attributes(settings, enclosing, binding)
        token = _FRAME.set(self._pushed(enclosing))
        operation: AbstractContextManager[Operation] | None = None
        if self._kind == "workflow":
            operation = scope(workflow=self._identity)
            operation.__enter__()
        handle: SpanHandle | None = None
        backend = _backend()
        if backend is not None:
            try:
                handle = backend.start(
                    self._span_name(), attributes, traceparent=None if binding is None else binding.traceparent
                )
            except Exception:  # noqa: BLE001 - a provider that cannot start a span costs the span, never the code
                handle = None
        return _Entry(token=token, operation=operation, handle=handle)

    def _pushed(self, enclosing: _Frame) -> _Frame:
        """The frame inside this span: its own id in its slot, the rest inherited. A tool encloses nothing new."""
        if self._kind == "workflow":
            return replace(enclosing, workflow=self._identity)
        if self._kind == "component":
            return replace(enclosing, component=self._identity)
        return enclosing

    def _span_name(self) -> str:
        if self._kind == "tool":
            return f"{EXECUTE_TOOL} {self._name or self._identity}"
        return f"{self._kind} {self._identity}"

    def _attributes(self, settings: HajerSettings, enclosing: _Frame, binding: _Binding | None) -> Attributes:
        """The enclosing stamp, then this span's own id in its slot — a workflow names itself, never an enclosing one."""
        attributes = context_attributes(settings, enclosing, binding)
        if self._kind == "workflow":
            attributes.pop(_semconv.COMPONENT_ID, None)
            attributes[_semconv.WORKFLOW_ID] = self._identity
        elif self._kind == "component":
            attributes[_semconv.COMPONENT_ID] = self._identity
        else:
            attributes[_semconv.TOOL_ID] = self._identity
            attributes[_semconv.TOOL_NAME] = self._name or self._identity
            attributes[_semconv.OPERATION_NAME] = EXECUTE_TOOL
            encoded = _tool_arguments(self._arguments, settings)
            if encoded is not None:
                attributes[_semconv.TOOL_CALL_ARGUMENTS] = encoded
        return attributes


def _tool_arguments(arguments: JsonObject | None, settings: HajerSettings) -> str | None:
    """The tool's arguments as the JSON string the span carries, or None when they may not or cannot be carried.

    Content, so `HAJER_CAPTURE_CONTENT` governs it; redacted under the catalog when `HAJER_REDACT_CLIENT` is on,
    like every other document the SDK sends; clipped to `HAJER_WRAPPED_CALL_MAX_BYTES` the way a wrapped call's
    content is (`fitted`: long strings lose their middle around their length and digest, short ones are untouched).
    None rather than a partial document when the shape alone does not fit, and None rather than an exception when
    the document cannot be encoded — the arguments are the application's and may be anything.
    """
    if arguments is None or not settings.capture_content:
        return None
    try:
        document, _entries = redact_document(arguments, policy=_policy()) if settings.redact_client else (arguments, ())
        result = fitted(document, settings.wrapped_call_max_bytes)
        if result is None:
            return None
        return json.dumps(result[0], ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001 - an argument document that cannot be read costs the attribute, never the tool
        return None


# ── the public three ────────────────────────────────────────────────────────────────────────────


def workflow(workflow_id: str) -> Span:
    """Declare one workflow: the unit an obligation is about, by a stable, non-secret id.

        @hajer.workflow("answer-support-question")
        def answer(ticket): ...

        with hajer.workflow("answer-support-question"):
            ...

    The span carries `hajer.workflow.id`, every component and tool span inside it copies that id, and the model
    calls `wrap` records inside it carry the same id as their workflow hint (`hajer.scope(workflow=…)` is entered
    for you). Repeated ids are repeated runs of one workflow, each with its own span.
    """
    return Span("workflow", workflow_id)


def component(component_id: str) -> Span:
    """Declare one component of a workflow — a retriever, a planner, a drafting step — by a stable id.

    The span carries `hajer.component.id` and the enclosing workflow's `hajer.workflow.id`; a tool span inside it
    copies both. A component outside any workflow is allowed and carries only its own id.
    """
    return Span("component", component_id)


def tool(tool_id: str, *, name: str | None = None, arguments: JsonObject | None = None) -> Span:
    """Declare one tool execution, in the shape an eval's trajectory assertions read.

    The span is named `execute_tool <name>` and carries `gen_ai.operation.name = "execute_tool"`, `gen_ai.tool.name`
    (`name`, or the id when none is given) and `hajer.tool.id`, plus the enclosing workflow and component ids. With
    `arguments` and `HAJER_CAPTURE_CONTENT` on it also carries `gen_ai.tool.call.arguments`: the arguments as JSON,
    redacted and bounded like every other document the SDK sends. The SDK never executes the tool: it records that
    the application did.
    """
    return Span("tool", tool_id, name=name, arguments=arguments)

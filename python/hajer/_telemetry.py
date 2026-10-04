"""`hajer.workflow` / `component` / `tool` — the spans an application declares about itself, never a reason it fails.

A model call is recorded by `wrap`; what this module records is the *shape around* the calls: which workflow a
request is, which component of it is running, which tool it executed and with what. Those three spans are what an
obligation is written against and what an eval's trajectory assertions read, so their names and attribute keys are
fixed here and nowhere else: `hajer.workflow.id`, `hajer.component.id`, `hajer.tool.id`, and on a tool span the
GenAI semantic-convention pair `gen_ai.tool.name` / `gen_ai.tool.call.arguments` that an eval engine's trajectory
checks look for by exactly those keys.

The ids are a stack in a `contextvars.ContextVar`, so a component inside a workflow carries the workflow's id and a
tool inside both carries both — across `await`, and into a thread that copied the context the way `asyncio.to_thread`
does. A workflow span also enters `hajer.scope(workflow=…)`, so the model calls `wrap` records inside it group
under the same identity the span carries; one declaration, two records that agree.

**Nothing here imports OpenTelemetry at module import.** The OTel half is `hajer._telemetry_otel`, loaded through
`importlib` on first use; without `hajer[otel]` every span still runs the code it wraps, still keeps the id stack,
still enters the scope, emits nothing, and says so once on the `hajer` logger. **Nothing here raises into the
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

from hajer._errors import HajerConfigError
from hajer._json import JsonObject
from hajer._redact import build_policy, redact_document
from hajer._settings import HajerSettings
from hajer._wrap import Operation, fitted, scope

P = ParamSpec("P")
R = TypeVar("R")

#: What one span attribute may hold: the OpenTelemetry scalar types and a sequence of strings (the obligation ids).
AttributeValue: TypeAlias = str | bool | int | float | tuple[str, ...]
Attributes: TypeAlias = dict[str, AttributeValue]
#: The three kinds of span this module emits, and the word each span's name begins with.
Kind: TypeAlias = Literal["workflow", "component", "tool"]

#: The operation name a tool span carries, spelled as the GenAI semantic conventions spell it: it is the value an
#: eval engine's trajectory assertions select tool spans by.
EXECUTE_TOOL: Final[str] = "execute_tool"
#: What the `hajer` logger says, once per configuration, when the OTel half cannot be imported.
OTEL_MISSING: Final[str] = (
    "hajer.workflow / component / tool emit no spans: the OpenTelemetry SDK is not installed. "
    "Install the `otel` extra (`pip install 'hajer[otel]'`) to export them."
)
#: What the `hajer` logger says when `HajerSettings.from_env()` refuses the environment: the emitter runs on defaults.
SETTINGS_INVALID: Final[str] = (
    "hajer telemetry is running on default settings because the environment could not be read: {error}"
)
_LOG: Final = logging.getLogger("hajer")
_NO_ERROR: Final[tuple[None, None, None]] = (None, None, None)
#: The catalog's default policy, built once: tool arguments are redacted under the same rules as every other
#: document the SDK sends, and `build_policy()` with no extras cannot refuse.
_DEFAULT_POLICY: Final = build_policy()


# ── the two seams the OTel half fills ───────────────────────────────────────────────────────────


class SpanHandle(Protocol):
    """One open span as this module holds it: something that can be ended, with or without an error."""

    def end(self, error: BaseException | None) -> None: ...


class Backend(Protocol):
    """What `hajer._telemetry_otel.build` returns. Structural, so this module never names an OTel type."""

    def start(self, name: str, attributes: Attributes, *, traceparent: str | None) -> SpanHandle: ...
    def flush(self, timeout_ms: int) -> bool: ...


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
    backend: Backend | None = None
    backend_loaded: bool = False
    warned: bool = False
    exit_hook_registered: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_CONFIGURATION = _Configuration()


def configure(settings: HajerSettings | None = None, *, tracer_provider: object | None = None) -> None:
    """Choose the settings and the tracer provider every later span uses; `None` means read the environment lazily.

    The explicit override and the test seam in one: an application with its own OpenTelemetry setup passes the
    provider it wants the spans on, a test passes an isolated SDK provider with an in-memory exporter, and
    `configure()` with nothing resets to the defaults. Takes effect for the next span; an open span is unaffected.
    """
    with _CONFIGURATION.lock:
        _CONFIGURATION.settings = settings
        _CONFIGURATION.tracer_provider = tracer_provider
        _CONFIGURATION.backend = None
        _CONFIGURATION.backend_loaded = False
        _CONFIGURATION.warned = False


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


def _backend() -> Backend | None:
    """The OTel half, imported on first use, or `None` — once, with one warning — when it cannot be.

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
    try:
        module = importlib.import_module("hajer._telemetry_otel")
        build = cast(Callable[[HajerSettings, object | None], Backend], module.build)
        backend: Backend | None = build(settings, _CONFIGURATION.tracer_provider)
    except Exception:  # noqa: BLE001 - an optional dependency, or a broken provider, may never break the application
        backend = None
    with _CONFIGURATION.lock:
        _CONFIGURATION.backend = backend
        if backend is None and not _CONFIGURATION.warned:
            _CONFIGURATION.warned = True
            _LOG.warning(OTEL_MISSING)
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
    """The `atexit` hook: whatever is still queued gets its one chance to leave. Never raises, at exit least of all."""
    try:
        flush()
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
        attributes: Attributes = {}
        if self._kind == "workflow":
            attributes["hajer.workflow.id"] = self._identity
        else:
            if enclosing.workflow is not None:
                attributes["hajer.workflow.id"] = enclosing.workflow
            if self._kind == "component":
                attributes["hajer.component.id"] = self._identity
            else:
                if enclosing.component is not None:
                    attributes["hajer.component.id"] = enclosing.component
                attributes["hajer.tool.id"] = self._identity
                attributes["gen_ai.tool.name"] = self._name or self._identity
                attributes["gen_ai.operation.name"] = EXECUTE_TOOL
                encoded = _tool_arguments(self._arguments, settings)
                if encoded is not None:
                    attributes["gen_ai.tool.call.arguments"] = encoded
        if settings.environment is not None:
            attributes["deployment.environment"] = settings.environment
            attributes["deployment.environment.name"] = settings.environment
        run_id = settings.eval_run_id if binding is None or binding.run_id is None else binding.run_id
        if run_id is not None:
            attributes["hajer.eval.run.id"] = run_id
        if binding is not None:
            if binding.test_case_id is not None:
                attributes["hajer.eval.test_case.id"] = binding.test_case_id
            if binding.workflow_id is not None:
                attributes["hajer.eval.workflow.id"] = binding.workflow_id
            if binding.obligation_ids:
                attributes["hajer.eval.obligation.ids"] = binding.obligation_ids
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
        document, _entries = (
            redact_document(arguments, policy=_DEFAULT_POLICY) if settings.redact_client else (arguments, ())
        )
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

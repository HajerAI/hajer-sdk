"""Opt-in, reversible installation. Hajer always records a call through its own wrap or HTTP capture.

Whatever other tool traces the same calls — an OpenTelemetry instrumentor, a vendor's drop-in client — Hajer's own
patch records them, with their caller frames: yielding a call to another tool was tried for three review rounds
and could not be made loss-free across emitters. `instrument(tracer_provider=…)` adds
an OpenTelemetry receiver for the calls Hajer cannot wrap; a span of a call Hajer also recorded is matched to that
record and dropped (`_claims`), and a call recorded from a span says it has no frames.
"""

from __future__ import annotations

import functools
import importlib
import importlib.abc
import importlib.machinery
import sys
import threading
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import MethodType, ModuleType
from typing import cast

import httpx

from hajer import _claims, _wrap
from hajer._client import Hajer
from hajer._settings import HajerSettings

_CLIENTS = {
    "openai": ("OpenAI", "AsyncOpenAI", "AzureOpenAI", "AsyncAzureOpenAI"),
    "anthropic": (
        "Anthropic",
        "AsyncAnthropic",
        "AnthropicBedrock",
        "AsyncAnthropicBedrock",
        "AnthropicVertex",
        "AsyncAnthropicVertex",
    ),
}
_LOCK = threading.RLock()


@dataclass
class _Patch:
    owner: weakref.ReferenceType[object]
    name: str
    original: Callable[[], object | None]
    replacement: weakref.ReferenceType[object]

    def restore(self) -> None:
        holder = self.owner()
        original = self.original()
        if holder is not None and original is not None and getattr(holder, self.name, None) is self.replacement():
            setattr(holder, self.name, original)


@dataclass
class Instrumentation:
    """The installation receipt says whether an OTel source is connected; absence is never verified."""

    mode: str
    tracer_provider: object | None = None
    connected: bool = False
    detail: str = ""
    #: `FRAMES` while every call is recorded here and carries its caller frames; `DEGRADED: …` once any call is
    #: recorded from a span instead (`LINKAGE_DEGRADED`), whatever `connected` says.
    linkage: str = "FRAMES"
    patches: list[_Patch] = field(default_factory=list, repr=False)
    client: Hajer | None = field(default=None, repr=False)
    receiver: object | None = field(default=None, repr=False)
    finder: _Finder | None = field(default=None, repr=False)
    previous_hook: _wrap.SettledHook | None = field(default=None, repr=False)
    hook: _wrap.SettledHook | None = field(default=None, repr=False)

    def remember(self, owner: object, name: str, original: object, replacement: object) -> None:
        # A bound original and a replacement closure both keep the resource alive. Neither may
        # be retained by the installation receipt; the resource itself owns its active method.
        self.patches[:] = [patch for patch in self.patches if patch.owner() is not None]
        original_ref = weakref.WeakMethod(original) if isinstance(original, MethodType) else weakref.ref(original)
        self.patches.append(_Patch(weakref.ref(owner), name, original_ref, weakref.ref(replacement)))


@dataclass
class _State:
    installation: Instrumentation | None = None


_STATE = _State()


def _patch_module(module: ModuleType, settings: HajerSettings, state: Instrumentation) -> None:
    for name in _CLIENTS.get(module.__name__, ()):
        owner: object = getattr(module, name, None)
        if not isinstance(owner, type):
            continue
        original = owner.__init__
        if getattr(original, "__hajer_global__", False):
            continue

        def build(init: Callable[..., None]) -> Callable[..., None]:
            @functools.wraps(init)
            def constructor(instance: object, *args: object, **kwargs: object) -> None:
                init(instance, *args, **kwargs)
                if _STATE.installation is state:
                    try:
                        _wrap.instrument(instance, settings, on_patch=state.remember)
                    except Exception:  # noqa: BLE001, S110 - instrumentation cannot break construction
                        pass

            marker = "__hajer_global__"
            setattr(constructor, marker, True)
            return constructor

        replacement = build(original)
        state.remember(owner, "__init__", original, replacement)
        owner.__init__ = replacement


class _Loader(importlib.abc.Loader):
    def __init__(self, inner: importlib.abc.Loader, settings: HajerSettings, state: Instrumentation) -> None:
        self.inner, self.settings, self.state = inner, settings, state

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self.inner.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self.inner.exec_module(module)
        try:
            _patch_module(module, self.settings, self.state)
        except Exception:  # noqa: BLE001, S110 - import success belongs to the provider
            pass


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, settings: HajerSettings, state: Instrumentation) -> None:
        self.settings, self.state = settings, state

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname not in _CLIENTS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, importlib.abc.Loader):
            spec.loader = _Loader(spec.loader, self.settings, self.state)
        return spec


def instrument(
    *,
    settings: HajerSettings | None = None,
    transport: httpx.BaseTransport | None = None,
    tracer_provider: object | None = None,
) -> Instrumentation:
    """Install once before client construction, and return the receipt (the standing one on a later call).

    Provider clients are instrumented as they are built, whatever else is loaded or traces them. An explicit
    `tracer_provider` adds the OpenTelemetry receiver for the model calls Hajer cannot wrap; the receipt says
    linkage is degraded once one of them is recorded.
    """
    with _LOCK:
        if _STATE.installation is not None:
            return _STATE.installation
        resolved = settings if settings is not None else HajerSettings.from_env()
        state = Instrumentation(mode="wrap")
        _STATE.installation = state
        state.client = Hajer(settings=resolved, transport=transport)
        state.previous_hook = _wrap.settled_hook()

        def observe(call: _wrap.WrappedCall) -> None:
            if state.client is not None:
                state.client.observe_call(call)

        state.hook = observe
        _wrap.set_settled_hook(state.hook)
        _wrap.run_installers(resolved)
        if tracer_provider is not None:
            _connect_otel(state, resolved, tracer_provider)
        state.finder = _Finder(resolved, state)
        sys.meta_path.insert(0, state.finder)
        for name in _CLIENTS:
            module = sys.modules.get(name)
            if module is not None:
                _patch_module(module, resolved, state)
        return state


def _restore_patches(state: Instrumentation) -> None:
    if state.finder in sys.meta_path:
        sys.meta_path.remove(state.finder)
    state.finder = None
    for patch in reversed(state.patches):
        patch.restore()
    state.patches.clear()


def _connect_otel(state: Instrumentation, settings: HajerSettings, source: object) -> None:
    """Receive the model spans on the provider named by `tracer_provider=`, beside Hajer's own capture."""

    def degraded() -> None:
        state.linkage = "DEGRADED: " + _wrap.LINKAGE_DEGRADED

    try:
        module = importlib.import_module("hajer._otel")
        connect = cast(Callable[..., tuple[object, object, bool]], module.connect)
        state.tracer_provider, state.receiver, state.connected = connect(settings, source, degraded)
        state.detail = (
            "Model calls Hajer does not wrap are observed from their gen_ai spans and carry no caller frames, so "
            "they cannot link to a call site by frame; a span of a call Hajer recorded is matched to that record."
        )
        if not state.connected:
            state.detail += " No readable SDK TracerProvider; pass tracer_provider=. Configured but unverified."
    except Exception:  # noqa: BLE001 - an optional dependency cannot prevent application startup
        state.connected = False
        state.detail = "OTel receiver unavailable; install hajer[otel]. Configured but unverified."


def uninstrument() -> None:
    """Restore only our bindings; later vendor changes remain theirs. Existing OTel bridges become inert."""
    with _LOCK:
        state = _STATE.installation
        if state is None:
            return
        _STATE.installation = None
        _restore_patches(state)
        _claims.forget()
        # `_http_capture` imports this module, so its reversal is reached at call time.
        from hajer._http_capture import uninstall_http_capture  # noqa: PLC0415

        uninstall_http_capture()
        if _wrap.settled_hook() is state.hook:
            _wrap.set_settled_hook(state.previous_hook)
        shutdown = "shutdown"
        if state.receiver is not None:
            cast(Callable[[], None], getattr(state.receiver, shutdown))()
        if state.tracer_provider is not None:
            cast(Callable[[], None], getattr(state.tracer_provider, shutdown))()
        if state.client is not None:
            state.client.close()

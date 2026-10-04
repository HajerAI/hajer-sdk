"""Opt-in, reversible installation: `hajer.instrument()` patches the provider client classes, `uninstrument()` restores.

Hajer always records a call through its own wrap or HTTP capture, with its caller frames. Whatever other tool
traces the same calls — an OpenTelemetry instrumentor, a vendor's drop-in client — Hajer's own patch records them;
whether a model span is *emitted* for a call another instrumentation already covers is the emitter's decision
(`hajer._telemetry`, `HAJER_MODEL_SPANS`).
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

from hajer import _wrap
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
    """The installation receipt: what was patched, so `uninstrument()` can put exactly that back."""

    mode: str
    patches: list[_Patch] = field(default_factory=list, repr=False)
    finder: _Finder | None = field(default=None, repr=False)

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


def instrument(*, settings: HajerSettings | None = None) -> Instrumentation:
    """Install once before client construction, and return the receipt (the standing one on a later call).

    Provider clients are instrumented as they are built, whatever else is loaded or traces them.
    """
    with _LOCK:
        if _STATE.installation is not None:
            return _STATE.installation
        resolved = settings if settings is not None else HajerSettings.from_env()
        state = Instrumentation(mode="wrap")
        _STATE.installation = state
        _wrap.run_installers(resolved)
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


def uninstrument() -> None:
    """Restore only our bindings; later vendor changes remain theirs."""
    with _LOCK:
        state = _STATE.installation
        if state is None:
            return
        _STATE.installation = None
        _restore_patches(state)
        # `_http_capture` imports this module, so its reversal is reached at call time.
        from hajer._http_capture import uninstall_http_capture  # noqa: PLC0415

        uninstall_http_capture()

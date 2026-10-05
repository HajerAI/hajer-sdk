"""`hajer.attach()` — the model calls of a whole process, recorded without a line of application code.

`wrap(client)` is one line at construction. Attach mode is the answer to a different question: *what is
this application actually asking models to do?*, asked of a process nobody has instrumented yet. With it
on, every provider client the process builds is instrumented at construction, so every model call it makes
is recorded and — with `HAJER_API_KEY` and `HAJER_TEAM_ID` set — leaves as a `gen_ai` span of its own
(`hajer._telemetry`). Nothing else changes: no scope is opened, nothing is declared.

    HAJER_ATTACH=1 PYTHONPATH="$(python -m hajer attach-path)" python -m your_app   # no code change
    import hajer.autoattach                                                        # or the one line
    hajer.attach()                                                                 # or explicitly

**How the hook is loaded.** Three ways, and they are the same hook:

1. the **shim**: `hajer/_bootstrap/` holds a `sitecustomize.py` whose whole body is
   `import hajer.autoattach`. Python imports `sitecustomize` at interpreter start-up from anywhere on
   `sys.path`, so putting that directory on `PYTHONPATH` attaches a process whose source nobody touched.
   `python -m hajer attach-path` prints the directory. It is a shim rather than a `.pth` file installed
   into `site-packages` for one reason: a `.pth` would attach every process in the environment as a
   property of *installation*, and an environment variable is a decision the operator can take back.
   (A published wheel may also ship the `.pth`; that is a packaging step, not a behaviour change.)
2. the **one line**: `import hajer.autoattach`, first thing in your entry point. It reads `HAJER_ATTACH`,
   so the line is safe to leave in a repository: the variable is the switch.
3. **`hajer.attach()`** in code, which does not consult the variable — a developer who wrote the call has
   already said yes.

**What is instrumented, and how it is found without importing a provider library.** The same rule as
`wrap`: the leaf `create` of a recognised surface, found by attribute. The difference is *when*. Attach
patches the **classes** a provider library exports, so every client the process constructs after that
point is instrumented at construction; and it installs a `sys.meta_path` finder so a library imported
*later* is patched the moment it is imported. Nothing here imports `openai`, `anthropic` or any LangChain
package — the finder wraps the loader the ordinary machinery chose, and the classes are read out of the
module object by name after it has executed. A module that is already imported when `attach()` is called
is patched immediately, so import order does not decide whether capture happens.

**What leaves the process** is what `hajer._telemetry` says a `gen_ai` span carries, under the same
switches as every other call: `HAJER_CAPTURE_CONTENT` for message text, tool arguments and tool results,
`HAJER_CAPTURE_CALL_SITE` for where the call was made. **With no key nothing is sent.** `HAJER_API_KEY` /
`HAJER_TEAM_ID` absent, or `HAJER_DISABLED=1`, and the attachment is inert: the classes are still
instrumented (so `hajer.wrapped_calls()` works and a test suite sees the same records), and no socket is
opened.

**What it costs the call it observes.** One record per call and one span handed to a batching exporter on
its own thread. Every step at the seam (`_wrap._opened`, `_wrap._settled`) is wrapped in a `try`, because
a monitoring path that raises into somebody's request path is worse than a missing span.
"""

from __future__ import annotations

import functools
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Final

from hajer import _targets_genai, _targets_litellm, _wrap
from hajer._settings import HajerSettings
from hajer._targets import Support, Target

#: The libraries attach mode watches for. The two provider clients `wrap` reads, their Azure and Bedrock
#: and Vertex variants (the same `create` behind a different constructor), the two LangChain packages
#: measured against a real subject repository — whose chat models hold the provider client on `_client` /
#: `_async_client` and are instrumented there, never at the framework's own method — and the two libraries
#: whose surfaces are not a `create` at all, each declared by its own module (`_targets_litellm.py`,
#: `_targets_genai.py`).
_TARGETS: Final[tuple[Target, ...]] = (
    Target(
        module="openai",
        classes=("OpenAI", "AsyncOpenAI", "AzureOpenAI", "AsyncAzureOpenAI"),
        support=Support(
            label="openai",
            sync=True,
            asynchronous=True,
            streaming=True,
            usage=False,
            content=True,
            caveat=(
                "A streamed call names its tokens only when the request carried "
                '`stream_options={"include_usage": True}`; without it the record says so rather than '
                "reporting zero. `chat.completions`, `responses` and both `stream` helpers are covered."
            ),
        ),
    ),
    Target(
        module="anthropic",
        classes=(
            "Anthropic",
            "AsyncAnthropic",
            "AnthropicBedrock",
            "AsyncAnthropicBedrock",
            "AnthropicVertex",
            "AsyncAnthropicVertex",
        ),
        support=Support(
            label="anthropic",
            sync=True,
            asynchronous=True,
            streaming=True,
            usage=True,
            content=True,
            caveat=(
                "`messages.create`, `beta.messages.create` and `messages.stream` are covered. A caller "
                "who reads only `text_stream` records 0 chunks; `get_final_message()` carries the usage."
            ),
        ),
    ),
    Target(
        module="langchain_anthropic",
        prefixes=("Chat",),
        support=Support(
            label="langchain-anthropic",
            sync=True,
            asynchronous=True,
            streaming=True,
            usage=True,
            content=True,
            caveat=(
                "Instrumented at the inner `anthropic` client the chat model holds (`_client` / "
                "`_async_client`), so the record is of the request that left the process."
            ),
        ),
    ),
    Target(
        module="langchain_openai",
        prefixes=("Chat", "AzureChat"),
        support=Support(
            label="langchain-openai",
            sync=True,
            asynchronous=True,
            streaming=True,
            usage=False,
            content=True,
            caveat=(
                "Instrumented at the inner `openai` clients (`root_client` / `root_async_client`), through the "
                "`with_raw_response` call LangChain makes: the record is the request that left the process and "
                "the answer it parsed. The streamed-usage caveat of `openai` applies (`stream_usage=True`)."
            ),
        ),
    ),
    _targets_litellm.TARGET,
    _targets_genai.TARGET,
)

#: Set on a class whose `__init__` this module has already wrapped. Read out of the class's **own**
#: `__dict__` and never inherited, so a subclass is patched rather than silently skipped.
_ATTACHED: Final[str] = "__hajer_attached__"


@dataclass(frozen=True, slots=True)
class Attachment:
    """What one `attach()` did: the classes it instrumented, and whether anything can be sent.

    `inert` is the honest half. An attachment with no credential still instruments everything — a test
    suite sees the same `wrapped_calls()` it would in production — and nothing it records reaches a socket.
    """

    classes: tuple[str, ...]
    modules: tuple[str, ...]
    inert: bool

    def describe(self) -> str:
        """One line for a log or a receipt: what was attached, and whether it can send."""
        where = ", ".join(self.modules) or "no provider library imported yet"
        return (
            f"hajer attached to {len(self.classes)} client classes ({where}); "
            f"{'inert: no key, nothing is sent' if self.inert else 'exporting every model call as a span'}"
        )


@dataclass(slots=True)
class _State:
    """The one attachment a process has. Written by `attach()` and `detach()` and by nothing else.

    `settings` is what "attached" means: it is set by `attach()` and cleared by `detach()`, and the
    constructor wrappers read it **at construction time** rather than closing over the settings they were
    installed with. That is what makes detaching real — a client built after `detach()` is not
    instrumented, even though the class it came from is still patched — and it is why re-attaching with
    different settings takes effect instead of silently keeping the first ones.
    """

    settings: HajerSettings | None = None
    finder: _AttachFinder | None = None
    classes: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)


_STATE = _State()


def _snapshot(settings: HajerSettings) -> Attachment:
    """What is attached right now, as a value. Recomputed rather than stored: the import hook keeps
    finding libraries after `attach()` returned, and a snapshot taken then would go stale."""
    return Attachment(classes=tuple(_STATE.classes), modules=tuple(dict.fromkeys(_STATE.modules)), inert=settings.inert)


# ── instrumenting a class, so every client built after this point is recorded ─────────────────────


def _instrument_class(owner: type[object], *, where: str) -> bool:
    """Wrap one client class's `__init__` so the instance it builds is instrumented. Idempotent.

    The constructor is the seam because it is the only moment attach mode is guaranteed to see: the
    object may be built anywhere, kept anywhere and called from anywhere, and the surfaces it carries —
    including the inner provider client a framework chat model builds for itself — exist by the time its
    own `__init__` returns.

    `where` is the name the library exports it under, which is the name an operator recognises — the
    class's own `__module__` may be a private submodule two packages away.
    """
    if owner.__dict__.get(_ATTACHED) is True:
        return False
    original = owner.__init__

    @functools.wraps(original)
    def __init__(instance: object, *args: object, **kwargs: object) -> None:  # noqa: N807 - it is __init__
        original(instance, *args, **kwargs)
        settings = _STATE.settings
        if settings is None:  # detached: the class stays patched, and the patch stops doing anything
            return
        try:
            _wrap.instrument(instance, settings)
        except Exception:  # noqa: BLE001, S110 - instrumenting a client may never break constructing one
            pass

    try:
        owner.__init__ = __init__  # pyright: ignore[reportAttributeAccessIssue] - the patch is the point
        setattr(owner, _ATTACHED, True)
    except (AttributeError, TypeError):
        # A class that refuses attribute assignment (a C extension type, a frozen namespace) is left
        # alone: `wrap(client)` still works on its instances, and saying nothing about it here is better
        # than an exception inside somebody's import.
        return False
    _STATE.classes.append(where)
    return True


def _instrument_module(module: ModuleType) -> int:
    """Patch what one of the targets recognises in `module`: its classes, and its own entry points.

    A class target is patched at its constructor, so every client built after this point is instrumented.
    A **function** target (litellm) has no constructor: the module attribute itself is replaced, and
    `_targets.rebind` moves every alias another module already bound, so import order stops deciding
    whether a call is captured.
    """
    name = getattr(module, "__name__", "")
    target = next((entry for entry in _TARGETS if entry.module == name), None)
    if target is None:
        return 0
    patched = 0
    for attribute, value in list(vars(module).items()):
        if not isinstance(value, type) or not target.matches(attribute):
            continue
        patched += int(_instrument_class(value, where=f"{name}.{attribute}"))
    settings = _STATE.settings
    if target.functions and settings is not None:
        patched += _wrap.instrument(module, settings)
    if patched:
        _STATE.modules.append(name)
    return patched


# ── instrumenting a library that is not imported yet ─────────────────────────────────────────────


class _AttachLoader(importlib.abc.Loader):
    """The loader the machinery chose, plus one step: patch the module once it has executed.

    Everything else is delegated, `__getattr__` included, so a package still answers the questions the
    import system and `pkgutil` ask of its loader.
    """

    def __init__(self, inner: importlib.abc.Loader) -> None:
        self._inner = inner

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self._inner.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self._inner.exec_module(module)
        try:
            _instrument_module(module)
        except Exception:  # noqa: BLE001, S110 - a failed attachment may never fail somebody's import
            pass

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _AttachFinder(importlib.abc.MetaPathFinder):
    """First on `sys.meta_path`: for a target module, find the real spec and wrap its loader.

    `importlib.util.find_spec` walks `sys.meta_path` again, so the name being resolved is held in
    `_resolving` while that happens — otherwise this finder would answer its own question forever.
    """

    def __init__(self) -> None:
        self._resolving: set[str] = set()

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,  # noqa: ARG002 - the protocol's signature, unused by design
        target: ModuleType | None = None,  # noqa: ARG002 - likewise: this finder resolves by name only
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname in self._resolving or not any(entry.module == fullname for entry in _TARGETS):
            return None
        self._resolving.add(fullname)
        try:
            spec = importlib.util.find_spec(fullname)
        except (ImportError, AttributeError, ValueError):
            return None
        finally:
            self._resolving.discard(fullname)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _AttachLoader(spec.loader)
        return spec


# ── the two public calls ─────────────────────────────────────────────────────────────────────────


def attach(*, settings: HajerSettings | None = None) -> Attachment:
    """Instrument this process: every provider client built from here on records, and exports, its model calls.

    Idempotent — the second call returns the standing attachment and changes nothing. `settings` is a way
    to attach with settings a caller built themselves.

    What it does, in this order and for this reason: instruments the provider libraries that are
    **already** imported (so import order does not decide whether capture happens), then installs the
    import hook for the ones that are not.
    """
    standing = _STATE.settings
    if standing is not None:
        return _snapshot(standing)
    resolved = settings if settings is not None else HajerSettings.from_env()
    _STATE.settings = resolved
    for name in list(sys.modules):
        module = sys.modules.get(name)
        if module is not None:
            _instrument_module(module)
    finder = _AttachFinder()
    sys.meta_path.insert(0, finder)
    _STATE.finder = finder
    return _snapshot(resolved)


def detach() -> None:
    """Stop instrumenting new clients and remove the import hook. Idempotent.

    It does **not** un-patch what it patched, and says so rather than pretending. Three things stay as
    they are, each for the same reason — another thread may be inside them right now:

    - a **constructor** this module wrapped stays wrapped, though the patch now does nothing, so a client
      constructed after this point is not instrumented at all;
    - a client **already built and instrumented** keeps recording and emitting exactly as `wrap(client)`
      alone would (that is `wrap`'s behaviour, and detaching does not undo it); `HAJER_DISABLED=1` or
      `HAJER_TRACES_ENABLED=0` is what stops a process from exporting;
    - a **module function** (litellm's `completion` / `acompletion`) and every alias `_targets.rebind`
      moved stay pointing at the replacement.
    """
    finder = _STATE.finder
    if finder is not None and finder in sys.meta_path:
        sys.meta_path.remove(finder)
    _STATE.settings = None
    _STATE.finder = None
    _STATE.classes.clear()
    _STATE.modules.clear()


def targets() -> tuple[Target, ...]:
    """Every library attach mode watches, in declaration order.

    One reader outside this module: `scripts/generate_support_matrix.py`, which prints
    the support matrix on docs.hajer.ai from these declarations so the documented support and the
    instrumented support cannot drift. `tests/test_support_matrix.py` holds them to a printable table.
    """
    return _TARGETS


def attachment() -> Attachment | None:
    """What this process has attached right now, or `None` when it is not attached."""
    settings = _STATE.settings
    return None if settings is None else _snapshot(settings)

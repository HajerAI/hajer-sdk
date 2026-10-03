"""litellm: one function call with a hundred providers behind it.

`litellm.completion(model="anthropic/claude-sonnet-5", …)` is the whole surface, and it is a **module
function** rather than a client method — so there is no constructor to patch and no object to hand to
`wrap`. What attach mode replaces is the module attribute, and then every name already bound to the
original by `from litellm import completion` somewhere else in the process (`_targets.rebind`), because
otherwise whether a call is captured would depend on the order two imports happened to run in.

**The provider is in the model string.** litellm routes by prefix, so `anthropic/claude-sonnet-5` names
both the upstream and the model. A record that said provider `litellm`, model `anthropic/claude-sonnet-5`
would be true about the router and useless about the call: the receipt is about what answered, so the
prefix is split out and the upstream's own name is recorded. An unprefixed model (litellm accepts
`gpt-4o` and resolves it itself) keeps the router's name, because that is all this seam was told.

**Usage arrives as an object or as a dict.** `litellm.completion` returns a `ModelResponse` with
attributes; a customer's own middleware, a cache layer or `litellm.completion` under some configurations
hands back the same document as a plain dict. `_wrap.py` reads both the same way (`_member` looks up a
mapping key and an attribute), so the price is derived either way rather than only on the happy shape.
"""

from __future__ import annotations

import inspect
from typing import Final

from hajer import _wrap
from hajer._settings import HajerSettings
from hajer._targets import Support, Target, rebind
from hajer._wrap import WrappedCall, instrument_coroutine, instrument_function, read_chat_completion

#: What the record says when litellm gave us no upstream to name.
PROVIDER: Final[str] = "litellm"
#: The api each entry point is. One name for both, because `acompletion` is the same contract awaited.
API: Final[str] = "litellm.completion"

TARGET: Final[Target] = Target(
    module="litellm",
    functions=("completion", "acompletion"),
    support=Support(
        label="litellm",
        sync=True,
        asynchronous=True,
        streaming=True,
        usage=True,
        content=True,
        caveat=(
            "The upstream provider is read from the model prefix (`anthropic/claude-sonnet-5`); an "
            "unprefixed model is recorded as provider `litellm`. Patched on the module, so a name bound "
            "by `from litellm import completion` before `attach()` is rebound — see the import-order rule."
        ),
    ),
)


def upstream(model: str | None) -> tuple[str, str | None]:
    """`anthropic/claude-sonnet-5` → (`anthropic`, `claude-sonnet-5`). Unprefixed stays as it is."""
    if model is None:
        return PROVIDER, None
    provider, separator, name = model.partition("/")
    if not separator or not name or not provider:
        return PROVIDER, model
    return provider, name


def read(call: WrappedCall, response: object, settings: HajerSettings) -> None:
    """A litellm answer is OpenAI-shaped; the only difference is whose name goes on the receipt."""
    read_chat_completion(call, response, settings)
    call.provider, call.model = upstream(call.model)


def instrument(candidate: object, settings: HajerSettings) -> int:
    """Replace `completion` and `acompletion` on the litellm module, and rebind their aliases.

    Registered with `_wrap.add_instrumenter`, so it is reached from both doors: `attach()` hands it the
    module as soon as litellm is imported, and `wrap(litellm)` hands it the same object for a developer
    who would rather name the library than attach the process.
    """
    if getattr(candidate, "__name__", None) != TARGET.module:
        return 0
    patched = 0
    for name in TARGET.functions:
        original = getattr(candidate, name, None)
        if not callable(original):
            continue
        build = instrument_coroutine if inspect.iscoroutinefunction(original) else instrument_function
        replacement = build(original, provider=PROVIDER, api=API, settings=settings, read=read)
        if replacement is None:  # already instrumented; patching twice would record the call twice
            continue
        setattr(candidate, name, replacement)
        rebind(original, replacement)
        patched += 1
    return patched


_wrap.add_instrumenter(instrument)

"""What attach mode knows about one library, and how a name already bound elsewhere is reached.

A `Target` is a declaration, not behaviour: the module to watch, the client classes to patch at
construction, the module-level functions to replace, and what capture that library actually gets
(`Support`, which is the row `python/docs/support-matrix.md` prints — generated from these declarations by
`python/scripts/generate_support_matrix.py`, never written by hand, so the documented support cannot drift
from the instrumented support).

**Why `rebind` exists.** Patching `litellm.completion` replaces one binding: the module's own. A caller
who wrote `from litellm import completion` at the top of their module bound the *function object* into
their namespace when they imported it, and that binding never looks at `litellm` again. Attach mode
happens at an arbitrary moment in a process's life, usually after those imports — so without rebinding,
whether a call is captured would depend on import order, which is the one thing a monitoring tool must
never make the customer reason about. `rebind` walks the modules already loaded and replaces every name
still pointing at the original. Import order stops deciding; `README.md` states the one case it cannot
fix (a local variable, or an alias created after `detach()`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final

#: Modules this package will not rewrite bindings inside. Hajer's own alias of a provider function is
#: Hajer's business, and rewriting it would mean instrumenting the instrumentation.
_OURS: Final[str] = __name__.split(".", maxsplit=1)[0]


@dataclass(frozen=True, slots=True)
class Support:
    """What capture one library actually gets, as one row of the support matrix.

    Honest columns, not aspirational ones: `usage` is false when the library's answer carries no token
    counts at this seam, and `caveat` is the sentence a reader needs before they trust the row.
    """

    #: The name a customer knows the library by (`openai`, `litellm`, `google-genai`).
    label: str
    #: A synchronous client or function is instrumented.
    sync: bool
    #: An `async` client or coroutine function is instrumented.
    asynchronous: bool
    #: A streamed call is recorded, at exhaustion or close.
    streaming: bool
    #: Token usage is observable for this library — including for its streamed calls.
    usage: bool
    #: Message text, tool arguments and tool results can be recorded with `HAJER_CAPTURE_CONTENT=1`.
    content: bool
    #: What a reader has to know before trusting the row above.
    caveat: str

    def row(self) -> tuple[str, ...]:
        """This library's cells, in `COLUMNS` order. The projection the generated matrix prints.

        Here rather than in the generator so the columns and their order are declared once, beside the
        data: a generator that decided the order would let the table and the declaration disagree about
        which flag is which, and `tests/test_support_matrix.py` asserts against this.
        """
        return (
            self.label,
            _mark(self.sync),
            _mark(self.asynchronous),
            _mark(self.streaming),
            _mark(self.usage),
            _mark(self.content),
            self.caveat,
        )


#: The support matrix's columns, in order. `scripts/generate_support_matrix.py` prints these headings
#: and one `Support.row()` per target; nothing about the table is written by hand.
COLUMNS: Final[tuple[str, ...]] = (
    "SDK",
    "sync",
    "async",
    "streaming",
    "usage available",
    "content captured",
    "caveat",
)
#: How a yes and a no are spelled in that table. One place, so every cell reads the same.
YES: Final[str] = "yes"
NO: Final[str] = "no"


def _mark(value: bool) -> str:
    return YES if value else NO


@dataclass(frozen=True, slots=True)
class Target:
    """One library attach mode watches, and how clients are recognised inside it.

    `classes` names them exactly; `prefixes` matches a family whose members are not worth enumerating —
    LangChain's chat models are `ChatAnthropic`, `ChatOpenAI`, `AzureChatOpenAI`, and a new one arrives
    with every provider. Matching by name is safe because being patched costs an object nothing: the
    constructor wrapper instruments the instance only if it actually carries a surface `wrap` recognises,
    and an object that carries none is left exactly as it was. `functions` is for a library whose entry
    point is not a client at all (`litellm.completion`), patched on the module object itself.
    """

    module: str
    support: Support
    classes: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()

    def matches(self, name: str) -> bool:
        return name in self.classes or any(name.startswith(prefix) for prefix in self.prefixes)


def rebind(original: object, replacement: object) -> int:
    """Point every already-bound alias of `original` at `replacement`. Returns how many it moved.

    Only names that are *still* the original object are touched, by identity — so this cannot rebind a
    second library's unrelated function of the same name, and running twice moves nothing the second
    time. A module whose namespace is being mutated by another thread is skipped rather than retried:
    the cost of missing one alias is a call recorded as a summary somebody else's import already had,
    and the cost of raising here would be a broken `attach()`.
    """
    moved = 0
    for name, module in list(sys.modules.items()):
        if name == _OURS or name.startswith(f"{_OURS}."):
            continue
        try:
            bindings = list(vars(module).items())
        except TypeError:  # pragma: no cover - an entry with no __dict__ (a namespace stand-in, or None)
            continue
        for attribute, value in bindings:
            if value is not original:
                continue
            try:
                setattr(module, attribute, replacement)
            except (AttributeError, TypeError):  # pragma: no cover - a module that refuses assignment
                continue
            moved += 1
    return moved

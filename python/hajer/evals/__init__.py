"""`hajer.evals` — the eval runner's public surface: the provider helper that binds a Promptfoo test to the trace.

The engine is upstream promptfoo at the exact version pinned in `engine/package.json`; `hajer eval` installs
and drives it, and everything Hajer adds is Python in this package. Nothing in the core SDK imports it.

Two names, because a Python provider is one function and the engine hands that function everything the binding
needs: `provider` decorates `call_api(prompt, options, context)` and `bind(context)` is the same thing as a block,
for a provider that is not one function. Both read the engine's `context` — the W3C `traceparent` it opened for the
row, the `testCaseId`, and the suite author's `test.metadata.hajer` (`workflowId`, `obligationIds`) — tolerating
every one of them missing or mistyped: a helper that raised inside a provider would fail the row it was meant to
grade. On exit the binding flushes, so the spans are at the engine's receiver before the row's assertions run.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
from collections.abc import Awaitable, Callable, Generator
from typing import ParamSpec, TypeVar, cast

from hajer._json import JsonObject, JsonValue
from hajer._telemetry import eval_binding, flush

P = ParamSpec("P")
R = TypeVar("R")

__all__ = ["bind", "provider"]


def provider(call_api: Callable[P, R]) -> Callable[P, R]:
    """Decorate a Promptfoo Python provider so every span the application emits during it belongs to the row.

        @hajer.evals.provider
        def call_api(prompt, options, context):
            return {"output": answer(prompt)}

    Sync or `async def`, signature and return type kept. The engine's `context` is the third positional argument
    (or `context=`); a call without one still runs, unbound.
    """
    if inspect.iscoroutinefunction(call_api):
        awaitable = cast(Callable[P, Awaitable[object]], call_api)

        @functools.wraps(call_api)
        async def awaited(*args: P.args, **kwargs: P.kwargs) -> object:
            with bind(_context(args, kwargs)):
                return await awaitable(*args, **kwargs)

        return cast(Callable[P, R], awaited)

    @functools.wraps(call_api)
    def called(*args: P.args, **kwargs: P.kwargs) -> R:
        with bind(_context(args, kwargs)):
            return call_api(*args, **kwargs)

    return called


@contextlib.contextmanager
def bind(context: JsonObject) -> Generator[None]:
    """Bind the spans emitted inside the block to the Promptfoo row `context` describes, and flush them on exit.

    The run id is left to `HAJER_EVAL_RUN_ID`, which `hajer eval` sets for the engine process; `evaluationId` is the
    engine's own and is not carried. A value of the wrong shape is read as absent rather than refused.
    """
    hajer_metadata = _object(_object(_object(context).get("test")).get("metadata")).get("hajer")
    with eval_binding(
        run_id=None,
        test_case_id=_string(_object(context).get("testCaseId")),
        workflow_id=_string(_object(hajer_metadata).get("workflowId")),
        obligation_ids=_strings(_object(hajer_metadata).get("obligationIds")),
        traceparent=_string(_object(context).get("traceparent")),
    ):
        try:
            yield
        finally:
            flush()


def _context(args: tuple[object, ...], kwargs: dict[str, object]) -> JsonObject:
    """The engine's `context` argument, wherever the provider took it; an empty one when it took none."""
    found: object = kwargs["context"] if "context" in kwargs else (args[2] if len(args) > 2 else None)
    return _object(found)


def _object(value: object) -> JsonObject:
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def _string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _strings(value: JsonValue | None) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)

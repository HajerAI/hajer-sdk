"""The file promptfoo loads as the suite's extension — `file://<this path>:beforeAll` — and nothing else.

It exists because the engine looks a hook up by *name in a file*, in a fresh interpreter it starts itself: it
imports this file by path (not as `hajer.evals._hook_entry`), finds the function whose name is the hook's, calls it
as `beforeAll(context, {"hookName": "beforeAll"})` and writes the return value back as JSON. A function named after
one of its four hooks is run for that hook only, which is why the name is `beforeAll` and not `before_all`: a
function with any other name is run for *every* hook, in the legacy `(hookName, context)` argument order, and
would be handed the string where it expects the suite. The logic lives in `_hook`, which the tests import directly;
this module is the one-function adapter the engine's naming rule demands.
"""

from __future__ import annotations

from hajer._json import JsonObject
from hajer.evals._hook import before_all


def beforeAll(context: JsonObject, options: JsonObject | None = None) -> JsonObject:  # noqa: N802 - promptfoo looks the hook up by this exact name
    """Classify, filter and report the suite; `options` is the engine's `{"hookName": "beforeAll"}`, already known."""
    del options
    return before_all(context)

"""The promptfoo provider: how the eval reaches the application. This is the whole adapter.

`hajer.evals.provider` binds the test to the trace — the W3C `traceparent` the engine minted, the test case id,
the workflow and obligation ids from `metadata.hajer` — and flushes the spans before the row is graded. The
application code it calls is the production code, unchanged.
"""

from __future__ import annotations

from app import handle

import hajer.evals
from hajer._json import JsonObject


@hajer.evals.provider
def call_api(prompt: str, options: JsonObject, context: JsonObject) -> JsonObject:
    del options
    variables = context.get("vars")
    values: JsonObject = variables if isinstance(variables, dict) else {}
    message = values.get("message", prompt)
    customer_id = values.get("customer_id", "")
    return {"output": handle(str(message), str(customer_id))}

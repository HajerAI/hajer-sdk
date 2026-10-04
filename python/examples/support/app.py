"""A support workflow small enough to read in a minute, instrumented the way a production one is.

Three stable ids — `wf_support` → `cmp_refund_agent` → `tool_refund_status` — are the whole point: they are
what production telemetry and an eval of this code both emit, so the platform can tell which workflow a test
exercised and which component and tool actually ran. The text is deterministic and no model is called, so the
example runs anywhere, with no key, in a second.
"""

from __future__ import annotations

import hajer

#: What the (pretend) payments system knows. The eval's cases pick customers out of this table.
REFUNDS: dict[str, str] = {"cust_123": "pending", "cust_456": "completed"}


@hajer.tool("tool_refund_status", name="get_refund_status")
def get_refund_status(customer_id: str) -> str:
    """The tool a real agent would call: the current refund status, or `unknown`."""
    return REFUNDS.get(customer_id, "unknown")


@hajer.component("cmp_refund_agent")
def refund_agent(customer_id: str) -> str:
    """The component that must look the status up before it says anything about a refund."""
    status = get_refund_status(customer_id)
    if status == "pending":
        return "Your refund is still pending: it has been approved but has not completed yet."
    if status == "completed":
        return "Your refund has completed; the amount is back with your payment provider."
    return "I cannot find a refund for this account; please check the order number."


@hajer.workflow("wf_support")
def handle(message: str, customer_id: str) -> str:
    """The user-facing capability: route a support message to the component that can answer it."""
    if "refund" in message.lower():
        return refund_agent(customer_id)
    return "I can help with refund questions; tell me about your order."

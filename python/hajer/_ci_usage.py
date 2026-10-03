"""Provider usage parsing for CI accounting; neither prompts nor replies enter the ledger."""

import json
from typing import cast

from pydantic import JsonValue

from hajer._ci_prices import Price

MICROUSD_PER_USD = 1_000_000


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def count(value: JsonValue) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("CI_USAGE_UNAVAILABLE")
    return value


def rounded(numerator: int) -> int:
    return (numerator + MICROUSD_PER_USD - 1) // MICROUSD_PER_USD


def usage_document(raw: bytes) -> dict[str, JsonValue]:
    try:
        value = object_value(cast(JsonValue, json.loads(raw)))
    except (ValueError, UnicodeError):
        # SSE: merge Anthropic's message_start/message_delta usage, or OpenAI's final usage chunk.
        value = {}
        usage: dict[str, JsonValue] = {}
        finished = False
        for line in raw.splitlines():
            if not line.startswith(b"data:"):
                continue
            if line[5:].strip() == b"[DONE]":
                finished = True
                continue
            event = object_value(cast(JsonValue, json.loads(line[5:])))
            finished = finished or event.get("type") in {"message_stop", "response.completed"}
            usage.update(object_value(object_value(event.get("message")).get("usage")))
            usage.update(object_value(event.get("usage")))
            response = object_value(event.get("response"))
            usage.update(object_value(response.get("usage")))
        if not finished:
            raise ValueError("CI_USAGE_UNAVAILABLE") from None
        value["usage"] = usage
    return object_value(value.get("usage"))


def charge(provider: str, price: Price, raw: bytes) -> tuple[int, dict[str, int]]:
    usage = usage_document(raw)
    if provider == "anthropic":
        written = count(usage.get("cache_creation_input_tokens", 0))
        creation = object_value(usage.get("cache_creation"))
        if written and not creation:
            # A missing TTL split cannot be guessed as the cheaper five-minute cache tier.
            raise ValueError("CI_USAGE_UNAVAILABLE")
        hour = count(creation.get("ephemeral_1h_input_tokens", 0))
        short = count(creation.get("ephemeral_5m_input_tokens", written - hour))
        if short + hour != written:
            raise ValueError("CI_USAGE_UNAVAILABLE")
        counts = {
            "input": count(usage.get("input_tokens")),
            "output": count(usage.get("output_tokens")),
            "write5m": short,
            "write1h": hour,
            "read": count(usage.get("cache_read_input_tokens", 0)),
        }
    else:
        prompt = count(usage.get("prompt_tokens", usage.get("input_tokens")))
        details = object_value(usage.get("prompt_tokens_details", usage.get("input_tokens_details")))
        cached = count(details.get("cached_tokens", 0))
        if cached > prompt:
            raise ValueError("CI_USAGE_UNAVAILABLE")
        counts = {
            "input": prompt - cached,
            "output": count(usage.get("completion_tokens", usage.get("output_tokens"))),
            "write5m": 0,
            "write1h": 0,
            "read": cached,
        }
    amount = sum(
        counts[key] * rate
        for key, rate in (
            ("input", price.input),
            ("output", price.output),
            ("write5m", price.write_5m),
            ("write1h", price.write_1h),
            ("read", price.read),
        )
    )
    return rounded(amount), counts

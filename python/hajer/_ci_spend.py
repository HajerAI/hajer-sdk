"""Local atomic CI spend reservations. Interrupted or unpriced replies keep their full reserve.

This covers supported httpx/httpx2 provider requests admitted by the existing CI sandbox. It is
not protection against customer code deliberately tampering with its own process or ledger.
"""

import hashlib
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
from pydantic import JsonValue

from hajer._ci_prices import OUTPUT_LIMITS, PRICES, Price
from hajer._ci_usage import charge, object_value, rounded
from hajer._errors import SpendRefusedError
from hajer._settings import HajerSettings
from hajer._temporary import write_private


class Spend:
    def __init__(self, settings: HajerSettings) -> None:
        self.settings = settings
        self.reason: str | None = None

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection]:
        path = self.settings.ci_budget_file
        if not path or not self.settings.ci_budget_microusd:
            raise SpendRefusedError("CI_SPEND_NOT_CONFIGURED")
        target = Path(path)
        if target.is_symlink():
            raise SpendRefusedError("CI_SPEND_LEDGER_REFUSED")
        if not target.exists():
            try:
                write_private(target, "")
            except FileExistsError:
                pass
        connection = sqlite3.connect(target)
        connection.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, ceiling INTEGER)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, reserved INTEGER, charged INTEGER, evidence TEXT)"
        )
        connection.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (self.settings.ci_budget_microusd,))
        ceiling = connection.execute("SELECT ceiling FROM settings WHERE id = 1").fetchone()
        connection.commit()
        if ceiling != (self.settings.ci_budget_microusd,):
            connection.close()
            raise SpendRefusedError("CI_SPEND_CEILING_CHANGED")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def reserve(self, request: httpx.Request, body: bytes) -> tuple[str, str, Price]:
        try:
            return self._reserve(request, body)
        except (ValueError, OSError, sqlite3.Error) as error:
            self.reason = str(error) if isinstance(error, SpendRefusedError) else "CI_SPEND_UNAVAILABLE"
            raise SpendRefusedError(self.reason) from None

    def _reserve(self, request: httpx.Request, body: bytes) -> tuple[str, str, Price]:
        from_body = object_value(cast(JsonValue, json.loads(body)))
        if not from_body:
            raise SpendRefusedError("CI_REQUEST_UNSUPPORTED")
        # The concrete body stays in memory; neither it nor credentials are ever stored.
        provider = "anthropic" if request.url.path == "/v1/messages" else "openai"
        paths = {"anthropic": {"/v1/messages"}, "openai": {"/v1/chat/completions", "/v1/responses"}}
        if request.method != "POST" or request.url.path not in paths[provider] or request.url.query:
            raise SpendRefusedError("CI_REQUEST_UNSUPPORTED")
        model = from_body.get("model")
        if not isinstance(model, str) or (price := PRICES.get((provider, model))) is None:
            raise SpendRefusedError("CI_MODEL_UNPRICED")
        # Flex/priority, multiple answers, stateful requests and server tools have different billing envelopes.
        if from_body.get("service_tier", "default") not in {"default", "standard"} or from_body.get("n", 1) != 1:
            raise SpendRefusedError("CI_REQUEST_UNSUPPORTED")
        if from_body.get("inference_geo", "global") != "global" or from_body.get("speed", "standard") != "standard":
            raise SpendRefusedError("CI_REQUEST_UNSUPPORTED")
        if any(
            from_body.get(key)
            for key in (
                "previous_response_id",
                "background",
                "container",
                "conversation",
                "prompt",
                "mcp_servers",
                "web_search_options",
            )
        ):
            raise SpendRefusedError("CI_REQUEST_UNSUPPORTED")
        if from_body.get("audio") or from_body.get("modalities", ["text"]) != ["text"]:
            raise SpendRefusedError("CI_REQUEST_UNSUPPORTED")
        tools = from_body.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type", "function") != "function" for tool in tools
        ):
            raise SpendRefusedError("CI_SERVER_TOOL_UNSUPPORTED")
        output_evidence: dict[str, int | str] = {"basis": "request"}
        explicit = [
            from_body[key] for key in ("max_output_tokens", "max_completion_tokens", "max_tokens") if key in from_body
        ]
        caps: list[int] = []
        for value in explicit:
            if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < price.context:
                raise SpendRefusedError("CI_OUTPUT_LIMIT_REQUIRED")
            caps.append(value)
        if caps:
            # Conflicting cap fields cannot make us reserve less than any cap the provider might honour.
            maximum = max(caps)
        else:
            if (limit := OUTPUT_LIMITS.get((provider, model))) is None:
                raise SpendRefusedError("CI_OUTPUT_LIMIT_REQUIRED")
            maximum = limit.tokens
            output_evidence.update(basis="published_model_maximum", source=limit.source, date=limit.fetched)
        output_evidence["tokens"] = maximum
        # Reserve the WHOLE published context at its costliest cache tier, independent of tokenisation.
        reserved = rounded(
            price.context * max(price.input, price.write_5m, price.write_1h, price.read) + maximum * price.output
        )
        identity = uuid4().hex
        evidence = json.dumps(
            {
                "provider": provider,
                "model": model,
                "priceSource": price.source,
                "priceDate": price.fetched,
                "priceDigest": hashlib.sha256(repr(price).encode()).hexdigest(),
                "outputLimit": output_evidence,
            }
        )
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            used = connection.execute("SELECT COALESCE(SUM(COALESCE(charged, reserved)), 0) FROM calls").fetchone()[0]
            if used + reserved > self.settings.ci_budget_microusd:
                raise SpendRefusedError("CI_BUDGET_EXHAUSTED")
            connection.execute("INSERT INTO calls VALUES (?, ?, NULL, ?)", (identity, reserved, evidence))
        return identity, provider, price

    def settle(self, reservation: tuple[str, str, Price], status: int, body: bytes) -> None:
        identity, provider, price = reservation
        try:
            if not 200 <= status < 300:
                raise ValueError("CI_USAGE_UNAVAILABLE")
            amount, usage = charge(provider, price, body)
            with self._db() as connection:
                row = connection.execute("SELECT reserved, evidence FROM calls WHERE id = ?", (identity,)).fetchone()
                evidence = object_value(cast(JsonValue, json.loads(row[1])))
                evidence["usage"] = dict(usage)
                connection.execute(
                    "UPDATE calls SET charged = ?, evidence = ? WHERE id = ? AND charged IS NULL",
                    (amount, json.dumps(evidence), identity),
                )
                if amount > row[0]:
                    self.reason = "CI_USAGE_EXCEEDED_RESERVATION"
        except (ValueError, OSError, sqlite3.Error):
            self.reason = "CI_USAGE_UNAVAILABLE"

    def snapshot(self) -> dict[str, int | str]:
        metadata: dict[str, int | str] = {"version": 1, "source": "caller_reported", "scope": "ci_budget_ledger"}
        try:
            with self._db() as connection:
                settled, uncertain, calls = connection.execute(
                    "SELECT COALESCE(SUM(charged), 0), COALESCE(SUM(CASE WHEN charged IS NULL THEN reserved ELSE 0 END), 0), COUNT(*) FROM calls"
                ).fetchone()
            return {
                **metadata,
                "basis": "dated_list_price_estimate",
                "settledMicroUsd": settled,
                "uncertainReservedMicroUsd": uncertain,
                "calls": calls,
                "ceilingMicroUsd": self.settings.ci_budget_microusd,
            }
        except (ValueError, OSError, sqlite3.Error):
            return {**metadata, "basis": "unavailable"}

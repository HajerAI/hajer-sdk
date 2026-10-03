"""A paid send needs a reservation; missing usage and interrupted sends cannot free it."""

import ast
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import JsonValue

from hajer._ci_prices import BACKEND_PRICES, OUTPUT_LIMITS, PRICES
from hajer._ci_spend import Spend
from hajer._ci_usage import charge
from hajer._errors import SpendRefusedError
from hajer._settings import HajerSettings
from tests.repo import platform_path, requires_platform

MODEL = "claude-haiku-4-5-20251001"


def request(**changes: JsonValue) -> httpx.Request:
    return httpx.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        json={
            "model": MODEL,
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "synthetic test"}],
            **changes,
        },
    )


def meter(root: Path, ceiling: int = 500_000) -> Spend:
    return Spend(HajerSettings(ci_budget_file=str(root / "spend.sqlite3"), ci_budget_microusd=ceiling))


def reserve(spend: Spend, **changes: JsonValue) -> tuple[str, str, object]:
    sent = request(**changes)
    return spend.reserve(sent, sent.read())


def test_no_budget_means_no_paid_send() -> None:
    with pytest.raises(SpendRefusedError, match="CI_SPEND_NOT_CONFIGURED"):
        reserve(Spend(HajerSettings()))


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4o-mini-2024-07-18"])
def test_reviewed_mini_prices_bound_and_settle_a_text_request(tmp_path: Path, model: str) -> None:
    spend = meter(tmp_path)
    sent = httpx.Request("POST", "https://api.openai.com/v1/chat/completions", json={"model": model, "messages": []})
    reservation = spend.reserve(sent, sent.read())
    assert spend.snapshot()["uncertainReservedMicroUsd"] == 29_031
    spend.settle(reservation, 200, b'{"usage":{"prompt_tokens":100,"completion_tokens":10}}')
    assert spend.snapshot()["settledMicroUsd"] == 21
    assert spend.snapshot()["uncertainReservedMicroUsd"] == 0


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"model": "unknown-model"}, "CI_MODEL_UNPRICED"),
        ({"max_tokens": None}, "CI_OUTPUT_LIMIT_REQUIRED"),
        ({"max_tokens": 200_000}, "CI_OUTPUT_LIMIT_REQUIRED"),
        ({"inference_geo": "us"}, "CI_REQUEST_UNSUPPORTED"),
        ({"speed": "fast"}, "CI_REQUEST_UNSUPPORTED"),
        ({"service_tier": "priority"}, "CI_REQUEST_UNSUPPORTED"),
        ({"prompt": {"id": "stored-tools"}}, "CI_REQUEST_UNSUPPORTED"),
        ({"web_search_options": {"search_context_size": "low"}}, "CI_REQUEST_UNSUPPORTED"),
        ({"mcp_servers": [{"type": "url", "url": "https://must-not-contact.test"}]}, "CI_REQUEST_UNSUPPORTED"),
        ({"tools": [{"type": "web_search_20250305", "name": "web_search"}]}, "CI_SERVER_TOOL_UNSUPPORTED"),
    ],
)
def test_unsupported_cost_envelopes_are_refused_before_reservation(
    tmp_path: Path, changes: dict[str, JsonValue], reason: str
) -> None:
    spend = meter(tmp_path)
    with pytest.raises(SpendRefusedError, match=reason):
        reserve(spend, **changes)
    assert spend.snapshot()["calls"] == 0


def test_interrupted_send_keeps_reservation_across_instances(tmp_path: Path) -> None:
    spend = meter(tmp_path)
    reserve(spend)
    later = meter(tmp_path)
    with pytest.raises(SpendRefusedError, match="CI_BUDGET_EXHAUSTED"):
        reserve(later)
    assert later.snapshot() == {
        "version": 1,
        "source": "caller_reported",
        "scope": "ci_budget_ledger",
        "basis": "dated_list_price_estimate",
        "settledMicroUsd": 0,
        "uncertainReservedMicroUsd": 400_500,
        "calls": 1,
        "ceilingMicroUsd": 500_000,
    }


def test_parallel_children_cannot_reserve_the_same_remaining_money(tmp_path: Path) -> None:
    meter(tmp_path).snapshot()  # initialize outside concurrent schema creation

    def send(_: int) -> bool:
        try:
            reserve(meter(tmp_path))
            return True
        except SpendRefusedError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(send, range(2))) == [False, True]


def test_settlement_uses_cache_tiers_and_releases_only_unused_reserve(tmp_path: Path) -> None:
    spend = meter(tmp_path)
    sent = request()
    reserved = spend.reserve(sent, sent.read())
    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 200,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 100},
        "cache_read_input_tokens": 50,
    }
    spend.settle(reserved, 200, json.dumps({"usage": usage, "content": "MUST_NOT_BE_RETAINED"}).encode())
    assert spend.snapshot()["settledMicroUsd"] == 440
    assert spend.snapshot()["uncertainReservedMicroUsd"] == 0
    assert b"MUST_NOT_BE_RETAINED" not in (tmp_path / "spend.sqlite3").read_bytes()
    reserve(spend)


@pytest.mark.parametrize(("status", "body"), [(500, b"{}"), (200, b"{}"), (200, b"not json")])
def test_unknown_usage_keeps_money_reserved(tmp_path: Path, status: int, body: bytes) -> None:
    spend = meter(tmp_path)
    sent = request()
    reserved = spend.reserve(sent, sent.read())
    spend.settle(reserved, status, body)
    assert spend.snapshot()["uncertainReservedMicroUsd"] == 400_500
    assert spend.reason == "CI_USAGE_UNAVAILABLE"


def test_existing_budget_cannot_be_silently_reset_or_raised(tmp_path: Path) -> None:
    reserve(meter(tmp_path))
    with pytest.raises(SpendRefusedError, match="CI_SPEND_CEILING_CHANGED"):
        reserve(meter(tmp_path, 999_999))


def test_openai_cached_tokens_are_subtracted_from_uncached_input() -> None:
    amount, usage = charge(
        "openai",
        PRICES[("openai", "gpt-5.1")],
        json.dumps(
            {"usage": {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 500}, "completion_tokens": 20}}
        ).encode(),
    )
    assert usage == {"input": 500, "output": 20, "read": 500, "write5m": 0, "write1h": 0}
    assert amount == 888


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
def test_omitted_openai_cap_uses_evidenced_model_maximum_without_editing_request(tmp_path: Path, path: str) -> None:
    body = b'{ "model": "gpt-5.1", "messages": [{"role":"user","content":"synthetic test"}] }'
    sent = httpx.Request("POST", "https://api.openai.com" + path, content=body)
    spend = meter(tmp_path, 2_000_000)
    spend.reserve(sent, sent.read())
    assert sent.read() == body
    assert spend.snapshot()["uncertainReservedMicroUsd"] == 1_780_000
    with sqlite3.connect(tmp_path / "spend.sqlite3") as connection:
        evidence = json.loads(connection.execute("SELECT evidence FROM calls").fetchone()[0])
    assert evidence["outputLimit"] == {
        "tokens": 128_000,
        "basis": "published_model_maximum",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.1",
        "date": "2026-09-29",
    }
    assert "synthetic test" not in json.dumps(evidence)


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
@pytest.mark.parametrize("invalid", [None, 0, -1, True, "100", 400_000])
def test_published_maximum_never_overrides_an_explicit_invalid_cap(
    tmp_path: Path, key: str, invalid: JsonValue
) -> None:
    sent = httpx.Request("POST", "https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.1", key: invalid})
    spend = meter(tmp_path, 2_000_000)
    with pytest.raises(SpendRefusedError, match="CI_OUTPUT_LIMIT_REQUIRED"):
        spend.reserve(sent, sent.read())
    assert spend.snapshot()["calls"] == 0


def test_omitted_cap_without_a_verified_model_maximum_still_refuses(tmp_path: Path) -> None:
    sent = httpx.Request("POST", "https://api.anthropic.com/v1/messages", json={"model": MODEL})
    spend = meter(tmp_path)
    with pytest.raises(SpendRefusedError, match="CI_OUTPUT_LIMIT_REQUIRED"):
        spend.reserve(sent, sent.read())
    assert spend.snapshot()["calls"] == 0


def test_explicit_cap_keeps_its_smaller_reservation(tmp_path: Path) -> None:
    sent = httpx.Request(
        "POST", "https://api.openai.com/v1/chat/completions", json={"model": "gpt-5.1", "max_completion_tokens": 100}
    )
    spend = meter(tmp_path, 501_000)
    spend.reserve(sent, sent.read())
    assert spend.snapshot()["uncertainReservedMicroUsd"] == 501_000


def test_published_maxima_are_bounded_by_their_priced_contexts() -> None:
    for identity, limit in OUTPUT_LIMITS.items():
        assert 0 < limit.tokens < PRICES[identity].context


def test_stream_usage_is_combined_without_storing_stream_content() -> None:
    raw = b'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n\ndata: {"type":"message_delta","usage":{"output_tokens":20}}\n\ndata: {"type":"message_stop"}\n\n'
    assert charge("anthropic", PRICES[("anthropic", MODEL)], raw)[0] == 110


@pytest.mark.parametrize(
    "raw",
    [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}\n\n',
        b'{"usage":{"input_tokens":10,"output_tokens":20,"cache_creation_input_tokens":100}}',
    ],
)
def test_partial_stream_or_unknown_cache_tier_has_no_fabricated_cost(raw: bytes) -> None:
    with pytest.raises(ValueError, match="CI_USAGE_UNAVAILABLE"):
        charge("anthropic", PRICES[("anthropic", MODEL)], raw)


@requires_platform
def test_sdk_price_projection_matches_canonical_backend_rows() -> None:
    path = platform_path("backend/app/engine/s2_model/budget/prices.py")
    tree = ast.parse(path.read_text())
    rows: dict[str, dict[str, int | str]] = {}
    routes: dict[tuple[str, str], str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.targets[0], ast.Name):
            values: dict[str, int | str] = {}
            for keyword in node.value.keywords:
                if (
                    keyword.arg
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, int | str)
                ):
                    values[keyword.arg] = keyword.value.value
                elif keyword.arg == "fetched_on" and isinstance(keyword.value, ast.Call):
                    date = [cast(int, ast.literal_eval(arg)) for arg in keyword.value.args]
                    values["fetched_on"] = f"{date[0]:04}-{date[1]:02}-{date[2]:02}"
            rows[node.targets[0].id] = values
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "PRICES":
            assert isinstance(node.value, ast.Dict)
            for key, value in zip(node.value.keys, node.value.values, strict=True):
                assert key is not None
                assert isinstance(value, ast.Name)
                routes[cast(tuple[str, str], ast.literal_eval(key))] = value.id
    assert set(BACKEND_PRICES) == set(routes)
    for identity, price in BACKEND_PRICES.items():
        row = rows[routes[identity]]
        assert (
            price.input,
            price.output,
            price.write_5m,
            price.write_1h,
            price.read,
            price.context,
            price.source,
            price.fetched,
        ) == tuple(
            row[key]
            for key in (
                "input_microusd_per_mtok",
                "output_microusd_per_mtok",
                "cache_write_5m_microusd_per_mtok",
                "cache_write_1h_microusd_per_mtok",
                "cache_read_microusd_per_mtok",
                "context_window_tokens",
                "source_url",
                "fetched_on",
            )
        )

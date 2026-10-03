"""Versioned customer rules protect denied fields even inside allowed parents."""

import pytest
from pydantic import TypeAdapter

from hajer import _redact
from hajer._json import JsonObject
from tests.repo import CONTRACT


def test_secret_field_vectors_match_the_backend_contract() -> None:
    path = CONTRACT / "secret-field-vectors.json"
    vectors = TypeAdapter(dict[str, list[str]]).validate_json(path.read_text())
    policy = _redact.load_rules({"version": 1})
    exposed = [
        key
        for key in vectors["secret"]
        if _redact.redact_document({key: "opaque"}, policy=policy)[0] != {key: "[redacted:FIELD]"}
    ]
    withheld = [
        key
        for key in vectors["ordinary"]
        if _redact.redact_document({key: "opaque"}, policy=policy)[0] != {key: "opaque"}
    ]
    assert (exposed, withheld) == ([], [])


def test_overlap_vectors_match_the_backend_contract() -> None:
    path = CONTRACT / "overlap-vectors.json"
    for vector in TypeAdapter(list[JsonObject]).validate_json(path.read_text()):
        spec = vector["spec"]
        assert isinstance(spec, dict)
        result, _ = _redact.redact_document(vector["input"], policy=_redact.load_rules(spec))
        assert result == vector["expected"]


@pytest.mark.parametrize("key", ["safe.private", "safe[0]", ""])
def test_literal_keys_cannot_impersonate_allowed_paths(key: str) -> None:
    result, _ = _redact.redact_document(
        {"safe": {}, key: "sensitive-unallowed"}, policy=_redact.load_rules({"version": 1, "allow_fields": ["safe"]})
    )
    assert result == {"safe": {}, key: "[redacted:FIELD]"}


def test_integral_float_card_matches_the_same_card_as_text() -> None:
    document: JsonObject = {"card": 4111111111111111.0, "text": "4111111111111111", "count": 4.5}
    policy = _redact.load_rules({"version": 1})
    result, _ = _redact.redact_document(document, policy=policy)
    assert result == {"card": "[redacted:CARD]", "text": "[redacted:CARD]", "count": 4.5}
    assert _redact.redact_document(document, policy=policy)[0] == result


def test_customer_spec_removes_known_values_patterns_and_denied_fields() -> None:
    spec: JsonObject = {
        "version": 1,
        "known_values": {"PERSON": ["Ada Lovelace"]},
        "patterns": [{"entity_type": "CUSTOMER_ID", "regex": "cust-[0-9]+"}],
        "allow_fields": ["messages[*]"],
        "deny_fields": ["messages[*].cvv"],
    }
    policy = _redact.load_rules(spec)
    result, _ = _redact.redact_document(
        {"messages": [{"text": "Ada Lovelace cust-42", "cvv": 123}], "internal": "private"}, policy=policy
    )
    assert result == {
        "messages": [{"text": "[redacted:PERSON] [redacted:CUSTOMER_ID]", "cvv": "[redacted:FIELD]"}],
        "internal": "[redacted:FIELD]",
    }


def test_allowlist_does_not_exempt_card_or_secret_scrubbing() -> None:
    result, _ = _redact.redact_document(
        {"text": "4111 1111 1111 1111 sk-ant-abcdefghijklmnop"},
        policy=_redact.load_rules({"version": 1, "allow_fields": ["text"]}),
    )
    assert "4111" not in str(result)
    assert "sk-ant" not in str(result)


def test_numeric_card_values_do_not_escape_the_export_scrub() -> None:
    result, _ = _redact.redact_document(
        {"card": 4111111111111111, "count": 4}, policy=_redact.load_rules({"version": 1})
    )
    assert result == {"card": "[redacted:CARD]", "count": 4}


def test_credentials_and_card_security_fields_are_always_denied() -> None:
    result, _ = _redact.redact_document(
        {"password": "very-private", "cvv": 123, "nested": {"api_key": "unrecognized-shape"}},
        policy=_redact.load_rules({"version": 1, "allow_fields": ["password", "cvv", "nested"]}),
    )
    assert result == {
        "password": "[redacted:FIELD]",
        "cvv": "[redacted:FIELD]",
        "nested": {"api_key": "[redacted:FIELD]"},
    }


@pytest.mark.parametrize("budget", ["REDACT_MAX_NODES", "REDACT_MAX_BYTES", "REDACT_MAX_DEPTH"])
def test_unscanned_values_are_withheld_when_a_budget_is_exhausted(budget: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_redact, budget, 1)
    result, report = _redact.redact_submission(
        {"request": {"raw": "private-unscanned-value"}, "output": {"raw": "private-unscanned-value"}},
        policy=_redact.load_rules({"version": 1}),
    )
    assert "private-unscanned-value" not in str(result)
    assert isinstance(result, dict)
    assert report is not None
    assert report.degraded is not None

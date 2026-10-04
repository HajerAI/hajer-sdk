"""Client-side redaction: on by default, bounded, never raising, and the same rules the server runs.

The values below are the same ones the Hajer platform's own detector corpus uses, because
the whole claim of `_rules.py` is that the two sides remove the same shapes.
"""

from __future__ import annotations

import json
import re
from typing import cast

import pytest

from hajer._json import JsonObject, JsonValue
from hajer._jsonpath import PathSyntaxError
from hajer._redact import (
    ClientRedactionPolicy,
    RedactionEntry,
    _walk,  # pyright: ignore[reportPrivateUsage] - the pass's own report, for its degradations
    build_policy,
    redact_document,
)
from hajer._rules import CATALOG_ID, PLACEHOLDERS, RULES
from hajer._settings import REDACT_MAX_DEPTH, REDACT_MAX_NODES, REDACT_MAX_STRING_CHARS, HajerSettings

#: The ambiguous nesting the platform's rule generator refuses at generation time, written again here so
#: this suite asserts the property of the **shipped** rule set without importing the generator. The generator's
#: own copy is asserted by its own tests, which plant a pattern and require
#: the lint to name it; this is the other half — that nothing ambiguous reached `_rules.py`.
_UNBOUNDED = r"(?:[+*]|\{\d+,\})"
_ATOM = r"(?:\((?:[^()]|\\.)*\)|\[(?:[^\]\\]|\\.)*\]|\\.|[^()\[\]])"
AMBIGUOUS_NESTING = re.compile(rf"\((?:\?:)?{_ATOM}{_UNBOUNDED}\??\)\s*{_UNBOUNDED}")


CARD = "4111 1111 1111 1111"
IBAN = "GB82 WEST 1234 5698 7654 32"
VIN = "1HGCM82633A004352"
US_SSN = "123-45-6789"
EMAIL = "dana.okafor@example.com"
KEY = "sk-ant-abcdefghijklmnop"


def categories(entries: tuple[RedactionEntry, ...]) -> set[str]:
    return {entry.category for entry in entries}


def test_client_redaction_is_on_by_default() -> None:
    """A card number in a message never leaves the process, with nothing configured: the default is on."""
    assert HajerSettings().redact_client is True
    assert HajerSettings.from_env({}).redact_client is True
    redacted, entries = redact_document({"note": f"pay {CARD} to {IBAN}"}, policy=build_policy())
    sent = json.dumps(redacted)
    assert "4111" not in sent
    assert "GB82" not in sent
    assert categories(entries) == {"CARD", "IBAN"}


def test_entries_are_always_a_tuple() -> None:
    """One return type, whatever happened. Never a list, never `None`."""
    policy = build_policy()
    for document in ({}, {"note": "nothing here"}, {"note": CARD}, [CARD], "a string", 7, None):
        redacted, entries = redact_document(cast("JsonValue", document), policy=policy)
        assert isinstance(entries, tuple)
        assert all(isinstance(entry, RedactionEntry) for entry in entries)
        del redacted


def test_rules_compile_once() -> None:
    """Every pattern is a compiled object in the generated module, not a string compiled per call.

    Compiling inside the walk would recompile seventeen patterns for every string value of every
    submission, inside somebody else's request path. The assertion is on the *identity* of the compiled
    object across two passes, which is the only thing that distinguishes "compiled once at import" from
    "compiled once per call and cached somewhere".
    """
    assert all(isinstance(rule.pattern, re.Pattern) for rule in RULES)
    first = [rule.pattern for rule in RULES]
    redact_document({"note": CARD}, policy=build_policy())
    assert [rule.pattern for rule in RULES] == first
    assert all(before is after for before, after in zip(first, [rule.pattern for rule in RULES], strict=True))


def test_unknown_validator_degrades_and_never_raises() -> None:
    """A rule naming a checksum this SDK version does not carry skips, records, and sets `degraded`.

    A newer catalog with an older client is the ordinary forward-compatibility case. The alternatives are
    removing a value without the checksum that admits it — a false positive on every long number — and
    raising inside a customer's request path.
    """
    rule = RULES[0]
    unknown = type(rule)(
        rule_id="future-rule",
        category="FUTURE",
        pattern=re.compile(r"\bFUTURE-\d+\b"),
        validator="SHA3_CHECKSUM_V9",
        placeholder="[redacted:FUTURE]",
        phase="PRE_PII",
        default_on=True,
    )
    policy = ClientRedactionPolicy(_compiled=(unknown,))

    redacted, state = _walk({"note": "FUTURE-12 and nothing else"}, policy)

    assert cast("JsonObject", redacted)["note"] == "FUTURE-12 and nothing else"
    assert state.degraded is not None
    assert "SHA3_CHECKSUM_V9" in state.degraded
    assert "future-rule" in state.degraded


def test_extra_rules_and_exempt_paths() -> None:
    """A team's own shape is removed; a path its checks read is left alone.

    The exemption is the answer to "a check that compares an account number cannot compare a
    placeholder": the caller names the paths its checks read, those values travel, and the service's own
    content contract is what protects them from there.
    """
    policy = build_policy(extra_rules=(("CUSTOMER_REF", r"\bCUS-[0-9]{6}\b"),))
    redacted, entries = redact_document({"note": "ref CUS-123456"}, policy=policy)
    assert cast("JsonObject", redacted)["note"] == "ref [redacted:CUSTOMER_REF]"
    assert categories(entries) == {"CUSTOMER_REF"}

    exempting = build_policy(paths_exempt=("request.account",))
    document: JsonValue = {"request": {"account": CARD, "note": f"and {CARD}"}}
    kept, found = redact_document(document, policy=exempting)
    assert cast("JsonObject", cast("JsonObject", kept)["request"])["account"] == CARD
    assert cast("JsonObject", cast("JsonObject", kept)["request"])["note"] == "and [redacted:CARD]"
    assert {entry.path for entry in found} == {"request.note"}

    with pytest.raises(PathSyntaxError):
        build_policy(paths_exempt=("a..b[",))
    with pytest.raises(re.error):
        build_policy(extra_rules=(("BROKEN", "([unclosed"),))


def test_budgets_degrade_instead_of_raising() -> None:
    """Past any budget the pass stops scanning and says why. It never raises and never lies.

    Three of the four budgets, each on its own document: one value too long (the value is replaced with
    the `UNSCANNED` placeholder, because a value redaction did not read is a value it will not send), more
    nodes than the walk will visit, and a whole document past the byte budget.
    """
    policy = build_policy()

    long_value = "x" * (REDACT_MAX_STRING_CHARS + 1)
    redacted, state = _walk({"note": long_value}, policy)
    assert cast("JsonObject", redacted)["note"] == "[redacted:UNSCANNED]"
    assert state.degraded is not None
    assert "REDACT_MAX_STRING_CHARS" in state.degraded

    wide: JsonObject = {f"k{index}": "x" for index in range(REDACT_MAX_NODES + 2)}
    _, wide_state = _walk(wide, policy)
    assert wide_state.degraded is not None
    assert "REDACT_MAX_NODES" in wide_state.degraded

    huge: JsonObject = {f"k{index}": "y" * 4096 for index in range(300)}
    _, huge_state = _walk(huge, policy)
    assert huge_state.degraded is not None
    assert "REDACT_MAX_BYTES" in huge_state.degraded


def test_deep_json_does_not_recurse() -> None:
    """A document nested far past Python's own recursion limit is walked, not crashed on.

    The walk is an explicit stack for exactly this reason: the depth of a customer's own document is the
    customer's business, and a `RecursionError` raised inside their request path would be ours.
    """
    document: JsonValue = CARD
    for _ in range(3_000):
        document = {"next": document}

    redacted, state = _walk(document, build_policy())

    assert state.degraded is not None
    assert "REDACT_MAX_DEPTH" in state.degraded
    # Everything above the depth bound was still rebuilt; nothing raised.
    assert isinstance(redacted, dict)
    assert REDACT_MAX_DEPTH < 3_000


def test_patterns_have_no_nested_quantifiers() -> None:
    """No shipped pattern has an unbounded-quantified group whose body is a lone unbounded atom.

    That is the shape one input can be split between the two quantifiers exponentially many ways, and this
    rule set runs inside a customer's request path. `(?:[ \\-]\\d{4,})+` (the catalog's account-like rule) is
    deliberately *not* that shape: its body begins with a mandatory separator from a class disjoint from the
    trailing `\\d`, so each repetition consumes a character no other repetition can claim.

    The generator refuses such a pattern at generation time and its own tests
    assert that it does; this asserts the consequence, on what actually shipped.
    """
    offenders = [rule.rule_id for rule in RULES if AMBIGUOUS_NESTING.search(rule.pattern.pattern)]

    assert offenders == []
    # And the detector itself is not vacuous: it names the classic shape.
    assert AMBIGUOUS_NESTING.search(r"(?:\d+)+") is not None
    assert AMBIGUOUS_NESTING.search(r"(a*)*") is not None
    assert AMBIGUOUS_NESTING.search(r"(?:[ \-]\d{4,})+") is None


def test_idempotent_and_placeholders_match_nothing() -> None:
    """`redact(redact(x)) == redact(x)`, and no rule matches any placeholder this rule set writes.

    A second pass over a document the caller already redacted must be a no-op: the wire can carry one, and
    a pass that re-redacted its own placeholders would inflate every count on it.
    """
    policy = build_policy()
    document: JsonValue = {"note": f"{CARD} {IBAN} {VIN} {US_SSN} {EMAIL} {KEY}"}
    once, first = redact_document(document, policy=policy)
    twice, second = redact_document(once, policy=policy)

    assert once == twice
    assert first
    assert not second
    for placeholder in PLACEHOLDERS:
        for rule in RULES:
            assert rule.pattern.search(placeholder) is None, f"{rule.rule_id} matches {placeholder}"


def test_the_structural_classes_reach_the_client() -> None:
    """Three structural classes reach the client with no code change in this package.

    This test was skip-marked before the catalog had them. Nothing in `hajer/` was
    edited to un-skip it: the catalog gained three rules, `generate_rules.py --write` ran, and the
    categories, the placeholders and the removal all followed from the generated module. That is the
    whole claim of generating `_rules.py` — a class the server learns about is a class the client has.
    """
    assert {"PERSON_NAME", "POSTAL_ADDRESS", "DATE_OF_BIRTH"} <= {rule.category for rule in RULES}
    assert CATALOG_ID == "redaction-rules@6"
    document: JsonValue = {
        "notes": "Dr. Alice Nakamura, 221B Baker Street, London NW1 6XE, date of birth: 12/03/1984",
    }
    redacted, entries = redact_document(document, policy=build_policy())
    rendered = json.dumps(redacted)
    for value in ("Alice Nakamura", "Baker Street", "12/03/1984"):
        assert value not in rendered, value
    assert {entry.category for entry in entries} == {"PERSON_NAME", "POSTAL_ADDRESS", "DATE_OF_BIRTH"}

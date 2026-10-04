"""Remove what must not travel, in the customer's own process, before anything is sent.

The client half of PII removal. The
service removes these shapes too — but it removes them *after* they had crossed the
network and been read into a request body. A card number in a support ticket was still a card number in
transit, in a log line, in a proxy's buffer. This module is the same rule set (`_rules.py`, generated from
the platform's redaction catalog) applied one hop earlier, so the value does not leave the process at all.

**It never raises into application code.** That is the SDK's whole contract: `verify`
returns `unavailable`, and a redaction pass that threw would turn Hajer into the reason somebody's email
did not send. So every failure here **degrades and records**: an unknown validator, a malformed rule, a
document past one of the budgets — the pass does what it can, sets `degraded` to the sentence that says
why, and the wire carries it to the assessment's `limitations`. A caller who believes their payload was
redacted and whose pass degraded is told.

**The walk is iterative, under four budgets.** A recursive walk over a customer's own document is a
recursion limit somebody else's process hits, and this runs inside their request path: `REDACT_MAX_DEPTH`
bounds nesting, `REDACT_MAX_NODES` bounds the number of values visited, `REDACT_MAX_BYTES` bounds the whole
document and `REDACT_MAX_STRING_CHARS` bounds one value. Past any of them the pass stops scanning and says
so; it never silently returns a document it did not finish reading.

The byte budget is **accumulated during the walk** rather than measured with `json.dumps` first, and the
reason is the same one the walk is iterative for: `json.dumps` recurses, so measuring a deeply nested
document would raise the very `RecursionError` this module exists not to raise. The accumulation counts
each key and each scalar leaf plus a fixed per-node overhead for the punctuation around it, which is an
estimate — a close one, and one that can only over-count, so the budget binds a little early rather than a
little late.

**The entries are always a tuple, and the return type is always the same pair.** One shape, `tuple[
JsonValue, tuple[RedactionEntry, ...]]`, whatever happened: a caller that had to check whether it got a
list or a tuple back would be a caller with two code paths over one result.

**Hajer's own replay-marker ids are not scanned** (`_replay_marker.py`): a string at exactly
`evidence.hajerReplay.<runId | planId | caseId>` that whole matches the grammar of the digest Hajer mints there is
kept as it is. Anything else under `hajerReplay` is scanned like any value.

**Placeholders match nothing.** `redact(redact(x)) == redact(x)`, asserted by the tests, because the wire
can carry a document the caller already passed through this pass and a second pass that re-redacted its own
placeholders would inflate every count.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

from hajer._checksums import VALIDATORS, Validator
from hajer._json import JsonObject, JsonValue
from hajer._jsonpath import PathSyntaxError, matched, parse_paths
from hajer._replay_marker import minted_marker_id
from hajer._rules import RULES, Rule
from hajer._settings import (
    REDACT_MAX_BYTES,
    REDACT_MAX_DEPTH,
    REDACT_MAX_NODES,
    REDACT_MAX_STRING_CHARS,
)

#: What replaces a value the pass could not scan at all. The one entry that means *we did not read this*
#: rather than *we found one*, spelled exactly as the server spells it.
UNSCANNED_PLACEHOLDER: Final[str] = "[redacted:UNSCANNED]"
UNSCANNED_CATEGORY: Final[str] = "UNSCANNED"
#: Field names whose value is withheld whatever it looks like: normalised to lowercase letters and digits, then
#: an exact name or a suffix, so `client_secret`, `x-api-key` and `session_token` are withheld and `max_tokens` is
#: not. The Hajer platform declares the same two tuples (its tests compare them) and both
#: sides run `contract/secret-field-vectors.json`.
_SECRET_FIELD_NAMES = ("pin", "pincode", "cvv", "cvv2", "cvc", "cvc2", "otp", "pwd")
_SECRET_FIELD_SUFFIXES = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "privatekey",
    "secretkey",
    "accesskey",
    "signingkey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
)


def _secret_field(key: str) -> bool:
    name = re.sub(r"[^a-z0-9]", "", key.lower())
    return name in _SECRET_FIELD_NAMES or name.endswith(_SECRET_FIELD_SUFFIXES)


@dataclass(frozen=True, slots=True)
class RedactionEntry:
    """One value that was changed: where it was, what class was found, and how many times.

    `category` — never `klass` (accepted DX row): the wire key is `class`, the attribute is the word for
    it in English, and a field named after a Python keyword it is not is a field a reader has to decode.
    """

    category: str
    path: str
    count: int


@dataclass(frozen=True, slots=True)
class ClientRedactionPolicy:
    """What this process removes, and what it leaves alone.

    * `classes_off` — a class this team has decided about. A team whose order ids are written in groups of
      four turns `ACCOUNT_LIKE` off, exactly as they would on the server.
    * `extra_rules` — a team's own shapes: an internal customer id, a licence number. `(category,
      pattern)` pairs, compiled once when the policy is built, and a pattern that does not compile is
      refused **here**, where it is written, rather than degrading every later call.
    * `paths_exempt` — paths the pass must not touch. This is how a verifier's *decisive* fields stay
      readable: a check that compares an account number cannot compare a placeholder, so the caller names
      the paths its checks read and those values travel (the service's own content contract is what
      protects them from there). Same path grammar as everything else (`_jsonpath.py`).
    """

    classes_off: frozenset[str] = frozenset()
    extra_rules: tuple[tuple[str, str], ...] = ()
    paths_exempt: tuple[str, ...] = ()
    allow_fields: tuple[str, ...] = ()
    deny_fields: tuple[str, ...] = ()
    _compiled: tuple[Rule, ...] = field(default=(), repr=False, compare=False)

    def rules(self) -> tuple[Rule, ...]:
        """Every rule this policy runs, in order: the catalog's, then the team's own."""
        catalog = tuple(rule for rule in RULES if rule.category not in self.classes_off and rule.default_on)
        return catalog + self._compiled


class _PatternSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entity_type: str
    regex: str


class _RulesSpec(BaseModel):
    """JSON-compatible v1 contract; the same fields as the platform's redaction rules."""

    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    known_values: dict[str, list[str]] = {}
    patterns: list[_PatternSpec] = []
    allow_fields: list[str] = []
    deny_fields: list[str] = []


def load_rules(document: JsonObject) -> ClientRedactionPolicy:
    """Validate configuration at setup, never during export; invalid privacy rules must be visible."""
    spec = _RulesSpec.model_validate(document)
    extra = [(item.entity_type, item.regex) for item in spec.patterns]
    extra.extend((kind, re.escape(value)) for kind, values in spec.known_values.items() for value in values)
    # A setup error is deliberate: an empty match would rewrite every position in every value.
    if any(not kind or re.compile(pattern).search("") is not None for kind, pattern in extra):
        raise ValueError("redaction rules require a type and a nonempty match")
    built = build_policy(extra_rules=extra)
    parse_paths(spec.allow_fields)
    parse_paths(spec.deny_fields)
    return replace(
        built,
        allow_fields=tuple(spec.allow_fields),
        deny_fields=tuple(spec.deny_fields),
    )


def build_policy(
    *,
    classes_off: frozenset[str] | Sequence[str] = (),
    extra_rules: Sequence[tuple[str, str]] = (),
    paths_exempt: Sequence[str] = (),
) -> ClientRedactionPolicy:
    """One policy with its extra patterns compiled and its exempt paths parsed, or a refusal.

    Called once, where the policy is written — a constructor rather than the dataclass because the two
    things that can be wrong with a policy (a pattern that does not compile, a path the grammar cannot
    read) are the caller's mistakes and belong at the point they are made. Inside `verify` there is no
    good answer to either.
    """
    compiled: list[Rule] = []
    for category, pattern in extra_rules:
        compiled.append(
            Rule(
                rule_id=f"extra:{category}",
                category=category,
                pattern=re.compile(pattern),
                validator="NONE",
                placeholder=f"[redacted:{category}]",
                phase="EXTRA",
                default_on=True,
            )
        )
    parse_paths(paths_exempt)  # refused here, where the path is written
    return ClientRedactionPolicy(
        classes_off=frozenset(classes_off),
        extra_rules=tuple(extra_rules),
        paths_exempt=tuple(paths_exempt),
        _compiled=tuple(compiled),
    )


#: What one node of structure costs in the estimate, for the quotes, colon, comma and braces around it.
_NODE_OVERHEAD: Final[int] = 4
_Frame: TypeAlias = tuple[dict[str, JsonValue] | list[JsonValue], str | int, JsonValue, str, int]


@dataclass(slots=True)
class _Pass:
    """One walk's running state: what it found, what it could not do, and how much it has spent."""

    entries: list[RedactionEntry] = field(default_factory=list)
    degraded: str | None = None
    nodes: int = 0
    size: int = 0

    def spend(self, value: JsonValue, key: str | int) -> bool:
        """Charge one node against the byte budget. `True` when the budget is now spent."""
        self.size += _NODE_OVERHEAD + (len(key.encode("utf-8")) if isinstance(key, str) else 0)
        if isinstance(value, str):
            self.size += len(value.encode("utf-8"))
        elif value is not None and not isinstance(value, (dict, list)):
            self.size += len(str(value))
        if self.size > REDACT_MAX_BYTES:
            self.degrade(
                f"the document is over REDACT_MAX_BYTES ({REDACT_MAX_BYTES}) by the time {_shown(key)} was "
                "reached; the rest was not scanned"
            )
            return True
        return False

    def degrade(self, reason: str) -> None:
        """Record the first reason the pass could not complete. The first, because it is the cause."""
        if self.degraded is None:
            self.degraded = reason


def _validator(rule: Rule, state: _Pass) -> Validator | None:
    """The checksum this rule names, or `None` when the client's copy does not have it.

    A rule naming a validator this SDK version does not carry is exactly the forward-compatibility case:
    a newer catalog, an older client. Skipping the rule and recording it is the only honest answer — the
    alternatives are removing a value without the checksum that admits it (a false positive on every long
    number) and raising inside somebody's request path.
    """
    found = VALIDATORS.get(rule.validator)
    if found is None:
        state.degrade(
            f"rule {rule.rule_id} names validator {rule.validator}, which this SDK version does not carry; "
            "the rule was skipped"
        )
    return found


def _redact_text(text: str, path: str, rules: Sequence[Rule], state: _Pass) -> str:
    """Detect on the original text, union overlaps, then replace without exposing unmatched suffixes."""
    if len(text) > REDACT_MAX_STRING_CHARS:
        state.degrade(f"a value at {path} is {len(text)} characters, over REDACT_MAX_STRING_CHARS; it was not scanned")
        state.entries.append(RedactionEntry(category=UNSCANNED_CATEGORY, path=path, count=1))
        return UNSCANNED_PLACEHOLDER
    matches: list[tuple[int, int, int]] = []
    for index, rule in enumerate(rules):
        admits = _validator(rule, state)
        if admits is None:
            continue
        matches.extend(
            (match.start(), match.end(), index) for match in rule.pattern.finditer(text) if admits(match.group(0))
        )
    merged: list[tuple[int, int, int]] = []
    for start, end, index in sorted(matches):
        if merged and start < merged[-1][1]:
            previous_start, previous_end, previous_index = merged.pop()
            start, end, index = previous_start, max(end, previous_end), min(index, previous_index)
        merged.append((start, end, index))
    counts: dict[str, int] = {}
    for start, end, index in reversed(merged):
        rule = rules[index]
        text = text[:start] + rule.placeholder + text[end:]
        counts[rule.category] = counts.get(rule.category, 0) + 1
    state.entries.extend(
        RedactionEntry(category=category, path=path, count=count) for category, count in counts.items()
    )
    return text


def _step(path: str, key: str | int) -> str:
    return f"{path}[{key}]" if isinstance(key, int) else (key if not path else f"{path}.{key}")


def redact_document(
    document: JsonValue, *, policy: ClientRedactionPolicy
) -> tuple[JsonValue, tuple[RedactionEntry, ...]]:
    """Rebuild this document with every recognised value replaced. Never raises; degrades and records.

    One return type, always the same pair (accepted DX row): a caller that had to check whether it got a
    list or a tuple back would be a caller with two code paths over one result.
    """
    redacted, state = _walk(document, policy)
    return redacted, tuple(state.entries)


def _walk(document: JsonValue, policy: ClientRedactionPolicy) -> tuple[JsonValue, _Pass]:
    """The pass itself: the rebuilt document and everything the pass recorded about doing it.

    The walk is an explicit stack of `(parent container, key, value, path, depth)` frames rather than a
    recursive call, because the depth of a customer's own document is the customer's business and a
    `RecursionError` inside their request path would be ours.
    """
    state = _Pass()
    exempt = _exempt_paths(document, policy, state)
    allowed = matched(document, parse_paths(policy.allow_fields))
    denied = matched(document, parse_paths(policy.deny_fields))
    rules = policy.rules()
    root: JsonObject = {"": document}
    stack: list[_Frame] = [(root, "", document, "", 0)]
    while stack:
        container, key, value, path, depth = stack.pop()
        secret_field = isinstance(key, str) and _secret_field(key)
        ambiguous_key = isinstance(key, str) and depth > 0 and (not key or any(mark in key for mark in ".[]"))
        if (
            ambiguous_key
            or secret_field
            or path in denied
            or (policy.allow_fields and path and not _capture_path(path, allowed))
        ):
            _assign(container, key, "[redacted:FIELD]")
            state.entries.append(RedactionEntry(category="FIELD", path=path, count=1))
            continue
        state.nodes += 1
        if state.nodes > REDACT_MAX_NODES:
            state.degrade(
                f"the document has more than REDACT_MAX_NODES ({REDACT_MAX_NODES}) values; the rest was not scanned"
            )
            _withhold_pending(stack, (container, key, value, path, depth), state)
            break
        if depth > REDACT_MAX_DEPTH:
            state.degrade(
                f"a value at {path} nests deeper than REDACT_MAX_DEPTH ({REDACT_MAX_DEPTH}); it was not scanned"
            )
            _assign(container, key, UNSCANNED_PLACEHOLDER)
            state.entries.append(RedactionEntry(category=UNSCANNED_CATEGORY, path=path, count=1))
            continue
        if path in exempt:
            continue
        if state.spend(value, key):
            _withhold_pending(stack, (container, key, value, path, depth), state)
            break
        if isinstance(value, str):
            if not minted_marker_id(path, value):
                _assign(container, key, _redact_text(value, path, rules, state))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = _numeric_text(value)
            if numeric is None:
                state.degrade("a non-finite numeric value cannot be scanned")
                state.entries.append(RedactionEntry(category=UNSCANNED_CATEGORY, path=path, count=1))
                _assign(container, key, UNSCANNED_PLACEHOLDER)
                continue
            redacted_number = _redact_text(numeric, path, rules, state)
            if redacted_number != numeric:
                _assign(container, key, redacted_number)
        elif isinstance(value, dict):
            rebuilt: dict[str, JsonValue] = dict(value)
            _assign(container, key, rebuilt)
            stack.extend((rebuilt, name, item, _step(path, name), depth + 1) for name, item in value.items())
        elif isinstance(value, list):
            kept: list[JsonValue] = list(value)
            _assign(container, key, kept)
            stack.extend((kept, index, item, _step(path, index), depth + 1) for index, item in enumerate(value))
    return root[""], state


def _numeric_text(value: int | float) -> str | None:
    """Use a stable decimal representation so floats cannot hide identifiers behind .0 or exponents."""
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return None
    return str(int(value)) if value.is_integer() else format(Decimal(str(value)), "f")


def _withhold_pending(stack: list[_Frame], current: _Frame, state: _Pass) -> None:
    """Unvisited values cannot cross the export boundary; preserve the submission's root shape."""
    stack.append(current)
    for container, key, value, path, _depth in stack:
        replacement: JsonValue = UNSCANNED_PLACEHOLDER
        if not path and isinstance(value, dict):
            replacement = dict.fromkeys(value, UNSCANNED_PLACEHOLDER)
        elif not path and isinstance(value, list):
            replacement = [UNSCANNED_PLACEHOLDER for _ in value]
        _assign(container, key, replacement)
        state.entries.append(RedactionEntry(category=UNSCANNED_CATEGORY, path=path, count=1))
    stack.clear()


def _capture_path(path: str, allowed: frozenset[str]) -> bool:
    """Traverse ancestors and capture descendants of allowed paths; siblings stay private."""
    return any(
        path == hit or path.startswith((hit + ".", hit + "[")) or hit.startswith((path + ".", path + "["))
        for hit in allowed
    )


def _assign(container: dict[str, JsonValue] | list[JsonValue], key: str | int, value: JsonValue) -> None:
    if isinstance(container, list) and isinstance(key, int):
        container[key] = value
    elif isinstance(container, dict) and isinstance(key, str):
        container[key] = value


def _shown(key: str | int) -> str:
    """One key, for a degradation sentence. A key is a field name, never a value."""
    return f"index {key}" if isinstance(key, int) else (f"{key!r}" if key else "the root")


def _exempt_paths(document: JsonValue, policy: ClientRedactionPolicy, state: _Pass) -> frozenset[str]:
    """The concrete paths this policy exempts, or none plus a degradation when one cannot be read."""
    if not policy.paths_exempt:
        return frozenset()
    try:
        return matched(document, parse_paths(policy.paths_exempt))
    except PathSyntaxError as unreadable:  # pragma: no cover - `build_policy` refuses these first
        state.degrade(f"an exempt path could not be read ({unreadable}); nothing was exempted")
        return frozenset()

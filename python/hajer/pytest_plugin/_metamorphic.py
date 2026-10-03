"""Metamorphic relations in customer CI, live only, with the platform's input edits.

An INV relation runs the case's input edited in a meaning-preserving way (`WHITESPACE`: every space doubled and a
trailing line break; `CASING`: lower case; `PARAPHRASE`: a fixed polite frame; `TONE`: a fixed angry frame) and
passes when the base check gives the edited reply the verdict it gave the original — or, with `keep`, when the named
output field is unchanged. A DIR relation appends the condition's trigger (`APPEND`) and passes when the base check
holds on the edited reply. The edits are the platform's, pinned by the shared
vectors' `metamorphicEdits`; a replay has no second recorded answer, so it reports METAMORPHIC_NEEDS_LIVE.
"""

from typing import Final, cast

from pydantic import JsonValue

from hajer.pytest_plugin._models import Verdict
from hajer.pytest_plugin._values import Missing, equal, lookup

PARAPHRASE_PREFIX: Final = "Hello, I need some help with the following. "
PARAPHRASE_SUFFIX: Final = " Thank you."
TONE_PREFIX: Final = "I am extremely angry and fed up, fix this now!!! "


def edited_text(text: str, edit: dict[str, JsonValue]) -> str:
    kind = edit.get("kind")
    if kind == "WHITESPACE":
        return text.replace(" ", "  ") + "\n"
    if kind == "CASING":
        return text.lower()
    if kind == "PARAPHRASE":
        return PARAPHRASE_PREFIX + text + PARAPHRASE_SUFFIX
    if kind == "TONE":
        return TONE_PREFIX + text
    extra = edit.get("text")
    return f"{text} {extra if isinstance(extra, str) else ''}".rstrip()


def edited_input(document: JsonValue, edit: dict[str, JsonValue]) -> JsonValue | None:
    """The input with the text at `edit.field` edited; None when no text sits there."""
    field = edit.get("field")
    path = cast(list[str], field) if isinstance(field, list) else []
    if not path:
        return edited_text(document, edit) if isinstance(document, str) else None
    if not isinstance(document, dict) or path[0] not in document:
        return None
    inner = edited_input(document[path[0]], {**edit, "field": cast(JsonValue, path[1:])})
    return None if inner is None else {**document, path[0]: inner}


def relation_verdict(
    relation: dict[str, JsonValue],
    base: tuple[Verdict, JsonValue],
    edited: tuple[Verdict, JsonValue],
) -> Verdict:
    """One attempt's verdict: the base check's (verdict, output) on the original input and on the edited one."""
    keep = relation.get("keep")
    if isinstance(keep, list):
        path = cast(list[str], keep)
        left = lookup({"output": base[1]}, path)
        right = lookup({"output": edited[1]}, path)
        if isinstance(left, Missing) or isinstance(right, Missing):
            return "UNABLE_TO_VERIFY"
        return "PASS" if equal(left, right) else "FAIL"
    if relation.get("relation") == "DIR":
        return edited[0]
    if "UNABLE_TO_VERIFY" in (base[0], edited[0]):
        return "UNABLE_TO_VERIFY"
    return "PASS" if base[0] == edited[0] else "FAIL"

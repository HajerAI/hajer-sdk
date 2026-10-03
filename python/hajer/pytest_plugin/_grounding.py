"""`grounded` and `excludes` in customer CI: the plugin's copy of the platform's grounding predicates.

The rules and their names are the platform's; the shared vectors hold both copies to the same answers.
"""

import json
import math
import re
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Final, cast

from pydantic import JsonValue

from hajer.pytest_plugin._values import Missing, lookup

_URL: Final = re.compile(r"https?://[^\s<>\"'()\[\]]+")
_EMAIL: Final = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MONTH_NAMES: Final = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTHS: Final[dict[str, int]] = {
    **{name: index for index, name in enumerate(_MONTH_NAMES, start=1)},
    **{name[:3]: index for index, name in enumerate(_MONTH_NAMES, start=1)},
}
_MONTH: Final = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ISO: Final = re.compile(r"(?<![\w-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\w-])")
_SLASH: Final = re.compile(r"(?<![\w/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\w/])")
_MONTH_DAY: Final = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}}\b)?", re.IGNORECASE)
_DAY_MONTH: Final = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})\b\.?(?:\s+\d{{4}}\b)?", re.IGNORECASE)
_MARKER: Final = re.compile(r"(?m)^\s*\d+[.)](?=\s)")
_NUMBER: Final = re.compile(r"(?<![\w.,])\d[\d,]*(?:\.\d+)?(?![\w]|\.\d|,\d)")
_WORDS: Final[dict[str, int]] = {
    word: value
    for value, word in enumerate(
        (
            "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
            "seventeen eighteen nineteen twenty"
        ).split()
    )
}
_WORD: Final = re.compile(r"[A-Za-z]+")
#: How many distinct source numbers DERIVED_ARITHMETIC pairs, a rule of the language (as `in`'s member cap is).
_PAIRED: Final = 64
_UNSPACED: Final = re.compile(
    "[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]"
)


def finite(value: JsonValue) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def texts(value: JsonValue, *, numbers: bool = False) -> list[str]:
    """Every string inside the value (object values in key order), and its numbers too when asked."""
    if isinstance(value, str):
        return [value]
    if numbers and finite(value):
        return [json.dumps(value)]
    if isinstance(value, dict):
        return [text for key in sorted(value) for text in texts(value[key], numbers=numbers)]
    if isinstance(value, list):
        return [text for item in value for text in texts(item, numbers=numbers)]
    return []


def _number(token: str) -> Decimal | None:
    try:
        found = Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None
    return found.normalize() if found.is_finite() else None


def _dates(text: str) -> tuple[set[tuple[int, int]], str]:
    """The month-day pairs a text writes, and the text without them."""
    found: set[tuple[int, int]] = set()

    def keep(month: int, day: int) -> str:
        if 1 <= month <= 12 and 1 <= day <= 31:
            found.add((month, day))
        return " "

    text = _ISO.sub(lambda match: keep(int(match.group(2)), int(match.group(3))), text)
    text = _SLASH.sub(lambda match: keep(int(match.group(1)), int(match.group(2))), text)
    text = _MONTH_DAY.sub(lambda match: keep(_MONTHS[match.group(1).casefold()], int(match.group(2))), text)
    text = _DAY_MONTH.sub(lambda match: keep(_MONTHS[match.group(2).casefold()], int(match.group(1))), text)
    return found, text


def _read(text: str) -> tuple[set[str], set[tuple[int, int]], list[Decimal]]:
    """A text's whole URLs and e-mails (case-folded), its dates and its numbers, each read once."""
    keyed = {match.group().rstrip(".,;:!?").casefold() for match in _URL.finditer(text)}
    text = _URL.sub(" ", text)
    keyed |= {match.group().rstrip(".").casefold() for match in _EMAIL.finditer(text)}
    text = _EMAIL.sub(" ", text)
    dates, text = _dates(text)
    text = _MARKER.sub(" ", text)
    numbers = [found for match in _NUMBER.finditer(text) if (found := _number(match.group().rstrip(","))) is not None]
    return keyed, dates, numbers


def _derived(claim: Decimal, sources: list[Decimal]) -> bool:
    pairs = combinations(sorted(set(sources))[:_PAIRED], 2)
    return any(claim in {left + right, left - right, right - left, left * right} for left, right in pairs)


def ungrounded(subject: JsonValue, document: JsonValue, parameters: dict[str, JsonValue]) -> list[str]:
    """The subject's claims that no named source states, by rule (URL/EMAIL, DATE, NUMBER)."""
    parts: list[str] = []
    for path in cast(list[list[str]], parameters.get("sources", [])):
        value = lookup(document, list(path))
        if not isinstance(value, Missing):
            parts.extend(texts(value, numbers=True))
    parts.extend(cast(list[str], parameters.get("allow", [])))
    keyed, dates, numbers = _read("\n".join(parts))
    numbers += [
        Decimal(_WORDS[word.casefold()]) for part in parts for word in _WORD.findall(part) if word.casefold() in _WORDS
    ]
    found: list[str] = []
    for text in texts(subject, numbers=True):
        claimed, claimed_dates, claimed_numbers = _read(text)
        found += sorted(claimed - keyed)
        found += [f"{month:02d}-{day:02d}" for month, day in sorted(claimed_dates - dates)]
        found += [
            format(number, "f") for number in claimed_numbers if number not in numbers and not _derived(number, numbers)
        ]
    return found


def _pattern(term: str) -> str:
    """A term as a whole word; a term in a script written without spaces (CJK, Thai, Lao, Myanmar, Khmer, kana and
    halfwidth kana) matches anywhere, since such text has no word boundaries to hold it to."""
    folded = re.escape(term.casefold())
    return folded if _UNSPACED.search(term) else rf"(?<!\w){folded}(?!\w)"


def forbidden(subject: JsonValue, terms: list[str]) -> list[str]:
    """The listed terms the subject's text holds, case-folded both ways (review N3): whole words, except in a script
    written without spaces."""
    text = "\n".join(texts(subject)).casefold()
    return [term for term in terms if re.search(_pattern(term), text)]

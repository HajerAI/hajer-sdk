"""GENERATED from the Hajer platform's checksum module. Never hand-edited.

A shape says *could be*; the checksum says *is*. These are copied from the platform's own redaction
checksums (the generator runs in the platform repository) so that the client removes exactly
what the server would: a client Luhn that disagreed with the server's would report a `clientRedaction`
count that does not describe what it sent.

They never see a document. Each is handed the text one pattern matched and answers true or false; none
allocates anything that survives the call, and none raises — a value that cannot be read as the shape
claims is `False`, because a detector that crashed on a malformed candidate would break a customer's own
request path over a number in a comment.
"""

from collections.abc import Callable
from typing import Final

#: What one validator is: the matched text in, "this really is one of those" out.
Validator = Callable[[str], bool]

#: A digest over the backend module and this generator. `--check` compares it when the interpreter it runs
#: on cannot parse the backend's own syntax.
SOURCE_DIGEST: Final[str] = "sha256:dc25dd7ba176b3b5ab2d306cf922a3bcff19506cb51addc55835680642e11c59"

_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
_VIN_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"
_VIN_VALUES = (1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9)
_VIN_LETTER_VALUE = dict(zip(_VIN_LETTERS, _VIN_VALUES, strict=True))
_VIN_CHECK_POSITION = 8
_IBAN_REMAINDER = 1
_IBAN_ROTATION = 4
_IBAN_LETTER_OFFSET = 55
_CARD_DIGITS = range(13, 20)

def luhn(text: str) -> bool:
    """The Luhn sum over the digits of `text`, ignoring the separators between them.

    Separators are skipped rather than refused because the catalog's own patterns admit them: a card
    is written in groups of four as often as it is written whole, and the checksum is over the digits
    either way. An empty candidate is false — a checksum over nothing proves nothing.
    """
    digits = [int(character) for character in text if character.isdigit()]
    if not digits:
        return False
    doubled = (sum(divmod(digit * 2, 10)) for digit in digits[-2::-2])
    return (sum(digits[::-2]) + sum(doubled)) % 10 == 0
def luhn_prefix(text: str) -> bool:
    """A card written with more digits after it: the run begins with a 13 to 19 digit number that passes Luhn.

    What follows the card — its expiry, its CVV, a second card — is card data too, so the rule that names this
    validator removes the whole run; the checksum is what says the run begins with a card at all.
    """
    digits = "".join(character for character in text if character.isdigit())
    return any(luhn(digits[:length]) for length in _CARD_DIGITS if length <= len(digits))
def iban_mod97(text: str) -> bool:
    """ISO 13616: move the first four characters to the end, letters become numbers, mod 97 is 1.

    Spaces are removed first (the printed form is grouped in fours) and the letters are read in the
    IBAN alphabet where A is 10. A candidate with a character outside `[0-9A-Z]` leaves a non-numeric
    string and is false rather than an error, which is the shape a stray unicode letter arrives as.
    """
    packed = "".join(text.split()).upper()
    if len(packed) <= _IBAN_ROTATION:
        return False
    rotated = packed[_IBAN_ROTATION:] + packed[:_IBAN_ROTATION]
    numeric = "".join(
        str(ord(character) - _IBAN_LETTER_OFFSET) if character.isalpha() else character for character in rotated
    )
    return numeric.isdigit() and int(numeric) % 97 == _IBAN_REMAINDER
def vin_check(text: str) -> bool:
    """ISO 3779: the ninth character is the weighted sum of all seventeen, modulo eleven, with X for ten.

    The VIN alphabet has no I, O or Q — the pattern already excludes them, and a character outside the
    table is false here as well, so the two cannot disagree. A candidate of the wrong length is false:
    `zip(strict=True)` would raise, and this function does not raise.
    """
    if len(text) != len(_VIN_WEIGHTS):
        return False
    values: list[int] = []
    for character in text:
        found = int(character) if character.isdigit() else _VIN_LETTER_VALUE.get(character)
        if found is None:
            return False
        values.append(found)
    total = sum(value * weight for value, weight in zip(values, _VIN_WEIGHTS, strict=True))
    remainder = total % 11
    return text[_VIN_CHECK_POSITION] == ("X" if remainder == 10 else str(remainder))

def _none(text: str) -> bool:
    """The absence of a checksum: a rule whose shape is its whole argument admits every match.

    A function rather than a `None` in the table, so the walk has one shape to call and no branch on
    whether a rule has a validator at all.
    """
    return bool(text)


#: Every validator a generated rule may name. A rule naming anything else degrades the pass and records
#: it rather than raising — `_redact.py` is the one place that decides what a missing validator means.
VALIDATORS: Final[dict[str, Validator]] = {
    "NONE": _none,
    "LUHN": luhn,
    "LUHN_PREFIX": luhn_prefix,
    "IBAN_MOD97": iban_mod97,
    "VIN_CHECK": vin_check,
}

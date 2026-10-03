"""A closed vocabulary that a newer server may extend without raising in a customer's process.

Forward compatibility: the SDK reads an unknown `origin` or
`cost_source` literal as `UNKNOWN` **retaining the raw value**. Both of those vocabularies have already
grown — `origin` gained `BROKER` and `cost_source` gained `LOCAL_PRICED` — and the SDK in
a customer's request path is the last place that should discover a new member by raising.

**Why not `str`.** A plain string forces every reader to know the whole vocabulary, and a reader that
compares against members it knows will silently treat a new one as "none of the above" with no way to tell
that from a typo. `Vocabulary` says both things at once: `value` is the member this client understands or
`UNKNOWN`, and `raw` is exactly what the server sent, so a log line, a receipt or a support conversation
still has the real string in it.

**Why not `extra="ignore"` on the field.** The response models already ignore *unknown fields*; this is the
other half — a known field with an unknown *value* — and pydantic has no default for that but refusal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Final, TypeVar

from pydantic import BeforeValidator, PlainSerializer

#: What a member this client does not know reads as. Upper-case like every other non-status vocabulary in
#: this SDK, and deliberately a *member* rather than `None`: absent and unrecognised are different facts.
UNKNOWN: Final[str] = "UNKNOWN"

_Known = TypeVar("_Known", bound=str)


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """One closed-vocabulary value as this client read it, and as the server actually sent it.

    `value` is a member this SDK version knows, or `UNKNOWN`. `raw` is the server's own string, always —
    including when the two are the same, so a reader never has to branch on which field to log.
    """

    value: str
    raw: str

    def __str__(self) -> str:
        """The raw string, because that is what a log line or a support conversation needs."""
        return self.raw

    def __format__(self, specification: str) -> str:
        """`f"{row.mode:<7}"` formats the raw string, so a column of these lines up like the strings they were.

        Without this, a dataclass takes `object.__format__`, which refuses any specification at all — and the
        one place these values are printed is a tail whose columns are padded.
        """
        return format(self.raw, specification)

    @property
    def known(self) -> bool:
        """Did this SDK version recognise the member? `False` means the server is newer than this client."""
        return self.value != UNKNOWN


def read(value: object, known: Sequence[str]) -> Vocabulary:
    """One arriving value as a `Vocabulary`, keeping whatever arrived.

    A non-string value — a number, an object — reads as `UNKNOWN` with its `repr` as the raw: it is still not
    a member, and raising over it inside `verify` would be the SDK deciding that a malformed server answer is
    worth failing a customer's request for.

    A `Vocabulary` passes through unchanged, so `model_validate(model.model_dump())` is the identity.
    """
    if isinstance(value, Vocabulary):
        return value
    raw = value if isinstance(value, str) else repr(value)
    return Vocabulary(value=raw if raw in frozenset(known) else UNKNOWN, raw=raw)


def reading(known: Sequence[str]) -> BeforeValidator:
    """The validator half of `read`, for `Annotated[Vocabulary, reading(MEMBERS)]`."""
    return BeforeValidator(lambda value: read(value, known))


#: How one of these dumps: as the raw string the server sent, so `model_validate(model.model_dump())` is the
#: identity. Without it a dataclass dumps as an object, and the second read would find no member in it.
_AS_SENT: Final[PlainSerializer] = PlainSerializer(lambda value: value.raw, return_type=str)

#: What one `verify` or `observe` submission asked for.
MODES: Final[tuple[str, ...]] = ("VERIFY", "OBSERVE")

#: Where an observation's evidence came from, as contract v0 spells it. `BROKER` is the wire proxy's
#: (`_proxy.py`): the exchange was witnessed at the wire rather than reported by the application.
OBSERVATION_ORIGINS: Final[tuple[str, ...]] = (
    "TARGET_SELF_REPORT",
    "TRUSTED_COLLECTOR",
    "ADJUDICATED_CONTROL",
    "BROKER",
)

#: How a model-call receipt's money was arrived at. `LOCAL_PRICED` is the customer's own call,
#: priced by the service's table from the tokens the wrapper reported, and never a liability.
COST_SOURCES: Final[tuple[str, ...]] = ("PROVIDER", "LOCAL", "LOCAL_PRICED")

#: The one annotated alias a model field uses today. It reads an unknown member as `UNKNOWN` keeping the raw
#: string, and dumps as that raw string.
#:
#: `OBSERVATION_ORIGINS` and `COST_SOURCES` above have no field yet — the SDK's public models carry neither
#: `origin` nor `cost_source` — so their aliases are deliberately *not* declared here: an `Annotated` nothing
#: annotates is a seam with no caller, which is the flaw class this repository's guards are about. The
#: vocabularies themselves are declared because `read(value, MEMBERS)` is what a reader of a receipt or an
#: observation will call, and the test beside this file asserts both carry `BROKER` and `LOCAL_PRICED`.
Mode = Annotated[Vocabulary, reading(MODES), _AS_SENT]

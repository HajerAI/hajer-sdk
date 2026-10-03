"""One call, one record, when Hajer both records a call and receives an OpenTelemetry `gen_ai` span of it (`_otel`).

Hajer always records a call through its own wrap or HTTP capture; the receiver (`instrument(tracer_provider=…)`)
is for the calls it cannot wrap. When a span describes a call Hajer already recorded, the record wins and the span
is dropped — but only on evidence that it is that call:

- its `gen_ai.response.id` is the response id of a recorded call (`seen_response`);
- it began inside a call Hajer was recording, in that call's context (`OPEN_CALL`): a client-level instrumentor
  patches the provider class, under Hajer's instance-level patch, so its span begins and ends inside the call;
- it began just before one, and the call's answer agrees with it. A framework instrumentor begins its model span
  before the provider request and never makes it current (Traceloop 0.60 detaches it), so the first call Hajer
  records in the context where the span began, while it is open, is its candidate (`RECEIVED_SPAN`, taken once).
  When the span ends the candidate is checked: the same request model, and the same response id, usage or answer
  text. A disagreement keeps the span as a call of its own. Agreement that cannot be checked — nothing on one side
  to compare — keeps it too, noted `SPAN_MAY_DUPLICATE`: a distinct call is never dropped without a word.

Nothing here imports the rest of the SDK: calls are read by attribute.
"""

from __future__ import annotations

import collections
import threading
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final, Literal, cast

#: How many response ids, candidates and settled calls are remembered. A span ends right after its call settles,
#: so the newest few thousand are enough.
REMEMBERED: Final[int] = 4096

#: The span a receiver saw begin last in this context, until a call Hajer records takes it as its candidate.
RECEIVED_SPAN: ContextVar[int | None] = ContextVar("hajer_received_span", default=None)
#: The call Hajer is recording in this context right now (its provider method is running).
OPEN_CALL: ContextVar[object | None] = ContextVar("hajer_open_call", default=None)

#: Said on a call recorded from a span that may be a call Hajer also recorded.
SPAN_MAY_DUPLICATE: Final[str] = (
    "SPAN_MAY_DUPLICATE: this call was observed from a gen_ai span that began just before a call Hajer recorded, "
    "and neither carried a response id, usage or answer to tell whether they are one call. It is kept so that a "
    "distinct call is never lost, and may duplicate that record."
)

Verdict = Literal["drop", "keep", "flag"]


@dataclass(slots=True)
class Claimant:
    """The call a span may be. `call` is the record once it exists; a transport capture is built when its body
    ends."""

    model: str | None
    nested: bool
    call: object | None = None


@dataclass(slots=True)
class _Memory:
    responses: collections.OrderedDict[str, None] = field(default_factory=collections.OrderedDict)
    claims: collections.OrderedDict[int, Claimant] = field(default_factory=collections.OrderedDict)
    settled: collections.OrderedDict[int, None] = field(default_factory=collections.OrderedDict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_MEMORY = _Memory()


def _bounded(memory: collections.OrderedDict[str, None] | collections.OrderedDict[int, None]) -> None:
    if len(memory) > REMEMBERED:
        memory.popitem(last=False)


def remember(call: object) -> None:
    """A call Hajer recorded has settled: its response id and its answer are final."""
    response_id = getattr(call, "response_id", None)
    with _MEMORY.lock:
        if isinstance(response_id, str) and response_id:
            _MEMORY.responses[response_id] = None
            _bounded(_MEMORY.responses)
        _MEMORY.settled[id(call)] = None
        _bounded(_MEMORY.settled)


def seen_response(response_id: str) -> bool:
    """Whether a call with this provider response id was already recorded in this process."""
    with _MEMORY.lock:
        return response_id in _MEMORY.responses


def _store(span: int, claimant: Claimant) -> None:
    with _MEMORY.lock:
        _MEMORY.claims[span] = claimant
        if len(_MEMORY.claims) > REMEMBERED:
            _MEMORY.claims.popitem(last=False)


def claim(model: object, call: object | None = None) -> Claimant | None:
    """A call Hajer begins to record becomes the candidate of the span that began just before it in this context,
    if one is waiting — and that span waits for no other call."""
    span = RECEIVED_SPAN.get()
    if span is None:
        return None
    RECEIVED_SPAN.set(None)
    claimant = Claimant(model if isinstance(model, str) and model else None, nested=False, call=call)
    _store(span, claimant)
    return claimant


def span_began(span: int) -> None:
    """A received span began: inside a call Hajer is recording it is that call; otherwise it waits for one."""
    call = OPEN_CALL.get()
    if call is None:
        RECEIVED_SPAN.set(span)
        return
    model = getattr(call, "model", None)
    _store(span, Claimant(model if isinstance(model, str) and model else None, nested=True, call=call))


def span_ended(span: int) -> None:
    if RECEIVED_SPAN.get() == span:
        RECEIVED_SPAN.set(None)


def _mapping(value: object) -> Mapping[str, object]:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _items(value: object) -> Sequence[object]:
    return cast("Sequence[object]", value) if isinstance(value, list) else ()


def _response_id(call: object) -> str | None:
    value = getattr(call, "response_id", None)
    return value if isinstance(value, str) and value else None


def _usage(call: object) -> tuple[int, int] | None:
    usage = _mapping(getattr(call, "usage", None))
    given, taken = usage.get("input_tokens"), usage.get("output_tokens")
    return (given, taken) if isinstance(given, int) and isinstance(taken, int) else None


def _text(call: object) -> str | None:
    """The answer's text: a wrapped call's `output` or `streamedText`, a span's `outputMessages` text parts."""
    content = _mapping(getattr(call, "content", None))
    found: list[str] = []
    streamed = content.get("streamedText")
    if isinstance(streamed, str):
        found.append(streamed)
    found.extend(item for item in _items(content.get("output")) if isinstance(item, str))
    for message in _items(content.get("outputMessages")):
        for part in _items(_mapping(message).get("parts")):
            piece = _mapping(part)
            text = piece.get("content")
            if piece.get("type") == "text" and isinstance(text, str):
                found.append(text)
    return "".join(found).strip() or None


#: What a record and a span's call are compared on, most telling first.
_EVIDENCE: Final[tuple[Callable[[object], object], ...]] = (_response_id, _usage, _text)


def _agrees(recorded: object, observed: object) -> bool | None:
    """True or False when the record and the span's call can be compared on something they both carry."""
    with _MEMORY.lock:
        if id(recorded) not in _MEMORY.settled:
            return None  # its answer is not final yet
    for read in _EVIDENCE:
        mine, theirs = read(recorded), read(observed)
        if mine is not None and theirs is not None:
            return mine == theirs
    return None


def verdict(span: int, model: object, observed: object) -> Verdict:
    """What to do with the call a received span describes: `drop` it (Hajer recorded that call), `keep` it (a call
    of its own), or `flag` it (kept, noted `SPAN_MAY_DUPLICATE`)."""
    with _MEMORY.lock:
        claimant = _MEMORY.claims.pop(span, None)
    response_id = _response_id(observed)
    if response_id is not None and seen_response(response_id):
        return "drop"
    if claimant is None:
        return "keep"
    if claimant.model and isinstance(model, str) and model and claimant.model != model:
        return "keep"
    if claimant.nested:
        return "drop"
    agrees = None if claimant.call is None else _agrees(claimant.call, observed)
    return "flag" if agrees is None else "drop" if agrees else "keep"


def forget() -> None:
    """Forget every response id and candidate (`hajer.uninstrument()`)."""
    with _MEMORY.lock:
        _MEMORY.responses.clear()
        _MEMORY.claims.clear()
        _MEMORY.settled.clear()

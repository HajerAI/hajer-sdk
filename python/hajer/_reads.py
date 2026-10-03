"""Which parts of the model's answer the application actually read.

The captured request says what was asked; the captured response says what came back. Neither says
what the application **did** with it, and that is the third of the platform's OBSERVED facts: "the SINK is the
set of reply fields the application read, recorded by the SDK's wrapped reply, each a CONSUMER
warrant with its frame". A field nobody reads is a field the answer's shape does not have to keep;
a field the application slices is a cardinality it enforces.

**How it is recorded, and why it is off by default.** A reply the application can read is an object
the provider's own SDK built, and the only way to see a read is to hand back something that notices
one. `ReplyView` delegates everything — attributes, items, iteration, length, truth, `repr` — to the
answer it holds, and records the path of each read. That is strictly more than observation: an
`isinstance(answer, Message)` in the application's own code is false about the view, and a framework
that branches on the response's type would take a different branch. So this is `HAJER_RECORD_REPLY_READS`
and it is **off** unless somebody turns it on. A record made without it carries `replyReads: null`
rather than an empty list, so "not observed" and "the application read nothing" are two facts on
the wire and stay two facts in the warrant set.

The paths use the same grammar `_jsonpath.py` parses — `a.b[0].c`, `[*]` for an iteration — so a
recorded read can be matched against a declared path without a second grammar.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sized
from typing import Final, Protocol, cast

#: How many distinct paths one call records. Past it the reads are not a sink, they are a traversal,
#: and the record says so rather than growing without a bound inside somebody's request path.
MAX_REPLY_READS: Final[int] = 256
#: How long one recorded path may be. A path longer than this is a walk through a document, not a
#: field an obligation is about.
MAX_READ_PATH: Final[int] = 512
#: How deep a view wraps. Past it reads are recorded against the last path and the value is returned
#: unwrapped, so a recursive structure cannot make the SDK build views forever.
MAX_VIEW_DEPTH: Final[int] = 8

#: The names a delegating view must not answer from the wrapper itself, or the delegation stops
#: working: they are how Python finds the state the view holds.
_OWN: Final[frozenset[str]] = frozenset({"_recorder_target", "_recorder_sink", "_recorder_path", "_recorder_depth"})


class ReadSink:
    """The paths one call's application read off its reply, in first-read order, bounded."""

    __slots__ = ("_paths",)

    def __init__(self) -> None:
        self._paths: dict[str, None] = {}

    def note(self, path: str) -> None:
        """Record one read. Beyond the bound nothing is recorded and nothing raises."""
        if len(self._paths) >= MAX_REPLY_READS or not path:
            return
        self._paths.setdefault(path[:MAX_READ_PATH], None)

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path read, in the order it was first read."""
        return tuple(self._paths)


def _step(path: str, piece: str) -> str:
    return piece if not path else f"{path}{piece}" if piece.startswith("[") else f"{path}.{piece}"


class _Subscriptable(Protocol):
    """What a delegating view needs of the answer to read one item out of it."""

    def __getitem__(self, key: object, /) -> object: ...


def _key(key: object) -> str:
    """One subscript as a path step. A slice is written the way the source wrote it: `[:5]`.

    A slice is the one subscript that is itself an obligation — "at most five of them" — so it is
    rendered rather than repr'd: `[:5]`, `[2:]`, `[1:4]`. That is the text a CONSUMER warrant's
    cardinality is read off, and `slice(None, 5, None)` would have hidden it behind a repr.
    """
    if isinstance(key, slice):
        sliced = cast("slice[object, object, object]", key)
        start, stop, step = _bound(sliced.start), _bound(sliced.stop), _bound(sliced.step)
        return f"[{start}:{stop}:{step}]" if step else f"[{start}:{stop}]"
    if isinstance(key, int):
        return f"[{key}]"
    return str(key)


def _bound(value: object) -> str:
    """One end of a slice as text. `None` is the end the source left open, and writes nothing."""
    return "" if value is None else str(value)


class ReplyView:
    """One model answer, delegating, recording the path of every read made through it.

    Nothing here may raise on behalf of the recorder: a failure to record is a failure to observe and
    never a failure of the customer's call, so every recording step is a `try` whose `except` does
    nothing but continue.
    """

    __slots__ = ("_recorder_depth", "_recorder_path", "_recorder_sink", "_recorder_target")

    def __init__(self, target: object, sink: ReadSink, path: str = "", depth: int = 0) -> None:
        object.__setattr__(self, "_recorder_target", target)
        object.__setattr__(self, "_recorder_sink", sink)
        object.__setattr__(self, "_recorder_path", path)
        object.__setattr__(self, "_recorder_depth", depth)

    def _wrap(self, value: object, path: str) -> object:
        sink: ReadSink = object.__getattribute__(self, "_recorder_sink")
        depth: int = object.__getattribute__(self, "_recorder_depth")
        try:
            sink.note(path)
        except Exception:  # noqa: BLE001 - recording never reaches the caller
            return value
        if depth + 1 >= MAX_VIEW_DEPTH or value is None or isinstance(value, (str, bytes, int, float, bool)):
            return value
        # A CALLABLE is never wrapped, and this is not an optimisation. `dict(answer.input)` asks the
        # object for `keys` and then calls what comes back; a view around a bound method is not
        # callable, so wrapping it turns a customer's own line into a TypeError. Measured on a real
        # framework's tool-call reading, and the general rule it forced is the honest one:
        # this recorder observes VALUES the application reads, never the machinery it reads them with.
        if callable(value):
            return value
        return ReplyView(value, sink, path, depth + 1)

    def __getattr__(self, name: str) -> object:
        target: object = object.__getattribute__(self, "_recorder_target")
        value = getattr(target, name)
        if name.startswith("__") or name in _OWN:
            return value
        return self._wrap(value, _step(object.__getattribute__(self, "_recorder_path"), name))

    def __getitem__(self, key: object) -> object:
        target = cast(_Subscriptable, object.__getattribute__(self, "_recorder_target"))
        value = target[key]
        return self._wrap(value, _step(object.__getattribute__(self, "_recorder_path"), _key(key)))

    def __iter__(self) -> Iterator[object]:
        target = cast(Iterable[object], object.__getattribute__(self, "_recorder_target"))
        path = _step(object.__getattribute__(self, "_recorder_path"), "[*]")
        for item in target:
            yield self._wrap(item, path)

    def __len__(self) -> int:
        target = cast(Sized, object.__getattribute__(self, "_recorder_target"))
        sink: ReadSink = object.__getattribute__(self, "_recorder_sink")
        sink.note(_step(object.__getattribute__(self, "_recorder_path"), "[len]"))
        return len(target)

    @property
    def __class__(self) -> type:  # pyright: ignore[reportIncompatibleMethodOverride]
        """The class of the ANSWER, so `isinstance` about the view answers about what it holds.

        The standard transparent-proxy technique, and here it is load-bearing rather than polite.
        A real framework's reading of a provider answer branches on type — `isinstance(usage, BaseModel)`
        decides whether it calls `model_dump()` or treats the value as a mapping — and a view whose
        `isinstance` answered `False` sent that code down the wrong branch and raised inside the
        customer's own call. It does not make the view the answer: `type(view)` is still
        `ReplyView`, and the limitation this recorder carries is unchanged.
        """
        held: object = object.__getattribute__(self, "_recorder_target")
        return type(held)

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_recorder_target"))

    def __repr__(self) -> str:
        return repr(object.__getattribute__(self, "_recorder_target"))

    def __str__(self) -> str:
        return str(object.__getattribute__(self, "_recorder_target"))

    def __eq__(self, other: object) -> bool:
        return bool(object.__getattribute__(self, "_recorder_target") == other)

    def __hash__(self) -> int:
        return hash(object.__getattribute__(self, "_recorder_target"))


def recording(answer: object, sink: ReadSink) -> object:
    """The answer the caller gets back when reads are being recorded, and the answer itself when not."""
    return ReplyView(answer, sink)

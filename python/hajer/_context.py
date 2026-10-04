"""`hajer.session` / `user` / `context` — what a conversation is, said once and carried by every span inside it.

A trace is one request; a conversation is many. The platform groups traces into a conversation by `session.id`,
names the person behind them by `user.id`, and filters them by tags and metadata — and every one of those is a
fact the application knows at a point in its own code (the request handler that has the conversation id) and the
SDK cannot guess. So they are declared, as a block or a decorator, and from there they ride on every span opened
inside: the workflow, the components, the model calls, and the spans another instrumentation makes too
(`hajer._telemetry_otel.HajerContextProcessor` stamps those).

    with hajer.session(conversation.id), hajer.user(customer.id):
        answer = agent.run(question)

The scope is a `contextvars.ContextVar`, so it follows `await` and the threads that copy a context. Nesting merges:
an inner `context(...)` adds to or overrides the enclosing one key by key, and tags accumulate. Values are bounded
by the schema constants in `_settings` (clipped, never refused), with one exception written at the call site: a
metadata value that is not a scalar is a `TypeError` where the developer wrote it, because a nested document is a
different thing from a label and silently flattening it would be the lie.

Imports `_settings` and `_semconv` only.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, ParamSpec, TypeAlias, TypeVar, cast

from hajer import _semconv
from hajer._settings import CONTEXT_METADATA_KEYS_MAX, CONTEXT_TAGS_MAX, CONTEXT_VALUE_MAX_CHARS

P = ParamSpec("P")
R = TypeVar("R")

#: What a metadata value may be: a label, a number, a flag. Not a document.
Scalar: TypeAlias = str | int | float | bool
#: What one span attribute may hold, as the emitter spells it.
AttributeValue: TypeAlias = str | bool | int | float | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TraceContext:
    """The conversation facts in force: immutable, so a child context never writes into its parent's."""

    session: str | None = None
    user: str | None = None
    tags: tuple[str, ...] = ()
    metadata: tuple[tuple[str, Scalar], ...] = ()

    def merged(
        self, *, session: str | None, user: str | None, tags: Sequence[str], metadata: Mapping[str, Scalar] | None
    ) -> TraceContext:
        """This context with another's facts on top: a given value wins, tags accumulate in order, keys cap."""
        combined: dict[str, Scalar] = dict(self.metadata)
        for key, value in (metadata or {}).items():
            combined[_clipped(key)] = _clipped(value) if isinstance(value, str) else value
        kept_tags = tuple(dict.fromkeys((*self.tags, *(_clipped(tag) for tag in tags))))[:CONTEXT_TAGS_MAX]
        return TraceContext(
            session=_clipped(session) if session is not None else self.session,
            user=_clipped(user) if user is not None else self.user,
            tags=kept_tags,
            metadata=tuple(combined.items())[:CONTEXT_METADATA_KEYS_MAX],
        )


_ROOT: Final[TraceContext] = TraceContext()
_CURRENT: contextvars.ContextVar[TraceContext] = contextvars.ContextVar("hajer_trace_context", default=_ROOT)


def _clipped(value: str) -> str:
    return value[:CONTEXT_VALUE_MAX_CHARS]


def current() -> TraceContext:
    """The conversation facts in force here."""
    return _CURRENT.get()


def attributes(context: TraceContext) -> dict[str, AttributeValue]:
    """The context as span attributes: `session.id`, `user.id`, `hajer.tags`, one `hajer.metadata.<key>` each."""
    found: dict[str, AttributeValue] = {}
    if context.session is not None:
        found[_semconv.SESSION_ID] = context.session
    if context.user is not None:
        found[_semconv.USER_ID] = context.user
    if context.tags:
        found[_semconv.TAGS] = context.tags
    for key, value in context.metadata:
        found[_semconv.METADATA_PREFIX + key] = value
    return found


class TraceContextScope:
    """One declaration, three spellings: `with`, `async with`, and a decorator on a sync or async function."""

    __slots__ = ("_metadata", "_session", "_tags", "_tokens", "_user")

    def __init__(
        self,
        *,
        session: str | None,
        user: str | None,
        tags: Sequence[str],
        metadata: Mapping[str, object] | None,
    ) -> None:
        labels: dict[str, Scalar] = {}
        for key, value in (metadata or {}).items():
            # Typed as scalars on the public names; checked here because a label reaches this seam from untyped code too.
            if not isinstance(value, (str, int, float, bool)):
                raise TypeError(
                    f"hajer.context metadata {key!r} is {type(value).__name__}; a value is a string, a number or a "
                    "boolean — a document belongs in the span's content, not in its labels"
                )
            labels[key] = value
        self._session = session
        self._user = user
        self._tags = tuple(str(tag) for tag in tags)
        self._metadata = labels
        self._tokens: list[contextvars.Token[TraceContext]] = []

    def __enter__(self) -> TraceContextScope:
        merged = _CURRENT.get().merged(session=self._session, user=self._user, tags=self._tags, metadata=self._metadata)
        self._tokens.append(_CURRENT.set(merged))
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        if not self._tokens:  # pragma: no cover - an exit with no entry
            return
        token = self._tokens.pop()
        try:
            _CURRENT.reset(token)
        except ValueError:  # pragma: no cover - a token from another context; the value is that context's
            pass

    async def __aenter__(self) -> TraceContextScope:
        return self.__enter__()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.__exit__(exc_type, exc, tb)

    def __call__(self, function: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(function):
            awaitable = cast(Callable[P, Awaitable[object]], function)

            @functools.wraps(function)
            async def awaited(*args: P.args, **kwargs: P.kwargs) -> object:
                with self._fresh():
                    return await awaitable(*args, **kwargs)

            return cast(Callable[P, R], awaited)

        @functools.wraps(function)
        def called(*args: P.args, **kwargs: P.kwargs) -> R:
            with self._fresh():
                return function(*args, **kwargs)

        return called

    def _fresh(self) -> TraceContextScope:
        """A decorator enters once per call, so each call gets its own scope object and its own token stack."""
        return TraceContextScope(session=self._session, user=self._user, tags=self._tags, metadata=self._metadata)


def context(
    *,
    session: str | None = None,
    user: str | None = None,
    tags: Sequence[str] = (),
    metadata: Mapping[str, Scalar] | None = None,
) -> TraceContextScope:
    """Declare the conversation facts every span inside carries: the session, the user, tags, metadata.

        with hajer.context(session=conversation.id, user=customer.id, tags=["beta"], metadata={"plan": "pro"}):
            ...

    Each is optional and each merges with the enclosing declaration: a given value wins, tags accumulate.
    """
    return TraceContextScope(session=session, user=user, tags=tags, metadata=metadata)


def session(session_id: str) -> TraceContextScope:
    """Declare the conversation a span belongs to. The platform reads a conversation through this id."""
    return TraceContextScope(session=session_id, user=None, tags=(), metadata=None)


def user(user_id: str) -> TraceContextScope:
    """Declare the person behind the request, by an id of the application's own — never a name or an email."""
    return TraceContextScope(session=None, user=user_id, tags=(), metadata=None)

"""The one name for "a value that survives a JSON round trip".

`pydantic.JsonValue` is the recursive alias pydantic itself validates against, so the SDK uses it
rather than declaring a second one. Every payload the SDK sends and every value it reads out of a
provider object is typed with these two names: there is no written `Any` anywhere in this package
(`reportExplicitAny = "error"`), and an opaque value is `object`, narrowed before it is used.
"""

from __future__ import annotations

from typing import TypeAlias

from pydantic import JsonValue

JsonObject: TypeAlias = dict[str, JsonValue]

__all__ = ["JsonObject", "JsonValue"]

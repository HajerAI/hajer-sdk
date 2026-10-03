"""The named outcomes an adapter can end in before the application's own code runs."""

from __future__ import annotations

from typing import Literal, TypeAlias

EntryReason: TypeAlias = Literal["MISSING_DEPENDENCY", "ADAPTER_MISSING", "PARSE"]


class EntryUnavailable(Exception):  # noqa: N818 - a named outcome, not a crash
    def __init__(self, reason: EntryReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: EntryReason = reason

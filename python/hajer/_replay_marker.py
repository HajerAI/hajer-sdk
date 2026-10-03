"""Hajer's own replay marker, `evidence.hajerReplay`: the ids it carries, and the one thing client redaction skips.

The replay child (`replay/_case.py`) puts `{runId, planId, caseId, attempt}` in every replay `verify`'s evidence. The
ids are Hajer's own sha256-hex digests, and about one in a few hundred holds a Luhn-valid digit run: the client pass
then reported a phantom CARD and sent the id mangled, so the capture no longer named its run, plan or case. A string at
exactly `evidence.hajerReplay.<runId | planId | caseId>` that whole matches the grammar of the id Hajer mints there is
therefore left as it is and counted nowhere; any other value under `hajerReplay` is scanned as today.

The grammars are the service's, spelled identically (the platform's test compares the two and holds each to the
function that mints the id).
"""

from __future__ import annotations

import re
from typing import Final

REPLAY_MARKER: Final = "hajerReplay"
#: Each marker id's grammar, anchored, as the service mints the id.
MARKER_ID_GRAMMARS: Final[dict[str, str]] = {
    "runId": r"^replay-run-[0-9a-f]{32}$",
    "planId": r"^(?:suite-replay-[0-9a-f]{32}|cycle-investigate-[0-9a-f]{40}-replay-(?:baseline|candidate|observed))$",
    "caseId": r"^case-[0-9a-f]{64}$",
}
_COMPILED: Final = {name: re.compile(grammar) for name, grammar in MARKER_ID_GRAMMARS.items()}
_PREFIX: Final = f"evidence.{REPLAY_MARKER}."


def minted_marker_id(path: str, value: str) -> bool:
    """Whether `value`, at `path` in a submission, is one of Hajer's own replay-marker ids, whole."""
    if not path.startswith(_PREFIX):
        return False
    grammar = _COMPILED.get(path.removeprefix(_PREFIX))
    return grammar is not None and grammar.fullmatch(value) is not None

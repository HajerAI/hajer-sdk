"""The qualification rule, the same one the Hajer platform applies: labels only, never a gate.

Only PASS and FAIL count. Both present is `flaky`. At least MIN_AGREEING counted attempts, all equal, is
`qualified`, whichever verdict they agree on (three FAILs are a qualified FAIL). Anything else is `not_yet`, and
another attempt is due while the label is `not_yet` and fewer than MAX_REPEATS were made. UNABLE_TO_VERIFY is the
rule's UNKNOWN: it spends an attempt and never counts toward agreement.

A live check's verdict is the one its label settled on, as the platform reads it: PASS or FAIL when `qualified`,
and UNABLE_TO_VERIFY (no settled verdict) when `not_yet` or `flaky`. A replay reads fixed recorded answers, so its
one attempt is deterministic and is its verdict, while its label stays `not_yet`: labels describe live repeated
behaviour, and the receipt says `mode: replay` so a replay PASS is never read as a qualified one. The
attempts stay in the receipt whatever the verdict, so a qualified PASS still shows an earlier UNKNOWN.

This mirrors the platform's rule, and `tests/test_qualification_vectors.py`
holds it to the 363 shared vectors in `contract/qualification-vectors.json`. The two numbers are the rule's,
not bounds a customer tunes, so they are not settings: a different cap or agreement would be a different rule and
would disagree with the platform.
"""

from collections.abc import Sequence
from typing import Final

from hajer.pytest_plugin._models import Label, Verdict

MAX_REPEATS: Final = 5
MIN_AGREEING: Final = MAX_REPEATS // 2 + 1

_COUNTED: Final[frozenset[Verdict]] = frozenset({"PASS", "FAIL"})


def label(outcomes: Sequence[Verdict]) -> Label:
    """The label these attempts earn, in any order and of any length."""
    counted = [outcome for outcome in outcomes if outcome in _COUNTED]
    if len(set(counted)) > 1:
        return "flaky"
    return "qualified" if len(counted) >= MIN_AGREEING else "not_yet"


def next_repeat(outcomes: Sequence[Verdict]) -> bool:
    """Whether another attempt is due after these: still `not_yet`, and still under the cap."""
    return len(outcomes) < MAX_REPEATS and label(outcomes) == "not_yet"


def read(outcomes: Sequence[Verdict], *, replayed: bool) -> tuple[Label, Verdict]:
    """The label and verdict these attempts give a check: live, the settled verdict; replay, its single read."""
    if replayed:
        # One deterministic read decides a replay; anything but exactly one attempt is not a replay and settles nothing.
        return "not_yet", outcomes[0] if len(outcomes) == 1 else "UNABLE_TO_VERIFY"
    earned = label(outcomes)
    if earned != "qualified":
        return earned, "UNABLE_TO_VERIFY"
    settled: Verdict = next(outcome for outcome in outcomes if outcome in _COUNTED)
    return earned, settled

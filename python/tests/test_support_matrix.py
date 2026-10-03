"""`docs/support-matrix.md` says what the code does, because the code prints it.

The table is the answer to "will my library be captured, and what will the record carry" — the kind of
document that goes stale silently and is then worse than no document at all. So it is generated from the
`Target` declarations and this test holds the committed file to them: add a library, change a caveat, or
turn a `no` into a `yes`, and the table moves in the same commit or the suite goes red.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol, cast

from hajer._attach import targets
from hajer._targets import COLUMNS, NO, YES, Target

#: `tests/` → this package's root, where the generator and its output live.
_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
GENERATOR: Final[Path] = _ROOT / "scripts" / "generate_support_matrix.py"
MATRIX: Final[Path] = _ROOT / "docs" / "support-matrix.md"


class _Generator(Protocol):
    def render(self, rows: Sequence[Target]) -> str: ...


def _generator() -> _Generator:
    """Load the generator by path: it is a script beside this package, not part of it."""
    specification = importlib.util.spec_from_file_location("generate_support_matrix", GENERATOR)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return cast(_Generator, module)


def test_matrix_matches_targets() -> None:
    """The committed table is exactly what the declarations print. No drift, in either direction."""
    assert MATRIX.exists(), f"{MATRIX} is missing; run scripts/generate_support_matrix.py --write"
    assert MATRIX.read_text(encoding="utf-8") == _generator().render(targets()), (
        "docs/support-matrix.md is stale; run: uv run python scripts/generate_support_matrix.py --write"
    )


def test_every_instrumented_library_has_a_row_and_every_cell_is_filled() -> None:
    """A target with no support declaration would be a library nobody could look up."""
    rows = targets()
    assert len(rows) >= 6, "openai, anthropic, both LangChain packages, litellm and google-genai"
    labels = [target.support.label for target in rows]
    assert labels == sorted(set(labels), key=labels.index), "two rows for one library would be ambiguous"
    for target in rows:
        cells = target.support.row()
        assert len(cells) == len(COLUMNS), f"{target.module} has {len(cells)} cells for {len(COLUMNS)} columns"
        assert all(cell for cell in cells), f"{target.module} has an empty cell"
        assert set(cells[1:6]) <= {YES, NO}, f"{target.module} spells a flag some other way"
        assert cells[-1].endswith("."), f"{target.module}'s caveat is not a sentence"


def test_litellm_and_genai_are_in_the_matrix() -> None:
    """The two libraries added together, named the way a customer installs them."""
    labels = {target.support.label for target in targets()}
    assert {"litellm", "google-genai"} <= labels

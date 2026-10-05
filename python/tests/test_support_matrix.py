"""The support matrix on docs.hajer.ai says what the code does, because the code prints it.

The table is the answer to "will my library be captured, and what will the record carry" — the kind of
document that goes stale silently and is then worse than no document at all. So it is generated from the
`Target` declarations into the docs site (HajerAI/hajer-docs, `docs/telemetry/_support-matrix.mdx`), and
these tests hold the declarations and the generator to a table that can be printed: every library has a
complete row, and the rendered partial carries every one of them.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol, cast

from hajer._attach import targets
from hajer._targets import COLUMNS, NO, YES, Target

#: `tests/` → this package's root, where the generator lives.
_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
GENERATOR: Final[Path] = _ROOT / "scripts" / "generate_support_matrix.py"


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


def test_rendered_partial_has_one_row_per_target() -> None:
    """The partial the docs site imports: an MDX comment (an HTML one breaks MDX), the header, and every target."""
    rendered = _generator().render(targets())
    assert rendered.startswith("{/*"), "the banner must be an MDX comment"
    assert "<!--" not in rendered, "MDX cannot parse an HTML comment"
    assert f"| {' | '.join(COLUMNS)} |" in rendered
    rows = [
        line for line in rendered.splitlines() if line.startswith("| ") and not line.startswith(f"| {COLUMNS[0]} |")
    ]
    assert [row.split(" | ")[0].removeprefix("| ") for row in rows] == [t.support.label for t in targets()]


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

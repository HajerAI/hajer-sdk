"""Where the files this suite reads live.

`contract/` at the repository root is the Hajer platform's half of the one agreement this SDK still keeps:
the redaction vectors both sides run. It is copied from the platform, never edited here (`contract/README.md`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

SDK_REPOSITORY: Final[Path] = Path(__file__).resolve().parents[2]
CONTRACT: Final[Path] = SDK_REPOSITORY / "contract"

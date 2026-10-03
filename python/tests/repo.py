"""Where the files this suite reads live.

`contract/` at the repository root is the Hajer platform's half of every agreement this SDK keeps: the
OpenAPI snapshot `hajer/_wire.py` is generated from, the shared test vectors, and the recorded fixtures
the platform's own tests read back. It is copied from the platform, never edited here (`contract/README.md`).

A handful of checks compare the SDK against the platform's *source* rather than a vendored copy. They run
only when this repository is checked out as the platform's `sdk/` submodule, and skip everywhere else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

SDK_REPOSITORY: Final[Path] = Path(__file__).resolve().parents[2]
CONTRACT: Final[Path] = SDK_REPOSITORY / "contract"
ACTION: Final[Path] = SDK_REPOSITORY / "action"

_PARENT = SDK_REPOSITORY.parent
#: The platform checkout this SDK is mounted in, or None when it stands alone.
PLATFORM: Final[Path | None] = _PARENT if (_PARENT / "backend" / "app").is_dir() else None

requires_platform = pytest.mark.skipif(
    PLATFORM is None,
    reason="compares against the Hajer platform source; runs when this repo is the platform's sdk/ submodule",
)


def platform_path(relative: str) -> Path:
    """A path inside the platform checkout; only called from a test marked `requires_platform`."""
    assert PLATFORM is not None, "guarded by requires_platform"
    return PLATFORM / relative

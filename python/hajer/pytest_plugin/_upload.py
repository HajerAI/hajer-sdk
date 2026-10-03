"""The receipt upload lives in `hajer._upload`, importable without pytest (`python -m hajer upload-results`)."""

from hajer._upload import upload

__all__ = ["upload"]

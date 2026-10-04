"""Content capture is on by default, and opting out is one explicit variable."""

from __future__ import annotations

import pytest

from hajer._settings import HajerSettings


@pytest.mark.parametrize("from_env", [False, True])
def test_content_defaults_on_and_opt_out_is_explicit(from_env: bool) -> None:
    settings = HajerSettings.from_env({}) if from_env else HajerSettings()
    assert settings.capture_content is True
    assert HajerSettings.from_env({"HAJER_CAPTURE_CONTENT": "0"}).capture_content is False

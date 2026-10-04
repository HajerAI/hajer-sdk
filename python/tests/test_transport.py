"""The deadline and the body bound every SDK request obeys."""

from __future__ import annotations

import time

import pytest

import hajer
from hajer._transport import Deadline, encode_body


class TestDeadline:
    def test_remaining_never_goes_negative(self) -> None:
        deadline = Deadline(1)
        time.sleep(0.01)
        assert deadline.remaining_ms() == 0
        assert deadline.expired() is True
        assert deadline.timeout_s() == 0.0

    def test_budget_is_what_was_asked_for(self) -> None:
        deadline = Deadline(750)
        assert deadline.budget_ms == 750
        assert 0 < deadline.remaining_ms() <= 750
        assert deadline.elapsed_ms() >= 0


class TestEncodeBody:
    def test_canonical_bytes_are_stable_across_key_order(self) -> None:
        first = encode_body({"b": 1, "a": 2}, limit=1024)
        second = encode_body({"a": 2, "b": 1}, limit=1024)
        assert first == second == b'{"a":2,"b":1}'

    def test_over_the_bound_refuses_locally(self) -> None:
        with pytest.raises(hajer.BodyOverBoundError) as raised:
            encode_body({"payload": "x" * 200}, limit=64)
        assert raised.value.limit == 64
        assert raised.value.size > 64

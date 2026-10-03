"""The case key this side derives, against the vectors the platform answers with the same digests.

`contract/case-key-vectors.json`. One file of vectors, two implementations, two tests reading it: the
Hajer platform's own test is the other. Writing the recipe twice is the price of the SDK having two runtime dependencies; this file
is what makes the price safe to pay.
"""

from __future__ import annotations

import json
from typing import cast

import pytest

from hajer._case_key import CASE_KEY_SCHEME, derive_case_key, normalised_request
from hajer._json import JsonObject, JsonValue
from hajer._jsonpath import PathSyntaxError
from tests.repo import CONTRACT

VECTORS = CONTRACT / "case-key-vectors.json"


def test_shared_vectors_agree_between_sdk_and_server() -> None:
    """Every committed vector is what this implementation produces, name by name."""
    document = cast("dict[str, object]", json.loads(VECTORS.read_text(encoding="utf-8")))
    vectors = cast("list[dict[str, object]]", document["vectors"])

    assert document["scheme"] == CASE_KEY_SCHEME
    assert len(vectors) >= 10
    for vector in vectors:
        request = cast("JsonObject", vector["request"])
        paths = tuple(str(path) for path in cast("list[object]", vector["volatilePaths"]))
        assert derive_case_key(request, volatile_paths=paths) == vector["caseKey"], vector["name"]


def test_the_scheme_is_named_in_the_key() -> None:
    """A change to what the digest covers is `ck2`, so two SDK versions cannot disagree silently."""
    assert derive_case_key({"a": 1}).startswith(f"{CASE_KEY_SCHEME}_")


def test_key_order_and_volatile_paths_do_not_move_the_key() -> None:
    """Two spellings of one request are one case; a volatile field is not part of the case at all."""
    assert derive_case_key({"a": 1, "b": 2}) == derive_case_key({"b": 2, "a": 1})
    assert derive_case_key({"q": "x", "requestId": "r-1"}, volatile_paths=("requestId",)) == derive_case_key(
        {"q": "x", "requestId": "r-2"}, volatile_paths=("requestId",)
    )
    assert derive_case_key({"q": "x"}, volatile_paths=("absent",)) == derive_case_key({"q": "x"})


def test_a_removed_field_is_absent_and_not_null() -> None:
    """`null` is a value a caller can send; a removed volatile field must not become one."""
    assert normalised_request({"q": "x", "r": "1"}, volatile_paths=("r",)) == '{"q":"x"}'
    assert normalised_request({"q": "x", "r": None}) == '{"q":"x","r":null}'


def test_a_request_that_is_not_an_object_normalises_to_an_empty_one() -> None:
    """`verify` takes any JSON as the request; a scalar has no fields, so it has no case of its own."""
    assert derive_case_key(cast("JsonValue", "a string")) == derive_case_key({})


def test_a_malformed_volatile_path_is_refused_where_it_is_written() -> None:
    """A path the grammar cannot read is a declaration error, never a silent miss."""
    with pytest.raises(PathSyntaxError):
        derive_case_key({"a": 1}, volatile_paths=("a..b[",))

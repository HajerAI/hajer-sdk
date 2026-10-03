"""The case key, derived here so that it is derived *before* anything rewrites the request.

`contract/case-key-vectors.json` holds the shared vectors both this module's tests and the
server's read, so the two implementations cannot drift without a red test on both sides.

**Why the SDK derives it at all.** The server can derive a case key too, and does when the wire carries
none — but it can only read the request *as it arrives*, which is after client-side redaction has
replaced a matched value with a placeholder. A key derived there moves the moment a rule fires, and two
attempts at one case land in two groups. Derived here, before redaction, it does not move; the row says
`DERIVED_CLIENT` so a reader of a repeatability receipt knows which of the two it has.

**A caller's own key beats both.** `verify(..., case_key="case-alpha")` is `CALLER`, and an application
that knows its own scenario id knows more than any digest of a payload can.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final, Literal

from hajer._json import JsonObject, JsonValue
from hajer._jsonpath import parse_paths, without

#: `ck1` names the derivation, not the key: a change to what the digest covers is `ck2`, so two SDK
#: versions can never disagree silently about whether two requests are one case.
CASE_KEY_SCHEME: Final[str] = "ck1"

#: Where a key came from. `DERIVED_SERVER` is the backend's own label and never sent from here.
CaseKeySource = Literal["CALLER", "DERIVED_CLIENT"]


def _canonical(document: JsonObject) -> str:
    """The same canonical JSON the server digests."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)


def _length_prefixed(part: str) -> bytes:
    """One field of the seed, length-prefixed so two fields can never run together."""
    encoded = part.encode("utf-8")
    return f"{len(encoded)}:".encode() + encoded


def normalised_request(request: JsonValue, *, volatile_paths: tuple[str, ...] = ()) -> str:
    """The request as the bytes a case key is taken over: canonical JSON, volatile paths removed."""
    document: JsonObject = dict(request) if isinstance(request, dict) else {}
    stripped, _ = without(document, parse_paths(volatile_paths))
    return _canonical(stripped if isinstance(stripped, dict) else {})


def derive_case_key(request: JsonValue, *, volatile_paths: tuple[str, ...] = ()) -> str:
    """One case key over one request. `ck1_<sha256>` — 68 characters, inside the server's bound."""
    seed = _length_prefixed(CASE_KEY_SCHEME) + _length_prefixed(
        normalised_request(request, volatile_paths=volatile_paths)
    )
    return f"{CASE_KEY_SCHEME}_{hashlib.sha256(seed).hexdigest()}"

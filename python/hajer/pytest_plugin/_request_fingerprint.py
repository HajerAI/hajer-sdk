"""Hash observed provider requests in order without retaining credentials or request values."""

import hashlib

import httpx


class RequestFingerprint:
    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self._observed = False

    def observe(self, request: httpx.Request) -> None:
        # Headers (especially credentials) are intentionally absent. Hash each framed component so
        # different request boundaries cannot collide by concatenation. Raw values never leave memory.
        for value in (request.method.encode(), str(request.url).encode(), request.read()):
            self._hash.update(hashlib.sha256(value).digest())
        self._observed = True

    def digest(self) -> str | None:
        return "sha256:" + self._hash.hexdigest() if self._observed else None

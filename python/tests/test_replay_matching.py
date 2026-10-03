"""A recorded boundary answers only the request it recorded: method, URL and request-body digest.

A recording made before the SDK kept request digests is still served on method and URL, and the attempt says
WEAK_BOUNDARY_MATCH; a request whose body differs from every recording of its URL is NETWORK_UNRECORDED.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import hajer
from hajer._boundary import UNREADABLE_REQUEST, request_body_digest
from hajer._settings import HajerSettings
from hajer.replay._guard import SandboxRefused, recordings_from
from tests.conftest import Recorder, assessment_json, responds
from tests.test_boundary import crm, responses
from tests.test_replay import boundary, log, sandbox, sent

POST_BODY = json.dumps({"customer": 7, "amount": 12}).encode()


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _request(body: bytes = POST_BODY) -> httpx.Request:
    return httpx.Request("POST", "https://crm.example.test/p?id=1", content=body)


def test_the_recorder_keeps_the_request_body_digest(settings: HajerSettings) -> None:
    recorder = Recorder(responds(assessment_json()))
    dependency = httpx.Client(transport=httpx.MockTransport(crm), base_url="https://crm.internal")
    with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
        with hajer.record_boundaries(hosts=["crm.internal"], settings=settings):
            dependency.post("/customers/c-42", content=POST_BODY)
            client.verify("refunds@1", {"orderId": "o-1"}, "reply")
    (entry,) = responses(recorder.bodies()[0])
    assert entry["requestBodyDigest"] == _digest(POST_BODY)

    def chunks() -> Iterator[bytes]:
        yield b"streamed"

    assert request_body_digest(httpx.Request("POST", "https://x.test", content=chunks())) == UNREADABLE_REQUEST


def test_a_recording_is_served_only_for_its_own_request_body(tmp_path: Path) -> None:
    entry = boundary('{"refund": "ok"}', method="POST", requestBodyDigest=_digest(POST_BODY))
    guard = sandbox(tmp_path, recordings_from([entry]))  # pyright: ignore[reportArgumentType]

    with pytest.raises(SandboxRefused):
        guard.send_sync(_request(json.dumps({"customer": 8, "amount": 12}).encode()), sent)
    served = guard.send_sync(_request(), sent)

    assert served.json() == {"refund": "ok"}
    assert guard.record.refusals == ["NETWORK_UNRECORDED"]
    assert guard.record.weak_matches == 0
    assert [(item["allowed"], item["served"]) for item in log(tmp_path)] == [(False, False), (False, True)]


def test_an_unreadable_recorded_request_is_never_served(tmp_path: Path) -> None:
    entry = boundary("{}", method="POST", requestBodyDigest=UNREADABLE_REQUEST)
    guard = sandbox(tmp_path, recordings_from([entry]))  # pyright: ignore[reportArgumentType]
    with pytest.raises(SandboxRefused):
        guard.send_sync(_request(), sent)
    assert guard.record.refusals == ["NETWORK_UNRECORDED"]


def test_a_legacy_recording_is_served_on_method_and_url_and_marked_weak(tmp_path: Path) -> None:
    guard = sandbox(tmp_path, recordings_from([boundary('{"tier": "gold"}', method="POST")]))  # pyright: ignore[reportArgumentType]
    served = guard.send_sync(_request(b"anything"), sent)
    assert served.json() == {"tier": "gold"}
    assert guard.record.weak_matches == 1
    assert guard.record.refusals == []


def test_a_refusal_inside_a_forked_child_reaches_the_shared_egress_log(tmp_path: Path) -> None:
    """Under bwrap nothing forbids fork at the OS layer. A refusal a forked child logs is appended to
    the same egress log the parent reads (append mode, flushed per line), so the attempt still fails closed."""
    guard = sandbox(tmp_path)
    guard.refuse("NETWORK_UNRECORDED", "crm.example.test", 443)
    child = os.fork()
    if child == 0:  # pragma: no cover - runs in the forked process
        guard.refuse("BLOCKED_EFFECT", "hooks.example.test", 443)
        os._exit(0)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert [(item["host"], item["allowed"]) for item in log(tmp_path)] == [
        ("crm.example.test", False),
        ("hooks.example.test", False),
    ]

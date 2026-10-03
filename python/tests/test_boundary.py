"""`hajer.record_boundaries`: opt-in recording of non-model HTTP responses into a submission's evidence.

No network: the application's dependency and Hajer itself are both `httpx.MockTransport`s.
"""

from __future__ import annotations

import asyncio
from typing import cast

import httpx
import pytest

import hajer
from hajer._boundary import BOUNDARY_EVIDENCE_KEY
from hajer._json import JsonObject
from tests.conftest import Recorder, assessment_json, responds

CAP = 64
KEPT = 4


def crm(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/big":
        return httpx.Response(200, content=b"x" * (CAP + 10), headers={"content-type": "text/plain"})
    return httpx.Response(200, json={"customer": request.url.path.rsplit("/", 1)[-1], "tier": "gold"})


def boundary(body: JsonObject) -> JsonObject:
    evidence = body["evidence"]
    assert isinstance(evidence, dict)
    recorded = evidence[BOUNDARY_EVIDENCE_KEY]
    assert isinstance(recorded, dict)
    return recorded


def responses(body: JsonObject) -> list[JsonObject]:
    return cast("list[JsonObject]", boundary(body)["responses"])


def test_named_hosts_are_recorded_into_evidence_and_taken_once(settings: hajer.HajerSettings) -> None:
    recorder = Recorder(responds(assessment_json()))
    dependency = httpx.Client(transport=httpx.MockTransport(crm), base_url="https://crm.internal")
    other = httpx.Client(transport=httpx.MockTransport(crm), base_url="https://elsewhere.example")
    with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
        with hajer.record_boundaries(hosts=["crm.internal"], settings=settings):
            dependency.get("/customers/c-42", params={"expand": "tier"})
            other.get("/customers/c-43")
            client.verify("refunds@1", {"orderId": "o-1"}, "reply", {"policy": "v3"})
            client.verify("refunds@1", {"orderId": "o-2"}, "reply")
        dependency.get("/customers/c-44")
        client.verify("refunds@1", {"orderId": "o-3"}, "reply")

    first, second, outside = recorder.bodies()
    evidence = first["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["policy"] == "v3"
    assert boundary(first)["schema"] == "hajer-boundary-v1"
    assert boundary(first)["dropped"] == 0
    (entry,) = responses(first)
    assert (entry["method"], entry["host"], entry["path"], entry["query"]) == (
        "GET",
        "crm.internal",
        "/customers/c-42",
        "expand=tier",
    )
    assert entry["status"] == 200
    assert entry["body"] == '{"customer":"c-42","tier":"gold"}'
    assert entry["truncated"] is False
    assert str(entry["bodyDigest"]).startswith("sha256:")
    # Taken with the first submission; the SDK's own posts to Hajer and unnamed hosts are never recorded.
    assert second["evidence"] == {}
    assert outside["evidence"] == {}


def test_bodies_are_capped_and_counts_are_kept(settings: hajer.HajerSettings) -> None:
    recorder = Recorder(responds(assessment_json()))
    dependency = httpx.Client(transport=httpx.MockTransport(crm), base_url="https://crm.internal")
    with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
        bounded = settings.model_copy(update={"boundary_body_max_bytes": CAP, "boundary_responses_max": KEPT})
        with hajer.record_boundaries(hosts=["CRM.internal"], settings=bounded):
            dependency.get("/big")
            for index in range(KEPT + 2):
                dependency.get(f"/customers/c-{index}")
            client.verify("refunds@1", {"orderId": "o-1"}, "reply")
    (body,) = recorder.bodies()
    kept = responses(body)
    assert len(kept) == KEPT
    assert boundary(body)["dropped"] == 3
    big = kept[0]
    assert big["truncated"] is True
    assert big["bodyBytes"] == CAP + 10
    assert len(str(big["body"])) == CAP


def test_streamed_responses_are_recorded_without_reading_them(settings: hajer.HajerSettings) -> None:
    recorder = Recorder(responds(assessment_json()))
    dependency = httpx.Client(transport=httpx.MockTransport(crm), base_url="https://crm.internal")
    with hajer.Hajer(settings=settings, transport=recorder.transport()) as client:
        with hajer.record_boundaries(hosts=["crm.internal"], settings=settings):
            with dependency.stream("GET", "/customers/c-9") as streamed:
                assert streamed.read() == b'{"customer":"c-9","tier":"gold"}'
            client.verify("refunds@1", {"orderId": "o-1"}, "reply")
    (entry,) = responses(recorder.bodies()[0])
    assert entry["streamed"] is True
    assert entry["body"] is None


def test_the_async_client_is_recorded_too(settings: hajer.HajerSettings) -> None:
    recorder = Recorder(responds(assessment_json()))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(crm), base_url="https://crm.internal") as dependency:
            async with hajer.AsyncHajer(settings=settings, transport=recorder.transport()) as client:
                with hajer.record_boundaries(hosts=["crm.internal"], settings=settings):
                    await dependency.get("/customers/c-7")
                    await client.verify("refunds@1", {"orderId": "o-1"}, "reply")

    asyncio.run(run())
    (entry,) = responses(recorder.bodies()[0])
    assert entry["path"] == "/customers/c-7"


@pytest.mark.parametrize(
    "hosts", [[], [""], ["api.openai.com"], ["eu.api.anthropic.com"], ["bedrock-runtime.us-east-1.amazonaws.com"]]
)
def test_model_hosts_and_empty_host_lists_are_refused(hosts: list[str], settings: hajer.HajerSettings) -> None:
    with pytest.raises(ValueError, match="host"), hajer.record_boundaries(hosts=hosts, settings=settings):
        pass

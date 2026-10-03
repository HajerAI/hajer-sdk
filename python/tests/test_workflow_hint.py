"""A declaration guides grouping without changing call ownership or provider arguments."""

from __future__ import annotations

import asyncio

import pytest

import hajer
from tests.fakes import FakeAsyncOpenAI, FakeOpenAI


@pytest.mark.parametrize("raw", [False, True])
def test_hint_is_captured_without_changing_provider_request(raw: bool) -> None:
    client = hajer.wrap(FakeOpenAI(), settings=hajer.HajerSettings(capture_content=raw, capture_raw=raw))
    with hajer.scope(workflow="support-answer") as operation:
        client.chat.completions.create(model="fake", messages=[])
    call = operation.calls[0]
    assert call.workflow_hint == "support-answer"
    assert call.to_wire()["workflowHint"] == "support-answer"
    assert "workflowHint" not in client.completions.calls[0]
    assert "workflow" not in client.completions.calls[0]


def test_nested_scopes_restore_names_and_do_not_inherit_implicitly() -> None:
    client = hajer.wrap(FakeOpenAI())
    with hajer.scope(workflow="outer") as outer:
        client.chat.completions.create(model="fake", messages=[])
        with hajer.scope(workflow="inner") as inner:
            client.chat.completions.create(model="fake", messages=[])
        with hajer.scope() as unnamed:
            client.chat.completions.create(model="fake", messages=[])
        client.chat.completions.create(model="fake", messages=[])
    assert [call.workflow_hint for call in outer.calls] == ["outer", "outer"]
    assert inner.calls[0].workflow_hint == "inner"
    assert "workflowHint" not in unnamed.calls[0].to_wire()
    client.chat.completions.create(model="fake", messages=[])
    assert "workflowHint" not in hajer.wrapped_calls()[0].to_wire()


async def test_parallel_tasks_keep_their_declared_workflows() -> None:
    client = hajer.wrap(FakeAsyncOpenAI())

    async def run(name: str) -> hajer.WrappedCall:
        with hajer.scope(workflow=name) as operation:
            await asyncio.sleep(0)
            await client.chat.completions.create(model="fake", messages=[])
        return operation.calls[0]

    calls = await asyncio.gather(run("support"), run("billing"), run("support"))
    assert [call.workflow_hint for call in calls] == ["support", "billing", "support"]
    assert len({id(call) for call in calls}) == 3

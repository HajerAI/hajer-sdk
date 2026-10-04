"""Framework fan-out retains the actual application caller without cross-task attribution."""

import asyncio

import pytest

from hajer import scope, wrap
from hajer._frame_context import frames_for_call
from hajer._settings import HajerSettings
from tests.fakes import FakeAsyncAnthropic


class Framework:
    def __init__(self) -> None:
        self._async_client = FakeAsyncAnthropic()

    async def ainvoke(self) -> object:
        answers = await asyncio.gather(self._async_client.messages.create(model="fixture", messages=[]))
        return answers[0]


async def workflow_alpha(client: Framework) -> tuple[str, ...]:
    with scope() as operation:
        await client.ainvoke()
        return tuple(frame.qualname for frame in operation.calls[0].caller_frames)


async def workflow_beta(client: Framework) -> tuple[str, ...]:
    with scope() as operation:
        await client.ainvoke()
        return tuple(frame.qualname for frame in operation.calls[0].caller_frames)


async def test_framework_child_task_keeps_caller_and_isolates_concurrent_workflows() -> None:
    settings = HajerSettings(capture_content=True, disabled=True)
    client = wrap(Framework(), settings=settings)
    wrap(client, settings=settings)
    alpha, beta = await asyncio.gather(workflow_alpha(client), workflow_beta(client))
    assert "workflow_alpha" in alpha
    assert "workflow_beta" not in alpha
    assert "workflow_beta" in beta
    assert "workflow_alpha" not in beta
    assert not {"workflow_alpha", "workflow_beta"} & {frame.qualname for frame in frames_for_call()}


async def test_framework_exception_restores_previous_context() -> None:
    class Broken(Framework):
        async def ainvoke(self) -> object:
            raise RuntimeError("application failure")

    client = wrap(Broken(), settings=HajerSettings(capture_content=True, disabled=True))
    with pytest.raises(RuntimeError, match="application failure"):
        await workflow_alpha(client)
    assert "workflow_alpha" not in {frame.qualname for frame in frames_for_call()}

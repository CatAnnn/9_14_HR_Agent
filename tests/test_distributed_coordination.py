from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.redis.distributed_coordination import RedisDistributedCoordinator


@pytest.mark.asyncio
async def test_distributed_lease_releases_slot_when_request_is_cancelled():
    released: list[str] = []
    entered = asyncio.Event()

    class Coordinator(RedisDistributedCoordinator):
        async def _acquire(
            self,
            keys,
            *,
            token,
            capacity,
            timeout_seconds,
            priority,
        ):
            return True, 1, 0

        async def _heartbeat(self, lease_key, token):
            await asyncio.Event().wait()

        async def _release(self, keys, token):
            released.append(token)

    settings = SimpleNamespace(
        distributed_coordination_enabled=True,
        distributed_coordination_key_prefix="test:coordination",
    )
    coordinator = Coordinator(settings)

    async def worker() -> None:
        async with coordinator.lease("rag-db", capacity=2):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(released) == 1


@pytest.mark.asyncio
async def test_disabled_distributed_coordination_is_non_blocking():
    settings = SimpleNamespace(
        distributed_coordination_enabled=False,
        distributed_coordination_key_prefix="test:coordination",
    )
    coordinator = RedisDistributedCoordinator(settings)

    async with coordinator.lease("workflow-db", capacity=64) as slot:
        assert slot.distributed is False
        assert slot.active_count == 0
        assert slot.queue_depth == 0
    assert coordinator._keys("workflow-db")[-1].endswith(":sequence")


@pytest.mark.asyncio
async def test_distributed_lease_propagates_queue_priority():
    observed: list[int] = []

    class Coordinator(RedisDistributedCoordinator):
        async def _acquire(
            self,
            keys,
            *,
            token,
            capacity,
            timeout_seconds,
            priority,
        ):
            observed.append(priority)
            return True, 1, 0

        async def _heartbeat(self, lease_key, token):
            await asyncio.Event().wait()

        async def _release(self, keys, token):
            return None

    settings = SimpleNamespace(
        distributed_coordination_enabled=True,
        distributed_coordination_key_prefix="test:coordination",
    )
    coordinator = Coordinator(settings)

    async with coordinator.lease("rag-db", capacity=2, priority=10) as slot:
        assert slot.priority == 10

    assert observed == [10]

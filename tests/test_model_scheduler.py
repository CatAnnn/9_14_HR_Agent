from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.services.model_scheduler import (
    FairAsyncLimiter,
    ModelScheduler,
    current_model_endpoint,
    record_model_endpoint_attempt,
)


@pytest.mark.asyncio
async def test_fair_limiter_prioritizes_then_rotates_sessions():
    limiter = FairAsyncLimiter(1)
    await limiter.acquire(session_id="holder", priority=0, timeout_seconds=1)
    order: list[str] = []

    async def worker(session_id: str, label: str, priority: int) -> None:
        await limiter.acquire(
            session_id=session_id,
            priority=priority,
            timeout_seconds=1,
        )
        try:
            order.append(label)
            await asyncio.sleep(0)
        finally:
            await limiter.release()

    a1 = asyncio.create_task(worker("a", "a1", 2))
    await asyncio.sleep(0)
    a2 = asyncio.create_task(worker("a", "a2", 2))
    await asyncio.sleep(0)
    b1 = asyncio.create_task(worker("b", "b1", 2))
    await asyncio.sleep(0)
    priority = asyncio.create_task(worker("priority", "priority", 1))
    await asyncio.sleep(0)

    await limiter.release()
    await asyncio.gather(a1, a2, b1, priority)

    assert order == ["priority", "a1", "b1", "a2"]
    assert limiter.active_count == 0


@pytest.mark.asyncio
async def test_model_scheduler_cancellation_releases_granted_slot():
    settings = SimpleNamespace(
        coach_model_max_concurrency=1,
        model_queue_timeout_seconds=0.1,
    )
    scheduler = ModelScheduler(settings)
    acquired = asyncio.Event()

    async def holder() -> None:
        async with scheduler.slot(
            session_id="a",
            category="coach",
            endpoint="endpoint",
            model="model",
        ):
            acquired.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(holder())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with scheduler.slot(
        session_id="b",
        category="coach",
        endpoint="endpoint",
        model="model",
    ):
        pass
    limiter = scheduler._coach_limiters[("endpoint", "model")]
    assert limiter.active_count == 0


@pytest.mark.asyncio
async def test_model_scheduler_routes_away_from_slow_failing_endpoint():
    settings = SimpleNamespace(
        chat_urls=("https://model-a.example/chat", "https://model-b.example/chat"),
        coach_model_max_concurrency=28,
        model_total_max_concurrency=0,
        model_queue_timeout_seconds=1,
    )
    scheduler = ModelScheduler(settings)

    async with scheduler.slot(
        session_id="first",
        category="guidance",
        endpoint=settings.chat_urls[0],
        model="model",
    ) as first:
        assert first.endpoint == settings.chat_urls[0]
        assert current_model_endpoint() == first.endpoint
        record_model_endpoint_attempt(
            first.endpoint,
            duration_ms=20_000,
            success=False,
        )

    async with scheduler.slot(
        session_id="second",
        category="guidance",
        endpoint=settings.chat_urls[0],
        model="model",
    ) as second:
        assert second.endpoint == settings.chat_urls[1]
        assert current_model_endpoint() == second.endpoint

    assert current_model_endpoint() == ""
    assert all(state.active == 0 for state in scheduler._endpoint_states.values())


@pytest.mark.asyncio
async def test_model_scheduler_enforces_coach_cap_and_separates_queue_timeout():
    settings = SimpleNamespace(
        coach_model_max_concurrency=2,
        model_queue_timeout_seconds=0.02,
    )
    scheduler = ModelScheduler(settings)
    active = 0
    peak = 0
    release = asyncio.Event()

    async def worker(session_id: str) -> None:
        nonlocal active, peak
        async with scheduler.slot(
            session_id=session_id,
            category="coach",
            endpoint="endpoint",
            model="model",
        ):
            active += 1
            peak = max(peak, active)
            try:
                await release.wait()
            finally:
                active -= 1

    first = asyncio.create_task(worker("a"))
    second = asyncio.create_task(worker("b"))
    await asyncio.sleep(0.01)
    with pytest.raises(TimeoutError):
        async with scheduler.slot(
            session_id="c",
            category="coach",
            endpoint="endpoint",
            model="model",
        ):
            pass
    release.set()
    await asyncio.gather(first, second)

    assert peak == 2
    async with scheduler.slot(
        session_id="d",
        category="coach",
        endpoint="endpoint",
        model="model",
    ):
        await asyncio.sleep(0.04)


@pytest.mark.asyncio
async def test_model_scheduler_optional_total_cap_covers_all_categories():
    settings = SimpleNamespace(
        coach_model_max_concurrency=28,
        model_total_max_concurrency=2,
        model_queue_timeout_seconds=1,
    )
    scheduler = ModelScheduler(settings)
    active = 0
    peak = 0
    entered = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def worker(session_id: str, category: str) -> None:
        nonlocal active, peak, entered
        async with scheduler.slot(
            session_id=session_id,
            category=category,
            endpoint="endpoint",
            model="model",
        ):
            active += 1
            peak = max(peak, active)
            entered += 1
            if entered == 2:
                all_entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

    tasks = [
        asyncio.create_task(worker("a", "interactive")),
        asyncio.create_task(worker("b", "guidance")),
        asyncio.create_task(worker("c", "coach")),
    ]
    await asyncio.wait_for(all_entered.wait(), timeout=1)
    await asyncio.sleep(0.01)
    assert entered == 2
    release.set()
    await asyncio.gather(*tasks)

    assert peak == 2
    assert scheduler._global_limiters[("endpoint", "model")].active_count == 0


@pytest.mark.asyncio
async def test_final_generation_precedes_optional_evidence_for_same_model():
    settings = SimpleNamespace(
        coach_model_max_concurrency=2,
        model_total_max_concurrency=1,
        model_queue_timeout_seconds=1,
    )
    scheduler = ModelScheduler(settings)
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    order: list[str] = []

    async def holder():
        async with scheduler.slot(
            session_id="holder",
            category="interactive",
            endpoint="endpoint",
            model="model",
        ):
            holder_entered.set()
            await release_holder.wait()

    async def waiter(label: str, category: str):
        async with scheduler.slot(
            session_id="same-session",
            category=category,
            endpoint="endpoint",
            model="model",
        ):
            order.append(label)

    active = asyncio.create_task(holder())
    await holder_entered.wait()
    evidence = asyncio.create_task(
        waiter("evidence", "guidance_evidence")
    )
    await asyncio.sleep(0)
    final = asyncio.create_task(waiter("final", "guidance"))
    await asyncio.sleep(0)
    release_holder.set()
    await asyncio.gather(active, evidence, final)

    assert order == ["final", "evidence"]


@pytest.mark.asyncio
async def test_model_scheduler_cancellation_releases_category_slot_while_global_waits():
    settings = SimpleNamespace(
        coach_model_max_concurrency=1,
        model_total_max_concurrency=1,
        model_queue_timeout_seconds=1,
    )
    scheduler = ModelScheduler(settings)
    global_holder_entered = asyncio.Event()
    release = asyncio.Event()

    async def global_holder() -> None:
        async with scheduler.slot(
            session_id="interactive",
            category="interactive",
            endpoint="endpoint",
            model="model",
        ):
            global_holder_entered.set()
            await release.wait()

    holder = asyncio.create_task(global_holder())
    await global_holder_entered.wait()
    waiting_coach = asyncio.create_task(
        scheduler.slot(
            session_id="coach",
            category="coach",
            endpoint="endpoint",
            model="model",
        ).__aenter__()
    )
    await asyncio.sleep(0.01)
    waiting_coach.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_coach

    assert scheduler._coach_limiters[("endpoint", "model")].active_count == 0
    release.set()
    await holder

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.api.routes.health import autoscaling_workload
from backend.observability.user_activity_middleware import (
    ActiveWorkloadTracker,
    UserActivityMiddleware,
    active_workload_tracker,
)


async def _discard(_message: dict[str, object]) -> None:
    return None


def test_active_workload_tracker_is_thread_safe() -> None:
    tracker = ActiveWorkloadTracker()

    def cycle() -> None:
        for _ in range(1_000):
            tracker.enter("http")
            tracker.enter("websocket")
            tracker.exit("websocket")
            tracker.exit("http")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: cycle(), range(8)))

    snapshot = tracker.snapshot()
    assert snapshot["active_http_requests"] == 0
    assert snapshot["active_websockets"] == 0
    assert snapshot["idle_for_seconds"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["http", "websocket"])
async def test_middleware_tracks_full_asgi_lifetime_and_releases_on_error(
    protocol: str,
) -> None:
    tracker = ActiveWorkloadTracker()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def downstream(_scope, _receive, _send) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("connection ended")

    middleware = UserActivityMiddleware(downstream, workload_tracker=tracker)
    scope = {"type": protocol, "path": "/api/v1/rehearsal/stream"}
    if protocol == "http":
        scope["method"] = "POST"

    task = asyncio.create_task(middleware(scope, None, _discard))
    await entered.wait()

    expected = {
        "active_http_requests": int(protocol == "http"),
        "active_websockets": int(protocol == "websocket"),
        "idle_for_seconds": 0.0,
    }
    assert tracker.snapshot() == expected

    release.set()
    with pytest.raises(RuntimeError, match="connection ended"):
        await task
    snapshot = tracker.snapshot()
    assert snapshot["active_http_requests"] == 0
    assert snapshot["active_websockets"] == 0
    assert snapshot["idle_for_seconds"] >= 0


@pytest.mark.asyncio
async def test_autoscaling_probe_is_excluded_from_its_own_count() -> None:
    tracker = ActiveWorkloadTracker()
    observed: dict[str, int] = {}

    async def downstream(_scope, _receive, _send) -> None:
        observed.update(tracker.snapshot())

    middleware = UserActivityMiddleware(downstream, workload_tracker=tracker)
    await middleware(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/health/autoscaling",
        },
        None,
        _discard,
    )

    assert observed == {
        "active_http_requests": 0,
        "active_websockets": 0,
        "idle_for_seconds": pytest.approx(observed["idle_for_seconds"]),
    }
    snapshot = tracker.snapshot()
    assert snapshot["active_http_requests"] == 0
    assert snapshot["active_websockets"] == 0
    assert snapshot["idle_for_seconds"] >= observed["idle_for_seconds"]


def test_autoscaling_probe_exposes_only_workload_counts() -> None:
    active_workload_tracker.enter("http")
    active_workload_tracker.enter("websocket")
    try:
        assert autoscaling_workload() == {
            "active_http_requests": 1,
            "active_websockets": 1,
            "idle_for_seconds": 0.0,
        }
    finally:
        active_workload_tracker.exit("websocket")
        active_workload_tracker.exit("http")

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from backend.services.executor_utils import (
    run_db_with_context,
    run_sync_with_context,
)


@pytest.mark.asyncio
async def test_shared_executor_bridge_preserves_request_context() -> None:
    request_value = contextvars.ContextVar("request_value", default="missing")
    token = request_value.set("owner-a")
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        value = await run_sync_with_context(executor, request_value.get)
    finally:
        request_value.reset(token)
        executor.shutdown(wait=True)

    assert value == "owner-a"


@pytest.mark.asyncio
async def test_database_bridge_runs_off_event_loop_thread() -> None:
    event_loop_thread = threading.get_ident()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        worker_thread = await run_db_with_context(
            executor,
            "test.thread_identity",
            threading.get_ident,
        )
    finally:
        executor.shutdown(wait=True)

    assert worker_thread != event_loop_thread


@pytest.mark.asyncio
async def test_database_bridge_cancellation_does_not_leak_executor_worker() -> None:
    started = threading.Event()
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)

    def blocking_database_call() -> None:
        started.set()
        release.wait(timeout=2)

    task = asyncio.create_task(
        run_db_with_context(
            executor,
            "test.cancelled",
            blocking_database_call,
        )
    )
    try:
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        marker = await run_sync_with_context(executor, lambda: "released")
    finally:
        release.set()
        executor.shutdown(wait=True)

    assert marker == "released"

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
import contextvars
from functools import partial
import time
from typing import ParamSpec, TypeVar

from backend.observability.metrics import (
    log_metric,
    record_workflow_db_operation,
)
from backend.config.settings import get_settings
from backend.redis.distributed_coordination import get_distributed_coordinator


P = ParamSpec("P")
R = TypeVar("R")


async def run_sync_with_context(
    executor: Executor | None,
    function: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Run synchronous work on an executor while preserving request context."""

    context = contextvars.copy_context()
    call = partial(function, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(
        executor,
        context.run,
        call,
    )


async def run_db_with_context(
    executor: Executor | None,
    operation: str,
    function: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Run database work off-loop and expose executor queue and execution time."""

    context = contextvars.copy_context()
    call = partial(function, *args, **kwargs)
    queued_at = time.perf_counter()
    timings: dict[str, float] = {}

    def invoke() -> R:
        timings["started_at"] = time.perf_counter()
        try:
            return context.run(call)
        finally:
            timings["finished_at"] = time.perf_counter()

    outcome = "success"
    try:
        settings = get_settings()
        coordinator = get_distributed_coordinator(settings)
        async with coordinator.lease(
            "workflow-db",
            capacity=settings.workflow_global_db_max_concurrency,
        ):
            return await asyncio.get_running_loop().run_in_executor(executor, invoke)
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        observed_at = time.perf_counter()
        started_at = timings.get("started_at", observed_at)
        finished_at = timings.get("finished_at", observed_at)
        queue_ms = max(0.0, (started_at - queued_at) * 1000)
        duration_ms = max(0.0, (finished_at - started_at) * 1000)
        record_workflow_db_operation(
            operation,
            queue_ms=queue_ms,
            duration_ms=duration_ms,
            outcome=outcome,
        )
        log_metric(
            "workflow.db",
            db_operation=operation,
            db_executor_queue_ms=round(queue_ms, 2),
            db_operation_ms=round(duration_ms, 2),
            outcome=outcome,
        )

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar, cast


MODEL_RETRY_RACE_WIDTH = 5
MODEL_TRANSIENT_RETRY_DELAYS_SECONDS = (0.0, 1.0, 3.0, 7.0, 15.0)

ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class ModelRaceResult(Generic[ResultT]):
    value: ResultT
    winner_index: int
    failed_count: int
    canceled_count: int


class ModelRetryRaceExhausted(Exception):
    def __init__(self, errors: list[Exception]):
        self.errors = tuple(errors)
        super().__init__(f"All {len(errors)} model retry candidates failed.")


async def first_valid_model_result(
    attempt: Callable[[int], Awaitable[ResultT]],
    *,
    width: int = MODEL_RETRY_RACE_WIDTH,
    start_delays_seconds: Sequence[float] | None = None,
) -> ModelRaceResult[ResultT]:
    """Return the first successful candidate and cancel unfinished losers."""
    if width < 1:
        raise ValueError("Model retry race width must be at least 1.")
    delays = tuple(start_delays_seconds or ())
    if delays and len(delays) != width:
        raise ValueError("Model retry race delays must match the configured width.")
    if any(delay < 0 for delay in delays):
        raise ValueError("Model retry race delays must not be negative.")

    async def run_candidate(
        candidate_index: int,
    ) -> tuple[int, ResultT | None, Exception | None]:
        try:
            if delays:
                delay_seconds = delays[candidate_index - 1]
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
            return candidate_index, await attempt(candidate_index), None
        except Exception as exc:  # noqa: BLE001
            return candidate_index, None, exc

    tasks = [
        asyncio.create_task(
            run_candidate(candidate_index),
            name=f"model-retry-race-{candidate_index}",
        )
        for candidate_index in range(1, width + 1)
    ]
    errors: list[Exception] = []
    try:
        for completed in asyncio.as_completed(tasks):
            candidate_index, value, error = await completed
            if error is not None:
                errors.append(error)
                continue

            canceled_count = sum(not task.done() for task in tasks)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return ModelRaceResult(
                value=cast(ResultT, value),
                winner_index=candidate_index,
                failed_count=len(errors),
                canceled_count=canceled_count,
            )
        raise ModelRetryRaceExhausted(errors)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

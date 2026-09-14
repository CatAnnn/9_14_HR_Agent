from __future__ import annotations

import asyncio
import math
import time


class SpeechPlaybackBuffer:
    """Pause sentence admission while fresh browser feedback reports ample audio."""

    def __init__(
        self,
        high_water_seconds: float = 8.0,
        low_water_seconds: float = 4.0,
        feedback_timeout_seconds: float = 3.0,
    ) -> None:
        self.validate_limits(
            high_water_seconds, low_water_seconds, feedback_timeout_seconds
        )
        self.high_water_seconds = high_water_seconds
        self.low_water_seconds = low_water_seconds
        self.feedback_timeout_seconds = feedback_timeout_seconds
        self._last_sequence = -1
        self._updated_at: float | None = None
        self._paused = False
        self._cancelled = False
        self._changed = asyncio.Event()

    @staticmethod
    def validate_limits(high: float, low: float, timeout: float) -> None:
        if (
            not all(math.isfinite(value) for value in (high, low, timeout))
            or not 0 <= low < high <= 120
            or timeout <= 0
        ):
            raise ValueError("Playback limits require 0 <= low < high <= 120 and a positive timeout.")

    def report(self, buffered_seconds: object, sequence: object) -> bool:
        if (
            self._cancelled
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
            or sequence <= self._last_sequence
            or not isinstance(buffered_seconds, (int, float))
            or isinstance(buffered_seconds, bool)
        ):
            return False
        try:
            buffered = float(buffered_seconds)
        except (OverflowError, ValueError):
            return False
        if not math.isfinite(buffered) or not 0 <= buffered <= 120:
            return False

        now = time.monotonic()
        if (
            self._updated_at is None
            or now - self._updated_at >= self.feedback_timeout_seconds
        ):
            self._paused = False
        self._last_sequence = sequence
        self._updated_at = now
        if buffered >= self.high_water_seconds:
            self._paused = True
        elif buffered <= self.low_water_seconds:
            self._paused = False
        self._changed.set()
        return True

    def cancel(self) -> None:
        self._cancelled = True
        self._changed.set()

    async def wait_for_capacity(self) -> None:
        """Wait outside the inference lease; missing or expired feedback admits work."""
        while True:
            if self._cancelled:
                raise asyncio.CancelledError
            if not self._paused or self._updated_at is None:
                return
            remaining = self.feedback_timeout_seconds - (
                time.monotonic() - self._updated_at
            )
            if remaining <= 0:
                self._paused = False
                return
            self._changed.clear()
            try:
                async with asyncio.timeout(remaining):
                    await self._changed.wait()
            except TimeoutError:
                # Recheck freshness in case feedback arrived at the deadline.
                continue

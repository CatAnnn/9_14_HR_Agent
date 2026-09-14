from __future__ import annotations

import time


_last_bosch_http_activity = time.monotonic()


def mark_bosch_http_asr_activity() -> None:
    """Record real or keepwarm traffic without retaining request content."""

    global _last_bosch_http_activity
    _last_bosch_http_activity = time.monotonic()


def bosch_http_asr_idle_seconds() -> float:
    return max(0.0, time.monotonic() - _last_bosch_http_activity)

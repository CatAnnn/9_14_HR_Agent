from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

from loadtests import MAX_USERS_PER_WORKER, MAX_WORKFLOW_USERS


@dataclass(frozen=True, slots=True)
class ShapeStage:
    users: int
    duration_seconds: int
    spawn_rate: float


def required_workers(users: int, *, max_users_per_worker: int = MAX_USERS_PER_WORKER) -> int:
    if users < 1 or max_users_per_worker < 1:
        raise ValueError("users and max_users_per_worker must be positive")
    return math.ceil(users / max_users_per_worker)


def build_shape(profile: str, values: Mapping[str, str] | None = None) -> tuple[ShapeStage, ...]:
    env = values or {}
    normalized = profile.strip().lower()
    if normalized == "smoke":
        return (ShapeStage(1, int(env.get("LOADTEST_SMOKE_SECONDS", "900")), 1.0),)
    if normalized == "staircase":
        regular = int(env.get("LOADTEST_STAIR_STEP_SECONDS", "300"))
        final = max(1800, int(env.get("LOADTEST_STAIR_400_SECONDS", "1800")))
        users = (1, 10, 30, 60, 100, 200, 300, 400)
        return tuple(
            ShapeStage(user_count, final if user_count == 400 else regular, max(1.0, user_count / 30.0))
            for user_count in users
        )
    if normalized == "spike":
        ramp = int(env.get("LOADTEST_SPIKE_RAMP_SECONDS", "60"))
        hold = int(env.get("LOADTEST_SPIKE_HOLD_SECONDS", "900"))
        return (
            ShapeStage(50, ramp, 50.0 / max(1, ramp)),
            ShapeStage(400, hold, 350.0 / max(1, ramp)),
        )
    if normalized == "soak":
        return (
            ShapeStage(
                400,
                int(env.get("LOADTEST_SOAK_SECONDS", "3600")),
                float(env.get("LOADTEST_SOAK_SPAWN_RATE", "20")),
            ),
        )
    raise ValueError("LOADTEST_SHAPE must be one of smoke, staircase, spike, or soak")


def validate_shape(stages: tuple[ShapeStage, ...]) -> None:
    if not stages:
        raise ValueError("shape must contain at least one stage")
    if max(stage.users for stage in stages) > MAX_WORKFLOW_USERS:
        raise ValueError(f"shape exceeds the {MAX_WORKFLOW_USERS}-user safety ceiling")
    if any(stage.users < 1 or stage.duration_seconds < 1 or stage.spawn_rate <= 0 for stage in stages):
        raise ValueError("shape stages must use positive users, duration, and spawn rate")


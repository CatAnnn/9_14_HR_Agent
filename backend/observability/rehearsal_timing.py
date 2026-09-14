from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import logging
import time
import uuid
from typing import Any

from opentelemetry import trace as otel_trace

from backend.core.session_context import get_current_observability_user_id
from backend.observability.metrics import record_rehearsal_turn_timing


logger = logging.getLogger(__name__)
_TRACER = otel_trace.get_tracer("hr_agent.rehearsal")


@dataclass(slots=True)
class _StageToken:
    name: str
    started_at: float
    parallel_group: str | None = None
    finished: bool = False


class RehearsalTurnTiming:
    """Collect one rehearsal turn as a monotonic, concurrency-aware waterfall."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        session_id: str,
        transport: str,
        speech_enabled: bool,
        explicit_thinking_enabled: bool,
        attempt_id: str | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._clock = clock
        self._started_at = clock()
        self._session_hash = hashlib.sha256(
            str(session_id).encode("utf-8")
        ).hexdigest()[:16]
        self.attempt_id = attempt_id or uuid.uuid4().hex
        self.transport = str(transport)
        self.speech_enabled = bool(speech_enabled)
        self.explicit_thinking_enabled = bool(explicit_thinking_enabled)
        self.user_id = get_current_observability_user_id()
        self.manager_turn_index: int | None = None
        self.employee_turn_index: int | None = None
        self._milestones_ms: dict[str, int] = {}
        self._stages: list[dict[str, Any]] = []
        self._final_payload: dict[str, Any] | None = None
        self._root_span: Any = None

    @property
    def finalized(self) -> bool:
        return self._final_payload is not None

    @contextmanager
    def trace(self) -> Iterator[None]:
        attributes = {
            "hr_agent.user.id": self.user_id,
            "hr_agent.rehearsal.attempt_id": self.attempt_id,
            "hr_agent.rehearsal.session_hash": self._session_hash,
            "hr_agent.rehearsal.transport": self.transport,
            "hr_agent.rehearsal.speech_enabled": self.speech_enabled,
        }
        with _TRACER.start_as_current_span(
            "rehearsal.turn",
            attributes=attributes,
        ) as span:
            self._root_span = span
            try:
                yield
            finally:
                self._root_span = None

    def set_turn_indexes(
        self,
        *,
        manager_turn_index: int,
        employee_turn_index: int,
    ) -> None:
        self.manager_turn_index = int(manager_turn_index)
        self.employee_turn_index = int(employee_turn_index)
        if self._root_span is not None and self._root_span.is_recording():
            self._root_span.set_attribute(
                "hr_agent.rehearsal.manager_turn_index",
                self.manager_turn_index,
            )
            self._root_span.set_attribute(
                "hr_agent.rehearsal.employee_turn_index",
                self.employee_turn_index,
            )

    def start_stage(
        self,
        name: str,
        *,
        parallel_group: str | None = None,
    ) -> _StageToken:
        return _StageToken(
            name=str(name),
            started_at=self._clock(),
            parallel_group=parallel_group,
        )

    def finish_stage(
        self,
        token: _StageToken,
        *,
        outcome: str = "success",
    ) -> int:
        if token.finished:
            for stage in reversed(self._stages):
                if stage.get("_token") is token:
                    return int(stage["duration_ms"])
            return 0
        token.finished = True
        finished_at = self._clock()
        duration_ms = self._duration_ms(token.started_at, finished_at)
        stage = {
            "name": token.name,
            "start_offset_ms": self._offset_ms(token.started_at),
            "end_offset_ms": self._offset_ms(finished_at),
            "duration_ms": duration_ms,
            "outcome": str(outcome),
            "_token": token,
        }
        if token.parallel_group:
            stage["parallel_group"] = token.parallel_group
        self._stages.append(stage)
        return duration_ms

    @contextmanager
    def stage(
        self,
        name: str,
        *,
        parallel_group: str | None = None,
    ) -> Iterator[Any]:
        token = self.start_stage(name, parallel_group=parallel_group)
        span_attributes = {
            "hr_agent.rehearsal.operation": token.name,
            "hr_agent.rehearsal.attempt_id": self.attempt_id,
        }
        if parallel_group:
            span_attributes["hr_agent.rehearsal.parallel_group"] = parallel_group
        outcome = "success"
        with _TRACER.start_as_current_span(
            f"rehearsal.{token.name}",
            attributes=span_attributes,
        ) as span:
            try:
                yield span
            except BaseException as exc:  # includes cancellation/closed streams
                outcome = (
                    "cancelled"
                    if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                    else "error"
                )
                raise
            finally:
                duration_ms = self.finish_stage(token, outcome=outcome)
                if span.is_recording():
                    span.set_attribute(
                        "hr_agent.rehearsal.operation.duration_ms",
                        duration_ms,
                    )
                    span.set_attribute(
                        "hr_agent.rehearsal.operation.outcome",
                        outcome,
                    )

    def record_duration(
        self,
        name: str,
        duration_ms: float | int | None,
        *,
        outcome: str = "success",
        parallel_group: str | None = None,
    ) -> None:
        if duration_ms is None:
            return
        duration = max(0, int(round(float(duration_ms))))
        end_offset = self._offset_ms(self._clock())
        stage: dict[str, Any] = {
            "name": str(name),
            "start_offset_ms": max(0, end_offset - duration),
            "end_offset_ms": end_offset,
            "duration_ms": duration,
            "outcome": str(outcome),
        }
        if parallel_group:
            stage["parallel_group"] = parallel_group
        self._stages.append(stage)

    def record_skipped(self, name: str) -> None:
        offset = self._offset_ms(self._clock())
        self._stages.append(
            {
                "name": str(name),
                "start_offset_ms": offset,
                "end_offset_ms": offset,
                "duration_ms": 0,
                "outcome": "skipped",
            }
        )

    def mark_milestone(self, name: str) -> int:
        key = str(name)
        if key not in self._milestones_ms:
            self._milestones_ms[key] = self._offset_ms(self._clock())
        return self._milestones_ms[key]

    def snapshot(self) -> dict[str, Any]:
        stages = [
            {key: value for key, value in stage.items() if key != "_token"}
            for stage in sorted(
                self._stages,
                key=lambda item: (
                    int(item["start_offset_ms"]),
                    int(item["end_offset_ms"]),
                    str(item["name"]),
                ),
            )
        ]
        return {
            "schema_version": self.SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "transport": self.transport,
            "speech_enabled": self.speech_enabled,
            "explicit_thinking_enabled": self.explicit_thinking_enabled,
            # Providers do not currently expose a trustworthy reasoning boundary.
            "explicit_thinking_ms": None,
            "manager_turn_index": self.manager_turn_index,
            "employee_turn_index": self.employee_turn_index,
            "milestones_ms": dict(sorted(self._milestones_ms.items())),
            "summary_ms": self._summary_ms(),
            "stages": stages,
        }

    def finalize(self, outcome: str) -> dict[str, Any]:
        if self._final_payload is not None:
            return self._final_payload
        self.mark_milestone("done")
        payload = {**self.snapshot(), "outcome": str(outcome)}
        self._final_payload = payload
        summary = payload["summary_ms"]
        if self._root_span is not None and self._root_span.is_recording():
            self._root_span.set_attribute(
                "hr_agent.rehearsal.outcome",
                str(outcome),
            )
            for name, value in summary.items():
                self._root_span.set_attribute(
                    f"hr_agent.rehearsal.{name}",
                    int(value),
                )
        try:
            record_rehearsal_turn_timing(
                user_id=self.user_id,
                attempt_id=self.attempt_id,
                session_hash=self._session_hash,
                manager_turn_index=self.manager_turn_index,
                employee_turn_index=self.employee_turn_index,
                transport=self.transport,
                speech_enabled=self.speech_enabled,
                outcome=str(outcome),
                stages=payload["stages"],
                summary_ms=summary,
            )
        except Exception:  # observability must never break a rehearsal turn
            logger.exception("Failed to record rehearsal turn timing")
        return payload

    def _summary_ms(self) -> dict[str, int]:
        milestones = self._milestones_ms
        total = int(milestones.get("done", self._offset_ms(self._clock())))
        generating = milestones.get("generating")
        first_visible = milestones.get("first_visible_delta")
        text_complete = milestones.get("text_complete")
        summary: dict[str, int] = {"total_ms": max(0, total)}
        if generating is not None:
            summary["analysis_and_preparation_ms"] = max(0, int(generating))
        if first_visible is not None:
            summary["wait_for_first_visible_reply_ms"] = max(
                0,
                int(first_visible),
            )
            if generating is not None:
                summary["generation_to_first_visible_ms"] = max(
                    0,
                    int(first_visible) - int(generating),
                )
        if text_complete is not None:
            summary["text_total_ms"] = max(0, int(text_complete))
            summary["postprocess_ms"] = max(0, total - int(text_complete))
            if first_visible is not None:
                summary["visible_reply_stream_ms"] = max(
                    0,
                    int(text_complete) - int(first_visible),
                )
        first_audio = milestones.get("first_audio")
        if first_audio is not None:
            summary["wait_for_first_audio_ms"] = max(0, int(first_audio))
        return summary

    def _offset_ms(self, timestamp: float) -> int:
        return self._duration_ms(self._started_at, timestamp)

    @staticmethod
    def _duration_ms(started_at: float, finished_at: float) -> int:
        return max(0, int(round((finished_at - started_at) * 1000)))

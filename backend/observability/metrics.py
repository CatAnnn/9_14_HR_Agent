from __future__ import annotations

import json
import logging
from logging.handlers import QueueHandler, QueueListener
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace

from backend.core.session_context import get_current_observability_user_id


class ActiveUserTracker:
    """Keep a bounded process-local view of recently authenticated users."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark_seen(self, user_identifier: str) -> None:
        now = self._clock()
        with self._lock:
            self._last_seen[user_identifier] = now
            self._prune_locked(now)

    def count(self, window_seconds: int) -> int:
        cutoff = self._clock() - max(1, int(window_seconds))
        with self._lock:
            return sum(last_seen >= cutoff for last_seen in self._last_seen.values())

    def observations(self, _options: Any) -> Iterable[otel_metrics.Observation]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            last_seen_values = tuple(self._last_seen.values())
        for window_name, window_seconds in (("5m", 300), ("15m", 900)):
            cutoff = now - window_seconds
            yield otel_metrics.Observation(
                sum(last_seen >= cutoff for last_seen in last_seen_values),
                {"hr_agent.activity.window": window_name},
            )

    def _prune_locked(self, now: float) -> None:
        retention_cutoff = now - 86_400
        stale = [
            user_id
            for user_id, last_seen in self._last_seen.items()
            if last_seen < retention_cutoff
        ]
        for user_id in stale:
            self._last_seen.pop(user_id, None)


_LOGGER_NAME = "backend.metrics"
_METRICS_QUEUE_MAX_SIZE = 8192
_metrics_logger_lock = threading.Lock()
_metrics_listener: QueueListener | None = None
_metrics_queue_handler: QueueHandler | None = None
_metrics_stream_handler: logging.StreamHandler | None = None
_METER = otel_metrics.get_meter("hr_agent.observability")
_ACTIVE_USER_TRACKER = ActiveUserTracker()
_ACTIVE_USERS_GAUGE = _METER.create_observable_gauge(
    "hr_agent.user.active",
    callbacks=[_ACTIVE_USER_TRACKER.observations],
    unit="{user}",
    description="Authenticated users active in a rolling process-local window.",
)
_USER_REQUEST_COUNTER = _METER.create_counter(
    "hr_agent.user.request",
    unit="{request}",
    description="Completed authenticated HTTP and WebSocket requests by email local-part.",
)
_USER_REQUEST_DURATION = _METER.create_histogram(
    "hr_agent.user.request.duration",
    unit="ms",
    description="Duration of authenticated HTTP and WebSocket requests.",
)
_REHEARSAL_TURN_COUNTER = _METER.create_counter(
    "hr_agent.rehearsal.turn",
    unit="{turn}",
    description="Completed rehearsal turns grouped by user and outcome.",
)
_REHEARSAL_TURN_DURATION = _METER.create_histogram(
    "hr_agent.rehearsal.turn.duration",
    unit="ms",
    description="Rehearsal turn operation and user-visible milestone durations.",
)
_EMOTION_TRANSITION_COUNTER = _METER.create_counter(
    "hr_agent.emotion.transition",
    unit="{transition}",
    description="Emotion transitions grouped by anchors, strategy, and visible strength.",
)
_EMOTION_STIMULUS_INTENSITY = _METER.create_histogram(
    "hr_agent.emotion.stimulus.intensity",
    unit="1",
    description="Dominant-axis intensity requested for an emotion transition.",
)
_EMOTION_ACTUAL_CHANGE = _METER.create_histogram(
    "hr_agent.emotion.actual_change",
    unit="1",
    description="Largest realized VAD-axis change after Markov constraints.",
)
_LLM_TOKEN_USAGE_COUNTER = _METER.create_counter(
    "hr_agent.llm.token.usage",
    unit="{token}",
    description="Cumulative input and output tokens consumed by LLM calls.",
)
_LLM_REQUEST_COUNTER = _METER.create_counter(
    "hr_agent.llm.request",
    unit="{request}",
    description="Cumulative LLM API calls grouped by model, task, and outcome.",
)
_LLM_REQUEST_DURATION = _METER.create_histogram(
    "hr_agent.llm.request.duration",
    unit="ms",
    description="End-to-end duration of one LLM API call.",
)
_LLM_COST_COUNTER = _METER.create_counter(
    "hr_agent.llm.cost",
    unit="CNY",
    description="LLM API cost calculated from configured per-model token prices.",
)
_LLM_PRICING_MISSING_COUNTER = _METER.create_counter(
    "hr_agent.llm.pricing.missing",
    unit="{request}",
    description="LLM API calls whose model has no configured token price.",
)
_STRUCTURED_OUTPUT_COUNTER = _METER.create_counter(
    "hr_agent.llm.structured_output",
    unit="{result}",
    description="Structured LLM results grouped by valid, recovered, and fallback outcomes.",
)
_STRUCTURED_OUTPUT_DURATION = _METER.create_histogram(
    "hr_agent.llm.structured_output.duration",
    unit="ms",
    description="End-to-end duration of one structured LLM task.",
)
_LLM_STREAM_MILESTONE_DURATION = _METER.create_histogram(
    "hr_agent.llm.stream.milestone.duration",
    unit="ms",
    description="Elapsed time from request start to streamed LLM milestones.",
)
_CONVERSATION_SUMMARY_COUNTER = _METER.create_counter(
    "hr_agent.conversation.summary.events",
    unit="{event}",
    description="Rolling conversation summary outcomes.",
)
_CONVERSATION_SUMMARY_DURATION = _METER.create_histogram(
    "hr_agent.conversation.summary.duration",
    unit="ms",
    description="Duration of rolling conversation summary updates.",
)
_CONVERSATION_SUMMARY_COVERAGE = _METER.create_histogram(
    "hr_agent.conversation.summary.covered_turn",
    unit="{turn}",
    description="Turn index covered by successful or rejected rolling summary writes.",
)
_RUNTIME_EVENT_LOOP_LAG = _METER.create_histogram(
    "hr_agent.runtime.event_loop.lag",
    unit="ms",
    description="Delay beyond the scheduled event-loop monitor tick.",
)
_WORKFLOW_DB_QUEUE_DURATION = _METER.create_histogram(
    "hr_agent.workflow.db.queue.duration",
    unit="ms",
    description="Time synchronous workflow database work waits for an executor thread.",
)
_WORKFLOW_DB_OPERATION_DURATION = _METER.create_histogram(
    "hr_agent.workflow.db.operation.duration",
    unit="ms",
    description="Execution time of synchronous workflow database work.",
)
_POSTGRES_POOL_WAIT_DURATION = _METER.create_histogram(
    "hr_agent.postgres.pool.wait.duration",
    unit="ms",
    description="Time spent waiting to acquire a PostgreSQL pooled connection.",
)
_HTTP_POOL_WAIT_DURATION = _METER.create_histogram(
    "hr_agent.http.pool.wait.duration",
    unit="ms",
    description="Time spent waiting for a shared HTTP client connection.",
)
_EBOOK_EVENT_COUNTER = _METER.create_counter(
    "hr_agent.ebook.event",
    unit="{event}",
    description="Authenticated ebook reader and download lifecycle events.",
)
_EBOOK_DURATION = _METER.create_histogram(
    "hr_agent.ebook.duration",
    unit="ms",
    description="Ebook reader readiness and page-change duration.",
)
_ASR_ACTIVE_COUNTER = _METER.create_up_down_counter(
    "hr_agent.asr.active",
    unit="{session}",
    description="Active ASR capture and preview sessions.",
)
_ASR_EVENT_COUNTER = _METER.create_counter(
    "hr_agent.asr.event",
    unit="{event}",
    description="ASR lifecycle and failure events.",
)
_ASR_DURATION = _METER.create_histogram(
    "hr_agent.asr.duration",
    unit="ms",
    description="ASR capture and final-transcription stage duration.",
)
_ASR_AUDIO_BYTES = _METER.create_counter(
    "hr_agent.asr.audio.bytes",
    unit="By",
    description="PCM audio bytes accepted by disk-backed ASR capture.",
)
_ASR_FINAL_SEGMENTS = _METER.create_histogram(
    "hr_agent.asr.final.segments",
    unit="{segment}",
    description="Number of remote ASR segments per completed recording.",
)
_ASR_DISK_FREE = _METER.create_histogram(
    "hr_agent.asr.disk.free",
    unit="By",
    description="Free bytes observed in the ASR temporary recording filesystem.",
)
_ASR_PREVIEW_WINDOW = _METER.create_histogram(
    "hr_agent.asr.preview.window",
    unit="s",
    description="Audio window duration sent to local ASR preview inference.",
)
_ASR_SILENCE_SKIP_COUNTER = _METER.create_counter(
    "hr_agent.asr.preview.silence_skip",
    unit="{skip}",
    description="Local ASR preview refreshes skipped because the current chunk is silent.",
)


class _LosslessQueueHandler(QueueHandler):
    def __init__(
        self,
        event_queue: queue.Queue[logging.LogRecord],
        fallback: logging.Handler,
    ) -> None:
        super().__init__(event_queue)
        self._fallback = fallback

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._fallback.handle(record)


class _FlushQueueListener(QueueListener):
    def enqueue_sentinel(self) -> None:
        self.queue.put(self._sentinel)


def metrics_logger() -> logging.Logger:
    global _metrics_listener, _metrics_queue_handler, _metrics_stream_handler

    logger = logging.getLogger(_LOGGER_NAME)
    with _metrics_logger_lock:
        if _metrics_listener is not None:
            return logger
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        event_queue: queue.Queue[logging.LogRecord] = queue.Queue(
            maxsize=_METRICS_QUEUE_MAX_SIZE
        )
        queue_handler = _LosslessQueueHandler(event_queue, stream_handler)
        listener = _FlushQueueListener(
            event_queue,
            stream_handler,
            respect_handler_level=True,
        )
        logger.handlers.clear()
        logger.addHandler(queue_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _metrics_stream_handler = stream_handler
        _metrics_queue_handler = queue_handler
        _metrics_listener = listener
        listener.start()
    return logger


def shutdown_metrics_logger() -> None:
    global _metrics_listener, _metrics_queue_handler, _metrics_stream_handler

    logger = logging.getLogger(_LOGGER_NAME)
    with _metrics_logger_lock:
        listener = _metrics_listener
        queue_handler = _metrics_queue_handler
        stream_handler = _metrics_stream_handler
        _metrics_listener = None
        _metrics_queue_handler = None
        _metrics_stream_handler = None
        if queue_handler is not None:
            logger.removeHandler(queue_handler)
    if listener is not None:
        listener.stop()
    if stream_handler is not None:
        stream_handler.flush()


def now_ms() -> float:
    return time.perf_counter()


def elapsed_ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))


def log_metric(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    metrics_logger().info(json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True))


def _current_user_attributes() -> dict[str, str]:
    return {"hr_agent.user.id": get_current_observability_user_id()}


def record_ebook_event(
    event: str,
    *,
    ebook_id: str,
    outcome: str,
    duration_ms: float | None = None,
    file_bytes: int | None = None,
    user_id: str | None = None,
) -> None:
    attributes = {
        **_current_user_attributes(),
        "hr_agent.ebook.id": str(ebook_id),
        "hr_agent.ebook.event": str(event),
        "hr_agent.ebook.outcome": str(outcome),
    }
    _EBOOK_EVENT_COUNTER.add(1, attributes=attributes)
    if duration_ms is not None:
        _EBOOK_DURATION.record(max(0.0, float(duration_ms)), attributes=attributes)
    log_metric(
        "ebook.event",
        ebook_id=ebook_id,
        ebook_event=event,
        outcome=outcome,
        duration_ms=duration_ms,
        file_bytes=file_bytes,
        user_id=user_id,
    )


def record_runtime_event_loop_lag(duration_ms: float) -> None:
    _RUNTIME_EVENT_LOOP_LAG.record(max(0.0, float(duration_ms)))


def record_workflow_db_operation(
    operation: str,
    *,
    queue_ms: float,
    duration_ms: float,
    outcome: str,
) -> None:
    attributes = {
        **_current_user_attributes(),
        "hr_agent.workflow.db.operation": str(operation),
        "hr_agent.workflow.db.outcome": str(outcome),
    }
    _WORKFLOW_DB_QUEUE_DURATION.record(
        max(0.0, float(queue_ms)),
        attributes=attributes,
    )
    _WORKFLOW_DB_OPERATION_DURATION.record(
        max(0.0, float(duration_ms)),
        attributes=attributes,
    )


def record_postgres_pool_wait(duration_ms: float, *, outcome: str) -> None:
    _POSTGRES_POOL_WAIT_DURATION.record(
        max(0.0, float(duration_ms)),
        attributes={
            **_current_user_attributes(),
            "hr_agent.postgres.pool.outcome": str(outcome),
        },
    )


def record_http_pool_wait(
    pool: str,
    duration_ms: float,
    *,
    outcome: str,
) -> None:
    _HTTP_POOL_WAIT_DURATION.record(
        max(0.0, float(duration_ms)),
        attributes={
            **_current_user_attributes(),
            "hr_agent.http.pool": str(pool),
            "hr_agent.http.pool.outcome": str(outcome),
        },
    )


def change_asr_active(kind: str, delta: int) -> None:
    attributes = {
        **_current_user_attributes(),
        "hr_agent.asr.kind": str(kind),
    }
    _ASR_ACTIVE_COUNTER.add(int(delta), attributes=attributes)


def record_asr_event(event: str, *, outcome: str = "success") -> None:
    attributes = {
        **_current_user_attributes(),
        "hr_agent.asr.event": str(event),
        "hr_agent.asr.outcome": str(outcome),
    }
    _ASR_EVENT_COUNTER.add(1, attributes=attributes)
    log_metric(
        "asr_event",
        asr_event=event,
        outcome=outcome,
        user_id=attributes["hr_agent.user.id"],
    )


def record_asr_duration(stage: str, duration_ms: float, *, outcome: str) -> None:
    attributes = {
        **_current_user_attributes(),
        "hr_agent.asr.stage": str(stage),
        "hr_agent.asr.outcome": str(outcome),
    }
    _ASR_DURATION.record(max(0.0, float(duration_ms)), attributes=attributes)


def record_asr_audio_bytes(byte_count: int) -> None:
    _ASR_AUDIO_BYTES.add(
        max(0, int(byte_count)),
        attributes=_current_user_attributes(),
    )


def record_asr_final_segments(segment_count: int) -> None:
    _ASR_FINAL_SEGMENTS.record(
        max(0, int(segment_count)),
        attributes=_current_user_attributes(),
    )


def record_asr_disk_free(free_bytes: int) -> None:
    _ASR_DISK_FREE.record(
        max(0, int(free_bytes)),
        attributes={"hr_agent.asr.storage": "temporary_recording"},
    )


def record_asr_preview_window(window_seconds: float) -> None:
    _ASR_PREVIEW_WINDOW.record(
        max(0.0, float(window_seconds)),
        attributes=_current_user_attributes(),
    )


def record_asr_silence_skip() -> None:
    _ASR_SILENCE_SKIP_COUNTER.add(
        1,
        attributes=_current_user_attributes(),
    )


def mark_authenticated_user_active() -> None:
    _ACTIVE_USER_TRACKER.mark_seen(get_current_observability_user_id())


def initialize_user_observability() -> None:
    """Publish an empty request series so a fresh dashboard has no missing metric."""

    _USER_REQUEST_COUNTER.add(
        0,
        attributes={
            "hr_agent.user.id": "system",
            "hr_agent.request.protocol": "http",
            "http.request.method": "BOOTSTRAP",
            "http.route": "bootstrap",
            "http.response.status_code": 0,
            "hr_agent.response.status_class": "unknown",
            "hr_agent.request.outcome": "success",
        },
    )


def record_authenticated_user_request(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_ms: int,
    protocol: str = "http",
) -> None:
    """Record one completed authenticated request using its email local-part."""

    user_identifier = get_current_observability_user_id()
    _ACTIVE_USER_TRACKER.mark_seen(user_identifier)
    normalized_status = max(0, int(status_code))
    status_class = f"{normalized_status // 100}xx" if normalized_status else "unknown"
    outcome = "error" if normalized_status >= 400 else "success"
    attributes: dict[str, Any] = {
        "hr_agent.user.id": user_identifier,
        "hr_agent.request.protocol": protocol or "unknown",
        "http.request.method": (method or "unknown").upper(),
        "http.route": route or "unknown",
        "http.response.status_code": normalized_status,
        "hr_agent.response.status_class": status_class,
        "hr_agent.request.outcome": outcome,
    }
    _USER_REQUEST_COUNTER.add(1, attributes=attributes)
    _USER_REQUEST_DURATION.record(max(0, int(duration_ms)), attributes=attributes)

    span = otel_trace.get_current_span()
    if span.is_recording():
        span.set_attribute("hr_agent.user.id", user_identifier)
        span.set_attribute("hr_agent.request.outcome", outcome)


def record_rehearsal_turn_timing(
    *,
    user_id: str,
    attempt_id: str,
    session_hash: str,
    manager_turn_index: int | None,
    employee_turn_index: int | None,
    transport: str,
    speech_enabled: bool,
    outcome: str,
    stages: list[dict[str, Any]],
    summary_ms: dict[str, int],
) -> None:
    """Export a low-cardinality turn histogram and one correlated waterfall log."""

    user_identifier = str(user_id or "anonymous")
    base_attributes: dict[str, Any] = {
        "hr_agent.user.id": user_identifier,
        "hr_agent.rehearsal.transport": transport or "unknown",
        "hr_agent.rehearsal.speech_enabled": bool(speech_enabled),
        "hr_agent.rehearsal.outcome": outcome or "unknown",
    }
    _REHEARSAL_TURN_COUNTER.add(1, attributes=base_attributes)
    for stage in stages:
        duration = stage.get("duration_ms")
        if duration is None:
            continue
        _REHEARSAL_TURN_DURATION.record(
            max(0, int(duration)),
            attributes={
                **base_attributes,
                "hr_agent.rehearsal.operation": str(
                    stage.get("name") or "unknown"
                ),
                "hr_agent.rehearsal.operation.outcome": str(
                    stage.get("outcome") or "unknown"
                ),
                "hr_agent.rehearsal.parallel_group": str(
                    stage.get("parallel_group") or "none"
                ),
            },
        )
    for name, duration in summary_ms.items():
        _REHEARSAL_TURN_DURATION.record(
            max(0, int(duration)),
            attributes={
                **base_attributes,
                "hr_agent.rehearsal.operation": str(name),
                "hr_agent.rehearsal.operation.outcome": outcome or "unknown",
                "hr_agent.rehearsal.parallel_group": "summary",
            },
        )

    log_metric(
        "rehearsal.turn",
        user_id=user_identifier,
        rehearsal_attempt_id=attempt_id,
        rehearsal_session_hash=session_hash,
        manager_turn_index=manager_turn_index,
        employee_turn_index=employee_turn_index,
        transport=transport,
        speech_enabled=bool(speech_enabled),
        outcome=outcome,
        summary_ms=summary_ms,
        stages=stages,
    )


def record_emotion_transition(
    *,
    source_anchor: str,
    target_anchor: str,
    result_anchor: str,
    strategy: str,
    stimulus_intensity: float,
    actual_change: float,
    anchor_changed: bool,
    strength: str,
) -> None:
    """Record transition behavior without manager or employee message content."""

    attributes = {
        **_current_user_attributes(),
        "hr_agent.emotion.source_anchor": source_anchor or "unknown",
        "hr_agent.emotion.target_anchor": target_anchor or "unknown",
        "hr_agent.emotion.result_anchor": result_anchor or "unknown",
        "hr_agent.emotion.strategy": strategy or "unknown",
        "hr_agent.emotion.anchor_changed": bool(anchor_changed),
        "hr_agent.emotion.strength": strength or "unknown",
    }
    bounded_stimulus = max(0.0, min(1.0, float(stimulus_intensity)))
    bounded_change = max(0.0, min(1.0, float(actual_change)))
    _EMOTION_TRANSITION_COUNTER.add(1, attributes=attributes)
    _EMOTION_STIMULUS_INTENSITY.record(bounded_stimulus, attributes=attributes)
    _EMOTION_ACTUAL_CHANGE.record(bounded_change, attributes=attributes)
    log_metric(
        "emotion.transition",
        user_id=attributes["hr_agent.user.id"],
        source_anchor=source_anchor,
        target_anchor=target_anchor,
        result_anchor=result_anchor,
        strategy=strategy,
        stimulus_intensity=round(bounded_stimulus, 4),
        actual_change=round(bounded_change, 4),
        anchor_changed=bool(anchor_changed),
        strength=strength,
    )

def record_llm_token_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    task_name: str,
    model_name: str,
    provider_name: str,
    stream: bool,
    request_outcome: str,
    input_estimated: bool,
    output_estimated: bool,
) -> None:
    """Export low-cardinality token counters through the configured OTel meter."""

    base_attributes = {
        **_current_user_attributes(),
        "hr_agent.task.name": task_name or "default",
        "gen_ai.request.model": model_name or "unknown",
        "gen_ai.provider.name": provider_name or "unknown",
        "hr_agent.stream": bool(stream),
        "hr_agent.request.outcome": request_outcome or "unknown",
    }
    for token_type, value, estimated in (
        ("input", input_tokens, input_estimated),
        ("output", output_tokens, output_estimated),
    ):
        token_count = max(0, int(value))
        if token_count == 0:
            continue
        _LLM_TOKEN_USAGE_COUNTER.add(
            token_count,
            attributes={
                **base_attributes,
                "gen_ai.token.type": token_type,
                "hr_agent.token.estimated": bool(estimated),
            },
        )


def record_llm_request(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    input_cost_cny: float | None,
    cached_input_cost_cny: float | None,
    output_cost_cny: float | None,
    task_name: str,
    model_name: str,
    provider_name: str,
    stream: bool,
    request_outcome: str,
    duration_ms: int,
    input_estimated: bool,
    output_estimated: bool,
    pricing_version: str,
    pricing_configured: bool,
) -> None:
    """Export request, latency, pricing coverage, and token-cost metrics."""

    base_attributes = {
        **_current_user_attributes(),
        "hr_agent.task.name": task_name or "default",
        "gen_ai.request.model": model_name or "unknown",
        "gen_ai.provider.name": provider_name or "unknown",
        "hr_agent.stream": bool(stream),
        "hr_agent.request.outcome": request_outcome or "unknown",
    }
    _LLM_REQUEST_COUNTER.add(1, attributes=base_attributes)
    _LLM_REQUEST_DURATION.record(max(0, int(duration_ms)), attributes=base_attributes)

    if not pricing_configured:
        _LLM_PRICING_MISSING_COUNTER.add(1, attributes=base_attributes)
    else:
        for token_type, cost, estimated in (
            ("input", input_cost_cny, input_estimated),
            ("cached_input", cached_input_cost_cny, False),
            ("output", output_cost_cny, output_estimated),
        ):
            if cost is None:
                continue
            _LLM_COST_COUNTER.add(
                max(0.0, float(cost)),
                attributes={
                    **base_attributes,
                    "gen_ai.token.type": token_type,
                    "hr_agent.cost.currency": "CNY",
                    "hr_agent.cost.estimated": bool(estimated),
                    "hr_agent.pricing.version": pricing_version or "unversioned",
                },
            )

    span = otel_trace.get_current_span()
    if span.is_recording():
        span.set_attribute("gen_ai.usage.input_tokens", max(0, int(input_tokens)))
        span.set_attribute(
            "gen_ai.usage.cached_input_tokens",
            max(0, int(cached_input_tokens)),
        )
        span.set_attribute("gen_ai.usage.output_tokens", max(0, int(output_tokens)))
        span.set_attribute(
            "hr_agent.llm.pricing.configured",
            bool(pricing_configured),
        )
        span.set_attribute(
            "hr_agent.llm.pricing.version",
            pricing_version or "unversioned",
        )
        if pricing_configured:
            total_cost = max(0.0, float(input_cost_cny or 0.0)) + max(
                0.0,
                float(cached_input_cost_cny or 0.0),
            ) + max(
                0.0,
                float(output_cost_cny or 0.0),
            )
            span.set_attribute("hr_agent.llm.cost.cny", total_cost)
            span.set_attribute("hr_agent.llm.cost.currency", "CNY")
            span.set_attribute(
                "hr_agent.llm.cost.estimated",
                bool(input_estimated or output_estimated),
            )


def record_llm_stream_timing(
    *,
    task_name: str,
    model_name: str,
    provider_name: str,
    ttft_ms: int | None,
    thinking_complete_ms: int | None,
    tool_call_complete_ms: int | None,
    complete: bool,
) -> None:
    """Export streamed LLM milestone timings and trace attributes."""

    base_attributes = {
        **_current_user_attributes(),
        "hr_agent.task.name": task_name or "default",
        "gen_ai.request.model": model_name or "unknown",
        "gen_ai.provider.name": provider_name or "unknown",
        "hr_agent.request.outcome": "success" if complete else "error",
    }
    milestones = (
        ("ttft", ttft_ms),
        ("thinking_complete", thinking_complete_ms),
        ("tool_call_complete", tool_call_complete_ms),
    )
    for milestone, value in milestones:
        if value is None:
            continue
        _LLM_STREAM_MILESTONE_DURATION.record(
            max(0, int(value)),
            attributes={
                **base_attributes,
                "hr_agent.llm.stream.milestone": milestone,
            },
        )

    span = otel_trace.get_current_span()
    if span.is_recording():
        span.set_attribute("hr_agent.llm.stream.complete", bool(complete))
        for milestone, value in milestones:
            if value is not None:
                span.set_attribute(
                    f"hr_agent.llm.stream.{milestone}_ms",
                    max(0, int(value)),
                )


def record_structured_output(
    *,
    task_name: str,
    schema_name: str,
    source: str,
    valid: bool,
    recovered: bool,
    repair_steps: list[str],
    fallback_reason: str,
    duration_ms: int,
) -> None:
    """Export low-cardinality structured-output metrics and detailed trace attributes."""

    if valid:
        outcome = "recovered" if recovered else "valid"
    else:
        outcome = "fallback"
    attributes = {
        **_current_user_attributes(),
        "hr_agent.task.name": task_name or "default",
        "hr_agent.structured_output.schema": schema_name or "unknown",
        "hr_agent.structured_output.source": source or "none",
        "hr_agent.structured_output.outcome": outcome,
        "hr_agent.structured_output.fallback_reason": fallback_reason or "none",
    }
    _STRUCTURED_OUTPUT_COUNTER.add(1, attributes=attributes)
    _STRUCTURED_OUTPUT_DURATION.record(max(0, int(duration_ms)), attributes=attributes)

    span = otel_trace.get_current_span()
    if span.is_recording():
        span.set_attribute("hr_agent.structured_output.recovered", bool(recovered))
        span.set_attribute("hr_agent.structured_output.repair_steps", ",".join(repair_steps) or "none")
        span.set_attribute("hr_agent.structured_output.source", source or "none")
        span.set_attribute("hr_agent.structured_output.fallback_reason", fallback_reason or "none")


def record_conversation_summary(
    *,
    status: str,
    duration_ms: int,
    trigger: str,
    is_compensation: bool,
    target_turn_index: int = 0,
    summarized_turn_count: int = 0,
) -> None:
    """Export low-cardinality rolling-summary metrics and trace attributes."""

    attributes = {
        **_current_user_attributes(),
        "hr_agent.task.name": "conversation_summary",
        "hr_agent.conversation_summary.status": status or "unknown",
        "hr_agent.conversation_summary.trigger": trigger or "unknown",
        "hr_agent.conversation_summary.compensation": bool(is_compensation),
    }
    _CONVERSATION_SUMMARY_COUNTER.add(1, attributes=attributes)
    _CONVERSATION_SUMMARY_DURATION.record(max(0, int(duration_ms)), attributes=attributes)
    if target_turn_index > 0:
        _CONVERSATION_SUMMARY_COVERAGE.record(
            int(target_turn_index),
            attributes=attributes,
        )

    span = otel_trace.get_current_span()
    if span.is_recording():
        span.set_attribute("hr_agent.conversation_summary.status", status or "unknown")
        span.set_attribute("hr_agent.conversation_summary.trigger", trigger or "unknown")
        span.set_attribute(
            "hr_agent.conversation_summary.compensation",
            bool(is_compensation),
        )
        span.set_attribute(
            "hr_agent.conversation_summary.covered_through_turn_index",
            max(0, int(target_turn_index)),
        )
        span.set_attribute(
            "hr_agent.conversation_summary.summarized_turn_count",
            max(0, int(summarized_turn_count)),
        )


def token_count_from_usage(usage: dict[str, Any] | None, *keys: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def cached_input_token_count_from_usage(usage: dict[str, Any] | None) -> int | None:
    """Read cached prompt tokens from common OpenAI-compatible usage shapes."""

    direct = token_count_from_usage(
        usage,
        "cached_input_tokens",
        "cache_read_input_tokens",
        "prompt_cache_hit_tokens",
    )
    if direct is not None:
        return direct
    if not isinstance(usage, dict):
        return None
    for details_key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(details_key)
        nested = token_count_from_usage(
            details,
            "cached_tokens",
            "cached_input_tokens",
        )
        if nested is not None:
            return nested
    return None


def estimate_tokens(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, len(stripped) // 4)

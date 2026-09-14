from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
import uuid

import requests
import websocket

from loadtests.accounts import LoadTestAccount, configured_accounts
from loadtests.asr_load import (
    AsrSessionResult,
    _receive_event,
    _record_event,
    _websocket_url,
)
from loadtests.cost import estimate_cost_cny, parse_rates
from loadtests.payloads import (
    MANAGER_MESSAGES,
    intent_request,
    profile_payload,
    simulation_payload,
)
from loadtests.sse import SSEParser, SSEProtocolError, StreamObservation


_ALLOWED_CONCURRENCY = (1, 10, 30, 60)
_TRUE_VALUES = {"1", "true", "yes", "on"}

# These are client-observable p95 targets, except for the explicitly named
# server-side model scheduler queue. "Smooth" is the desired experience;
# "acceptable" is the maximum production tolerance before the run is failed.
_UX_THRESHOLDS_MS: dict[str, tuple[float, float]] = {
    "asr_ready_wait": (1000.0, 2500.0),
    "asr_first_partial_wait": (1500.0, 3000.0),
    "asr_stop_ack_wait": (500.0, 1500.0),
    "asr_final_wait_after_stop": (2000.0, 5000.0),
    "rehearsal_startup_wait": (1000.0, 3000.0),
    "rehearsal_model_queue_wait": (1000.0, 5000.0),
    "rehearsal_first_visible_wait": (3000.0, 10000.0),
    "rehearsal_completion_wait": (15000.0, 30000.0),
    "joint_parallel_first_feedback_wait": (3000.0, 10000.0),
    "joint_parallel_completion_wait": (15000.0, 30000.0),
}

_UX_METRIC_DEFINITIONS = {
    "asr_ready_wait": "WebSocket connect to recording_ready",
    "asr_first_partial_wait": "audio capture start to first non-empty partial",
    "asr_stop_ack_wait": "client stop send to capture_stopped",
    "asr_final_wait_after_stop": "client stop send to final transcript",
    "rehearsal_startup_wait": "request start to first SSE start event",
    "rehearsal_model_queue_wait": (
        "server scheduler critical-path wait: max parallel analysis queue plus "
        "employee reply queue"
    ),
    "rehearsal_first_visible_wait": "request start to first visible reply delta",
    "rehearsal_completion_wait": "request start to terminal done event",
    "joint_parallel_first_feedback_wait": (
        "time until both independently started operations have visible feedback"
    ),
    "joint_parallel_completion_wait": (
        "time until both independently started operations complete, including "
        "the fixed 10-second audio capture"
    ),
}


class JointLoadError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class PreparedUser:
    account: LoadTestAccount
    session_id: str
    session_ref: str
    cookie: str
    http_session: requests.Session


@dataclass(slots=True)
class SetupRun:
    account_index: int
    duration_ms: float
    session_ref: str = ""
    error_code: str = ""
    prepared: PreparedUser | None = None


@dataclass(slots=True)
class AsrRun:
    account_index: int
    session_ref: str
    started_ms: float | None = None
    finished_ms: float | None = None
    capture_started_ms: float | None = None
    capture_finished_ms: float | None = None
    ready_ms: float | None = None
    first_partial_ms: float | None = None
    capture_stopped_ms: float | None = None
    final_ms: float | None = None
    first_partial_wait_ms: float | None = None
    stop_ack_wait_ms: float | None = None
    final_wait_after_stop_ms: float | None = None
    accepted: bool = False
    partial_count: int = 0
    terminal_type: str = ""
    terminal_code: str = ""
    recording_ref: str = ""
    event_types: tuple[str, ...] = ()
    degradation_codes: tuple[str, ...] = ()
    error_code: str = ""


@dataclass(slots=True)
class RehearsalRun:
    account_index: int
    session_ref: str
    started_ms: float | None = None
    finished_ms: float | None = None
    connect_ms: float | None = None
    first_event_ms: float | None = None
    first_content_ms: float | None = None
    completed_ms: float | None = None
    startup_wait_ms: float | None = None
    analysis_model_queue_ms: float | None = None
    employee_model_queue_ms: float | None = None
    model_queue_wait_ms: float | None = None
    server_first_visible_ms: float | None = None
    server_total_ms: float | None = None
    response_bytes: int = 0
    status_code: int | None = None
    event_names: tuple[str, ...] = ()
    error_code: str = ""


def _round_ms(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _offset_ms(origin: float) -> float:
    return max(0.0, (time.perf_counter() - origin) * 1000.0)


def _elapsed_delta_ms(
    later_ms: float | None,
    earlier_ms: float | None,
) -> float | None:
    if later_ms is None or earlier_ms is None:
        return None
    value = float(later_ms) - float(earlier_ms)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _elapsed_from_barrier_ms(
    started_ms: float | None,
    elapsed_ms: float | None,
    barrier_release_ms: float | None,
) -> float | None:
    if started_ms is None or elapsed_ms is None:
        return None
    return _elapsed_delta_ms(started_ms + elapsed_ms, barrier_release_ms)


def _latest_from_barrier_ms(
    barrier_release_ms: float | None,
    *milestones: tuple[float | None, float | None],
) -> float | None:
    values = [
        _elapsed_from_barrier_ms(started_ms, elapsed_ms, barrier_release_ms)
        for started_ms, elapsed_ms in milestones
    ]
    if any(value is None for value in values):
        return None
    return max(float(value) for value in values)


def _nonnegative_ms(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _extract_rehearsal_timing(
    terminal_payload: Mapping[str, Any] | None,
) -> dict[str, float | None]:
    timing = (
        terminal_payload.get("timing")
        if isinstance(terminal_payload, Mapping)
        else None
    )
    if not isinstance(timing, Mapping):
        timing = {}
    summary = timing.get("summary_ms")
    if not isinstance(summary, Mapping):
        summary = {}
    stages = timing.get("stages")
    if not isinstance(stages, list):
        stages = []

    stage_durations: dict[str, list[float]] = {}
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        name = str(stage.get("name") or "")
        duration = _nonnegative_ms(stage.get("duration_ms"))
        if name and duration is not None:
            stage_durations.setdefault(name, []).append(duration)

    motivation_queue = max(
        stage_durations.get("motivation_model_queue", []),
        default=0.0,
    )
    emotion_queue = max(
        stage_durations.get("emotion_model_queue", []),
        default=0.0,
    )
    dimension_queue = max(
        stage_durations.get("dimension_model_queue", []),
        default=0.0,
    )
    employee_queues = stage_durations.get("employee_model_queue", [])
    has_queue_observation = bool(
        stage_durations.get("motivation_model_queue")
        or stage_durations.get("emotion_model_queue")
        or stage_durations.get("dimension_model_queue")
        or employee_queues
    )
    analysis_queue = max(motivation_queue, emotion_queue, dimension_queue)
    employee_queue = sum(employee_queues)
    model_queue = (
        analysis_queue + employee_queue if has_queue_observation else None
    )
    return {
        "analysis_model_queue_ms": (
            analysis_queue if has_queue_observation else None
        ),
        "employee_model_queue_ms": (
            employee_queue if has_queue_observation else None
        ),
        "model_queue_wait_ms": model_queue,
        "server_first_visible_ms": _nonnegative_ms(
            summary.get("wait_for_first_visible_reply_ms")
        ),
        "server_total_ms": _nonnegative_ms(summary.get("total_ms")),
    }


def _safe_ref(run_id: str, value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(f"{run_id}:{value}".encode()).hexdigest()[:16]


def _safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, JointLoadError):
        return exc.code
    if isinstance(exc, threading.BrokenBarrierError):
        return "joint_start_barrier_broken"
    if isinstance(exc, requests.Timeout):
        return "http_timeout"
    if isinstance(exc, requests.RequestException):
        return "http_transport_error"
    if isinstance(exc, websocket.WebSocketTimeoutException):
        return "websocket_timeout"
    if isinstance(exc, websocket.WebSocketException):
        return "websocket_error"
    if isinstance(exc, SSEProtocolError):
        return "sse_protocol_error"
    name = re.sub(r"[^a-z0-9]+", "_", type(exc).__name__.casefold()).strip("_")
    return f"unexpected_{name or 'error'}"


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def authorize_live_run(
    *,
    concurrency: int,
    allow_live_models: bool,
    maximum_cost_cny: float | None,
    model: str | None,
    input_tokens_per_user: int | None,
    output_tokens_per_user: int | None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values = os.environ if env is None else env
    allowed = allow_live_models or (
        values.get("LOADTEST_ALLOW_LIVE_MODELS", "").strip().casefold()
        in _TRUE_VALUES
    )
    if not allowed:
        raise ValueError(
            "joint load refuses live requests without --allow-live-models "
            "or LOADTEST_ALLOW_LIVE_MODELS=true"
        )

    ceiling = (
        maximum_cost_cny
        if maximum_cost_cny is not None
        else float(values.get("LOADTEST_MAX_COST_CNY", "0") or 0)
    )
    if ceiling <= 0:
        raise ValueError(
            "--max-cost-cny or LOADTEST_MAX_COST_CNY must be positive"
        )

    selected_model = (
        str(model or "").strip()
        or values.get("LOADTEST_LIVE_MODEL", "").strip()
        or values.get("EMPLOYEE_MODEL", "").strip()
        or "glm-5.2"
    )
    estimated_input = (
        input_tokens_per_user
        if input_tokens_per_user is not None
        else int(values.get("LOADTEST_ESTIMATED_INPUT_TOKENS_PER_FLOW", "120000"))
    )
    estimated_output = (
        output_tokens_per_user
        if output_tokens_per_user is not None
        else int(values.get("LOADTEST_ESTIMATED_OUTPUT_TOKENS_PER_FLOW", "16000"))
    )
    if estimated_input <= 0 or estimated_output <= 0:
        raise ValueError("conservative token estimates must be positive")

    estimate = estimate_cost_cny(
        model=selected_model,
        input_tokens=estimated_input,
        output_tokens=estimated_output,
        request_count=concurrency,
        rates=parse_rates(values.get("LOADTEST_MODEL_PRICING")),
    )
    if estimate > ceiling:
        raise ValueError(
            f"conservative {concurrency}-user estimate {estimate:.4f} CNY "
            f"exceeds the configured ceiling {ceiling:.4f} CNY"
        )
    return {
        "model_mode": "live",
        "authorized": True,
        "model": selected_model,
        "maximum_cost_cny": round(ceiling, 6),
        "conservative_estimated_cost_cny": round(estimate, 6),
        "input_tokens_per_user": estimated_input,
        "output_tokens_per_user": estimated_output,
        "user_count": concurrency,
        "basis": "full-flow-equivalent upper bound per setup-plus-one-turn user",
    }


def _request_json(
    session: requests.Session,
    *,
    method: str,
    url: str,
    stage: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        response = session.request(
            method,
            url,
            headers=headers,
            json=payload,
            timeout=(20, timeout_seconds),
        )
    except requests.Timeout as exc:
        raise JointLoadError(f"{stage}_timeout") from exc
    except requests.RequestException as exc:
        raise JointLoadError(f"{stage}_transport_error") from exc
    try:
        if response.status_code < 200 or response.status_code >= 300:
            raise JointLoadError(f"{stage}_http_{response.status_code}")
        try:
            decoded = response.json()
        except (TypeError, ValueError) as exc:
            raise JointLoadError(f"{stage}_invalid_json") from exc
        if not isinstance(decoded, dict):
            raise JointLoadError(f"{stage}_invalid_json")
        return decoded
    finally:
        response.close()


def _prepare_user(
    *,
    base_url: str,
    account: LoadTestAccount,
    run_id: str,
    timeout_seconds: float,
) -> SetupRun:
    started = time.perf_counter()
    session = requests.Session()
    headers = {
        "X-Load-Test-Run-ID": run_id,
        "X-Load-Test-User": f"joint-{account.index:04d}",
    }
    try:
        login = _request_json(
            session,
            method="POST",
            url=f"{base_url}/api/v1/auth/login",
            stage="login",
            headers=headers,
            payload={"email": account.email, "password": account.password},
            timeout_seconds=timeout_seconds,
        )
        returned_email = str((login.get("user") or {}).get("email") or "").lower()
        if returned_email != account.email:
            raise JointLoadError("login_identity_mismatch")

        state = _request_json(
            session,
            method="POST",
            url=f"{base_url}/api/v1/sessions",
            stage="session_create",
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        session_id = str(state.get("session_id") or "").strip()
        if not session_id:
            raise JointLoadError("session_create_missing_id")

        _request_json(
            session,
            method="PATCH",
            url=f"{base_url}/api/v1/setup/{session_id}/profile",
            stage="setup_profile",
            headers=headers,
            payload=profile_payload(account),
            timeout_seconds=timeout_seconds,
        )
        draft = _request_json(
            session,
            method="POST",
            url=f"{base_url}/api/v1/setup/{session_id}/intent-performance-draft",
            stage="setup_intent_draft",
            headers=headers,
            payload={"intent_id": "development"},
            timeout_seconds=timeout_seconds,
        )
        performance_items = draft.get("performance_items")
        if not isinstance(performance_items, list) or len(performance_items) != 3:
            raise JointLoadError("setup_intent_draft_invalid_items")
        _request_json(
            session,
            method="PATCH",
            url=f"{base_url}/api/v1/setup/{session_id}/intent",
            stage="setup_intent",
            headers=headers,
            payload=intent_request("development", performance_items),
            timeout_seconds=timeout_seconds,
        )
        simulation = simulation_payload()
        simulation["run_mode"] = "rehearsal_report"
        _request_json(
            session,
            method="PATCH",
            url=f"{base_url}/api/v1/setup/{session_id}/simulation",
            stage="setup_simulation",
            headers=headers,
            payload=simulation,
            timeout_seconds=timeout_seconds,
        )
        completed = _request_json(
            session,
            method="POST",
            url=f"{base_url}/api/v1/setup/{session_id}/complete",
            stage="setup_complete",
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        if not completed.get("setup_ready"):
            raise JointLoadError("setup_not_ready")

        cookie = "; ".join(
            f"{item.name}={item.value}" for item in session.cookies
        )
        if not cookie:
            raise JointLoadError("login_cookie_missing")
        session_ref = _safe_ref(run_id, session_id)
        return SetupRun(
            account_index=account.index,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            session_ref=session_ref,
            prepared=PreparedUser(
                account=account,
                session_id=session_id,
                session_ref=session_ref,
                cookie=cookie,
                http_session=session,
            ),
        )
    except BaseException as exc:
        session.close()
        return SetupRun(
            account_index=account.index,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error_code=_safe_error_code(exc),
        )


def _run_asr(
    *,
    user: PreparedUser,
    base_url: str,
    run_id: str,
    pcm: bytes,
    sample_rate: int,
    chunk_ms: int,
    timeout_seconds: float,
    barrier_timeout_seconds: float,
    barrier: threading.Barrier,
    origin: float,
) -> AsrRun:
    result = AsrSessionResult(
        account_index=user.account.index,
        session_id=user.session_id,
    )
    connection: websocket.WebSocket | None = None
    started_at: float | None = None
    started_ms: float | None = None
    finished_ms: float | None = None
    capture_started_ms: float | None = None
    capture_finished_ms: float | None = None
    capture_started_at: float | None = None
    stop_sent_elapsed_ms: float | None = None
    error_code = ""
    try:
        barrier.wait(timeout=barrier_timeout_seconds)
        started_at = time.perf_counter()
        started_ms = _offset_ms(origin)
        connection = websocket.create_connection(
            _websocket_url(base_url, user.session_id),
            cookie=user.cookie,
            header=[f"X-Load-Test-Run-ID: {run_id}"],
            timeout=20,
            enable_multithread=True,
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and not result.accepted and not result.terminal_type:
            event = _receive_event(connection, 0.5)
            if event:
                _record_event(result, event, started_at)
        if not result.accepted:
            if not result.terminal_type:
                raise JointLoadError("asr_recording_ready_timeout")
            raise JointLoadError("asr_rejected")

        bytes_per_chunk = max(2, sample_rate * 2 * chunk_ms // 1000)
        capture_started_at = time.perf_counter()
        capture_started_ms = _offset_ms(origin)
        for offset in range(0, len(pcm), bytes_per_chunk):
            connection.send_binary(pcm[offset : offset + bytes_per_chunk])
            interval_deadline = time.monotonic() + chunk_ms / 1000.0
            while True:
                remaining_seconds = interval_deadline - time.monotonic()
                if remaining_seconds <= 0:
                    break
                event = _receive_event(
                    connection,
                    min(0.05, remaining_seconds),
                )
                if event:
                    _record_event(result, event, started_at)
        capture_finished_ms = _offset_ms(origin)
        stop_sent_elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        connection.send(
            json.dumps(
                {"type": "stop", "client_stopped_at_ms": int(time.time() * 1000)}
            )
        )
        while time.monotonic() < deadline and not result.terminal_type:
            event = _receive_event(connection, 0.5)
            if event:
                _record_event(result, event, started_at)
        if not result.terminal_type:
            raise JointLoadError("asr_final_timeout")
        if result.terminal_type != "final":
            raise JointLoadError("asr_terminal_error")
    except BaseException as exc:
        error_code = _safe_error_code(exc)
    finally:
        finished_ms = _offset_ms(origin)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    capture_started_elapsed_ms = (
        (capture_started_at - started_at) * 1000.0
        if capture_started_at is not None and started_at is not None
        else None
    )
    return AsrRun(
        account_index=user.account.index,
        session_ref=user.session_ref,
        started_ms=started_ms,
        finished_ms=finished_ms,
        capture_started_ms=capture_started_ms,
        capture_finished_ms=capture_finished_ms,
        ready_ms=result.ready_ms,
        first_partial_ms=result.first_partial_ms,
        capture_stopped_ms=result.capture_stopped_ms,
        final_ms=result.final_ms,
        first_partial_wait_ms=_elapsed_delta_ms(
            result.first_partial_ms,
            capture_started_elapsed_ms,
        ),
        stop_ack_wait_ms=_elapsed_delta_ms(
            result.capture_stopped_ms,
            stop_sent_elapsed_ms,
        ),
        final_wait_after_stop_ms=_elapsed_delta_ms(
            result.final_ms,
            stop_sent_elapsed_ms,
        ),
        accepted=result.accepted,
        partial_count=result.partial_count,
        terminal_type=result.terminal_type,
        terminal_code=result.terminal_code,
        recording_ref=_safe_ref(run_id, result.recording_id),
        event_types=tuple(result.event_types),
        degradation_codes=tuple(result.degradation_codes),
        error_code=error_code,
    )


def _run_rehearsal(
    *,
    user: PreparedUser,
    base_url: str,
    run_id: str,
    timeout_seconds: float,
    barrier_timeout_seconds: float,
    barrier: threading.Barrier,
    origin: float,
) -> RehearsalRun:
    started_ms: float | None = None
    finished_ms: float | None = None
    connect_ms: float | None = None
    status_code: int | None = None
    observation: StreamObservation | None = None
    error_code = ""
    response: requests.Response | None = None
    try:
        barrier.wait(timeout=barrier_timeout_seconds)
        started_at = time.perf_counter()
        started_ms = _offset_ms(origin)
        observation = StreamObservation(flow="rehearsal", started_at=started_at)
        parser = SSEParser()
        try:
            response = user.http_session.post(
                f"{base_url}/api/v1/rehearsal/{user.session_id}/message/stream",
                headers={
                    "X-Load-Test-Run-ID": run_id,
                    "X-Load-Test-User": f"joint-{user.account.index:04d}",
                },
                json={"message": MANAGER_MESSAGES[0]},
                timeout=(20, timeout_seconds),
                stream=True,
            )
        except requests.Timeout as exc:
            raise JointLoadError("rehearsal_timeout") from exc
        except requests.RequestException as exc:
            raise JointLoadError("rehearsal_transport_error") from exc
        connect_ms = (time.perf_counter() - started_at) * 1000.0
        status_code = response.status_code
        if status_code < 200 or status_code >= 300:
            raise JointLoadError(f"rehearsal_http_{status_code}")
        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue
            observation.response_bytes += len(chunk)
            for event in parser.feed(chunk):
                observation.observe(event)
        for event in parser.finish():
            observation.observe(event)
        observation.validate()
    except BaseException as exc:
        error_code = _safe_error_code(exc)
    finally:
        if response is not None:
            response.close()
        finished_ms = _offset_ms(origin)

    server_timing = _extract_rehearsal_timing(
        observation.terminal_payload if observation else None
    )
    return RehearsalRun(
        account_index=user.account.index,
        session_ref=user.session_ref,
        started_ms=started_ms,
        finished_ms=finished_ms,
        connect_ms=connect_ms,
        first_event_ms=observation.first_event_ms if observation else None,
        first_content_ms=observation.first_content_ms if observation else None,
        completed_ms=observation.completed_ms if observation else None,
        startup_wait_ms=observation.first_event_ms if observation else None,
        analysis_model_queue_ms=server_timing["analysis_model_queue_ms"],
        employee_model_queue_ms=server_timing["employee_model_queue_ms"],
        model_queue_wait_ms=server_timing["model_queue_wait_ms"],
        server_first_visible_ms=server_timing["server_first_visible_ms"],
        server_total_ms=server_timing["server_total_ms"],
        response_bytes=observation.response_bytes if observation else 0,
        status_code=status_code,
        event_names=tuple(observation.event_names) if observation else (),
        error_code=error_code,
    )


def _run_joint_tasks(
    *,
    users: list[PreparedUser],
    base_url: str,
    run_id: str,
    pcm: bytes,
    sample_rate: int,
    chunk_ms: int,
    timeout_seconds: float,
    barrier_timeout_seconds: float,
) -> tuple[list[AsrRun], list[RehearsalRun], float | None]:
    origin = time.perf_counter()
    release_at: list[float | None] = [None]

    def mark_release() -> None:
        release_at[0] = _offset_ms(origin)

    barrier = threading.Barrier(len(users) * 2, action=mark_release)
    asr_results: list[AsrRun] = []
    rehearsal_results: list[RehearsalRun] = []
    with ThreadPoolExecutor(max_workers=len(users) * 2) as executor:
        futures: dict[Any, str] = {}
        for user in users:
            futures[
                executor.submit(
                    _run_asr,
                    user=user,
                    base_url=base_url,
                    run_id=run_id,
                    pcm=pcm,
                    sample_rate=sample_rate,
                    chunk_ms=chunk_ms,
                    timeout_seconds=timeout_seconds,
                    barrier_timeout_seconds=barrier_timeout_seconds,
                    barrier=barrier,
                    origin=origin,
                )
            ] = "asr"
            futures[
                executor.submit(
                    _run_rehearsal,
                    user=user,
                    base_url=base_url,
                    run_id=run_id,
                    timeout_seconds=timeout_seconds,
                    barrier_timeout_seconds=barrier_timeout_seconds,
                    barrier=barrier,
                    origin=origin,
                )
            ] = "rehearsal"
        for future in as_completed(futures):
            if futures[future] == "asr":
                asr_results.append(future.result())
            else:
                rehearsal_results.append(future.result())
    asr_results.sort(key=lambda item: item.account_index)
    rehearsal_results.sort(key=lambda item: item.account_index)
    return asr_results, rehearsal_results, release_at[0]


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 3)


def _metric(values: list[float | None]) -> dict[str, Any]:
    present = [float(value) for value in values if value is not None]
    return {
        "count": len(present),
        "p50_ms": _percentile(present, 0.50),
        "p95_ms": _percentile(present, 0.95),
        "p99_ms": _percentile(present, 0.99),
        "max_ms": round(max(present), 3) if present else None,
    }


def _experience_metric(
    values: list[float | None],
    *,
    expected_count: int,
    smooth_threshold_ms: float,
    acceptable_threshold_ms: float,
) -> dict[str, Any]:
    present = [
        float(value)
        for value in values
        if value is not None
        and math.isfinite(float(value))
        and float(value) >= 0
    ]
    statistics = _metric(present)
    p95 = statistics["p95_ms"]
    maximum = statistics["max_ms"]
    if len(present) != expected_count or p95 is None:
        grade = "poor"
        reason = "incomplete_samples"
    elif maximum is not None and maximum > acceptable_threshold_ms * 2:
        grade = "poor"
        reason = "tail_exceeded"
    elif p95 <= smooth_threshold_ms:
        grade = "smooth"
        reason = "within_smooth_p95"
    elif p95 <= acceptable_threshold_ms:
        grade = "acceptable"
        reason = "within_acceptable_p95"
    else:
        grade = "poor"
        reason = "p95_exceeded"
    return {
        **statistics,
        "expected_count": expected_count,
        "missing_count": max(0, expected_count - len(present)),
        "smooth_threshold_ms": smooth_threshold_ms,
        "acceptable_threshold_ms": acceptable_threshold_ms,
        "users_by_grade": {
            "smooth": sum(value <= smooth_threshold_ms for value in present),
            "acceptable": sum(
                smooth_threshold_ms < value <= acceptable_threshold_ms
                for value in present
            ),
            "poor": sum(value > acceptable_threshold_ms for value in present),
        },
        "grade": grade,
        "reason": reason,
    }


def _build_user_experience(
    *,
    concurrency: int,
    successful_joint_users: int,
    asr_results: list[AsrRun],
    rehearsal_results: list[RehearsalRun],
    barrier_release_ms: float | None,
) -> dict[str, Any]:
    asr_by_account = {item.account_index: item for item in asr_results}
    rehearsal_by_account = {
        item.account_index: item for item in rehearsal_results
    }
    account_indexes = sorted(set(asr_by_account) | set(rehearsal_by_account))
    joint_first_feedback = [
        elapsed
        for index in account_indexes
        if index in asr_by_account
        and index in rehearsal_by_account
        and (elapsed := _latest_from_barrier_ms(
            barrier_release_ms,
            (
                asr_by_account[index].started_ms,
                asr_by_account[index].first_partial_ms,
            ),
            (
                rehearsal_by_account[index].started_ms,
                rehearsal_by_account[index].first_content_ms,
            ),
        )) is not None
    ]
    joint_completion = [
        elapsed
        for index in account_indexes
        if index in asr_by_account
        and index in rehearsal_by_account
        and (elapsed := _latest_from_barrier_ms(
            barrier_release_ms,
            (asr_by_account[index].started_ms, asr_by_account[index].final_ms),
            (
                rehearsal_by_account[index].started_ms,
                rehearsal_by_account[index].completed_ms,
            ),
        )) is not None
    ]
    values = {
        "asr_ready_wait": [item.ready_ms for item in asr_results],
        "asr_first_partial_wait": [
            item.first_partial_wait_ms for item in asr_results
        ],
        "asr_stop_ack_wait": [item.stop_ack_wait_ms for item in asr_results],
        "asr_final_wait_after_stop": [
            item.final_wait_after_stop_ms for item in asr_results
        ],
        "rehearsal_startup_wait": [
            item.startup_wait_ms for item in rehearsal_results
        ],
        "rehearsal_model_queue_wait": [
            item.model_queue_wait_ms for item in rehearsal_results
        ],
        "rehearsal_first_visible_wait": [
            item.first_content_ms for item in rehearsal_results
        ],
        "rehearsal_completion_wait": [
            item.completed_ms for item in rehearsal_results
        ],
        "joint_parallel_first_feedback_wait": joint_first_feedback,
        "joint_parallel_completion_wait": joint_completion,
    }
    metrics = {
        name: {
            "definition": _UX_METRIC_DEFINITIONS[name],
            **_experience_metric(
                values[name],
                expected_count=concurrency,
                smooth_threshold_ms=thresholds[0],
                acceptable_threshold_ms=thresholds[1],
            ),
        }
        for name, thresholds in _UX_THRESHOLDS_MS.items()
    }
    failed_metrics = [
        name for name, metric in metrics.items() if metric["grade"] == "poor"
    ]
    grades = {metric["grade"] for metric in metrics.values()}
    status = (
        "poor"
        if failed_metrics or successful_joint_users != concurrency
        else "acceptable"
        if "acceptable" in grades
        else "smooth"
    )
    return {
        "status": status,
        "passed": status != "poor",
        "grading_basis": "p95 plus complete sample coverage",
        "successful_joint_users": successful_joint_users,
        "success_rate_pct": round(
            successful_joint_users * 100.0 / concurrency,
            3,
        ),
        "failed_metrics": failed_metrics,
        "metrics": metrics,
        "scope": {
            "mode": "simultaneous_resource_contention",
            "speech_to_reply_chain": False,
            "note": (
                "ASR audio and a fixed-text rehearsal request start together. "
                "This measures contention and overlap, not ASR-final-to-employee-"
                "reply latency."
            ),
        },
    }


def _overlap_ms(
    first_start: float | None,
    first_end: float | None,
    second_start: float | None,
    second_end: float | None,
) -> float:
    if None in (first_start, first_end, second_start, second_end):
        return 0.0
    return round(
        max(
            0.0,
            min(float(first_end), float(second_end))
            - max(float(first_start), float(second_start)),
        ),
        3,
    )


def _intersection_ms(intervals: list[tuple[float | None, float | None]]) -> float:
    if not intervals or any(start is None or end is None for start, end in intervals):
        return 0.0
    return round(
        max(
            0.0,
            min(float(end) for _, end in intervals)
            - max(float(start) for start, _ in intervals),
        ),
        3,
    )


def build_report(
    *,
    run_id: str,
    base_url: str,
    concurrency: int,
    pcm: bytes,
    sample_rate: int,
    chunk_ms: int,
    budget: dict[str, Any],
    setup_results: list[SetupRun],
    asr_results: list[AsrRun],
    rehearsal_results: list[RehearsalRun],
    barrier_release_ms: float | None,
    setup_wall_ms: float,
    max_start_skew_ms: float,
) -> dict[str, Any]:
    setup_by_account = {item.account_index: item for item in setup_results}
    asr_by_account = {item.account_index: item for item in asr_results}
    rehearsal_by_account = {
        item.account_index: item for item in rehearsal_results
    }
    users: list[dict[str, Any]] = []
    failures: list[str] = []
    overlaps: list[float] = []
    successful_joint_users = 0

    if len(setup_results) != concurrency:
        failures.append("setup result count does not match concurrency")
    if len(asr_results) != concurrency:
        failures.append("ASR task count does not match concurrency")
    if len(rehearsal_results) != concurrency:
        failures.append("rehearsal task count does not match concurrency")

    session_refs = [item.session_ref for item in setup_results if item.session_ref]
    if len(session_refs) != concurrency or len(set(session_refs)) != concurrency:
        failures.append("setup sessions are missing or not unique")

    for account_index in sorted(
        set(setup_by_account) | set(asr_by_account) | set(rehearsal_by_account)
    ):
        setup = setup_by_account.get(account_index)
        asr = asr_by_account.get(account_index)
        rehearsal = rehearsal_by_account.get(account_index)
        setup_ok = setup is not None and not setup.error_code
        capture_duration_ms = (
            _elapsed_delta_ms(asr.capture_finished_ms, asr.capture_started_ms)
            if asr is not None else None
        )
        asr_ok = (
            asr is not None
            and not asr.error_code
            and asr.accepted
            and asr.terminal_type == "final"
            and asr.partial_count >= 1
            and not asr.degradation_codes
            and asr.first_partial_wait_ms is not None
            and asr.stop_ack_wait_ms is not None
            and asr.final_wait_after_stop_ms is not None
            and capture_duration_ms is not None
            and 9800.0 <= capture_duration_ms <= 10500.0
        )
        rehearsal_ok = (
            rehearsal is not None
            and not rehearsal.error_code
            and rehearsal.startup_wait_ms is not None
            and rehearsal.model_queue_wait_ms is not None
            and rehearsal.first_content_ms is not None
            and rehearsal.completed_ms is not None
        )
        if not setup_ok:
            failures.append(f"account {account_index} setup failed")
        if asr is None:
            failures.append(f"account {account_index} omitted ASR result")
        elif not asr_ok:
            failures.append(f"account {account_index} ASR failed")
        if rehearsal is None:
            failures.append(f"account {account_index} omitted rehearsal result")
        elif not rehearsal_ok:
            failures.append(f"account {account_index} rehearsal failed")

        overlap = (
            _overlap_ms(
                asr.capture_started_ms,
                asr.capture_finished_ms,
                rehearsal.started_ms,
                rehearsal.finished_ms,
            )
            if asr is not None and rehearsal is not None
            else 0.0
        )
        overlaps.append(overlap)
        if asr is not None and rehearsal is not None and overlap <= 0:
            failures.append(
                f"account {account_index} had no ASR-capture/rehearsal overlap"
            )
        if setup_ok and asr_ok and rehearsal_ok and overlap > 0:
            successful_joint_users += 1
        joint_first_feedback_ms = (
            max(asr.first_partial_ms, rehearsal.first_content_ms)
            if asr is not None
            and rehearsal is not None
            and asr.first_partial_ms is not None
            and rehearsal.first_content_ms is not None
            else None
        )
        joint_completion_ms = (
            max(asr.final_ms, rehearsal.completed_ms)
            if asr is not None
            and rehearsal is not None
            and asr.final_ms is not None
            and rehearsal.completed_ms is not None
            else None
        )
        users.append(
            {
                "account_index": account_index,
                "session_ref": setup.session_ref if setup else "",
                "setup": {
                    "duration_ms": _round_ms(setup.duration_ms) if setup else None,
                    "error_code": setup.error_code if setup else "missing",
                },
                "asr": (
                    {
                        "started_ms": _round_ms(asr.started_ms),
                        "finished_ms": _round_ms(asr.finished_ms),
                        "capture_started_ms": _round_ms(asr.capture_started_ms),
                        "capture_finished_ms": _round_ms(asr.capture_finished_ms),
                        "ready_ms": _round_ms(asr.ready_ms),
                        "ttft_ms": _round_ms(asr.first_partial_ms),
                        "capture_stopped_ms": _round_ms(asr.capture_stopped_ms),
                        "final_ms": _round_ms(asr.final_ms),
                        "first_partial_wait_ms": _round_ms(
                            asr.first_partial_wait_ms
                        ),
                        "stop_ack_wait_ms": _round_ms(asr.stop_ack_wait_ms),
                        "final_wait_after_stop_ms": _round_ms(
                            asr.final_wait_after_stop_ms
                        ),
                        "accepted": asr.accepted,
                        "partial_count": asr.partial_count,
                        "terminal_type": asr.terminal_type,
                        "terminal_code": asr.terminal_code,
                        "recording_ref": asr.recording_ref,
                        "event_types": list(asr.event_types),
                        "degradation_codes": list(asr.degradation_codes),
                        "error_code": asr.error_code,
                    }
                    if asr
                    else None
                ),
                "rehearsal": (
                    {
                        "started_ms": _round_ms(rehearsal.started_ms),
                        "finished_ms": _round_ms(rehearsal.finished_ms),
                        "connect_ms": _round_ms(rehearsal.connect_ms),
                        "first_event_ms": _round_ms(rehearsal.first_event_ms),
                        "ttft_ms": _round_ms(rehearsal.first_content_ms),
                        "completed_ms": _round_ms(rehearsal.completed_ms),
                        "startup_wait_ms": _round_ms(rehearsal.startup_wait_ms),
                        "analysis_model_queue_ms": _round_ms(
                            rehearsal.analysis_model_queue_ms
                        ),
                        "employee_model_queue_ms": _round_ms(
                            rehearsal.employee_model_queue_ms
                        ),
                        "model_queue_wait_ms": _round_ms(
                            rehearsal.model_queue_wait_ms
                        ),
                        "server_first_visible_ms": _round_ms(
                            rehearsal.server_first_visible_ms
                        ),
                        "server_total_ms": _round_ms(rehearsal.server_total_ms),
                        "response_bytes": rehearsal.response_bytes,
                        "status_code": rehearsal.status_code,
                        "event_names": list(rehearsal.event_names),
                        "error_code": rehearsal.error_code,
                    }
                    if rehearsal
                    else None
                ),
                "capture_rehearsal_overlap_ms": overlap,
                "joint_parallel_waits": {
                    "first_feedback_ms": _round_ms(joint_first_feedback_ms),
                    "completion_ms": _round_ms(joint_completion_ms),
                },
            }
        )

    operation_starts = [
        value
        for result in (*asr_results, *rehearsal_results)
        if (value := result.started_ms) is not None
    ]
    start_skew = (
        round(max(operation_starts) - min(operation_starts), 3)
        if operation_starts
        else None
    )
    if start_skew is None or len(operation_starts) != concurrency * 2:
        failures.append("one or more barrier participants did not start")
    elif start_skew > max_start_skew_ms:
        failures.append(
            f"joint start skew {start_skew:.3f}ms exceeded "
            f"{max_start_skew_ms:.3f}ms"
        )

    all_operation_intervals = [
        (item.started_ms, item.finished_ms)
        for item in (*asr_results, *rehearsal_results)
    ]
    all_capture_and_rehearsal_intervals = [
        *((item.capture_started_ms, item.capture_finished_ms) for item in asr_results),
        *((item.started_ms, item.finished_ms) for item in rehearsal_results),
    ]
    all_joint_overlap_ms = _intersection_ms(
        all_capture_and_rehearsal_intervals
    )
    minimum_global_overlap_ms = (
        500.0 if budget.get("model_mode") == "live" else 0.0
    )
    if all_joint_overlap_ms < minimum_global_overlap_ms:
        failures.append(
            "all captures and rehearsals overlapped for less than 500ms"
        )
    user_experience = _build_user_experience(
        concurrency=concurrency,
        successful_joint_users=successful_joint_users,
        asr_results=asr_results,
        rehearsal_results=rehearsal_results,
        barrier_release_ms=barrier_release_ms,
    )
    if not user_experience["passed"]:
        failed_metrics = (
            ", ".join(user_experience["failed_metrics"]) or "success_rate"
        )
        failures.append(f"user experience acceptance failed: {failed_metrics}")
    failures = list(dict.fromkeys(failures))
    return {
        "format_version": 2,
        "kind": "joint-rehearsal-asr",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": _redact_url(base_url),
        "concurrency": concurrency,
        "audio": {
            "sha256": hashlib.sha256(pcm).hexdigest(),
            "bytes": len(pcm),
            "sample_rate": sample_rate,
            "chunk_ms": chunk_ms,
            "duration_seconds": round(len(pcm) / (sample_rate * 2), 3),
        },
        "budget_gate": budget,
        "setup": {
            "wall_ms": round(setup_wall_ms, 3),
            "completed": sum(not item.error_code for item in setup_results),
            "errors": sum(bool(item.error_code) for item in setup_results),
            "duration": _metric([item.duration_ms for item in setup_results]),
        },
        "joint_start": {
            "participants": concurrency * 2,
            "barrier_release_ms": _round_ms(barrier_release_ms),
            "start_skew_ms": start_skew,
            "maximum_allowed_start_skew_ms": max_start_skew_ms,
        },
        "asr": {
            "accepted": sum(item.accepted for item in asr_results),
            "preview_streams": sum(item.partial_count > 0 for item in asr_results),
            "finals": sum(item.terminal_type == "final" for item in asr_results),
            "errors": sum(bool(item.error_code) for item in asr_results),
            "ready": _metric([item.ready_ms for item in asr_results]),
            "ttft": _metric([item.first_partial_ms for item in asr_results]),
            "final": _metric([item.final_ms for item in asr_results]),
            "waits": {
                "ready": _metric([item.ready_ms for item in asr_results]),
                "first_partial_after_capture": _metric(
                    [item.first_partial_wait_ms for item in asr_results]
                ),
                "stop_acknowledgement": _metric(
                    [item.stop_ack_wait_ms for item in asr_results]
                ),
                "final_after_stop": _metric(
                    [item.final_wait_after_stop_ms for item in asr_results]
                ),
            },
        },
        "rehearsal": {
            "completed": sum(
                item.completed_ms is not None and not item.error_code
                for item in rehearsal_results
            ),
            "errors": sum(bool(item.error_code) for item in rehearsal_results),
            "connect": _metric([item.connect_ms for item in rehearsal_results]),
            "ttft": _metric([item.first_content_ms for item in rehearsal_results]),
            "complete": _metric([item.completed_ms for item in rehearsal_results]),
            "waits": {
                "startup": _metric(
                    [item.startup_wait_ms for item in rehearsal_results]
                ),
                "model_queue": _metric(
                    [item.model_queue_wait_ms for item in rehearsal_results]
                ),
                "analysis_model_queue": _metric(
                    [item.analysis_model_queue_ms for item in rehearsal_results]
                ),
                "employee_model_queue": _metric(
                    [item.employee_model_queue_ms for item in rehearsal_results]
                ),
                "first_visible": _metric(
                    [item.first_content_ms for item in rehearsal_results]
                ),
                "complete": _metric(
                    [item.completed_ms for item in rehearsal_results]
                ),
                "server_first_visible": _metric(
                    [item.server_first_visible_ms for item in rehearsal_results]
                ),
                "server_total": _metric(
                    [item.server_total_ms for item in rehearsal_results]
                ),
            },
        },
        "overlap": {
            "users_with_capture_rehearsal_overlap": sum(value > 0 for value in overlaps),
            "per_user": _metric(overlaps),
            "all_operations_overlap_ms": _intersection_ms(all_operation_intervals),
            "all_captures_and_rehearsals_overlap_ms": _intersection_ms(
                all_capture_and_rehearsal_intervals
            ),
            "minimum_required_global_overlap_ms": minimum_global_overlap_ms,
        },
        "user_experience": user_experience,
        "users": users,
        "acceptance": {"passed": not failures, "failures": failures},
    }


def _logout(users: list[PreparedUser], base_url: str, run_id: str) -> None:
    for user in users:
        try:
            user.http_session.post(
                f"{base_url}/api/v1/auth/logout",
                headers={"X-Load-Test-Run-ID": run_id},
                timeout=(5, 15),
            )
        except Exception:
            pass
        user.http_session.close()


def run_joint_load(args: argparse.Namespace, budget: dict[str, Any]) -> dict[str, Any]:
    base_url = args.base_url.rstrip("/")
    pcm = args.pcm_file.read_bytes()
    expected_bytes = args.sample_rate * 2 * 10
    if len(pcm) != expected_bytes:
        raise ValueError(
            f"PCM fixture must be exactly 10 seconds ({expected_bytes} bytes)"
        )
    accounts = configured_accounts(args.accounts_file)
    if len(accounts) < args.concurrency:
        raise ValueError("account pool is smaller than requested concurrency")

    setup_started = time.perf_counter()
    setup_results: list[SetupRun] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                _prepare_user,
                base_url=base_url,
                account=account,
                run_id=args.run_id,
                timeout_seconds=args.timeout_seconds,
            )
            for account in accounts[: args.concurrency]
        ]
        for future in as_completed(futures):
            setup_results.append(future.result())
    setup_results.sort(key=lambda item: item.account_index)
    setup_wall_ms = (time.perf_counter() - setup_started) * 1000.0
    users = [
        item.prepared
        for item in setup_results
        if item.prepared is not None and not item.error_code
    ]
    asr_results: list[AsrRun] = []
    rehearsal_results: list[RehearsalRun] = []
    barrier_release_ms: float | None = None
    try:
        if len(users) == args.concurrency:
            asr_results, rehearsal_results, barrier_release_ms = _run_joint_tasks(
                users=users,
                base_url=base_url,
                run_id=args.run_id,
                pcm=pcm,
                sample_rate=args.sample_rate,
                chunk_ms=args.chunk_ms,
                timeout_seconds=args.timeout_seconds,
                barrier_timeout_seconds=args.barrier_timeout_seconds,
            )
        return build_report(
            run_id=args.run_id,
            base_url=base_url,
            concurrency=args.concurrency,
            pcm=pcm,
            sample_rate=args.sample_rate,
            chunk_ms=args.chunk_ms,
            budget=budget,
            setup_results=setup_results,
            asr_results=asr_results,
            rehearsal_results=rehearsal_results,
            barrier_release_ms=barrier_release_ms,
            setup_wall_ms=setup_wall_ms,
            max_start_skew_ms=args.max_start_skew_ms,
        )
    finally:
        _logout(users, base_url, args.run_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run synchronized setup-complete rehearsal SSE and 10-second ASR "
            "WebSocket load for the same isolated users."
        )
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--pcm-file", type=Path, required=True)
    parser.add_argument(
        "--accounts-file",
        type=Path,
        default=Path("loadtests/data/accounts.jsonl"),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        choices=_ALLOWED_CONCURRENCY,
        default=30,
    )
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--barrier-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-start-skew-ms", type=float, default=1000.0)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("loadtests/results"),
    )
    parser.add_argument("--run-id")
    parser.add_argument(
        "--model-mode",
        choices=("stub", "live"),
        default="stub",
    )
    parser.add_argument("--allow-live-models", action="store_true")
    parser.add_argument("--max-cost-cny", type=float)
    parser.add_argument("--live-model")
    parser.add_argument("--estimated-input-tokens-per-user", type=int)
    parser.add_argument("--estimated-output-tokens-per-user", type=int)
    return parser


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    args.run_id = args.run_id or f"joint-{uuid.uuid4().hex[:12]}"
    try:
        if args.model_mode == "stub":
            budget = {
                "model_mode": "stub",
                "authorized": False,
                "model": "loadtest-stub",
                "maximum_cost_cny": 0.0,
                "conservative_estimated_cost_cny": 0.0,
                "input_tokens_per_user": 0,
                "output_tokens_per_user": 0,
                "user_count": args.concurrency,
                "basis": "isolated local stub; no external model requests",
            }
        else:
            budget = authorize_live_run(
                concurrency=args.concurrency,
                allow_live_models=args.allow_live_models,
                maximum_cost_cny=args.max_cost_cny,
                model=args.live_model,
                input_tokens_per_user=args.estimated_input_tokens_per_user,
                output_tokens_per_user=args.estimated_output_tokens_per_user,
            )
        payload = run_joint_load(args, budget)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    output = (
        args.artifact_root
        / args.run_id
        / "joint-rehearsal-asr"
        / f"concurrency-{args.concurrency}.json"
    )
    _write_artifact(output, payload)
    passed = bool((payload.get("acceptance") or {}).get("passed"))
    print(json.dumps({"artifact": str(output), "passed": passed}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

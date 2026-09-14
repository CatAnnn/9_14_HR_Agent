from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, BinaryIO

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)

from backend.api.dependencies import get_session_service
from backend.config.settings import get_settings
from backend.core.auth_dependency import get_current_user
from backend.observability.metrics import (
    change_asr_active,
    record_asr_audio_bytes,
    record_asr_duration,
    record_asr_event,
)
from backend.schemas.asr import AsrTranscribeResponse
from backend.schemas.auth import AuthUserResponse
from backend.services.http_client import get_shared_async_client
from backend.services.executor_utils import run_db_with_context
from backend.services.local_model_runtime import get_local_model_runtime_recycler
from backend.services.model_api_auth import ModelAPIAuth
from backend.services.realtime_asr_service import (
    AsrConfigurationError,
    AsrPreviewCapacityError,
    create_realtime_asr_provider,
    get_asr_readiness,
)
from backend.services.session_service import SessionService
from backend.services.recording_asr_service import (
    AsrFinalTranscriptionError,
    AsrStorageError,
    PcmRecording,
    RemoteAsrTranscriber,
    extract_transcript as _extract_transcript,
)

router = APIRouter(prefix="/asr", tags=["asr"])
logger = logging.getLogger(__name__)
_capture_slots: asyncio.Semaphore | None = None
_capture_slots_size = 0
_SESSION_ASR_LANGUAGE = {
    "zh-CN": "zh",
    "en": "en",
    "de": "de",
    "ja": "ja",
}
_ASR_LANGUAGE_ALIASES = {
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "de": "de",
    "de-de": "de",
    "de-at": "de",
    "de-ch": "de",
    "ja": "ja",
    "ja-jp": "ja",
}


def _normalize_asr_language(
    value: str | None,
    *,
    default: str | None = "zh",
) -> str:
    """Map UI/browser locale aliases to languages supported by the workflow.

    An unknown client hint never reaches the model API. It falls back to the
    configured language when that setting is supported, then to Chinese for
    compatibility with existing deployments.
    """

    def normalized(candidate: str | None) -> str | None:
        key = str(candidate or "").strip().lower().replace("_", "-")
        return _ASR_LANGUAGE_ALIASES.get(key)

    return normalized(value) or normalized(default) or "zh"


def _asr_model_language(language: str, settings: Any) -> str:
    """Choose an optional model hint without changing the trusted UI locale.

    Local Qwen3-ASR identifies the spoken language when the hint is omitted, so
    the page language must not constrain recognition. Other providers retain
    the explicit session language because their auto-detection contract is not
    established here.
    """

    if (
        getattr(settings, "asr_provider_mode", "") == "local"
        and bool(getattr(settings, "asr_local_language_auto_detect", True))
    ):
        # Both the realtime proxy and file fallback omit an empty language,
        # which reaches Qwen3-ASR as automatic language identification.
        return ""
    return language


def _seekable_upload_size(upload_stream: BinaryIO) -> int:
    """Return a spooled upload's size without loading it into process memory."""

    position = upload_stream.tell()
    try:
        upload_stream.seek(0, 2)
        return int(upload_stream.tell())
    finally:
        upload_stream.seek(position)

_ALLOWED_AUDIO_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "application/octet-stream",
}


def _get_capture_slots(max_connections: int) -> asyncio.Semaphore | None:
    """Return an optional compatibility cap; zero means unbounded admission."""

    global _capture_slots, _capture_slots_size
    if max_connections <= 0:
        return None
    size = int(max_connections)
    if _capture_slots is None or _capture_slots_size != size:
        _capture_slots = asyncio.Semaphore(size)
        _capture_slots_size = size
    return _capture_slots


def _recording_has_speech(recording: Any) -> bool:
    detector = getattr(recording, "has_speech", None)
    if callable(detector):
        try:
            return bool(detector())
        except Exception:
            logger.warning("Unable to read recording speech activity", exc_info=True)
            return True
    value = getattr(recording, "speech_detected", None)
    return bool(value) if isinstance(value, bool) else True


def _require_owned_workflow_session(
    websocket: WebSocket,
    session_service: SessionService,
) -> tuple[str, str]:
    query_params = getattr(websocket, "query_params", {})
    session_id = str(query_params.get("session_id") or "").strip()
    if not session_id or len(session_id) > 128:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="A valid workflow session_id is required",
        )
    try:
        workflow_state = session_service.get_session(session_id)
    except (KeyError, ValueError) as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Workflow session not found",
        ) from exc
    try:
        asr_language = _SESSION_ASR_LANGUAGE[workflow_state.locale]
    except (AttributeError, KeyError) as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Workflow session locale is not supported",
        ) from exc
    return session_id, asr_language


@router.get("/readiness")
async def asr_readiness(
    _current_user: AuthUserResponse = Depends(get_current_user),
) -> dict[str, Any]:
    settings = get_settings()
    readiness = await get_asr_readiness(settings)
    if (
        readiness.get("status") != "ready"
        and settings.asr_enabled
        and settings.asr_realtime_enabled
        and settings.asr_provider_mode == "local"
    ):
        get_local_model_runtime_recycler().ensure_resident("qwen3_asr")
    return readiness


@router.websocket("/realtime")
async def asr_realtime(
    websocket: WebSocket,
    current_user: AuthUserResponse = Depends(get_current_user),
    session_service: SessionService = Depends(get_session_service),
) -> None:
    app = getattr(websocket, "app", None)
    state = getattr(app, "state", None)
    runtime = getattr(state, "runtime", None)
    workflow_session_id, asr_language = await run_db_with_context(
        getattr(
            runtime,
            "workflow_db_executor",
            getattr(runtime, "db_executor", None),
        ),
        "asr.session_ownership",
        _require_owned_workflow_session,
        websocket,
        session_service,
    )
    settings = get_settings()
    model_language = _asr_model_language(asr_language, settings)
    distributed_coordinator = getattr(runtime, "distributed_coordinator", None)
    coordination_kwargs: dict[str, Any] = (
        {"coordinator": distributed_coordinator}
        if distributed_coordinator is not None
        else {}
    )
    if settings.asr_provider_mode == "local":
        realtime_provider_name = settings.asr_realtime_model
    elif (
        settings.asr_provider_mode == "bosch"
        and settings.asr_bosch_realtime_url.strip()
    ):
        realtime_provider_name = settings.asr_bosch_realtime_model
    else:
        realtime_provider_name = settings.asr_http_model
    await get_asr_readiness(settings, force_refresh=True)
    await websocket.accept()
    if settings.asr_provider_mode == "browser":
        await websocket.send_json(
            {
                "type": "error",
                "code": "browser_asr_uses_client_runtime",
                "message": "当前配置使用浏览器内置语音识别。",
                "recoverable": False,
            }
        )
        await websocket.close(code=1008)
        return
    slots = _get_capture_slots(settings.asr_capture_max_connections)
    slot_acquired = False
    if slots is not None:
        try:
            await asyncio.wait_for(slots.acquire(), timeout=0.05)
            slot_acquired = True
        except TimeoutError:
            record_asr_event("capture_capacity", outcome="rejected")
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "capture_capacity_exceeded",
                    "message": "语音录音连接已满，请稍后重试。",
                    "recoverable": False,
                }
            )
            await websocket.close(code=1013)
            return

    recording = PcmRecording(
        settings,
        owner_key=current_user.email,
        session_id=workflow_session_id,
    )
    preview = create_realtime_asr_provider(
        settings,
        local_client=get_shared_async_client("local_inference"),
        speech_client=get_shared_async_client("speech"),
        routing_key=recording.recording_id,
        language=model_language,
        **coordination_kwargs,
    )
    preconnect_platform_preview = bool(
        settings.asr_provider_mode == "bosch"
        and not settings.asr_bosch_realtime_url.strip()
    )
    frontend_task: asyncio.Task[str] | None = None
    preview_connect_task: asyncio.Task[None] | None = None
    preview_events_task: asyncio.Task[None] | None = None
    preview_close_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    preview_finish_task: asyncio.Task[dict[str, Any]] | None = None
    full_transcription_task: asyncio.Task[str] | None = None
    send_lock = asyncio.Lock()
    client_disconnected = asyncio.Event()
    recording_ready_sent = asyncio.Event()
    capture_started_at = time.perf_counter()
    preview_first_text_at: float | None = None
    preview_last_text_at: float | None = None
    client_stopped_at_ms = 0.0
    capture_active = False
    capture_ended = False
    preview_active = False
    session_outcome = "disconnected"

    async def send_frontend(payload: dict[str, Any]) -> bool:
        if client_disconnected.is_set():
            return False
        enriched = dict(payload)
        enriched.setdefault("session_id", workflow_session_id)
        enriched.setdefault("recording_id", recording.recording_id)
        async with send_lock:
            if client_disconnected.is_set():
                return False
            try:
                await websocket.send_text(json.dumps(enriched, ensure_ascii=False))
                return True
            except (WebSocketDisconnect, RuntimeError):
                client_disconnected.set()
                return False

    async def forward_preview_event(payload: dict[str, Any]) -> None:
        nonlocal preview_first_text_at, preview_last_text_at
        await recording_ready_sent.wait()
        if capture_ended and payload.get("type") == "partial":
            return
        if payload.get("type") == "partial" and str(payload.get("text") or "").strip():
            now = time.perf_counter()
            if preview_first_text_at is None:
                preview_first_text_at = now
                record_asr_duration(
                    "preview_ttft",
                    (now - capture_started_at) * 1000,
                    outcome="success",
                )
            elif preview_last_text_at is not None:
                record_asr_duration(
                    "preview_partial_interval",
                    (now - preview_last_text_at) * 1000,
                    outcome="success",
                )
            preview_last_text_at = now
        await send_frontend(payload)

    async def connect_preview() -> None:
        nonlocal preview_active
        announcement: dict[str, Any]
        try:
            await preview.connect()
        except AsrPreviewCapacityError:
            record_asr_event("preview", outcome="capacity_exceeded")
            announcement = {
                "type": "preview_unavailable",
                "code": "preview_capacity_exceeded",
                "message": "实时语音预览并发已满，完整录音仍在继续。",
                "recoverable": True,
            }
        except AsrConfigurationError:
            record_asr_event("preview", outcome="configuration_error")
            announcement = {
                "type": "preview_unavailable",
                "code": "preview_unavailable",
                "message": "实时语音预览暂不可用，完整录音仍在继续。",
                "recoverable": True,
            }
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Realtime ASR preview connection failed for user=%s",
                current_user.email,
                exc_info=True,
            )
            record_asr_event("preview", outcome="failed")
            announcement = {
                "type": "preview_unavailable",
                "code": "preview_unavailable",
                "message": "实时语音预览暂不可用，完整录音仍在继续。",
                "recoverable": True,
            }
        else:
            preview_active = True
            change_asr_active("preview", 1)
            record_asr_event("preview", outcome="ready")
            logger.info("Realtime ASR preview connected for user=%s", current_user.email)
            announcement = {
                "type": "status",
                "code": "ready",
                "message": "ready",
                "provider": realtime_provider_name,
            }

        # Authentication and scheduler startup run before capture is announced,
        # but protocol order remains recording_ready -> ready/unavailable.
        await recording_ready_sent.wait()
        await send_frontend(announcement)

    async def forward_frontend() -> str:
        nonlocal client_stopped_at_ms
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                client_disconnected.set()
                return "disconnect"
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                client_disconnected.set()
                return "disconnect"
            pcm_bytes = message.get("bytes")
            if pcm_bytes is not None:
                try:
                    recording.append(pcm_bytes)
                except AsrStorageError as exc:
                    await send_frontend(
                        {
                            "type": "error",
                            "code": "recording_storage_failed",
                            "message": str(exc),
                            "recoverable": False,
                        }
                    )
                    return "storage_error"
                await preview.append_audio(pcm_bytes)
                continue
            raw_text = message.get("text")
            if raw_text is None:
                continue
            try:
                event = json.loads(raw_text or "{}")
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "cancel":
                return "cancel"
            if event_type == "stop":
                try:
                    client_stopped_at_ms = float(event.get("client_stopped_at_ms") or 0)
                except (TypeError, ValueError):
                    client_stopped_at_ms = 0.0
                return "stop"
            if event_type == "ping":
                await send_frontend(
                    {"type": "status", "code": "pong", "message": "pong"}
                )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(15)
            sent = await send_frontend(
                {"type": "status", "code": "heartbeat", "message": "heartbeat"}
            )
            if not sent:
                return

    if preconnect_platform_preview:
        preview_connect_task = asyncio.create_task(
            connect_preview(),
            name=f"asr-preview-connect-{recording.recording_id[:8]}",
        )
        preview_events_task = asyncio.create_task(
            preview.receive_loop(forward_preview_event),
            name=f"asr-preview-events-{recording.recording_id[:8]}",
        )

    try:
        try:
            await asyncio.to_thread(recording.open)
        except AsrStorageError as exc:
            await send_frontend(
                {
                    "type": "error",
                    "code": "recording_storage_failed",
                    "message": str(exc),
                    "recoverable": False,
                }
            )
            return
        except OSError:
            logger.warning("Unable to create ASR recording file", exc_info=True)
            await send_frontend(
                {
                    "type": "error",
                    "code": "recording_storage_failed",
                    "message": "无法创建录音文件，请稍后重试。",
                    "recoverable": False,
                }
            )
            return

        capture_active = True
        change_asr_active("capture", 1)
        record_asr_event("capture", outcome="started")

        await send_frontend(
            {
                "type": "recording_ready",
                "code": "recording_ready",
                "message": "recording_ready",
                "recording_id": recording.recording_id,
                "provider": realtime_provider_name,
            }
        )
        recording_ready_sent.set()
        record_asr_duration(
            "recording_ready",
            (time.perf_counter() - capture_started_at) * 1000,
            outcome="success",
        )
        try:
            query_params = getattr(websocket, "query_params", {})
            client_started_at_ms = float(query_params.get("client_started_at_ms") or 0)
        except (TypeError, ValueError):
            client_started_at_ms = 0
        client_ready_ms = time.time() * 1000 - client_started_at_ms
        if 0 < client_ready_ms <= 120_000:
            record_asr_duration(
                "client_to_recording_ready",
                client_ready_ms,
                outcome="success",
            )
        frontend_task = asyncio.create_task(
            forward_frontend(),
            name=f"asr-capture-{recording.recording_id[:8]}",
        )
        if preview_connect_task is None:
            preview_connect_task = asyncio.create_task(
                connect_preview(),
                name=f"asr-preview-connect-{recording.recording_id[:8]}",
            )
            preview_events_task = asyncio.create_task(
                preview.receive_loop(forward_preview_event),
                name=f"asr-preview-events-{recording.recording_id[:8]}",
            )
        heartbeat_task = asyncio.create_task(
            heartbeat(),
            name=f"asr-heartbeat-{recording.recording_id[:8]}",
        )

        reason = await frontend_task
        if reason != "stop":
            session_outcome = reason
            return

        capture_ended = True
        stop_received_at = time.perf_counter()
        await send_frontend(
            {
                "type": "capture_stopped",
                "code": "capture_stopped",
                "recording_id": recording.recording_id,
            }
        )
        record_asr_duration(
            "capture_stopped",
            (time.perf_counter() - stop_received_at) * 1000,
            outcome="success",
        )
        client_stop_ms = time.time() * 1000 - client_stopped_at_ms
        if 0 < client_stop_ms <= 120_000:
            record_asr_duration(
                "client_to_capture_stopped",
                client_stop_ms,
                outcome="success",
            )

        close_input_task = asyncio.create_task(
            recording.close_input(),
            name=f"asr-recording-close-{recording.recording_id[:8]}",
        )

        stream_final_started_at = time.perf_counter()

        async def report_progress(completed: int, total: int) -> None:
            await send_frontend(
                {
                    "type": "finalizing",
                    "code": "finalizing",
                    "completed_segments": completed,
                    "total_segments": total,
                }
            )

        async def finish_preview() -> dict[str, Any]:
            try:
                if preview_connect_task is not None and not preview_connect_task.done():
                    preview_connect_timeout = 2.0
                    if settings.asr_provider_mode == "local":
                        preview_connect_timeout = max(
                            preview_connect_timeout,
                            float(
                                getattr(
                                    settings,
                                    "asr_local_admission_timeout_seconds",
                                    2.0,
                                )
                            )
                            + float(
                                getattr(
                                    settings,
                                    "asr_local_routing_probe_timeout_seconds",
                                    0.25,
                                )
                            )
                            + 0.25,
                        )
                    await asyncio.wait_for(
                        preview_connect_task,
                        timeout=preview_connect_timeout,
                    )
                elif preview_connect_task is not None:
                    await asyncio.gather(preview_connect_task, return_exceptions=True)
                if preview_active and not bool(getattr(preview, "failed", False)):
                    return await preview.finish()
            except (TimeoutError, AsrConfigurationError, httpx.HTTPError):
                logger.info(
                    "ASR stream finalization unavailable for user=%s; using full fallback",
                    current_user.email,
                    exc_info=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "ASR stream finalization failed for user=%s",
                    current_user.email,
                    exc_info=True,
                )
            return {}

        async def transcribe_full_recording() -> str:
            change_asr_active("final_transcription", 1)
            fallback_started_at = time.perf_counter()
            try:
                result = await RemoteAsrTranscriber(
                    settings,
                    client=get_shared_async_client("speech"),
                    **coordination_kwargs,
                ).transcribe(
                    recording,
                    language=model_language,
                    progress=report_progress,
                )
                record_asr_duration(
                    "full_fallback",
                    (time.perf_counter() - fallback_started_at) * 1000,
                    outcome="success",
                )
                return result
            except asyncio.CancelledError:
                record_asr_duration(
                    "full_fallback",
                    (time.perf_counter() - fallback_started_at) * 1000,
                    outcome="cancelled",
                )
                raise
            except Exception:
                record_asr_duration(
                    "full_fallback",
                    (time.perf_counter() - fallback_started_at) * 1000,
                    outcome="failed",
                )
                raise
            finally:
                change_asr_active("final_transcription", -1)

        def verified_stream_text(payload: dict[str, Any]) -> str:
            text = str(payload.get("text") or "").strip()
            authoritative = payload.get("authoritative")
            if authoritative is None:
                authoritative = getattr(preview, "authoritative_final", False)
            return text if authoritative is True else ""

        async def start_full_fallback() -> asyncio.Task[str]:
            await send_frontend(
                {
                    "type": "finalizing",
                    "code": "finalizing",
                    "completed_segments": 0,
                    "total_segments": None,
                }
            )
            return asyncio.create_task(
                transcribe_full_recording(),
                name=f"asr-full-final-{recording.recording_id[:8]}",
            )

        preview_finish_task = asyncio.create_task(
            finish_preview(),
            name=f"asr-preview-final-{recording.recording_id[:8]}",
        )
        verified_bosch_mode = bool(
            settings.asr_provider_mode == "bosch"
            and not str(getattr(settings, "asr_bosch_realtime_url", "")).strip()
            and getattr(
                settings,
                "asr_bosch_verified_stream_final_enabled",
                True,
            )
        )
        stream_payload: dict[str, Any] = {}
        if verified_bosch_mode:
            hedge_seconds = max(
                0.0,
                float(getattr(settings, "asr_bosch_final_hedge_delay_ms", 150))
                / 1000,
            )
            done, _pending = await asyncio.wait(
                {preview_finish_task},
                timeout=hedge_seconds,
            )
            if done:
                stream_payload = preview_finish_task.result()
            await close_input_task
        else:
            stream_payload, _close_result = await asyncio.gather(
                preview_finish_task,
                close_input_task,
            )

        transcript = verified_stream_text(stream_payload)
        speech_detected = bool(
            stream_payload.get("speech_detected") is True
            or _recording_has_speech(recording)
        )
        final_provider = realtime_provider_name if transcript else ""
        final_source = "verified_stream" if transcript else ""

        if not speech_detected:
            session_outcome = "no_speech_detected"
            record_asr_event("final_transcription", outcome="no_speech")
            record_asr_duration(
                "stream_finalization",
                (time.perf_counter() - stream_final_started_at) * 1000,
                outcome="no_speech",
            )
            if not preview_finish_task.done():
                preview_finish_task.cancel()
                await asyncio.gather(preview_finish_task, return_exceptions=True)
            await preview.close()
            await send_frontend(
                {
                    "type": "error",
                    "code": "no_speech_detected",
                    "message": "未检测到清晰语音，请重新录制。",
                    "recoverable": False,
                }
            )
            return

        if not transcript:
            if settings.asr_provider_mode == "local" and not bool(
                getattr(settings, "asr_remote_fallback_on_stream_failure", True)
            ):
                raise AsrFinalTranscriptionError(
                    "实时语音流未能生成完整文字，请重新录音。"
                )

            record_asr_event("full_fallback", outcome="started")
            full_transcription_task = await start_full_fallback()
            try:
                if verified_bosch_mode and not preview_finish_task.done():
                    done, _pending = await asyncio.wait(
                        {preview_finish_task, full_transcription_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if preview_finish_task in done:
                        stream_payload = preview_finish_task.result()
                        transcript = verified_stream_text(stream_payload)
                    if transcript:
                        final_provider = realtime_provider_name
                        final_source = "verified_stream"
                        if not full_transcription_task.done():
                            full_transcription_task.cancel()
                            await asyncio.gather(
                                full_transcription_task,
                                return_exceptions=True,
                            )
                    else:
                        try:
                            transcript = await full_transcription_task
                            final_provider = settings.asr_http_model
                            final_source = "full_fallback"
                            if not preview_finish_task.done():
                                preview_finish_task.cancel()
                                await asyncio.gather(
                                    preview_finish_task,
                                    return_exceptions=True,
                                )
                        except AsrFinalTranscriptionError as fallback_error:
                            if not preview_finish_task.done():
                                stream_payload = await preview_finish_task
                            transcript = verified_stream_text(stream_payload)
                            if not transcript:
                                raise fallback_error
                            final_provider = realtime_provider_name
                            final_source = "verified_stream"
                else:
                    transcript = await full_transcription_task
                    final_provider = settings.asr_http_model
                    final_source = "full_fallback"
            except AsrFinalTranscriptionError as exc:
                session_outcome = "final_transcription_failed"
                record_asr_event("final_transcription", outcome="failed")
                record_asr_duration(
                    "final_transcription",
                    (time.perf_counter() - stream_final_started_at) * 1000,
                    outcome="failed",
                )
                await send_frontend(
                    {
                        "type": "error",
                        "code": "final_transcription_failed",
                        "message": str(exc),
                        "recoverable": False,
                    }
                )
                return

        stream_outcome = "success" if final_source == "verified_stream" else "fallback"
        record_asr_event("stream_finalization", outcome=stream_outcome)
        record_asr_duration(
            "stream_finalization",
            (time.perf_counter() - stream_final_started_at) * 1000,
            outcome=stream_outcome,
        )
        record_asr_event("final_source", outcome=final_source)

        session_outcome = "success"
        record_asr_event("final_transcription", outcome="success")
        record_asr_duration(
            "final_transcription",
            (time.perf_counter() - stream_final_started_at) * 1000,
            outcome="success",
        )

        await send_frontend(
            {
                "type": "final",
                "code": "final",
                "transcript": transcript,
                "text": transcript,
                "provider": final_provider,
                "duration_seconds": round(recording.duration_seconds, 3),
                "final_source": final_source,
            }
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        session_outcome = "failed"
        logger.warning(
            "Realtime ASR failed for user=%s: %s",
            current_user.email,
            type(exc).__name__,
            exc_info=True,
        )
        await send_frontend(
            {
                "type": "error",
                "code": "realtime_failed",
                "message": "语音录音或转写失败，请重试。",
                "recoverable": False,
            }
        )
    finally:
        for task in (
            frontend_task,
            preview_connect_task,
            preview_events_task,
            preview_close_task,
            heartbeat_task,
            preview_finish_task,
            full_transcription_task,
        ):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (
                    frontend_task,
                    preview_connect_task,
                    preview_events_task,
                    preview_close_task,
                    heartbeat_task,
                    preview_finish_task,
                    full_transcription_task,
                )
                if task is not None
            ),
            return_exceptions=True,
        )
        try:
            await preview.close()
        except Exception:
            logger.debug("Realtime ASR preview cleanup failed", exc_info=True)
        await recording.discard()
        if preview_active:
            change_asr_active("preview", -1)
        if capture_active:
            record_asr_audio_bytes(recording.bytes_written)
            record_asr_duration(
                "capture",
                (time.perf_counter() - capture_started_at) * 1000,
                outcome=session_outcome,
            )
            record_asr_event("capture", outcome=session_outcome)
            change_asr_active("capture", -1)
        if slot_acquired and slots is not None:
            slots.release()
        try:
            await websocket.close(code=1000)
        except Exception:
            pass


async def _transcribe_audio(
    file: UploadFile = File(...),
    session_id: str | None = Form(default=None),
    language: str | None = Form(default=None),
) -> AsrTranscribeResponse:
    settings = get_settings()
    max_file_bytes = settings.asr_max_file_bytes
    timeout_seconds = settings.asr_timeout_seconds
    if not settings.asr_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="语音输入当前未启用。",
        )
    if not settings.asr_http_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="语音转写地址未配置。",
        )
    content_type = (file.content_type or "application/octet-stream").split(";", 1)[0].lower()
    if content_type not in _ALLOWED_AUDIO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="不支持该录音格式，请使用 Chrome 或 Edge 重新录制。",
        )

    upload_stream = file.file
    try:
        upload_size = await asyncio.to_thread(_seekable_upload_size, upload_stream)
        await asyncio.to_thread(upload_stream.seek, 0)
    except (AttributeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="无法读取录音内容。") from exc
    if upload_size <= 0:
        raise HTTPException(status_code=400, detail="没有收到录音内容。")
    if max_file_bytes > 0 and upload_size > max_file_bytes:
        raise HTTPException(status_code=413, detail="录音文件过大，请缩短录音时长。")

    try:
        headers = await ModelAPIAuth().async_headers(settings.effective_asr_api_key)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="语音转写凭据未配置。",
        ) from exc

    model_language = _asr_model_language(
        _normalize_asr_language(language, default=settings.asr_language),
        settings,
    )
    data = {"model": settings.asr_http_model}
    if model_language:
        data["language"] = model_language
    files = {
        "file": (
            file.filename or "speech.webm",
            upload_stream,
            content_type,
        )
    }
    try:
        response = await get_shared_async_client("speech").post(
            settings.asr_http_url,
            headers=headers,
            data=data,
            files=files,
            timeout=httpx.Timeout(timeout_seconds),
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "ASR transcribe request failed for session=%s: %s",
            session_id or "none",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="语音转写服务请求失败，请稍后重试。",
        ) from exc

    if response.status_code in {401, 403}:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="语音转写服务未接受当前凭据。",
        )
    if response.status_code >= 400:
        logger.warning(
            "ASR transcribe upstream returned status=%s for session=%s",
            response.status_code,
            session_id or "none",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"语音转写服务返回错误 {response.status_code}。",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="语音转写服务返回了无效响应。",
        ) from exc

    transcript = _extract_transcript(payload)
    if not transcript:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="语音转写服务未返回可用文本。",
        )
    return AsrTranscribeResponse(
        text=transcript,
        provider=settings.asr_http_model,
    )


@router.post("/transcribe", response_model=AsrTranscribeResponse)
async def asr_transcribe(
    file: UploadFile = File(...),
    session_id: str | None = Form(default=None),
    language: str | None = Form(default=None),
) -> AsrTranscribeResponse:
    return await _transcribe_audio(
        file=file,
        session_id=session_id,
        language=language,
    )

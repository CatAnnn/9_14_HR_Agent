from __future__ import annotations

import asyncio
from contextlib import aclosing, suppress
import json

from fastapi import (
    APIRouter,
    Depends,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.responses import StreamingResponse

from backend.api.dependencies import (
    get_rehearsal_service,
    get_session_service,
    get_speech_websocket_hub,
)
from backend.config.settings import get_settings
from backend.core.auth_dependency import get_current_user
from backend.schemas.api import RehearsalContextUpdateRequest, RehearsalMessageRequest
from backend.schemas.auth import AuthUserResponse
from backend.schemas.state import SessionState
from backend.services.rehearsal_service import RehearsalService
from backend.services.executor_utils import run_db_with_context
from backend.services.session_service import SessionService
from backend.services.speech_websocket_service import SpeechWebSocketHub

router = APIRouter(prefix="/rehearsal", tags=["rehearsal"])


@router.post("/{session_id}/message", response_model=SessionState)
async def send_message(session_id: str, payload: RehearsalMessageRequest, service: RehearsalService = Depends(get_rehearsal_service)):
    return await service.send_manager_message(
        session_id,
        payload.message,
        request_id=payload.request_id,
    )


def _sse(event: dict) -> str:
    event_name = str(event.get("event") or "message")
    data = {key: value for key, value in event.items() if key != "event"}
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _require_owned_workflow_session(
    websocket: WebSocket,
    session_service: SessionService,
) -> str:
    session_id = str(websocket.query_params.get("session_id") or "").strip()
    if not session_id or len(session_id) > 128:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="A valid workflow session_id is required",
        )
    try:
        session_service.get_session(session_id)
    except (KeyError, ValueError) as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Workflow session not found",
        ) from exc
    return session_id


@router.websocket("/speech/realtime")
async def speech_realtime(
    websocket: WebSocket,
    _current_user: AuthUserResponse = Depends(get_current_user),
    session_service: SessionService = Depends(get_session_service),
    speech_hub: SpeechWebSocketHub = Depends(get_speech_websocket_hub),
) -> None:
    app = getattr(websocket, "app", None)
    state = getattr(app, "state", None)
    runtime = getattr(state, "runtime", None)
    runtime_settings = getattr(runtime, "settings", None) or get_settings()
    if not runtime_settings.tts_enabled:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Employee speech output is disabled",
        )
        return
    session_id = await run_db_with_context(
        getattr(
            runtime,
            "workflow_db_executor",
            getattr(runtime, "db_executor", None),
        ),
        "rehearsal.speech.session_ownership",
        _require_owned_workflow_session,
        websocket,
        session_service,
    )
    await websocket.accept()
    connection_id = await speech_hub.register(session_id, websocket)
    await speech_hub.send_to_connection(
        session_id,
        connection_id,
        {
            "event": "speech_socket_ready",
            "session_id": session_id,
            "transport": "websocket",
            "audio_frames": "binary",
        },
    )
    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            if message.get("type") == "ping":
                await speech_hub.send_to_connection(
                    session_id,
                    connection_id,
                    {
                        "event": "speech_pong",
                        "session_id": session_id,
                    },
                )
            elif message.get("type") == "cancel":
                stream_id = message.get("speech_stream_id")
                if isinstance(stream_id, str) and 0 < len(stream_id) <= 128:
                    await speech_hub.cancel_stream(session_id, connection_id, stream_id)
            elif message.get("type") == "playback_progress":
                stream_id = message.get("speech_stream_id")
                if isinstance(stream_id, str) and 0 < len(stream_id) <= 128:
                    await speech_hub.report_playback(
                        session_id,
                        connection_id,
                        stream_id,
                        buffered_seconds=message.get("buffered_seconds"),
                        sequence=message.get("sequence"),
                    )
    except WebSocketDisconnect:
        pass
    finally:
        await speech_hub.unregister(session_id, connection_id)
        with suppress(RuntimeError):
            await websocket.close(code=1000)


@router.post("/{session_id}/message/stream")
async def stream_message(
    session_id: str,
    payload: RehearsalMessageRequest,
    service: RehearsalService = Depends(get_rehearsal_service),
    speech_hub: SpeechWebSocketHub = Depends(get_speech_websocket_hub),
):
    speech_requested = bool(payload.speech is not None and payload.speech.enabled)
    speech_voice = payload.speech.voice if payload.speech is not None else None
    speech_stream_id = (
        payload.speech.stream_id if payload.speech is not None else None
    )
    speech_enabled = bool(
        speech_requested
        and speech_stream_id
        and getattr(getattr(service, "settings", None), "tts_enabled", True)
        and await speech_hub.is_connected(session_id)
    )

    async def events():
        delivery = (
            await speech_hub.open_stream(session_id, speech_stream_id)
            if speech_enabled and speech_stream_id
            else None
        )
        completed = False
        try:
            async with aclosing(service.stream_manager_message(
                session_id,
                payload.message,
                request_id=payload.request_id,
                speech_enabled=delivery is not None,
                speech_voice=speech_voice,
                speech_stream_id=speech_stream_id,
                speech_cancel_event=delivery.cancelled if delivery is not None else None,
                speech_playback_buffer=delivery.playback if delivery is not None else None,
            )) as event_stream:
                async for event in event_stream:
                    if str(event.get("event") or "").startswith("speech_"):
                        if delivery is not None:
                            delivery.enqueue(event)
                        # Give the independent writer a turn without awaiting the client.
                        await asyncio.sleep(0)
                        continue
                    yield _sse(event)
                    await asyncio.sleep(0)
            completed = True
        finally:
            if delivery is not None:
                await speech_hub.close_stream(delivery, drain=completed)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.patch("/{session_id}/context", response_model=SessionState)
def update_rehearsal_context(
    session_id: str,
    payload: RehearsalContextUpdateRequest,
    service: RehearsalService = Depends(get_rehearsal_service),
):
    return service.update_runtime_context(
        session_id,
        runtime_note=payload.runtime_note,
        runtime_notes=payload.runtime_notes,
        clear_context=payload.clear_context,
    )


@router.post("/{session_id}/end", response_model=SessionState)
def end_rehearsal(session_id: str, service: RehearsalService = Depends(get_rehearsal_service)):
    return service.end_rehearsal(session_id)


@router.post("/{session_id}/retry", response_model=SessionState)
def retry_rehearsal(session_id: str, service: RehearsalService = Depends(get_rehearsal_service)):
    return service.retry_rehearsal(session_id)

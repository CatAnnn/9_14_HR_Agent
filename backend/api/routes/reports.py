from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.api.dependencies import get_coach_service, get_session_service
from backend.schemas.coach import CoachReport
from backend.services.coach_service import CoachService
from backend.services.session_service import SessionService

router = APIRouter(prefix="/reports", tags=["reports"])


@router.post("/{session_id}/coach", response_model=CoachReport)
async def generate_coach_report(session_id: str, service: CoachService = Depends(get_coach_service)):
    return await service.generate(session_id)


def _sse(event: dict) -> str:
    event_name = str(event.get("event") or "message")
    data = {key: value for key, value in event.items() if key != "event"}
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/{session_id}/coach/stream")
async def stream_coach_report(session_id: str, service: CoachService = Depends(get_coach_service)):
    async def events():
        async for event in service.stream_generate(session_id):
            yield _sse(event)
            await asyncio.sleep(0)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}/coach", response_model=CoachReport)
def get_coach_report(
    session_id: str,
    service: CoachService = Depends(get_coach_service),
    session_service: SessionService = Depends(get_session_service),
):
    session_service.get_session(session_id)
    return service.get(session_id)

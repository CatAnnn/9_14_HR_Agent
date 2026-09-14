from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import Response, StreamingResponse

from backend.api.dependencies import get_guidance_service, get_session_service
from backend.schemas.guidance import GuidanceReport
from backend.services.guidance_service import GuidanceService
from backend.services.session_service import SessionService
from backend.services.guidance_word_export_service import (
    GUIDANCE_WORD_MEDIA_TYPE,
    build_guidance_word_document,
    guidance_word_content_disposition,
)

router = APIRouter(prefix="/guidance", tags=["guidance"])


@router.post("/{session_id}", response_model=GuidanceReport)
async def generate_guidance(session_id: str, service: GuidanceService = Depends(get_guidance_service)):
    return await service.generate(session_id)


def _sse(event: dict) -> str:
    event_name = str(event.get("event") or "message")
    data = {key: value for key, value in event.items() if key != "event"}
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/{session_id}/stream")
async def stream_guidance(session_id: str, service: GuidanceService = Depends(get_guidance_service)):
    async def events():
        async for event in service.stream_generate(session_id):
            yield _sse(event)
            await asyncio.sleep(0)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}/export.docx")
def export_guidance_word(
    session_id: str,
    service: GuidanceService = Depends(get_guidance_service),
    session_service: SessionService = Depends(get_session_service),
):
    session_service.get_session(session_id)
    report = service.get(session_id)
    return Response(
        content=build_guidance_word_document(report),
        media_type=GUIDANCE_WORD_MEDIA_TYPE,
        headers={
            "Content-Disposition": guidance_word_content_disposition(
                session_id,
                report.locale,
            ),
            "Cache-Control": "no-store",
        },
    )


@router.get("/{session_id}", response_model=GuidanceReport)
def get_guidance(
    session_id: str,
    service: GuidanceService = Depends(get_guidance_service),
    session_service: SessionService = Depends(get_session_service),
):
    session_service.get_session(session_id)
    return service.get(session_id)

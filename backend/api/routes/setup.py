from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.api.dependencies import get_setup_service
from backend.api.dependencies import get_session_service
from backend.schemas.api import (
    ConfirmIntentRequest,
    ConfirmProfileRequest,
    ConfirmSimulationRequest,
    EmployeeLatestSetupSettingsResponse,
    IntentPerformanceDraftRequest,
    IntentPerformanceDraftResponse,
)
from backend.schemas.state import SessionState
from backend.services.session_service import SessionService
from backend.services.setup_service import SetupService

router = APIRouter(prefix="/setup", tags=["setup"])


def _sse(event: dict[str, object]) -> str:
    event_name = str(event.get("event") or "message")
    data = {key: value for key, value in event.items() if key != "event"}
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/options")
def setup_options(service: SetupService = Depends(get_setup_service)):
    return service.list_options()


@router.get(
    "/employees/{employee_id}/latest-settings",
    response_model=EmployeeLatestSetupSettingsResponse,
)
def latest_employee_settings(
    employee_id: str,
    service: SessionService = Depends(get_session_service),
):
    return service.get_latest_employee_setup_settings(employee_id)


@router.patch("/{session_id}/profile", response_model=SessionState)
def confirm_profile(session_id: str, payload: ConfirmProfileRequest, service: SetupService = Depends(get_setup_service)):
    return service.confirm_profile(session_id, payload.profile)


@router.patch("/{session_id}/intent", response_model=SessionState)
async def confirm_intent(session_id: str, payload: ConfirmIntentRequest, service: SetupService = Depends(get_setup_service)):
    return await service.confirm_intent(
        session_id,
        intent_id=payload.intent_id,
        performance_items=payload.performance_items,
    )


@router.post(
    "/{session_id}/intent-performance-draft",
    response_model=IntentPerformanceDraftResponse,
)
async def generate_intent_performance_draft(
    session_id: str,
    payload: IntentPerformanceDraftRequest,
    service: SetupService = Depends(get_setup_service),
):
    return await service.generate_intent_performance_draft(
        session_id,
        intent_id=payload.intent_id,
    )



@router.post("/{session_id}/intent-performance-draft/stream")
async def stream_intent_performance_draft(
    session_id: str,
    payload: IntentPerformanceDraftRequest,
    service: SetupService = Depends(get_setup_service),
):
    async def events():
        async for event in service.stream_intent_performance_draft(
            session_id,
            intent_id=payload.intent_id,
        ):
            yield _sse(event)
            await asyncio.sleep(0)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.patch("/{session_id}/simulation", response_model=SessionState)
def confirm_simulation(session_id: str, payload: ConfirmSimulationRequest, service: SetupService = Depends(get_setup_service)):
    return service.confirm_simulation(
        session_id,
        personality=payload.personality,
        primary_motive_id=payload.primary_motive_id,
        secondary_motive_ids=payload.secondary_motive_ids,
        run_mode=payload.run_mode,
    )


@router.post("/{session_id}/complete", response_model=SessionState)
def complete_setup(session_id: str, service: SetupService = Depends(get_setup_service)):
    return service.complete_setup(session_id)

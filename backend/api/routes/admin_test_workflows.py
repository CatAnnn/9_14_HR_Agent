from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.api.dependencies import get_session_service, get_setup_service
from backend.core.auth_dependency import require_admin_session
from backend.schemas.admin_test_workflow import AdminTestWorkflowCreateRequest
from backend.schemas.state import SessionState
from backend.services.admin_test_workflow_service import AdminTestWorkflowService
from backend.services.auth_session_service import AuthSession
from backend.services.session_service import SessionService
from backend.services.setup_service import SetupService


router = APIRouter(prefix="/admin/test-workflows", tags=["admin"])


@router.post("", response_model=SessionState)
def create_admin_test_workflow(
    payload: AdminTestWorkflowCreateRequest,
    _admin: AuthSession = Depends(require_admin_session),
    setup_service: SetupService = Depends(get_setup_service),
    session_service: SessionService = Depends(get_session_service),
) -> SessionState:
    return AdminTestWorkflowService(
        setup_service=setup_service,
        session_service=session_service,
    ).create(payload)

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from functools import lru_cache, partial
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from backend.core.auth_dependency import require_admin_session
from backend.schemas.user_record_export import (
    AdminExportConversationListResponse,
    AdminUserSessionDetail,
    UserRecordExportRequest,
)
from backend.services.auth_session_service import AuthSession
from backend.services.user_content_export_service import ExportAuditContext, ExportInProgress
from backend.services.user_record_export_service import (
    ExportSelection,
    InvalidExportSelection,
    NoExportRecords,
    UserRecordExportService,
)

router = APIRouter(prefix="/admin/exports", tags=["admin"])


@lru_cache(maxsize=1)
def get_user_content_export_service() -> UserRecordExportService:
    return UserRecordExportService()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "0.0.0.0"


def _cleanup_export_directory(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _cleanup_cancelled_export(
    directory: Path,
    task: asyncio.Task[object],
) -> None:
    try:
        task.result()
    except BaseException:
        pass
    _cleanup_export_directory(directory)


@router.post("/user-content")
async def export_selected_user_content(
    payload: UserRecordExportRequest,
    request: Request,
    admin: AuthSession = Depends(require_admin_session),
    service: UserRecordExportService = Depends(get_user_content_export_service),
) -> FileResponse:
    selection = ExportSelection.from_dates(
        user_emails=payload.user_emails,
        session_ids=payload.session_ids,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )
    return await _create_export_response(request, admin, service, selection)


@router.get(
    "/user-content/sessions",
    response_model=AdminExportConversationListResponse,
)
async def list_export_conversations(
    response: Response,
    _admin: AuthSession = Depends(require_admin_session),
    service: UserRecordExportService = Depends(get_user_content_export_service),
) -> AdminExportConversationListResponse:
    items = await asyncio.to_thread(service.list_conversations)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return AdminExportConversationListResponse(items=items)


@router.get(
    "/user-content/sessions/{session_id}",
    response_model=AdminUserSessionDetail,
)
async def get_user_session_detail(
    session_id: str,
    request: Request,
    response: Response,
    admin: AuthSession = Depends(require_admin_session),
    service: UserRecordExportService = Depends(get_user_content_export_service),
) -> AdminUserSessionDetail:
    audit_context = ExportAuditContext(
        actor_email=admin.user.email,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", "unknown"),
    )
    try:
        detail = await asyncio.to_thread(
            service.get_session_detail,
            session_id,
            audit_context=audit_context,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该使用记录。",
            headers={"Cache-Control": "no-store"},
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return detail


@router.get("/user-content")
async def export_user_content(
    request: Request,
    admin: AuthSession = Depends(require_admin_session),
    service: UserRecordExportService = Depends(get_user_content_export_service),
) -> FileResponse:
    """Backward-compatible all-user, all-time export."""
    return await _create_export_response(request, admin, service, ExportSelection())


async def _create_export_response(
    request: Request,
    admin: AuthSession,
    service: UserRecordExportService,
    selection: ExportSelection,
) -> FileResponse:
    export_directory = Path(tempfile.mkdtemp(prefix="hr_agent_user_export_"))
    os.chmod(export_directory, 0o700)
    output_path = export_directory / service.default_filename()
    audit_context = ExportAuditContext(
        actor_email=admin.user.email,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", "unknown"),
    )
    export_task = asyncio.create_task(
        asyncio.to_thread(
            service.export_to,
            output_path,
            audit_context=audit_context,
            selection=selection,
        )
    )
    try:
        artifact = await asyncio.shield(export_task)
    except asyncio.CancelledError:
        export_task.add_done_callback(
            partial(_cleanup_cancelled_export, export_directory)
        )
        raise
    except ExportInProgress as exc:
        _cleanup_export_directory(export_directory)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已有使用记录导出正在执行，请稍后重试。",
        ) from exc
    except InvalidExportSelection as exc:
        _cleanup_export_directory(export_directory)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except NoExportRecords as exc:
        _cleanup_export_directory(export_directory)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        _cleanup_export_directory(export_directory)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="使用记录导出失败。",
        ) from exc

    return FileResponse(
        path=artifact.path,
        filename=artifact.filename,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Export-SHA256": artifact.sha256,
            "X-Export-Schema-Version": "hr-agent-readable-user-records/v2",
        },
        background=BackgroundTask(
            _cleanup_export_directory,
            export_directory,
        ),
    )

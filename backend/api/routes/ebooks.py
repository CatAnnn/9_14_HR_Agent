from __future__ import annotations

from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.core.auth_dependency import get_current_session
from backend.observability.metrics import record_ebook_event
from backend.services.auth_session_service import AuthSession
from backend.services.ebook_catalog_service import EbookCatalogService


router = APIRouter(prefix="/resources/ebooks", tags=["resources"])


class EbookTelemetryEvent(BaseModel):
    event: Literal[
        "reader_open",
        "reader_ready",
        "page_change",
        "progress_restored",
        "reader_error",
        "download",
    ]
    duration_ms: float | None = Field(default=None, ge=0, le=3_600_000)


@lru_cache(maxsize=1)
def get_ebook_catalog_service() -> EbookCatalogService:
    return EbookCatalogService()


@router.api_route("/{book_id}/file", methods=["GET", "HEAD"])
async def ebook_file(
    book_id: str,
    download: bool = Query(default=False),
    session: AuthSession = Depends(get_current_session),
    catalog: EbookCatalogService = Depends(get_ebook_catalog_service),
) -> FileResponse:
    record = catalog.get(book_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到这本电子书。",
        )
    mode = "download" if download else "read"
    record_ebook_event(
        mode,
        ebook_id=record.id,
        outcome="success",
        file_bytes=record.file_path.stat().st_size,
        user_id=session.user_id,
    )
    return FileResponse(
        path=record.file_path,
        filename=record.download_filename,
        media_type="application/epub+zip",
        content_disposition_type="attachment" if download else "inline",
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{book_id}/events", status_code=status.HTTP_204_NO_CONTENT)
async def ebook_event(
    book_id: str,
    payload: EbookTelemetryEvent,
    session: AuthSession = Depends(get_current_session),
    catalog: EbookCatalogService = Depends(get_ebook_catalog_service),
) -> None:
    record = catalog.get(book_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到这本电子书。",
        )
    record_ebook_event(
        payload.event,
        ebook_id=record.id,
        outcome="success",
        duration_ms=payload.duration_ms,
        user_id=session.user_id,
    )


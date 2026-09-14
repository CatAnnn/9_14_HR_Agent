from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile, Form

from backend.api.dependencies import get_document_service
from backend.config.settings import get_settings
from backend.repositories.document_repository import DocumentRepository
from backend.schemas.api import TextDocumentRequest
from backend.schemas.document import DocumentRecord
from backend.services.upload_document import UploadDocumentService
from backend.utils.logger import get_logger

router = APIRouter(prefix="/documents", tags=["documents"])
logger = get_logger(__name__)


@router.post("/text", response_model=DocumentRecord)
async def upload_text(payload: TextDocumentRequest, service: UploadDocumentService = Depends(get_document_service)):
    logger.info(
        "documents.text.start session_id=%s text_chars=%s filename=%s",
        payload.session_id,
        len(payload.text or ""),
        payload.filename,
    )
    try:
        record = await service.process_text(text=payload.text, filename=payload.filename, session_id=payload.session_id)
    except Exception:
        logger.exception("documents.text.failed session_id=%s", payload.session_id)
        raise
    logger.info("documents.text.done session_id=%s document_id=%s", payload.session_id, record.document_id)
    return record


@router.post("/upload", response_model=DocumentRecord)
async def upload_file(
    file: UploadFile = File(...),
    session_id: str | None = Form(default=None),
    service: UploadDocumentService = Depends(get_document_service),
):
    upload_dir = get_settings().data_dir / "upload_raw"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "uploaded_document").name
    target = upload_dir / f"{uuid4().hex}_{safe_name}"
    target.write_bytes(await file.read())
    return await service.process_file(Path(target), session_id=session_id, filename=safe_name)


@router.get("/{document_id}", response_model=DocumentRecord)
def get_document(document_id: str):
    return DocumentRepository().get(document_id)

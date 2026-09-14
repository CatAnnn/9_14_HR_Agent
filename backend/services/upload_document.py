from __future__ import annotations

import asyncio
from pathlib import Path

from backend.repositories.document_repository import DocumentRepository
from backend.schemas.document import DocumentRecord
from backend.schemas.state import SessionState
from backend.services.document_pipeline import DocumentPipeline
from backend.services.session_service import SessionService


class UploadDocumentService:
    def __init__(self):
        self.repo = DocumentRepository()
        self.pipeline = DocumentPipeline()
        self.session_service = SessionService()

    async def process_text(self, text: str, filename: str | None = None, session_id: str | None = None) -> DocumentRecord:
        document_id = self.repo.new_id()
        parsed = self.pipeline.parse_text(text=text, document_id=document_id, filename=filename)
        state = self._attach_session_supplemental_info(session_id, parsed.text, label=filename or "手动补充信息")
        record = DocumentRecord(
            document_id=document_id,
            filename=filename,
            parsed_text=parsed.text,
            profile=state.employee_profile if state else None,
            pages=parsed.pages,
            blocks=parsed.blocks,
            tables=parsed.tables,
            images=parsed.images,
            metadata={**parsed.metadata, "usage": "session_supplemental_info"},
        )
        self.repo.save(record)
        return record

    async def process_file(self, path: Path, session_id: str | None = None, filename: str | None = None) -> DocumentRecord:
        document_id = self.repo.new_id()
        # MarkItDown is synchronous and can spend meaningful time in Office/PDF
        # conversion. Run it off the FastAPI event loop so one upload cannot
        # stall unrelated users while parsing is in progress.
        parsed = await asyncio.to_thread(
            self.pipeline.parse_file,
            path=path,
            document_id=document_id,
        )
        display_name = filename or path.name
        state = self._attach_session_supplemental_info(session_id, parsed.text, label=display_name)
        record = DocumentRecord(
            document_id=document_id,
            filename=display_name,
            raw_path=str(path),
            parsed_text=parsed.text,
            profile=state.employee_profile if state else None,
            pages=parsed.pages,
            blocks=parsed.blocks,
            tables=parsed.tables,
            images=parsed.images,
            metadata={**parsed.metadata, "usage": "session_supplemental_info"},
        )
        self.repo.save(record)
        return record

    def _attach_session_supplemental_info(self, session_id: str | None, text: str, *, label: str) -> SessionState | None:
        cleaned = str(text or "").strip()
        if not session_id or not cleaned:
            return None

        state = self.session_service.get_session(session_id)
        if state.employee_profile and state.employee_profile.supplemental_info:
            state.supplemental_info = self._merge_text(state.supplemental_info, state.employee_profile.supplemental_info)
            state.employee_profile.supplemental_info = None
        section = f"{label.strip() or '额外提供的信息'}：\n{cleaned}"
        state.supplemental_info = self._merge_text(state.supplemental_info, section)
        self.session_service.save_session(state)
        return state

    @staticmethod
    def _merge_text(existing: str | None, addition: str | None) -> str:
        existing_text = str(existing or "").strip()
        addition_text = str(addition or "").strip()
        if not existing_text:
            return addition_text
        if not addition_text or addition_text in existing_text:
            return existing_text
        if existing_text in addition_text:
            return addition_text
        return f"{existing_text}\n\n{addition_text}"

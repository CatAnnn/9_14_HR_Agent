from __future__ import annotations

import uuid

from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.document import DocumentRecord


class DocumentRepository:
    """PostgreSQL-backed document record repository."""

    def __init__(self, repository: PostgresRepository | None = None):
        self.repo = repository or PostgresRepository()

    def new_id(self) -> str:
        return str(uuid.uuid4())

    def save(self, record: DocumentRecord) -> DocumentRecord:
        payload = record.model_dump(mode="json")
        with self.repo.connection() as conn:
            conn.execute(
                """
                INSERT INTO documents (document_id, filename, raw_path, record_json, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (document_id) DO UPDATE SET
                    filename = excluded.filename,
                    raw_path = excluded.raw_path,
                    record_json = excluded.record_json,
                    updated_at = NOW()
                """,
                (record.document_id, record.filename, record.raw_path, self.repo.dumps(payload)),
            )
        return record

    def get(self, document_id: str) -> DocumentRecord:
        with self.repo.connection() as conn:
            row = conn.execute("SELECT record_json FROM documents WHERE document_id = %s", (document_id,)).fetchone()
        if row is None:
            raise KeyError(f"Document not found: {document_id}")
        return DocumentRecord.model_validate(row["record_json"])

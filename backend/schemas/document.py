from __future__ import annotations

from datetime import datetime, timezone
from pydantic import BaseModel, Field
from backend.schemas.profile import EmployeeProfile


class ParsedDocument(BaseModel):
    document_id: str
    filename: str | None = None
    text: str
    pages: list[dict] = Field(default_factory=list)
    blocks: list[dict] = Field(default_factory=list)
    tables: list[dict] = Field(default_factory=list)
    images: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DocumentRecord(BaseModel):
    document_id: str
    filename: str | None = None
    raw_path: str | None = None
    parsed_text: str
    profile: EmployeeProfile | None = None
    pages: list[dict] = Field(default_factory=list)
    blocks: list[dict] = Field(default_factory=list)
    tables: list[dict] = Field(default_factory=list)
    images: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

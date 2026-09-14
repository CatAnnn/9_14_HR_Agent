from __future__ import annotations

import json
from typing import Any

from backend.repositories.postgres_repository import PostgresRepository


class DocumentImageAnalysisRepository:
    """PostgreSQL cache for model-generated document image descriptions."""

    def __init__(self, repository: PostgresRepository | None = None):
        self._repo = repository

    @property
    def repo(self) -> PostgresRepository:
        if self._repo is None:
            self._repo = PostgresRepository()
        return self._repo

    def get(self, cache_key: str) -> dict[str, Any] | None:
        with self.repo.connection() as conn:
            row = conn.execute(
                "SELECT analysis_json FROM document_image_analyses WHERE cache_key = %s",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        value = row["analysis_json"]
        if isinstance(value, str):
            value = json.loads(value)
        return dict(value) if isinstance(value, dict) else None

    def save(
        self,
        *,
        cache_key: str,
        image_sha256: str,
        model_name: str,
        prompt_version: str,
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        with self.repo.connection() as conn:
            conn.execute(
                """
                INSERT INTO document_image_analyses (
                    cache_key, image_sha256, model_name, prompt_version, analysis_json, updated_at
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (cache_key) DO UPDATE SET
                    analysis_json = excluded.analysis_json,
                    updated_at = NOW()
                """,
                (
                    cache_key,
                    image_sha256,
                    model_name,
                    prompt_version,
                    self.repo.dumps(analysis),
                ),
            )
        return analysis

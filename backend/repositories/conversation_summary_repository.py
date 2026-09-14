from __future__ import annotations

from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.conversation_summary import ConversationSummaryRecord


class ConversationSummaryRepository:
    """Persistence for the latest monotonic rolling summary of a session."""

    def __init__(self, repository: PostgresRepository | None = None):
        self._repo = repository

    @property
    def repo(self) -> PostgresRepository:
        if self._repo is None:
            self._repo = PostgresRepository()
        return self._repo

    def get(self, session_id: str) -> ConversationSummaryRecord | None:
        with self.repo.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    session_id,
                    generation,
                    summary_text,
                    covered_through_turn_index,
                    model_name,
                    prompt_version
                FROM conversation_summaries
                WHERE session_id = %s
                """,
                (session_id,),
            ).fetchone()
        return self._record(row) if row else None

    def save_if_newer(
        self,
        *,
        session_id: str,
        generation: int,
        summary_text: str,
        covered_through_turn_index: int,
        model_name: str,
        prompt_version: str,
    ) -> ConversationSummaryRecord | None:
        with self.repo.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO conversation_summaries (
                    session_id,
                    generation,
                    summary_text,
                    covered_through_turn_index,
                    model_name,
                    prompt_version,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    generation = EXCLUDED.generation,
                    summary_text = EXCLUDED.summary_text,
                    covered_through_turn_index = EXCLUDED.covered_through_turn_index,
                    model_name = EXCLUDED.model_name,
                    prompt_version = EXCLUDED.prompt_version,
                    updated_at = NOW()
                WHERE
                    conversation_summaries.generation < EXCLUDED.generation
                    OR (
                        conversation_summaries.generation = EXCLUDED.generation
                        AND conversation_summaries.covered_through_turn_index
                            < EXCLUDED.covered_through_turn_index
                    )
                RETURNING
                    session_id,
                    generation,
                    summary_text,
                    covered_through_turn_index,
                    model_name,
                    prompt_version
                """,
                (
                    session_id,
                    generation,
                    summary_text,
                    covered_through_turn_index,
                    model_name,
                    prompt_version,
                ),
            ).fetchone()
        return self._record(row) if row else None

    def reset_generation(self, session_id: str) -> ConversationSummaryRecord:
        with self.repo.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO conversation_summaries (
                    session_id,
                    generation,
                    summary_text,
                    covered_through_turn_index,
                    model_name,
                    prompt_version,
                    updated_at
                )
                VALUES (%s, 1, '', 0, NULL, 'v1', NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    generation = conversation_summaries.generation + 1,
                    summary_text = '',
                    covered_through_turn_index = 0,
                    model_name = NULL,
                    prompt_version = 'v1',
                    updated_at = NOW()
                RETURNING
                    session_id,
                    generation,
                    summary_text,
                    covered_through_turn_index,
                    model_name,
                    prompt_version
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to reset the conversation summary generation.")
        return self._record(row)

    @staticmethod
    def _record(row: dict) -> ConversationSummaryRecord:
        return ConversationSummaryRecord(
            session_id=str(row["session_id"]),
            generation=int(row.get("generation") or 0),
            summary_text=str(row.get("summary_text") or ""),
            covered_through_turn_index=int(row.get("covered_through_turn_index") or 0),
            model_name=str(row["model_name"]) if row.get("model_name") else None,
            prompt_version=str(row.get("prompt_version") or "v1"),
        )


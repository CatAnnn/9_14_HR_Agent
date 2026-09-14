from __future__ import annotations

import hashlib
import json
from typing import Literal

from backend.repositories.postgres_repository import PostgresRepository
from backend.repositories.session_repository import SessionRepository
from backend.schemas.coach import CoachReport
from backend.schemas.guidance import GuidanceReport
from backend.schemas.task import CoachTaskResult
from backend.schemas.state import SessionState


ReportSource = Literal["generation", "historical_fallback", "legacy_backfill"]
_REPORT_SOURCES = frozenset({"generation", "historical_fallback", "legacy_backfill"})


class ReportRepository:
    """PostgreSQL-backed GuidanceReport / CoachReport repository."""

    def __init__(self, repository: PostgresRepository | None = None):
        self.repo = repository or PostgresRepository()

    @staticmethod
    def _serialized_report(report: GuidanceReport | CoachReport) -> tuple[str, str]:
        payload = json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialized_input_context(
        state: SessionState,
        *,
        include_conversation: bool,
    ) -> str:
        payload: dict[str, object] = {
            "snapshot_version": 2,
            "session_id": state.session_id,
            "locale": state.locale,
            "employee_profile": (
                state.employee_profile.model_dump(mode="json")
                if state.employee_profile is not None
                else None
            ),
            "supplemental_info": state.supplemental_info,
            "intent": (
                state.intent.model_dump(mode="json")
                if state.intent is not None
                else None
            ),
            "personality": (
                state.personality.model_dump(mode="json")
                if state.personality is not None
                else None
            ),
            "motivation": (
                state.motivation.model_dump(mode="json")
                if state.motivation is not None
                else None
            ),
            "conversation": (
                [
                    {
                        "turn_index": turn.turn_index,
                        "speaker": turn.speaker,
                        "text": turn.text,
                    }
                    for turn in state.conversation
                    if turn.speaker in {"manager", "employee"}
                ]
                if include_conversation
                else []
            ),
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _validated_source(source: str) -> str:
        normalized = str(source or "").strip()
        if normalized not in _REPORT_SOURCES:
            raise ValueError(f"Unsupported report source: {source!r}")
        return normalized

    def save_guidance(
        self,
        report: GuidanceReport,
        *,
        source: ReportSource = "generation",
    ) -> GuidanceReport:
        source = self._validated_source(source)
        payload, report_sha256 = self._serialized_report(report)
        with self.repo.connection() as conn:
            self._save_guidance_on_connection(
                conn,
                report,
                payload=payload,
                report_sha256=report_sha256,
                source=source,
                input_context_payload=None,
            )
        return report

    def save_guidance_with_state(
        self,
        report: GuidanceReport,
        state: SessionState,
        *,
        source: ReportSource = "generation",
    ) -> SessionState:
        source = self._validated_source(source)
        payload, report_sha256 = self._serialized_report(report)
        input_context_payload = self._serialized_input_context(
            state,
            include_conversation=False,
        )
        with self.repo.connection() as conn:
            self._save_guidance_on_connection(
                conn,
                report,
                payload=payload,
                report_sha256=report_sha256,
                source=source,
                input_context_payload=input_context_payload,
            )
            SessionRepository(self.repo).save_on_connection(
                conn,
                state,
                owner_user_id=SessionRepository._current_owner_user_id(),
            )
        return state

    @staticmethod
    def _save_guidance_on_connection(
        conn,
        report: GuidanceReport,
        *,
        payload: str,
        report_sha256: str,
        source: ReportSource = "generation",
        input_context_payload: str | None = None,
    ) -> None:
        conn.execute(
                """
                INSERT INTO guidance_report_versions (
                    session_id, report_json, report_sha256, source,
                    input_context_json
                )
                VALUES (%s, %s::jsonb, %s, %s, %s::jsonb)
                """,
                (
                    report.session_id,
                    payload,
                    report_sha256,
                    source,
                    input_context_payload,
                ),
            )
        conn.execute(
                """
                INSERT INTO guidance_reports (
                    session_id, report_json, source, updated_at
                )
                VALUES (%s, %s::jsonb, %s, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    report_json = excluded.report_json,
                    source = excluded.source,
                    updated_at = NOW()
                """,
                (report.session_id, payload, source),
            )

    def get_guidance(self, session_id: str) -> GuidanceReport:
        with self.repo.connection() as conn:
            row = conn.execute("SELECT report_json FROM guidance_reports WHERE session_id = %s", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"Guidance report not found: {session_id}")
        return GuidanceReport.model_validate(row["report_json"])

    def get_guidance_source(self, session_id: str) -> str:
        with self.repo.connection() as conn:
            row = conn.execute(
                "SELECT source FROM guidance_reports WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Guidance report not found: {session_id}")
        return str(row["source"])

    def save_coach(
        self,
        report: CoachReport,
        *,
        source: ReportSource = "generation",
    ) -> CoachReport:
        source = self._validated_source(source)
        payload, report_sha256 = self._serialized_report(report)
        with self.repo.connection() as conn:
            self._save_coach_on_connection(
                conn,
                report,
                payload=payload,
                report_sha256=report_sha256,
                source=source,
                input_context_payload=None,
            )
        return report

    def save_coach_with_state(
        self,
        report: CoachReport,
        state: SessionState,
        *,
        source: ReportSource = "generation",
    ) -> SessionState:
        source = self._validated_source(source)
        payload, report_sha256 = self._serialized_report(report)
        input_context_payload = self._serialized_input_context(
            state,
            include_conversation=True,
        )
        with self.repo.connection() as conn:
            self._save_coach_on_connection(
                conn,
                report,
                payload=payload,
                report_sha256=report_sha256,
                source=source,
                input_context_payload=input_context_payload,
            )
            SessionRepository(self.repo).save_on_connection(
                conn,
                state,
                owner_user_id=SessionRepository._current_owner_user_id(),
            )
        return state

    @staticmethod
    def _save_coach_on_connection(
        conn,
        report: CoachReport,
        *,
        payload: str,
        report_sha256: str,
        source: ReportSource = "generation",
        input_context_payload: str | None = None,
    ) -> None:
        conn.execute(
                """
                INSERT INTO coach_report_versions (
                    session_id, report_json, report_sha256, source,
                    input_context_json
                )
                VALUES (%s, %s::jsonb, %s, %s, %s::jsonb)
                """,
                (
                    report.session_id,
                    payload,
                    report_sha256,
                    source,
                    input_context_payload,
                ),
            )
        conn.execute(
                """
                INSERT INTO coach_reports (
                    session_id, report_json, source, updated_at
                )
                VALUES (%s, %s::jsonb, %s, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    report_json = excluded.report_json,
                    source = excluded.source,
                    updated_at = NOW()
                """,
                (report.session_id, payload, source),
            )

    def get_coach(self, session_id: str) -> CoachReport:
        with self.repo.connection() as conn:
            row = conn.execute("SELECT report_json FROM coach_reports WHERE session_id = %s", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"Coach report not found: {session_id}")
        return CoachReport.model_validate(row["report_json"])

    def get_coach_source(self, session_id: str) -> str:
        with self.repo.connection() as conn:
            row = conn.execute(
                "SELECT source FROM coach_reports WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Coach report not found: {session_id}")
        return str(row["source"])

    def save_coach_task_result(
        self,
        *,
        session_id: str,
        input_fingerprint: str,
        result: CoachTaskResult,
        warning: str | None,
        error: str | None,
        retrieval_ms: int,
        task_ms: int,
        pipeline_ms: int,
    ) -> CoachTaskResult:
        with self.repo.connection() as conn:
            conn.execute(
                """
                INSERT INTO coach_task_results (
                    session_id, task_id, input_fingerprint, task_name, status,
                    result_json, warning, error, retrieval_ms, task_ms,
                    pipeline_ms, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (session_id, task_id) DO UPDATE SET
                    input_fingerprint = excluded.input_fingerprint,
                    task_name = excluded.task_name,
                    status = excluded.status,
                    result_json = excluded.result_json,
                    warning = excluded.warning,
                    error = excluded.error,
                    retrieval_ms = excluded.retrieval_ms,
                    task_ms = excluded.task_ms,
                    pipeline_ms = excluded.pipeline_ms,
                    updated_at = NOW()
                """,
                (
                    session_id,
                    result.task_id,
                    input_fingerprint,
                    result.task_name,
                    result.status,
                    self.repo.dumps(result.model_dump(mode="json")),
                    warning,
                    error,
                    int(retrieval_ms),
                    int(task_ms),
                    int(pipeline_ms),
                ),
            )
        return result

    def get_coach_task_results(
        self,
        session_id: str,
        input_fingerprint: str,
    ) -> dict[str, CoachTaskResult]:
        with self.repo.connection() as conn:
            rows = conn.execute(
                """
                SELECT task_id, result_json
                FROM coach_task_results
                WHERE session_id = %s AND input_fingerprint = %s
                ORDER BY task_id
                """,
                (session_id, input_fingerprint),
            ).fetchall()
        return {
            str(row["task_id"]): CoachTaskResult.model_validate(row["result_json"])
            for row in rows
        }

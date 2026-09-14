from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

from pydantic import ValidationError

from backend.core.session_context import get_current_auth_user_id
from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.coach import CoachReport
from backend.schemas.guidance import GuidanceReport
from backend.schemas.state import SessionState


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HistoricalSessionRecord:
    """One owner-isolated prior session and its genuine model artifacts."""

    state: SessionState
    updated_at: datetime
    guidance_report: GuidanceReport | None = None
    coach_report: CoachReport | None = None
    guidance_state: SessionState | None = None
    coach_state: SessionState | None = None
    guidance_history_id: int | None = None
    coach_history_id: int | None = None
    guidance_created_at: datetime | None = None
    coach_created_at: datetime | None = None


class ModelHistoryRepository:
    """Read previous successful artifacts for one employee and one intent.

    The owner/employee/intent predicates deliberately live in SQL as well as in
    the service matcher.  This prevents an algorithmic bug from widening the
    privacy boundary after sensitive HR records have been loaded.
    """

    def __init__(self, repository: PostgresRepository | None = None):
        self.repo = repository or PostgresRepository()

    def list_candidates(
        self,
        current_state: SessionState,
        *,
        limit: int,
        max_age_days: int,
        guidance_version: str | None = None,
        culture_version: str | None = None,
        coach_version: str | None = None,
    ) -> list[HistoricalSessionRecord]:
        owner_user_id = get_current_auth_user_id()
        employee_id = str(
            current_state.employee_profile.employee_id
            if current_state.employee_profile is not None
            else ""
        ).strip()
        intent_id = str(
            current_state.intent.intent_id
            if current_state.intent is not None
            else ""
        ).strip()
        # Authentication-disabled sessions have NULL owners in PostgreSQL.  A
        # NULL-owner cross-session query would combine unrelated users, so it is
        # intentionally disabled instead of relying on similarity scoring.
        if not owner_user_id or owner_user_id == "auth-disabled":
            return []
        if not employee_id or not intent_id:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        guidance_version_clause = ""
        guidance_params: list[object] = []
        if guidance_version is not None:
            guidance_version_clause = """
                      AND history.report_json ->> 'guidance_version' = %s
                      AND (history.report_json ->> 'culture_version')
                          IS NOT DISTINCT FROM %s
            """
            guidance_params.extend((guidance_version, culture_version))

        coach_version_clause = ""
        coach_params: list[object] = []
        if coach_version is not None:
            coach_version_clause = """
                      AND history.report_json ->> 'coach_version' = %s
            """
            coach_params.append(coach_version)

        required_artifact_clause = ""
        if guidance_version is not None:
            required_artifact_clause = "AND guidance.report_json IS NOT NULL"
        elif coach_version is not None:
            required_artifact_clause = "AND coach.report_json IS NOT NULL"

        with self.repo.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    candidate.session_id,
                    candidate.state_json,
                    candidate.updated_at,
                    guidance.report_json AS guidance_report_json,
                    guidance.input_context_json AS guidance_input_context_json,
                    guidance.history_id AS guidance_history_id,
                    guidance.created_at AS guidance_created_at,
                    guidance.is_latest_overall AS guidance_is_latest_overall,
                    coach.report_json AS coach_report_json,
                    coach.input_context_json AS coach_input_context_json,
                    coach.history_id AS coach_history_id,
                    coach.created_at AS coach_created_at,
                    coach.is_latest_overall AS coach_is_latest_overall
                FROM sessions AS candidate
                LEFT JOIN LATERAL (
                    SELECT
                        history.report_json,
                        history.input_context_json,
                        history.history_id,
                        history.created_at,
                        NOT EXISTS (
                            SELECT 1
                            FROM guidance_report_versions AS newer
                            WHERE newer.session_id = history.session_id
                              AND newer.history_id > history.history_id
                        ) AS is_latest_overall
                    FROM guidance_report_versions AS history
                    WHERE history.session_id = candidate.session_id
                      AND history.source IN ('generation', 'legacy_backfill')
                      AND history.created_at >= %s
                      {guidance_version_clause}
                    ORDER BY history.history_id DESC
                    LIMIT 1
                ) AS guidance ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        history.report_json,
                        history.input_context_json,
                        history.history_id,
                        history.created_at,
                        NOT EXISTS (
                            SELECT 1
                            FROM coach_report_versions AS newer
                            WHERE newer.session_id = history.session_id
                              AND newer.history_id > history.history_id
                        ) AS is_latest_overall
                    FROM coach_report_versions AS history
                    WHERE history.session_id = candidate.session_id
                      AND history.source IN ('generation', 'legacy_backfill')
                      AND history.created_at >= %s
                      {coach_version_clause}
                    ORDER BY history.history_id DESC
                    LIMIT 1
                ) AS coach ON TRUE
                WHERE candidate.owner_user_id = %s
                  AND candidate.session_id <> %s
                  AND candidate.updated_at >= %s
                  AND NULLIF(BTRIM(
                      candidate.state_json #>> '{{employee_profile,employee_id}}'
                  ), '') = %s
                  AND NULLIF(BTRIM(
                      candidate.state_json #>> '{{intent,intent_id}}'
                  ), '') = %s
                  {required_artifact_clause}
                ORDER BY candidate.updated_at DESC, candidate.session_id DESC
                LIMIT %s
                """,
                (
                    cutoff,
                    *guidance_params,
                    cutoff,
                    *coach_params,
                    owner_user_id,
                    current_state.session_id,
                    cutoff,
                    employee_id,
                    intent_id,
                    int(limit),
                ),
            ).fetchall()

        records: list[HistoricalSessionRecord] = []
        for row in rows:
            try:
                state = SessionState.model_validate(row["state_json"])
            except (ValidationError, TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping invalid historical session state: session_id=%s error=%s",
                    row.get("session_id"),
                    exc,
                )
                continue
            database_session_id = str(row["session_id"])
            if state.session_id != database_session_id:
                logger.warning(
                    "Skipping historical row with mismatched state session id: "
                    "database_session_id=%s state_session_id=%s",
                    database_session_id,
                    state.session_id,
                )
                continue
            guidance_report = self._validated_guidance(
                row.get("guidance_report_json"),
                database_session_id,
            )
            coach_report = self._validated_coach(
                row.get("coach_report_json"),
                database_session_id,
            )
            guidance_state = self._validated_input_state(
                row.get("guidance_input_context_json"),
                database_session_id,
            )
            coach_state = self._validated_input_state(
                row.get("coach_input_context_json"),
                database_session_id,
            )

            # Legacy rows have no immutable generation snapshot.  They may be
            # used only while they are still the session's active/latest report.
            if guidance_report is not None and guidance_state is None:
                if not (
                    row.get("guidance_is_latest_overall")
                    and state.guidance_report_id == database_session_id
                ):
                    guidance_report = None
                else:
                    guidance_state = state
            if coach_report is not None and coach_state is None:
                if not (
                    row.get("coach_is_latest_overall")
                    and state.coach_report_id == database_session_id
                ):
                    coach_report = None
                else:
                    coach_state = state
            records.append(
                HistoricalSessionRecord(
                    state=state,
                    updated_at=row["updated_at"],
                    guidance_report=guidance_report,
                    coach_report=coach_report,
                    guidance_state=guidance_state,
                    coach_state=coach_state,
                    guidance_history_id=row.get("guidance_history_id"),
                    coach_history_id=row.get("coach_history_id"),
                    guidance_created_at=row.get("guidance_created_at"),
                    coach_created_at=row.get("coach_created_at"),
                )
            )
        return records

    @staticmethod
    def _validated_guidance(
        payload: object,
        session_id: str,
    ) -> GuidanceReport | None:
        if payload is None:
            return None
        try:
            report = GuidanceReport.model_validate(payload)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping invalid historical Guidance report: session_id=%s error=%s",
                session_id,
                exc,
            )
            return None
        return report if report.session_id == session_id else None

    @staticmethod
    def _validated_input_state(
        payload: object,
        session_id: str,
    ) -> SessionState | None:
        if payload is None:
            return None
        try:
            state = SessionState.model_validate(payload)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping invalid historical input snapshot: session_id=%s error=%s",
                session_id,
                exc,
            )
            return None
        return state if state.session_id == session_id else None

    @staticmethod
    def _validated_coach(
        payload: object,
        session_id: str,
    ) -> CoachReport | None:
        if payload is None:
            return None
        try:
            report = CoachReport.model_validate(payload)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping invalid historical Coach report: session_id=%s error=%s",
                session_id,
                exc,
            )
            return None
        return report if report.session_id == session_id else None

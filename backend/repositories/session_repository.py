from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import uuid

from backend.core.session_context import (
    get_current_auth_is_guest,
    get_current_auth_user_id,
)
from backend.exceptions.workflow_errors import SessionRevisionConflictError
from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.simulation import BigFivePersonality, MotivationState
from backend.schemas.locale import SessionLocale
from backend.schemas.state import SessionState


@dataclass(frozen=True)
class EmployeeReusableSettings:
    supplemental_info: str | None = None
    personality: BigFivePersonality | None = None
    motivation: MotivationState | None = None
    updated_at: datetime | None = None


class SessionRepository:
    """PostgreSQL-backed SessionState repository."""

    def __init__(self, repository: PostgresRepository | None = None):
        self.repo = repository or PostgresRepository()

    def create(self, *, locale: SessionLocale = "zh-CN") -> SessionState:
        state = SessionState(session_id=str(uuid.uuid4()), locale=locale)
        self.save(state)
        return state

    def get(self, session_id: str) -> SessionState:
        owner_user_id = get_current_auth_user_id()
        with self.repo.connection() as conn:
            row = conn.execute(
                "SELECT owner_user_id::text, state_json, revision "
                "FROM sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Session not found: {session_id}")
        row_owner_user_id = row.get("owner_user_id")
        if get_current_auth_is_guest() and row_owner_user_id is not None:
            raise KeyError(f"Session not found: {session_id}")
        if owner_user_id and owner_user_id != "auth-disabled" and row_owner_user_id != owner_user_id:
            raise KeyError(f"Session not found: {session_id}")
        state = SessionState.model_validate(row["state_json"])
        state.revision = int(row.get("revision") or 0)
        return state

    def get_latest_employee_setup(
        self,
        employee_id: str,
    ) -> EmployeeReusableSettings | None:
        normalized_employee_id = str(employee_id or "").strip()
        if not normalized_employee_id:
            return None

        owner_user_id = self._current_owner_user_id()
        owner_clause = (
            "owner_user_id IS NULL"
            if owner_user_id is None
            else "owner_user_id = %s"
        )
        params: tuple[str, ...] = (
            (normalized_employee_id,)
            if owner_user_id is None
            else (owner_user_id, normalized_employee_id)
        )
        query = (
            "WITH matching AS ("
            "SELECT state_json, updated_at, session_id FROM sessions "
            f"WHERE {owner_clause} "
            "AND state_json #>> '{employee_profile,employee_id}' = %s"
            "), latest_supplemental AS ("
            "SELECT COALESCE("
            "NULLIF(BTRIM(state_json->>'supplemental_info'), ''), "
            "NULLIF(BTRIM(state_json #>> '{employee_profile,supplemental_info}'), '')"
            ") AS supplemental_info, updated_at FROM matching WHERE ("
            "POSITION('session_supplemental_info.txt：' IN "
            "COALESCE(state_json->>'supplemental_info', '')) > 0 OR "
            "POSITION('session_supplemental_info.txt:' IN "
            "COALESCE(state_json->>'supplemental_info', '')) > 0 OR "
            "POSITION('手动补充信息：' IN "
            "COALESCE(state_json->>'supplemental_info', '')) > 0 OR "
            "POSITION('手动补充信息:' IN "
            "COALESCE(state_json->>'supplemental_info', '')) > 0 OR "
            "NULLIF(BTRIM(state_json #>> '{employee_profile,supplemental_info}'), '') "
            "IS NOT NULL"
            ") ORDER BY updated_at DESC, session_id DESC LIMIT 1"
            "), latest_personality AS ("
            "SELECT state_json->'personality' AS personality, updated_at "
            "FROM matching WHERE state_json->'personality' IS NOT NULL "
            "AND state_json->'personality' <> 'null'::jsonb "
            "ORDER BY updated_at DESC, session_id DESC LIMIT 1"
            "), latest_motivation AS ("
            "SELECT state_json->'motivation' AS motivation, updated_at "
            "FROM matching WHERE state_json->'motivation' IS NOT NULL "
            "AND state_json->'motivation' <> 'null'::jsonb "
            "ORDER BY updated_at DESC, session_id DESC LIMIT 1"
            ") SELECT latest_supplemental.supplemental_info, "
            "latest_personality.personality, latest_motivation.motivation, "
            "GREATEST(latest_supplemental.updated_at, "
            "latest_personality.updated_at, latest_motivation.updated_at) AS updated_at "
            "FROM (SELECT 1) AS singleton "
            "LEFT JOIN latest_supplemental ON TRUE "
            "LEFT JOIN latest_personality ON TRUE "
            "LEFT JOIN latest_motivation ON TRUE "
            "WHERE latest_supplemental.supplemental_info IS NOT NULL "
            "OR latest_personality.personality IS NOT NULL "
            "OR latest_motivation.motivation IS NOT NULL"
        )
        with self.repo.connection() as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        personality = row.get("personality")
        motivation = row.get("motivation")
        return EmployeeReusableSettings(
            supplemental_info=row.get("supplemental_info"),
            personality=(
                BigFivePersonality.model_validate(personality)
                if personality
                else None
            ),
            motivation=(
                MotivationState.model_validate(motivation)
                if motivation
                else None
            ),
            updated_at=row.get("updated_at"),
        )

    def save(self, state: SessionState) -> SessionState:
        owner_user_id = self._current_owner_user_id()
        with self.repo.connection() as conn:
            self.save_on_connection(conn, state, owner_user_id=owner_user_id)
        return state

    @staticmethod
    def _current_owner_user_id() -> str | None:
        owner_user_id = get_current_auth_user_id()
        return None if owner_user_id == "auth-disabled" else owner_user_id

    def save_on_connection(
        self,
        conn,
        state: SessionState,
        *,
        owner_user_id: str | None = None,
    ) -> SessionState:
        state.touch()
        payload = state.persistence_payload()
        # A guest may create and update only unowned sessions.  In particular,
        # an excluded NULL owner must never turn an existing account-owned row
        # into a writable row.  Auth-disabled local mode retains its legacy
        # unrestricted behavior for development use.
        if get_current_auth_is_guest():
            ownership_condition = (
                "sessions.owner_user_id IS NULL "
                "AND excluded.owner_user_id IS NULL"
            )
        else:
            ownership_condition = (
                "sessions.owner_user_id IS NULL "
                "OR excluded.owner_user_id IS NULL "
                "OR sessions.owner_user_id = excluded.owner_user_id"
            )
        row = conn.execute(
            f"""
            WITH saved AS (
                INSERT INTO sessions (
                    session_id, owner_user_id, state_json, revision, updated_at
                )
                VALUES (%s, %s, %s::jsonb, %s, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    owner_user_id = COALESCE(sessions.owner_user_id, excluded.owner_user_id),
                    state_json = excluded.state_json,
                    revision = sessions.revision + 1,
                    updated_at = NOW()
                WHERE sessions.revision = excluded.revision
                  AND ({ownership_condition})
                RETURNING session_id, owner_user_id::text AS owner_user_id,
                          revision, TRUE AS saved
            )
            SELECT session_id, owner_user_id, revision, saved FROM saved
            UNION ALL
            SELECT session_id, owner_user_id::text, revision, FALSE AS saved
              FROM sessions
             WHERE session_id = %s
               AND NOT EXISTS (SELECT 1 FROM saved)
            LIMIT 1
            """,
            (
                state.session_id,
                owner_user_id,
                self.repo.dumps(payload),
                int(state.revision),
                state.session_id,
            ),
        ).fetchone()
        if row is None:
            raise KeyError(f"Session not found: {state.session_id}")
        if row.get("saved") is False:
            current_owner = row.get("owner_user_id")
            if current_owner and (
                get_current_auth_is_guest()
                or (owner_user_id and owner_user_id != current_owner)
            ):
                raise KeyError(f"Session not found: {state.session_id}")
            raise SessionRevisionConflictError(
                "会话已被另一个请求更新，请刷新后重试。"
            )
        state.revision = int(row.get("revision") or 0)
        return state

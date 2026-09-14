from __future__ import annotations

from backend.config.settings import get_settings
from backend.repositories.postgres_repository import PostgresRepository


class WhitelistService:
    def __init__(self, repo: PostgresRepository | None = None, settings=None) -> None:
        self.settings = settings or get_settings()
        self.repo = repo or PostgresRepository()

    def is_allowed(self, email: str) -> bool:
        normalized = email.strip().lower()
        with self.repo.connection() as conn:
            row = conn.execute(
                "SELECT enabled FROM auth_whitelist WHERE lower(email::text) = %s",
                (normalized,),
            ).fetchone()
        if row is not None:
            return bool(row["enabled"])
        if not self.settings.auth_whitelist_enabled:
            return True
        return normalized in self.settings.auth_allowed_email_set

    def list_accounts(self) -> list[dict]:
        with self.repo.connection() as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(whitelist.email::text, users.email::text) AS email,
                       COALESCE(whitelist.enabled, TRUE) AS whitelist_enabled,
                       users.display_name,
                       COALESCE(users.role, 'user') AS role,
                       users.id IS NOT NULL AS registered,
                       COALESCE(users.is_active, FALSE) AS is_active
                FROM app_users AS users
                FULL OUTER JOIN auth_whitelist AS whitelist
                  ON lower(users.email::text) = lower(whitelist.email::text)
                ORDER BY lower(COALESCE(whitelist.email::text, users.email::text))
                """
            ).fetchall()
        accounts = [dict(row) for row in rows]
        for account in accounts:
            if account["email"].lower() in self.settings.auth_admin_email_set:
                account["role"] = "admin"
        return accounts

    def set_allowed(self, email: str, enabled: bool, note: str = "managed by administrator") -> None:
        normalized = email.strip().lower()
        with self.repo.connection() as conn:
            conn.execute(
                """
                INSERT INTO auth_whitelist (email, enabled, note, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (email) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    note = EXCLUDED.note,
                    updated_at = NOW()
                """,
                (normalized, enabled, note),
            )

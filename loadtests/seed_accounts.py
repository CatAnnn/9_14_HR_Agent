from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from backend.config.settings import get_settings
from backend.repositories.postgres_repository import PostgresRepository
from backend.services.password_service import PasswordService
from loadtests.accounts import accounts_from_environment


def _assert_isolated_database(database_url: str) -> None:
    database_name = urlparse(database_url).path.strip("/").casefold()
    if "loadtest" not in database_name:
        raise RuntimeError(
            "refusing to seed accounts outside a database whose name contains 'loadtest'"
        )


def main() -> int:
    settings = get_settings()
    database_url = settings.admin_database_url
    _assert_isolated_database(database_url)
    accounts = accounts_from_environment()
    password_hash = PasswordService().hash_password(accounts[0].password)
    repository = PostgresRepository(database_url, initialize=False)
    with repository.connection() as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("hr_agent_loadtest_account_seed",),
        )
        for account in accounts:
            connection.execute(
                """
                INSERT INTO auth_whitelist (email, enabled, note, updated_at)
                VALUES (%s, TRUE, 'isolated load-test account', NOW())
                ON CONFLICT (email) DO UPDATE SET
                    enabled = TRUE,
                    note = excluded.note,
                    updated_at = NOW()
                """,
                (account.email,),
            )
            connection.execute(
                """
                INSERT INTO app_users (
                    email, display_name, password_hash, auth_provider,
                    role, is_active, is_email_verified, updated_at
                )
                VALUES (%s, %s, %s, 'local', 'user', TRUE, TRUE, NOW())
                ON CONFLICT (email) DO UPDATE SET
                    display_name = excluded.display_name,
                    password_hash = excluded.password_hash,
                    auth_provider = 'local',
                    role = 'user',
                    is_active = TRUE,
                    is_email_verified = TRUE,
                    updated_at = NOW()
                """,
                (account.email, account.display_name, password_hash),
            )
    print(
        json.dumps(
            {
                "event": "loadtest_accounts_seeded",
                "database": urlparse(database_url).path.strip("/"),
                "account_count": len(accounts),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

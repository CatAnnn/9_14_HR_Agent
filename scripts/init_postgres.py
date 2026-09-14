from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.repositories.postgres_repository import PostgresRepository  # noqa: E402
from backend.config.settings import get_settings  # noqa: E402


if __name__ == "__main__":
    settings = get_settings()
    repository = PostgresRepository(
        settings.admin_database_url,
        initialize=False,
    )
    repository.init_schema()
    print(
        {
            "ok": True,
            "message": "PostgreSQL schema and retrieval extensions are ready.",
            "extensions": repository.installed_extension_versions(),
        }
    )

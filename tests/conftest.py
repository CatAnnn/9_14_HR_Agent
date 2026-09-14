from __future__ import annotations

import os

import pytest

from backend.repositories.postgres_repository import PostgresRepository


@pytest.fixture(autouse=True)
def isolate_unit_tests_from_postgres(monkeypatch):
    if os.getenv("POSTGRES_TEST_DATABASE_URL"):
        yield
        return

    monkeypatch.setattr(
        PostgresRepository,
        "ensure_schema_initialized",
        lambda self: None,
    )
    try:
        yield
    finally:
        PostgresRepository.close_connection_pools()

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

from backend.core.session_context import set_current_auth_user_id
from backend.exceptions.workflow_errors import SessionRevisionConflictError
from backend.repositories.session_repository import SessionRepository
from backend.schemas.state import SessionState
from backend.schemas.simulation import BigFivePersonality


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self.row = row
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, query, params):
        self.calls.append((str(query), tuple(params)))
        return _Cursor(self.row)


class _Repository:
    def __init__(self, row):
        self.connection_value = _Connection(row)

    @contextmanager
    def connection(self):
        yield self.connection_value

    @staticmethod
    def dumps(payload):
        return "serialized-state"


def test_session_save_combines_ownership_check_and_upsert() -> None:
    repository = _Repository({"session_id": "session-1"})
    service = SessionRepository(repository=repository)
    set_current_auth_user_id("owner-a")
    try:
        saved = service.save(SessionState(session_id="session-1"))
    finally:
        set_current_auth_user_id(None)

    assert saved.session_id == "session-1"
    assert len(repository.connection_value.calls) == 1
    query, params = repository.connection_value.calls[0]
    assert "INSERT INTO sessions" in query
    assert "WHERE sessions.revision = excluded.revision" in query
    assert "sessions.owner_user_id IS NULL" in query
    assert "RETURNING session_id" in query
    assert params[1] == "owner-a"


def test_session_save_preserves_not_found_error_for_other_owner() -> None:
    repository = _Repository(None)
    service = SessionRepository(repository=repository)
    set_current_auth_user_id("owner-b")
    try:
        with pytest.raises(KeyError, match="Session not found: session-1"):
            service.save(SessionState(session_id="session-1"))
    finally:
        set_current_auth_user_id(None)

    assert len(repository.connection_value.calls) == 1


def test_session_save_updates_revision_from_cas_result() -> None:
    repository = _Repository(
        {
            "session_id": "session-1",
            "owner_user_id": "owner-a",
            "revision": 4,
            "saved": True,
        }
    )
    service = SessionRepository(repository=repository)
    state = SessionState(session_id="session-1", revision=3)
    set_current_auth_user_id("owner-a")
    try:
        saved = service.save(state)
    finally:
        set_current_auth_user_id(None)

    assert saved.revision == 4
    assert repository.connection_value.calls[0][1][3] == 3


def test_session_save_rejects_stale_revision_for_same_owner() -> None:
    repository = _Repository(
        {
            "session_id": "session-1",
            "owner_user_id": "owner-a",
            "revision": 4,
            "saved": False,
        }
    )
    service = SessionRepository(repository=repository)
    set_current_auth_user_id("owner-a")
    try:
        with pytest.raises(SessionRevisionConflictError, match="另一个请求更新"):
            service.save(SessionState(session_id="session-1", revision=3))
    finally:
        set_current_auth_user_id(None)


def test_session_save_hides_existing_session_from_other_owner() -> None:
    repository = _Repository(
        {
            "session_id": "session-1",
            "owner_user_id": "owner-a",
            "revision": 4,
            "saved": False,
        }
    )
    service = SessionRepository(repository=repository)
    set_current_auth_user_id("owner-b")
    try:
        with pytest.raises(KeyError, match="Session not found: session-1"):
            service.save(SessionState(session_id="session-1", revision=3))
    finally:
        set_current_auth_user_id(None)


def test_latest_employee_setup_query_is_scoped_to_current_owner() -> None:
    updated_at = datetime(2026, 8, 18, tzinfo=timezone.utc)
    repository = _Repository(
        {
            "supplemental_info": (
                "session_supplemental_info.txt：\n上次填写的员工背景"
            ),
            "personality": {
                "openness": 61,
                "conscientiousness": 72,
                "extraversion": 43,
                "agreeableness": 58,
                "neuroticism": 37,
            },
            "motivation": {
                "primary_motive_id": "growth",
                "secondary_motive_ids": ["recognition"],
            },
            "updated_at": updated_at,
        }
    )
    service = SessionRepository(repository=repository)
    set_current_auth_user_id("owner-a")
    try:
        loaded = service.get_latest_employee_setup(" E001 ")
    finally:
        set_current_auth_user_id(None)

    assert loaded is not None
    assert loaded.supplemental_info == (
        "session_supplemental_info.txt：\n上次填写的员工背景"
    )
    assert loaded.personality == BigFivePersonality(
        openness=61,
        conscientiousness=72,
        extraversion=43,
        agreeableness=58,
        neuroticism=37,
    )
    assert loaded.motivation is not None
    assert loaded.motivation.primary_motive_id == "growth"
    assert loaded.motivation.secondary_motive_ids == ["recognition"]
    assert loaded.updated_at == updated_at
    query, params = repository.connection_value.calls[0]
    assert "owner_user_id = %s" in query
    assert "state_json #>> '{employee_profile,employee_id}' = %s" in query
    assert "latest_supplemental" in query
    assert "latest_personality" in query
    assert "latest_motivation" in query
    assert "state_json->>'intent'" not in query
    assert params == ("owner-a", "E001")

    parsed_query = PostgresQuery(Transformer())
    parsed_query.convert(query, params)
    assert parsed_query.query.count(b"$") == len(params)


def test_latest_employee_setup_skips_empty_employee_id_without_database_access() -> None:
    repository = _Repository(None)
    service = SessionRepository(repository=repository)

    assert service.get_latest_employee_setup("   ") is None
    assert repository.connection_value.calls == []

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import stat
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.api.routes import admin_exports
from backend.core.auth_dependency import get_current_session
from backend.repositories.postgres_repository import PostgresRepository
from backend.repositories.report_repository import ReportRepository
from backend.schemas.auth import AuthUserResponse
from backend.services.auth_session_service import AuthSession
from backend.services.user_content_export_service import (
    ArchiveSummary,
    ExportArtifact,
    ExportAuditContext,
    ExportInProgress,
    PostgresUserContentSnapshot,
    UserContentArchiveWriter,
    UserContentExportService,
)
from backend.services.user_record_export_service import UserRecordExportService
from scripts.export_user_content import build_parser


class _Snapshot:
    def __init__(self) -> None:
        long_supplement = "这是完整补充信息。" + ("长期内容" * 3000)
        identity = {
            "user_id": "user-1",
            "email": "user.one@bosch.com",
            "display_name": "=2+2",
            "user_role": "user",
            "user_is_active": True,
        }
        self.users = [
            {
                "user_id": "user-1",
                "email": "user.one@bosch.com",
                "display_name": "=2+2",
                "auth_provider": "local",
                "role": "user",
                "is_active": True,
                "is_email_verified": True,
                "last_login_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
                "created_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
                "session_count": 1,
                "password_hash": "must-never-leak",
                "provider_subject": "must-never-leak",
            }
        ]
        turn = {
            "turn_index": 1,
            "speaker": "manager",
            "text": "完整保留这段经理原话。",
            "created_at": "2026-07-22T10:00:00+00:00",
            "metadata": {
                "channel": "text",
                "api_key": "must-never-leak",
            },
        }
        self.sessions = [
            {
                **identity,
                "ownership_status": "owned",
                "session_id": "session-1",
                "state_json": {
                    "stage": "coach",
                    "run_mode": "rehearsal",
                    "employee_profile": {
                        "employee_id": "E-100",
                        "name": "张三",
                    },
                    "supplemental_info": long_supplement,
                    "intent": {
                        "intent_id": "development",
                        "performance_context": "已确认当前表现：完整保留。",
                    },
                    "personality": {"openness": 0.7},
                    "motivation": {"primary_motive_id": "career"},
                    "emotion_state": {"anchor_id": "neutral"},
                    "psychological_pattern_state": {
                        "last_processed_manager_turn": 1,
                        "patterns": [
                            {
                                "pattern_id": "must-never-export-pattern",
                                "strength": "strong",
                            }
                        ],
                    },
                    "conversation": [turn],
                    "warnings": [],
                    "session_token": "must-never-leak",
                    "created_at": "2026-07-22T09:00:00+00:00",
                    "updated_at": "2026-07-22T10:00:00+00:00",
                },
                "db_created_at": datetime(2026, 7, 22, 9, tzinfo=timezone.utc),
                "db_updated_at": datetime(2026, 7, 22, 10, tzinfo=timezone.utc),
                "guidance_version_count": 1,
                "coach_version_count": 1,
            }
        ]
        guidance_report = {
            "session_id": "session-1",
            "intent_id": "development",
            "guidance_version": "v2",
            "purpose": ["明确目标"],
            "opening_suggestion": ["先确认事实"],
            "risk_preview": ["避免标签化"],
            "response_strategies": ["使用员工原话"],
            "safer_phrases": ["我们先对齐事实。"],
            "dimension_points": [{"title": "开场", "descriptions": ["明确沟通目的。"]}],
            "evidence_policy": "conversation_only",
            "citations": [],
            "disclaimer": "",
        }
        self.guidance_versions = [
            {
                **identity,
                "history_id": 10,
                "version_number": 1,
                "is_current": True,
                "session_id": "session-1",
                "report_sha256": "a" * 64,
                "source": "generation",
                "report_created_at": datetime(2026, 7, 22, 9, 30, tzinfo=timezone.utc),
                "report_json": guidance_report,
            }
        ]
        self.turns = [
            {
                **identity,
                "session_id": "session-1",
                "stored_position": 1,
                "turn_json": turn,
            }
        ]
        task_result = {
            "task_id": "start",
            "task_name": "开场",
            "status": "success",
            "score": 4,
            "summary": "开场目标清晰。",
            "dimension_scores": [{"dimension_id": "start", "score": 4}],
            "evidence": [{"turn_index": 1, "quote": "完整保留这段经理原话。"}],
            "strengths": ["目标明确"],
            "improvement_points": ["补充时间安排"],
            "risks": [],
            "better_phrases": [{"original": "原话", "suggested": "建议表达"}],
            "career_elements_advice": [
                {
                    "element": "跨职能经历",
                    "suggestion": "通过跨部门项目扩大协作影响。",
                    "reason": "用于验证跨职能场景中的稳定表现。",
                }
            ],
            "citations": [],
            "extra": {"traceback": "must-never-leak"},
        }
        coach_report = {
            "session_id": "session-1",
            "coach_version": "v2",
            "status": "success",
            "task_results": [task_result],
            "disclaimer": "",
        }
        version_identity = {
            **identity,
            "history_id": 20,
            "version_number": 1,
            "is_current": True,
            "session_id": "session-1",
            "report_sha256": "b" * 64,
            "source": "generation",
            "report_created_at": datetime(2026, 7, 22, 11, tzinfo=timezone.utc),
        }
        self.coach_versions = [
            {
                **version_identity,
                "report_json": coach_report,
            }
        ]
        self.coach_dimensions = [
            {
                **version_identity,
                "report_json": coach_report,
                "task_position": 1,
                "task_result_json": task_result,
            }
        ]
        self.checkpoints = [
            {
                **identity,
                "session_id": "session-1",
                "task_id": "start",
                "task_name": "开场",
                "status": "success",
                "result_json": task_result,
                "retrieval_ms": 120,
                "task_ms": 800,
                "pipeline_ms": 920,
                "created_at": datetime(2026, 7, 22, 10, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 7, 22, 10, 1, tzinfo=timezone.utc),
                "has_final_report": True,
                "input_fingerprint": "must-never-leak",
                "internal_exception_text": "must-never-leak",
            }
        ]
        self.orphans = [
            {
                "record_type": "session_without_user",
                "session_id": "legacy-session",
                "record_json": {
                    "supplemental_info": "旧会话完整内容",
                    "psychological_pattern_state": {
                        "patterns": ["must-never-export-orphan-pattern"]
                    },
                },
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
            }
        ]
        self.long_supplement = long_supplement

    def iter_users(self):
        return iter(self.users)

    def iter_sessions(self):
        return iter(self.sessions)

    def iter_guidance_versions(self):
        return iter(self.guidance_versions)

    def iter_rehearsal_turns(self):
        return iter(self.turns)

    def iter_coach_versions(self):
        return iter(self.coach_versions)

    def iter_coach_dimension_rows(self):
        return iter(self.coach_dimensions)

    def iter_coach_task_checkpoints(self):
        return iter(self.checkpoints)

    def iter_orphan_records(self):
        return iter(self.orphans)


def test_archive_contains_csv_and_lossless_jsonl_without_secrets(tmp_path: Path):
    snapshot = _Snapshot()
    output = tmp_path / "export.zip"

    summary = UserContentArchiveWriter().write(
        output,
        snapshot,
        requested_by="admin@bosch.com",
    )

    assert summary.counts == {
        "users": 1,
        "sessions": 1,
        "guidance_report_versions": 1,
        "rehearsal_turns": 1,
        "coach_report_versions": 1,
        "coach_dimension_rows": 1,
        "coach_task_checkpoints": 1,
        "orphan_records": 1,
    }
    assert summary.size_bytes == output.stat().st_size
    assert summary.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()

    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "README.txt",
            "manifest.json",
            "users.csv",
            "sessions.csv",
            "guidance_report_versions.csv",
            "rehearsal_turns.csv",
            "coach_report_versions.csv",
            "coach_task_checkpoints.csv",
            "raw/users.jsonl",
            "raw/sessions.jsonl",
            "raw/guidance_report_versions.jsonl",
            "raw/rehearsal_turns.jsonl",
            "raw/coach_report_versions.jsonl",
            "raw/coach_task_checkpoints.jsonl",
            "raw/orphan_records.jsonl",
        }
        users_csv = archive.read("users.csv")
        assert users_csv.startswith(b"\xef\xbb\xbf")
        user_row = next(csv.DictReader(io.StringIO(users_csv.decode("utf-8-sig"))))
        assert user_row["display_name"] == "'=2+2"

        session_row = next(
            csv.DictReader(
                io.StringIO(archive.read("sessions.csv").decode("utf-8-sig"))
            )
        )
        assert session_row["supplemental_info"] == snapshot.long_supplement
        assert session_row["performance_context"] == "已确认当前表现：完整保留。"

        expected_career_elements_advice = snapshot.coach_versions[0][
            "report_json"
        ]["task_results"][0]["career_elements_advice"]
        coach_dimension_row = next(
            csv.DictReader(
                io.StringIO(
                    archive.read("coach_report_versions.csv").decode("utf-8-sig")
                )
            )
        )
        checkpoint_row = next(
            csv.DictReader(
                io.StringIO(
                    archive.read("coach_task_checkpoints.csv").decode("utf-8-sig")
                )
            )
        )
        assert json.loads(coach_dimension_row["career_elements_advice_json"]) == (
            expected_career_elements_advice
        )
        assert json.loads(checkpoint_row["career_elements_advice_json"]) == (
            expected_career_elements_advice
        )

        raw_session = json.loads(
            archive.read("raw/sessions.jsonl").decode("utf-8").strip()
        )
        assert raw_session["state_json"]["supplemental_info"] == snapshot.long_supplement
        assert raw_session["state_json"]["intent"]["performance_context"] == (
            "已确认当前表现：完整保留。"
        )
        assert raw_session["state_json"]["conversation"][0]["text"] == "完整保留这段经理原话。"
        assert "psychological_pattern_state" not in raw_session["state_json"]

        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema"] == "hr-agent-user-content/v1"
        assert manifest["counts"] == summary.counts
        assert "psychological_pattern_state" in manifest["excluded"]

        exported_text = "\n".join(
            archive.read(name).decode("utf-8-sig")
            for name in archive.namelist()
            if name.endswith((".csv", ".jsonl"))
        )
        for forbidden in (
            "must-never-leak",
            "password_hash",
            "provider_subject",
            "session_token",
            "input_fingerprint",
            "internal_exception_text",
            "traceback",
            "api_key",
            "psychological_pattern_state",
            "must-never-export-pattern",
            "must-never-export-orphan-pattern",
        ):
            assert forbidden not in exported_text


class _EmptyNamedCursor:
    def __init__(self, queries: list[str]) -> None:
        self.queries = queries

    def execute(self, query: str) -> None:
        self.queries.append(query)

    @staticmethod
    def fetchmany(_batch_size: int) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def close() -> None:
        return None


class _SnapshotQueryConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def cursor(self, *, name: str) -> _EmptyNamedCursor:
        assert name.startswith("user_content_export_")
        return _EmptyNamedCursor(self.queries)


def test_postgres_snapshot_removes_pattern_state_before_export_rows_exist():
    connection = _SnapshotQueryConnection()
    snapshot = PostgresUserContentSnapshot(connection)

    assert list(snapshot.iter_sessions()) == []
    assert list(snapshot.iter_orphan_records()) == []

    assert len(connection.queries) == 2
    for query in connection.queries:
        assert "- 'personality_facets'" in query
        assert "- 'psychological_pattern_state'" in query


class _ExecuteResult:
    def __init__(self, row: dict[str, Any] | None = None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


class _RecordingConnection:
    def __init__(self, *, lock_acquired: bool = True):
        self.lock_acquired = lock_acquired
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None):
        self.calls.append((query, params))
        if "pg_try_advisory_xact_lock" in query:
            return _ExecuteResult({"acquired": self.lock_acquired})
        return _ExecuteResult()


class _RecordingRepository:
    def __init__(self, *, lock_acquired: bool = True):
        self.lock_acquired = lock_acquired
        self.connections: list[_RecordingConnection] = []

    @contextmanager
    def connection(self):
        connection = _RecordingConnection(lock_acquired=self.lock_acquired)
        self.connections.append(connection)
        yield connection


class _SmallArchiveWriter:
    def __init__(self):
        self.called = False

    def write(self, output_path: Path, _snapshot: Any, *, requested_by: str):
        self.called = True
        payload = b"valid-zip-placeholder"
        output_path.write_bytes(payload)
        return ArchiveSummary(
            generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
            counts={"users": 1},
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )


def _export_service(repository, writer):
    service = object.__new__(UserContentExportService)
    service.repository = repository
    service.archive_writer = writer
    service.settings = SimpleNamespace(auth_admin_email_set={"admin@bosch.com"})
    return service


def test_export_service_uses_snapshot_lock_permissions_and_audit(tmp_path: Path):
    repository = _RecordingRepository()
    writer = _SmallArchiveWriter()
    service = _export_service(repository, writer)
    output = tmp_path / "full.zip"

    artifact = service.export_to(
        output,
        audit_context=ExportAuditContext(
            actor_email="Admin@Bosch.com",
            ip_address="127.0.0.1",
            user_agent="pytest",
        ),
    )

    assert writer.called is True
    assert artifact.path == output
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    snapshot_sql = "\n".join(query for query, _ in repository.connections[0].calls)
    assert "REPEATABLE READ READ ONLY" in snapshot_sql
    assert "pg_try_advisory_xact_lock" in snapshot_sql

    audit_query, audit_params = repository.connections[1].calls[0]
    assert "admin_export_user_content" in audit_query
    assert audit_params[0] == "admin@bosch.com"
    reason = json.loads(audit_params[2])
    assert reason["archive_sha256"] == artifact.sha256
    assert reason["counts"] == {"users": 1}


def test_export_lock_conflict_removes_partial_file_and_records_failure(tmp_path: Path):
    repository = _RecordingRepository(lock_acquired=False)
    writer = _SmallArchiveWriter()
    service = _export_service(repository, writer)
    output = tmp_path / "blocked.zip"

    with pytest.raises(ExportInProgress):
        service.export_to(
            output,
            audit_context=ExportAuditContext(
                actor_email="admin@bosch.com",
                ip_address="127.0.0.1",
                user_agent="pytest",
            ),
        )

    assert writer.called is False
    assert not output.exists()
    assert not list(tmp_path.glob("*.partial"))
    reason = json.loads(repository.connections[1].calls[0][1][2])
    assert reason == {"error_type": "ExportInProgress"}


class _ReportModel:
    def __init__(self, session_id: str, value: str):
        self.session_id = session_id
        self.value = value

    def model_dump(self, *, mode: str):
        assert mode == "json"
        return {"session_id": self.session_id, "value": self.value}


def test_report_save_appends_history_and_updates_latest_on_one_connection():
    repository = _RecordingRepository()
    reports = ReportRepository(repository=repository)

    reports.save_guidance(_ReportModel("session-1", "指导"))
    reports.save_coach(_ReportModel("session-1", "复盘"))

    assert len(repository.connections) == 2
    for connection, history_table, latest_table in (
        (
            repository.connections[0],
            "guidance_report_versions",
            "guidance_reports",
        ),
        (
            repository.connections[1],
            "coach_report_versions",
            "coach_reports",
        ),
    ):
        assert len(connection.calls) == 2
        assert history_table in connection.calls[0][0]
        assert connection.calls[0][1][3] == "generation"
        assert latest_table in connection.calls[1][0]
        assert connection.calls[1][1][2] == "generation"
        payload = connection.calls[0][1][1]
        report_sha256 = connection.calls[0][1][2]
        assert report_sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert connection.calls[1][1][1] == payload


class _RouteExportService:
    def __init__(self):
        self.calls = 0
        self.detail_calls = 0
        self.last_path: Path | None = None
        self.selection = None

    @staticmethod
    def default_filename():
        return "hr_agent_user_content_test.zip"

    @staticmethod
    def list_conversations():
        return [
            {
                "session_id": "session-1",
                "user_email": "user.one@bosch.com",
                "user_display_name": "王经理",
                "employee_name": "张三",
                "intent_id": "development",
                "stage": "rehearsal",
                "session_created_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
                "conversation_started_at": datetime(
                    2026, 7, 22, 1, tzinfo=timezone.utc
                ),
                "conversation_ended_at": datetime(
                    2026, 7, 22, 1, 5, tzinfo=timezone.utc
                ),
                "turn_count": 4,
                "has_conversation": True,
                "has_rehearsal": True,
            }
        ]

    def export_to(
        self,
        output_path: Path,
        *,
        audit_context: ExportAuditContext,
        selection=None,
    ):
        self.calls += 1
        self.last_path = output_path
        self.selection = selection
        payload = b"downloaded-zip"
        output_path.write_bytes(payload)
        return ExportArtifact(
            path=output_path,
            filename=output_path.name,
            generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
            counts={"users": 1},
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )

    def get_session_detail(self, session_id, *, audit_context):
        self.detail_calls += 1
        if session_id == "missing-session":
            raise KeyError(session_id)
        assert audit_context.actor_email == "admin@bosch.com"
        return {
            "read_only": True,
            "session_id": session_id,
            "user_email": "user.one@bosch.com",
            "user_display_name": "王经理",
            "stage": "report_ready",
            "run_mode": "guidance_then_rehearsal",
            "locale": "zh-CN",
            "session_created_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
            "session_updated_at": datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
            "rehearsal_ended_at": None,
            "employee_profile": None,
            "supplemental_info": None,
            "intent": None,
            "personality": None,
            "motivation": None,
            "runtime_notes": [],
            "conversation": [],
            "guidance": None,
            "coach_tasks": [],
        }


def _auth_session(role: str) -> AuthSession:
    return AuthSession(
        session_id="auth-session",
        user=AuthUserResponse(
            email=f"{role}@bosch.com",
            display_name=role,
            role=role,
        ),
        user_id=f"{role}-id",
        role=role,
        created_at=1.0,
        last_seen_at=2.0,
    )


def _export_test_app(session: AuthSession, service: _RouteExportService):
    app = FastAPI()
    app.include_router(admin_exports.router)
    app.dependency_overrides[get_current_session] = lambda: session
    app.dependency_overrides[
        admin_exports.get_user_content_export_service
    ] = lambda: service
    return app


def test_export_route_rejects_non_admin_before_generating_file():
    service = _RouteExportService()
    with TestClient(_export_test_app(_auth_session("user"), service)) as client:
        response = client.get("/admin/exports/user-content")

    assert response.status_code == 403
    assert service.calls == 0


def test_export_route_returns_conflict_when_an_export_is_running():
    service = _RouteExportService()
    service.export_to = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ExportInProgress("busy")
    )
    with TestClient(_export_test_app(_auth_session("admin"), service)) as client:
        response = client.get("/admin/exports/user-content")
    assert response.status_code == 409


class _SlowRouteExportService(_RouteExportService):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def export_to(
        self,
        output_path: Path,
        *,
        audit_context: ExportAuditContext,
        selection=None,
    ):
        self.last_path = output_path
        self.started.set()
        self.release.wait(timeout=2)
        return super().export_to(
            output_path,
            audit_context=audit_context,
            selection=selection,
        )


@pytest.mark.asyncio
async def test_cancelled_export_request_cleans_directory_after_worker_finishes():
    service = _SlowRouteExportService()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/exports/user-content",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
    )
    task = asyncio.create_task(
        admin_exports.export_user_content(
            request,
            _auth_session("admin"),
            service,
        )
    )
    assert await asyncio.to_thread(service.started.wait, 1)
    assert service.last_path is not None
    export_directory = service.last_path.parent

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert export_directory.exists()

    service.release.set()
    for _ in range(100):
        if not export_directory.exists():
            break
        await asyncio.sleep(0.01)
    assert not export_directory.exists()


def test_export_route_downloads_zip_and_cleans_temporary_directory():
    service = _RouteExportService()
    with TestClient(_export_test_app(_auth_session("admin"), service)) as client:
        response = client.get("/admin/exports/user-content")

    assert response.status_code == 200
    assert response.content == b"downloaded-zip"
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-export-schema-version"] == (
        "hr-agent-readable-user-records/v2"
    )
    assert "hr_agent_user_content_test.zip" in response.headers["content-disposition"]
    assert service.last_path is not None
    assert not service.last_path.parent.exists()


def test_export_route_accepts_batch_users_and_inclusive_dates():
    service = _RouteExportService()
    with TestClient(_export_test_app(_auth_session("admin"), service)) as client:
        response = client.post(
            "/admin/exports/user-content",
            json={
                "user_emails": ["User.One@bosch.com", "user.two@bosch.com"],
                "session_ids": ["session-2", "session-1", "session-2"],
                "start_date": "2026-07-01",
                "end_date": "2026-07-22",
            },
        )

    assert response.status_code == 200
    assert service.selection.user_emails == (
        "user.one@bosch.com",
        "user.two@bosch.com",
    )
    assert service.selection.session_ids == ("session-2", "session-1")
    assert service.selection.start_at.isoformat() == "2026-06-30T16:00:00+00:00"
    assert service.selection.end_at.isoformat() == "2026-07-22T16:00:00+00:00"


def test_export_conversation_catalog_is_admin_only_and_returns_dates():
    service = _RouteExportService()
    with TestClient(_export_test_app(_auth_session("admin"), service)) as client:
        response = client.get("/admin/exports/user-content/sessions")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    item = response.json()["items"][0]
    assert item["session_id"] == "session-1"
    assert item["conversation_started_at"] == "2026-07-22T01:00:00Z"
    assert item["turn_count"] == 4
    assert item["has_rehearsal"] is True

    with TestClient(_export_test_app(_auth_session("user"), service)) as client:
        forbidden = client.get("/admin/exports/user-content/sessions")
    assert forbidden.status_code == 403


def test_admin_can_open_read_only_user_session_detail_without_caching():
    service = _RouteExportService()
    with TestClient(_export_test_app(_auth_session("admin"), service)) as client:
        response = client.get(
            "/admin/exports/user-content/sessions/session-1",
            headers={"user-agent": "pytest-detail"},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["read_only"] is True
    assert response.json()["session_id"] == "session-1"
    assert service.detail_calls == 1


def test_user_cannot_open_another_users_session_detail():
    service = _RouteExportService()
    with TestClient(_export_test_app(_auth_session("user"), service)) as client:
        response = client.get("/admin/exports/user-content/sessions/session-1")

    assert response.status_code == 403
    assert service.detail_calls == 0


def test_admin_user_session_detail_returns_not_found_without_caching():
    service = _RouteExportService()
    with TestClient(_export_test_app(_auth_session("admin"), service)) as client:
        response = client.get(
            "/admin/exports/user-content/sessions/missing-session"
        )

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert service.detail_calls == 1


def test_admin_coach_projection_keeps_legacy_suggestions_aligned_by_index():
    tasks = UserRecordExportService._project_admin_coach_tasks(
        {
            "task_results": [
                {
                    "task_id": "emotion",
                    "task_name": "情绪承接",
                    "status": "success",
                    "improvement_points": ["问题一", "问题二", "问题三"],
                    "better_phrases": ["旧版字符串建议", {}, {"suggestion": "建议三"}],
                }
            ]
        },
        [],
    )

    assert len(tasks) == 1
    assert len(tasks[0].better_phrases) == 3
    assert tasks[0].better_phrases[0].suggestion == "旧版字符串建议"
    assert tasks[0].better_phrases[1].suggestion is None
    assert tasks[0].better_phrases[2].suggestion == "建议三"


def test_export_cli_requires_requester_and_parses_output(tmp_path: Path):
    with pytest.raises(SystemExit):
        build_parser().parse_args([])

    args = build_parser().parse_args(
        [
            "--requested-by",
            "admin@bosch.com",
            "--output",
            str(tmp_path / "content.zip"),
            "--user",
            "user.one@bosch.com",
            "--user",
            "user.two@bosch.com",
            "--start-date",
            "2026-07-01",
            "--end-date",
            "2026-07-22",
        ]
    )
    assert args.requested_by == "admin@bosch.com"
    assert args.output == tmp_path / "content.zip"
    assert args.user_emails == ["user.one@bosch.com", "user.two@bosch.com"]
    assert args.start_date.isoformat() == "2026-07-01"
    assert args.end_date.isoformat() == "2026-07-22"


class _SchemaConnection:
    def __init__(self):
        self.queries: list[str] = []

    def execute(self, query: str, _params: Any = None):
        self.queries.append(query)
        return _ExecuteResult()


class _SchemaRepository(PostgresRepository):
    def __init__(self):
        self.schema_connection = _SchemaConnection()
        self.settings = SimpleNamespace(
            auth_allowed_email_set=set(),
            auth_admin_email_set=set(),
        )

    @contextmanager
    def connection(self):
        yield self.schema_connection

    def _ensure_retrieval_extensions(self, _connection):
        return None

    def _validate_retrieval_runtime(self, _connection):
        return None


def test_schema_initialization_creates_and_idempotently_backfills_report_history():
    repository = _SchemaRepository()

    repository.init_schema()

    sql = "\n".join(repository.schema_connection.queries)
    assert "CREATE TABLE IF NOT EXISTS guidance_report_versions" in sql
    assert "CREATE TABLE IF NOT EXISTS coach_report_versions" in sql
    assert "uq_guidance_report_legacy_backfill" in sql
    assert "uq_coach_report_legacy_backfill" in sql
    backfills = [
        query
        for query in repository.schema_connection.queries
        if "INSERT INTO" in query and "'legacy_backfill'" in query
    ]
    assert len(backfills) == 2
    assert all("WHERE NOT EXISTS" in query for query in backfills)
    assert all("INNER JOIN sessions" in query for query in backfills)
    assert all(
        "ON CONFLICT (session_id) WHERE source = 'legacy_backfill'" in query
        for query in backfills
    )

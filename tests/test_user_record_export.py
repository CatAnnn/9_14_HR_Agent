from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backend.services.user_record_export_service import (
    ExportSelection,
    NoExportRecords,
    PostgresUserRecordSnapshot,
    ReadableUserRecordArchiveWriter,
    UserRecordExportService,
)
from backend.services.user_content_export_service import ExportAuditContext


class _ReadableSnapshot:
    def __init__(self) -> None:
        self.users = [
            {
                "user_id": "user-1",
                "email": "user.one@bosch.com",
                "display_name": "王经理",
                "session_count": 1,
                "first_session_at": datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
                "last_session_at": datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
            }
        ]
        self.sessions = [
            {
                "user_id": "user-1",
                "email": "user.one@bosch.com",
                "session_id": "session-1",
                "session_created_at": datetime(
                    2026, 7, 22, 1, tzinfo=timezone.utc
                ),
                "state_json": {
                    "employee_profile": {
                        "employee_alias": "张三",
                        "role": "工程师",
                        "level": "L4",
                        "performance_rating": "3",
                        "key_goals": ["按期交付项目"],
                        "facts": [
                            {
                                "description": "关键里程碑延期一周",
                                "impact": "影响下游联调",
                                "evidence_source": "项目周报",
                            }
                        ],
                        "past_ratings": ["2025 年：2"],
                        "historical_feedback": ["需要加强跨团队协作"],
                        "management_actions": ["已安排每周检查点"],
                        "previous_improvement_discussion": "yes",
                        "has_pip": "no",
                        "involves_promotion_salary_transfer": "yes",
                        "sensitive_constraints": {
                            "promotion_timing": {
                                "status": "yes",
                                "business_impact_summary": "需要说明晋升评审节奏",
                            }
                        },
                        "source_profile_text": "不应直接导出的原始资料",
                    },
                    "supplemental_info": "本季度需要讨论发展路径。",
                    "run_mode": "guidance_then_rehearsal",
                    "intent": {
                        "intent_id": "development",
                        "performance_context": "已有表现：按期完成重点目标。",
                        "config": {
                            "name": "发展型反馈",
                            "business_goal": "帮助员工明确下一阶段发展重点。",
                            "expected_outcome": "形成双方认可的发展行动。",
                            "red_lines": ["不承诺未经审批的晋升安排。"],
                        },
                    },
                    "personality": {
                        "openness": 72,
                        "conscientiousness": 84,
                        "extraversion": 45,
                        "agreeableness": 67,
                        "neuroticism": 28,
                    },
                    "motivation": {
                        "primary_motive_id": "security",
                        "secondary_motive_ids": ["recognition", "affiliation"],
                    },
                    "rehearsal_context": {
                        "runtime_notes": ["员工近期对晋升节奏比较敏感。"]
                    },
                    "conversation": [
                        {
                            "turn_index": 1,
                            "speaker": "manager",
                            "text": "我们先回顾本季度的结果。",
                            "metadata": {"api_key": "must-never-leak"},
                        },
                        {
                            "turn_index": 2,
                            "speaker": "employee",
                            "text": "我希望明确下一阶段的发展方向。",
                        },
                        {
                            "turn_index": 3,
                            "speaker": "system",
                            "text": "内部系统消息不应导出。",
                        },
                    ],
                },
                "guidance_report_json": {
                    "dimension_points": {
                        "start": [
                            {
                                "title": "先对齐目标",
                                "details": ["说明本次沟通关注发展和下一步。"],
                            }
                        ],
                        "emotion": [],
                        "requirement": [],
                        "plan": [],
                    },
                    "citations": [{"chunk_id": "raw-chunk"}],
                },
                "coach_report_json": {
                    "task_results": [
                        {
                            "task_id": "start",
                            "task_name": "开场",
                            "status": "success",
                            "score": 4,
                            "summary": "沟通目标清楚。",
                            "dimension_scores": [
                                {"basis": "开场直接说明了沟通目的。"}
                            ],
                            "strengths": ["先说明目标"],
                            "improvement_points": ["补充时间安排"],
                            "evidence": [
                                {
                                    "turn_index": 1,
                                    "quote": "我们先回顾本季度的结果。",
                                }
                            ],
                            "better_phrases": [
                                {
                                    "original": "回顾结果",
                                    "suggestion": "先回顾结果，再确认发展方向。",
                                    "reason": "让沟通路径更清楚。",
                                }
                            ],
                            "extra": {"traceback": "must-never-leak"},
                        }
                    ],
                    "report_sha256": "must-never-leak",
                },
                "checkpoint_results": [],
            }
        ]

    def iter_users(self):
        return iter(self.users)

    def iter_session_records(self):
        return iter(self.sessions)


class _EmptySnapshot:
    def iter_users(self):
        return iter(())

    def iter_session_records(self):
        return iter(())


def test_readable_archive_contains_only_key_text_reports(tmp_path: Path):
    output = tmp_path / "records.zip"
    selection = ExportSelection.from_dates(
        user_emails=["USER.ONE@bosch.com"],
        start_date=date(2026, 7, 22),
        end_date=date(2026, 7, 22),
    )

    summary = ReadableUserRecordArchiveWriter().write(
        output,
        _ReadableSnapshot(),
        requested_by="admin@bosch.com",
        selection=selection,
    )

    assert summary.counts == {
        "users": 1,
        "sessions": 1,
        "guidance_reports": 1,
        "rehearsal_turns": 2,
        "coach_reports": 1,
        "coach_checkpoints": 0,
    }
    assert summary.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert names[0] == "README.txt"
        assert names[-1] == "summary.txt"
        assert len([name for name in names if name.startswith("users/")]) == 1
        assert all(name.endswith(".txt") for name in names)
        assert not any(name.endswith((".json", ".jsonl", ".csv")) for name in names)
        user_report_name = next(name for name in names if name.startswith("users/"))
        report = archive.read(user_report_name).decode("utf-8-sig")
        assert "用户邮箱：user.one@bosch.com" in report
        assert "【用户选择与输入】" in report
        assert "会话流程：谈前指导后进入多轮预演" in report
        assert "沟通意图：发展型反馈" in report
        assert "已确认当前表现：" in report
        assert "已有表现：按期完成重点目标。" in report
        assert "沟通业务目标：" not in report
        assert "期望沟通结果：" not in report
        assert "沟通注意事项：" not in report
        assert "接受新事物程度：72/100（更愿意尝试新方案）" in report
        assert "做事靠谱程度：84/100（很重视计划和兑现）" in report
        assert "员工主要诉求：稳定" in report
        assert "员工辅助诉求：认可、融洽团队" in report
        assert "员工近期对晋升节奏比较敏感。" in report
        assert "历史绩效结果：" in report
        assert "2025 年：2" in report
        assert "关键里程碑延期一周；影响：影响下游联调；依据：项目周报" in report
        assert "是否进行过改进沟通：是" in report
        assert "是否已有绩效改进计划：否" in report
        assert "promotion timing：是；业务影响：需要说明晋升评审节奏" in report
        assert "员工背景原始输入：" in report
        assert "不应直接导出的原始资料" in report
        assert "额外提供的信息：" in report
        assert "本季度需要讨论发展路径。" in report
        assert "先对齐目标" in report
        assert "第 1 轮 经理：我们先回顾本季度的结果。" in report
        assert "开场（已完成，评分 4/5）" in report
        assert "建议：先回顾结果，再确认发展方向。" in report
        for forbidden in (
            "must-never-leak",
            "source_profile_text",
            "metadata",
            "task_id",
            "report_sha256",
            "traceback",
            "内部系统消息不应导出",
        ):
            assert forbidden not in report


def test_readable_archive_rejects_empty_selection_result(tmp_path: Path):
    with pytest.raises(NoExportRecords):
        ReadableUserRecordArchiveWriter().write(
            tmp_path / "empty.zip",
            _EmptySnapshot(),
            requested_by="admin@bosch.com",
            selection=ExportSelection(),
        )


def test_export_selection_uses_inclusive_beijing_calendar_dates():
    selection = ExportSelection.from_dates(
        user_emails=["One@bosch.com", "one@bosch.com", "two@bosch.com"],
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 22),
    )

    assert selection.user_emails == ("one@bosch.com", "two@bosch.com")
    assert selection.start_at.isoformat() == "2026-06-30T16:00:00+00:00"
    assert selection.end_at.isoformat() == "2026-07-22T16:00:00+00:00"
    assert "2026-07-01 至 2026-07-22" in selection.display_period()


def test_postgres_snapshot_filter_is_parameterized():
    selection = ExportSelection.from_dates(
        user_emails=["one@bosch.com", "two@bosch.com"],
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 22),
    )
    snapshot = PostgresUserRecordSnapshot(object(), selection)

    where_sql, params = snapshot._where_clause(
        session_alias="workflow_session",
        account_alias="account",
    )

    assert "lower(account.email::text) = ANY(%s::text[])" in where_sql
    assert "workflow_session.created_at >= %s" in where_sql
    assert "workflow_session.created_at < %s" in where_sql
    assert params[0] == ["one@bosch.com", "two@bosch.com"]
    assert params[1] == selection.start_at
    assert params[2] == selection.end_at


def test_postgres_snapshot_filters_exact_session_ids_without_sql_interpolation():
    selection = ExportSelection.from_dates(
        user_emails=["one@bosch.com"],
        session_ids=["session-2", "session-1", "session-2"],
    )
    snapshot = PostgresUserRecordSnapshot(object(), selection)

    where_sql, params = snapshot._where_clause(
        session_alias="workflow_session",
        account_alias="account",
    )

    assert "workflow_session.session_id = ANY(%s::text[])" in where_sql
    assert "session-1" not in where_sql
    assert selection.session_ids == ("session-2", "session-1")
    assert params == (['one@bosch.com'], ['session-2', 'session-1'])


class _ConversationCatalogResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _ConversationCatalogConnection:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []

    def execute(self, query: str, _params=None):
        self.queries.append(query)
        return _ConversationCatalogResult(self.rows if "FROM sessions" in query else [])


class _ConversationCatalogRepository:
    def __init__(self, rows):
        self.connection_instance = _ConversationCatalogConnection(rows)

    def connection(self):
        class _Context:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                return self.connection

            def __exit__(self, *_args):
                return False

        return _Context(self.connection_instance)


def test_conversation_catalog_keeps_undated_and_empty_sessions_distinct():
    rows = [
        {
            "session_id": "dated",
            "user_email": "USER.ONE@bosch.com",
            "user_display_name": "王经理",
            "session_created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "stage": "rehearsal",
            "employee_profile_json": {"name": "张三"},
            "intent_json": {"intent_id": "development"},
            "conversation_json": [
                {
                    "speaker": "manager",
                    "text": "我们先回顾目标。",
                    "created_at": "2026-08-01T08:00:00Z",
                },
                {
                    "speaker": "employee",
                    "text": "好的，我们可以开始。",
                    "created_at": "2026-08-01T08:01:00Z",
                },
            ],
        },
        {
            "session_id": "undated",
            "user_email": "user.one@bosch.com",
            "user_display_name": "王经理",
            "session_created_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
            "employee_profile_json": {"employee_alias": "李四"},
            "intent_json": {},
            "conversation_json": [{"speaker": "manager", "text": "历史原话"}],
        },
        {
            "session_id": "empty",
            "user_email": "user.two@bosch.com",
            "user_display_name": None,
            "session_created_at": datetime(2026, 8, 3, tzinfo=timezone.utc),
            "employee_profile_json": {},
            "intent_json": {},
            "conversation_json": [],
        },
    ]
    service = object.__new__(UserRecordExportService)
    service.repository = _ConversationCatalogRepository(rows)

    items = service.list_conversations()

    assert [item["session_id"] for item in items] == ["empty", "undated", "dated"]
    by_session = {item["session_id"]: item for item in items}
    assert by_session["dated"]["user_email"] == "user.one@bosch.com"
    assert by_session["dated"]["employee_name"] == "张三"
    assert by_session["dated"]["conversation_started_at"].isoformat() == (
        "2026-08-01T08:00:00+00:00"
    )
    assert by_session["dated"]["has_conversation"] is True
    assert by_session["dated"]["has_rehearsal"] is True
    assert by_session["undated"]["has_conversation"] is True
    assert by_session["undated"]["has_rehearsal"] is False
    assert by_session["undated"]["conversation_started_at"] is None
    assert by_session["empty"]["has_conversation"] is False
    assert by_session["empty"]["has_rehearsal"] is False
    assert by_session["empty"]["turn_count"] == 0
    assert "SET TRANSACTION READ ONLY" in service.repository.connection_instance.queries


def _admin_detail_row() -> dict:
    row = copy.deepcopy(_ReadableSnapshot().sessions[0])
    state = row["state_json"]
    state.update(
        {
            "locale": "zh-CN",
            "stage": "report_ready",
            "rehearsal_ended_at": "2026-07-22T02:00:00Z",
            "emotion_state": {"current_vad": {"valence": 0.8}},
            "psychological_pattern_state": {"patterns": ["must-never-leak"]},
        }
    )
    state["intent"]["performance_items"] = [
        {
            "goal": "结果复盘",
            "current_performance": "核心目标按期完成。",
            "generation_reason": "internal-generation-reason",
        }
    ]
    state["conversation"][0]["metadata"].update(
        {
            "rehearsal_timing": {"private": True},
            "rehearsal_request_id": "private-request-id",
        }
    )
    state["conversation"][1]["metadata"] = {
        "emotion_snapshot": {"valence": 0.4},
    }
    state["motivation"].update(
        {
            "primary_score": 95,
            "last_change_reason": "private-motive-reason",
        }
    )
    row.update(
        {
            "user_id": "user-1-internal-id",
            "user_email": "USER.ONE@bosch.com",
            "user_display_name": "王经理",
            "session_updated_at": datetime(2026, 7, 22, 3, tzinfo=timezone.utc),
        }
    )
    return row


def test_admin_session_detail_projects_only_user_visible_fields():
    detail = UserRecordExportService._session_detail_item(_admin_detail_row())
    payload = detail.model_dump(mode="json")
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert payload["read_only"] is True
    assert payload["session_id"] == "session-1"
    assert payload["user_email"] == "user.one@bosch.com"
    assert "user_id" not in payload
    assert payload["employee_profile"]["employee_alias"] == "张三"
    assert payload["supplemental_info"] == "本季度需要讨论发展路径。"
    assert payload["intent"] == {
        "intent_id": "development",
        "name": "发展型反馈",
        "performance_context": "已有表现：按期完成重点目标。",
        "performance_items": [
            {
                "goal": "结果复盘",
                "current_performance": "核心目标按期完成。",
            }
        ],
    }
    assert payload["motivation"] == {
        "primary_motive_id": "security",
        "secondary_motive_ids": ["recognition", "affiliation"],
    }
    assert [turn["speaker"] for turn in payload["conversation"]] == [
        "manager",
        "employee",
    ]
    assert payload["guidance"]["dimension_points"]["start"][0] == {
        "title": "先对齐目标",
        "summary": None,
        "details": ["说明本次沟通关注发展和下一步。"],
    }
    assert payload["coach_tasks"][0]["basis"] == [
        "开场直接说明了沟通目的。"
    ]
    assert payload["coach_tasks"][0]["better_phrases"][0]["suggestion"] == (
        "先回顾结果，再确认发展方向。"
    )
    for forbidden in (
        "must-never-leak",
        "user-1-internal-id",
        "metadata",
        "api_key",
        "rehearsal_timing",
        "rehearsal_request_id",
        "private-request-id",
        "emotion_state",
        "psychological_pattern_state",
        "primary_score",
        "private-motive-reason",
        "generation_reason",
        "report_sha256",
        "traceback",
        "citations",
    ):
        assert forbidden not in serialized


def test_admin_session_detail_tolerates_legacy_user_visible_shapes():
    row = {
        "session_id": "legacy-session",
        "user_id": "internal-user-id",
        "user_email": "legacy@bosch.com",
        "user_display_name": None,
        "session_created_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "session_updated_at": datetime(2026, 7, 2, tzinfo=timezone.utc),
        "state_json": {
            "profile": {
                "name": "旧员工",
                "job_title": "工程师",
                "job_level": "G8",
                "facts": {"fact": "完成关键项目", "source": "旧项目记录"},
                "unknown_internal": "hidden",
            },
            "personality": {"openness": 0.7},
            "intent": {
                "id": "development",
                "label": "发展",
                "performance_items": [
                    {"title": "目标", "performance": "表现", "secret": "hidden"}
                ],
            },
            "conversation": [
                {"speaker": "user", "content": "经理旧原话", "secret": "hidden"},
                {"speaker": "assistant", "content": "员工旧回复"},
                {"speaker": "system", "text": "内部提示"},
            ],
        },
        "guidance_report_json": {
            "purpose": ["先对齐", "再讨论"],
            "opening_suggestion": ["开场一", "开场二"],
            "dimension_points": [
                {"title": "旧分组", "descriptions": ["旧建议"]}
            ],
        },
        "coach_report_json": None,
        "checkpoint_results": [
            {
                "task_id": "start",
                "task_name": "开场",
                "status": "completed",
                "summary": "已完成",
                "dimension_scores": {"basis": "旧评分依据"},
                "better_phrases": [
                    {"suggested": "旧建议表达", "reason": "更清楚"}
                ],
            }
        ],
    }

    payload = UserRecordExportService._session_detail_item(row).model_dump(
        mode="json"
    )

    assert payload["employee_profile"]["employee_alias"] == "旧员工"
    assert payload["employee_profile"]["role"] == "工程师"
    assert payload["employee_profile"]["level"] == "G8"
    assert payload["employee_profile"]["facts"][0]["description"] == "完成关键项目"
    assert payload["employee_profile"]["facts"][0]["evidence_source"] == "旧项目记录"
    assert payload["personality"]["openness"] == 70
    assert payload["personality"]["conscientiousness"] is None
    assert payload["intent"]["performance_items"][0] == {
        "goal": "目标",
        "current_performance": "表现",
    }
    assert [turn["speaker"] for turn in payload["conversation"]] == [
        "manager",
        "employee",
    ]
    assert payload["guidance"]["purpose"] == "先对齐\n再讨论"
    assert payload["guidance"]["dimension_points"]["start"][0]["details"] == [
        "旧建议"
    ]
    assert payload["coach_tasks"][0]["status"] == "success"
    assert payload["coach_tasks"][0]["basis"] == ["旧评分依据"]
    assert payload["coach_tasks"][0]["better_phrases"][0]["suggestion"] == (
        "旧建议表达"
    )
    assert "hidden" not in json.dumps(payload, ensure_ascii=False)


class _SessionDetailFetchResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _SessionDetailConnection:
    def __init__(self, row):
        self.row = row
        self.queries: list[tuple[str, object]] = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "FROM sessions AS workflow_session" in query:
            return _SessionDetailFetchResult(self.row)
        return _SessionDetailFetchResult(None)


class _SessionDetailContext:
    def __init__(self, connection=None, *, fail=False):
        self.connection = connection
        self.fail = fail

    def __enter__(self):
        if self.fail:
            raise RuntimeError("audit unavailable")
        return self.connection

    def __exit__(self, *_args):
        return False


class _SessionDetailRepository:
    def __init__(self, row, *, fail_audit=False):
        self.connection_instance = _SessionDetailConnection(row)
        self.fail_audit = fail_audit
        self.connection_calls = 0

    def connection(self):
        self.connection_calls += 1
        if self.connection_calls > 1 and self.fail_audit:
            return _SessionDetailContext(fail=True)
        return _SessionDetailContext(self.connection_instance)


def test_admin_session_detail_query_is_exact_parameterized_and_audit_is_best_effort():
    service = object.__new__(UserRecordExportService)
    service.repository = _SessionDetailRepository(
        _admin_detail_row(),
        fail_audit=True,
    )
    requested_session_id = "session-1' OR TRUE --"

    detail = service.get_session_detail(
        requested_session_id,
        audit_context=ExportAuditContext(
            actor_email="admin@bosch.com",
            ip_address="127.0.0.1",
            user_agent="pytest",
        ),
    )

    assert detail.session_id == "session-1"
    query, params = next(
        call
        for call in service.repository.connection_instance.queries
        if "FROM sessions AS workflow_session" in call[0]
    )
    assert "WHERE workflow_session.session_id = %s" in query
    assert requested_session_id not in query
    assert params == (requested_session_id,)
    assert service.repository.connection_calls == 2


def test_admin_session_detail_records_actor_target_and_request_audit_context():
    service = object.__new__(UserRecordExportService)
    service.repository = _SessionDetailRepository(_admin_detail_row())

    service.get_session_detail(
        "session-1",
        audit_context=ExportAuditContext(
            actor_email="ADMIN@BOSCH.COM",
            ip_address="192.0.2.7",
            user_agent="pytest-admin-view",
        ),
    )

    audit_query, audit_params = next(
        call
        for call in service.repository.connection_instance.queries
        if "INSERT INTO auth_audit_log" in call[0]
    )
    assert "'admin_view_user_session'" in audit_query
    assert audit_params[0] == "admin@bosch.com"
    assert audit_params[1] is True
    assert json.loads(audit_params[2]) == {
        "actor_email": "admin@bosch.com",
        "session_id": "session-1",
        "target_user_email": "user.one@bosch.com",
        "target_user_id": "user-1-internal-id",
    }
    assert audit_params[3] == "192.0.2.7"
    assert audit_params[4] == "pytest-admin-view"
    assert isinstance(audit_params[5], datetime)

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, Protocol, TextIO
from zoneinfo import ZoneInfo

from backend.config.settings import get_settings
from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.locale import normalize_session_locale
from backend.schemas.user_record_export import (
    AdminUserSessionCareerAdvice,
    AdminUserSessionCoachBetterPhrase,
    AdminUserSessionCoachTask,
    AdminUserSessionConversationTurn,
    AdminUserSessionDetail,
    AdminUserSessionEmployeeProfile,
    AdminUserSessionGuidance,
    AdminUserSessionGuidanceDimensions,
    AdminUserSessionGuidancePoint,
    AdminUserSessionIntent,
    AdminUserSessionIntentPerformanceItem,
    AdminUserSessionMotivation,
    AdminUserSessionPersonality,
    AdminUserSessionProfileFact,
    AdminUserSessionSensitiveConstraint,
)
from backend.services.user_content_export_service import (
    ArchiveSummary,
    ExportArtifact,
    ExportAuditContext,
    ExportInProgress,
    InvalidExportAdministrator,
    UserContentExportError,
)


REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


class InvalidExportSelection(UserContentExportError):
    pass


class NoExportRecords(UserContentExportError):
    pass


@dataclass(frozen=True, slots=True)
class ExportSelection:
    """Filter complete sessions by owner, identifier, and creation date."""

    user_emails: tuple[str, ...] = ()
    start_at: datetime | None = None
    end_at: datetime | None = None
    session_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = tuple(
            dict.fromkeys(
                email.strip().lower()
                for email in self.user_emails
                if isinstance(email, str) and email.strip()
            )
        )
        if len(normalized) > 1000:
            raise InvalidExportSelection("一次最多选择 1000 个用户。")
        if any("@" not in email for email in normalized):
            raise InvalidExportSelection("用户邮箱格式无效。")
        object.__setattr__(self, "user_emails", normalized)
        normalized_session_ids = tuple(
            dict.fromkeys(
                session_id.strip()
                for session_id in self.session_ids
                if isinstance(session_id, str) and session_id.strip()
            )
        )
        if len(normalized_session_ids) > 5000:
            raise InvalidExportSelection("一次最多选择 5000 次会话。")
        if any(
            len(session_id) > 256
            or any(ord(character) < 32 for character in session_id)
            for session_id in normalized_session_ids
        ):
            raise InvalidExportSelection("会话 ID 格式无效。")
        object.__setattr__(self, "session_ids", normalized_session_ids)
        for boundary in (self.start_at, self.end_at):
            if boundary is not None and boundary.tzinfo is None:
                raise InvalidExportSelection("导出时间必须包含时区。")
        if self.start_at and self.end_at and self.start_at >= self.end_at:
            raise InvalidExportSelection("结束日期必须晚于或等于开始日期。")

    @classmethod
    def from_dates(
        cls,
        *,
        user_emails: Iterable[str] = (),
        session_ids: Iterable[str] = (),
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ExportSelection:
        if start_date and end_date and start_date > end_date:
            raise InvalidExportSelection("结束日期不能早于开始日期。")
        start_at = (
            datetime.combine(start_date, time.min, tzinfo=REPORT_TIMEZONE)
            .astimezone(timezone.utc)
            if start_date
            else None
        )
        end_at = (
            datetime.combine(
                end_date + timedelta(days=1),
                time.min,
                tzinfo=REPORT_TIMEZONE,
            ).astimezone(timezone.utc)
            if end_date
            else None
        )
        return cls(
            user_emails=tuple(user_emails),
            start_at=start_at,
            end_at=end_at,
            session_ids=tuple(session_ids),
        )

    def audit_payload(self) -> dict[str, Any]:
        return {
            "user_emails": list(self.user_emails),
            "session_ids": list(self.session_ids),
            "session_count": len(self.session_ids),
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at_exclusive": self.end_at.isoformat() if self.end_at else None,
            "date_basis": "session_created_at",
        }

    def display_period(self) -> str:
        start = (
            self.start_at.astimezone(REPORT_TIMEZONE).date().isoformat()
            if self.start_at
            else "最早记录"
        )
        end = (
            (self.end_at.astimezone(REPORT_TIMEZONE).date() - timedelta(days=1)).isoformat()
            if self.end_at
            else "当前"
        )
        return f"{start} 至 {end}（按会话创建日期，北京时间）"


class UserRecordSnapshot(Protocol):
    def iter_users(self) -> Iterator[dict[str, Any]]: ...

    def iter_session_records(self) -> Iterator[dict[str, Any]]: ...


class PostgresUserRecordSnapshot:
    def __init__(
        self,
        connection: Any,
        selection: ExportSelection,
        *,
        batch_size: int = 200,
    ) -> None:
        self.connection = connection
        self.selection = selection
        self.batch_size = max(1, int(batch_size))
        self._cursor_index = 0

    def _iter_query(
        self,
        query: str,
        params: Sequence[Any] = (),
    ) -> Iterator[dict[str, Any]]:
        self._cursor_index += 1
        cursor = self.connection.cursor(name=f"user_record_export_{self._cursor_index}")
        try:
            cursor.execute(query, tuple(params))
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                for row in rows:
                    yield dict(row)
        finally:
            cursor.close()

    def _where_clause(
        self,
        *,
        session_alias: str,
        account_alias: str,
    ) -> tuple[str, tuple[Any, ...]]:
        conditions: list[str] = []
        params: list[Any] = []
        if self.selection.user_emails:
            conditions.append(f"lower({account_alias}.email::text) = ANY(%s::text[])")
            params.append(list(self.selection.user_emails))
        if self.selection.session_ids:
            conditions.append(f"{session_alias}.session_id = ANY(%s::text[])")
            params.append(list(self.selection.session_ids))
        if self.selection.start_at:
            conditions.append(f"{session_alias}.created_at >= %s")
            params.append(self.selection.start_at)
        if self.selection.end_at:
            conditions.append(f"{session_alias}.created_at < %s")
            params.append(self.selection.end_at)
        where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
        return where_sql, tuple(params)

    def iter_users(self) -> Iterator[dict[str, Any]]:
        where_sql, params = self._where_clause(
            session_alias="workflow_session",
            account_alias="account",
        )
        return self._iter_query(
            f"""
            SELECT
                account.id::text AS user_id,
                account.email::text AS email,
                account.display_name,
                COUNT(*) AS session_count,
                MIN(workflow_session.created_at) AS first_session_at,
                MAX(workflow_session.created_at) AS last_session_at
            FROM app_users AS account
            INNER JOIN sessions AS workflow_session
                ON workflow_session.owner_user_id = account.id
            {where_sql}
            GROUP BY account.id, account.email, account.display_name
            ORDER BY lower(account.email::text), account.id
            """,
            params,
        )

    def iter_session_records(self) -> Iterator[dict[str, Any]]:
        where_sql, params = self._where_clause(
            session_alias="workflow_session",
            account_alias="account",
        )
        return self._iter_query(
            f"""
            SELECT
                account.id::text AS user_id,
                account.email::text AS email,
                account.display_name,
                workflow_session.session_id,
                workflow_session.state_json,
                workflow_session.created_at AS session_created_at,
                latest_guidance.report_json AS guidance_report_json,
                latest_coach.report_json AS coach_report_json,
                COALESCE(checkpoint_data.results, '[]'::jsonb) AS checkpoint_results
            FROM sessions AS workflow_session
            INNER JOIN app_users AS account
                ON account.id = workflow_session.owner_user_id
            LEFT JOIN LATERAL (
                SELECT history.report_json
                FROM guidance_report_versions AS history
                WHERE history.session_id = workflow_session.session_id
                ORDER BY history.history_id DESC
                LIMIT 1
            ) AS latest_guidance ON TRUE
            LEFT JOIN LATERAL (
                SELECT history.report_json
                FROM coach_report_versions AS history
                WHERE history.session_id = workflow_session.session_id
                ORDER BY history.history_id DESC
                LIMIT 1
            ) AS latest_coach ON TRUE
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                    checkpoint.result_json ORDER BY checkpoint.task_id
                ) AS results
                FROM coach_task_results AS checkpoint
                WHERE checkpoint.session_id = workflow_session.session_id
            ) AS checkpoint_data ON TRUE
            {where_sql}
            ORDER BY
                lower(account.email::text), account.id,
                workflow_session.created_at, workflow_session.session_id
            """,
            params,
        )


class ReadableUserRecordArchiveWriter:
    INTENT_LABELS = {
        "development": "发展型反馈",
        "improvement": "改进型反馈",
        "development_improvement": "发展与改进混合反馈",
        "exit": "退出型沟通",
        "improvement_exit": "改进与退出预警",
    }
    RUN_MODE_LABELS = {
        "guidance_only": "仅生成谈前指导",
        "guidance_then_rehearsal": "谈前指导后进入多轮预演",
        "rehearsal_report": "直接进行多轮预演与复盘",
    }
    PERSONALITY_DIMENSIONS = (
        (
            "openness",
            "接受新事物程度",
            "更习惯现有方式",
            "接受适度变化",
            "更愿意尝试新方案",
        ),
        (
            "conscientiousness",
            "做事靠谱程度",
            "更灵活随性",
            "基本按计划推进",
            "很重视计划和兑现",
        ),
        (
            "extraversion",
            "表达主动程度",
            "更少主动表达",
            "需要时会表达",
            "更主动表达想法",
        ),
        (
            "agreeableness",
            "好沟通程度",
            "更坚持自己看法",
            "能协商但有保留",
            "更愿意配合沟通",
        ),
        (
            "neuroticism",
            "情绪敏感程度",
            "不太容易被影响",
            "压力反应中等",
            "更容易感到压力",
        ),
    )
    MOTIVE_LABELS = {
        "commerce": "薪酬",
        "power": "权力",
        "recognition": "认可",
        "affiliation": "融洽团队",
        "security": "稳定",
        "hedonism": "舒适工作环境",
        "career": "职业发展",
    }
    SENSITIVE_FLAG_LABELS = {
        "yes": "是",
        "no": "否",
        "unknown": "未确认",
    }
    DIMENSION_LABELS = {
        "start": "开场与沟通目标",
        "emotion": "情绪识别与回应",
        "requirement": "产出与标准",
        "plan": "行动计划与跟进",
    }
    PROFILE_FIELDS = (
        (("employee_alias", "name"), "员工"),
        (("employee_id",), "员工编号"),
        (("role", "job_title"), "岗位"),
        (("department",), "部门"),
        (("level", "job_level"), "职级"),
        (("reporting_line",), "汇报关系"),
        (("performance_rating",), "绩效结果"),
        (("tcl",), "TCL"),
        (("review_cycle",), "评估周期"),
        (("conversation_topic",), "沟通主题"),
        (("employee_status_summary",), "员工情况"),
    )
    STATUS_LABELS = {
        "success": "已完成",
        "insufficient_information": "信息不足",
        "failed": "生成失败",
    }

    def write(
        self,
        output_path: Path,
        snapshot: UserRecordSnapshot,
        *,
        requested_by: str,
        selection: ExportSelection,
    ) -> ArchiveSummary:
        generated_at = datetime.now(timezone.utc)
        users = list(snapshot.iter_users())
        if not users:
            raise NoExportRecords("所选用户和日期范围内没有可导出的沟通记录。")
        counts = {
            "users": 0,
            "sessions": 0,
            "guidance_reports": 0,
            "rehearsal_turns": 0,
            "coach_reports": 0,
            "coach_checkpoints": 0,
        }
        with zipfile.ZipFile(
            output_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            archive.writestr("README.txt", self._readme().encode("utf-8-sig"))
            session_groups = iter(
                groupby(
                    snapshot.iter_session_records(),
                    key=lambda row: str(row.get("user_id") or ""),
                )
            )
            current_group = next(session_groups, None)
            for index, user in enumerate(users, start=1):
                user_id = str(user.get("user_id") or "")
                if current_group is None or current_group[0] != user_id:
                    raise UserContentExportError("用户与会话快照不一致，已停止导出。")
                filename = self._user_filename(index, str(user.get("email") or "user"))
                with archive.open(filename, "w") as binary_file:
                    text_file = io.TextIOWrapper(
                        binary_file,
                        encoding="utf-8-sig",
                        newline="\n",
                    )
                    try:
                        self._write_user_report(
                            text_file,
                            user,
                            current_group[1],
                            counts,
                        )
                        text_file.flush()
                    finally:
                        text_file.detach()
                counts["users"] += 1
                current_group = next(session_groups, None)
            if current_group is not None:
                raise UserContentExportError("会话快照包含未匹配用户，已停止导出。")
            archive.writestr(
                "summary.txt",
                self._summary(generated_at, requested_by, selection, counts).encode(
                    "utf-8-sig"
                ),
            )
        size_bytes = output_path.stat().st_size
        return ArchiveSummary(
            generated_at=generated_at,
            counts=counts,
            sha256=self._file_sha256(output_path),
            size_bytes=size_bytes,
        )

    def _write_user_report(
        self,
        output: TextIO,
        user: Mapping[str, Any],
        sessions: Iterable[Mapping[str, Any]],
        counts: dict[str, int],
    ) -> None:
        self._line(output, "HR Agent 用户沟通记录")
        self._line(output, "=" * 64)
        self._line(output, f"用户名称：{self._text(user.get('display_name')) or '未设置'}")
        self._line(output, f"用户邮箱：{self._text(user.get('email')) or '未知'}")
        self._line(output, f"记录数量：{int(user.get('session_count') or 0)}")
        self._line(
            output,
            f"记录时间：{self._display_time(user.get('first_session_at'))} 至 "
            f"{self._display_time(user.get('last_session_at'))}",
        )
        exported = 0
        for position, session in enumerate(sessions, start=1):
            exported += 1
            counts["sessions"] += 1
            self._line(output)
            self._line(output, "-" * 64)
            self._write_session(output, position, session, counts)
        if exported != int(user.get("session_count") or 0):
            raise UserContentExportError("用户会话数量在导出期间发生变化。")

    def _write_session(
        self,
        output: TextIO,
        position: int,
        row: Mapping[str, Any],
        counts: dict[str, int],
    ) -> None:
        state = self._mapping(row.get("state_json"))
        profile = self._mapping(state.get("employee_profile") or state.get("profile"))
        self._line(output, f"记录 {position}")
        self._line(output, f"记录编号：{self._text(row.get('session_id')) or '未知'}")
        self._line(output, f"创建时间：{self._display_time(row.get('session_created_at'))}")

        self._line(output)
        self._line(output, "【用户选择与输入】")
        self._write_setup_inputs(output, state)

        self._line(output)
        self._line(output, "【员工与沟通背景】")
        background_count = 0
        for keys, label in self.PROFILE_FIELDS:
            value = self._first(profile, keys)
            if value:
                self._line(output, f"{label}：{value}")
                background_count += 1
        for field, label in (
            ("key_goals", "关键目标"),
            ("past_ratings", "历史绩效结果"),
            ("historical_feedback", "历史反馈"),
            ("management_actions", "已采取管理行动"),
        ):
            values = self._items(profile.get(field))
            if values:
                self._line(output, f"{label}：")
                self._bullets(output, values)
                background_count += 1

        facts = self._profile_fact_lines(profile.get("facts"))
        if facts:
            self._line(output, "关键事实：")
            self._bullets(output, facts)
            background_count += 1

        for field, label in (
            ("previous_improvement_discussion", "是否进行过改进沟通"),
            ("has_pip", "是否已有绩效改进计划"),
            (
                "involves_promotion_salary_transfer",
                "是否涉及晋升、薪酬或岗位调整",
            ),
        ):
            value = self._text(profile.get(field)).lower()
            if value and value != "unknown":
                self._line(
                    output,
                    f"{label}：{self.SENSITIVE_FLAG_LABELS.get(value, value)}",
                )
                background_count += 1

        constraints = self._mapping(profile.get("sensitive_constraints"))
        if constraints:
            self._line(output, "敏感约束：")
            for key, raw_constraint in constraints.items():
                constraint = self._mapping(raw_constraint)
                status_value = self._text(
                    constraint.get("status") if constraint else raw_constraint
                ).lower()
                status = self.SENSITIVE_FLAG_LABELS.get(
                    status_value,
                    status_value or "未确认",
                )
                impact = self._text(
                    constraint.get("business_impact_summary")
                    if constraint
                    else None
                )
                rendered = f"{self._display_field_name(key)}：{status}"
                if impact:
                    rendered += f"；业务影响：{impact}"
                self._bullets(output, [rendered])
            background_count += 1

        source_profile_text = self._text(profile.get("source_profile_text"))
        if source_profile_text:
            self._line(output, "员工背景原始输入：")
            self._indented_text(output, source_profile_text)
            background_count += 1

        profile_supplemental = self._text(profile.get("supplemental_info"))
        session_supplemental = self._text(state.get("supplemental_info"))
        if profile_supplemental:
            self._line(output, "员工资料补充信息：")
            self._indented_text(output, profile_supplemental)
            background_count += 1
        if session_supplemental and session_supplemental != profile_supplemental:
            self._line(output, "额外提供的信息：")
            self._indented_text(output, session_supplemental)
            background_count += 1
        if not background_count:
            self._line(output, "未保存有效的员工背景信息。")

        self._line(output)
        self._line(output, "【谈前指导】")
        guidance = self._mapping(row.get("guidance_report_json"))
        if guidance:
            counts["guidance_reports"] += 1
            self._write_guidance(output, guidance)
        else:
            self._line(output, "本次记录未生成谈前指导。")

        self._line(output)
        self._line(output, "【多轮预演】")
        turns = self._write_conversation(output, self._sequence(state.get("conversation")))
        counts["rehearsal_turns"] += turns
        if not turns:
            self._line(output, "本次记录没有经理与员工的有效预演对话。")

        self._line(output)
        self._line(output, "【复盘报告】")
        coach = self._mapping(row.get("coach_report_json"))
        if coach:
            counts["coach_reports"] += 1
            self._write_coach_tasks(output, self._sequence(coach.get("task_results")))
        else:
            checkpoints = self._sequence(row.get("checkpoint_results"))
            if checkpoints:
                counts["coach_checkpoints"] += len(checkpoints)
                self._line(output, "最终复盘尚未完成，以下为已完成的维度：")
                self._write_coach_tasks(output, checkpoints)
            else:
                self._line(output, "本次记录未生成复盘报告。")

    def _write_setup_inputs(
        self,
        output: TextIO,
        state: Mapping[str, Any],
    ) -> None:
        run_mode = self._text(state.get("run_mode"))
        self._line(
            output,
            f"会话流程：{self.RUN_MODE_LABELS.get(run_mode, run_mode or '未设置')}",
        )

        self._line(output, f"沟通意图：{self._intent_label(state.get('intent'))}")

        performance_context = self._text(
            self._mapping(state.get("intent")).get("performance_context")
        )
        if performance_context:
            self._line(output, "已确认当前表现：")
            self._indented_text(output, performance_context)

        personality = self._mapping(state.get("personality"))
        if personality:
            self._line(output, "大五人格设置：")
            for key, label, low, middle, high in self.PERSONALITY_DIMENSIONS:
                value = self._personality_value(personality.get(key))
                if value is None:
                    continue
                description = low if value < 35 else high if value > 65 else middle
                self._line(
                    output,
                    f"  - {label}：{self._format_number(value)}/100（{description}）",
                )
        else:
            self._line(output, "大五人格设置：未保存")

        motivation = self._mapping(state.get("motivation"))
        if motivation:
            primary = self._motive_label(motivation.get("primary_motive_id"))
            secondary = [
                self._motive_label(value)
                for value in self._sequence(motivation.get("secondary_motive_ids"))
                if self._text(value)
            ]
            self._line(output, f"员工主要诉求：{primary or '未设置'}")
            self._line(
                output,
                f"员工辅助诉求：{'、'.join(secondary) if secondary else '未设置'}",
            )
        else:
            self._line(output, "员工动机选择：未保存")

        rehearsal_context = self._mapping(state.get("rehearsal_context"))
        runtime_notes = self._items(rehearsal_context.get("runtime_notes"))
        if runtime_notes:
            self._line(output, "预演期间补充要求：")
            self._bullets(output, runtime_notes)

    @classmethod
    def _profile_fact_lines(cls, value: Any) -> list[str]:
        facts = cls._sequence(value)
        if not facts:
            return cls._items(value)
        result: list[str] = []
        for raw_fact in facts:
            fact = cls._mapping(raw_fact)
            if not fact:
                result.extend(cls._items(raw_fact))
                continue
            description = cls._text(
                fact.get("description") or fact.get("fact") or fact.get("text")
            )
            impact = cls._text(fact.get("impact"))
            evidence = cls._text(
                fact.get("evidence_source") or fact.get("source")
            )
            parts = [description] if description else []
            if impact:
                parts.append(f"影响：{impact}")
            if evidence:
                parts.append(f"依据：{evidence}")
            if parts:
                result.append("；".join(parts))
        return result

    @classmethod
    def _personality_value(cls, value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if 0.0 < number < 1.0:
            number *= 100.0
        return max(0.0, min(100.0, number))

    @staticmethod
    def _format_number(value: float) -> str:
        return str(int(value)) if value.is_integer() else f"{value:.1f}"

    @classmethod
    def _motive_label(cls, value: Any) -> str:
        motive_id = cls._text(value)
        return cls.MOTIVE_LABELS.get(motive_id, cls._display_field_name(motive_id))

    @classmethod
    def _display_field_name(cls, value: Any) -> str:
        text_value = cls._text(value)
        return text_value.replace("_", " ").strip() or "未命名项目"

    @classmethod
    def _indented_text(
        cls,
        output: TextIO,
        value: Any,
        *,
        indent: str = "  ",
    ) -> None:
        text_value = cls._text(value)
        if not text_value:
            return
        for line in text_value.splitlines() or [text_value]:
            cls._line(output, f"{indent}{line}")

    def _write_guidance(self, output: TextIO, report: Mapping[str, Any]) -> None:
        dimensions = self._mapping(report.get("dimension_points"))
        wrote_dimension = False
        for dimension_id, label in self.DIMENSION_LABELS.items():
            value = dimensions.get(dimension_id)
            groups = self._sequence(value)
            if not groups and isinstance(value, Mapping):
                groups = [value]
            if not groups:
                continue
            wrote_dimension = True
            self._line(output, f"{label}：")
            for group_value in groups:
                group = self._mapping(group_value)
                if group:
                    title = self._text(group.get("title"))
                    details = self._items(
                        group.get("details") or group.get("descriptions")
                    )
                    if title:
                        self._line(output, f"  {title}")
                    self._bullets(output, details, indent="    ")
                else:
                    self._bullets(output, self._items(group_value))
        if wrote_dimension:
            return
        for field, label in (
            ("purpose", "沟通目标"),
            ("opening_suggestion", "开场建议"),
            ("risk_preview", "重点风险"),
            ("response_strategies", "回应策略"),
            ("safer_phrases", "建议表达"),
        ):
            values = self._items(report.get(field))
            if values:
                self._line(output, f"{label}：")
                self._bullets(output, values)

    def _write_conversation(self, output: TextIO, values: Sequence[Any]) -> int:
        speaker_labels = {
            "manager": "经理",
            "user": "经理",
            "employee": "员工",
            "assistant": "员工",
        }
        count = 0
        for fallback_index, turn_value in enumerate(values, start=1):
            turn = self._mapping(turn_value)
            label = speaker_labels.get(str(turn.get("speaker") or "").lower())
            text_value = self._text(turn.get("text") or turn.get("content"))
            if not label or not text_value:
                continue
            turn_index = turn.get("turn_index") or fallback_index
            rendered = text_value.replace("\n", "\n    ")
            self._line(output, f"第 {turn_index} 轮 {label}：{rendered}")
            count += 1
        return count

    def _write_coach_tasks(self, output: TextIO, values: Sequence[Any]) -> None:
        tasks = [self._mapping(value) for value in values]
        tasks = [task for task in tasks if task]
        if not tasks:
            self._line(output, "复盘报告没有可展示的维度结果。")
            return
        for task in tasks:
            name = self._text(task.get("task_name") or task.get("task_id")) or "未命名维度"
            status = self.STATUS_LABELS.get(
                str(task.get("status") or "").lower(), "已完成"
            )
            score = task.get("score")
            score_text = f"，评分 {score}/5" if score is not None else ""
            self._line(output, f"{name}（{status}{score_text}）")
            summary = self._text(task.get("summary"))
            if summary:
                self._line(output, f"  结论：{summary}")
            bases: list[str] = []
            for score_value in self._sequence(task.get("dimension_scores")):
                dimension_score = self._mapping(score_value)
                basis = self._text(
                    dimension_score.get("basis")
                    or dimension_score.get("comment")
                    or dimension_score.get("reason")
                )
                if basis:
                    bases.append(basis)
            if bases:
                self._line(output, "  评分依据：")
                self._bullets(output, bases, indent="    ")
            for field, label in (
                ("strengths", "做得较好"),
                ("improvement_points", "需要改进"),
                ("risks", "需要关注"),
            ):
                items = self._items(task.get(field))
                if items:
                    self._line(output, f"  {label}：")
                    self._bullets(output, items, indent="    ")
            evidence_items: list[str] = []
            for evidence_value in self._sequence(task.get("evidence")):
                evidence = self._mapping(evidence_value)
                quote = self._text(evidence.get("quote"))
                if quote:
                    turn_index = evidence.get("turn_index")
                    prefix = f"第 {turn_index} 轮：" if turn_index is not None else ""
                    evidence_items.append(f"{prefix}“{quote}”")
            if evidence_items:
                self._line(output, "  对话依据：")
                self._bullets(output, evidence_items, indent="    ")
            phrases = self._sequence(task.get("better_phrases"))
            if phrases:
                self._line(output, "  建议表达：")
                for index, phrase_value in enumerate(phrases, start=1):
                    phrase = self._mapping(phrase_value)
                    original = self._text(
                        phrase.get("original") or phrase.get("original_context")
                    )
                    suggestion = self._text(
                        phrase.get("suggestion")
                        or phrase.get("suggested")
                        or phrase.get("suggested_phrase")
                        or phrase.get("better_phrase")
                    )
                    reason = self._text(
                        phrase.get("reason") or phrase.get("explanation")
                    )
                    if not any((original, suggestion, reason)):
                        continue
                    self._line(output, f"    {index}. 原表达：{original or '未记录'}")
                    if suggestion:
                        self._line(output, f"       建议：{suggestion}")
                    if reason:
                        self._line(output, f"       原因：{reason}")
            self._line(output)

    @classmethod
    def _items(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text_value = cls._text(value)
            return [text_value] if text_value else []
        if isinstance(value, Mapping):
            for key in (
                "text",
                "point",
                "description",
                "detail",
                "summary",
                "fact",
                "explanation",
                "risk",
                "value",
            ):
                text_value = cls._text(value.get(key))
                if text_value:
                    return [text_value]
            return []
        if isinstance(value, (list, tuple, set, frozenset)):
            result: list[str] = []
            for item in value:
                result.extend(cls._items(item))
            return result
        text_value = cls._text(value)
        return [text_value] if text_value else []

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return {}
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        return {}

    @staticmethod
    def _sequence(value: Any) -> list[Any]:
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return []
            return list(parsed) if isinstance(parsed, list) else []
        return []

    @staticmethod
    def _text(value: Any) -> str:
        if value is None or isinstance(value, (Mapping, list, tuple, set, frozenset)):
            return ""
        return (
            str(value)
            .replace("\x00", "")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )

    @classmethod
    def _first(cls, value: Mapping[str, Any], keys: Sequence[str]) -> str:
        for key in keys:
            text_value = cls._text(value.get(key))
            if text_value:
                return text_value
        return ""

    @classmethod
    def _intent_label(cls, value: Any) -> str:
        intent = cls._mapping(value)
        intent_id = cls._text(intent.get("intent_id") or intent.get("id") or value)
        name = cls._text(intent.get("name") or intent.get("label"))
        return name or cls.INTENT_LABELS.get(intent_id, intent_id or "未设置")

    @staticmethod
    def _line(output: TextIO, value: str = "") -> None:
        output.write(value)
        output.write("\n")

    @classmethod
    def _bullets(
        cls,
        output: TextIO,
        values: Iterable[str],
        *,
        indent: str = "  ",
    ) -> None:
        for value in values:
            cleaned = cls._text(value)
            if cleaned:
                continuation = "\n" + indent + "  "
                cls._line(output, f"{indent}- {cleaned.replace(chr(10), continuation)}")

    @staticmethod
    def _display_time(value: Any) -> str:
        parsed: datetime | None = None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return value.strip()
        if parsed is None:
            return "未知"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(REPORT_TIMEZONE).strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _user_filename(index: int, email: str) -> str:
        local_part = email.split("@", 1)[0]
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", local_part).strip("._-")
        digest = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:8]
        return f"users/{index:04d}_{(safe_name or 'user')[:48]}_{digest}.txt"

    @classmethod
    def _summary(
        cls,
        generated_at: datetime,
        requested_by: str,
        selection: ExportSelection,
        counts: Mapping[str, int],
    ) -> str:
        selected = (
            "、".join(selection.user_emails)
            if selection.user_emails
            else "全部有沟通记录的用户"
        )
        return "\n".join(
            (
                "HR Agent 使用记录导出汇总",
                "=" * 64,
                f"生成时间：{cls._display_time(generated_at)}",
                f"导出管理员：{requested_by}",
                f"用户范围：{selected}",
                f"时间范围：{selection.display_period()}",
                "",
                f"用户数量：{counts.get('users', 0)}",
                f"沟通记录：{counts.get('sessions', 0)}",
                f"谈前指导：{counts.get('guidance_reports', 0)}",
                f"预演对话轮次：{counts.get('rehearsal_turns', 0)}",
                f"完整复盘报告：{counts.get('coach_reports', 0)}",
                f"未完成复盘维度：{counts.get('coach_checkpoints', 0)}",
                "",
                "每位用户的记录位于 users/ 目录。",
                "",
            )
        )

    @staticmethod
    def _readme() -> str:
        return """HR Agent 用户沟通记录导出

此归档仅包含便于阅读的文本报告：
1. 每个用户一份 TXT，按会话创建时间排列。
2. 每次会话保留流程与意图选择、大五人格、员工动机、员工与沟通背景、最新谈前指导、经理/员工预演对话和最新复盘结果。
3. 最终复盘尚未生成时，仅展示已完成的维度结果。

此归档不会包含原始 JSON、CSV、模型参数、内部 metadata、报告哈希、性能耗时、密码、Cookie、API 密钥或异常堆栈。

安全要求：
1. 仅在受控 Bosch 设备和授权范围内使用。
2. 不要通过非受控邮件、即时通信或公共存储传递。
3. 使用完成后按内部数据保留政策删除。
"""

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()


class UserRecordExportService:
    _ADVISORY_LOCK_CLASS = 72810421
    _ADVISORY_LOCK_OBJECT = 20260722

    def __init__(
        self,
        repository: PostgresRepository | None = None,
        archive_writer: ReadableUserRecordArchiveWriter | None = None,
    ) -> None:
        self.repository = repository or PostgresRepository()
        self.archive_writer = archive_writer or ReadableUserRecordArchiveWriter()
        self.settings = get_settings()

    @staticmethod
    def default_filename(at: datetime | None = None) -> str:
        timestamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return f"hr_agent_user_records_{timestamp.strftime('%Y%m%dT%H%M%SZ')}.zip"

    def validate_admin(self, email: str) -> None:
        normalized = email.strip().lower()
        with self.repository.connection() as connection:
            row = connection.execute(
                """
                SELECT email::text AS email, role, is_active
                FROM app_users
                WHERE lower(email::text) = %s
                """,
                (normalized,),
            ).fetchone()
        configured_admin = normalized in self.settings.auth_admin_email_set
        if (
            row is None
            or not bool(PostgresRepository._row_value(row, "is_active"))
            or (
                PostgresRepository._row_value(row, "role") != "admin"
                and not configured_admin
            )
        ):
            raise InvalidExportAdministrator(
                "--requested-by must identify an active administrator"
            )

    def list_conversations(self) -> list[dict[str, Any]]:
        """Return a content-safe catalog for exact conversation selection."""
        with self.repository.connection() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                """
                SELECT
                    workflow_session.session_id,
                    workflow_session.state_json->'conversation' AS conversation_json,
                    workflow_session.state_json->'employee_profile' AS employee_profile_json,
                    workflow_session.state_json->'intent' AS intent_json,
                    workflow_session.state_json->>'stage' AS stage,
                    workflow_session.created_at AS session_created_at,
                    account.email::text AS user_email,
                    account.display_name AS user_display_name
                FROM sessions AS workflow_session
                INNER JOIN app_users AS account
                    ON account.id = workflow_session.owner_user_id
                ORDER BY
                    workflow_session.created_at DESC,
                    workflow_session.session_id DESC
                """
            ).fetchall()
        items = [self._conversation_catalog_item(dict(row)) for row in rows]
        items.sort(
            key=lambda item: (
                item["conversation_started_at"] or item["session_created_at"],
                item["session_id"],
            ),
            reverse=True,
        )
        return items

    def get_session_detail(
        self,
        session_id: str,
        *,
        audit_context: ExportAuditContext,
    ) -> AdminUserSessionDetail:
        """Load one content-safe, read-only admin view of a persisted session."""

        normalized_session_id = str(session_id or "").strip()
        if (
            not normalized_session_id
            or len(normalized_session_id) > 256
            or any(ord(character) < 32 for character in normalized_session_id)
        ):
            self._record_session_view_audit_best_effort(
                audit_context,
                session_id=normalized_session_id,
                target_user_id=None,
                target_user_email=None,
                success=False,
                error_type="InvalidSessionId",
            )
            raise KeyError(f"Session not found: {normalized_session_id}")

        row: Mapping[str, Any] | None = None
        try:
            with self.repository.connection() as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                fetched = connection.execute(
                    """
                    SELECT
                        workflow_session.session_id,
                        workflow_session.owner_user_id::text AS user_id,
                        account.email::text AS user_email,
                        account.display_name AS user_display_name,
                        workflow_session.state_json,
                        workflow_session.created_at AS session_created_at,
                        workflow_session.updated_at AS session_updated_at,
                        latest_guidance.report_json AS guidance_report_json,
                        latest_coach.report_json AS coach_report_json,
                        COALESCE(checkpoint_data.results, '[]'::jsonb)
                            AS checkpoint_results
                    FROM sessions AS workflow_session
                    INNER JOIN app_users AS account
                        ON account.id = workflow_session.owner_user_id
                    LEFT JOIN LATERAL (
                        SELECT history.report_json
                        FROM guidance_report_versions AS history
                        WHERE history.session_id = workflow_session.session_id
                        ORDER BY history.history_id DESC
                        LIMIT 1
                    ) AS latest_guidance ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT history.report_json
                        FROM coach_report_versions AS history
                        WHERE history.session_id = workflow_session.session_id
                        ORDER BY history.history_id DESC
                        LIMIT 1
                    ) AS latest_coach ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT jsonb_agg(
                            checkpoint.result_json ORDER BY checkpoint.task_id
                        ) AS results
                        FROM coach_task_results AS checkpoint
                        WHERE checkpoint.session_id = workflow_session.session_id
                    ) AS checkpoint_data ON TRUE
                    WHERE workflow_session.session_id = %s
                    LIMIT 1
                    """,
                    (normalized_session_id,),
                ).fetchone()
            if fetched is None:
                raise KeyError(f"Session not found: {normalized_session_id}")
            row = dict(fetched)
            detail = self._session_detail_item(row)
        except Exception as exc:
            self._record_session_view_audit_best_effort(
                audit_context,
                session_id=normalized_session_id,
                target_user_id=(
                    self._optional_text(row.get("user_id")) if row is not None else None
                ),
                target_user_email=(
                    self._optional_text(row.get("user_email"))
                    if row is not None
                    else None
                ),
                success=False,
                error_type=type(exc).__name__,
            )
            raise

        self._record_session_view_audit_best_effort(
            audit_context,
            session_id=normalized_session_id,
            target_user_id=self._optional_text(row.get("user_id")),
            target_user_email=detail.user_email,
            success=True,
            error_type=None,
        )
        return detail

    @classmethod
    def _session_detail_item(
        cls,
        row: Mapping[str, Any],
    ) -> AdminUserSessionDetail:
        state = cls._json_mapping(row.get("state_json"))
        created_at = cls._parse_datetime(row.get("session_created_at"))
        updated_at = cls._parse_datetime(row.get("session_updated_at"))
        if created_at is None or updated_at is None:
            raise ValueError("Session database timestamps are required")

        profile_payload = cls._json_mapping(
            state.get("employee_profile") or state.get("profile")
        )
        personality_payload = cls._json_mapping(state.get("personality"))
        locale = normalize_session_locale(cls._optional_text(state.get("locale")))
        rehearsal_context = cls._json_mapping(state.get("rehearsal_context"))

        return AdminUserSessionDetail(
            session_id=cls._optional_text(row.get("session_id")) or "",
            user_email=(cls._optional_text(row.get("user_email")) or "").lower(),
            user_display_name=cls._optional_text(row.get("user_display_name")),
            stage=cls._optional_text(state.get("stage")),
            run_mode=cls._optional_text(state.get("run_mode")),
            locale=locale,
            session_created_at=created_at,
            session_updated_at=updated_at,
            rehearsal_ended_at=cls._parse_datetime(state.get("rehearsal_ended_at")),
            employee_profile=cls._project_admin_profile(profile_payload),
            supplemental_info=cls._optional_text(state.get("supplemental_info")),
            intent=cls._project_admin_intent(state.get("intent")),
            personality=cls._project_admin_personality(personality_payload),
            motivation=cls._project_admin_motivation(state.get("motivation")),
            runtime_notes=cls._text_list(rehearsal_context.get("runtime_notes")),
            conversation=cls._project_admin_conversation(state.get("conversation")),
            guidance=cls._project_admin_guidance(row.get("guidance_report_json")),
            coach_tasks=cls._project_admin_coach_tasks(
                row.get("coach_report_json"),
                row.get("checkpoint_results"),
            ),
        )

    @classmethod
    def _project_admin_profile(
        cls,
        value: Any,
    ) -> AdminUserSessionEmployeeProfile | None:
        profile = cls._json_mapping(value)
        if not profile:
            return None

        name = cls._optional_text(profile.get("name"))
        employee_alias = cls._optional_text(profile.get("employee_alias")) or name
        facts: list[AdminUserSessionProfileFact] = []
        raw_facts = cls._json_sequence_or_mapping(profile.get("facts"))
        if not raw_facts and cls._optional_text(profile.get("facts")):
            raw_facts = [profile.get("facts")]
        for raw_fact in raw_facts:
            fact = cls._json_mapping(raw_fact)
            description = cls._optional_text(
                fact.get("description") or fact.get("fact") or fact.get("text")
                if fact
                else raw_fact
            )
            if not description:
                continue
            facts.append(
                AdminUserSessionProfileFact(
                    description=description,
                    impact=cls._optional_text(fact.get("impact")) if fact else None,
                    evidence_source=(
                        cls._optional_text(
                            fact.get("evidence_source") or fact.get("source")
                        )
                        if fact
                        else None
                    ),
                )
            )

        constraints: dict[str, AdminUserSessionSensitiveConstraint] = {}
        for raw_key, raw_constraint in cls._json_mapping(
            profile.get("sensitive_constraints")
        ).items():
            key = cls._optional_text(raw_key)
            constraint = cls._json_mapping(raw_constraint)
            if not key or not constraint:
                continue
            constraints[key] = AdminUserSessionSensitiveConstraint(
                status=cls._optional_text(constraint.get("status")),
                business_impact_summary=cls._optional_text(
                    constraint.get("business_impact_summary")
                ),
            )

        return AdminUserSessionEmployeeProfile(
            name=name,
            employee_alias=employee_alias,
            employee_id=cls._optional_text(profile.get("employee_id")),
            role=cls._optional_text(profile.get("role") or profile.get("job_title")),
            department=cls._optional_text(profile.get("department")),
            level=cls._optional_text(profile.get("level") or profile.get("job_level")),
            reporting_line=cls._optional_text(profile.get("reporting_line")),
            performance_rating=cls._optional_text(profile.get("performance_rating")),
            tcl=cls._optional_text(profile.get("tcl")),
            review_cycle=cls._optional_text(profile.get("review_cycle")),
            conversation_topic=cls._optional_text(profile.get("conversation_topic")),
            key_goals=cls._text_list(profile.get("key_goals")),
            facts=facts,
            past_ratings=cls._text_list(profile.get("past_ratings")),
            historical_feedback=cls._text_list(profile.get("historical_feedback")),
            previous_improvement_discussion=cls._optional_text(
                profile.get("previous_improvement_discussion")
            ),
            management_actions=cls._text_list(profile.get("management_actions")),
            has_pip=cls._optional_text(profile.get("has_pip")),
            involves_promotion_salary_transfer=cls._optional_text(
                profile.get("involves_promotion_salary_transfer")
            ),
            employee_status_summary=cls._optional_text(
                profile.get("employee_status_summary")
            ),
            sensitive_constraints=constraints,
            source_profile_text=cls._optional_text(profile.get("source_profile_text")),
            supplemental_info=cls._optional_text(profile.get("supplemental_info")),
            current_career_elements=cls._text_list(
                profile.get("current_career_elements")
            ),
        )

    @classmethod
    def _project_admin_personality(
        cls,
        value: Any,
    ) -> AdminUserSessionPersonality | None:
        personality = cls._json_mapping(value)
        if not personality:
            return None
        values = {
            dimension: ReadableUserRecordArchiveWriter._personality_value(
                personality.get(dimension)
            )
            for dimension in (
                "openness",
                "conscientiousness",
                "extraversion",
                "agreeableness",
                "neuroticism",
            )
        }
        if not any(value is not None for value in values.values()):
            return None
        return AdminUserSessionPersonality(**values)

    @classmethod
    def _project_admin_intent(cls, value: Any) -> AdminUserSessionIntent | None:
        intent = cls._json_mapping(value)
        if not intent:
            return None
        config = cls._json_mapping(intent.get("config"))
        performance_items: list[AdminUserSessionIntentPerformanceItem] = []
        for raw_item in cls._json_sequence(intent.get("performance_items")):
            item = cls._json_mapping(raw_item)
            goal = cls._optional_text(item.get("goal") or item.get("title"))
            current_performance = cls._optional_text(
                item.get("current_performance") or item.get("performance")
            )
            if goal and current_performance:
                performance_items.append(
                    AdminUserSessionIntentPerformanceItem(
                        goal=goal,
                        current_performance=current_performance,
                    )
                )
        return AdminUserSessionIntent(
            intent_id=cls._optional_text(intent.get("intent_id") or intent.get("id")),
            name=cls._optional_text(
                config.get("name") or intent.get("name") or intent.get("label")
            ),
            performance_context=cls._optional_text(intent.get("performance_context")),
            performance_items=performance_items,
        )

    @classmethod
    def _project_admin_motivation(
        cls,
        value: Any,
    ) -> AdminUserSessionMotivation | None:
        motivation = cls._json_mapping(value)
        if not motivation:
            return None
        return AdminUserSessionMotivation(
            primary_motive_id=cls._optional_text(
                motivation.get("primary_motive_id")
            ),
            secondary_motive_ids=cls._text_list(
                motivation.get("secondary_motive_ids")
            ),
        )

    @classmethod
    def _project_admin_conversation(
        cls,
        value: Any,
    ) -> list[AdminUserSessionConversationTurn]:
        speaker_aliases = {
            "manager": "manager",
            "user": "manager",
            "employee": "employee",
            "assistant": "employee",
        }
        turns: list[AdminUserSessionConversationTurn] = []
        for stored_position, raw_turn in enumerate(cls._json_sequence(value), start=1):
            turn = cls._json_mapping(raw_turn)
            speaker = speaker_aliases.get(
                str(turn.get("speaker") or "").strip().lower()
            )
            text = cls._optional_text(turn.get("text") or turn.get("content"))
            if speaker is None or not text:
                continue
            raw_turn_index = turn.get("turn_index")
            try:
                turn_index = int(raw_turn_index)
            except (TypeError, ValueError):
                turn_index = stored_position
            if turn_index < 0:
                turn_index = stored_position
            turns.append(
                AdminUserSessionConversationTurn(
                    turn_index=turn_index,
                    speaker=speaker,
                    text=text,
                    created_at=cls._parse_datetime(turn.get("created_at")),
                )
            )
        return turns

    @classmethod
    def _project_admin_guidance(
        cls,
        value: Any,
    ) -> AdminUserSessionGuidance | None:
        guidance = cls._json_mapping(value)
        if not guidance:
            return None
        raw_dimension_value = guidance.get("dimension_points")
        raw_dimensions = cls._json_mapping(raw_dimension_value)
        if not raw_dimensions and isinstance(raw_dimension_value, (list, tuple)):
            raw_dimensions = {"start": list(raw_dimension_value)}
        if not raw_dimensions and isinstance(guidance.get("points"), (list, tuple)):
            raw_dimensions = {"start": list(guidance.get("points") or [])}
        dimensions: AdminUserSessionGuidanceDimensions | None = None
        if raw_dimensions:
            projected: dict[str, list[AdminUserSessionGuidancePoint]] = {}
            for dimension_id in ("start", "emotion", "requirement", "plan"):
                points: list[AdminUserSessionGuidancePoint] = []
                for raw_point in cls._json_sequence_or_mapping(
                    raw_dimensions.get(dimension_id)
                ):
                    point = cls._json_mapping(raw_point)
                    if not point:
                        continue
                    title = cls._optional_text(point.get("title"))
                    summary = cls._optional_text(point.get("summary"))
                    details = cls._text_list(
                        point.get("details") or point.get("descriptions")
                    )
                    if title or summary or details:
                        points.append(
                            AdminUserSessionGuidancePoint(
                                title=title,
                                summary=summary,
                                details=details,
                            )
                        )
                projected[dimension_id] = points
            dimensions = AdminUserSessionGuidanceDimensions(**projected)
        return AdminUserSessionGuidance(
            purpose=cls._joined_text(guidance.get("purpose")),
            opening_suggestion=cls._joined_text(
                guidance.get("opening_suggestion")
            ),
            risk_preview=cls._text_list(guidance.get("risk_preview")),
            response_strategies=cls._text_list(
                guidance.get("response_strategies")
            ),
            safer_phrases=cls._text_list(guidance.get("safer_phrases")),
            dimension_points=dimensions,
        )

    @classmethod
    def _project_admin_coach_tasks(
        cls,
        report_value: Any,
        checkpoint_value: Any,
    ) -> list[AdminUserSessionCoachTask]:
        report = cls._json_mapping(report_value)
        raw_tasks = cls._json_sequence(report.get("task_results"))
        if not raw_tasks:
            raw_tasks = cls._json_sequence(checkpoint_value)

        tasks: list[AdminUserSessionCoachTask] = []
        for position, raw_task in enumerate(raw_tasks, start=1):
            task = cls._json_mapping(raw_task)
            if not task:
                continue
            task_id = cls._optional_text(task.get("task_id")) or f"task-{position}"
            task_name = cls._optional_text(task.get("task_name")) or task_id
            bases = cls._text_list(task.get("basis"))
            for raw_score in cls._json_sequence_or_mapping(
                task.get("dimension_scores")
            ):
                score = cls._json_mapping(raw_score)
                basis = cls._optional_text(
                    score.get("basis")
                    or score.get("comment")
                    or score.get("reason")
                )
                if basis and basis not in bases:
                    bases.append(basis)

            risks: list[str] = []
            for raw_risk in cls._json_sequence(task.get("risks")):
                risk = cls._json_mapping(raw_risk)
                explanation = cls._optional_text(
                    risk.get("explanation") if risk else raw_risk
                )
                if explanation:
                    risks.append(explanation)

            better_phrases: list[AdminUserSessionCoachBetterPhrase] = []
            for raw_phrase in cls._json_sequence(task.get("better_phrases")):
                phrase = cls._json_mapping(raw_phrase)
                if isinstance(raw_phrase, str):
                    better_phrases.append(
                        AdminUserSessionCoachBetterPhrase(
                            suggestion=cls._optional_text(raw_phrase)
                        )
                    )
                    continue
                if not phrase:
                    # Keep the source index aligned with improvement_points for
                    # legacy reports whose suggestion array contains gaps.
                    better_phrases.append(AdminUserSessionCoachBetterPhrase())
                    continue
                projected_phrase = AdminUserSessionCoachBetterPhrase(
                    diagnostic_dimension_id=cls._optional_text(
                        phrase.get("diagnostic_dimension_id")
                    ),
                    original=cls._optional_text(
                        phrase.get("original") or phrase.get("original_context")
                    ),
                    suggestion=cls._optional_text(
                        phrase.get("suggestion")
                        or phrase.get("suggested")
                        or phrase.get("suggested_phrase")
                        or phrase.get("better_phrase")
                        or phrase.get("safer_phrase")
                        or phrase.get("phrase")
                    ),
                    reason=cls._optional_text(phrase.get("reason")),
                )
                better_phrases.append(projected_phrase)

            career_advice: list[AdminUserSessionCareerAdvice] = []
            for raw_advice in cls._json_sequence(
                task.get("career_elements_advice")
            ):
                advice = cls._json_mapping(raw_advice)
                if not advice:
                    continue
                projected_advice = AdminUserSessionCareerAdvice(
                    element=cls._optional_text(advice.get("element")),
                    suggestion=cls._optional_text(advice.get("suggestion")),
                    reason=cls._optional_text(advice.get("reason")),
                )
                if any(projected_advice.model_dump().values()):
                    career_advice.append(projected_advice)

            raw_score = task.get("score")
            try:
                task_score = int(raw_score) if raw_score is not None else None
            except (TypeError, ValueError):
                task_score = None
            tasks.append(
                AdminUserSessionCoachTask(
                    task_id=task_id,
                    task_name=task_name,
                    status=cls._normalized_coach_status(task.get("status")),
                    score=task_score,
                    summary=cls._optional_text(task.get("summary")),
                    basis=bases,
                    strengths=cls._text_list(task.get("strengths")),
                    improvement_points=cls._text_list(
                        task.get("improvement_points")
                    ),
                    risks=risks,
                    better_phrases=better_phrases,
                    career_elements_advice=career_advice,
                )
            )
        return tasks

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @classmethod
    def _joined_text(cls, value: Any) -> str | None:
        if isinstance(value, (list, tuple)):
            values = cls._text_list(value)
            return "\n".join(values) or None
        return cls._optional_text(value)

    @classmethod
    def _text_list(cls, value: Any) -> list[str]:
        values = value if isinstance(value, (list, tuple)) else [value]
        return [
            normalized
            for item in values
            if (normalized := cls._optional_text(item)) is not None
        ]

    @staticmethod
    def _json_sequence(value: Any) -> list[Any]:
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return []
            return list(parsed) if isinstance(parsed, list) else []
        return []

    @classmethod
    def _json_sequence_or_mapping(cls, value: Any) -> list[Any]:
        values = cls._json_sequence(value)
        if values:
            return values
        mapping = cls._json_mapping(value)
        return [mapping] if mapping else []

    @classmethod
    def _normalized_coach_status(cls, value: Any) -> str:
        status = (cls._optional_text(value) or "unknown").lower()
        if status in {"completed", "complete", "ok", "passed", "pass"}:
            return "success"
        if status in {
            "insufficient",
            "not_enough_information",
            "not_applicable",
            "n/a",
        }:
            return "insufficient_information"
        if status in {"error", "failure"}:
            return "failed"
        return status

    def _record_session_view_audit_best_effort(
        self,
        context: ExportAuditContext,
        *,
        session_id: str,
        target_user_id: str | None,
        target_user_email: str | None,
        success: bool,
        error_type: str | None,
    ) -> None:
        try:
            reason = {
                "actor_email": context.actor_email.strip().lower(),
                "session_id": session_id,
                "target_user_id": target_user_id,
                "target_user_email": (
                    target_user_email.strip().lower() if target_user_email else None
                ),
            }
            if error_type:
                reason["error_type"] = error_type
            with self.repository.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO auth_audit_log (
                        email, event_type, success, reason,
                        ip_address, user_agent, created_at
                    )
                    VALUES (
                        %s, 'admin_view_user_session', %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        context.actor_email.strip().lower(),
                        success,
                        json.dumps(
                            reason,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        context.ip_address,
                        context.user_agent,
                        datetime.now(timezone.utc),
                    ),
                )
        except Exception:
            # Observability must not make a successful read unavailable.
            return

    @classmethod
    def _conversation_catalog_item(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        profile = cls._json_mapping(row.get("employee_profile_json"))
        intent = cls._json_mapping(row.get("intent_json"))
        raw_conversation = row.get("conversation_json")
        conversation = (
            list(raw_conversation)
            if isinstance(raw_conversation, (list, tuple))
            else []
        )
        export_speakers = {"manager", "user", "employee", "assistant"}
        turns = [
            turn
            for turn in conversation
            if isinstance(turn, Mapping)
            and str(turn.get("speaker") or "").strip().lower() in export_speakers
            and str(turn.get("text") or turn.get("content") or "").strip()
        ]
        turn_speakers = {
            str(turn.get("speaker") or "").strip().lower()
            for turn in turns
        }
        has_rehearsal = bool(turn_speakers & {"manager", "user"}) and bool(
            turn_speakers & {"employee", "assistant"}
        )
        turn_dates = [
            parsed
            for parsed in (cls._parse_datetime(turn.get("created_at")) for turn in turns)
            if parsed is not None
        ]
        session_created_at = cls._parse_datetime(row.get("session_created_at"))
        if session_created_at is None:
            session_created_at = datetime.now(timezone.utc)
        employee_name = next(
            (
                str(profile.get(key)).strip()
                for key in ("name", "employee_alias", "employee_id")
                if str(profile.get(key) or "").strip()
            ),
            None,
        )
        intent_id = str(intent.get("intent_id") or "").strip() or None
        stage = str(row.get("stage") or "").strip() or None
        return {
            "session_id": str(row.get("session_id") or ""),
            "user_email": str(row.get("user_email") or "").strip().lower(),
            "user_display_name": str(row.get("user_display_name") or "").strip()
            or None,
            "employee_name": employee_name,
            "intent_id": intent_id,
            "stage": stage,
            "session_created_at": session_created_at,
            "conversation_started_at": min(turn_dates) if turn_dates else None,
            "conversation_ended_at": max(turn_dates) if turn_dates else None,
            "turn_count": len(turns),
            "has_conversation": bool(turns),
            "has_rehearsal": has_rehearsal,
        }

    @staticmethod
    def _json_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return {}
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        return {}

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def export_to(
        self,
        output_path: str | Path,
        *,
        audit_context: ExportAuditContext,
        selection: ExportSelection | None = None,
        validate_admin: bool = False,
    ) -> ExportArtifact:
        output = Path(output_path)
        actor_email = audit_context.actor_email.strip().lower()
        active_selection = selection or ExportSelection()
        partial_path: Path | None = None
        output_created = False
        try:
            if validate_admin:
                self.validate_admin(actor_email)
            if output.exists():
                raise FileExistsError(f"Export target already exists: {output}")
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, partial_name = tempfile.mkstemp(
                prefix=f".{output.name}.",
                suffix=".partial",
                dir=output.parent,
            )
            os.close(descriptor)
            partial_path = Path(partial_name)
            os.chmod(partial_path, 0o600)
            with self.repository.connection() as connection:
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                lock_row = connection.execute(
                    "SELECT pg_try_advisory_xact_lock(%s, %s) AS acquired",
                    (self._ADVISORY_LOCK_CLASS, self._ADVISORY_LOCK_OBJECT),
                ).fetchone()
                if not bool(PostgresRepository._row_value(lock_row, "acquired")):
                    raise ExportInProgress("Another user-content export is running")
                summary = self.archive_writer.write(
                    partial_path,
                    PostgresUserRecordSnapshot(connection, active_selection),
                    requested_by=actor_email,
                    selection=active_selection,
                )
            os.replace(partial_path, output)
            partial_path = None
            output_created = True
            os.chmod(output, 0o600)
            artifact = ExportArtifact(
                path=output,
                filename=output.name,
                generated_at=summary.generated_at,
                counts=summary.counts,
                sha256=summary.sha256,
                size_bytes=summary.size_bytes,
            )
            self._record_audit(
                audit_context,
                success=True,
                reason={
                    "archive_sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "counts": artifact.counts,
                    "selection": active_selection.audit_payload(),
                },
            )
            return artifact
        except Exception as exc:
            if partial_path is not None:
                partial_path.unlink(missing_ok=True)
            if output_created:
                output.unlink(missing_ok=True)
            self._record_audit_best_effort(
                audit_context,
                success=False,
                reason={
                    "error_type": type(exc).__name__,
                    "selection": active_selection.audit_payload(),
                },
            )
            raise

    def _record_audit(
        self,
        context: ExportAuditContext,
        *,
        success: bool,
        reason: Mapping[str, Any],
    ) -> None:
        with self.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO auth_audit_log (
                    email, event_type, success, reason,
                    ip_address, user_agent, created_at
                )
                VALUES (%s, 'admin_export_user_content', %s, %s, %s, %s, %s)
                """,
                (
                    context.actor_email.strip().lower(),
                    success,
                    json.dumps(
                        reason,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    context.ip_address,
                    context.user_agent,
                    datetime.now(timezone.utc),
                ),
            )

    def _record_audit_best_effort(
        self,
        context: ExportAuditContext,
        *,
        success: bool,
        reason: Mapping[str, Any],
    ) -> None:
        try:
            self._record_audit(context, success=success, reason=reason)
        except Exception:
            return

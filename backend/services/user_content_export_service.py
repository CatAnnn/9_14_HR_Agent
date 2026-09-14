from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from backend.config.settings import get_settings
from backend.repositories.postgres_repository import PostgresRepository


class UserContentExportError(RuntimeError):
    pass


class ExportInProgress(UserContentExportError):
    pass


class InvalidExportAdministrator(UserContentExportError):
    pass


@dataclass(frozen=True, slots=True)
class ExportAuditContext:
    actor_email: str
    ip_address: str
    user_agent: str


@dataclass(frozen=True, slots=True)
class ArchiveSummary:
    generated_at: datetime
    counts: dict[str, int]
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    path: Path
    filename: str
    generated_at: datetime
    counts: dict[str, int]
    sha256: str
    size_bytes: int


class UserContentSnapshot(Protocol):
    def iter_users(self) -> Iterator[dict[str, Any]]: ...

    def iter_sessions(self) -> Iterator[dict[str, Any]]: ...

    def iter_guidance_versions(self) -> Iterator[dict[str, Any]]: ...

    def iter_rehearsal_turns(self) -> Iterator[dict[str, Any]]: ...

    def iter_coach_versions(self) -> Iterator[dict[str, Any]]: ...

    def iter_coach_dimension_rows(self) -> Iterator[dict[str, Any]]: ...

    def iter_coach_task_checkpoints(self) -> Iterator[dict[str, Any]]: ...

    def iter_orphan_records(self) -> Iterator[dict[str, Any]]: ...


class PostgresUserContentSnapshot:
    def __init__(self, connection: Any, *, batch_size: int = 500) -> None:
        self.connection = connection
        self.batch_size = max(1, int(batch_size))
        self._cursor_index = 0

    def _iter_query(self, query: str) -> Iterator[dict[str, Any]]:
        self._cursor_index += 1
        cursor = self.connection.cursor(name=f"user_content_export_{self._cursor_index}")
        try:
            cursor.execute(query)
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                for row in rows:
                    yield dict(row)
        finally:
            cursor.close()

    def iter_users(self) -> Iterator[dict[str, Any]]:
        return self._iter_query(
            """
            SELECT
                account.id::text AS user_id,
                account.email::text AS email,
                account.display_name,
                account.auth_provider,
                account.role,
                account.is_active,
                account.is_email_verified,
                account.last_login_at,
                account.created_at,
                account.updated_at,
                (
                    SELECT COUNT(*)
                    FROM sessions AS owned_session
                    WHERE owned_session.owner_user_id = account.id
                ) AS session_count
            FROM app_users AS account
            ORDER BY lower(account.email::text), account.id
            """
        )

    def iter_sessions(self) -> Iterator[dict[str, Any]]:
        return self._iter_query(
            """
            SELECT
                workflow_session.session_id,
                workflow_session.owner_user_id::text AS user_id,
                account.email::text AS email,
                account.display_name,
                account.role AS user_role,
                account.is_active AS user_is_active,
                CASE
                    WHEN workflow_session.owner_user_id IS NULL THEN 'unowned'
                    ELSE 'owned'
                END AS ownership_status,
                workflow_session.state_json
                    - 'personality_facets'
                    - 'psychological_pattern_state' AS state_json,
                workflow_session.created_at AS db_created_at,
                workflow_session.updated_at AS db_updated_at,
                (
                    SELECT COUNT(*)
                    FROM guidance_report_versions AS guidance_history
                    WHERE guidance_history.session_id = workflow_session.session_id
                ) AS guidance_version_count,
                (
                    SELECT COUNT(*)
                    FROM coach_report_versions AS coach_history
                    WHERE coach_history.session_id = workflow_session.session_id
                ) AS coach_version_count
            FROM sessions AS workflow_session
            LEFT JOIN app_users AS account
                ON account.id = workflow_session.owner_user_id
            ORDER BY workflow_session.created_at, workflow_session.session_id
            """
        )

    def iter_guidance_versions(self) -> Iterator[dict[str, Any]]:
        return self._iter_query(
            """
            SELECT
                history.history_id,
                ROW_NUMBER() OVER (
                    PARTITION BY history.session_id
                    ORDER BY history.history_id
                ) AS version_number,
                history.history_id = MAX(history.history_id) OVER (
                    PARTITION BY history.session_id
                ) AS is_current,
                history.session_id,
                history.report_sha256,
                history.source,
                history.created_at AS report_created_at,
                workflow_session.owner_user_id::text AS user_id,
                account.email::text AS email,
                account.display_name,
                account.role AS user_role,
                account.is_active AS user_is_active,
                history.report_json
            FROM guidance_report_versions AS history
            INNER JOIN sessions AS workflow_session
                ON workflow_session.session_id = history.session_id
            LEFT JOIN app_users AS account
                ON account.id = workflow_session.owner_user_id
            ORDER BY workflow_session.created_at, history.session_id, history.history_id
            """
        )

    def iter_rehearsal_turns(self) -> Iterator[dict[str, Any]]:
        return self._iter_query(
            """
            SELECT
                workflow_session.session_id,
                workflow_session.owner_user_id::text AS user_id,
                account.email::text AS email,
                account.display_name,
                account.role AS user_role,
                account.is_active AS user_is_active,
                turn.ordinality AS stored_position,
                turn.item AS turn_json,
                workflow_session.created_at AS session_created_at
            FROM sessions AS workflow_session
            LEFT JOIN app_users AS account
                ON account.id = workflow_session.owner_user_id
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(workflow_session.state_json -> 'conversation') = 'array'
                        THEN workflow_session.state_json -> 'conversation'
                    ELSE '[]'::jsonb
                END
            ) WITH ORDINALITY AS turn(item, ordinality)
            ORDER BY
                workflow_session.created_at,
                workflow_session.session_id,
                COALESCE(
                    NULLIF(turn.item ->> 'turn_index', '')::integer,
                    turn.ordinality::integer
                )
            """
        )

    def iter_coach_versions(self) -> Iterator[dict[str, Any]]:
        return self._iter_query(
            """
            SELECT
                history.history_id,
                ROW_NUMBER() OVER (
                    PARTITION BY history.session_id
                    ORDER BY history.history_id
                ) AS version_number,
                history.history_id = MAX(history.history_id) OVER (
                    PARTITION BY history.session_id
                ) AS is_current,
                history.session_id,
                history.report_sha256,
                history.source,
                history.created_at AS report_created_at,
                workflow_session.owner_user_id::text AS user_id,
                account.email::text AS email,
                account.display_name,
                account.role AS user_role,
                account.is_active AS user_is_active,
                history.report_json
            FROM coach_report_versions AS history
            INNER JOIN sessions AS workflow_session
                ON workflow_session.session_id = history.session_id
            LEFT JOIN app_users AS account
                ON account.id = workflow_session.owner_user_id
            ORDER BY workflow_session.created_at, history.session_id, history.history_id
            """
        )

    def iter_coach_dimension_rows(self) -> Iterator[dict[str, Any]]:
        return self._iter_query(
            """
            SELECT
                history.history_id,
                ROW_NUMBER() OVER (
                    PARTITION BY history.session_id
                    ORDER BY history.history_id
                ) AS version_number,
                history.history_id = MAX(history.history_id) OVER (
                    PARTITION BY history.session_id
                ) AS is_current,
                history.session_id,
                history.report_sha256,
                history.source,
                history.created_at AS report_created_at,
                workflow_session.owner_user_id::text AS user_id,
                account.email::text AS email,
                account.display_name,
                account.role AS user_role,
                account.is_active AS user_is_active,
                history.report_json,
                task.ordinality AS task_position,
                task.item AS task_result_json
            FROM coach_report_versions AS history
            INNER JOIN sessions AS workflow_session
                ON workflow_session.session_id = history.session_id
            LEFT JOIN app_users AS account
                ON account.id = workflow_session.owner_user_id
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(history.report_json -> 'task_results') = 'array'
                        THEN history.report_json -> 'task_results'
                    ELSE '[]'::jsonb
                END
            ) WITH ORDINALITY AS task(item, ordinality)
            ORDER BY
                workflow_session.created_at,
                history.session_id,
                history.history_id,
                task.ordinality
            """
        )

    def iter_coach_task_checkpoints(self) -> Iterator[dict[str, Any]]:
        return self._iter_query(
            """
            SELECT
                checkpoint.session_id,
                workflow_session.owner_user_id::text AS user_id,
                account.email::text AS email,
                account.display_name,
                account.role AS user_role,
                account.is_active AS user_is_active,
                checkpoint.task_id,
                checkpoint.task_name,
                checkpoint.status,
                checkpoint.result_json,
                checkpoint.retrieval_ms,
                checkpoint.task_ms,
                checkpoint.pipeline_ms,
                checkpoint.created_at,
                checkpoint.updated_at,
                EXISTS (
                    SELECT 1
                    FROM coach_reports AS current_report
                    WHERE current_report.session_id = checkpoint.session_id
                ) AS has_final_report
            FROM coach_task_results AS checkpoint
            INNER JOIN sessions AS workflow_session
                ON workflow_session.session_id = checkpoint.session_id
            LEFT JOIN app_users AS account
                ON account.id = workflow_session.owner_user_id
            ORDER BY
                workflow_session.created_at,
                checkpoint.session_id,
                checkpoint.task_id
            """
        )

    def iter_orphan_records(self) -> Iterator[dict[str, Any]]:
        return self._iter_query(
            """
            SELECT
                'session_without_user'::text AS record_type,
                workflow_session.session_id,
                workflow_session.state_json
                    - 'personality_facets'
                    - 'psychological_pattern_state' AS record_json,
                workflow_session.created_at,
                workflow_session.updated_at
            FROM sessions AS workflow_session
            LEFT JOIN app_users AS account
                ON account.id = workflow_session.owner_user_id
            WHERE account.id IS NULL

            UNION ALL

            SELECT
                'guidance_report'::text AS record_type,
                current_report.session_id,
                current_report.report_json AS record_json,
                current_report.created_at,
                current_report.updated_at
            FROM guidance_reports AS current_report
            LEFT JOIN sessions AS workflow_session
                ON workflow_session.session_id = current_report.session_id
            WHERE workflow_session.session_id IS NULL

            UNION ALL

            SELECT
                'coach_report'::text AS record_type,
                current_report.session_id,
                current_report.report_json AS record_json,
                current_report.created_at,
                current_report.updated_at
            FROM coach_reports AS current_report
            LEFT JOIN sessions AS workflow_session
                ON workflow_session.session_id = current_report.session_id
            WHERE workflow_session.session_id IS NULL

            ORDER BY record_type, session_id
            """
        )


class UserContentArchiveWriter:
    RAW_BLOCKED_KEYS = frozenset(
        {
            "access_token",
            "api_key",
            "api_keys",
            "auth_session",
            "auth_sessions",
            "cookie",
            "cookies",
            "exception_stack",
            "input_fingerprint",
            "internal_exception",
            "internal_exception_text",
            "password_hash",
            "psychological_pattern_state",
            "provider_subject",
            "refresh_token",
            "session_token",
            "traceback",
        }
    )
    USER_FIELDS = (
        "user_id",
        "email",
        "display_name",
        "auth_provider",
        "role",
        "is_active",
        "is_email_verified",
        "last_login_at",
        "created_at",
        "updated_at",
        "session_count",
    )
    SESSION_FIELDS = (
        "user_id",
        "email",
        "display_name",
        "user_role",
        "user_is_active",
        "ownership_status",
        "session_id",
        "stage",
        "run_mode",
        "employee_profile_json",
        "supplemental_info",
        "intent_json",
        "performance_context",
        "personality_json",
        "motivation_json",
        "emotion_state_json",
        "setup_ready",
        "guidance_report_id",
        "coach_report_id",
        "user_turn_count",
        "conversation_turn_count",
        "warnings_json",
        "state_created_at",
        "state_updated_at",
        "ended_at",
        "db_created_at",
        "db_updated_at",
        "guidance_version_count",
        "coach_version_count",
    )
    GUIDANCE_FIELDS = (
        "user_id",
        "email",
        "display_name",
        "user_role",
        "user_is_active",
        "session_id",
        "history_id",
        "version_number",
        "is_current",
        "source",
        "report_sha256",
        "report_created_at",
        "intent_id",
        "guidance_version",
        "culture_version",
        "primary_motive_id",
        "secondary_motive_ids_json",
        "purpose_json",
        "opening_suggestion_json",
        "risk_preview_json",
        "response_strategies_json",
        "safer_phrases_json",
        "dimension_points_json",
        "evidence_policy",
        "citations_json",
        "disclaimer",
    )
    TURN_FIELDS = (
        "user_id",
        "email",
        "display_name",
        "user_role",
        "user_is_active",
        "session_id",
        "turn_index",
        "speaker",
        "text",
        "created_at",
        "metadata_json",
    )
    COACH_DIMENSION_FIELDS = (
        "user_id",
        "email",
        "display_name",
        "user_role",
        "user_is_active",
        "session_id",
        "history_id",
        "version_number",
        "is_current",
        "source",
        "report_sha256",
        "report_created_at",
        "coach_version",
        "report_status",
        "report_disclaimer",
        "task_position",
        "task_id",
        "task_name",
        "task_status",
        "score",
        "summary",
        "dimension_scores_json",
        "evidence_json",
        "strengths_json",
        "improvement_points_json",
        "risks_json",
        "better_phrases_json",
        "career_elements_advice_json",
        "citations_json",
        "extra_json",
    )
    CHECKPOINT_FIELDS = (
        "user_id",
        "email",
        "display_name",
        "user_role",
        "user_is_active",
        "session_id",
        "task_id",
        "task_name",
        "status",
        "score",
        "summary",
        "dimension_scores_json",
        "evidence_json",
        "strengths_json",
        "improvement_points_json",
        "risks_json",
        "better_phrases_json",
        "career_elements_advice_json",
        "citations_json",
        "extra_json",
        "retrieval_ms",
        "task_ms",
        "pipeline_ms",
        "created_at",
        "updated_at",
        "has_final_report",
    )

    def write(
        self,
        output_path: Path,
        snapshot: UserContentSnapshot,
        *,
        requested_by: str,
    ) -> ArchiveSummary:
        generated_at = datetime.now(timezone.utc)
        files: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}

        with zipfile.ZipFile(
            output_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            archive.writestr("README.txt", self._readme())

            users_csv = self._write_csv(
                archive,
                "users.csv",
                snapshot.iter_users(),
                self.USER_FIELDS,
                self._user_csv,
            )
            users_raw = self._write_jsonl(
                archive,
                "raw/users.jsonl",
                snapshot.iter_users(),
            )
            self._require_equal("users", users_csv, users_raw)
            counts["users"] = users_raw
            self._record_file(files, "users.csv", users_csv, "Registered user identities without credentials.")
            self._record_file(files, "raw/users.jsonl", users_raw, "Lossless safe user records.")

            sessions_csv = self._write_csv(
                archive,
                "sessions.csv",
                snapshot.iter_sessions(),
                self.SESSION_FIELDS,
                self._session_csv,
            )
            sessions_raw = self._write_jsonl(
                archive,
                "raw/sessions.jsonl",
                snapshot.iter_sessions(),
            )
            self._require_equal("sessions", sessions_csv, sessions_raw)
            counts["sessions"] = sessions_raw
            self._record_file(files, "sessions.csv", sessions_csv, "Session context and full preparation inputs.")
            self._record_file(files, "raw/sessions.jsonl", sessions_raw, "Lossless session state including conversations.")

            guidance_csv = self._write_csv(
                archive,
                "guidance_report_versions.csv",
                snapshot.iter_guidance_versions(),
                self.GUIDANCE_FIELDS,
                self._guidance_csv,
            )
            guidance_raw = self._write_jsonl(
                archive,
                "raw/guidance_report_versions.jsonl",
                snapshot.iter_guidance_versions(),
            )
            self._require_equal("guidance report versions", guidance_csv, guidance_raw)
            counts["guidance_report_versions"] = guidance_raw
            self._record_file(files, "guidance_report_versions.csv", guidance_csv, "All retained pre-conversation guidance versions.")
            self._record_file(files, "raw/guidance_report_versions.jsonl", guidance_raw, "Lossless guidance report versions.")

            turns_csv = self._write_csv(
                archive,
                "rehearsal_turns.csv",
                snapshot.iter_rehearsal_turns(),
                self.TURN_FIELDS,
                self._turn_csv,
            )
            turns_raw = self._write_jsonl(
                archive,
                "raw/rehearsal_turns.jsonl",
                snapshot.iter_rehearsal_turns(),
            )
            self._require_equal("rehearsal turns", turns_csv, turns_raw)
            counts["rehearsal_turns"] = turns_raw
            self._record_file(files, "rehearsal_turns.csv", turns_csv, "One complete row per rehearsal turn.")
            self._record_file(files, "raw/rehearsal_turns.jsonl", turns_raw, "Lossless extracted rehearsal turns.")

            coach_versions_raw = self._write_jsonl(
                archive,
                "raw/coach_report_versions.jsonl",
                snapshot.iter_coach_versions(),
            )
            coach_dimension_rows = self._write_csv(
                archive,
                "coach_report_versions.csv",
                snapshot.iter_coach_dimension_rows(),
                self.COACH_DIMENSION_FIELDS,
                self._coach_dimension_csv,
            )
            counts["coach_report_versions"] = coach_versions_raw
            counts["coach_dimension_rows"] = coach_dimension_rows
            self._record_file(files, "coach_report_versions.csv", coach_dimension_rows, "One row per retained Coach dimension result.")
            self._record_file(files, "raw/coach_report_versions.jsonl", coach_versions_raw, "Lossless Coach report versions.")

            checkpoints_csv = self._write_csv(
                archive,
                "coach_task_checkpoints.csv",
                snapshot.iter_coach_task_checkpoints(),
                self.CHECKPOINT_FIELDS,
                self._checkpoint_csv,
            )
            checkpoints_raw = self._write_jsonl(
                archive,
                "raw/coach_task_checkpoints.jsonl",
                snapshot.iter_coach_task_checkpoints(),
            )
            self._require_equal("Coach task checkpoints", checkpoints_csv, checkpoints_raw)
            counts["coach_task_checkpoints"] = checkpoints_raw
            self._record_file(files, "coach_task_checkpoints.csv", checkpoints_csv, "Persisted dimension content, excluding exception details and fingerprints.")
            self._record_file(files, "raw/coach_task_checkpoints.jsonl", checkpoints_raw, "Lossless safe Coach checkpoints.")

            orphan_records = self._write_jsonl(
                archive,
                "raw/orphan_records.jsonl",
                snapshot.iter_orphan_records(),
            )
            counts["orphan_records"] = orphan_records
            self._record_file(files, "raw/orphan_records.jsonl", orphan_records, "Sessions without a current user and reports without a current session.")

            manifest = {
                "schema": "hr-agent-user-content/v1",
                "generated_at": generated_at,
                "requested_by": requested_by,
                "scope": "all_users_all_time",
                "counts": counts,
                "files": files,
                "excluded": [
                    "password_hash",
                    "psychological_pattern_state",
                    "provider_subject",
                    "authentication_sessions",
                    "cookies",
                    "api_keys",
                    "internal_exception_text",
                    "coach_input_fingerprint",
                ],
            }
            archive.writestr(
                "manifest.json",
                json.dumps(
                    self._json_safe(manifest),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
            )

        size_bytes = output_path.stat().st_size
        sha256 = self._file_sha256(output_path)
        return ArchiveSummary(
            generated_at=generated_at,
            counts=counts,
            sha256=sha256,
            size_bytes=size_bytes,
        )

    @classmethod
    def _write_csv(
        cls,
        archive: zipfile.ZipFile,
        filename: str,
        rows: Iterable[Mapping[str, Any]],
        fieldnames: tuple[str, ...],
        transform: Any,
    ) -> int:
        count = 0
        with archive.open(filename, "w") as binary_file:
            text_file = io.TextIOWrapper(binary_file, encoding="utf-8-sig", newline="")
            try:
                writer = csv.DictWriter(
                    text_file,
                    fieldnames=fieldnames,
                    extrasaction="ignore",
                    lineterminator="\r\n",
                )
                writer.writeheader()
                for row in rows:
                    payload = transform(dict(row))
                    writer.writerow(
                        {
                            field: cls._csv_value(payload.get(field))
                            for field in fieldnames
                        }
                    )
                    count += 1
                text_file.flush()
            finally:
                text_file.detach()
        return count

    @classmethod
    def _write_jsonl(
        cls,
        archive: zipfile.ZipFile,
        filename: str,
        rows: Iterable[Mapping[str, Any]],
    ) -> int:
        count = 0
        with archive.open(filename, "w") as binary_file:
            text_file = io.TextIOWrapper(binary_file, encoding="utf-8", newline="\n")
            try:
                for row in rows:
                    text_file.write(
                        json.dumps(
                            cls._export_safe(dict(row)),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    text_file.write("\n")
                    count += 1
                text_file.flush()
            finally:
                text_file.detach()
        return count

    @staticmethod
    def _record_file(
        files: dict[str, dict[str, Any]],
        filename: str,
        rows: int,
        description: str,
    ) -> None:
        files[filename] = {"rows": rows, "description": description}

    @staticmethod
    def _require_equal(label: str, first: int, second: int) -> None:
        if first != second:
            raise UserContentExportError(
                f"Snapshot changed while exporting {label}: {first} != {second}"
            )

    @classmethod
    def _user_csv(cls, row: dict[str, Any]) -> dict[str, Any]:
        return row

    @classmethod
    def _session_csv(cls, row: dict[str, Any]) -> dict[str, Any]:
        state = cls._mapping(row.get("state_json"))
        conversation = cls._sequence(state.get("conversation"))
        return {
            **{key: row.get(key) for key in (
                "user_id",
                "email",
                "display_name",
                "user_role",
                "user_is_active",
                "ownership_status",
                "session_id",
                "db_created_at",
                "db_updated_at",
                "guidance_version_count",
                "coach_version_count",
            )},
            "stage": state.get("stage"),
            "run_mode": state.get("run_mode"),
            "employee_profile_json": cls._json_text(state.get("employee_profile")),
            "supplemental_info": state.get("supplemental_info"),
            "intent_json": cls._json_text(state.get("intent")),
            "performance_context": cls._mapping(state.get("intent")).get(
                "performance_context"
            ),
            "personality_json": cls._json_text(state.get("personality")),
            "motivation_json": cls._json_text(state.get("motivation")),
            "emotion_state_json": cls._json_text(state.get("emotion_state")),
            "setup_ready": state.get("setup_ready"),
            "guidance_report_id": state.get("guidance_report_id"),
            "coach_report_id": state.get("coach_report_id"),
            "user_turn_count": state.get("user_turn_count"),
            "conversation_turn_count": len(conversation),
            "warnings_json": cls._json_text(state.get("warnings")),
            "state_created_at": state.get("created_at"),
            "state_updated_at": state.get("updated_at"),
            "ended_at": state.get("ended_at"),
        }

    @classmethod
    def _guidance_csv(cls, row: dict[str, Any]) -> dict[str, Any]:
        report = cls._mapping(row.get("report_json"))
        return {
            **{key: row.get(key) for key in (
                "user_id",
                "email",
                "display_name",
                "user_role",
                "user_is_active",
                "session_id",
                "history_id",
                "version_number",
                "is_current",
                "source",
                "report_sha256",
                "report_created_at",
            )},
            "intent_id": report.get("intent_id"),
            "guidance_version": report.get("guidance_version"),
            "culture_version": report.get("culture_version"),
            "primary_motive_id": report.get("primary_motive_id"),
            "secondary_motive_ids_json": cls._json_text(report.get("secondary_motive_ids")),
            "purpose_json": cls._json_text(report.get("purpose")),
            "opening_suggestion_json": cls._json_text(report.get("opening_suggestion")),
            "risk_preview_json": cls._json_text(report.get("risk_preview")),
            "response_strategies_json": cls._json_text(report.get("response_strategies")),
            "safer_phrases_json": cls._json_text(report.get("safer_phrases")),
            "dimension_points_json": cls._json_text(report.get("dimension_points")),
            "evidence_policy": report.get("evidence_policy"),
            "citations_json": cls._json_text(report.get("citations")),
            "disclaimer": report.get("disclaimer"),
        }

    @classmethod
    def _turn_csv(cls, row: dict[str, Any]) -> dict[str, Any]:
        turn = cls._mapping(row.get("turn_json"))
        return {
            **{key: row.get(key) for key in (
                "user_id",
                "email",
                "display_name",
                "user_role",
                "user_is_active",
                "session_id",
            )},
            "turn_index": turn.get("turn_index") or row.get("stored_position"),
            "speaker": turn.get("speaker"),
            "text": turn.get("text"),
            "created_at": turn.get("created_at"),
            "metadata_json": cls._json_text(turn.get("metadata")),
        }

    @classmethod
    def _coach_dimension_csv(cls, row: dict[str, Any]) -> dict[str, Any]:
        report = cls._mapping(row.get("report_json"))
        task = cls._mapping(row.get("task_result_json"))
        return {
            **{key: row.get(key) for key in (
                "user_id",
                "email",
                "display_name",
                "user_role",
                "user_is_active",
                "session_id",
                "history_id",
                "version_number",
                "is_current",
                "source",
                "report_sha256",
                "report_created_at",
                "task_position",
            )},
            "coach_version": report.get("coach_version"),
            "report_status": report.get("status"),
            "report_disclaimer": report.get("disclaimer"),
            "task_id": task.get("task_id"),
            "task_name": task.get("task_name"),
            "task_status": task.get("status"),
            "score": task.get("score"),
            "summary": task.get("summary"),
            "dimension_scores_json": cls._json_text(task.get("dimension_scores")),
            "evidence_json": cls._json_text(task.get("evidence")),
            "strengths_json": cls._json_text(task.get("strengths")),
            "improvement_points_json": cls._json_text(task.get("improvement_points")),
            "risks_json": cls._json_text(task.get("risks")),
            "better_phrases_json": cls._json_text(task.get("better_phrases")),
            "career_elements_advice_json": cls._json_text(
                task.get("career_elements_advice")
            ),
            "citations_json": cls._json_text(task.get("citations")),
            "extra_json": cls._json_text(task.get("extra")),
        }

    @classmethod
    def _checkpoint_csv(cls, row: dict[str, Any]) -> dict[str, Any]:
        result = cls._mapping(row.get("result_json"))
        return {
            **{key: row.get(key) for key in (
                "user_id",
                "email",
                "display_name",
                "user_role",
                "user_is_active",
                "session_id",
                "task_id",
                "task_name",
                "status",
                "retrieval_ms",
                "task_ms",
                "pipeline_ms",
                "created_at",
                "updated_at",
                "has_final_report",
            )},
            "score": result.get("score"),
            "summary": result.get("summary"),
            "dimension_scores_json": cls._json_text(result.get("dimension_scores")),
            "evidence_json": cls._json_text(result.get("evidence")),
            "strengths_json": cls._json_text(result.get("strengths")),
            "improvement_points_json": cls._json_text(result.get("improvement_points")),
            "risks_json": cls._json_text(result.get("risks")),
            "better_phrases_json": cls._json_text(result.get("better_phrases")),
            "career_elements_advice_json": cls._json_text(
                result.get("career_elements_advice")
            ),
            "citations_json": cls._json_text(result.get("citations")),
            "extra_json": cls._json_text(result.get("extra")),
        }

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
        return list(value) if isinstance(value, (list, tuple)) else []

    @classmethod
    def _json_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return json.dumps(
            cls._export_safe(value),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls._json_safe(item) for item in value]
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, (UUID, Decimal)):
            return str(value)
        if isinstance(value, bytes):
            return value.hex()
        return value

    @classmethod
    def _export_safe(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): cls._export_safe(item)
                for key, item in value.items()
                if str(key).strip().lower() not in cls.RAW_BLOCKED_KEYS
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls._export_safe(item) for item in value]
        return cls._json_safe(value)

    @classmethod
    def _csv_value(cls, value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            value = cls._json_text(value)
        elif isinstance(value, (datetime, date, UUID, Decimal)):
            value = cls._json_safe(value)
        text = str(value)
        if text.lstrip()[:1] in {"=", "+", "-", "@"} or text[:1] in {"\t", "\r"}:
            return "'" + text
        return text

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _readme() -> str:
        return """HR Agent 全用户沟通内容导出

此归档包含可识别的用户、员工、谈前指导、完整多轮预演和复盘报告数据。
CSV 文件面向人工检查；raw/*.jsonl 保留原始嵌套结构，正文不会被截断。

安全要求：
1. 仅在受控 Bosch 设备和授权范围内使用。
2. 不要通过非受控邮件、即时通信或公共存储传递。
3. 使用完成后按内部数据保留政策删除。
4. 密码哈希、登录会话、Cookie、API 密钥和内部异常文本未包含在归档中。
"""


class UserContentExportService:
    _ADVISORY_LOCK_CLASS = 72810421
    _ADVISORY_LOCK_OBJECT = 20260722

    def __init__(
        self,
        repository: PostgresRepository | None = None,
        archive_writer: UserContentArchiveWriter | None = None,
    ) -> None:
        self.repository = repository or PostgresRepository()
        self.archive_writer = archive_writer or UserContentArchiveWriter()
        self.settings = get_settings()

    @staticmethod
    def default_filename(at: datetime | None = None) -> str:
        timestamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return f"hr_agent_user_content_{timestamp.strftime('%Y%m%dT%H%M%SZ')}.zip"

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

    def export_to(
        self,
        output_path: str | Path,
        *,
        audit_context: ExportAuditContext,
        validate_admin: bool = False,
    ) -> ExportArtifact:
        output = Path(output_path)
        actor_email = audit_context.actor_email.strip().lower()
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
                    raise ExportInProgress("Another full user-content export is running")
                summary = self.archive_writer.write(
                    partial_path,
                    PostgresUserContentSnapshot(connection),
                    requested_by=actor_email,
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
                reason={"error_type": type(exc).__name__},
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
                        UserContentArchiveWriter._json_safe(reason),
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

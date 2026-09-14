from __future__ import annotations

import json
from typing import Any

from backend.config.settings import get_settings
from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.employee_database import EmployeeDatabaseRecord, EmployeeDatabaseUpsertRequest
from backend.schemas.profile import EmployeeProfile


class EmployeeDatabaseService:
    """PostgreSQL-backed employee information lookup.

    Employee master data is stored in the same PostgreSQL database used for
    sessions, documents, reports, KB chunks, and pgvector embeddings.
    """

    def __init__(self, repository: PostgresRepository | None = None):
        self.settings = get_settings()
        self.repo = repository or PostgresRepository()
        self.database_url = self.settings.database_url

    def search(
        self,
        query: str | None = None,
        limit: int | None = None,
        employee_id: str | None = None,
        name: str | None = None,
    ) -> list[EmployeeDatabaseRecord]:
        needle = (query or "").strip()
        employee_id = (employee_id or "").strip()
        name = (name or "").strip()

        if limit is not None and limit < 1:
            raise ValueError("Employee search limit must be a positive integer.")

        where: list[str] = []
        params: list[str | int] = []
        if employee_id:
            where.append("employee_id = %s")
            params.append(employee_id)
        if name:
            where.append("(name ILIKE %s OR employee_alias ILIKE %s)")
            name_like = f"%{name}%"
            params.extend([name_like, name_like])
        searchable_fields = (
            "employee_id",
            "employee_alias",
            "name",
            "department",
            "role",
            "manager",
            "profile_text",
        )
        for term in needle.split():
            like = f"%{term}%"
            where.append(
                "(" + " OR ".join(f"{field} ILIKE %s" for field in searchable_fields) + ")"
            )
            params.extend([like] * len(searchable_fields))

        sql = "SELECT * FROM employees"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, employee_id ASC"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)

        with self.repo.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get(self, employee_id: str) -> EmployeeDatabaseRecord:
        with self.repo.connection() as conn:
            row = conn.execute("SELECT * FROM employees WHERE employee_id = %s", (employee_id,)).fetchone()
        if row is None:
            raise KeyError(f"Employee not found: {employee_id}")
        return self._row_to_record(row)

    def upsert(self, payload: EmployeeDatabaseUpsertRequest) -> EmployeeDatabaseRecord:
        with self.repo.connection() as conn:
            self._upsert_on_connection(conn, payload)
        return self.get(payload.employee_id)

    def sync_exact(
        self,
        payloads: list[EmployeeDatabaseUpsertRequest],
    ) -> tuple[list[EmployeeDatabaseRecord], int]:
        records, pruned, _ = self.sync_exact_if_changed(
            payloads,
            source_path=None,
            content_sha256=None,
        )
        return records, pruned

    def sync_exact_if_changed(
        self,
        payloads: list[EmployeeDatabaseUpsertRequest],
        *,
        source_path: str | None,
        content_sha256: str | None,
    ) -> tuple[list[EmployeeDatabaseRecord], int, bool]:
        if not payloads:
            raise ValueError("Exact employee sync requires at least one employee.")

        employee_ids = [payload.employee_id for payload in payloads]
        if len(set(employee_ids)) != len(employee_ids):
            raise ValueError("Exact employee sync does not allow duplicate employee IDs.")

        normalized_source = str(source_path or "").strip()
        normalized_sha = str(content_sha256 or "").strip().lower()
        if bool(normalized_source) != bool(normalized_sha):
            raise ValueError(
                "source_path and content_sha256 must be provided together."
            )
        if normalized_sha and (
            len(normalized_sha) != 64
            or any(character not in "0123456789abcdef" for character in normalized_sha)
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest.")

        with self.repo.connection() as conn:
            if normalized_source:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"employee-sync:{normalized_source}",),
                )
                manifest = conn.execute(
                    "SELECT content_sha256 FROM employee_import_manifests "
                    "WHERE source_path = %s",
                    (normalized_source,),
                ).fetchone()
                if manifest and manifest.get("content_sha256") == normalized_sha:
                    rows = conn.execute(
                        "SELECT * FROM employees ORDER BY employee_id ASC"
                    ).fetchall()
                    return [self._row_to_record(row) for row in rows], 0, False
            for payload in payloads:
                self._upsert_on_connection(conn, payload)
            pruned_rows = conn.execute(
                """
                DELETE FROM employees
                WHERE NOT (employee_id = ANY(%s))
                RETURNING employee_id
                """,
                (employee_ids,),
            ).fetchall()
            rows = conn.execute(
                """
                SELECT *
                FROM employees
                WHERE employee_id = ANY(%s)
                ORDER BY employee_id ASC
                """,
                (employee_ids,),
            ).fetchall()
            if normalized_source:
                conn.execute(
                    """
                    INSERT INTO employee_import_manifests (
                        source_path, content_sha256, employee_count, updated_at
                    )
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (source_path) DO UPDATE SET
                        content_sha256 = excluded.content_sha256,
                        employee_count = excluded.employee_count,
                        updated_at = NOW()
                    """,
                    (normalized_source, normalized_sha, len(employee_ids)),
                )

        return [self._row_to_record(row) for row in rows], len(pruned_rows), True

    @staticmethod
    def _upsert_on_connection(
        conn: Any,
        payload: EmployeeDatabaseUpsertRequest,
    ) -> None:
        profile_json = payload.profile.model_dump(mode="json", exclude_none=True) if payload.profile else None
        conn.execute(
            """
            INSERT INTO employees (
                employee_id, employee_alias, name, department, role, manager,
                profile_text, profile_json, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT(employee_id) DO UPDATE SET
                employee_alias = excluded.employee_alias,
                name = excluded.name,
                department = excluded.department,
                role = excluded.role,
                manager = excluded.manager,
                profile_text = excluded.profile_text,
                profile_json = excluded.profile_json,
                updated_at = NOW()
            """,
            (
                payload.employee_id,
                payload.employee_alias,
                payload.name,
                payload.department,
                payload.role,
                payload.manager,
                payload.profile_text,
                json.dumps(profile_json, ensure_ascii=False) if profile_json is not None else None,
            ),
        )

    def _row_to_record(self, row: dict[str, Any]) -> EmployeeDatabaseRecord:
        profile = self._parse_profile(row.get("profile_json"))
        record = EmployeeDatabaseRecord(
            employee_id=row["employee_id"],
            employee_alias=row.get("employee_alias"),
            name=row.get("name"),
            department=row.get("department"),
            role=row.get("role"),
            manager=row.get("manager"),
            profile_text=row.get("profile_text"),
            profile=profile,
            updated_at=row.get("updated_at").isoformat() if hasattr(row.get("updated_at"), "isoformat") else row.get("updated_at"),
        )
        if not record.profile_text:
            record.profile_text = self._build_profile_text(record)
        return record

    @staticmethod
    def _parse_profile(raw: Any) -> EmployeeProfile | None:
        if not raw:
            return None
        if isinstance(raw, str):
            raw = json.loads(raw)
        return EmployeeProfile.model_validate(raw)

    @staticmethod
    def _build_profile_text(record: EmployeeDatabaseRecord) -> str:
        lines = [
            ("员工ID", record.employee_id),
            ("员工代称", record.employee_alias or record.name),
            ("岗位", record.role),
            ("部门", record.department),
            ("汇报对象", record.manager),
        ]
        if record.profile:
            data: dict[str, Any] = record.profile.model_dump(exclude_none=True)
            for key in [
                "level",
                "reporting_line",
                "performance_rating",
                "tcl",
                "review_cycle",
                "conversation_topic",
                "employee_status_summary",
            ]:
                if data.get(key):
                    lines.append((key, data[key]))
            if data.get("key_goals"):
                lines.append(("关键目标", "、".join(map(str, data["key_goals"]))))
            if data.get("facts"):
                facts = [item.get("description") for item in data["facts"] if isinstance(item, dict) and item.get("description")]
                if facts:
                    lines.append(("事实", "；".join(facts)))
        return "\n".join(f"{label}：{value}" for label, value in lines if value)

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.employee_database import EmployeeDatabaseRecord, EmployeeDatabaseUpsertRequest
from backend.schemas.profile import EmployeeProfile
from backend.services.employee_database_service import EmployeeDatabaseService


def test_employee_database_profile_text_builder_without_database():
    record = EmployeeDatabaseRecord(
        employee_id="E001",
        employee_alias="员工A",
        department="销售部",
        role="销售经理",
        manager="经理B",
        profile=EmployeeProfile(
            employee_alias="员工A",
            role="销售经理",
            department="销售部",
            performance_rating="M-",
            review_cycle="2026 H1",
            conversation_topic="PIP / 绩效不达预期",
            key_goals=["季度销售额", "客户续约率"],
        ),
    )
    text = EmployeeDatabaseService._build_profile_text(record)
    assert "员工代称：员工A" in text
    assert "关键目标：季度销售额、客户续约率" in text


@pytest.mark.skipif(not os.getenv("POSTGRES_TEST_DATABASE_URL"), reason="需要真实 PostgreSQL + pgvector 测试库")
def test_employee_database_upsert_search_and_profile_text_against_postgres():
    repo = PostgresRepository(os.environ["POSTGRES_TEST_DATABASE_URL"])
    service = EmployeeDatabaseService(repository=repo)
    record = service.upsert(
        EmployeeDatabaseUpsertRequest(
            employee_id="E001",
            employee_alias="员工A",
            department="销售部",
            role="销售经理",
            manager="经理B",
            profile=EmployeeProfile(
                employee_alias="员工A",
                role="销售经理",
                department="销售部",
                performance_rating="M-",
                review_cycle="2026 H1",
                conversation_topic="PIP / 绩效不达预期",
                key_goals=["季度销售额", "客户续约率"],
            ),
        )
    )

    assert record.employee_id == "E001"
    assert "员工代称：员工A" in (record.profile_text or "")
    assert service.search("销售部")[0].employee_id == "E001"
    assert service.search(employee_id="E001")[0].employee_id == "E001"
    assert service.search(name="员工A")[0].employee_id == "E001"


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _SearchConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return _Rows([])


class _SearchRepository:
    def __init__(self):
        self.conn = _SearchConnection()

    @contextmanager
    def connection(self):
        yield self.conn


def test_employee_search_returns_all_matches_when_limit_is_omitted():
    repository = _SearchRepository()
    service = EmployeeDatabaseService(repository=repository)

    service.search()

    sql, params = repository.conn.calls[-1]
    assert " LIMIT " not in sql
    assert params == []


def test_employee_search_accepts_an_explicit_limit_without_an_upper_cap():
    repository = _SearchRepository()
    service = EmployeeDatabaseService(repository=repository)

    service.search(limit=500)

    sql, params = repository.conn.calls[-1]
    assert sql.endswith("LIMIT %s")
    assert params == [500]


def test_employee_search_matches_each_term_across_all_fields():
    repository = _SearchRepository()
    service = EmployeeDatabaseService(repository=repository)

    service.search("销售 经理")

    sql, params = repository.conn.calls[-1]
    assert sql.count("employee_id ILIKE %s") == 2
    assert ") AND (" in sql
    assert params == ["%销售%"] * 7 + ["%经理%"] * 7


def test_employee_search_combines_fuzzy_exact_id_and_name_filters():
    repository = _SearchRepository()
    service = EmployeeDatabaseService(repository=repository)

    service.search("销售 经理", employee_id="E001", name="员工A", limit=250)

    sql, params = repository.conn.calls[-1]
    assert "employee_id = %s" in sql
    assert "(name ILIKE %s OR employee_alias ILIKE %s)" in sql
    assert sql.count("employee_id ILIKE %s") == 2
    assert params == [
        "E001",
        "%员工A%",
        "%员工A%",
        *(["%销售%"] * 7),
        *(["%经理%"] * 7),
        250,
    ]


@pytest.mark.parametrize("limit", [0, -1])
def test_employee_search_rejects_non_positive_limits(limit):
    repository = _SearchRepository()
    service = EmployeeDatabaseService(repository=repository)

    with pytest.raises(ValueError, match="positive integer"):
        service.search(limit=limit)

    assert repository.conn.calls == []


class _ExactSyncConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        normalized_sql = " ".join(sql.split())
        self.calls.append((normalized_sql, params))
        if normalized_sql.startswith("DELETE FROM employees"):
            return _Rows([{"employee_id": "STALE"}])
        if normalized_sql.startswith("SELECT * FROM employees"):
            employee_ids = params[0]
            return _Rows(
                [
                    {
                        "employee_id": employee_id,
                        "employee_alias": employee_id,
                        "name": employee_id,
                        "department": None,
                        "role": None,
                        "manager": None,
                        "profile_text": None,
                        "profile_json": None,
                        "updated_at": None,
                    }
                    for employee_id in employee_ids
                ]
            )
        return _Rows([])


class _ExactSyncRepository:
    def __init__(self):
        self.conn = _ExactSyncConnection()

    @contextmanager
    def connection(self):
        yield self.conn


def test_employee_database_exact_sync_upserts_then_prunes_stale_rows():
    repository = _ExactSyncRepository()
    service = EmployeeDatabaseService(repository=repository)

    records, pruned = service.sync_exact(
        [
            EmployeeDatabaseUpsertRequest(employee_id="E001", name="员工A"),
            EmployeeDatabaseUpsertRequest(employee_id="E002", name="员工B"),
        ]
    )

    assert [record.employee_id for record in records] == ["E001", "E002"]
    assert pruned == 1
    assert sum(
        sql.startswith("INSERT INTO employees")
        for sql, _params in repository.conn.calls
    ) == 2
    delete_sql, delete_params = next(
        (sql, params)
        for sql, params in repository.conn.calls
        if sql.startswith("DELETE FROM employees")
    )
    assert "NOT (employee_id = ANY(%s))" in delete_sql
    assert delete_params == (["E001", "E002"],)


def test_employee_database_exact_sync_skips_unchanged_file_sha():
    digest = "a" * 64

    class Connection:
        def __init__(self):
            self.calls: list[str] = []

        def execute(self, sql, params=()):
            normalized_sql = " ".join(sql.split())
            self.calls.append(normalized_sql)
            if normalized_sql.startswith("SELECT pg_advisory_xact_lock"):
                return _Rows([])
            if normalized_sql.startswith("SELECT content_sha256"):
                return _Rows([{"content_sha256": digest}])
            if normalized_sql.startswith("SELECT * FROM employees"):
                return _Rows(
                    [
                        {
                            "employee_id": "E001",
                            "employee_alias": "员工A",
                            "name": "员工A",
                            "department": None,
                            "role": None,
                            "manager": None,
                            "profile_text": None,
                            "profile_json": None,
                            "updated_at": None,
                        }
                    ]
                )
            raise AssertionError(f"Unexpected write/query: {normalized_sql}")

    class Repository:
        def __init__(self):
            self.conn = Connection()

        @contextmanager
        def connection(self):
            yield self.conn

    repository = Repository()
    service = EmployeeDatabaseService(repository=repository)

    records, pruned, changed = service.sync_exact_if_changed(
        [EmployeeDatabaseUpsertRequest(employee_id="E001", name="员工A")],
        source_path="/app/data/employee_database/employees.xlsx",
        content_sha256=digest,
    )

    assert [record.employee_id for record in records] == ["E001"]
    assert pruned == 0
    assert changed is False
    assert not any(
        sql.startswith(("INSERT INTO employees", "DELETE FROM employees"))
        for sql in repository.conn.calls
    )


@pytest.mark.parametrize(
    "payloads",
    [
        [],
        [
            EmployeeDatabaseUpsertRequest(employee_id="E001"),
            EmployeeDatabaseUpsertRequest(employee_id="E001"),
        ],
    ],
)
def test_employee_database_exact_sync_rejects_unsafe_input(payloads):
    service = object.__new__(EmployeeDatabaseService)

    with pytest.raises(ValueError):
        service.sync_exact(payloads)


def test_backend_startup_uses_exact_employee_snapshot():
    compose = Path("deployment/compose/compose.services.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "python scripts/import_employees.py "
        "data/employee_database/employees.xlsx --exact --quiet;"
    ) in compose

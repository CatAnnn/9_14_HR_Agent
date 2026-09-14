from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from backend.config.settings import get_settings
from backend.observability.metrics import record_postgres_pool_wait
from backend.rag.structured_facts import (
    CULTURE_EXACT_FACT_POLICY_VERSION,
    contextual_exact_fact_text,
    extract_exact_facts,
)
from backend.vectorstore.embedding_profile import (
    EmbeddingCorpus,
    embedding_metadata_in_corpus,
    embedding_profile_corpus,
    embedding_profile_id,
    normalize_embedding_dimensions,
    normalize_embedding_model_name,
    resolve_embedding_dimensions,
)


@dataclass(frozen=True, slots=True)
class RetrievalScopeCapability:
    collection_name: str
    scope: str
    row_count: int
    bm25_ready: bool
    ann_dimensions: frozenset[int]
    retrieval_unit_bm25_ready: bool = True
    ann_required_dimensions: frozenset[int] = frozenset()

    def ann_ready(self, dimension: int) -> bool:
        return int(dimension) in self.ann_dimensions

    def ann_required(self, dimension: int) -> bool:
        return int(dimension) in self.ann_required_dimensions


@dataclass(frozen=True, slots=True)
class EmbeddingProfileState:
    profile_id: str
    model_name: str
    dimensions: int
    status: str
    active_build_id: str | None
    source_fingerprint: str
    chunk_count: int
    vector_count: int
    current_chunk_count: int
    stale_reason: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self.status == "ready"
            and bool(self.active_build_id)
            and self.chunk_count == self.current_chunk_count
            and self.vector_count == self.current_chunk_count
            and self.stale_reason is None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "model_name": self.model_name,
            "dimensions": self.dimensions,
            "status": self.status,
            "active_build_id": self.active_build_id,
            "source_fingerprint": self.source_fingerprint,
            "chunk_count": self.chunk_count,
            "vector_count": self.vector_count,
            "current_chunk_count": self.current_chunk_count,
            "stale_reason": self.stale_reason,
            "ready": self.ready,
        }


class PostgresRepository:
    """Shared PostgreSQL + pgvector access layer.

    All structured runtime data and KB vectors are stored in this single
    PostgreSQL database. The service fails fast when PostgreSQL, pgvector,
    schema creation, or credentials are unavailable.
    """

    REQUIRED_EXTENSION_VERSIONS = {
        "vector": "0.8.2",
        "vchord": "1.1.1",
        "pg_tokenizer": "0.1.1",
        "vchord_bm25": "0.3.0",
    }
    FORWARD_COMPATIBLE_EXTENSION_NAMES = {"vector"}
    REQUIRED_AVAILABLE_EXTENSION_VERSIONS = dict(REQUIRED_EXTENSION_VERSIONS)
    JIEBA_ANALYZER_CONFIG = """[pre_tokenizer.jieba]
[[character_filters]]
to_lowercase = {}
[[character_filters]]
unicode_normalization = "nfkc"
[[token_filters]]
skip_non_alphanumeric = {}
"""
    _schema_initialized_urls: set[str] = set()
    _schema_init_lock = threading.Lock()
    _retrieval_maintenance_lock = threading.Lock()
    _pools: dict[str, Any] = {}
    _pool_lock = threading.Lock()

    def __init__(self, database_url: str | None = None, *, initialize: bool = True):
        self.settings = get_settings()
        self.database_url = (database_url or self.settings.database_url or "").strip()
        if not self.database_url:
            raise RuntimeError("DATABASE_URL 未配置。PostgreSQL + pgvector 模式必须显式配置 DATABASE_URL。")
        if initialize and self.settings.postgres_auto_initialize_schema:
            self.ensure_schema_initialized()

    def ensure_schema_initialized(self) -> None:
        if self.database_url in self._schema_initialized_urls:
            return
        with self._schema_init_lock:
            if self.database_url in self._schema_initialized_urls:
                return
            self.init_schema()
            self._schema_initialized_urls.add(self.database_url)

    def _connection_pool(self):
        pool = self._pools.get(self.database_url)
        if pool is not None:
            return pool
        with self._pool_lock:
            pool = self._pools.get(self.database_url)
            if pool is not None:
                return pool
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError:
                return None
            max_size = max(1, int(self.settings.postgres_pool_max_size))
            min_size = min(max_size, max(0, int(self.settings.postgres_pool_min_size)))
            pool = ConnectionPool(
                conninfo=self.database_url,
                min_size=min_size,
                max_size=max_size,
                timeout=float(self.settings.postgres_pool_timeout_seconds),
                kwargs={"row_factory": dict_row},
                open=True,
            )
            self._pools[self.database_url] = pool
            return pool

    @classmethod
    def close_connection_pools(cls) -> None:
        with cls._pool_lock:
            pools = list(cls._pools.values())
            cls._pools.clear()
        for pool in pools:
            pool.close()

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg 未安装。请安装 backend/requirements.txt 后再连接 PostgreSQL。") from exc
        try:
            return psycopg.connect(self.database_url, row_factory=dict_row)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"无法连接 PostgreSQL：{exc}") from exc

    @contextmanager
    def autocommit_connection(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg 未安装。请安装 backend/requirements.txt 后再连接 PostgreSQL。") from exc
        try:
            conn = psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"无法创建 PostgreSQL 维护连接：{exc}") from exc
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def connection(self):
        pool = self._connection_pool()
        if pool is not None:
            wait_started = time.perf_counter()
            acquired = False
            try:
                with pool.connection(timeout=float(self.settings.postgres_pool_timeout_seconds)) as conn:
                    acquired = True
                    record_postgres_pool_wait(
                        (time.perf_counter() - wait_started) * 1000,
                        outcome="success",
                    )
                    try:
                        yield conn
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
            except Exception:
                if not acquired:
                    record_postgres_pool_wait(
                        (time.perf_counter() - wait_started) * 1000,
                        outcome="error",
                    )
                raise
            return
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_retrieval_extensions(self, conn) -> None:
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                """
                DO $migration$
                DECLARE
                    current_version INTEGER[];
                BEGIN
                    SELECT string_to_array(extversion, '.')::INTEGER[]
                    INTO current_version
                    FROM pg_extension
                    WHERE extname = 'vector';

                    IF current_version < ARRAY[0, 8, 2] THEN
                        ALTER EXTENSION vector UPDATE TO '0.8.2';
                    END IF;
                END
                $migration$
                """
            )
            conn.execute("CREATE EXTENSION IF NOT EXISTS vchord CASCADE")
            conn.execute("ALTER EXTENSION vchord UPDATE TO '1.1.1'")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_tokenizer CASCADE")
            conn.execute("ALTER EXTENSION pg_tokenizer UPDATE TO '0.1.1'")
            conn.execute("CREATE EXTENSION IF NOT EXISTS vchord_bm25 CASCADE")
            conn.execute("ALTER EXTENSION vchord_bm25 UPDATE TO '0.3.0'")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "PostgreSQL 检索扩展初始化失败。需要固定的 VectorChord Suite 镜像、"
                "CREATE EXTENSION 权限及 shared_preload_libraries 配置。"
            ) from exc

    @staticmethod
    def _row_value(row: Any, key: str) -> Any:
        if row is None:
            return None
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except (KeyError, TypeError):
            return row[0]

    @staticmethod
    def _json_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                return {}
            return dict(decoded) if isinstance(decoded, dict) else {}
        return {}

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return None if value is None else int(value)

    @staticmethod
    def _numeric_extension_version(value: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in str(value).split("."))
        except (TypeError, ValueError):
            return ()

    @classmethod
    def extension_version_is_supported(
        cls,
        extension_name: str,
        actual_version: str | None,
    ) -> bool:
        expected_version = cls.REQUIRED_EXTENSION_VERSIONS.get(extension_name)
        if expected_version is None or actual_version is None:
            return False
        if extension_name not in cls.FORWARD_COMPATIBLE_EXTENSION_NAMES:
            return actual_version == expected_version

        expected = cls._numeric_extension_version(expected_version)
        actual = cls._numeric_extension_version(actual_version)
        return (
            bool(expected)
            and bool(actual)
            and actual[:2] == expected[:2]
            and actual >= expected
        )

    @classmethod
    def _read_installed_extension_versions(cls, conn) -> dict[str, str]:
        rows = conn.execute(
            "SELECT extname, extversion FROM pg_extension WHERE extname = ANY(%s)",
            (list(cls.REQUIRED_EXTENSION_VERSIONS),),
        ).fetchall()
        return {
            str(cls._row_value(row, "extname")): str(cls._row_value(row, "extversion"))
            for row in rows
        }

    def installed_extension_versions(self) -> dict[str, str]:
        with self.connection() as conn:
            return self._read_installed_extension_versions(conn)

    def _validate_retrieval_runtime(self, conn) -> None:
        installed = self._read_installed_extension_versions(conn)
        unsupported = {
            name: installed.get(name)
            for name in self.REQUIRED_EXTENSION_VERSIONS
            if not self.extension_version_is_supported(name, installed.get(name))
        }
        if unsupported:
            raise RuntimeError(
                "PostgreSQL 已安装检索扩展版本不兼容："
                f"required={self.REQUIRED_EXTENSION_VERSIONS}, "
                f"unsupported={unsupported}, actual={installed}"
            )

        available_rows = conn.execute(
            "SELECT name, default_version FROM pg_available_extensions WHERE name = ANY(%s)",
            (list(self.REQUIRED_AVAILABLE_EXTENSION_VERSIONS),),
        ).fetchall()
        available = {
            str(self._row_value(row, "name")): str(self._row_value(row, "default_version"))
            for row in available_rows
        }
        if available != self.REQUIRED_AVAILABLE_EXTENSION_VERSIONS:
            raise RuntimeError(
                "PostgreSQL 镜像内检索扩展版本不匹配："
                f"expected={self.REQUIRED_AVAILABLE_EXTENSION_VERSIONS}, actual={available}"
            )

        max_connections_row = conn.execute("SHOW max_connections").fetchone()
        max_connections = int(self._row_value(max_connections_row, "max_connections") or 0)
        required_connections = int(self.settings.postgres_pool_max_size) + 20
        if max_connections < required_connections:
            raise RuntimeError(
                f"PostgreSQL max_connections={max_connections}，低于应用连接池和维护余量要求 "
                f"{required_connections}。"
            )

        preload_row = conn.execute("SHOW shared_preload_libraries").fetchone()
        preload_value = str(self._row_value(preload_row, "shared_preload_libraries") or "")
        preloaded = {item.strip() for item in preload_value.split(",") if item.strip()}
        required_preloads = {"vchord", "vchord_bm25", "vector", "pg_tokenizer"}
        if not required_preloads.issubset(preloaded):
            raise RuntimeError(
                "PostgreSQL shared_preload_libraries 缺少检索扩展："
                f"required={sorted(required_preloads)}, actual={sorted(preloaded)}"
            )

        search_path_row = conn.execute("SHOW search_path").fetchone()
        search_path = str(self._row_value(search_path_row, "search_path") or "")
        if "bm25_catalog" not in search_path or "tokenizer_catalog" not in search_path:
            raise RuntimeError(f"PostgreSQL search_path 未包含 BM25/tokenizer catalog：{search_path}")

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("hr_agent_schema_migration",),
            )
            self._ensure_retrieval_extensions(conn)
            conn.execute("CREATE EXTENSION IF NOT EXISTS citext")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_users (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    email CITEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    password_hash TEXT,
                    auth_provider TEXT NOT NULL DEFAULT 'local',
                    provider_subject TEXT,
                    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    is_email_verified BOOLEAN NOT NULL DEFAULT TRUE,
                    last_login_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_whitelist (
                    email CITEXT PRIMARY KEY,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    note TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_audit_log (
                    id BIGSERIAL PRIMARY KEY,
                    email CITEXT,
                    event_type TEXT NOT NULL,
                    success BOOLEAN NOT NULL,
                    reason TEXT,
                    ip_address TEXT,
                    user_agent TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_audit_created_at ON auth_audit_log(created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_audit_email ON auth_audit_log(email)")

            settings = getattr(self, "settings", None) or get_settings()
            bootstrap_emails = settings.auth_allowed_email_set | settings.auth_admin_email_set
            for email in sorted(bootstrap_emails):
                conn.execute(
                    """
                    INSERT INTO auth_whitelist (email, enabled, note)
                    VALUES (%s, TRUE, 'configured bootstrap whitelist')
                    ON CONFLICT (email) DO NOTHING
                    """,
                    (email,),
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id TEXT PRIMARY KEY,
                    employee_alias TEXT,
                    name TEXT,
                    department TEXT,
                    role TEXT,
                    manager TEXT,
                    profile_text TEXT,
                    profile_json JSONB,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_employees_alias ON employees(employee_alias)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_employees_name ON employees(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_employees_department ON employees(department)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_employees_role ON employees(role)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
                    state_json JSONB NOT NULL,
                    revision BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES app_users(id) ON DELETE SET NULL"
            )
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS employee_import_manifests (
                    source_path TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL CHECK (
                        char_length(content_sha256) = 64
                    ),
                    employee_count INTEGER NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated ON sessions(owner_user_id, updated_at DESC)")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sessions_owner_employee_updated
                ON sessions (
                    owner_user_id,
                    (state_json #>> '{employee_profile,employee_id}'),
                    updated_at DESC
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS personality_facet_profiles (
                    profile_key TEXT PRIMARY KEY,
                    employee_key_hash TEXT NOT NULL,
                    parent_scores JSONB NOT NULL,
                    facets_json JSONB NOT NULL,
                    source TEXT NOT NULL CHECK (
                        source IN ('model', 'deterministic_fallback')
                    ),
                    generator_version TEXT NOT NULL,
                    model_name TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_personality_facet_profiles_employee
                ON personality_facet_profiles(employee_key_hash, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
                    generation BIGINT NOT NULL DEFAULT 0,
                    summary_text TEXT NOT NULL DEFAULT '',
                    covered_through_turn_index INTEGER NOT NULL DEFAULT 0,
                    model_name TEXT,
                    prompt_version TEXT NOT NULL DEFAULT 'v1',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CHECK (generation >= 0),
                    CHECK (covered_through_turn_index >= 0)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    filename TEXT,
                    raw_path TEXT,
                    record_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guidance_reports (
                    session_id TEXT PRIMARY KEY,
                    report_json JSONB NOT NULL,
                    source TEXT NOT NULL DEFAULT 'generation',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "ALTER TABLE guidance_reports "
                "ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'generation'"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coach_reports (
                    session_id TEXT PRIMARY KEY,
                    report_json JSONB NOT NULL,
                    source TEXT NOT NULL DEFAULT 'generation',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "ALTER TABLE coach_reports "
                "ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'generation'"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guidance_report_versions (
                    history_id BIGSERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    report_json JSONB NOT NULL,
                    report_sha256 TEXT NOT NULL CHECK (char_length(report_sha256) = 64),
                    source TEXT NOT NULL,
                    input_context_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "ALTER TABLE guidance_report_versions "
                "ADD COLUMN IF NOT EXISTS input_context_json JSONB"
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_guidance_report_versions_session
                ON guidance_report_versions(session_id, history_id)
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_guidance_report_legacy_backfill
                ON guidance_report_versions(session_id)
                WHERE source = 'legacy_backfill'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coach_report_versions (
                    history_id BIGSERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    report_json JSONB NOT NULL,
                    report_sha256 TEXT NOT NULL CHECK (char_length(report_sha256) = 64),
                    source TEXT NOT NULL,
                    input_context_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "ALTER TABLE coach_report_versions "
                "ADD COLUMN IF NOT EXISTS input_context_json JSONB"
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_coach_report_versions_session
                ON coach_report_versions(session_id, history_id)
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_coach_report_legacy_backfill
                ON coach_report_versions(session_id)
                WHERE source = 'legacy_backfill'
                """
            )
            conn.execute(
                """
                INSERT INTO guidance_report_versions (
                    session_id, report_json, report_sha256, source, created_at
                )
                SELECT
                    report.session_id,
                    report.report_json,
                    encode(
                        digest(convert_to(report.report_json::text, 'UTF8'), 'sha256'),
                        'hex'
                    ),
                    'legacy_backfill',
                    report.updated_at
                FROM guidance_reports AS report
                INNER JOIN sessions AS session
                    ON session.session_id = report.session_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM guidance_report_versions AS history
                    WHERE history.session_id = report.session_id
                )
                ON CONFLICT (session_id) WHERE source = 'legacy_backfill'
                DO NOTHING
                """
            )
            conn.execute(
                """
                INSERT INTO coach_report_versions (
                    session_id, report_json, report_sha256, source, created_at
                )
                SELECT
                    report.session_id,
                    report.report_json,
                    encode(
                        digest(convert_to(report.report_json::text, 'UTF8'), 'sha256'),
                        'hex'
                    ),
                    'legacy_backfill',
                    report.updated_at
                FROM coach_reports AS report
                INNER JOIN sessions AS session
                    ON session.session_id = report.session_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM coach_report_versions AS history
                    WHERE history.session_id = report.session_id
                )
                ON CONFLICT (session_id) WHERE source = 'legacy_backfill'
                DO NOTHING
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coach_task_results (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json JSONB NOT NULL,
                    warning TEXT,
                    error TEXT,
                    retrieval_ms INTEGER,
                    task_ms INTEGER,
                    pipeline_ms INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (session_id, task_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_coach_task_results_fingerprint "
                "ON coach_task_results(session_id, input_fingerprint)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_documents (
                    doc_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_documents_scope ON kb_documents(scope)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS document_image_analyses (
                    cache_key TEXT PRIMARY KEY,
                    image_sha256 TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    analysis_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_document_image_analyses_sha256 "
                "ON document_image_analyses(image_sha256)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    collection_name TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    text TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    embedding VECTOR NOT NULL,
                    bm25_embedding bm25_catalog.bm25vector,
                    index_version TEXT NOT NULL,
                    content_hash TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS "
                "bm25_embedding bm25_catalog.bm25vector"
            )
            conn.execute(
                "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS search_text TEXT"
            )
            conn.execute(
                "UPDATE kb_chunks SET search_text = text "
                "WHERE search_text IS NULL OR search_text = ''"
            )
            conn.execute(
                "ALTER TABLE kb_chunks ALTER COLUMN search_text SET NOT NULL"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_collection ON kb_chunks(collection_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_scope ON kb_chunks(scope)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunks_collection_scope "
                "ON kb_chunks(collection_name, scope)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc_id ON kb_chunks(doc_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_metadata ON kb_chunks USING GIN(metadata)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_retrieval_units (
                    unit_id TEXT PRIMARY KEY,
                    parent_chunk_id TEXT NOT NULL
                        REFERENCES kb_chunks(chunk_id) ON DELETE CASCADE,
                    doc_id TEXT NOT NULL,
                    collection_name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    unit_type TEXT NOT NULL,
                    unit_index INTEGER NOT NULL CHECK (unit_index >= 0),
                    text TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    bm25_embedding bm25_catalog.bm25vector,
                    index_version TEXT NOT NULL,
                    content_hash TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (parent_chunk_id, unit_type, unit_index)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_retrieval_units_parent "
                "ON kb_retrieval_units(parent_chunk_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_retrieval_units_doc "
                "ON kb_retrieval_units(doc_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_retrieval_units_scope "
                "ON kb_retrieval_units(collection_name, scope)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_sections (
                    section_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL REFERENCES kb_documents(doc_id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    index_version TEXT NOT NULL,
                    parent_section_id TEXT,
                    chapter_section_id TEXT,
                    title TEXT NOT NULL,
                    level INTEGER NOT NULL CHECK (level >= 0),
                    heading_path JSONB NOT NULL DEFAULT '[]'::jsonb,
                    source_start_line INTEGER,
                    source_end_line INTEGER,
                    source_start_char INTEGER,
                    source_end_char INTEGER,
                    text TEXT NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_sections_doc "
                "ON kb_sections(doc_id, source_start_line)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_sections_parent "
                "ON kb_sections(parent_section_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_exact_facts (
                    fact_id TEXT PRIMARY KEY,
                    chunk_id TEXT NOT NULL REFERENCES kb_chunks(chunk_id) ON DELETE CASCADE,
                    doc_id TEXT NOT NULL,
                    section_id TEXT,
                    scope TEXT NOT NULL,
                    index_version TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    display_value TEXT NOT NULL,
                    source_quote TEXT NOT NULL,
                    source_start_line INTEGER,
                    source_end_line INTEGER,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_exact_facts_lookup "
                "ON kb_exact_facts(scope, index_version, normalized_value)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_exact_facts_global_lookup "
                "ON kb_exact_facts("
                "fact_type, normalized_value, chunk_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_exact_facts_chunk "
                "ON kb_exact_facts(chunk_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_retrieval_unit_exact_facts (
                    unit_id TEXT NOT NULL
                        REFERENCES kb_retrieval_units(unit_id) ON DELETE CASCADE,
                    parent_chunk_id TEXT NOT NULL
                        REFERENCES kb_chunks(chunk_id) ON DELETE CASCADE,
                    fact_type TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    source_quote TEXT NOT NULL,
                    document_frequency INTEGER NOT NULL DEFAULT 1
                        CHECK (document_frequency > 0),
                    PRIMARY KEY (unit_id, fact_type, normalized_value)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_kb_retrieval_unit_exact_facts_lookup "
                "ON kb_retrieval_unit_exact_facts("
                "fact_type, normalized_value, document_frequency)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_kb_retrieval_unit_exact_facts_parent "
                "ON kb_retrieval_unit_exact_facts(parent_chunk_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_kb_retrieval_unit_exact_facts_unit "
                "ON kb_retrieval_unit_exact_facts(unit_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_profiles (
                    profile_id TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
                    active_build_id UUID,
                    status TEXT NOT NULL DEFAULT 'building',
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    chunk_count BIGINT NOT NULL DEFAULT 0,
                    vector_count BIGINT NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (model_name, dimensions)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_profile_builds (
                    build_id UUID PRIMARY KEY,
                    profile_id TEXT NOT NULL
                        REFERENCES embedding_profiles(profile_id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'building',
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    chunk_count BIGINT NOT NULL DEFAULT 0,
                    vector_count BIGINT NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_chunk_embeddings (
                    build_id UUID NOT NULL
                        REFERENCES embedding_profile_builds(build_id) ON DELETE CASCADE,
                    profile_id TEXT NOT NULL
                        REFERENCES embedding_profiles(profile_id) ON DELETE CASCADE,
                    chunk_id TEXT NOT NULL
                        REFERENCES kb_chunks(chunk_id) ON DELETE CASCADE,
                    collection_name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    embedding VECTOR NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (build_id, chunk_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunk_embeddings_profile_build "
                "ON kb_chunk_embeddings(profile_id, build_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunk_embeddings_scope "
                "ON kb_chunk_embeddings(profile_id, build_id, collection_name, scope)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunk_embeddings_chunk "
                "ON kb_chunk_embeddings(chunk_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_retrieval_unit_embeddings (
                    build_id UUID NOT NULL
                        REFERENCES embedding_profile_builds(build_id) ON DELETE CASCADE,
                    profile_id TEXT NOT NULL
                        REFERENCES embedding_profiles(profile_id) ON DELETE CASCADE,
                    unit_id TEXT NOT NULL
                        REFERENCES kb_retrieval_units(unit_id) ON DELETE CASCADE,
                    collection_name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    embedding VECTOR NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (build_id, unit_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_retrieval_unit_embeddings_build "
                "ON kb_retrieval_unit_embeddings(profile_id, build_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_retrieval_unit_embeddings_scope "
                "ON kb_retrieval_unit_embeddings("
                "profile_id, build_id, collection_name, scope)"
            )
            self._backfill_legacy_embedding_profile(conn)
            if getattr(self, "settings", None) is not None:
                self._validate_retrieval_runtime(conn)
            conn.execute(
                """
                INSERT INTO schema_migrations (version)
                VALUES ('2026-08-09-high-concurrency-v1')
                ON CONFLICT (version) DO NOTHING
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations (version) "
                "VALUES ('2026-08-14-embedding-profiles-v1') "
                "ON CONFLICT (version) DO NOTHING"
            )
            conn.execute(
                "INSERT INTO schema_migrations (version) "
                "VALUES ('2026-08-21-structured-kb-v3') "
                "ON CONFLICT (version) DO NOTHING"
            )
            conn.execute(
                "INSERT INTO schema_migrations (version) "
                "VALUES ('2026-08-22-retrieval-units-v1') "
                "ON CONFLICT (version) DO NOTHING"
            )
            exact_fact_migration = conn.execute(
                "SELECT version FROM schema_migrations "
                "WHERE version = '2026-08-23-unit-exact-facts-v1'"
            ).fetchone()
            if exact_fact_migration is None:
                self._rebuild_exact_fact_indexes_on_connection(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version) "
                    "VALUES ('2026-08-23-unit-exact-facts-v1') "
                    "ON CONFLICT (version) DO NOTHING"
                )
            self._migrate_exact_fact_policy_on_connection(conn)

    def _migrate_exact_fact_policy_on_connection(self, conn) -> bool:
        migration_version = (
            "2026-08-25-exact-facts-"
            f"{CULTURE_EXACT_FACT_POLICY_VERSION}"
        )
        migration = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = %s",
            (migration_version,),
        ).fetchone()
        if migration is not None:
            return False

        # Exact facts are derived search indexes. Rebuild them from stored text
        # without touching chunks, vector embeddings, or the active build.
        conn.execute(
            "DELETE FROM kb_exact_facts WHERE fact_type = %s",
            ("culture",),
        )
        self._rebuild_exact_fact_indexes_on_connection(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) "
            "ON CONFLICT (version) DO NOTHING",
            (migration_version,),
        )
        return True

    @staticmethod
    def _embedding_corpus_predicate(
        corpus: EmbeddingCorpus,
        *,
        metadata_expression: str,
    ) -> tuple[str, tuple[Any, ...]]:
        if corpus == "kb_raw":
            return (
                f"COALESCE({metadata_expression} ->> 'source_path', '') "
                "LIKE %s",
                ("%/data/kb_raw/%",),
            )
        return "TRUE", ()

    def _profile_corpus_on_connection(
        self,
        conn,
        profile_id: str,
    ) -> EmbeddingCorpus:
        row = conn.execute(
            """
            SELECT model_name, dimensions
            FROM embedding_profiles
            WHERE profile_id = %s
            """,
            (profile_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Embedding profile does not exist: {profile_id}")
        return embedding_profile_corpus(
            str(self._row_value(row, "model_name")),
            int(self._row_value(row, "dimensions")),
        )

    @classmethod
    def _knowledge_fingerprint_on_connection(
        cls,
        conn,
        *,
        model_name: str,
        dimensions: int,
    ) -> tuple[str, int]:
        corpus = embedding_profile_corpus(model_name, dimensions)
        chunk_predicate, chunk_params = cls._embedding_corpus_predicate(
            corpus,
            metadata_expression="c.metadata",
        )
        rows = conn.execute(
            f"""
            SELECT c.chunk_id, c.content_hash, c.index_version
            FROM kb_chunks c
            WHERE {chunk_predicate}
            ORDER BY c.chunk_id
            """,
            chunk_params,
        ).fetchall()
        digest = hashlib.sha256()
        if corpus != "all":
            digest.update(f"corpus:{corpus}\x00".encode("utf-8"))
        for row in rows:
            digest.update(b"chunk\x00")
            for key in ("chunk_id", "content_hash", "index_version"):
                digest.update(
                    str(cls._row_value(row, key) or "").encode("utf-8")
                )
                digest.update(b"\x00")
        unit_predicate, unit_params = cls._embedding_corpus_predicate(
            corpus,
            metadata_expression="c.metadata",
        )
        unit_rows = conn.execute(
            f"""
            SELECT u.unit_id, u.content_hash, u.index_version
            FROM kb_retrieval_units u
            JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
            WHERE {unit_predicate}
            ORDER BY u.unit_id
            """,
            unit_params,
        ).fetchall()
        for row in unit_rows:
            digest.update(b"retrieval_unit\x00")
            for key in ("unit_id", "content_hash", "index_version"):
                digest.update(
                    str(cls._row_value(row, key) or "").encode("utf-8")
                )
                digest.update(b"\x00")
        return digest.hexdigest(), len(rows)

    def _backfill_legacy_embedding_profile(self, conn) -> None:
        dimension_rows = conn.execute(
            """
            SELECT vector_dims(embedding) AS dimensions, COUNT(*) AS row_count
            FROM kb_chunks
            GROUP BY vector_dims(embedding)
            ORDER BY dimensions
            """
        ).fetchall()
        if not dimension_rows:
            return
        if len(dimension_rows) != 1:
            return

        dimension = int(self._row_value(dimension_rows[0], "dimensions"))
        model_rows = conn.execute(
            """
            SELECT DISTINCT NULLIF(BTRIM(metadata ->> 'embedding_model'), '') AS model_name
            FROM kb_documents
            WHERE NULLIF(BTRIM(metadata ->> 'embedding_model'), '') IS NOT NULL
            ORDER BY model_name
            """
        ).fetchall()
        models = [
            str(self._row_value(row, "model_name"))
            for row in model_rows
            if self._row_value(row, "model_name")
        ]
        model_name = (
            models[0]
            if len(models) == 1
            else str(self.settings.effective_embedding_model)
        )
        model_name = normalize_embedding_model_name(model_name)
        profile_id = embedding_profile_id(model_name, dimension)
        existing = conn.execute(
            "SELECT active_build_id FROM embedding_profiles WHERE profile_id = %s",
            (profile_id,),
        ).fetchone()
        if existing and self._row_value(existing, "active_build_id"):
            return

        source_fingerprint, chunk_count = (
            self._knowledge_fingerprint_on_connection(
                conn,
                model_name=model_name,
                dimensions=dimension,
            )
        )
        build_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO embedding_profiles (
                profile_id, model_name, dimensions, status, created_at, updated_at
            )
            VALUES (%s, %s, %s, 'building', NOW(), NOW())
            ON CONFLICT (profile_id) DO NOTHING
            """,
            (profile_id, model_name, dimension),
        )
        conn.execute(
            """
            INSERT INTO embedding_profile_builds (
                build_id, profile_id, status, source_fingerprint,
                chunk_count, vector_count
            )
            VALUES (%s, %s, 'building', %s, %s, 0)
            """,
            (build_id, profile_id, source_fingerprint, chunk_count),
        )
        corpus = embedding_profile_corpus(model_name, dimension)
        corpus_predicate, corpus_params = self._embedding_corpus_predicate(
            corpus,
            metadata_expression="metadata",
        )
        cursor = conn.execute(
            f"""
            INSERT INTO kb_chunk_embeddings (
                build_id, profile_id, chunk_id, collection_name, scope, embedding
            )
            SELECT %s, %s, chunk_id, collection_name, scope, embedding
            FROM kb_chunks
            WHERE vector_dims(embedding) = %s
              AND {corpus_predicate}
            """,
            (build_id, profile_id, dimension, *corpus_params),
        )
        vector_count = max(0, int(cursor.rowcount or 0))
        if vector_count != chunk_count:
            raise RuntimeError(
                "Legacy embedding migration did not copy every knowledge chunk."
            )
        conn.execute(
            """
            UPDATE embedding_profile_builds
            SET status = 'ready', vector_count = %s, completed_at = NOW()
            WHERE build_id = %s
            """,
            (vector_count, build_id),
        )
        conn.execute(
            """
            UPDATE embedding_profiles
            SET active_build_id = %s,
                status = 'ready',
                source_fingerprint = %s,
                chunk_count = %s,
                vector_count = %s,
                last_error = NULL,
                updated_at = NOW()
            WHERE profile_id = %s
            """,
            (
                build_id,
                source_fingerprint,
                chunk_count,
                vector_count,
                profile_id,
            ),
        )

    def begin_embedding_profile_build(
        self,
        conn,
        *,
        model_name: str,
        dimensions: int,
    ) -> tuple[str, str]:
        normalized_model = normalize_embedding_model_name(model_name)
        normalized_dimensions = normalize_embedding_dimensions(dimensions)
        profile_id = embedding_profile_id(
            normalized_model,
            normalized_dimensions,
        )
        build_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO embedding_profiles (
                profile_id, model_name, dimensions, status, created_at, updated_at
            )
            VALUES (%s, %s, %s, 'building', NOW(), NOW())
            ON CONFLICT (profile_id) DO UPDATE SET
                model_name = excluded.model_name,
                dimensions = excluded.dimensions,
                updated_at = NOW()
            """,
            (profile_id, normalized_model, normalized_dimensions),
        )
        conn.execute(
            """
            INSERT INTO embedding_profile_builds (
                build_id, profile_id, status, created_at
            )
            VALUES (%s, %s, 'building', NOW())
            """,
            (build_id, profile_id),
        )
        return profile_id, build_id

    def copy_active_embedding_rows(
        self,
        conn,
        *,
        profile_id: str,
        build_id: str,
        excluded_doc_ids: set[str] | None = None,
    ) -> int:
        row = conn.execute(
            """
            SELECT active_build_id, model_name, dimensions
            FROM embedding_profiles
            WHERE profile_id = %s
            """,
            (profile_id,),
        ).fetchone()
        active_build_id = (
            str(self._row_value(row, "active_build_id"))
            if row and self._row_value(row, "active_build_id")
            else ""
        )
        if not active_build_id:
            return 0
        corpus = embedding_profile_corpus(
            str(self._row_value(row, "model_name")),
            int(self._row_value(row, "dimensions")),
        )
        corpus_predicate, corpus_params = self._embedding_corpus_predicate(
            corpus,
            metadata_expression="c.metadata",
        )
        excluded = sorted(
            {
                str(doc_id).strip()
                for doc_id in (excluded_doc_ids or set())
                if str(doc_id).strip()
            }
        )
        excluded_predicate = ""
        params: list[Any] = [
            build_id,
            profile_id,
            active_build_id,
            *corpus_params,
        ]
        if excluded:
            excluded_predicate = "AND c.doc_id <> ALL(%s::text[])"
            params.append(excluded)
        cursor = conn.execute(
            f"""
            INSERT INTO kb_chunk_embeddings (
                build_id, profile_id, chunk_id, collection_name, scope, embedding
            )
            SELECT %s, %s, e.chunk_id, e.collection_name, e.scope, e.embedding
            FROM kb_chunk_embeddings e
            JOIN kb_chunks c ON c.chunk_id = e.chunk_id
            WHERE e.build_id = %s
              AND {corpus_predicate}
              {excluded_predicate}
            ON CONFLICT (build_id, chunk_id) DO NOTHING
            """,
            tuple(params),
        )
        return max(0, int(cursor.rowcount or 0))

    def copy_active_retrieval_unit_embedding_rows(
        self,
        conn,
        *,
        profile_id: str,
        build_id: str,
        excluded_doc_ids: set[str] | None = None,
    ) -> int:
        row = conn.execute(
            """
            SELECT active_build_id, model_name, dimensions
            FROM embedding_profiles
            WHERE profile_id = %s
            """,
            (profile_id,),
        ).fetchone()
        active_build_id = (
            str(self._row_value(row, "active_build_id"))
            if row and self._row_value(row, "active_build_id")
            else ""
        )
        if not active_build_id:
            return 0
        corpus = embedding_profile_corpus(
            str(self._row_value(row, "model_name")),
            int(self._row_value(row, "dimensions")),
        )
        corpus_predicate, corpus_params = self._embedding_corpus_predicate(
            corpus,
            metadata_expression="c.metadata",
        )
        excluded = sorted(
            {
                str(doc_id).strip()
                for doc_id in (excluded_doc_ids or set())
                if str(doc_id).strip()
            }
        )
        excluded_predicate = ""
        params: list[Any] = [
            build_id,
            profile_id,
            active_build_id,
            *corpus_params,
        ]
        if excluded:
            excluded_predicate = "AND u.doc_id <> ALL(%s::text[])"
            params.append(excluded)
        cursor = conn.execute(
            f"""
            INSERT INTO kb_retrieval_unit_embeddings (
                build_id, profile_id, unit_id, collection_name, scope, embedding
            )
            SELECT %s, %s, e.unit_id, e.collection_name, e.scope, e.embedding
            FROM kb_retrieval_unit_embeddings e
            JOIN kb_retrieval_units u ON u.unit_id = e.unit_id
            JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
            WHERE e.build_id = %s
              AND {corpus_predicate}
              {excluded_predicate}
            ON CONFLICT (build_id, unit_id) DO NOTHING
            """,
            tuple(params),
        )
        return max(0, int(cursor.rowcount or 0))

    @staticmethod
    def _insert_retrieval_unit_exact_facts(
        conn,
        *,
        unit_id: str,
        parent_chunk_id: str,
        facts: list[dict[str, Any]],
    ) -> int:
        inserted = 0
        for fact in facts:
            fact_type = str(fact.get("fact_type") or "").strip()
            normalized_value = str(
                fact.get("normalized_value") or ""
            ).strip()
            source_quote = str(fact.get("source_quote") or "").strip()
            if not fact_type or not normalized_value or not source_quote:
                continue
            conn.execute(
                """
                INSERT INTO kb_retrieval_unit_exact_facts (
                    unit_id, parent_chunk_id, fact_type, normalized_value,
                    source_quote, document_frequency
                )
                VALUES (%s, %s, %s, %s, %s, 1)
                ON CONFLICT (unit_id, fact_type, normalized_value) DO UPDATE SET
                    parent_chunk_id = excluded.parent_chunk_id,
                    source_quote = excluded.source_quote,
                    document_frequency = excluded.document_frequency
                """,
                (
                    unit_id,
                    parent_chunk_id,
                    fact_type,
                    normalized_value,
                    source_quote,
                ),
            )
            inserted += 1
        return inserted

    @staticmethod
    def refresh_retrieval_unit_exact_fact_document_frequencies(conn) -> int:
        cursor = conn.execute(
            """
            WITH frequencies AS (
                SELECT
                    fact_type,
                    normalized_value,
                    COUNT(DISTINCT chunk_id)::integer
                        AS document_frequency
                FROM kb_exact_facts
                GROUP BY fact_type, normalized_value
            )
            UPDATE kb_retrieval_unit_exact_facts f
            SET document_frequency = frequencies.document_frequency
            FROM frequencies
            WHERE frequencies.fact_type = f.fact_type
              AND frequencies.normalized_value = f.normalized_value
              AND f.document_frequency
                    IS DISTINCT FROM frequencies.document_frequency
            """
        )
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))

    def _rebuild_exact_fact_indexes_on_connection(
        self,
        conn,
    ) -> dict[str, int]:
        conn.execute("DELETE FROM kb_retrieval_unit_exact_facts")
        unit_fact_count = 0
        unit_rows = conn.execute(
            """
            SELECT unit_id, parent_chunk_id, scope, text, metadata
            FROM kb_retrieval_units
            ORDER BY unit_id
            """
        ).fetchall()
        for row in unit_rows:
            metadata = self._json_mapping(
                self._row_value(row, "metadata")
            )
            metadata.setdefault(
                "scope",
                str(self._row_value(row, "scope") or ""),
            )
            facts = extract_exact_facts(
                contextual_exact_fact_text(
                    str(self._row_value(row, "text") or ""),
                    metadata=metadata,
                ),
                metadata=metadata,
            )
            unit_fact_count += self._insert_retrieval_unit_exact_facts(
                conn,
                unit_id=str(self._row_value(row, "unit_id") or ""),
                parent_chunk_id=str(
                    self._row_value(row, "parent_chunk_id") or ""
                ),
                facts=facts,
            )

        parent_fact_count = 0
        chunk_rows = conn.execute(
            """
            SELECT
                chunk_id, doc_id, scope, text, metadata, index_version
            FROM kb_chunks
            ORDER BY chunk_id
            """
        ).fetchall()
        for row in chunk_rows:
            chunk_id = str(self._row_value(row, "chunk_id") or "")
            metadata = self._json_mapping(
                self._row_value(row, "metadata")
            )
            metadata.setdefault(
                "scope",
                str(self._row_value(row, "scope") or ""),
            )
            facts = extract_exact_facts(
                str(self._row_value(row, "text") or ""),
                metadata=metadata,
            )
            for fact in facts:
                fact_id = hashlib.sha256(
                    "\x00".join(
                        (
                            chunk_id,
                            str(fact.get("fact_type") or ""),
                            str(fact.get("normalized_value") or ""),
                            str(fact.get("source_quote") or ""),
                        )
                    ).encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """
                    INSERT INTO kb_exact_facts (
                        fact_id, chunk_id, doc_id, section_id, scope,
                        index_version, fact_type, normalized_value,
                        display_value, source_quote, source_start_line,
                        source_end_line, metadata
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, '{}'::jsonb
                    )
                    ON CONFLICT (fact_id) DO NOTHING
                    """,
                    (
                        fact_id,
                        chunk_id,
                        str(self._row_value(row, "doc_id") or ""),
                        metadata.get("parent_section_id"),
                        str(self._row_value(row, "scope") or ""),
                        str(
                            self._row_value(row, "index_version") or ""
                        ),
                        fact.get("fact_type"),
                        fact.get("normalized_value"),
                        fact.get("display_value"),
                        fact.get("source_quote"),
                        metadata.get("source_start_line"),
                        metadata.get("source_end_line"),
                    ),
                )
                parent_fact_count += 1

        self.refresh_retrieval_unit_exact_fact_document_frequencies(conn)
        return {
            "parent_facts": parent_fact_count,
            "retrieval_unit_facts": unit_fact_count,
        }

    def replace_retrieval_units(
        self,
        conn,
        *,
        retrieval_units: list[dict[str, Any]],
        refresh_document_frequencies: bool = True,
    ) -> int:
        if not retrieval_units:
            return 0
        parent_chunk_ids = sorted(
            {
                str(unit.get("parent_chunk_id") or "").strip()
                for unit in retrieval_units
                if str(unit.get("parent_chunk_id") or "").strip()
            }
        )
        if parent_chunk_ids:
            conn.execute(
                "DELETE FROM kb_retrieval_units "
                "WHERE parent_chunk_id = ANY(%s::text[])",
                (parent_chunk_ids,),
            )
        for unit in retrieval_units:
            conn.execute(
                """
                INSERT INTO kb_retrieval_units (
                    unit_id, parent_chunk_id, doc_id, collection_name, scope,
                    unit_type, unit_index, text, search_text, metadata,
                    index_version, content_hash, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    %s, %s, NOW()
                )
                ON CONFLICT (unit_id) DO UPDATE SET
                    parent_chunk_id = excluded.parent_chunk_id,
                    doc_id = excluded.doc_id,
                    collection_name = excluded.collection_name,
                    scope = excluded.scope,
                    unit_type = excluded.unit_type,
                    unit_index = excluded.unit_index,
                    text = excluded.text,
                    search_text = excluded.search_text,
                    metadata = excluded.metadata,
                    index_version = excluded.index_version,
                    content_hash = excluded.content_hash,
                    updated_at = NOW()
                """,
                (
                    str(unit["unit_id"]),
                    str(unit["parent_chunk_id"]),
                    str(unit["doc_id"]),
                    str(unit["collection_name"]),
                    str(unit["scope"]),
                    str(unit["unit_type"]),
                    int(unit["unit_index"]),
                    str(unit["text"]),
                    str(unit["search_text"]),
                    json.dumps(unit.get("metadata") or {}, ensure_ascii=False),
                    str(unit["index_version"]),
                    str(unit.get("content_hash") or ""),
                ),
            )
            self._insert_retrieval_unit_exact_facts(
                conn,
                unit_id=str(unit["unit_id"]),
                parent_chunk_id=str(unit["parent_chunk_id"]),
                facts=list(unit.get("exact_facts") or []),
            )
        if refresh_document_frequencies:
            self.refresh_retrieval_unit_exact_fact_document_frequencies(
                conn
            )
        return len(retrieval_units)

    def upsert_retrieval_unit_embedding_rows(
        self,
        conn,
        *,
        profile_id: str,
        build_id: str,
        retrieval_units: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> int:
        if len(retrieval_units) != len(embeddings):
            raise ValueError(
                "retrieval_units and embeddings length mismatch"
            )
        pairs = list(zip(retrieval_units, embeddings, strict=True))
        corpus = self._profile_corpus_on_connection(conn, profile_id)
        if corpus == "kb_raw" and pairs:
            parent_ids = sorted(
                {
                    str(unit["parent_chunk_id"])
                    for unit, _ in pairs
                }
            )
            predicate, predicate_params = self._embedding_corpus_predicate(
                corpus,
                metadata_expression="c.metadata",
            )
            allowed_rows = conn.execute(
                f"""
                SELECT c.chunk_id
                FROM kb_chunks c
                WHERE c.chunk_id = ANY(%s::text[])
                  AND {predicate}
                """,
                (parent_ids, *predicate_params),
            ).fetchall()
            allowed_parent_ids = {
                str(self._row_value(row, "chunk_id"))
                for row in allowed_rows
            }
            pairs = [
                (unit, embedding)
                for unit, embedding in pairs
                if str(unit["parent_chunk_id"]) in allowed_parent_ids
            ]
        for unit, embedding in pairs:
            conn.execute(
                """
                INSERT INTO kb_retrieval_unit_embeddings (
                    build_id, profile_id, unit_id, collection_name, scope,
                    embedding, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::vector, NOW())
                ON CONFLICT (build_id, unit_id) DO UPDATE SET
                    collection_name = excluded.collection_name,
                    scope = excluded.scope,
                    embedding = excluded.embedding,
                    updated_at = NOW()
                """,
                (
                    build_id,
                    profile_id,
                    str(unit["unit_id"]),
                    str(unit["collection_name"]),
                    str(unit["scope"]),
                    self.vector_literal(embedding),
                ),
            )
        return len(pairs)

    def upsert_embedding_rows(
        self,
        conn,
        *,
        profile_id: str,
        build_id: str,
        collection_name: str,
        chunks: list[Any],
        embeddings: list[list[float]],
    ) -> int:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        corpus = self._profile_corpus_on_connection(conn, profile_id)
        pairs = [
            (chunk, embedding)
            for chunk, embedding in zip(chunks, embeddings, strict=True)
            if embedding_metadata_in_corpus(
                getattr(chunk, "metadata", None),
                corpus,
            )
        ]
        for chunk, embedding in pairs:
            conn.execute(
                """
                INSERT INTO kb_chunk_embeddings (
                    build_id, profile_id, chunk_id, collection_name, scope,
                    embedding, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::vector, NOW())
                ON CONFLICT (build_id, chunk_id) DO UPDATE SET
                    collection_name = excluded.collection_name,
                    scope = excluded.scope,
                    embedding = excluded.embedding,
                    updated_at = NOW()
                """,
                (
                    build_id,
                    profile_id,
                    str(chunk.chunk_id),
                    collection_name,
                    str(chunk.scope),
                    self.vector_literal(embedding),
                ),
            )
        return len(pairs)

    def activate_embedding_profile_build(
        self,
        conn,
        *,
        profile_id: str,
        build_id: str,
    ) -> EmbeddingProfileState:
        profile_row = conn.execute(
            """
            SELECT model_name, dimensions
            FROM embedding_profiles
            WHERE profile_id = %s
            """,
            (profile_id,),
        ).fetchone()
        if not profile_row:
            raise RuntimeError(f"Embedding profile does not exist: {profile_id}")
        model_name = str(self._row_value(profile_row, "model_name"))
        dimensions = int(self._row_value(profile_row, "dimensions"))
        source_fingerprint, chunk_count = (
            self._knowledge_fingerprint_on_connection(
                conn,
                model_name=model_name,
                dimensions=dimensions,
            )
        )
        count_row = conn.execute(
            """
            SELECT
                COUNT(*) AS vector_count,
                COUNT(*) FILTER (
                    WHERE vector_dims(embedding) = %s
                ) AS matching_dimension_count
            FROM kb_chunk_embeddings
            WHERE profile_id = %s AND build_id = %s
            """,
            (dimensions, profile_id, build_id),
        ).fetchone()
        vector_count = int(self._row_value(count_row, "vector_count") or 0)
        matching_count = int(
            self._row_value(count_row, "matching_dimension_count") or 0
        )
        if vector_count != chunk_count or matching_count != chunk_count:
            raise RuntimeError(
                "Embedding profile build is incomplete: "
                f"profile={profile_id}, chunks={chunk_count}, "
                f"vectors={vector_count}, matching_dimensions={matching_count}."
            )

        corpus = embedding_profile_corpus(model_name, dimensions)
        unit_predicate, unit_params = self._embedding_corpus_predicate(
            corpus,
            metadata_expression="c.metadata",
        )
        unit_count_row = conn.execute(
            f"""
            SELECT COUNT(*) AS unit_count
            FROM kb_retrieval_units u
            JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
            WHERE {unit_predicate}
            """,
            unit_params,
        ).fetchone()
        unit_count = int(
            self._row_value(unit_count_row, "unit_count") or 0
        )
        unit_vector_row = conn.execute(
            """
            SELECT
                COUNT(*) AS vector_count,
                COUNT(*) FILTER (
                    WHERE vector_dims(embedding) = %s
                ) AS matching_dimension_count
            FROM kb_retrieval_unit_embeddings
            WHERE profile_id = %s AND build_id = %s
            """,
            (dimensions, profile_id, build_id),
        ).fetchone()
        unit_vector_count = int(
            self._row_value(unit_vector_row, "vector_count") or 0
        )
        unit_matching_count = int(
            self._row_value(
                unit_vector_row,
                "matching_dimension_count",
            )
            or 0
        )
        if (
            unit_vector_count != unit_count
            or unit_matching_count != unit_count
        ):
            raise RuntimeError(
                "Embedding profile retrieval-unit build is incomplete: "
                f"profile={profile_id}, units={unit_count}, "
                f"vectors={unit_vector_count}, "
                f"matching_dimensions={unit_matching_count}."
            )
        conn.execute(
            """
            UPDATE embedding_profile_builds
            SET status = 'ready',
                source_fingerprint = %s,
                chunk_count = %s,
                vector_count = %s,
                error = NULL,
                completed_at = NOW()
            WHERE build_id = %s AND profile_id = %s
            """,
            (
                source_fingerprint,
                chunk_count,
                vector_count,
                build_id,
                profile_id,
            ),
        )
        conn.execute(
            """
            UPDATE embedding_profiles
            SET active_build_id = %s,
                status = 'ready',
                source_fingerprint = %s,
                chunk_count = %s,
                vector_count = %s,
                last_error = NULL,
                updated_at = NOW()
            WHERE profile_id = %s
            """,
            (
                build_id,
                source_fingerprint,
                chunk_count,
                vector_count,
                profile_id,
            ),
        )

        other_profiles = conn.execute(
            """
            SELECT profile_id, model_name, dimensions, source_fingerprint
            FROM embedding_profiles
            WHERE profile_id <> %s AND active_build_id IS NOT NULL
            """,
            (profile_id,),
        ).fetchall()
        for other in other_profiles:
            other_model = str(self._row_value(other, "model_name"))
            other_dimensions = int(self._row_value(other, "dimensions"))
            expected_fingerprint, _ = self._knowledge_fingerprint_on_connection(
                conn,
                model_name=other_model,
                dimensions=other_dimensions,
            )
            if str(self._row_value(other, "source_fingerprint") or "") != (
                expected_fingerprint
            ):
                conn.execute(
                    """
                    UPDATE embedding_profiles
                    SET status = 'stale', updated_at = NOW()
                    WHERE profile_id = %s
                    """,
                    (str(self._row_value(other, "profile_id")),),
                )
        return EmbeddingProfileState(
            profile_id=profile_id,
            model_name=model_name,
            dimensions=dimensions,
            status="ready",
            active_build_id=build_id,
            source_fingerprint=source_fingerprint,
            chunk_count=chunk_count,
            vector_count=vector_count,
            current_chunk_count=chunk_count,
        )

    def _embedding_profile_state_on_connection(
        self,
        conn,
        *,
        model_name: str,
        dimensions: int,
    ) -> EmbeddingProfileState:
        normalized_model = normalize_embedding_model_name(model_name)
        normalized_dimensions = normalize_embedding_dimensions(dimensions)
        profile_id = embedding_profile_id(
            normalized_model,
            normalized_dimensions,
        )
        source_fingerprint, current_chunk_count = (
            self._knowledge_fingerprint_on_connection(
                conn,
                model_name=normalized_model,
                dimensions=normalized_dimensions,
            )
        )
        row = conn.execute(
            """
            SELECT p.status, p.active_build_id, p.source_fingerprint,
                   p.chunk_count, p.vector_count,
                   COALESCE(actual.vector_count, 0) AS actual_vector_count,
                   COALESCE(actual.matching_dimension_count, 0)
                       AS matching_dimension_count
            FROM embedding_profiles p
            LEFT JOIN LATERAL (
                SELECT
                    COUNT(*) AS vector_count,
                    COUNT(*) FILTER (
                        WHERE vector_dims(e.embedding) = p.dimensions
                    ) AS matching_dimension_count
                FROM kb_chunk_embeddings e
                WHERE e.profile_id = p.profile_id
                  AND e.build_id = p.active_build_id
            ) actual ON TRUE
            WHERE p.profile_id = %s
              AND p.model_name = %s
              AND p.dimensions = %s
            """,
            (profile_id, normalized_model, normalized_dimensions),
        ).fetchone()
        if not row:
            return EmbeddingProfileState(
                profile_id=profile_id,
                model_name=normalized_model,
                dimensions=normalized_dimensions,
                status="missing",
                active_build_id=None,
                source_fingerprint="",
                chunk_count=0,
                vector_count=0,
                current_chunk_count=current_chunk_count,
                stale_reason="profile_missing",
            )

        active_build_id = self._row_value(row, "active_build_id")
        stored_fingerprint = str(
            self._row_value(row, "source_fingerprint") or ""
        )
        stored_chunk_count = int(
            self._row_value(row, "chunk_count") or 0
        )
        actual_vector_count = int(
            self._row_value(row, "actual_vector_count") or 0
        )
        matching_count = int(
            self._row_value(row, "matching_dimension_count") or 0
        )
        corpus = embedding_profile_corpus(
            normalized_model,
            normalized_dimensions,
        )
        unit_predicate, unit_params = self._embedding_corpus_predicate(
            corpus,
            metadata_expression="c.metadata",
        )
        unit_count_row = conn.execute(
            f"""
            SELECT COUNT(*) AS unit_count
            FROM kb_retrieval_units u
            JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
            WHERE {unit_predicate}
            """,
            unit_params,
        ).fetchone()
        current_unit_count = int(
            self._row_value(unit_count_row, "unit_count") or 0
        )
        unit_vector_row = conn.execute(
            """
            SELECT
                COUNT(*) AS vector_count,
                COUNT(*) FILTER (
                    WHERE vector_dims(embedding) = %s
                ) AS matching_dimension_count
            FROM kb_retrieval_unit_embeddings
            WHERE profile_id = %s AND build_id = %s
            """,
            (
                normalized_dimensions,
                profile_id,
                active_build_id,
            ),
        ).fetchone()
        unit_vector_count = int(
            self._row_value(unit_vector_row, "vector_count") or 0
        )
        unit_matching_count = int(
            self._row_value(
                unit_vector_row,
                "matching_dimension_count",
            )
            or 0
        )
        status = str(self._row_value(row, "status") or "missing")
        stale_reason: str | None = None
        if not active_build_id:
            stale_reason = "active_build_missing"
        elif stored_fingerprint != source_fingerprint:
            stale_reason = "knowledge_fingerprint_changed"
        elif stored_chunk_count != current_chunk_count:
            stale_reason = "chunk_count_changed"
        elif actual_vector_count != current_chunk_count:
            stale_reason = "vector_count_incomplete"
        elif matching_count != current_chunk_count:
            stale_reason = "vector_dimension_mismatch"
        elif unit_vector_count != current_unit_count:
            stale_reason = "retrieval_unit_vector_count_incomplete"
        elif unit_matching_count != current_unit_count:
            stale_reason = "retrieval_unit_vector_dimension_mismatch"
        elif status != "ready":
            stale_reason = f"profile_status_{status}"
        if stale_reason and status == "ready":
            status = "stale"
        return EmbeddingProfileState(
            profile_id=profile_id,
            model_name=normalized_model,
            dimensions=normalized_dimensions,
            status=status,
            active_build_id=(
                str(active_build_id) if active_build_id else None
            ),
            source_fingerprint=stored_fingerprint,
            chunk_count=stored_chunk_count,
            vector_count=actual_vector_count,
            current_chunk_count=current_chunk_count,
            stale_reason=stale_reason,
        )

    def embedding_profile_state(
        self,
        model_name: str,
        dimensions: int,
    ) -> EmbeddingProfileState:
        with self.connection() as conn:
            return self._embedding_profile_state_on_connection(
                conn,
                model_name=model_name,
                dimensions=dimensions,
            )

    def active_embedding_build_id(
        self,
        model_name: str,
        dimensions: int,
    ) -> str | None:
        """Return the ready build id without validating every vector row.

        Runtime workers use this as a cheap activation probe. A changed id is
        still followed by ``require_active_embedding_profile`` before the new
        build is accepted, so this method never replaces the full integrity
        check performed at startup or during a build switch.
        """

        normalized_model = normalize_embedding_model_name(model_name)
        normalized_dimensions = normalize_embedding_dimensions(dimensions)
        profile_id = embedding_profile_id(
            normalized_model,
            normalized_dimensions,
        )
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT active_build_id
                FROM embedding_profiles
                WHERE profile_id = %s
                  AND model_name = %s
                  AND dimensions = %s
                  AND status = 'ready'
                  AND active_build_id IS NOT NULL
                """,
                (profile_id, normalized_model, normalized_dimensions),
            ).fetchone()
        active_build_id = (
            self._row_value(row, "active_build_id") if row else None
        )
        return str(active_build_id) if active_build_id else None

    def require_active_embedding_profile(
        self,
        model_name: str,
        dimensions: int,
    ) -> EmbeddingProfileState:
        state = self.embedding_profile_state(model_name, dimensions)
        if not state.ready:
            raise RuntimeError(
                "The configured embedding vector repository is unavailable or "
                "stale: "
                f"model={state.model_name}, dimensions={state.dimensions}, "
                f"profile_id={state.profile_id}, status={state.status}, "
                f"reason={state.stale_reason}. Build and verify this profile "
                "before starting the backend."
            )
        return state

    def list_embedding_profiles(self) -> list[EmbeddingProfileState]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT model_name, dimensions
                FROM embedding_profiles
                ORDER BY model_name, dimensions
                """
            ).fetchall()
            return [
                self._embedding_profile_state_on_connection(
                    conn,
                    model_name=str(self._row_value(row, "model_name")),
                    dimensions=int(self._row_value(row, "dimensions")),
                )
                for row in rows
            ]

    def fetch_embedding_source_chunks(
        self,
        *,
        model_name: str,
        dimensions: int,
    ) -> list[dict[str, Any]]:
        corpus = embedding_profile_corpus(model_name, dimensions)
        predicate, params = self._embedding_corpus_predicate(
            corpus,
            metadata_expression="c.metadata",
        )
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.collection_name, c.doc_id, c.source_id,
                       c.title, c.scope, c.text, c.search_text, c.metadata,
                       c.index_version, c.content_hash
                FROM kb_chunks c
                WHERE {predicate}
                ORDER BY c.chunk_id
                """,
                params,
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            metadata = self._row_value(row, "metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            output.append(
                {
                    "chunk_id": str(self._row_value(row, "chunk_id")),
                    "collection_name": str(
                        self._row_value(row, "collection_name")
                    ),
                    "doc_id": str(self._row_value(row, "doc_id")),
                    "source_id": str(self._row_value(row, "source_id")),
                    "title": str(self._row_value(row, "title")),
                    "scope": str(self._row_value(row, "scope")),
                    "text": str(self._row_value(row, "text")),
                    "search_text": str(
                        self._row_value(row, "search_text")
                        or self._row_value(row, "text")
                    ),
                    "metadata": dict(metadata),
                    "index_version": str(
                        self._row_value(row, "index_version")
                    ),
                    "content_hash": str(
                        self._row_value(row, "content_hash") or ""
                    ),
                }
            )
        return output

    def fetch_embedding_source_retrieval_units(
        self,
        *,
        model_name: str,
        dimensions: int,
    ) -> list[dict[str, Any]]:
        corpus = embedding_profile_corpus(model_name, dimensions)
        predicate, params = self._embedding_corpus_predicate(
            corpus,
            metadata_expression="c.metadata",
        )
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT u.unit_id, u.parent_chunk_id, u.doc_id,
                       u.collection_name, u.scope, u.unit_type, u.unit_index,
                       u.text, u.search_text, u.metadata, u.index_version,
                       u.content_hash
                FROM kb_retrieval_units u
                JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
                WHERE {predicate}
                ORDER BY u.unit_id
                """,
                params,
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            metadata = self._row_value(row, "metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            output.append(
                {
                    "unit_id": str(self._row_value(row, "unit_id")),
                    "parent_chunk_id": str(
                        self._row_value(row, "parent_chunk_id")
                    ),
                    "doc_id": str(self._row_value(row, "doc_id")),
                    "collection_name": str(
                        self._row_value(row, "collection_name")
                    ),
                    "scope": str(self._row_value(row, "scope")),
                    "unit_type": str(self._row_value(row, "unit_type")),
                    "unit_index": int(
                        self._row_value(row, "unit_index") or 0
                    ),
                    "text": str(self._row_value(row, "text")),
                    "search_text": str(
                        self._row_value(row, "search_text")
                        or self._row_value(row, "text")
                    ),
                    "metadata": dict(metadata),
                    "index_version": str(
                        self._row_value(row, "index_version")
                    ),
                    "content_hash": str(
                        self._row_value(row, "content_hash") or ""
                    ),
                }
            )
        return output

    def fetch_active_embedding_source_ids(
        self,
        *,
        model_name: str,
        dimensions: int,
    ) -> tuple[set[str], set[str]]:
        normalized_model = normalize_embedding_model_name(model_name)
        normalized_dimensions = normalize_embedding_dimensions(dimensions)
        profile_id = embedding_profile_id(
            normalized_model,
            normalized_dimensions,
        )
        corpus = embedding_profile_corpus(
            normalized_model,
            normalized_dimensions,
        )
        predicate, params = self._embedding_corpus_predicate(
            corpus,
            metadata_expression="c.metadata",
        )
        with self.connection() as conn:
            profile_row = conn.execute(
                """
                SELECT active_build_id
                FROM embedding_profiles
                WHERE profile_id = %s
                """,
                (profile_id,),
            ).fetchone()
            active_build_id = (
                str(self._row_value(profile_row, "active_build_id"))
                if profile_row
                and self._row_value(profile_row, "active_build_id")
                else ""
            )
            if not active_build_id:
                return set(), set()
            chunk_rows = conn.execute(
                f"""
                SELECT e.chunk_id
                FROM kb_chunk_embeddings e
                JOIN kb_chunks c ON c.chunk_id = e.chunk_id
                WHERE e.profile_id = %s
                  AND e.build_id = %s
                  AND vector_dims(e.embedding) = %s
                  AND {predicate}
                """,
                (
                    profile_id,
                    active_build_id,
                    normalized_dimensions,
                    *params,
                ),
            ).fetchall()
            unit_rows = conn.execute(
                f"""
                SELECT e.unit_id
                FROM kb_retrieval_unit_embeddings e
                JOIN kb_retrieval_units u ON u.unit_id = e.unit_id
                JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
                WHERE e.profile_id = %s
                  AND e.build_id = %s
                  AND vector_dims(e.embedding) = %s
                  AND {predicate}
                """,
                (
                    profile_id,
                    active_build_id,
                    normalized_dimensions,
                    *params,
                ),
            ).fetchall()
        return (
            {
                str(self._row_value(row, "chunk_id"))
                for row in chunk_rows
            },
            {
                str(self._row_value(row, "unit_id"))
                for row in unit_rows
            },
        )

    def clean_embedding_profile(
        self,
        model_name: str,
        dimensions: int,
        *,
        remove_profile: bool = False,
        allow_active_removal: bool = False,
    ) -> dict[str, Any]:
        profile_id = embedding_profile_id(model_name, dimensions)
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT active_build_id
                FROM embedding_profiles
                WHERE profile_id = %s
                FOR UPDATE
                """,
                (profile_id,),
            ).fetchone()
            if not row:
                return {
                    "profile_id": profile_id,
                    "removed_builds": 0,
                    "removed_profile": False,
                }
            active_build_id = self._row_value(row, "active_build_id")
            cursor = conn.execute(
                """
                DELETE FROM embedding_profile_builds
                WHERE profile_id = %s
                  AND (
                        %s::uuid IS NULL
                        OR build_id <> %s::uuid
                      )
                """,
                (profile_id, active_build_id, active_build_id),
            )
            removed_builds = max(0, int(cursor.rowcount or 0))
            removed_profile = False
            if remove_profile:
                current_profile_id = embedding_profile_id(
                    self.settings.effective_embedding_model,
                    resolve_embedding_dimensions(self.settings),
                )
                if (
                    profile_id == current_profile_id
                    and active_build_id
                    and not allow_active_removal
                ):
                    raise RuntimeError(
                        "Refusing to remove the configured active embedding "
                        "profile without explicit force."
                    )
                delete_cursor = conn.execute(
                    "DELETE FROM embedding_profiles WHERE profile_id = %s",
                    (profile_id,),
                )
                removed_profile = bool(delete_cursor.rowcount)
        return {
            "profile_id": profile_id,
            "removed_builds": removed_builds,
            "removed_profile": removed_profile,
        }

    @staticmethod
    def _index_digest(*parts: object) -> str:
        raw = "\x00".join(str(part) for part in parts).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:12]

    @classmethod
    def hnsw_index_name(
        cls,
        collection_name: str,
        scope: str,
        dimension: int,
        *,
        profile_id: str | None = None,
        build_id: str | None = None,
    ) -> str:
        if profile_id and build_id:
            digest = cls._index_digest(
                profile_id,
                build_id,
                collection_name,
                scope,
                int(dimension),
            )
            return f"idx_kb_profile_hnsw_{digest}_{int(dimension)}"
        digest = cls._index_digest(collection_name, scope, int(dimension))
        return f"idx_kb_hnsw_{digest}_{int(dimension)}"

    @classmethod
    def retrieval_unit_hnsw_index_name(
        cls,
        collection_name: str,
        scope: str,
        dimension: int,
        *,
        profile_id: str | None = None,
        build_id: str | None = None,
    ) -> str:
        if profile_id and build_id:
            digest = cls._index_digest(
                "retrieval_unit",
                profile_id,
                build_id,
                collection_name,
                scope,
                int(dimension),
            )
            return f"idx_kb_unit_profile_hnsw_{digest}_{int(dimension)}"
        digest = cls._index_digest(
            "retrieval_unit",
            collection_name,
            scope,
            int(dimension),
        )
        return f"idx_kb_unit_hnsw_{digest}_{int(dimension)}"

    def _ann_min_rows_for_scope(self, scope: str) -> int:
        force_scopes = {
            item.strip().lower()
            for item in str(
                getattr(self.settings, "postgres_ann_force_scopes", "")
            ).split(",")
            if item.strip()
        }
        if "*" in force_scopes or str(scope).strip().lower() in force_scopes:
            return 1
        return int(self.settings.postgres_ann_min_rows)

    @classmethod
    def bm25_index_name(cls, collection_name: str, scope: str) -> str:
        return f"idx_kb_bm25_{cls._index_digest(collection_name, scope)}"

    @staticmethod
    def retrieval_unit_bm25_index_name() -> str:
        return "idx_kb_retrieval_units_bm25"

    def retrieval_index_specs(
        self,
        profile: EmbeddingProfileState | None = None,
    ) -> list[dict[str, Any]]:
        profile = profile or self.require_active_embedding_profile(
            self.settings.effective_embedding_model,
            resolve_embedding_dimensions(self.settings),
        )
        if not profile.active_build_id:
            return []
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.collection_name,
                    e.scope,
                    vector_dims(e.embedding) AS dimension,
                    COUNT(*) AS row_count
                FROM kb_chunk_embeddings e
                WHERE e.profile_id = %s AND e.build_id = %s
                GROUP BY e.collection_name, e.scope, vector_dims(e.embedding)
                ORDER BY e.collection_name, e.scope, vector_dims(e.embedding)
                """,
                (profile.profile_id, profile.active_build_id),
            ).fetchall()
        return [
            {
                "profile_id": profile.profile_id,
                "build_id": profile.active_build_id,
                "collection_name": str(
                    self._row_value(row, "collection_name")
                ),
                "scope": str(self._row_value(row, "scope")),
                "dimension": int(self._row_value(row, "dimension")),
                "row_count": int(self._row_value(row, "row_count") or 0),
            }
            for row in rows
        ]

    def retrieval_unit_index_specs(
        self,
        profile: EmbeddingProfileState | None = None,
    ) -> list[dict[str, Any]]:
        profile = profile or self.require_active_embedding_profile(
            self.settings.effective_embedding_model,
            resolve_embedding_dimensions(self.settings),
        )
        if not profile.active_build_id:
            return []
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.collection_name,
                    e.scope,
                    vector_dims(e.embedding) AS dimension,
                    COUNT(*) AS row_count
                FROM kb_retrieval_unit_embeddings e
                WHERE e.profile_id = %s AND e.build_id = %s
                GROUP BY e.collection_name, e.scope, vector_dims(e.embedding)
                ORDER BY e.collection_name, e.scope, vector_dims(e.embedding)
                """,
                (profile.profile_id, profile.active_build_id),
            ).fetchall()
        return [
            {
                "profile_id": profile.profile_id,
                "build_id": profile.active_build_id,
                "collection_name": str(
                    self._row_value(row, "collection_name")
                ),
                "scope": str(self._row_value(row, "scope")),
                "dimension": int(self._row_value(row, "dimension")),
                "row_count": int(self._row_value(row, "row_count") or 0),
            }
            for row in rows
        ]

    def retrieval_capability_snapshot(
        self,
        profile: EmbeddingProfileState | None = None,
    ) -> dict[tuple[str, str], RetrievalScopeCapability]:
        profile = profile or self.require_active_embedding_profile(
            self.settings.effective_embedding_model,
            resolve_embedding_dimensions(self.settings),
        )
        if not profile.active_build_id:
            return {}
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.collection_name,
                    c.scope,
                    vector_dims(e.embedding) AS dimension,
                    COUNT(*) AS row_count,
                    COUNT(c.bm25_embedding) AS bm25_count
                FROM kb_chunk_embeddings e
                JOIN kb_chunks c ON c.chunk_id = e.chunk_id
                WHERE e.profile_id = %s AND e.build_id = %s
                GROUP BY c.collection_name, c.scope, vector_dims(e.embedding)
                ORDER BY c.collection_name, c.scope, vector_dims(e.embedding)
                """,
                (profile.profile_id, profile.active_build_id),
            ).fetchall()
            unit_rows = conn.execute(
                """
                SELECT
                    e.collection_name,
                    e.scope,
                    vector_dims(e.embedding) AS dimension,
                    COUNT(*) AS row_count
                FROM kb_retrieval_unit_embeddings e
                WHERE e.profile_id = %s AND e.build_id = %s
                GROUP BY e.collection_name, e.scope, vector_dims(e.embedding)
                ORDER BY e.collection_name, e.scope, vector_dims(e.embedding)
                """,
                (profile.profile_id, profile.active_build_id),
            ).fetchall()
            index_rows = conn.execute(
                """
                SELECT c.relname AS index_name
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_index i ON i.indexrelid = c.oid
                WHERE n.nspname = 'public'
                  AND i.indisvalid
                  AND c.relname LIKE ANY(%s)
                """,
                (
                    [
                        "idx_kb_profile_hnsw_%",
                        "idx_kb_unit_profile_hnsw_%",
                        "idx_kb_bm25_%",
                        self.retrieval_unit_bm25_index_name(),
                    ],
                ),
            ).fetchall()

        valid_indexes = {
            str(self._row_value(row, "index_name")) for row in index_rows
        }
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            collection_name = str(self._row_value(row, "collection_name"))
            scope = str(self._row_value(row, "scope"))
            dimension = int(self._row_value(row, "dimension"))
            row_count = int(self._row_value(row, "row_count") or 0)
            bm25_count = int(self._row_value(row, "bm25_count") or 0)
            key = (collection_name, scope)
            state = grouped.setdefault(
                key,
                {
                    "row_count": 0,
                    "bm25_count": 0,
                    "unit_row_count": 0,
                    "ann_by_dimension": {},
                },
            )
            state["row_count"] += row_count
            state["bm25_count"] += bm25_count
            ann_state = state["ann_by_dimension"].setdefault(
                dimension,
                {
                    "chunk_rows": 0,
                    "unit_rows": 0,
                    "chunk_index_ready": False,
                    "unit_index_ready": False,
                },
            )
            ann_state["chunk_rows"] += row_count
            hnsw_name = self.hnsw_index_name(
                collection_name,
                scope,
                dimension,
                profile_id=profile.profile_id,
                build_id=profile.active_build_id,
            )
            ann_state["chunk_index_ready"] = hnsw_name in valid_indexes

        for row in unit_rows:
            collection_name = str(self._row_value(row, "collection_name"))
            scope = str(self._row_value(row, "scope"))
            dimension = int(self._row_value(row, "dimension"))
            row_count = int(self._row_value(row, "row_count") or 0)
            key = (collection_name, scope)
            state = grouped.setdefault(
                key,
                {
                    "row_count": 0,
                    "bm25_count": 0,
                    "unit_row_count": 0,
                    "ann_by_dimension": {},
                },
            )
            state["unit_row_count"] += row_count
            ann_state = state["ann_by_dimension"].setdefault(
                dimension,
                {
                    "chunk_rows": 0,
                    "unit_rows": 0,
                    "chunk_index_ready": False,
                    "unit_index_ready": False,
                },
            )
            ann_state["unit_rows"] += row_count
            unit_hnsw_name = self.retrieval_unit_hnsw_index_name(
                collection_name,
                scope,
                dimension,
                profile_id=profile.profile_id,
                build_id=profile.active_build_id,
            )
            ann_state["unit_index_ready"] = unit_hnsw_name in valid_indexes

        snapshot: dict[tuple[str, str], RetrievalScopeCapability] = {}
        for (collection_name, scope), state in grouped.items():
            row_count = int(state["row_count"])
            unit_row_count = int(state["unit_row_count"])
            bm25_name = self.bm25_index_name(collection_name, scope)
            retrieval_unit_bm25_ready = bool(
                unit_row_count == 0
                or self.retrieval_unit_bm25_index_name() in valid_indexes
            )
            parent_bm25_ready = bool(
                row_count == 0
                or (
                    int(state["bm25_count"]) == row_count
                    and bm25_name in valid_indexes
                )
            )
            ann_threshold = self._ann_min_rows_for_scope(scope)
            ann_dimensions = {
                int(dimension)
                for dimension, ann_state in state[
                    "ann_by_dimension"
                ].items()
                if (
                    (
                        int(ann_state["chunk_rows"]) >= ann_threshold
                        or int(ann_state["unit_rows"]) >= ann_threshold
                    )
                    and (
                        int(ann_state["chunk_rows"]) < ann_threshold
                        or bool(ann_state["chunk_index_ready"])
                    )
                    and (
                        int(ann_state["unit_rows"]) < ann_threshold
                        or bool(ann_state["unit_index_ready"])
                    )
                )
            }
            ann_required_dimensions = {
                int(dimension)
                for dimension, ann_state in state[
                    "ann_by_dimension"
                ].items()
                if (
                    int(ann_state["chunk_rows"]) >= ann_threshold
                    or int(ann_state["unit_rows"]) >= ann_threshold
                )
            }
            snapshot[(collection_name, scope)] = RetrievalScopeCapability(
                collection_name=collection_name,
                scope=scope,
                row_count=row_count,
                bm25_ready=(
                    bool(row_count or unit_row_count)
                    and parent_bm25_ready
                    and retrieval_unit_bm25_ready
                ),
                ann_dimensions=frozenset(ann_dimensions),
                retrieval_unit_bm25_ready=(
                    retrieval_unit_bm25_ready
                ),
                ann_required_dimensions=frozenset(
                    ann_required_dimensions
                ),
            )
        return snapshot

    def replace_knowledge_structure(
        self,
        conn,
        *,
        document: dict[str, Any],
        chunks: list[Any],
    ) -> None:
        """Replace complete parent sections and source-backed exact facts."""

        doc_id = str(document["doc_id"])
        conn.execute("DELETE FROM kb_exact_facts WHERE doc_id = %s", (doc_id,))
        conn.execute("DELETE FROM kb_sections WHERE doc_id = %s", (doc_id,))

        sections = list(document.get("_sections") or [])
        if not sections:
            grouped: dict[str, list[Any]] = {}
            for chunk in chunks:
                if str(chunk.metadata.get("doc_id") or "") != doc_id:
                    continue
                section_id = str(
                    chunk.metadata.get("parent_section_id") or ""
                ).strip()
                if section_id:
                    grouped.setdefault(section_id, []).append(chunk)
            for section_id, section_chunks in grouped.items():
                first = section_chunks[0]
                metadata = dict(first.metadata or {})
                sections.append(
                    {
                        "section_id": section_id,
                        "doc_id": doc_id,
                        "source_id": first.source_id,
                        "scope": first.scope,
                        "index_version": metadata.get("index_version"),
                        "parent_section_id": None,
                        "chapter_section_id": section_id,
                        "title": str(metadata.get("section") or first.title),
                        "level": 1,
                        "heading_path": metadata.get("heading_path") or [],
                        "source_start_line": metadata.get("source_start_line"),
                        "source_end_line": metadata.get("source_end_line"),
                        "source_start_char": metadata.get("source_start_char"),
                        "source_end_char": metadata.get("source_end_char"),
                        "text": "\n\n".join(chunk.text for chunk in section_chunks),
                        "metadata": {"synthetic_from_chunks": True},
                    }
                )

        for section in sections:
            conn.execute(
                """
                INSERT INTO kb_sections (
                    section_id, doc_id, source_id, scope, index_version,
                    parent_section_id, chapter_section_id, title, level,
                    heading_path, source_start_line, source_end_line,
                    source_start_char, source_end_char, text, metadata, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    %s, %s, %s, %s, %s, %s::jsonb, NOW()
                )
                ON CONFLICT (section_id) DO UPDATE SET
                    parent_section_id = excluded.parent_section_id,
                    chapter_section_id = excluded.chapter_section_id,
                    title = excluded.title,
                    level = excluded.level,
                    heading_path = excluded.heading_path,
                    source_start_line = excluded.source_start_line,
                    source_end_line = excluded.source_end_line,
                    source_start_char = excluded.source_start_char,
                    source_end_char = excluded.source_end_char,
                    text = excluded.text,
                    metadata = excluded.metadata,
                    updated_at = NOW()
                """,
                (
                    section["section_id"],
                    doc_id,
                    section.get("source_id") or document["relative_path"],
                    section.get("scope") or document["scope"],
                    section.get("index_version") or document["index_version"],
                    section.get("parent_section_id"),
                    section.get("chapter_section_id"),
                    section.get("title") or document["relative_path"],
                    int(section.get("level") or 0),
                    json.dumps(section.get("heading_path") or [], ensure_ascii=False),
                    section.get("source_start_line"),
                    section.get("source_end_line"),
                    section.get("source_start_char"),
                    section.get("source_end_char"),
                    str(section.get("text") or ""),
                    json.dumps(section.get("metadata") or {}, ensure_ascii=False),
                ),
            )

        for chunk in chunks:
            metadata = dict(chunk.metadata or {})
            if str(metadata.get("doc_id") or "") != doc_id:
                continue
            for fact in metadata.get("exact_facts") or []:
                fact_id = hashlib.sha256(
                    "\x00".join(
                        (
                            chunk.chunk_id,
                            str(fact.get("fact_type") or ""),
                            str(fact.get("normalized_value") or ""),
                            str(fact.get("source_quote") or ""),
                        )
                    ).encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """
                    INSERT INTO kb_exact_facts (
                        fact_id, chunk_id, doc_id, section_id, scope,
                        index_version, fact_type, normalized_value,
                        display_value, source_quote, source_start_line,
                        source_end_line, metadata
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, '{}'::jsonb
                    )
                    ON CONFLICT (fact_id) DO NOTHING
                    """,
                    (
                        fact_id,
                        chunk.chunk_id,
                        doc_id,
                        metadata.get("parent_section_id"),
                        chunk.scope,
                        metadata.get("index_version"),
                        fact.get("fact_type"),
                        fact.get("normalized_value"),
                        fact.get("display_value"),
                        fact.get("source_quote"),
                        metadata.get("source_start_line"),
                        metadata.get("source_end_line"),
                    ),
                )

    def fetch_chunk_parent_contexts(
        self,
        chunk_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        normalized = [str(value) for value in chunk_ids if str(value).strip()]
        if not normalized:
            return {}
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.chunk_id, s.section_id, s.title, s.heading_path,
                       s.text, s.source_start_line, s.source_end_line,
                       s.source_start_char, s.source_end_char
                FROM kb_chunks c
                LEFT JOIN kb_sections s
                  ON s.section_id = c.metadata->>'parent_section_id'
                WHERE c.chunk_id = ANY(%s::text[])
                """,
                (normalized,),
            ).fetchall()
        return {
            str(self._row_value(row, "chunk_id")): {
                "section_id": self._row_value(row, "section_id"),
                "title": self._row_value(row, "title"),
                "heading_path": self._row_value(row, "heading_path") or [],
                "text": self._row_value(row, "text"),
                "source_start_line": self._row_value(row, "source_start_line"),
                "source_end_line": self._row_value(row, "source_end_line"),
                "source_start_char": self._row_value(row, "source_start_char"),
                "source_end_char": self._row_value(row, "source_end_char"),
            }
            for row in rows
            if self._row_value(row, "text")
        }

    def knowledge_document_states(
        self,
        index_version: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    d.doc_id,
                    d.scope,
                    d.source_path,
                    d.relative_path,
                    d.content_hash,
                    d.metadata,
                    COALESCE(s.chunk_count, 0) AS chunk_count,
                    s.min_dimension,
                    s.max_dimension,
                    s.min_collection_name,
                    s.max_collection_name
                FROM kb_documents d
                LEFT JOIN (
                    SELECT
                        doc_id,
                        COUNT(*)::bigint AS chunk_count,
                        MIN(vector_dims(embedding)) AS min_dimension,
                        MAX(vector_dims(embedding)) AS max_dimension,
                        MIN(collection_name) AS min_collection_name,
                        MAX(collection_name) AS max_collection_name
                    FROM kb_chunks
                    GROUP BY doc_id
                ) s ON s.doc_id = d.doc_id
                ORDER BY d.doc_id
                """
            ).fetchall()

        states: dict[str, dict[str, Any]] = {}
        for row in rows:
            doc_id = str(self._row_value(row, "doc_id") or "").strip()
            if not doc_id:
                continue
            metadata = self._row_value(row, "metadata")
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            if index_version is not None and str(
                metadata.get("index_version") or ""
            ) != str(index_version):
                continue
            states[doc_id] = {
                "doc_id": doc_id,
                "scope": str(self._row_value(row, "scope") or ""),
                "source_path": str(
                    self._row_value(row, "source_path") or ""
                ),
                "relative_path": str(
                    self._row_value(row, "relative_path") or ""
                ),
                "content_hash": str(
                    self._row_value(row, "content_hash") or ""
                ),
                "metadata": metadata,
                "chunk_count": int(
                    self._row_value(row, "chunk_count") or 0
                ),
                "min_dimension": self._optional_int(
                    self._row_value(row, "min_dimension")
                ),
                "max_dimension": self._optional_int(
                    self._row_value(row, "max_dimension")
                ),
                "min_collection_name": str(
                    self._row_value(row, "min_collection_name") or ""
                ),
                "max_collection_name": str(
                    self._row_value(row, "max_collection_name") or ""
                ),
            }
        return states

    def relocate_knowledge_document_sources(
        self,
        relocations: list[dict[str, str]],
        *,
        connection: Any | None = None,
    ) -> int:
        """Update physical source paths without touching parsed/vector data."""
        normalized = {
            str(item.get("doc_id") or "").strip(): str(
                item.get("source_path") or ""
            ).strip()
            for item in relocations
            if str(item.get("doc_id") or "").strip()
            and str(item.get("source_path") or "").strip()
        }
        if not normalized:
            return 0

        def apply(conn: Any) -> int:
            updated = 0
            for doc_id, source_path in sorted(normalized.items()):
                cursor = conn.execute(
                    """
                    UPDATE kb_documents
                    SET source_path = %s,
                        metadata = jsonb_set(
                            jsonb_set(
                                jsonb_set(
                                    COALESCE(metadata, '{}'::jsonb),
                                    '{source_path}',
                                    to_jsonb(%s::text),
                                    true
                                ),
                                '{input_path}',
                                to_jsonb(%s::text),
                                true
                            ),
                            '{parse_metadata}',
                            COALESCE(
                                metadata -> 'parse_metadata',
                                '{}'::jsonb
                            ) || jsonb_build_object(
                                'input_path', %s::text
                            ),
                            true
                        ),
                        updated_at = NOW()
                    WHERE doc_id = %s
                    """,
                    (
                        source_path,
                        source_path,
                        source_path,
                        source_path,
                        doc_id,
                    ),
                )
                updated += max(0, int(cursor.rowcount or 0))
                conn.execute(
                    """
                    UPDATE kb_chunks
                    SET metadata = jsonb_set(
                            COALESCE(metadata, '{}'::jsonb),
                            '{source_path}',
                            to_jsonb(%s::text),
                            true
                        ),
                        updated_at = NOW()
                    WHERE doc_id = %s
                    """,
                    (source_path, doc_id),
                )
            return updated

        if connection is not None:
            return apply(connection)
        with self.connection() as conn:
            return apply(conn)

    def stamp_knowledge_document_fingerprints(
        self,
        doc_ids: set[str],
        ingestion_fingerprint: str,
    ) -> int:
        normalized_ids = sorted(
            {
                str(doc_id).strip()
                for doc_id in doc_ids
                if str(doc_id).strip()
            }
        )
        normalized_fingerprint = str(ingestion_fingerprint).strip()
        if not normalized_ids or not normalized_fingerprint:
            return 0
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE kb_documents
                SET metadata = jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{ingestion_fingerprint}',
                        to_jsonb(%s::text),
                        true
                    ),
                    updated_at = NOW()
                WHERE doc_id = ANY(%s::text[])
                  AND COALESCE(metadata ->> 'ingestion_fingerprint', '')
                      IS DISTINCT FROM %s
                """,
                (
                    normalized_fingerprint,
                    normalized_ids,
                    normalized_fingerprint,
                ),
            )
        return max(0, int(cursor.rowcount or 0))

    def prune_knowledge_documents(
        self,
        *,
        active_doc_ids: set[str],
        active_collection_names: set[str],
    ) -> dict[str, int]:
        active_ids = sorted(
            {str(doc_id).strip() for doc_id in active_doc_ids if str(doc_id).strip()}
        )
        active_names = sorted(
            {
                str(name).strip()
                for name in active_collection_names
                if str(name).strip()
            }
        )
        if not active_ids or not active_names:
            raise ValueError(
                "Incremental knowledge sync requires active documents and collections."
            )
        with self.connection() as conn:
            chunks_cursor = conn.execute(
                """
                DELETE FROM kb_chunks
                WHERE collection_name = ANY(%s::text[])
                  AND doc_id <> ALL(%s::text[])
                """,
                (active_names, active_ids),
            )
            documents_cursor = conn.execute(
                """
                DELETE FROM kb_documents d
                WHERE NOT EXISTS (
                    SELECT 1 FROM kb_chunks c WHERE c.doc_id = d.doc_id
                )
                """
            )
        return {
            "pruned_chunks": max(0, int(chunks_cursor.rowcount or 0)),
            "pruned_documents": max(
                0, int(documents_cursor.rowcount or 0)
            ),
        }

    def count_missing_bm25_embeddings(self) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM kb_chunks
                     WHERE bm25_embedding IS NULL)
                    +
                    (SELECT COUNT(*) FROM kb_retrieval_units
                     WHERE bm25_embedding IS NULL) AS missing
                """
            ).fetchone()
        return int(self._row_value(row, "missing") or 0)

    def prune_knowledge_collections(
        self,
        active_collection_names: set[str],
    ) -> dict[str, Any]:
        active_names = sorted(
            {
                str(name).strip()
                for name in active_collection_names
                if str(name).strip()
            }
        )
        if not active_names:
            raise ValueError("Exact knowledge rebuild requires at least one active collection.")

        with self.connection() as conn:
            stale_rows = conn.execute(
                """
                SELECT DISTINCT collection_name, scope, vector_dims(embedding) AS dimension
                FROM kb_chunks
                WHERE collection_name <> ALL(%s::text[])
                ORDER BY collection_name, scope, dimension
                """,
                (active_names,),
            ).fetchall()
            stale_collections = sorted(
                {
                    str(self._row_value(row, "collection_name"))
                    for row in stale_rows
                }
            )
            stale_hnsw_names = sorted(
                {
                    self.hnsw_index_name(
                        str(self._row_value(row, "collection_name")),
                        str(self._row_value(row, "scope")),
                        int(self._row_value(row, "dimension")),
                    )
                    for row in stale_rows
                }
            )
            chunks_cursor = conn.execute(
                "DELETE FROM kb_chunks WHERE collection_name <> ALL(%s::text[])",
                (active_names,),
            )
            documents_cursor = conn.execute(
                """
                DELETE FROM kb_documents d
                WHERE NOT EXISTS (
                    SELECT 1 FROM kb_chunks c WHERE c.doc_id = d.doc_id
                )
                """
            )
            pruned_chunks = max(0, int(chunks_cursor.rowcount or 0))
            pruned_documents = max(0, int(documents_cursor.rowcount or 0))

        dropped_hnsw_indexes: list[str] = []
        if stale_hnsw_names:
            with self.autocommit_connection() as conn:
                for name in stale_hnsw_names:
                    if self._index_state(conn, name)[0]:
                        self._drop_index_concurrently(conn, name)
                        dropped_hnsw_indexes.append(name)

        return {
            "pruned_collections": stale_collections,
            "pruned_chunks": pruned_chunks,
            "pruned_documents": pruned_documents,
            "dropped_hnsw_indexes": dropped_hnsw_indexes,
        }

    def maintain_retrieval_indexes(
        self,
        *,
        rebuild_lexical: bool = True,
        drop_legacy_hnsw: bool = True,
        profile: EmbeddingProfileState | None = None,
    ) -> dict[str, Any]:
        profile = profile or self.require_active_embedding_profile(
            self.settings.effective_embedding_model,
            resolve_embedding_dimensions(self.settings),
        )
        specs = self.retrieval_index_specs(profile)
        unit_specs = self.retrieval_unit_index_specs(profile)
        summary: dict[str, Any] = {
            "embedding_profile": profile.as_dict(),
            "spec_count": len(specs),
            "retrieval_unit_spec_count": len(unit_specs),
            "hnsw_indexes": [],
            "retrieval_unit_hnsw_indexes": [],
            "bm25_indexes": [],
            "retrieval_unit_bm25_index": None,
            "dropped_indexes": [],
            "skipped_dimensions": [],
            "skipped_ann_scopes": [],
            "skipped_retrieval_unit_ann_scopes": [],
            "warnings": [],
            "bm25_backfilled_rows": 0,
        }
        if not specs and not unit_specs:
            return summary

        scope_pairs = sorted(
            {(item["collection_name"], item["scope"]) for item in specs}
        )
        if len(scope_pairs) > 32:
            summary["warnings"].append(
                f"知识域数量为 {len(scope_pairs)}，建议评估 kb_chunks LIST 分区。"
            )

        with (
            self._retrieval_maintenance_lock,
            self.autocommit_connection() as conn,
        ):
            conn.execute(
                "SELECT pg_advisory_lock(hashtext(%s))",
                ("hr_agent_retrieval_index_maintenance",),
            )
            try:
                if rebuild_lexical:
                    summary["dropped_indexes"].extend(
                        self._drop_indexes_with_prefix(conn, "idx_kb_bm25_")
                    )
                    summary["dropped_indexes"].extend(
                        self._drop_indexes_with_prefix(
                            conn,
                            "idx_kb_retrieval_units_bm25",
                        )
                    )
                    summary["bm25_backfilled_rows"] = (
                        self._rebuild_lexical_artifacts(conn)
                    )

                if self.settings.postgres_create_hnsw_index:
                    for item in specs:
                        dimension = int(item["dimension"])
                        hnsw_args = {
                            "collection_name": str(item["collection_name"]),
                            "scope": str(item["scope"]),
                            "dimension": dimension,
                            "profile_id": str(item["profile_id"]),
                            "build_id": str(item["build_id"]),
                        }
                        if dimension > 4000:
                            summary["skipped_dimensions"].append(dimension)
                            continue
                        if int(item["row_count"]) < int(
                            self._ann_min_rows_for_scope(
                                hnsw_args["scope"]
                            )
                        ):
                            stale_name = self.hnsw_index_name(**hnsw_args)
                            if self._index_state(conn, stale_name)[0]:
                                self._drop_index_concurrently(conn, stale_name)
                                summary["dropped_indexes"].append(stale_name)
                            summary["skipped_ann_scopes"].append(
                                {
                                    "collection_name": hnsw_args[
                                        "collection_name"
                                    ],
                                    "scope": hnsw_args["scope"],
                                    "dimension": dimension,
                                    "row_count": int(item["row_count"]),
                                }
                            )
                            continue
                        summary["hnsw_indexes"].append(
                            self._create_partial_hnsw_index(
                                conn,
                                **hnsw_args,
                            )
                        )
                    for item in unit_specs:
                        dimension = int(item["dimension"])
                        hnsw_args = {
                            "collection_name": str(item["collection_name"]),
                            "scope": str(item["scope"]),
                            "dimension": dimension,
                            "profile_id": str(item["profile_id"]),
                            "build_id": str(item["build_id"]),
                        }
                        if dimension > 4000:
                            summary["skipped_dimensions"].append(dimension)
                            continue
                        if int(item["row_count"]) < int(
                            self._ann_min_rows_for_scope(
                                hnsw_args["scope"]
                            )
                        ):
                            stale_name = (
                                self.retrieval_unit_hnsw_index_name(
                                    **hnsw_args
                                )
                            )
                            if self._index_state(conn, stale_name)[0]:
                                self._drop_index_concurrently(conn, stale_name)
                                summary["dropped_indexes"].append(stale_name)
                            summary[
                                "skipped_retrieval_unit_ann_scopes"
                            ].append(
                                {
                                    "collection_name": hnsw_args[
                                        "collection_name"
                                    ],
                                    "scope": hnsw_args["scope"],
                                    "dimension": dimension,
                                    "row_count": int(item["row_count"]),
                                }
                            )
                            continue
                        summary["retrieval_unit_hnsw_indexes"].append(
                            self._create_partial_retrieval_unit_hnsw_index(
                                conn,
                                **hnsw_args,
                            )
                        )

                for collection_name, scope in scope_pairs:
                    summary["bm25_indexes"].append(
                        self._create_partial_bm25_index(
                            conn,
                            collection_name=collection_name,
                            scope=scope,
                        )
                    )
                summary["retrieval_unit_bm25_index"] = (
                    self._create_retrieval_unit_bm25_index(conn)
                )

                expected_indexes = (
                    summary["hnsw_indexes"]
                    + summary["retrieval_unit_hnsw_indexes"]
                    + summary["bm25_indexes"]
                )
                expected_indexes.append(
                    str(summary["retrieval_unit_bm25_index"])
                )
                invalid = [
                    name
                    for name in expected_indexes
                    if not self._index_is_valid(conn, name)
                ]
                if invalid:
                    raise RuntimeError(
                        f"检索索引创建后无效：{invalid}"
                    )

                if drop_legacy_hnsw:
                    for prefix in (
                        "idx_kb_chunks_embedding_hnsw_",
                        "idx_kb_hnsw_",
                    ):
                        summary["dropped_indexes"].extend(
                            self._drop_indexes_with_prefix(conn, prefix)
                        )

                active_rows = conn.execute(
                    """
                    SELECT p.profile_id, p.active_build_id,
                           e.collection_name, e.scope,
                           vector_dims(e.embedding) AS dimension,
                           COUNT(*) AS row_count
                    FROM embedding_profiles p
                    JOIN kb_chunk_embeddings e
                      ON e.profile_id = p.profile_id
                     AND e.build_id = p.active_build_id
                    WHERE p.status = 'ready'
                      AND p.active_build_id IS NOT NULL
                    GROUP BY p.profile_id, p.active_build_id,
                             e.collection_name, e.scope,
                             vector_dims(e.embedding)
                    """
                ).fetchall()
                active_unit_rows = conn.execute(
                    """
                    SELECT p.profile_id, p.active_build_id,
                           e.collection_name, e.scope,
                           vector_dims(e.embedding) AS dimension,
                           COUNT(*) AS row_count
                    FROM embedding_profiles p
                    JOIN kb_retrieval_unit_embeddings e
                      ON e.profile_id = p.profile_id
                     AND e.build_id = p.active_build_id
                    WHERE p.status = 'ready'
                      AND p.active_build_id IS NOT NULL
                    GROUP BY p.profile_id, p.active_build_id,
                             e.collection_name, e.scope,
                             vector_dims(e.embedding)
                    """
                ).fetchall()
                expected_hnsw = {
                    self.hnsw_index_name(
                        str(self._row_value(row, "collection_name")),
                        str(self._row_value(row, "scope")),
                        int(self._row_value(row, "dimension")),
                        profile_id=str(self._row_value(row, "profile_id")),
                        build_id=str(
                            self._row_value(row, "active_build_id")
                        ),
                    )
                    for row in active_rows
                    if int(self._row_value(row, "dimension")) <= 4000
                    and int(self._row_value(row, "row_count") or 0)
                    >= self._ann_min_rows_for_scope(
                        str(self._row_value(row, "scope"))
                    )
                }
                rows = conn.execute(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'kb_chunk_embeddings'
                      AND indexname LIKE 'idx_kb_profile_hnsw_%'
                    ORDER BY indexname
                    """
                ).fetchall()
                for row in rows:
                    name = str(self._row_value(row, "indexname"))
                    if name not in expected_hnsw:
                        self._drop_index_concurrently(conn, name)
                        summary["dropped_indexes"].append(name)
                expected_unit_hnsw = {
                    self.retrieval_unit_hnsw_index_name(
                        str(self._row_value(row, "collection_name")),
                        str(self._row_value(row, "scope")),
                        int(self._row_value(row, "dimension")),
                        profile_id=str(self._row_value(row, "profile_id")),
                        build_id=str(
                            self._row_value(row, "active_build_id")
                        ),
                    )
                    for row in active_unit_rows
                    if int(self._row_value(row, "dimension")) <= 4000
                    and int(self._row_value(row, "row_count") or 0)
                    >= self._ann_min_rows_for_scope(
                        str(self._row_value(row, "scope"))
                    )
                }
                unit_index_rows = conn.execute(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'kb_retrieval_unit_embeddings'
                      AND indexname LIKE 'idx_kb_unit_profile_hnsw_%'
                    ORDER BY indexname
                    """
                ).fetchall()
                for row in unit_index_rows:
                    name = str(self._row_value(row, "indexname"))
                    if name not in expected_unit_hnsw:
                        self._drop_index_concurrently(conn, name)
                        summary["dropped_indexes"].append(name)
            finally:
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))",
                    ("hr_agent_retrieval_index_maintenance",),
                )
        return summary

    def _lexical_artifact_names(self) -> tuple[str, str, str]:
        tokenizer = str(self.settings.postgres_bm25_tokenizer_name).strip()
        if not tokenizer or len(tokenizer) > 40 or not all(
            char.isascii() and (char.isalnum() or char == "_") for char in tokenizer
        ):
            raise ValueError("POSTGRES_BM25_TOKENIZER_NAME 只能包含 1-40 个 ASCII 字母、数字或下划线。")
        return tokenizer, f"{tokenizer}_model", f"{tokenizer}_analyzer"

    def _retrieval_unit_lexical_artifact_names(
        self,
    ) -> tuple[str, str, str]:
        base, _, _ = self._lexical_artifact_names()
        tokenizer = f"{base[:37]}_ru"
        return tokenizer, f"{tokenizer}_model", f"{tokenizer}_analyzer"

    def retrieval_unit_bm25_tokenizer_name(self) -> str:
        return self._retrieval_unit_lexical_artifact_names()[0]

    def _rebuild_lexical_artifacts(self, conn) -> int:
        specs = (
            (*self._lexical_artifact_names(), "kb_chunks"),
            (
                *self._retrieval_unit_lexical_artifact_names(),
                "kb_retrieval_units",
            ),
        )
        backfilled = 0
        for tokenizer, model, analyzer, table_name in specs:
            self._drop_catalog_entry(
                conn,
                table="tokenizer",
                name=tokenizer,
                function="drop_tokenizer",
            )
            self._drop_catalog_entry(
                conn,
                table="model",
                name=model,
                function="drop_custom_model",
            )
            self._drop_catalog_entry(
                conn,
                table="text_analyzer",
                name=analyzer,
                function="drop_text_analyzer",
            )
            conn.execute(
                "SELECT tokenizer_catalog.create_text_analyzer(%s, %s)",
                (analyzer, self.JIEBA_ANALYZER_CONFIG),
            )
            conn.execute(
                """
                SELECT tokenizer_catalog.create_custom_model_tokenizer_and_trigger(
                    %s, %s, %s, %s, 'search_text', 'bm25_embedding'
                )
                """,
                (tokenizer, model, analyzer, table_name),
            )
            cursor = conn.execute(
                f"UPDATE public.{table_name} SET search_text = search_text"
            )
            remaining_row = conn.execute(
                f"SELECT COUNT(*) AS missing FROM public.{table_name} "
                "WHERE bm25_embedding IS NULL"
            ).fetchone()
            missing = int(
                self._row_value(remaining_row, "missing") or 0
            )
            if missing:
                raise RuntimeError(
                    "BM25 tokenizer trigger did not backfill "
                    f"{missing} {table_name} rows."
                )
            backfilled += max(
                0,
                int(getattr(cursor, "rowcount", 0) or 0),
            )
        return backfilled

    @staticmethod
    def _drop_catalog_entry(conn, *, table: str, name: str, function: str) -> None:
        allowed = {
            ("tokenizer", "drop_tokenizer"),
            ("model", "drop_custom_model"),
            ("text_analyzer", "drop_text_analyzer"),
        }
        if (table, function) not in allowed:
            raise ValueError("Unsupported tokenizer catalog operation.")
        row = conn.execute(
            f"SELECT EXISTS(SELECT 1 FROM tokenizer_catalog.{table} WHERE name = %s) AS present",
            (name,),
        ).fetchone()
        present = bool(PostgresRepository._row_value(row, "present"))
        if present:
            conn.execute(f"SELECT tokenizer_catalog.{function}(%s)", (name,))

    def _create_partial_hnsw_index(
        self,
        conn,
        *,
        collection_name: str,
        scope: str,
        dimension: int,
        profile_id: str,
        build_id: str,
    ) -> str:
        from psycopg import sql

        vector_type = "halfvec" if dimension > 2000 else "vector"
        opclass = "halfvec_cosine_ops" if dimension > 2000 else "vector_cosine_ops"
        name = self.hnsw_index_name(
            collection_name,
            scope,
            dimension,
            profile_id=profile_id,
            build_id=build_id,
        )
        self._drop_invalid_index(conn, name)
        statement = sql.SQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS {} "
            "ON public.kb_chunk_embeddings USING hnsw "
            "((embedding::{}({})) {}) WHERE profile_id = {} "
            "AND build_id = {}::uuid AND collection_name = {} AND scope = {} "
            "AND vector_dims(embedding) = {}"
        ).format(
            sql.Identifier(name),
            sql.SQL(vector_type),
            sql.Literal(dimension),
            sql.SQL(opclass),
            sql.Literal(profile_id),
            sql.Literal(build_id),
            sql.Literal(collection_name),
            sql.Literal(scope),
            sql.Literal(dimension),
        )
        conn.execute(statement)
        return name

    def _create_partial_retrieval_unit_hnsw_index(
        self,
        conn,
        *,
        collection_name: str,
        scope: str,
        dimension: int,
        profile_id: str,
        build_id: str,
    ) -> str:
        from psycopg import sql

        vector_type = "halfvec" if dimension > 2000 else "vector"
        opclass = (
            "halfvec_cosine_ops" if dimension > 2000 else "vector_cosine_ops"
        )
        name = self.retrieval_unit_hnsw_index_name(
            collection_name,
            scope,
            dimension,
            profile_id=profile_id,
            build_id=build_id,
        )
        self._drop_invalid_index(conn, name)
        statement = sql.SQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS {} "
            "ON public.kb_retrieval_unit_embeddings USING hnsw "
            "((embedding::{}({})) {}) WHERE profile_id = {} "
            "AND build_id = {}::uuid AND collection_name = {} AND scope = {} "
            "AND vector_dims(embedding) = {}"
        ).format(
            sql.Identifier(name),
            sql.SQL(vector_type),
            sql.Literal(dimension),
            sql.SQL(opclass),
            sql.Literal(profile_id),
            sql.Literal(build_id),
            sql.Literal(collection_name),
            sql.Literal(scope),
            sql.Literal(dimension),
        )
        conn.execute(statement)
        return name

    def _create_partial_bm25_index(self, conn, *, collection_name: str, scope: str) -> str:
        from psycopg import sql

        name = self.bm25_index_name(collection_name, scope)
        self._drop_invalid_index(conn, name)
        statement = sql.SQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON public.kb_chunks "
            "USING bm25 (bm25_embedding bm25_ops) WHERE bm25_embedding IS NOT NULL "
            "AND collection_name = {} AND scope = {}"
        ).format(
            sql.Identifier(name),
            sql.Literal(collection_name),
            sql.Literal(scope),
        )
        conn.execute(statement)
        return name

    def _create_retrieval_unit_bm25_index(self, conn) -> str:
        from psycopg import sql

        name = self.retrieval_unit_bm25_index_name()
        self._drop_invalid_index(conn, name)
        statement = sql.SQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS {} "
            "ON public.kb_retrieval_units "
            "USING bm25 (bm25_embedding bm25_ops) "
            "WHERE bm25_embedding IS NOT NULL"
        ).format(sql.Identifier(name))
        conn.execute(statement)
        return name

    @staticmethod
    def _index_state(conn, name: str) -> tuple[bool, bool]:
        row = conn.execute(
            """
            SELECT TRUE AS present, i.indisvalid AS valid
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE n.nspname = 'public' AND c.relname = %s
            """,
            (name,),
        ).fetchone()
        if row is None:
            return False, False
        return True, bool(PostgresRepository._row_value(row, "valid"))

    @classmethod
    def _index_is_valid(cls, conn, name: str) -> bool:
        present, valid = cls._index_state(conn, name)
        return present and valid

    @classmethod
    def _drop_invalid_index(cls, conn, name: str) -> None:
        present, valid = cls._index_state(conn, name)
        if present and not valid:
            cls._drop_index_concurrently(conn, name)

    @staticmethod
    def _drop_index_concurrently(conn, name: str) -> None:
        from psycopg import sql

        conn.execute(
            sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}.{}").format(
                sql.Identifier("public"),
                sql.Identifier(name),
            )
        )

    @staticmethod
    def _drop_indexes_with_prefix(conn, prefix: str) -> list[str]:
        rows = conn.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'kb_chunks' AND indexname LIKE %s
            ORDER BY indexname
            """,
            (f"{prefix}%",),
        ).fetchall()
        names = [str(PostgresRepository._row_value(row, "indexname")) for row in rows]
        for name in names:
            PostgresRepository._drop_index_concurrently(conn, name)
        return names

    @staticmethod
    def dumps(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def vector_literal(vector: list[float]) -> str:
        if not vector:
            raise ValueError("Embedding vector cannot be empty.")
        return "[" + ",".join(str(float(x)) for x in vector) + "]"

    def load_metadata(self, key: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM app_metadata WHERE key = %s", (key,)).fetchone()
        if not row:
            return {}
        value = row["value"]
        if isinstance(value, str):
            return json.loads(value)
        return dict(value)

    def save_metadata(self, key: str, value: dict) -> dict:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO app_metadata (key, value, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = NOW()
                """,
                (key, self.dumps(value)),
            )
        return value

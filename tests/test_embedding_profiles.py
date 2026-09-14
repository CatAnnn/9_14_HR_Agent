from __future__ import annotations

from contextlib import contextmanager
import json

import pytest

from backend.config.settings import get_settings
from backend.redis.embedding_cache import QueryEmbeddingCache
from backend.repositories.postgres_repository import (
    EmbeddingProfileState,
    PostgresRepository,
)
from backend.vectorstore.embedding_profile import (
    embedding_metadata_in_corpus,
    embedding_profile_corpus,
    embedding_profile_id,
)
from backend.vectorstore.index_manager import IndexManager


def test_profile_id_uses_nfkc_trim_model_and_dimensions_only():
    canonical = embedding_profile_id("Qwen/Qwen3-Embedding-4B", 2048)

    assert (
        embedding_profile_id("  Ｑwen/Qwen3-Embedding-4B  ", 2048)
        == canonical
    )
    assert (
        embedding_profile_id("qwen/Qwen3-Embedding-4B", 2048)
        != canonical
    )
    assert (
        embedding_profile_id("Qwen/Qwen3-Embedding-4B", 1024)
        != canonical
    )


def test_text_embedding_v4_2048_uses_only_kb_raw_corpus():
    assert embedding_profile_corpus("text-embedding-v4", 2048) == "kb_raw"
    assert embedding_profile_corpus("text-embedding-v4", 1024) == "all"
    assert (
        embedding_profile_corpus("Qwen/Qwen3-Embedding-4B", 2048)
        == "all"
    )
    assert embedding_metadata_in_corpus(
        {"source_path": "/app/data/kb_raw/performance/rules.md"},
        "kb_raw",
    )
    assert not embedding_metadata_in_corpus(
        {
            "source_path": (
                "/app/data/kb_large/organization_unit/"
                "organization_unit.xlsx"
            )
        },
        "kb_raw",
    )


def test_profile_source_queries_filter_kb_raw_for_text_embedding_v4():
    statements: list[tuple[str, tuple]] = []

    class Cursor:
        def fetchall(self):
            return []

    class Connection:
        def execute(self, statement, params=()):
            statements.append((str(statement), tuple(params or ())))
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    repository = object.__new__(PostgresRepository)
    repository.connection = connection

    assert (
        repository.fetch_embedding_source_chunks(
            model_name="text-embedding-v4",
            dimensions=2048,
        )
        == []
    )
    assert (
        repository.fetch_embedding_source_retrieval_units(
            model_name="text-embedding-v4",
            dimensions=2048,
        )
        == []
    )

    assert len(statements) == 2
    assert all("/data/kb_raw/" in params[0] for _, params in statements)
    assert "FROM kb_chunks c" in statements[0][0]
    assert "JOIN kb_chunks c" in statements[1][0]


def test_provider_and_endpoint_do_not_change_profile_or_query_cache_key():
    base = get_settings()
    local = base.model_copy(
        update={
            "model_provider_mode": "local",
            "embedding_local_model": "shared-model",
            "embedding_local_dimensions": 2048,
            "embedding_dimensions": 2048,
            "embedding_local_api_endpoint": "http://local/embeddings",
        }
    )
    platform = base.model_copy(
        update={
            "model_provider_mode": "platform",
            "embedding_provider": "bosch",
            "embedding_model": "shared-model",
            "embedding_dimensions": 2048,
            "embedding_api_endpoint": "https://platform/embeddings",
        }
    )

    assert local.embedding_profile_id == platform.embedding_profile_id
    assert (
        QueryEmbeddingCache(local).key_for_query("  同一个 查询 ")
        == QueryEmbeddingCache(platform).key_for_query("同一个 查询")
    )


def test_require_active_profile_rejects_missing_or_stale_repository():
    repository = object.__new__(PostgresRepository)
    stale = EmbeddingProfileState(
        profile_id="profile-a",
        model_name="embedding-model",
        dimensions=2048,
        status="stale",
        active_build_id="00000000-0000-0000-0000-000000000001",
        source_fingerprint="old",
        chunk_count=827,
        vector_count=826,
        current_chunk_count=827,
        stale_reason="vector_count_incomplete",
    )
    repository.embedding_profile_state = lambda model, dimensions: stale

    with pytest.raises(RuntimeError, match="profile-a"):
        repository.require_active_embedding_profile(
            "embedding-model",
            2048,
        )

    missing = EmbeddingProfileState(
        profile_id="profile-empty",
        model_name="embedding-model",
        dimensions=2048,
        status="missing",
        active_build_id=None,
        source_fingerprint="",
        chunk_count=0,
        vector_count=0,
        current_chunk_count=0,
        stale_reason="profile_missing",
    )
    repository.embedding_profile_state = (
        lambda model, dimensions: missing
    )

    with pytest.raises(RuntimeError, match="profile-empty"):
        repository.require_active_embedding_profile(
            "embedding-model",
            2048,
        )


def test_legacy_provider_specific_fingerprint_is_adopted_without_rebuild():
    manager = object.__new__(IndexManager)
    manager.settings = get_settings().model_copy(
        update={
            "model_provider_mode": "platform",
            "embedding_provider": "bosch",
            "embedding_model": "text-embedding-v4",
            "embedding_dimensions": 2048,
        }
    )
    old_fingerprint = manager._serialize_ingestion_fingerprint(
        fingerprint_version="kb-ingestion-v1",
        embedding={
            "provider": "bosch",
            "model": "text-embedding-v4",
            "dimensions": 2048,
        },
    )
    new_fingerprint = manager._ingestion_fingerprint()
    source = {
        "scope": "performance",
        "relative_path": "performance/rules.md",
        "content_hash": "content-a",
        "collection_name": "hr_agent_kb_performance",
        "index_version": "v2",
    }
    state = {
        "scope": "performance",
        "relative_path": "performance/rules.md",
        "content_hash": "content-a",
        "chunk_count": 8,
        "min_collection_name": "hr_agent_kb_performance",
        "max_collection_name": "hr_agent_kb_performance",
        "metadata": {
            "chunk_count": 8,
            "index_version": "v2",
            "embedding_model": "text-embedding-v4",
            "embedding_dimensions": 2048,
            "ingestion_fingerprint": old_fingerprint,
        },
    }
    legacy = manager._legacy_ingestion_fingerprints({"doc-a": state})

    assert old_fingerprint in legacy
    assert manager._source_is_current(
        source,
        state,
        ingestion_fingerprint=new_fingerprint,
        legacy_ingestion_fingerprints=legacy,
    )
    assert manager._is_legacy_current_source(
        source,
        state,
        ingestion_fingerprint=new_fingerprint,
        legacy_ingestion_fingerprints=legacy,
    )


def test_complete_profile_build_embeds_before_atomic_database_write(
    monkeypatch,
):
    import backend.vectorstore.index_manager as index_manager_module
    from backend.services.retrieval_service import RetrievalService

    events: list[tuple] = []
    settings = get_settings().model_copy(
        update={
            "model_provider_mode": "platform",
            "embedding_model": "model-a",
            "embedding_dimensions": 2,
            "kb_ingest_batch_size": 2,
        }
    )
    source_rows = [
        {
            "chunk_id": f"chunk-{index}",
            "collection_name": "kb_general",
            "doc_id": "doc-a",
            "source_id": "general/a.md",
            "title": "A",
            "scope": "general",
            "text": f"text-{index}",
            "search_text": f"context-{index}",
            "metadata": {},
            "index_version": "v2",
            "content_hash": "hash-a",
        }
        for index in range(3)
    ]
    profile = EmbeddingProfileState(
        profile_id=embedding_profile_id("model-a", 2),
        model_name="model-a",
        dimensions=2,
        status="ready",
        active_build_id="00000000-0000-0000-0000-000000000001",
        source_fingerprint="fingerprint",
        chunk_count=3,
        vector_count=3,
        current_chunk_count=3,
    )

    class Embeddings:
        def __init__(self, *, settings):
            self.settings = settings

        def embed(self, texts):
            events.append(("embed", tuple(texts)))
            return [[1.0, 0.0] for _ in texts]

    class Repository:
        def fetch_embedding_source_chunks(
            self,
            *,
            model_name,
            dimensions,
        ):
            assert model_name == "model-a"
            assert dimensions == 2
            return source_rows

        @contextmanager
        def connection(self):
            events.append(("transaction_enter",))
            yield object()
            events.append(("transaction_exit",))

        def begin_embedding_profile_build(self, conn, **kwargs):
            events.append(("begin", kwargs))
            return profile.profile_id, profile.active_build_id

        def upsert_embedding_rows(self, conn, **kwargs):
            events.append(("write", len(kwargs["chunks"])))
            return len(kwargs["chunks"])

        def activate_embedding_profile_build(self, conn, **kwargs):
            events.append(("activate", kwargs))
            return profile

        def maintain_retrieval_indexes(self, **kwargs):
            events.append(("maintain", kwargs))
            return {"hnsw_indexes": []}

    manager = object.__new__(IndexManager)
    manager.settings = settings
    manager.postgres_repo = Repository()
    monkeypatch.setattr(index_manager_module, "EmbeddingService", Embeddings)
    monkeypatch.setattr(
        RetrievalService,
        "clear_result_cache",
        staticmethod(lambda: events.append(("cache_clear",))),
    )

    result = manager.build_embedding_profile(
        model_name="model-a",
        dimensions=2,
        provider_mode="platform",
    )

    last_embed = max(
        index for index, event in enumerate(events) if event[0] == "embed"
    )
    begin = next(
        index for index, event in enumerate(events) if event[0] == "begin"
    )
    transaction_exit = next(
        index
        for index, event in enumerate(events)
        if event[0] == "transaction_exit"
    )
    maintain = next(
        index for index, event in enumerate(events) if event[0] == "maintain"
    )
    embedded_texts = [
        value
        for event in events
        if event[0] == "embed"
        for value in event[1]
    ]
    assert embedded_texts == ["context-0", "context-1", "context-2"]
    assert last_embed < begin
    assert transaction_exit < maintain
    assert sum(event[1] for event in events if event[0] == "write") == 3
    assert result["embedding_profile"]["ready"] is True
    assert result["vector_count"] == 3


def test_legacy_vectors_are_copied_without_embedding_model_call():
    statements: list[str] = []
    knowledge_rows = [
        {
            "chunk_id": f"chunk-{index}",
            "content_hash": "hash",
            "index_version": "v2",
        }
        for index in range(827)
    ]

    class _Cursor:
        def __init__(self, *, rows=None, row=None, rowcount=0):
            self._rows = rows or []
            self._row = row
            self.rowcount = rowcount

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._row

    class Connection:
        def execute(self, statement, params=None):
            sql = str(statement)
            statements.append(sql)
            if "GROUP BY vector_dims(embedding)" in sql:
                return _Cursor(
                    rows=[{"dimensions": 2048, "row_count": 827}]
                )
            if "SELECT DISTINCT NULLIF" in sql:
                return _Cursor(
                    rows=[{"model_name": "text-embedding-v4"}]
                )
            if "SELECT active_build_id" in sql:
                return _Cursor(row=None)
            if "SELECT c.chunk_id, c.content_hash, c.index_version" in sql:
                return _Cursor(rows=knowledge_rows)
            if "INSERT INTO kb_chunk_embeddings" in sql:
                return _Cursor(rowcount=827)
            return _Cursor()

    repository = object.__new__(PostgresRepository)
    repository.settings = get_settings().model_copy(
        update={
            "model_provider_mode": "platform",
            "embedding_model": "some-other-model",
            "embedding_dimensions": 1024,
        }
    )

    repository._backfill_legacy_embedding_profile(Connection())

    copy_sql = next(
        sql
        for sql in statements
        if "INSERT INTO kb_chunk_embeddings" in sql
    )
    assert "SELECT %s, %s, chunk_id, collection_name, scope, embedding" in copy_sql
    assert "FROM kb_chunks" in copy_sql


def test_profile_maintenance_cli_main_outputs_json_and_exit_code(
    monkeypatch,
    capsys,
):
    import scripts.manage_embedding_profiles as cli

    monkeypatch.setattr(cli, "parse_args", lambda argv: object())
    monkeypatch.setattr(
        cli,
        "run",
        lambda args: ({"embedding_profile": {"status": "ready"}}, 2),
    )

    assert cli.main([]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "embedding_profile": {"status": "ready"}
    }


def test_schema_initializes_embedding_profile_tables():
    statements: list[str] = []

    class Cursor:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class Connection:
        def execute(self, statement, params=None):
            statements.append(str(statement))
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    repository = object.__new__(PostgresRepository)
    repository.connection = connection
    repository.init_schema()

    schema_sql = "\n".join(statements)
    assert "CREATE TABLE IF NOT EXISTS embedding_profiles" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS embedding_profile_builds" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS kb_chunk_embeddings" in schema_sql

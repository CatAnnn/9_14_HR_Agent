from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

import backend.services.retrieval_service as retrieval_service_module
from backend.redis.retrieval_cache import RetrievalResultCache
from backend.repositories.postgres_repository import (
    EmbeddingProfileState,
    PostgresRepository,
    RetrievalScopeCapability,
)
from backend.schemas.retrieval import RetrievedChunk
from backend.services.retrieval_service import (
    RetrievalService,
    _RankedBranch,
    _RenderedQuerySpec,
    _RetrievalPlan,
    _ScopeSearchPolicy,
    _SearchJobResult,
)
from backend.vectorstore.pgvector_client import (
    HybridScopeSearchRequest,
    PGVectorClient,
)
from backend.vectorstore.index_manager import IndexManager


class _Cursor:
    def __init__(self, *, rows=None, row=None, rowcount=0):
        self._rows = rows or []
        self._row = row
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


def _chunk(chunk_id: str, score: float, *, scope: str = "general") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id=f"source-{chunk_id}",
        title=f"title-{chunk_id}",
        scope=scope,
        text=f"text-{chunk_id}",
        score=score,
    )


def test_hybrid_search_sets_local_before_queries_and_executes_no_ddl():
    statements: list[str] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append(str(statement))
            return _Cursor()

    class Repository:
        @contextmanager
        def connection(self):
            yield Connection()

        @staticmethod
        def vector_literal(vector):
            return "[1.0,0.0]"

        @staticmethod
        def bm25_index_name(collection_name, scope):
            return "idx_kb_bm25_test"

    client = object.__new__(PGVectorClient)
    client.collection_name = "kb_general"
    client.repo = Repository()
    client.settings = SimpleNamespace(
        postgres_hnsw_iterative_scan="relaxed_order",
        postgres_hnsw_ef_search=100,
        postgres_hnsw_max_scan_tuples=20_000,
        postgres_bm25_limit=200,
        postgres_bm25_tokenizer_name="kb_jieba_v1",
        postgres_create_hnsw_index=True,
        rag_hybrid_search_enabled=True,
    )

    result = client.search_hybrid_by_embedding(
        "绩效反馈",
        [1.0, 0.0],
        scope="general",
        metadata_filter={"scope": "general"},
    )

    assert result.dense == []
    assert result.lexical == []
    assert statements[0] == "SET LOCAL hnsw.iterative_scan = relaxed_order"
    assert statements[1] == "SET LOCAL hnsw.ef_search = 100"
    assert statements[2] == "SET LOCAL hnsw.max_scan_tuples = 20000"
    assert statements[3] == "SET LOCAL plan_cache_mode = force_custom_plan"
    dense_position = next(
        index
        for index, statement in enumerate(statements)
        if "ORDER BY e.embedding::vector(2)" in statement
    )
    bm25_setting_position = statements.index(
        "SET LOCAL bm25_catalog.bm25_limit = 200"
    )
    lexical_position = next(
        index
        for index, statement in enumerate(statements)
        if "to_bm25query" in statement
    )
    assert dense_position < bm25_setting_position < lexical_position
    assert not any(
        keyword in statement.upper()
        for statement in statements
        for keyword in ("CREATE INDEX", "DROP INDEX", "ALTER INDEX")
    )


def test_multi_scope_hybrid_search_batches_each_modality_once():
    statements: list[tuple[str, tuple]] = []

    def row(chunk_id: str, scope: str, score: float, branch_index: int):
        return {
            "branch_index": branch_index,
            "chunk_id": chunk_id,
            "source_id": f"{scope}.md",
            "title": scope,
            "scope": scope,
            "text": f"{scope} text",
            "metadata": {},
            "score": score,
        }

    class Connection:
        def execute(self, statement, params=None):
            sql = str(statement)
            statements.append((sql, tuple(params or ())))
            if "scoped_dense" in sql:
                return _Cursor(
                    rows=[
                        row("dense-general", "general", 0.9, 0),
                        row("dense-performance", "performance", 0.8, 1),
                    ]
                )
            if "scoped_lexical" in sql:
                return _Cursor(
                    rows=[
                        row("bm25-general", "general", 3.0, 0),
                        row("bm25-performance", "performance", 2.0, 1),
                    ]
                )
            return _Cursor()

    class Repository:
        @contextmanager
        def connection(self):
            yield Connection()

        @staticmethod
        def vector_literal(vector):
            return "[1.0,0.0]"

        @staticmethod
        def bm25_index_name(collection_name, scope):
            return f"idx_{scope}"

    client = object.__new__(PGVectorClient)
    client.collection_name = "kb_general"
    client.repo = Repository()
    client.settings = SimpleNamespace(
        postgres_create_hnsw_index=True,
        postgres_hnsw_iterative_scan="relaxed_order",
        postgres_hnsw_ef_search=100,
        postgres_hnsw_max_scan_tuples=20_000,
        postgres_bm25_limit=200,
        postgres_bm25_tokenizer_name="kb_jieba_v1",
        rag_hybrid_search_enabled=True,
    )
    requests = [
        HybridScopeSearchRequest(
            collection_name="kb_general",
            scope="general",
            metadata_filter={"scope": "general"},
            use_ann=True,
            lexical_enabled=True,
        ),
        HybridScopeSearchRequest(
            collection_name="kb_performance",
            scope="performance",
            metadata_filter={"scope": "performance"},
            use_ann=True,
            lexical_enabled=True,
        ),
    ]

    results = client.search_hybrid_multi_scope_by_embedding(
        "绩效反馈",
        [1.0, 0.0],
        requests=requests,
        dense_top_k=16,
        lexical_top_k=16,
    )

    assert [[chunk.chunk_id for chunk in result.dense] for result in results] == [
        ["dense-general"],
        ["dense-performance"],
    ]
    assert [[chunk.chunk_id for chunk in result.lexical] for result in results] == [
        ["bm25-general"],
        ["bm25-performance"],
    ]
    dense_sql = [sql for sql, _ in statements if "scoped_dense" in sql]
    lexical_sql = [sql for sql, _ in statements if "scoped_lexical" in sql]
    assert len(dense_sql) == 1
    assert len(lexical_sql) == 1
    assert "UNION ALL" in dense_sql[0]
    assert "UNION ALL" in lexical_sql[0]


def test_pgvector_client_disables_request_time_schema_initialization(monkeypatch):
    import backend.vectorstore.pgvector_client as pgvector_module

    initialization_flags = []

    class Repository:
        def __init__(self, *, initialize):
            initialization_flags.append(initialize)

    monkeypatch.setattr(pgvector_module, "PostgresRepository", Repository)

    PGVectorClient(
        embedding_service=object(),
        collection_name="kb_general",
    )

    assert initialization_flags == [False]


def test_upsert_request_path_executes_no_index_ddl():
    statements: list[str] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append(str(statement))
            return _Cursor()

    class Repository:
        @contextmanager
        def connection(self):
            yield Connection()

        @staticmethod
        def vector_literal(vector):
            return "[1.0,0.0]"

    client = object.__new__(PGVectorClient)
    client.collection_name = "kb_general"
    client.repo = Repository()
    client.settings = SimpleNamespace(kb_index_version="v1")

    count = client.upsert_chunks([_chunk("chunk-1", 0.0)], [[1.0, 0.0]])

    assert count == 1
    assert any("INSERT INTO kb_chunks" in statement for statement in statements)
    assert not any("CREATE INDEX" in statement.upper() for statement in statements)



def test_neighbor_lookup_uses_doc_chunk_index_and_record_boundary():
    executions = []

    class Connection:
        def execute(self, statement, params=None):
            executions.append((str(statement), params))
            return _Cursor(
                rows=[
                    {
                        "chunk_id": "neighbor-1",
                        "source_id": "source.md",
                        "title": "source",
                        "scope": "performance",
                        "text": "neighbor text",
                        "metadata": {
                            "doc_id": "doc-1",
                            "chunk_index": 4,
                            "record_id": "record-1",
                        },
                        "score": 0.0,
                        "anchor_chunk_id": "anchor-1",
                        "neighbor_distance": 1,
                    }
                ]
            )

    class Repository:
        @contextmanager
        def connection(self):
            yield Connection()

    client = object.__new__(PGVectorClient)
    client.collection_name = "kb-performance"
    client.repo = Repository()
    anchor = RetrievedChunk(
        chunk_id="anchor-1",
        source_id="source.md",
        title="source",
        scope="performance",
        text="anchor text",
        metadata={
            "doc_id": "doc-1",
            "chunk_index": 3,
            "record_id": "record-1",
        },
    )

    neighbors = client.fetch_neighbor_chunks([anchor], window=1)

    statement, params = executions[0]
    payload = json.loads(params[0])
    assert "jsonb_to_recordset" in statement
    assert "c.doc_id = anchors.doc_id" in statement
    assert "c.metadata ->> 'record_id' = anchors.record_id" in statement
    assert params[1:] == ("kb-performance", 1)
    assert payload == [
        {
            "anchor_chunk_id": "anchor-1",
            "doc_id": "doc-1",
            "scope": "performance",
            "chunk_index": 3,
            "record_id": "record-1",
        }
    ]
    assert [chunk.chunk_id for chunk in neighbors] == ["neighbor-1"]
    assert neighbors[0].metadata["agentic_neighbor"] == {
        "parent_chunk_ids": ["anchor-1"],
        "distance": 1,
    }


def test_kb_embedding_preflight_uses_configured_batches():
    calls: list[list[str]] = []

    class Embeddings:
        def embed(self, texts):
            calls.append(list(texts))
            return [[float(index)] for index, _ in enumerate(texts)]

    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(kb_ingest_batch_size=2)
    manager.embedding_service = Embeddings()
    chunks = [_chunk(f"chunk-{index}", 0.0) for index in range(5)]

    prepared_chunks, embeddings = manager._embed_chunks(chunks)

    assert [len(batch) for batch in calls] == [2, 2, 1]
    assert prepared_chunks == chunks
    assert len(embeddings) == len(chunks)


def test_exact_rebuild_finishes_all_writes_before_pruning_and_index_maintenance(
    monkeypatch,
    tmp_path,
):
    import backend.vectorstore.index_manager as index_manager_module

    events: list[tuple] = []
    chunks_by_scope = {
        "career": [_chunk("career-1", 0.0, scope="career")],
        "feedback": [_chunk("feedback-1", 0.0, scope="feedback")],
    }
    documents = [
        {"doc_id": "career-doc", "scope": "career"},
        {"doc_id": "feedback-doc", "scope": "feedback"},
    ]
    profile = EmbeddingProfileState(
        profile_id="profile-a",
        model_name="embedding-model",
        dimensions=2,
        status="ready",
        active_build_id="00000000-0000-0000-0000-000000000001",
        source_fingerprint="fingerprint",
        chunk_count=2,
        vector_count=2,
        current_chunk_count=2,
    )

    class Connection:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "SELECT DISTINCT collection_name" in sql:
                assert params[0] == "v3"
                return _Cursor(rows=[{"collection_name": "kb_bosch"}])
            if "DELETE FROM kb_chunks" in sql and "<> ALL" in sql:
                assert params[0] == "v3"
                events.append(("prune", tuple(params[1])))
                return _Cursor(rowcount=14)
            if "DELETE FROM kb_documents" in sql:
                events.append(("prune_documents",))
                return _Cursor(rowcount=2)
            return _Cursor()

    class Store:
        def __init__(self, *, collection_name, **kwargs):
            self.collection_name = collection_name

        def upsert_document(self, document, *, connection=None):
            events.append(
                ("document", self.collection_name, document["doc_id"])
            )

        def upsert_chunks(self, chunks, embeddings, **kwargs):
            events.append(("chunks", self.collection_name, len(chunks)))
            assert kwargs["embedding_profile_id"] == "profile-a"
            assert kwargs["embedding_build_id"].endswith("0001")
            return len(chunks)

    class Repository:
        @staticmethod
        def knowledge_document_states(index_version=None):
            return {}

        @contextmanager
        def connection(self):
            yield Connection()

        def begin_embedding_profile_build(self, conn, **kwargs):
            events.append(("begin", kwargs))
            return profile.profile_id, profile.active_build_id

        def copy_active_embedding_rows(self, conn, **kwargs):
            events.append(("copy", kwargs))
            return 0

        def replace_knowledge_structure(self, conn, **kwargs):
            events.append(("structure", kwargs["document"]["doc_id"]))

        def activate_embedding_profile_build(self, conn, **kwargs):
            events.append(("activate", kwargs))
            return profile

        def maintain_retrieval_indexes(self, **kwargs):
            events.append(("maintain", kwargs))
            return {"bm25_indexes": ["career", "feedback"]}

    raw_root = tmp_path / "kb_raw"
    raw_root.mkdir()
    (raw_root / "guide.md").write_text("core", encoding="utf-8")
    organization_root = tmp_path / "kb_large" / "organization_unit"
    organization_root.mkdir(parents=True)
    (organization_root / "organization_unit.xlsx").write_bytes(b"source")
    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(
        data_dir=tmp_path,
        collection_name_for_scope=lambda scope: f"kb_{scope}",
        database_url="postgresql://configured",
        vectorstore_provider="pgvector",
        effective_embedding_provider="openai_compatible",
        effective_embedding_model="embedding-model",
        embedding_dimensions=2,
        kb_index_version="v3",
    )
    manager.embedding_service = object()
    manager.postgres_repo = Repository()
    manager.manifest_repo = SimpleNamespace(save=lambda payload: None)
    manager.vector_repo = SimpleNamespace(save=lambda payload: None)
    manager._image_analysis_summary = {"enabled": True}
    manager._image_analysis_warnings = []
    manager._reset_image_analysis_summary = lambda: None
    manager._build_chunks = lambda root, **kwargs: (
        (chunks_by_scope, documents)
        if root == raw_root
        else ({}, [])
    )
    manager._embed_chunks = lambda chunks: (
        chunks,
        [[1.0, 0.0] for _ in chunks],
    )

    monkeypatch.setattr(index_manager_module, "PGVectorClient", Store)
    monkeypatch.setattr(
        RetrievalService,
        "clear_result_cache",
        staticmethod(lambda: events.append(("cache_clear",))),
    )

    rebuilt = manager.rebuild(exact=True)

    chunk_write_positions = [
        index for index, event in enumerate(events) if event[0] == "chunks"
    ]
    structure_write_positions = [
        index for index, event in enumerate(events) if event[0] == "structure"
    ]
    prune_position = next(
        index for index, event in enumerate(events) if event[0] == "prune"
    )
    activate_position = next(
        index for index, event in enumerate(events) if event[0] == "activate"
    )
    maintain_position = next(
        index for index, event in enumerate(events) if event[0] == "maintain"
    )
    assert len(rebuilt) == 2
    assert (
        max([*chunk_write_positions, *structure_write_positions])
        < prune_position
        < activate_position
        < maintain_position
    )
    assert manager.last_summary["active_scopes"] == ["career", "feedback"]
    assert manager.last_summary["pruned_collections"] == ["kb_bosch"]
    assert manager.last_summary["pruned_chunks"] == 14


def test_resource_documents_are_built_in_an_isolated_scope(tmp_path):
    resource_root = tmp_path / "resources"
    resource_root.mkdir()
    source = resource_root / "feedback-assessment.md"
    source.write_text(
        "# 反馈与评估\n\n使用事实、行为和影响组织反馈。",
        encoding="utf-8",
    )

    class Parser:
        @staticmethod
        def parse_file(path):
            return path.read_text(encoding="utf-8"), {
                "parser": "text",
                "structured_blocks": [],
                "images": [],
            }

    class ImageAnalysis:
        @staticmethod
        def analyze_sync(*, text, blocks, images, source_id):
            return SimpleNamespace(
                text=text,
                blocks=blocks,
                images=images,
                stats={},
                warnings=[],
            )

    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(
        kb_index_version="v2",
        effective_embedding_model="embedding-model",
        embedding_dimensions=2,
        kb_chunk_size=1400,
        kb_chunk_overlap=160,
        document_vision_model="vision-model",
        collection_name_for_scope=lambda scope: f"kb_{scope}",
    )
    manager.parser = Parser()
    manager.image_analysis_service = ImageAnalysis()
    manager._image_analysis_summary = {}
    manager._image_analysis_warnings = []

    chunks_by_scope, documents = manager._build_chunks(
        resource_root,
        scope_override="resources",
        source_prefix="resources",
    )

    assert set(chunks_by_scope) == {"resources"}
    assert chunks_by_scope["resources"][0].source_id == "resources/feedback-assessment.md"
    assert chunks_by_scope["resources"][0].title == "反馈与评估"
    assert documents[0]["scope"] == "resources"


def test_rebuild_embedding_failure_does_not_modify_database(monkeypatch, tmp_path):
    import backend.vectorstore.index_manager as index_manager_module

    raw_root = tmp_path / "kb_raw"
    raw_root.mkdir()
    (raw_root / "guide.md").write_text("core", encoding="utf-8")
    chunks_by_scope = {
        "career": [_chunk("career-1", 0.0, scope="career")],
        "feedback": [_chunk("feedback-1", 0.0, scope="feedback")],
    }
    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(
        data_dir=tmp_path,
        kb_index_version="v3",
        collection_name_for_scope=lambda scope: f"kb_{scope}",
        effective_embedding_model="embedding-model",
        embedding_dimensions=2,
    )
    manager._image_analysis_summary = {}
    manager._image_analysis_warnings = []
    manager._reset_image_analysis_summary = lambda: None
    manager._build_chunks = lambda raw_dir, **kwargs: (chunks_by_scope, [])

    def embed(chunks):
        if chunks[0].scope == "feedback":
            raise RuntimeError("embedding failed")
        return chunks, [[1.0, 0.0]]

    manager._embed_chunks = embed
    database_writes: list[str] = []

    class Store:
        def __init__(self, **kwargs):
            database_writes.append("client_created")

    monkeypatch.setattr(index_manager_module, "PGVectorClient", Store)

    with pytest.raises(RuntimeError, match="embedding failed"):
        manager.rebuild(dataset="core", exact=True)

    assert database_writes == []


def test_rebuild_rejects_empty_input_before_database_access(monkeypatch, tmp_path):
    import backend.vectorstore.index_manager as index_manager_module

    raw_root = tmp_path / "kb_raw"
    raw_root.mkdir()
    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(data_dir=tmp_path)
    manager._reset_image_analysis_summary = lambda: None
    manager._build_chunks = lambda raw_dir: ({}, [])
    database_writes: list[str] = []

    class Store:
        def __init__(self, **kwargs):
            database_writes.append("client_created")

    monkeypatch.setattr(index_manager_module, "PGVectorClient", Store)

    with pytest.raises(RuntimeError, match="没有可索引的 KB 文件"):
        manager.rebuild(dataset="core", exact=True)

    assert database_writes == []


def test_prune_knowledge_collections_removes_stale_chunks_and_orphan_documents():
    statements: list[tuple[str, object]] = []
    dropped_indexes: list[str] = []

    class TransactionConnection:
        def execute(self, statement, params=None):
            rendered = str(statement)
            statements.append((rendered, params))
            if "SELECT DISTINCT collection_name" in rendered:
                return _Cursor(
                    rows=[
                        {
                            "collection_name": "kb_bosch",
                            "scope": "bosch",
                            "dimension": 1024,
                        }
                    ]
                )
            if "DELETE FROM kb_chunks" in rendered:
                return _Cursor(rowcount=14)
            if "DELETE FROM kb_documents" in rendered:
                return _Cursor(rowcount=2)
            return _Cursor()

    @contextmanager
    def transaction_connection():
        yield TransactionConnection()

    @contextmanager
    def autocommit_connection():
        yield object()

    repository = object.__new__(PostgresRepository)
    repository.connection = transaction_connection
    repository.autocommit_connection = autocommit_connection
    repository._index_state = lambda connection, name: (True, True)
    repository._drop_index_concurrently = (
        lambda connection, name: dropped_indexes.append(name)
    )

    summary = repository.prune_knowledge_collections(
        {"kb_feedback", "kb_career"}
    )

    expected_hnsw = repository.hnsw_index_name("kb_bosch", "bosch", 1024)
    assert summary == {
        "pruned_collections": ["kb_bosch"],
        "pruned_chunks": 14,
        "pruned_documents": 2,
        "dropped_hnsw_indexes": [expected_hnsw],
    }
    assert dropped_indexes == [expected_hnsw]
    assert statements[0][1] == (["kb_career", "kb_feedback"],)
    assert "DELETE FROM kb_chunks" in statements[1][0]
    assert "DELETE FROM kb_documents" in statements[2][0]
    assert not any("document_image_analyses" in sql for sql, _ in statements)


def test_prune_knowledge_collections_rejects_empty_active_set():
    repository = object.__new__(PostgresRepository)

    with pytest.raises(ValueError, match="at least one active collection"):
        repository.prune_knowledge_collections(set())


def test_small_scope_dense_query_uses_safe_exact_similarity_order():
    statements: list[tuple[str, tuple]] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append((str(statement), tuple(params or ())))
            return _Cursor()

    client = object.__new__(PGVectorClient)
    client.collection_name = "kb_general"
    client.settings = SimpleNamespace(postgres_create_hnsw_index=True)
    rows = client._dense_rows(
        Connection(),
        vector="[1.0,0.0]",
        dimension=2,
        top_k=3,
        where_sql=" AND (c.scope = %s)",
        filter_params=["general"],
        profile_id="profile-a",
        build_id="00000000-0000-0000-0000-000000000001",
        use_ann=False,
        scope="general",
    )

    sql, params = statements[0]
    assert rows == []
    assert "MATERIALIZED" not in sql
    assert "FROM kb_chunk_embeddings e" in sql
    assert "JOIN kb_chunks c" in sql
    assert "ORDER BY score DESC" in sql
    assert params == (
        "[1.0,0.0]",
        "profile-a",
        "00000000-0000-0000-0000-000000000001",
        "kb_general",
        "general",
        "general",
        3,
    )


def test_retrieval_unit_dense_query_honors_exact_and_ann_modes():
    statements: list[tuple[str, tuple]] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append((str(statement), tuple(params or ())))
            return _Cursor()

    client = object.__new__(PGVectorClient)
    client.collection_name = "kb_employee"
    client.settings = SimpleNamespace(postgres_create_hnsw_index=True)
    common = {
        "conn": Connection(),
        "vector": "[1.0,0.0]",
        "dimension": 2560,
        "top_k": 3,
        "where_sql": " AND (c.scope = %s)",
        "filter_params": ["employee"],
        "profile_id": "profile-a",
        "build_id": "00000000-0000-0000-0000-000000000001",
        "scope": "employee",
    }

    exact_rows = client._dense_retrieval_unit_rows(
        **common,
        use_ann=False,
    )
    ann_rows = client._dense_retrieval_unit_rows(
        **common,
        use_ann=True,
    )

    exact_sql, exact_params = statements[0]
    ann_sql, ann_params = statements[1]
    assert exact_rows == []
    assert ann_rows == []
    assert "FROM kb_retrieval_unit_embeddings e" in exact_sql
    assert "ORDER BY score DESC" in exact_sql
    assert "::halfvec(2560)" not in exact_sql
    assert "1 - (e.embedding::halfvec(2560)" in ann_sql
    assert (
        "ORDER BY e.embedding::halfvec(2560)\n"
        "                         <=> %s::halfvec(2560)"
    ) in ann_sql
    assert "ORDER BY score DESC" not in ann_sql
    assert exact_params[-1] == 48
    assert ann_params.count("[1.0,0.0]") == 2
    assert ann_params[-1] == 48


def test_multi_scope_exact_dense_query_avoids_materialized_vector_ctes():
    statements: list[tuple[str, tuple]] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append((str(statement), tuple(params or ())))
            return _Cursor()

    client = object.__new__(PGVectorClient)
    requests = [
        HybridScopeSearchRequest(
            collection_name="kb_general",
            scope="general",
            metadata_filter={"scope": "general"},
            use_ann=False,
            lexical_enabled=False,
        ),
        HybridScopeSearchRequest(
            collection_name="kb_performance",
            scope="performance",
            metadata_filter={"scope": "performance"},
            use_ann=False,
            lexical_enabled=False,
        ),
    ]

    rows = client._dense_multi_scope_rows(
        Connection(),
        vector="[1.0,0.0]",
        dimension=2,
        top_k=3,
        requests=requests,
        profile_id="profile-a",
        build_id="00000000-0000-0000-0000-000000000001",
        ann_flags=[False, False],
    )

    sql, params = statements[0]
    assert rows == [[], []]
    assert "MATERIALIZED" not in sql
    assert sql.count("ORDER BY score DESC") == 2
    assert params == (
        "[1.0,0.0]",
        "profile-a",
        "00000000-0000-0000-0000-000000000001",
        "kb_general",
        "general",
        "general",
        3,
        "[1.0,0.0]",
        "profile-a",
        "00000000-0000-0000-0000-000000000001",
        "kb_performance",
        "performance",
        "performance",
        3,
    )


def test_multi_scope_retrieval_unit_dense_honors_each_ann_flag():
    statements: list[tuple[str, tuple]] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append((str(statement), tuple(params or ())))
            return _Cursor()

    client = object.__new__(PGVectorClient)
    requests = [
        HybridScopeSearchRequest(
            collection_name="kb_general",
            scope="general",
            metadata_filter={"scope": "general"},
            use_ann=False,
            lexical_enabled=False,
        ),
        HybridScopeSearchRequest(
            collection_name="kb_employee",
            scope="employee",
            metadata_filter={"scope": "employee"},
            use_ann=True,
            lexical_enabled=False,
        ),
    ]

    rows = client._dense_retrieval_unit_multi_scope_rows(
        Connection(),
        vector="[1.0,0.0]",
        dimension=2560,
        top_k=3,
        requests=requests,
        profile_id="profile-a",
        build_id="00000000-0000-0000-0000-000000000001",
        ann_flags=[False, True],
    )

    sql, params = statements[0]
    assert rows == [[], []]
    assert sql.count("ORDER BY score DESC") == 1
    assert sql.count("ORDER BY e.embedding::halfvec(2560)") == 1
    assert sql.count("FROM kb_retrieval_unit_embeddings e") == 2
    assert params == (
        "[1.0,0.0]",
        "profile-a",
        "00000000-0000-0000-0000-000000000001",
        "kb_general",
        "general",
        "general",
        48,
        "[1.0,0.0]",
        "profile-a",
        "00000000-0000-0000-0000-000000000001",
        "kb_employee",
        "employee",
        "employee",
        "[1.0,0.0]",
        48,
    )


def test_metadata_filters_use_jsonb_containment_and_expand_list_values():
    client = object.__new__(PGVectorClient)

    where, params = client._where_from_filter(
        {
            "scope": "general",
            "intent_id": ["improvement", "recognition"],
            "severity": 2,
        }
    )

    assert "scope = %s" in where
    assert where.count("metadata @> %s::jsonb") == 3
    assert " OR " in where
    assert params == [
        "general",
        '{"intent_id": "improvement"}',
        '{"intent_id": "recognition"}',
        '{"severity": 2}',
    ]


def test_partial_index_ddl_contains_collection_scope_and_dimension_predicates():
    statements = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append(statement)
            if "SELECT TRUE AS present" in str(statement):
                return _Cursor(row=None)
            return _Cursor()

    repository = object.__new__(PostgresRepository)
    connection = Connection()
    hnsw_name = repository._create_partial_hnsw_index(
        connection,
        collection_name="kb_general",
        scope="general",
        dimension=1024,
        profile_id="profile-a",
        build_id="00000000-0000-0000-0000-000000000001",
    )
    unit_hnsw_name = repository._create_partial_retrieval_unit_hnsw_index(
        connection,
        collection_name="kb_employee",
        scope="employee",
        dimension=2560,
        profile_id="profile-a",
        build_id="00000000-0000-0000-0000-000000000001",
    )
    bm25_name = repository._create_partial_bm25_index(
        connection,
        collection_name="kb_general",
        scope="general",
    )

    rendered = [
        statement.as_string()
        for statement in statements
        if hasattr(statement, "as_string")
    ]
    hnsw_sql = next(
        sql
        for sql in rendered
        if "ON public.kb_chunk_embeddings" in sql
    )
    unit_hnsw_sql = next(
        sql
        for sql in rendered
        if "ON public.kb_retrieval_unit_embeddings" in sql
    )
    bm25_sql = next(sql for sql in rendered if "USING bm25" in sql)
    assert hnsw_name in hnsw_sql
    assert "CREATE INDEX CONCURRENTLY" in hnsw_sql
    assert "ON public.kb_chunk_embeddings" in hnsw_sql
    assert "profile_id = 'profile-a'" in hnsw_sql
    assert "build_id = '00000000-0000-0000-0000-000000000001'::uuid" in hnsw_sql
    assert "collection_name = 'kb_general'" in hnsw_sql
    assert "scope = 'general'" in hnsw_sql
    assert "vector_dims(embedding) = 1024" in hnsw_sql
    assert unit_hnsw_name != hnsw_name
    assert unit_hnsw_name in unit_hnsw_sql
    assert "CREATE INDEX CONCURRENTLY" in unit_hnsw_sql
    assert "embedding::halfvec(2560)" in unit_hnsw_sql
    assert "halfvec_cosine_ops" in unit_hnsw_sql
    assert "profile_id = 'profile-a'" in unit_hnsw_sql
    assert (
        "build_id = '00000000-0000-0000-0000-000000000001'::uuid"
        in unit_hnsw_sql
    )
    assert "collection_name = 'kb_employee'" in unit_hnsw_sql
    assert "scope = 'employee'" in unit_hnsw_sql
    assert "vector_dims(embedding) = 2560" in unit_hnsw_sql
    assert bm25_name in bm25_sql
    assert "CREATE INDEX CONCURRENTLY" in bm25_sql
    assert "collection_name = 'kb_general'" in bm25_sql
    assert "scope = 'general'" in bm25_sql


def test_index_maintenance_builds_hnsw_for_large_retrieval_unit_scope():
    profile = EmbeddingProfileState(
        profile_id="profile-a",
        model_name="embedding",
        dimensions=2560,
        status="ready",
        active_build_id="00000000-0000-0000-0000-000000000001",
        source_fingerprint="fingerprint",
        chunk_count=900,
        vector_count=20_900,
        current_chunk_count=900,
    )
    chunk_spec = {
        "profile_id": profile.profile_id,
        "build_id": profile.active_build_id,
        "collection_name": "kb_employee",
        "scope": "employee",
        "dimension": 2560,
        "row_count": 900,
    }
    unit_spec = {**chunk_spec, "row_count": 20_000}
    unit_hnsw_name = PostgresRepository.retrieval_unit_hnsw_index_name(
        "kb_employee",
        "employee",
        2560,
        profile_id=profile.profile_id,
        build_id=profile.active_build_id,
    )
    created_unit_indexes: list[dict] = []

    class Connection:
        def execute(self, statement, params=None):
            rendered = str(statement)
            if "JOIN kb_chunk_embeddings e" in rendered:
                return _Cursor(
                    rows=[
                        {
                            **chunk_spec,
                            "active_build_id": profile.active_build_id,
                        }
                    ]
                )
            if "JOIN kb_retrieval_unit_embeddings e" in rendered:
                return _Cursor(
                    rows=[
                        {
                            **unit_spec,
                            "active_build_id": profile.active_build_id,
                        }
                    ]
                )
            if (
                "tablename = 'kb_retrieval_unit_embeddings'" in rendered
                and "FROM pg_indexes" in rendered
            ):
                return _Cursor(rows=[{"indexname": unit_hnsw_name}])
            return _Cursor()

    @contextmanager
    def autocommit_connection():
        yield Connection()

    repository = object.__new__(PostgresRepository)
    repository.settings = SimpleNamespace(
        postgres_create_hnsw_index=True,
        postgres_ann_min_rows=10_000,
    )
    repository.retrieval_index_specs = lambda _profile: [chunk_spec]
    repository.retrieval_unit_index_specs = lambda _profile: [unit_spec]
    repository.autocommit_connection = autocommit_connection
    repository._index_state = lambda _conn, _name: (False, False)
    repository._create_partial_retrieval_unit_hnsw_index = (
        lambda _conn, **kwargs: (
            created_unit_indexes.append(kwargs) or unit_hnsw_name
        )
    )
    repository._create_partial_bm25_index = (
        lambda _conn, **_kwargs: "idx-bm25"
    )
    repository._create_retrieval_unit_bm25_index = (
        lambda _conn: "idx-unit-bm25"
    )
    repository._index_is_valid = lambda _conn, _name: True
    repository._drop_index_concurrently = lambda _conn, _name: None

    summary = repository.maintain_retrieval_indexes(
        rebuild_lexical=False,
        drop_legacy_hnsw=False,
        profile=profile,
    )

    assert summary["hnsw_indexes"] == []
    assert summary["retrieval_unit_hnsw_indexes"] == [unit_hnsw_name]
    assert summary["skipped_ann_scopes"][0]["row_count"] == 900
    assert created_unit_indexes == [
        {
            "collection_name": "kb_employee",
            "scope": "employee",
            "dimension": 2560,
            "profile_id": profile.profile_id,
            "build_id": profile.active_build_id,
        }
    ]


def test_employee_and_organization_scopes_force_hnsw_below_global_row_threshold():
    repository = object.__new__(PostgresRepository)
    repository.settings = SimpleNamespace(
        postgres_ann_min_rows=10_000,
        postgres_ann_force_scopes="employee,organization_unit",
    )

    assert repository._ann_min_rows_for_scope("employee") == 1
    assert repository._ann_min_rows_for_scope("organization_unit") == 1
    assert repository._ann_min_rows_for_scope("general") == 10_000


def test_extension_version_compatibility_allows_newer_pgvector_patch_only():
    assert PostgresRepository.extension_version_is_supported("vector", "0.8.2")
    assert PostgresRepository.extension_version_is_supported("vector", "0.8.3")
    assert not PostgresRepository.extension_version_is_supported("vector", "0.8.1")
    assert not PostgresRepository.extension_version_is_supported("vector", "0.9.0")
    assert PostgresRepository.extension_version_is_supported("vchord", "1.1.1")
    assert not PostgresRepository.extension_version_is_supported("vchord", "1.1.2")


def test_installed_extension_versions_reports_actual_database_values():
    rows = [
        {"extname": "vector", "extversion": "0.8.3"},
        {"extname": "vchord", "extversion": "1.1.1"},
    ]

    class Connection:
        def execute(self, statement, params=None):
            assert "FROM pg_extension" in str(statement)
            assert params == (list(PostgresRepository.REQUIRED_EXTENSION_VERSIONS),)
            return _Cursor(rows=rows)

    @contextmanager
    def connection():
        yield Connection()

    repository = object.__new__(PostgresRepository)
    repository.connection = connection

    assert repository.installed_extension_versions() == {
        "vector": "0.8.3",
        "vchord": "1.1.1",
    }


def test_query_config_has_expected_scoped_task_counts():
    path = (
        Path(__file__).resolve().parents[1]
        / "backend"
        / "business_config"
        / "query.yaml"
    )
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    queries = config["queries"]
    expected_counts = {
        "guidance_start": 8,
        "guidance_emotion": 3,
        "guidance_requirement": 9,
        "guidance_plan": 8,
        "opening_evaluation": 11,
        "output_expectations_evaluation": 16,
        "development_plan_evaluation": 22,
        "emotion_evaluation": 3,
        "employee_response": 3,
        "resource_chat": 1,
    }

    for agent_name, expected_count in expected_counts.items():
        templates = queries[agent_name]["query_templates"]
        assert all(
            set(template) >= {"template", "scopes", "weight"}
            for template in templates
        )
        assert sum(len(template["scopes"]) for template in templates) == expected_count

    assert "guidance" not in queries
    assert "guidance_culture" not in queries


def test_query_config_prioritizes_complete_dimension_evidence():
    path = (
        Path(__file__).resolve().parents[1]
        / "backend"
        / "business_config"
        / "query.yaml"
    )
    queries = yaml.safe_load(path.read_text(encoding="utf-8"))["queries"]

    for agent_name in (
        "guidance_requirement",
        "output_expectations_evaluation",
    ):
        config = queries[agent_name]
        assert config["citation_scope_minimums"]["feedback"] == 2
        assert config["citation_scope_preferred_terms"]["job_level"] == [
            "结果",
            "能力",
            "行为",
        ]
        assert config["citation_scope_preferred_terms"]["feedback"] == [
            "目标与结果对齐",
            "实际结果",
            "差距 / 偏差",
            "只说事实",
        ]

    for agent_name in ("guidance_plan", "development_plan_evaluation"):
        assert list(
            queries[agent_name]["generation_prefix_scope_targets"]
        ) == [
            "job_level",
            "feedback",
            "career",
            "culture",
            "development_dialog",
        ]

    for agent_name in ("guidance_emotion", "emotion_evaluation"):
        preferred = queries[agent_name]["citation_scope_preferred_terms"][
            "emotion"
        ]
        assert "策略性的同理心" in preferred
        assert "共同解决问题" in preferred
        semantic_groups = queries[agent_name][
            "generation_prefix_scope_semantic_groups"
        ]["emotion"]
        assert len(semantic_groups) == 2
        assert "战术同理心" in semantic_groups[0]["required_terms"]
        assert "校准问题" in semantic_groups[1]["required_terms"]
        assert "目录" in semantic_groups[0]["downrank_terms"]
        assert "不用安慰" in semantic_groups[1]["downrank_terms"]
        assert "如何" not in semantic_groups[1]["preferred_terms"]
        assert "什么" not in semantic_groups[1]["preferred_terms"]
        assert "开放性的校准问题" in semantic_groups[1]["preferred_terms"]

    assert all(
        "generation_prefix_scope_semantic_groups" not in config
        for agent_name, config in queries.items()
        if agent_name not in {"guidance_emotion", "emotion_evaluation"}
    )


def test_legacy_string_templates_keep_cartesian_scope_compatibility():
    service = object.__new__(RetrievalService)

    specs = service._render_query_specs(
        templates=["query one", "query two"],
        context={},
        fallback_scopes=["general", "performance"],
        available_scopes={"general", "performance"},
    )

    assert len(specs) == 2
    assert [spec.scopes for spec in specs] == [
        ["general", "performance"],
        ["general", "performance"],
    ]


def test_career_elements_query_is_rendered_only_when_applicable():
    path = (
        Path(__file__).resolve().parents[1]
        / "backend"
        / "business_config"
        / "query.yaml"
    )
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    agent_cfg = config["queries"]["guidance_plan"]
    service = object.__new__(RetrievalService)
    common_context = {
        "profile": {"level": "G9"},
        "intent": {"name": "发展型反馈"},
        "current_career_elements": ["Cross Function"],
    }
    arguments = {
        "templates": agent_cfg["query_templates"],
        "fallback_scopes": agent_cfg["scopes"],
        "available_scopes": set(agent_cfg["scopes"]),
    }

    without_career = service._render_query_specs(
        **arguments,
        context={**common_context, "career_elements_applicable": False},
    )
    with_career = service._render_query_specs(
        **arguments,
        context={**common_context, "career_elements_applicable": True},
    )

    assert len(with_career) == len(without_career) + 1
    assert not any("Career Elements" in spec.query for spec in without_career)
    assert any(
        spec.scopes == ["career"]
        and "Career Elements" in spec.query
        and "Cross Function" in spec.query
        for spec in with_career
    )


def test_process_search_limiter_bounds_concurrency(monkeypatch):
    import backend.services.retrieval_service as retrieval_module

    active = 0
    max_active = 0
    lock = threading.Lock()

    class Client:
        def __init__(self, collection_name, embedding_service=None):
            self.collection_name = collection_name

        def search_by_embedding(self, query_embedding, top_k=5, metadata_filter=None):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.02)
                return []
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(retrieval_module, "PGVectorClient", Client)
    service = object.__new__(RetrievalService)
    service.settings = SimpleNamespace(
        collection_name_for_scope=lambda scope: f"kb_{scope}"
    )
    service.embedding_service = object()
    service._search_limiter = threading.BoundedSemaphore(64)

    with ThreadPoolExecutor(max_workers=100) as executor:
        futures = [
            executor.submit(
                service._search_scope,
                query_index=index,
                query="query",
                query_embedding=[1.0],
                scope="general",
                dense_top_k=1,
                lexical_top_k=1,
                metadata_filter={},
                template_weight=1.0,
                dense_weight=1.0,
                lexical_weight=1.0,
                hybrid_enabled=False,
            )
            for index in range(100)
        ]
        for future in futures:
            future.result()

    assert max_active == 64
    assert RetrievalService._get_search_limiter(64) is RetrievalService._get_search_limiter(64)


def test_zero_process_search_limit_uses_a_noop_context():
    with RetrievalService._get_search_limiter(0):
        pass

    assert 0 not in RetrievalService._search_limiters


@pytest.mark.asyncio
async def test_zero_global_search_limit_skips_distributed_admission():
    class Coordinator:
        @staticmethod
        def lease(*_args, **_kwargs):
            raise AssertionError("Unlimited RAG DB search must not request a lease.")

    service = object.__new__(RetrievalService)
    service.settings = SimpleNamespace(rag_global_db_search_max_concurrency=0)
    service.coordinator = Coordinator()

    async with service._global_lease(
        "rag-db",
        "rag_global_db_search_max_concurrency",
        98,
    ):
        pass


@pytest.mark.asyncio
async def test_process_rerank_limiter_is_shared_across_service_instances():
    first = object.__new__(RetrievalService)
    second = object.__new__(RetrievalService)
    first.settings = SimpleNamespace(rag_rerank_max_concurrency=46)
    second.settings = SimpleNamespace(rag_rerank_max_concurrency=46)

    assert first._get_rerank_limiter() is second._get_rerank_limiter()


def test_pgvector_clients_are_cached_per_collection(monkeypatch):
    import backend.services.retrieval_service as retrieval_module

    created: list[str] = []

    class Client:
        def __init__(self, *, collection_name, embedding_service):
            created.append(collection_name)
            self.collection_name = collection_name
            self.embedding_service = embedding_service

    monkeypatch.setattr(retrieval_module, "PGVectorClient", Client)
    service = object.__new__(RetrievalService)
    service.embedding_service = object()

    first = service._store_for_collection("kb_general")
    repeated = service._store_for_collection("kb_general")
    other = service._store_for_collection("kb_career")

    assert first is repeated
    assert other is not first
    assert created == ["kb_general", "kb_career"]


def test_rrf_rewards_documents_supported_by_multiple_branches():
    dense = _RankedBranch(
        branch_id="0:general:dense",
        modality="dense",
        scope="general",
        weight=1.0,
        chunks=[_chunk("only-dense", 0.99), _chunk("supported", 0.10)],
    )
    lexical = _RankedBranch(
        branch_id="0:general:bm25",
        modality="bm25",
        scope="general",
        weight=1.0,
        chunks=[_chunk("supported", 0.01)],
    )

    result = RetrievalService._rrf_fuse(
        [dense, lexical],
        rrf_k=60,
        top_n=10,
    )

    assert [chunk.chunk_id for chunk in result] == ["supported", "only-dense"]
    evidence = result[0].metadata["retrieval_fusion"]["evidence"]
    assert [item["rank"] for item in evidence] == [2, 1]
    assert {item["modality"] for item in evidence} == {"dense", "bm25"}


def test_rrf_aggregates_parent_units_by_combined_contribution():
    def parent_with_units(
        units: list[tuple[str, int]],
    ) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id="shared-parent",
            source_id="career/career.md",
            title="Career Elements",
            scope="career",
            text="complete parent text returned once",
            score=1.0,
            metadata={
                "retrieval_units": [
                    {
                        "unit_id": unit_id,
                        "unit_type": "list",
                        "unit_index": unit_rank,
                        "text": f"text {unit_id}",
                        "search_text": f"context {unit_id}",
                        "retrieval_rank": unit_rank,
                    }
                    for unit_id, unit_rank in units
                ]
            },
        )

    dense = _RankedBranch(
        branch_id="0:career:dense",
        modality="dense",
        scope="career",
        weight=1.0,
        chunks=[
            parent_with_units(
                [
                    ("unit-a", 1),
                    ("unit-b", 3),
                    ("unit-d", 4),
                    ("unit-e", 5),
                    ("unit-f", 6),
                    ("unit-g", 7),
                    ("unit-h", 8),
                    ("unit-i", 9),
                    ("unit-j", 10),
                    ("unit-k", 11),
                ]
            )
        ],
    )
    lexical = _RankedBranch(
        branch_id="0:career:bm25",
        modality="bm25",
        scope="career",
        weight=1.0,
        chunks=[parent_with_units([("unit-b", 1), ("unit-c", 2)])],
    )

    result = RetrievalService._rrf_fuse(
        [dense, lexical],
        rrf_k=60,
        top_n=8,
    )

    assert len(result) == 1
    assert result[0].text == "complete parent text returned once"
    units = result[0].metadata["retrieval_units"]
    assert [unit["unit_id"] for unit in units] == [
        "unit-b",
        "unit-a",
        "unit-c",
        "unit-d",
        "unit-e",
        "unit-f",
        "unit-g",
        "unit-h",
    ]
    assert len({unit["unit_id"] for unit in units}) == 8
    assert {"unit-i", "unit-j", "unit-k"}.isdisjoint(
        unit["unit_id"] for unit in units
    )
    assert units[0]["rrf_contribution"] == pytest.approx(
        1 / 63 + 1 / 61
    )
    assert result[0].metadata["retrieval_unit"]["unit_id"] == "unit-b"
    assert result[0].metadata["retrieval_fusion"]["first_stage_rank"] == 1
    assert result[0].metadata["search_text"].index("context unit-b") < (
        result[0].metadata["search_text"].index("context unit-a")
    )


def test_reranker_scores_units_separately_then_returns_complete_parent():
    structured_parent = RetrievedChunk(
        chunk_id="parent-structured",
        source_id="job_level/levels.md",
        title="Job levels",
        scope="job_level",
        text=(
            "## Levels\n\n"
            "G8 supports a team.\n\n"
            "G9 delivers cross-team impact."
        ),
        score=0.03,
        metadata={
            "retrieval_units": [
                {
                    "unit_id": "unit-g8",
                    "unit_type": "table",
                    "unit_index": 0,
                    "text": "G8 supports a team.",
                    "search_text": "Section: Levels\nG8 supports a team.",
                    "rrf_contribution": 0.01,
                    "metadata": {
                        "semantic_group_id": "levels",
                        "semantic_item_id": "g8",
                        "next_unit_text": (
                            "G9 delivers cross-team impact."
                        ),
                    },
                },
                {
                    "unit_id": "unit-g9",
                    "unit_type": "table",
                    "unit_index": 1,
                    "text": "G9 delivers cross-team impact.",
                    "search_text": (
                        "Section: Levels\n"
                        "G9 delivers cross-team impact."
                    ),
                    "rrf_contribution": 0.02,
                    "exact_facts": [
                        {
                            "fact_type": "business_code",
                            "normalized_value": "g9",
                            "source_quote": "G9",
                        }
                    ],
                    "metadata": {
                        "semantic_group_id": "levels",
                        "semantic_item_id": "g9",
                        "previous_unit_text": "G8 supports a team.",
                    },
                },
            ]
        },
    )
    plain_parent = _chunk("plain-parent", 0.02)

    unit_candidates = RetrievalService._expand_unit_rerank_candidates(
        [structured_parent, plain_parent]
    )

    assert len(unit_candidates) == 3
    assert {
        candidate.metadata["rerank_unit"]["unit_id"]
        for candidate in unit_candidates
    } == {"unit-g8", "unit-g9", "parent:plain-parent"}
    g9_candidate = next(
        candidate
        for candidate in unit_candidates
        if candidate.metadata["rerank_unit"]["unit_id"] == "unit-g9"
    )
    assert "Exact matched facts:" in g9_candidate.metadata["search_text"]
    assert "business_code | g9 | G9" in g9_candidate.metadata["search_text"]
    assert "Adjacent context only" in g9_candidate.metadata["search_text"]
    assert "[Previous unit]\nG8 supports a team." in g9_candidate.metadata[
        "search_text"
    ]

    for candidate in unit_candidates:
        unit_id = candidate.metadata["rerank_unit"]["unit_id"]
        candidate.score = {
            "unit-g8": 0.20,
            "unit-g9": 0.90,
            "parent:plain-parent": 0.80,
        }[unit_id]

    aggregated = RetrievalService._aggregate_unit_rerank_results(
        [structured_parent, plain_parent],
        unit_candidates,
    )

    assert [chunk.chunk_id for chunk in aggregated] == [
        "parent-structured",
        "plain-parent",
    ]
    assert aggregated[0].text == structured_parent.text
    expected_final_score = (1 / (60 + 1)) + (1 / (60 + 1))
    assert aggregated[0].score == pytest.approx(expected_final_score)
    assert aggregated[0].metadata["retrieval_unit"]["unit_id"] == "unit-g9"
    rerank_metadata = aggregated[0].metadata["retrieval_unit_rerank"]
    assert rerank_metadata["best_unit_id"] == "unit-g9"
    assert rerank_metadata["raw_rerank_score"] == pytest.approx(0.9)
    assert rerank_metadata["rrf_score"] == pytest.approx(0.03)
    assert rerank_metadata["final_score"] == pytest.approx(
        expected_final_score
    )
    assert rerank_metadata["first_stage_rank"] == 1
    assert rerank_metadata["rerank_rank"] == 1
    assert rerank_metadata["rank_fusion_k"] == 60
    assert rerank_metadata["rerank_rank_weight"] == pytest.approx(4 / 3)
    assert rerank_metadata["first_stage_rank_weight"] == pytest.approx(
        2 / 3
    )
    assert rerank_metadata["rerank_rank_component"] == pytest.approx(
        (4 / 3) / (60 + 1)
    )
    assert rerank_metadata["first_stage_rank_component"] == pytest.approx(
        (2 / 3) / (60 + 1)
    )
    assert rerank_metadata["evaluated_unit_count"] == 2
    assert "additional_unit_bonus" not in rerank_metadata


def test_unit_rerank_expansion_keeps_top_five_first_stage_units():
    contributions = [0.01, 0.08, 0.03, 0.07, 0.02, 0.06, 0.04, 0.05]
    parent = RetrievedChunk(
        chunk_id="parent-many-units",
        source_id="performance/many-units.md",
        title="Many units",
        scope="performance",
        text="complete parent text",
        score=0.08,
        metadata={
            "retrieval_units": [
                {
                    "unit_id": f"unit-{index}",
                    "unit_type": "paragraph",
                    "unit_index": index,
                    "text": f"unique unit text {index}",
                    "search_text": f"unique unit search text {index}",
                    "rrf_contribution": contribution,
                }
                for index, contribution in enumerate(contributions)
            ]
        },
    )

    candidates = RetrievalService._expand_unit_rerank_candidates([parent])

    assert [
        candidate.metadata["rerank_unit"]["unit_id"]
        for candidate in candidates
    ] == ["unit-1", "unit-3", "unit-5", "unit-7", "unit-6"]
    assert [candidate.score for candidate in candidates] == pytest.approx(
        [0.08, 0.07, 0.06, 0.05, 0.04]
    )


def _structured_parent(leaf_texts, *, title="Performance Management"):
    group_id = "performance-evidence"
    return RetrievedChunk(
        chunk_id="parent-performance",
        source_id="performance/ppm.md",
        title=title,
        scope="performance",
        text=f"## {title}\n\n" + "\n\n".join(leaf_texts),
        score=0.09,
        metadata={
            "atomic_structure": True,
            "semantic_group_id": group_id,
            "semantic_group_type": "ordered_sections",
            "semantic_group_title": title,
            "semantic_group_item_count": len(leaf_texts),
            "semantic_split": False,
            "heading_path": [title],
            "retrieval_fusion": {"first_stage_rank": 1},
            "retrieval_units": [
                {
                    "unit_id": f"leaf-{index}",
                    "unit_type": "ordered_sections",
                    "unit_index": index,
                    "text": text,
                    "search_text": text,
                    "rrf_contribution": 0.09 - index * 0.005,
                    "metadata": {
                        "semantic_group_id": group_id,
                        "semantic_item_id": f"item-{index}",
                        "source_start_line": 10 + index * 3,
                        "source_end_line": 12 + index * 3,
                    },
                }
                for index, text in enumerate(leaf_texts)
            ],
        },
    )


@pytest.mark.parametrize(
    ("leaf_text", "evidence_set"),
    [
        (
            "Cross-divisional experience; Cross-functional experience; "
            "International experience; Associate leadership experience; "
            "Project leadership experience",
            (
                "Cross-divisional experience",
                "Cross-functional experience",
                "International experience",
                "Associate leadership experience",
                "Project leadership experience",
            ),
        ),
        (
            "Commitment to WIN requires honoring QCD commitments.",
            ("Commitment to WIN", "QCD"),
        ),
    ],
)
def test_compound_candidate_is_not_added_when_one_leaf_is_complete(
    leaf_text,
    evidence_set,
):
    parent = _structured_parent([leaf_text, "unrelated sibling"])

    candidates = RetrievalService._expand_unit_rerank_candidates(
        [parent],
        include_semantic_group_candidate=True,
        compound_evidence_sets=(evidence_set,),
    )

    assert len(candidates) == 2
    assert all(
        candidate.metadata["rerank_unit"]["unit_type"]
        != "semantic_compound"
        for candidate in candidates
    )


def test_compound_candidate_uses_minimum_leaves_without_consuming_top_five():
    parent = _structured_parent(
        [
            "WHAT focuses on results; HOW focuses on behaviors.",
            "Calibration happens in People Conference.",
            "ASR is the annual salary review.",
            "unrelated evidence three",
            "unrelated evidence four",
            "unrelated evidence five",
        ]
    )

    candidates = RetrievalService._expand_unit_rerank_candidates(
        [parent],
        max_units_per_parent=5,
        include_semantic_group_candidate=True,
        compound_evidence_sets=(("WHAT", "HOW", "ASR"),),
    )

    atomic_candidates = [
        candidate
        for candidate in candidates
        if candidate.metadata["rerank_unit"]["unit_type"]
        != "semantic_compound"
    ]
    compound_candidates = [
        candidate
        for candidate in candidates
        if candidate.metadata["rerank_unit"]["unit_type"]
        == "semantic_compound"
    ]
    assert len(atomic_candidates) == 5
    assert len(compound_candidates) == 1
    compound = compound_candidates[0]
    assert all(
        term in compound.metadata["search_text"]
        for term in ("WHAT", "HOW", "ASR")
    )
    assert "Calibration happens" not in compound.metadata["search_text"]
    compound_metadata = compound.metadata["rerank_unit"]["metadata"]
    assert compound_metadata["compound_unit_ids"] == ["leaf-0", "leaf-2"]
    assert compound_metadata["evidence_set_complete"] is True
    assert compound_metadata["complete_semantic_group"] is False
    assert len(compound.metadata["rerank_unit"]["text"]) < len(parent.text)


def test_compound_candidate_is_skipped_instead_of_truncated():
    parent = _structured_parent(
        [
            "WHAT and HOW " + "x" * 700,
            "ASR " + "y" * 700,
        ]
    )

    candidates = RetrievalService._expand_unit_rerank_candidates(
        [parent],
        include_semantic_group_candidate=True,
        compound_evidence_sets=(("WHAT", "HOW", "ASR"),),
    )

    assert len(candidates) == 2
    assert all(
        candidate.metadata["rerank_unit"]["unit_type"]
        != "semantic_compound"
        for candidate in candidates
    )


def test_compound_candidate_preserves_focused_leaf_preference():
    parent = RetrievedChunk(
        chunk_id="parent-performance-single-item",
        source_id="performance/ppm.md",
        title="Performance Management",
        scope="performance",
        text=(
            "## Performance Management\n\nWHAT and HOW\n\nASR"
        ),
        score=0.08,
        metadata={
            "atomic_structure": True,
            "semantic_group_id": "performance-evidence",
            "semantic_group_type": "list",
            "semantic_group_title": "Performance Management",
            "semantic_group_item_count": 2,
            "heading_path": ["Performance Management"],
            "retrieval_fusion": {"first_stage_rank": 1},
            "retrieval_units": [
                {
                    "unit_id": "what-how",
                    "unit_type": "list",
                    "unit_index": 0,
                    "text": "WHAT and HOW",
                    "search_text": "WHAT and HOW",
                    "rrf_contribution": 0.08,
                    "metadata": {
                        "semantic_group_id": "performance-evidence",
                    },
                },
                {
                    "unit_id": "asr",
                    "unit_type": "list",
                    "unit_index": 1,
                    "text": "ASR",
                    "search_text": "ASR",
                    "rrf_contribution": 0.07,
                    "metadata": {
                        "semantic_group_id": "performance-evidence",
                    },
                },
            ],
        },
    )
    candidates = RetrievalService._expand_unit_rerank_candidates(
        [parent],
        include_semantic_group_candidate=True,
        compound_evidence_sets=(("WHAT", "HOW", "ASR"),),
    )
    for candidate in candidates:
        unit = candidate.metadata["rerank_unit"]
        score_key = (
            unit["unit_type"]
            if unit["unit_type"] == "semantic_compound"
            else unit["unit_id"]
        )
        candidate.score = {
            "what-how": 0.95,
            "asr": 0.20,
            "semantic_compound": 0.80,
        }[score_key]

    [result] = RetrievalService._aggregate_unit_rerank_results(
        [parent],
        candidates,
    )

    assert len(candidates) == 3
    assert result.metadata["retrieval_unit"]["unit_id"] == "what-how"
    assert result.metadata["retrieval_unit_rerank"]["best_unit_id"] == (
        "what-how"
    )
    assert len(result.metadata["retrieval_units"]) == 3


def test_parent_rank_fusion_ignores_additional_sibling_units():
    def aggregate(extra_sibling_scores: list[float]):
        parent_a = RetrievedChunk(
            chunk_id="parent-a",
            source_id="performance/a.md",
            title="Parent A",
            scope="performance",
            text="parent A",
            score=0.05,
            metadata={
                "retrieval_units": [
                    {
                        "unit_id": "a-best",
                        "unit_type": "paragraph",
                        "unit_index": 0,
                        "text": "unique A evidence",
                        "search_text": "unique A evidence",
                        "rrf_contribution": 0.05,
                    }
                ]
            },
        )
        parent_b = RetrievedChunk(
            chunk_id="parent-b",
            source_id="performance/b.md",
            title="Parent B",
            scope="performance",
            text="parent B",
            score=0.04,
            metadata={
                "retrieval_units": [
                    {
                        "unit_id": "b-best",
                        "unit_type": "paragraph",
                        "unit_index": 0,
                        "text": "unique B best evidence",
                        "search_text": "unique B best evidence",
                        "rrf_contribution": 0.05,
                    },
                    *[
                        {
                            "unit_id": f"b-sibling-{index}",
                            "unit_type": "paragraph",
                            "unit_index": index + 1,
                            "text": f"unique B sibling evidence {index}",
                            "search_text": (
                                f"unique B sibling evidence {index}"
                            ),
                            "rrf_contribution": 0.04 - (index * 0.01),
                        }
                        for index, _ in enumerate(extra_sibling_scores)
                    ],
                ]
            },
        )
        candidates = RetrievalService._expand_unit_rerank_candidates(
            [parent_a, parent_b]
        )
        raw_scores = {
            "a-best": 0.80,
            "b-best": 0.90,
            **{
                f"b-sibling-{index}": score
                for index, score in enumerate(extra_sibling_scores)
            },
        }
        for candidate in candidates:
            unit_id = candidate.metadata["rerank_unit"]["unit_id"]
            candidate.score = raw_scores[unit_id]
        return RetrievalService._aggregate_unit_rerank_results(
            [parent_a, parent_b],
            candidates,
        )

    without_siblings = aggregate([])
    with_siblings = aggregate([0.89, 0.88])

    assert [chunk.chunk_id for chunk in without_siblings] == [
        "parent-b",
        "parent-a",
    ]
    assert [chunk.chunk_id for chunk in with_siblings] == [
        "parent-b",
        "parent-a",
    ]
    without_sibling_metadata = {
        chunk.chunk_id: chunk.metadata["retrieval_unit_rerank"]
        for chunk in without_siblings
    }
    assert without_sibling_metadata["parent-a"]["first_stage_rank"] == 1
    assert without_sibling_metadata["parent-a"]["rerank_rank"] == 2
    assert without_sibling_metadata["parent-b"]["first_stage_rank"] == 2
    assert without_sibling_metadata["parent-b"]["rerank_rank"] == 1
    assert (
        without_sibling_metadata["parent-b"]["final_score"]
        > without_sibling_metadata["parent-a"]["final_score"]
    )
    for parent_id in ("parent-a", "parent-b"):
        baseline = next(
            chunk for chunk in without_siblings if chunk.chunk_id == parent_id
        )
        expanded = next(
            chunk for chunk in with_siblings if chunk.chunk_id == parent_id
        )
        baseline_metadata = baseline.metadata["retrieval_unit_rerank"]
        expanded_metadata = expanded.metadata["retrieval_unit_rerank"]
        assert expanded_metadata["rerank_rank"] == baseline_metadata[
            "rerank_rank"
        ]
        assert expanded_metadata["final_score"] == pytest.approx(
            baseline_metadata["final_score"]
        )
        assert expanded.score == pytest.approx(baseline.score)


def test_weighted_rank_fusion_still_preserves_strong_first_stage_evidence():
    first_stage_ids = ["parent-a", "parent-c", "parent-d", "parent-b"]
    raw_rerank_scores = {
        "parent-a": 0.80,
        "parent-b": 0.90,
        "parent-c": 0.70,
        "parent-d": 0.60,
    }
    parents = [
        RetrievedChunk(
            chunk_id=parent_id,
            source_id=f"performance/{parent_id}.md",
            title=parent_id,
            scope="performance",
            text=f"complete {parent_id} evidence",
            score=0.10 - index * 0.01,
            metadata={
                "retrieval_fusion": {"first_stage_rank": index + 1},
                "retrieval_units": [
                    {
                        "unit_id": f"{parent_id}-unit",
                        "unit_type": "paragraph",
                        "unit_index": 0,
                        "text": f"unique {parent_id} evidence",
                        "search_text": f"unique {parent_id} evidence",
                        "rrf_contribution": 0.10 - index * 0.01,
                    }
                ],
            },
        )
        for index, parent_id in enumerate(first_stage_ids)
    ]
    candidates = RetrievalService._expand_unit_rerank_candidates(parents)
    for candidate in candidates:
        parent_id = candidate.metadata["rerank_parent_chunk_id"]
        candidate.score = raw_rerank_scores[parent_id]

    aggregated = RetrievalService._aggregate_unit_rerank_results(
        parents,
        candidates,
    )

    assert [chunk.chunk_id for chunk in aggregated[:2]] == [
        "parent-a",
        "parent-b",
    ]
    parent_a = aggregated[0].metadata["retrieval_unit_rerank"]
    parent_b = aggregated[1].metadata["retrieval_unit_rerank"]
    assert parent_a["rerank_rank"] == 2
    assert parent_a["first_stage_rank"] == 1
    assert parent_b["rerank_rank"] == 1
    assert parent_b["first_stage_rank"] == 4
    assert parent_a["final_score"] > parent_b["final_score"]


@pytest.mark.parametrize(
    ("unit_type", "parent_metadata", "expected"),
    [
        ("paragraph", {}, False),
        ("list", {}, False),
        ("ordered_sections", {"oversized_atomic": False}, False),
        ("table", {}, True),
        ("ordered_sections", {"oversized_atomic": True}, True),
    ],
)
def test_reranker_only_keeps_adjacent_context_for_structural_continuity(
    unit_type,
    parent_metadata,
    expected,
):
    parent = RetrievedChunk(
        chunk_id=f"parent-{unit_type}",
        source_id="performance/example.md",
        title="Performance example",
        scope="performance",
        text="complete parent text",
        score=0.5,
        metadata={
            **parent_metadata,
            "retrieval_units": [
                {
                    "unit_id": f"unit-{unit_type}",
                    "unit_type": unit_type,
                    "unit_index": 1,
                    "text": "focused unit text",
                    "search_text": "focused unit search text",
                    "metadata": {
                        "previous_unit_text": "PREVIOUS_NEIGHBOR_MARKER",
                        "next_unit_text": "NEXT_NEIGHBOR_MARKER",
                    },
                }
            ],
        },
    )

    [candidate] = RetrievalService._expand_unit_rerank_candidates([parent])
    search_text = candidate.metadata["search_text"]

    assert ("Adjacent context only" in search_text) is expected
    assert ("PREVIOUS_NEIGHBOR_MARKER" in search_text) is expected
    assert ("NEXT_NEIGHBOR_MARKER" in search_text) is expected


def test_scope_minimums_replace_only_lowest_ranked_final_candidates():
    ranked = [
        _chunk(f"general-{index}", 1.0 - index / 100)
        for index in range(1, 10)
    ]
    ranked.extend(
        [
            _chunk("culture-evidence", 0.5, scope="culture"),
            _chunk("job-level-evidence", 0.4, scope="job_level"),
        ]
    )

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=8,
        scope_minimums={"culture": 1, "job_level": 1},
    )

    assert [chunk.chunk_id for chunk in selected] == [
        "general-1",
        "general-2",
        "general-3",
        "general-4",
        "general-5",
        "general-6",
        "culture-evidence",
        "job-level-evidence",
    ]


def _selection_chunk(
    chunk_id,
    score,
    *,
    source_id,
    scope="general",
    text="",
):
    return _chunk(chunk_id, score, scope=scope).model_copy(
        update={
            "source_id": source_id,
            "text": text or f"evidence {chunk_id}",
            "metadata": {
                "heading_path": [scope, chunk_id],
                "semantic_item_id": chunk_id,
            },
        }
    )


def test_generation_prefix_targets_keep_membership_and_scope_minimums():
    ranked = [
        _selection_chunk(
            "shared-1",
            1.0,
            source_id="general/shared.md",
        ),
        _selection_chunk(
            "shared-2",
            0.99,
            source_id="general/shared.md",
        ),
        _selection_chunk(
            "shared-3",
            0.98,
            source_id="general/shared.md",
        ),
        _selection_chunk(
            "alternative",
            0.97,
            source_id="general/alternative.md",
        ),
        *[
            _selection_chunk(
                f"general-{index}",
                0.90 - index / 100,
                source_id=f"general/source-{index}.md",
            )
            for index in range(5, 9)
        ],
        _selection_chunk(
            "culture-evidence",
            0.4,
            source_id="culture/values.md",
            scope="culture",
        ),
        _selection_chunk(
            "job-level-evidence",
            0.3,
            source_id="job_level/levels.md",
            scope="job_level",
            text="Bosch G9 requirements",
        ),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=8,
        scope_minimums={"culture": 1, "job_level": 1},
        scope_preferred_terms={"job_level": ("G9",)},
        diverse_prefix_k=3,
        generation_prefix_scope_targets={
            "culture": 1,
            "job_level": 1,
        },
        generation_prefix_fill_scopes=("general",),
    )

    assert [chunk.chunk_id for chunk in selected[:3]] == [
        "culture-evidence",
        "job-level-evidence",
        "shared-1",
    ]
    assert {
        chunk.chunk_id for chunk in selected
    } == {
        "shared-1",
        "shared-2",
        "shared-3",
        "alternative",
        "general-5",
        "general-6",
        "culture-evidence",
        "job-level-evidence",
    }
    assert sum(chunk.scope == "culture" for chunk in selected) == 1
    assert sum(chunk.scope == "job_level" for chunk in selected) == 1


def test_diverse_prefix_allows_uncovered_preferred_targets_to_break_source_cap():
    ranked = [
        _selection_chunk(
            "what",
            1.0,
            source_id="performance/ppm.md",
            scope="performance",
            text="WHAT is 70 percent",
        ),
        _selection_chunk(
            "how",
            0.99,
            source_id="performance/ppm.md",
            scope="performance",
            text="HOW is 30 percent",
        ),
        _selection_chunk(
            "asr",
            0.98,
            source_id="performance/ppm.md",
            scope="performance",
            text="ASR combines result and behavior",
        ),
        _selection_chunk(
            "alternative",
            0.97,
            source_id="performance/alternative.md",
            scope="performance",
        ),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=4,
        scope_minimums={"performance": 1},
        scope_preferred_terms={
            "performance": ("WHAT", "HOW", "ASR"),
        },
        diverse_prefix_k=3,
        generation_prefix_scope_targets={"performance": 3},
        generation_prefix_fill_scopes=("performance",),
    )

    assert [chunk.chunk_id for chunk in selected] == [
        "what",
        "how",
        "asr",
        "alternative",
    ]
    assert len({chunk.source_id for chunk in selected[:3]}) == 1


def test_diverse_prefix_keeps_original_order_without_an_alternative_source():
    ranked = [
        _selection_chunk(
            f"shared-{index}",
            1.0 - index / 100,
            source_id="general/shared.md",
        )
        for index in range(1, 6)
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=5,
        scope_minimums=None,
        diverse_prefix_k=3,
        generation_prefix_scope_targets={"general": 1},
        generation_prefix_fill_scopes=("general",),
    )

    assert [chunk.chunk_id for chunk in selected] == [
        "shared-1",
        "shared-2",
        "shared-3",
        "shared-4",
        "shared-5",
    ]


def test_generation_prefix_avoids_third_same_source_when_alternative_exists():
    ranked = [
        *[
            _selection_chunk(
                f"shared-{index}",
                1.0 - index / 100,
                source_id="general/shared.md",
            )
            for index in range(1, 4)
        ],
        _selection_chunk(
            "alternative",
            0.90,
            source_id="general/alternative.md",
        ),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=4,
        scope_minimums=None,
        diverse_prefix_k=3,
        generation_prefix_scope_targets={"general": 2},
        generation_prefix_fill_scopes=("general",),
    )

    assert [chunk.chunk_id for chunk in selected[:3]] == [
        "shared-1",
        "shared-2",
        "alternative",
    ]


def test_diverse_prefix_does_not_promote_conflicting_job_level_family():
    ranked = [
        _selection_chunk(
            "mercer",
            1.0,
            source_id="job_level/mercer.md",
            scope="job_level",
            text="Mercer P4 and M2 requirements",
        ),
        *[
            _selection_chunk(
                f"bosch-{index}",
                0.99 - index / 100,
                source_id="job_level/bosch.md",
                scope="job_level",
                text=f"Bosch G9 requirement {index}",
            )
            for index in range(1, 4)
        ],
        _selection_chunk(
            "general-alternative",
            0.95,
            source_id="general/feedback.md",
        ),
        *[
            _selection_chunk(
                f"general-{index}",
                0.90 - index / 100,
                source_id=f"general/source-{index}.md",
            )
            for index in range(6, 9)
        ],
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=8,
        scope_minimums={"job_level": 1},
        scope_preferred_terms={"job_level": ("G9",)},
        diverse_prefix_k=3,
        generation_prefix_scope_targets={"job_level": 1},
        generation_prefix_fill_scopes=("job_level", "general"),
    )

    assert [chunk.chunk_id for chunk in selected[:3]] == [
        "bosch-1",
        "bosch-2",
        "general-alternative",
    ]
    assert selected[0].chunk_id == "bosch-1"
    assert {chunk.chunk_id for chunk in selected} == {
        chunk.chunk_id for chunk in ranked
    }
    assert next(
        index
        for index, chunk in enumerate(selected)
        if chunk.chunk_id == "mercer"
    ) >= 3


def test_scope_selection_demotes_a_different_level_in_the_same_family():
    ranked = [
        _selection_chunk(
            "sl2-only",
            1.0,
            source_id="job_level/sl2.md",
            scope="job_level",
            text="SL2 High requirements",
        ),
        _selection_chunk(
            "all-levels",
            0.99,
            source_id="job_level/all.md",
            scope="job_level",
            text="G9 SL1 SL2 requirements table",
        ),
        *[
            _selection_chunk(
                f"general-{index}",
                0.98 - index / 100,
                source_id=f"general/{index}.md",
            )
            for index in range(1, 8)
        ],
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=8,
        scope_minimums={"job_level": 1},
        scope_preferred_terms={"job_level": ("SL1",)},
    )

    assert "all-levels" in {chunk.chunk_id for chunk in selected}
    assert "sl2-only" not in {chunk.chunk_id for chunk in selected}


def test_job_level_conflict_keeps_broad_table_with_applicable_row():
    broad_table = _selection_chunk(
        "all-levels",
        1.0,
        source_id="job_level/all.md",
        scope="job_level",
        text="G9 SL1 SL2 requirements table",
    )

    assert not RetrievalService._preferred_term_family_conflict(
        broad_table,
        ("SL1",),
    )


def test_job_level_conflict_uses_row_level_not_requirement_mentions():
    current_level_row = _selection_chunk(
        "g9-row",
        1.0,
        source_id="job_level/all.md",
        scope="job_level",
        text=(
            "职级: G9\n"
            "绩效等级: High\n"
            "能力：已经接近 SL1 层级"
        ),
    )

    assert RetrievalService._preferred_term_family_conflict(
        current_level_row,
        ("SL1",),
    )


def test_generation_prefix_is_disabled_without_explicit_scope_targets():
    ranked = [
        _selection_chunk(
            "general-1",
            1.0,
            source_id="general/shared.md",
        ),
        _selection_chunk(
            "general-2",
            0.99,
            source_id="general/shared.md",
        ),
        _selection_chunk(
            "general-3",
            0.98,
            source_id="general/shared.md",
        ),
        _selection_chunk(
            "culture",
            0.4,
            source_id="culture/values.md",
            scope="culture",
        ),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=4,
        scope_minimums={"culture": 1},
        diverse_prefix_k=3,
    )

    assert [chunk.chunk_id for chunk in selected] == [
        "general-1",
        "general-2",
        "general-3",
        "culture",
    ]


def test_generation_prefix_exact_target_can_break_same_source_cap():
    ranked = [
        _selection_chunk(
            "general-1",
            1.0,
            source_id="shared.md",
        ),
        _selection_chunk(
            "general-2",
            0.99,
            source_id="shared.md",
        ),
        _selection_chunk(
            "generic-culture",
            0.98,
            source_id="other.md",
            scope="culture",
            text="Generic company culture",
        ),
        _selection_chunk(
            "exact-culture",
            0.70,
            source_id="shared.md",
            scope="culture",
            text="Innovation to SHAPE",
        ),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=4,
        scope_minimums=None,
        scope_preferred_terms={
            "culture": ("innovation to shape",),
        },
        diverse_prefix_k=3,
        generation_prefix_scope_targets={"general": 2, "culture": 1},
        generation_prefix_fill_scopes=("general",),
    )

    assert [chunk.chunk_id for chunk in selected[:3]] == [
        "general-1",
        "general-2",
        "exact-culture",
    ]
    assert len({chunk.source_id for chunk in selected[:3]}) == 1


def test_generation_prefix_can_move_non_target_top_one_out_of_top_three():
    ranked = [
        _selection_chunk(
            "career-top",
            1.0,
            source_id="career/framework.md",
            scope="career",
        ),
        _selection_chunk(
            "career-second",
            0.99,
            source_id="career/framework.md",
            scope="career",
        ),
        _selection_chunk(
            "development",
            0.80,
            source_id="development/plan.md",
            scope="development_dialog",
        ),
        _selection_chunk(
            "culture",
            0.70,
            source_id="culture/values.md",
            scope="culture",
        ),
        _selection_chunk(
            "job-g9",
            0.60,
            source_id="job_level/g9.md",
            scope="job_level",
            text="Applicable G9 requirements",
        ),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=5,
        scope_minimums={
            "job_level": 1,
            "development_dialog": 1,
            "culture": 1,
        },
        scope_preferred_terms={"job_level": ("G9",)},
        diverse_prefix_k=3,
        generation_prefix_scope_targets={
            "job_level": 1,
            "development_dialog": 1,
            "culture": 1,
        },
        generation_prefix_fill_scopes=("development_dialog",),
    )

    assert [chunk.chunk_id for chunk in selected[:3]] == [
        "job-g9",
        "development",
        "culture",
    ]
    assert {chunk.chunk_id for chunk in selected} == {
        chunk.chunk_id for chunk in ranked
    }


def test_scope_minimum_prefers_matching_job_level_code_and_falls_back():
    ranked = [
        _chunk("general", 1.0),
        _chunk("mercer", 0.9, scope="job_level").model_copy(
            update={"text": "Mercer P4 and M2 requirements"}
        ),
        _chunk("general-2", 0.85),
        _chunk("bosch", 0.8, scope="job_level").model_copy(
            update={"text": "Bosch SL2 result competence behavior"}
        ),
    ]

    preferred = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=3,
        scope_minimums={"job_level": 1},
        scope_preferred_terms={"job_level": ("SL2",)},
    )
    fallback = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=3,
        scope_minimums={"job_level": 1},
        scope_preferred_terms={"job_level": ("G99",)},
    )

    assert [chunk.chunk_id for chunk in preferred] == [
        "general",
        "general-2",
        "bosch",
    ]
    assert [chunk.chunk_id for chunk in fallback] == [
        "general",
        "mercer",
        "general-2",
    ]


def test_scope_minimums_prefer_distinct_structural_evidence():
    def culture_chunk(chunk_id, score, heading):
        return _chunk(chunk_id, score, scope="culture").model_copy(
            update={
                "source_id": "culture/values.md",
                "metadata": {"heading_path": ["Culture", heading]},
            }
        )

    ranked = [
        culture_chunk("culture-a-1", 1.0, "Collaboration"),
        culture_chunk("culture-a-2", 0.99, "Collaboration"),
        _chunk("general", 0.98),
        culture_chunk("culture-b", 0.8, "Commitment"),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=3,
        scope_minimums={"culture": 2},
    )

    assert [chunk.chunk_id for chunk in selected] == [
        "culture-a-1",
        "general",
        "culture-b",
    ]


def test_scope_minimums_cover_new_preferred_terms_before_deduplication():
    def performance_chunk(chunk_id, score, text, item_id, heading):
        return _chunk(chunk_id, score, scope="performance").model_copy(
            update={
                "source_id": "performance/ppm.md",
                "text": text,
                "metadata": {
                    "heading_path": ["Performance", heading],
                    "retrieval_unit": {
                        "metadata": {"semantic_item_id": item_id}
                    },
                },
            }
        )

    ranked = [
        performance_chunk("what", 1.0, "WHAT is 70%", "what", "ASR"),
        performance_chunk("generic", 0.99, "rating overview", "rating", "Other"),
        performance_chunk("how", 0.8, "HOW is 30%", "how", "ASR"),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=2,
        scope_minimums={"performance": 2},
        scope_preferred_terms={"performance": ("WHAT", "HOW")},
    )

    assert [chunk.chunk_id for chunk in selected] == ["what", "how"]


def test_fill_covers_remaining_preferred_term_when_scope_minimum_is_one():
    def performance_chunk(chunk_id, score, text, item_id):
        return _chunk(chunk_id, score, scope="performance").model_copy(
            update={
                "source_id": "performance/ppm.md",
                "text": text,
                "metadata": {
                    "heading_path": ["Performance", "ASR"],
                    "semantic_item_id": item_id,
                },
            }
        )

    ranked = [
        performance_chunk("what", 1.0, "WHAT is 70%", "what"),
        _chunk("generic", 0.99),
        performance_chunk("how", 0.8, "HOW is 30%", "how"),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=2,
        scope_minimums={"performance": 1},
        scope_preferred_terms={"performance": ("WHAT", "HOW")},
    )

    assert [chunk.chunk_id for chunk in selected] == ["what", "how"]


def test_chunk_evidence_identity_uses_top_level_semantic_item_id():
    def performance_chunk(chunk_id, item_id):
        return _chunk(chunk_id, 1.0, scope="performance").model_copy(
            update={
                "source_id": "performance/ppm.md",
                "metadata": {
                    "heading_path": ["Performance", "ASR"],
                    "semantic_item_id": item_id,
                },
            }
        )

    assert RetrievalService._chunk_evidence_identity(
        performance_chunk("what", "what")
    ) != RetrievalService._chunk_evidence_identity(
        performance_chunk("how", "how")
    )


def test_fill_never_promotes_distinct_conflicting_level_over_bosch_duplicate():
    def level_chunk(chunk_id, score, text, heading):
        return _chunk(chunk_id, score, scope="job_level").model_copy(
            update={
                "source_id": "job_level/levels.md",
                "text": text,
                "metadata": {"heading_path": [heading]},
            }
        )

    ranked = [
        level_chunk("bosch-primary", 1.0, "Bosch G9 requirements", "Bosch"),
        level_chunk("mercer", 0.99, "Mercer P4 requirements", "Mercer"),
        level_chunk("bosch-detail", 0.98, "Bosch G9 behavior", "Bosch"),
        _chunk("general", 0.97),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=3,
        scope_minimums={"job_level": 1},
        scope_preferred_terms={"job_level": ("G9",)},
    )

    assert [chunk.chunk_id for chunk in selected] == [
        "bosch-primary",
        "bosch-detail",
        "general",
    ]


def test_preferred_culture_term_matches_a_chinese_only_source():
    chunk = _chunk("culture-cn", 1.0, scope="culture").model_copy(
        update={"text": "使命必达要求主动承担困难任务并兑现承诺。"}
    )

    assert RetrievalService._preferred_term_matches(
        chunk,
        ("commitment to win",),
    ) == {"commitment to win"}


@pytest.mark.parametrize(
    ("raw_level", "normalized", "next_level"),
    [
        ("G-9", "G9", "SL1"),
        ("G 9", "G9", "SL1"),
        ("G9招聘专家", "G9", "SL1"),
        ("SL1/G9", "SL1", "SL2"),
    ],
)
def test_template_context_normalizes_common_job_level_formats(
    raw_level,
    normalized,
    next_level,
):
    context = RetrievalService._template_context(
        {"profile": {"level": raw_level}}
    )

    assert context["normalized_employee_level"] == normalized
    assert context["next_employee_level"] == next_level


def test_sl4_development_plan_has_no_inapplicable_job_level_target():
    context = {
        "profile": {"level": "SL4"},
        "intent": {"id": "development"},
    }

    assert RetrievalService._applicable_job_levels(
        agent_name="guidance_plan",
        context=context,
    ) == ()
    assert RetrievalService._applicable_job_levels(
        agent_name="guidance_requirement",
        context=context,
    ) == ("SL4",)


@pytest.mark.parametrize("raw_level", ["SL4", "未提供"])
def test_development_plan_does_not_fallback_to_historical_level_codes(
    raw_level,
):
    preferred = RetrievalService._citation_scope_preferred_terms(
        agent_name="guidance_plan",
        context={
            "profile": {"level": raw_level},
            "intent": {"id": "development"},
        },
        available_scopes=["job_level"],
        rerank_query="历史评价曾提到 G9 和 P4",
        scope_minimums={},
    )

    assert preferred == {}


def test_scope_preferences_use_labelled_levels_and_exact_domain_terms():
    preferred = RetrievalService._citation_scope_preferred_terms(
        rerank_query=(
            "适用当前职级：G9；适用下一职级：SL1；"
            "历史背景提到 P4 与 M2；WHAT/HOW；"
            "Career Elements 与 Commitment to WIN"
        ),
        scope_minimums={"job_level": 1, "culture": 1, "performance": 1},
    )

    assert preferred == {
        "job_level": ("G9", "SL1"),
        "culture": ("commitment to win",),
        "performance": ("WHAT", "HOW"),
    }


def test_scope_preferences_prioritize_explicit_context_culture_term():
    preferred = RetrievalService._citation_scope_preferred_terms(
        context={"supplemental_info": "本轮适用创变未来的行为标准。"},
        rerank_query=(
            "Collaboration to DELIVER Innovation to SHAPE "
            "Customer-centricity to GROW Commitment to WIN"
        ),
        scope_minimums={"culture": 1},
    )

    assert preferred == {"culture": ("innovation to shape",)}


def test_scope_preferences_merge_task_config_with_derived_terms():
    preferred = RetrievalService._citation_scope_preferred_terms(
        agent_cfg={
            "citation_scope_preferred_terms": {
                "emotion": ["有效的停顿", "校准问题", "校准问题"],
                "unknown": ["ignored"],
            }
        },
        defaults={},
        available_scopes=["emotion", "performance"],
        rerank_query="WHAT/HOW 与主动倾听",
        scope_minimums={"emotion": 2, "performance": 1},
    )

    assert preferred == {
        "emotion": ("有效的停顿", "校准问题"),
        "performance": ("WHAT", "HOW"),
    }


def test_emotion_preferred_evidence_can_enter_top_eight_and_prefix():
    ranked = [
        _selection_chunk(
            "emotion-narrative",
            1.0,
            source_id="emotion/methods.md",
            scope="emotion",
            text="一段谈判历史案例。",
        ),
        _selection_chunk(
            "emotion-calibrated-question",
            0.99,
            source_id="emotion/methods.md",
            scope="emotion",
            text="使用校准问题，避免封闭性问题。",
        ),
        _selection_chunk(
            "culture-evidence",
            0.98,
            source_id="culture/values.md",
            scope="culture",
            text="尊重与信任。",
        ),
        *[
            _selection_chunk(
                f"emotion-generic-{index}",
                0.97 - index / 100,
                source_id="emotion/methods.md",
                scope="emotion",
                text="泛化的沟通建议。",
            )
            for index in range(4, 10)
        ],
        _selection_chunk(
            "emotion-listening-checklist",
            0.1,
            source_id="emotion/methods.md",
            scope="emotion",
            text="先做有效的停顿，再使用最低限度的鼓励。",
        ),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=8,
        scope_minimums={"emotion": 2, "culture": 1},
        scope_preferred_terms={
            "emotion": (
                "有效的停顿",
                "最低限度的鼓励",
                "校准问题",
                "封闭性问题",
            )
        },
        diverse_prefix_k=3,
        generation_prefix_scope_targets={"emotion": 2, "culture": 1},
        generation_prefix_fill_scopes=("emotion", "culture"),
    )

    assert [chunk.chunk_id for chunk in selected[:3]] == [
        "emotion-calibrated-question",
        "emotion-listening-checklist",
        "culture-evidence",
    ]
    assert len(selected) == 8
    assert "emotion-listening-checklist" in {
        chunk.chunk_id for chunk in selected
    }


def test_emotion_prefix_uses_one_slot_per_configured_semantic_family():
    query_config = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "business_config"
            / "query.yaml"
        ).read_text(encoding="utf-8")
    )["queries"]["guidance_emotion"]
    scope_targets = {"emotion": 2, "culture": 1}
    semantic_groups = (
        RetrievalService._generation_prefix_scope_semantic_groups(
            agent_cfg=query_config,
            defaults={},
            available_scopes=["emotion", "culture"],
            scope_targets=scope_targets,
        )
    )
    ranked = [
        _selection_chunk(
            "emotion-calibrated-question",
            1.0,
            source_id="emotion/methods.md",
            scope="emotion",
            text="使用校准问题和共同解决问题，避免封闭性问题。",
        ),
        _selection_chunk(
            "emotion-listening-checklist",
            0.99,
            source_id="emotion/methods.md",
            scope="emotion",
            text="先做有效的停顿，再使用最低限度的鼓励。",
        ),
        _selection_chunk(
            "culture-evidence",
            0.98,
            source_id="culture/values.md",
            scope="culture",
            text="尊重与信任。",
        ),
        _selection_chunk(
            "emotion-empathy-summary",
            0.97,
            source_id="emotion/methods.md",
            scope="emotion",
            text="本章介绍战术同理心。",
        ),
        _selection_chunk(
            "emotion-generic-1",
            0.96,
            source_id="emotion/methods.md",
            scope="emotion",
            text="泛化的沟通建议。",
        ),
        _selection_chunk(
            "emotion-generic-2",
            0.95,
            source_id="emotion/methods.md",
            scope="emotion",
            text="另一段泛化的沟通建议。",
        ),
        _selection_chunk(
            "emotion-empathy-method",
            0.1,
            source_id="emotion/methods.md",
            scope="emotion",
            text="同理心要先倾听，再用标注和停顿承接对方。",
        ),
    ]

    legacy = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=len(ranked),
        scope_minimums={"emotion": 2, "culture": 1},
        scope_preferred_terms={
            "emotion": tuple(
                query_config["citation_scope_preferred_terms"]["emotion"]
            )
        },
        diverse_prefix_k=3,
        generation_prefix_scope_targets=scope_targets,
        generation_prefix_fill_scopes=("emotion", "culture"),
    )
    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=len(ranked),
        scope_minimums={"emotion": 2, "culture": 1},
        scope_preferred_terms={
            "emotion": tuple(
                query_config["citation_scope_preferred_terms"]["emotion"]
            )
        },
        diverse_prefix_k=3,
        generation_prefix_scope_targets=scope_targets,
        generation_prefix_scope_semantic_groups=semantic_groups,
        generation_prefix_fill_scopes=("emotion", "culture"),
    )

    assert "emotion-empathy-method" not in {
        chunk.chunk_id for chunk in legacy[:3]
    }
    assert [chunk.chunk_id for chunk in selected[:3]] == [
        "emotion-empathy-method",
        "emotion-calibrated-question",
        "culture-evidence",
    ]


def test_emotion_semantic_prefix_downranks_preview_toc_and_negative_claims():
    query_config = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "business_config"
            / "query.yaml"
        ).read_text(encoding="utf-8")
    )["queries"]["guidance_emotion"]
    scope_targets = {"emotion": 2, "culture": 1}
    semantic_groups = (
        RetrievalService._generation_prefix_scope_semantic_groups(
            agent_cfg=query_config,
            defaults={},
            available_scopes=["emotion", "culture"],
            scope_targets=scope_targets,
        )
    )
    ranked = [
        _selection_chunk(
            "empathy-chapter-preview",
            1.0,
            source_id="emotion/methods.md",
            scope="emotion",
            text="本章介绍战术同理心、主动倾听、标注、镜像和释义。",
        ),
        _selection_chunk(
            "exploration-toc",
            0.99,
            source_id="emotion/methods.md",
            scope="emotion",
            text=(
                "目录：校准问题、共同解决问题、开放性的校准问题、"
                "寻求对方帮助。"
            ),
        ),
        _selection_chunk(
            "empathy-negative-claim",
            0.98,
            source_id="emotion/methods.md",
            scope="emotion",
            text="不用安慰，只需用战术同理心和主动倾听。",
        ),
        _selection_chunk(
            "empathy-direct-method",
            0.2,
            source_id="emotion/methods.md",
            scope="emotion",
            text="运用战术同理心时，先主动倾听，再标注对方的感受。",
        ),
        _selection_chunk(
            "exploration-direct-method",
            0.1,
            source_id="emotion/methods.md",
            scope="emotion",
            text=(
                "提出开放性的校准问题，寻求对方帮助，"
                "把对抗转成共同解决问题。"
            ),
        ),
        _selection_chunk(
            "culture-evidence",
            0.09,
            source_id="culture/values.md",
            scope="culture",
            text="尊重与信任。",
        ),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=len(ranked),
        scope_minimums={"emotion": 2, "culture": 1},
        scope_preferred_terms={
            "emotion": tuple(
                query_config["citation_scope_preferred_terms"]["emotion"]
            )
        },
        diverse_prefix_k=3,
        generation_prefix_scope_targets=scope_targets,
        generation_prefix_scope_semantic_groups=semantic_groups,
        generation_prefix_fill_scopes=("emotion", "culture"),
    )

    assert [chunk.chunk_id for chunk in selected[:3]] == [
        "empathy-direct-method",
        "exploration-direct-method",
        "culture-evidence",
    ]


def test_emotion_semantic_prefix_keeps_downranked_only_family_candidate():
    query_config = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "business_config"
            / "query.yaml"
        ).read_text(encoding="utf-8")
    )["queries"]["guidance_emotion"]
    scope_targets = {"emotion": 2, "culture": 1}
    semantic_groups = (
        RetrievalService._generation_prefix_scope_semantic_groups(
            agent_cfg=query_config,
            defaults={},
            available_scopes=["emotion", "culture"],
            scope_targets=scope_targets,
        )
    )
    ranked = [
        _selection_chunk(
            "empathy-only-preview",
            1.0,
            source_id="emotion/methods.md",
            scope="emotion",
            text="本章介绍战术同理心。",
        ),
        _selection_chunk(
            "exploration-method",
            0.9,
            source_id="emotion/methods.md",
            scope="emotion",
            text="用开放性的校准问题寻求对方帮助。",
        ),
        _selection_chunk(
            "culture-evidence",
            0.8,
            source_id="culture/values.md",
            scope="culture",
            text="尊重与信任。",
        ),
    ]

    selected = RetrievalService._select_with_scope_minimums(
        ranked,
        top_k=3,
        scope_minimums={"emotion": 2, "culture": 1},
        scope_preferred_terms={
            "emotion": tuple(
                query_config["citation_scope_preferred_terms"]["emotion"]
            )
        },
        diverse_prefix_k=3,
        generation_prefix_scope_targets=scope_targets,
        generation_prefix_scope_semantic_groups=semantic_groups,
        generation_prefix_fill_scopes=("emotion", "culture"),
    )

    assert [chunk.chunk_id for chunk in selected] == [
        "empathy-only-preview",
        "exploration-method",
        "culture-evidence",
    ]


def test_emotion_semantic_prefix_ignores_parent_cross_family_pollution():
    query_config = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "business_config"
            / "query.yaml"
        ).read_text(encoding="utf-8")
    )["queries"]["guidance_emotion"]
    scope_targets = {"emotion": 2, "culture": 1}
    semantic_groups = (
        RetrievalService._generation_prefix_scope_semantic_groups(
            agent_cfg=query_config,
            defaults={},
            available_scopes=["emotion", "culture"],
            scope_targets=scope_targets,
        )
    )

    polluted_parent = _selection_chunk(
        "emotion-parent-toc-pollution",
        1.0,
        source_id="emotion/methods.md",
        scope="emotion",
        text=(
            "第三章介绍战术同理心和主动倾听。\n\n"
            "第七章介绍校准问题以及如何共同解决问题。"
        ),
    ).model_copy(
        update={
            "metadata": {
                "search_text": "目录还提到了策略性的同理心和标注。",
                "retrieval_units": [
                    {
                        "unit_id": "calibration-leaf",
                        "unit_type": "paragraph",
                        "text": "使用校准问题，把对话转向共同解决问题。",
                        "search_text": "上文目录提到战术同理心。",
                    }
                ],
            }
        }
    )
    empathy_method = _selection_chunk(
        "emotion-visible-empathy-method",
        0.1,
        source_id="emotion/methods.md",
        scope="emotion",
        text="父文档中的其他内容。",
    ).model_copy(
        update={
            "metadata": {
                "retrieval_units": [
                    {
                        "unit_id": "empathy-leaf",
                        "unit_type": "paragraph",
                        "text": (
                            "运用策略性的同理心，先主动倾听，再标注对方的感受。"
                        ),
                    }
                ],
            }
        }
    )
    culture = _selection_chunk(
        "culture-evidence",
        0.09,
        source_id="culture/values.md",
        scope="culture",
        text="尊重与信任。",
    )
    empathy_terms = semantic_groups["emotion"][0].required_terms

    assert RetrievalService._preferred_term_matches(
        polluted_parent,
        empathy_terms,
    )
    assert not RetrievalService._generation_evidence_term_matches(
        polluted_parent,
        empathy_terms,
    )

    selected = RetrievalService._select_with_scope_minimums(
        [polluted_parent, empathy_method, culture],
        top_k=3,
        scope_minimums={"emotion": 2, "culture": 1},
        scope_preferred_terms={
            "emotion": tuple(
                query_config["citation_scope_preferred_terms"]["emotion"]
            )
        },
        diverse_prefix_k=3,
        generation_prefix_scope_targets=scope_targets,
        generation_prefix_scope_semantic_groups=semantic_groups,
        generation_prefix_fill_scopes=("emotion", "culture"),
    )

    assert [chunk.chunk_id for chunk in selected[:3]] == [
        "emotion-visible-empathy-method",
        "emotion-parent-toc-pollution",
        "culture-evidence",
    ]
    assert RetrievalService._generation_evidence_term_matches(
        selected[0],
        semantic_groups["emotion"][0].required_terms,
    )
    assert RetrievalService._generation_evidence_term_matches(
        selected[1],
        semantic_groups["emotion"][1].required_terms,
    )


@pytest.mark.parametrize(
    "raw_groups",
    [
        [{"required_terms": "同理心"}],
        [{"required_terms": ["同理心"], "downrank_terms": "目录"}],
        [{"required_terms": ["同理心"], "unknown": []}],
        [
            {"required_terms": ["同理心"]},
            {"required_terms": ["同理心", "校准问题"]},
        ],
        [
            {"required_terms": ["同理心"]},
            {"required_terms": ["校准问题"]},
            {"required_terms": ["倾听"]},
        ],
    ],
)
def test_generation_prefix_semantic_groups_fail_closed(raw_groups):
    parsed = RetrievalService._generation_prefix_scope_semantic_groups(
        agent_cfg={
            "generation_prefix_scope_semantic_groups": {
                "emotion": raw_groups,
            }
        },
        defaults={},
        available_scopes=["emotion"],
        scope_targets={"emotion": 2},
    )

    assert parsed == {}


def test_generation_prefix_semantic_group_downrank_terms_are_cached():
    parsed = RetrievalService._generation_prefix_scope_semantic_groups(
        agent_cfg={
            "generation_prefix_scope_semantic_groups": {
                "emotion": [
                    {
                        "required_terms": ["同理心"],
                        "preferred_terms": ["主动倾听"],
                        "downrank_terms": ["目录", "本章介绍"],
                    }
                ],
            }
        },
        defaults={},
        available_scopes=["emotion"],
        scope_targets={"emotion": 1},
    )

    assert parsed["emotion"][0].cache_identity() == {
        "required_terms": ["同理心"],
        "preferred_terms": ["主动倾听"],
        "downrank_terms": ["目录", "本章介绍"],
    }


def test_rrf_scope_minimums_keep_critical_scope_in_rerank_candidates():
    branch = _RankedBranch(
        branch_id="0:all:dense",
        modality="dense",
        scope="general",
        weight=1.0,
        chunks=[
            *[
                _chunk(f"general-{index}", 1.0 - index / 100)
                for index in range(1, 10)
            ],
            _chunk("culture-evidence", 0.1, scope="culture"),
        ],
    )

    result = RetrievalService._rrf_fuse(
        [branch],
        rrf_k=60,
        top_n=8,
        scope_minimums={"culture": 1},
    )

    assert len(result) == 8
    assert result[-1].chunk_id == "culture-evidence"


def test_conditional_culture_scope_minimum_requires_values_or_skill():
    arguments = {
        "agent_cfg": {
            "citation_scope_minimums": {"culture": 1},
            "citation_scope_conditions": {
                "culture": "company_values_or_culture_skill"
            },
        },
        "defaults": {},
        "active_skills": [],
        "available_scopes": ["culture", "performance"],
        "final_top_k": 8,
    }

    assert RetrievalService._citation_scope_minimums(
        **arguments,
        context={"company_value_terms": ""},
    ) == {}
    assert RetrievalService._citation_scope_minimums(
        **arguments,
        context={"company_value_terms": "使命必达"},
    ) == {"culture": 1}


def test_conditional_career_scope_minimum_requires_applicable_context():
    arguments = {
        "agent_cfg": {
            "citation_scope_minimums": {"career": 1},
            "citation_scope_conditions": {
                "career": "career_elements_applicable"
            },
        },
        "defaults": {},
        "active_skills": [],
        "available_scopes": ["career", "performance"],
        "final_top_k": 8,
    }

    assert RetrievalService._citation_scope_minimums(
        **arguments,
        context={"career_elements_applicable": False},
    ) == {}
    assert RetrievalService._citation_scope_minimums(
        **arguments,
        context={"career_elements_applicable": True},
    ) == {"career": 1}


def test_retrieval_scope_condition_removes_inapplicable_career_before_search():
    arguments = {
        "agent_name": "guidance_plan",
        "agent_cfg": {},
        "defaults": {
            "retrieval_scope_conditions": {
                "career": "career_elements_applicable",
            }
        },
        "active_skills": [],
        "available_scopes": ["job_level", "career", "development_dialog"],
    }

    assert RetrievalService._retrieval_scopes_for_context(
        **arguments,
        context={"career_elements_applicable": False},
    ) == ["job_level", "development_dialog"]
    assert RetrievalService._retrieval_scopes_for_context(
        **arguments,
        context={"career_elements_applicable": True},
    ) == ["job_level", "career", "development_dialog"]


def test_generation_prefix_config_filters_targets_and_fill_scopes_by_condition():
    agent_cfg = {
        "generation_prefix_scope_targets": {
            "job_level": 1,
            "career": 1,
            "culture": 1,
        },
        "generation_prefix_scope_conditions": {
            "career": "career_elements_applicable",
            "culture": "company_values_or_culture_skill",
        },
        "generation_prefix_fill_scopes": [
            "development_dialog",
            "career",
            "culture",
        ],
    }
    common = {
        "agent_name": "guidance_plan",
        "agent_cfg": agent_cfg,
        "defaults": {},
        "active_skills": [],
        "available_scopes": [
            "job_level",
            "career",
            "culture",
            "development_dialog",
        ],
    }

    assert RetrievalService._generation_prefix_scope_targets(
        **common,
        context={"career_elements_applicable": False},
        final_top_k=8,
    ) == {"job_level": 1}
    assert RetrievalService._generation_prefix_fill_scopes(
        **common,
        context={"career_elements_applicable": False},
    ) == ("development_dialog",)

    enabled_context = {
        "career_elements_applicable": True,
        "company_value_terms": "Innovation to SHAPE",
    }
    assert RetrievalService._generation_prefix_scope_targets(
        **common,
        context=enabled_context,
        final_top_k=8,
    ) == {"job_level": 1, "career": 1, "culture": 1}
    assert RetrievalService._generation_prefix_fill_scopes(
        **common,
        context=enabled_context,
    ) == ("development_dialog", "career", "culture")


def test_retrieval_cache_key_covers_hybrid_mapping_and_budgets(monkeypatch):
    service = object.__new__(RetrievalService)
    service.settings = SimpleNamespace(
        postgres_bm25_tokenizer_name="kb_jieba_v1",
        kb_index_version="v1",
        effective_embedding_provider="local_qwen",
        effective_embedding_model="embedding",
        embedding_url="http://qwen_embedding:8000/v1/embeddings",
        embedding_dimensions=1024,
        effective_rerank_provider="local_qwen",
        effective_rerank_model="reranker",
        rerank_url="http://qwen_reranker:8000/v1/completions",
    )
    specs = [
        _RenderedQuerySpec(
            query="query",
            scopes=["general"],
            weight=1.0,
            template_index=0,
        )
    ]
    arguments = {
        "agent_name": "guidance",
        "query_specs": specs,
        "rerank_query": "single rerank query",
        "rerank_instruction": "Rank guidance evidence.",
        "dense_top_k": 16,
        "lexical_top_k": 16,
        "rrf_k": 60,
        "fusion_top_n": 60,
        "rerank_candidate_top_n": 30,
        "rerank_units_per_parent": 5,
        "rerank_semantic_group_candidate_enabled": True,
        "rerank_compound_evidence_sets": (("WHAT", "HOW", "ASR"),),
        "rerank_rank_fusion_k": 60,
        "rerank_rank_weight": 4 / 3,
        "first_stage_rank_weight": 2 / 3,
        "final_top_k": 8,
        "rerank_enabled": True,
        "hybrid_enabled": True,
        "dense_weight": 1.0,
        "lexical_weight": 1.0,
        "metadata_filter": {},
    }

    first = service._result_cache_key(**arguments)
    selection_policy_version = (
        retrieval_service_module._SCOPE_SELECTION_POLICY_VERSION
    )
    monkeypatch.setattr(
        retrieval_service_module,
        "_SCOPE_SELECTION_POLICY_VERSION",
        f"{selection_policy_version}-next",
    )
    selection_policy_changed = service._result_cache_key(**arguments)
    monkeypatch.setattr(
        retrieval_service_module,
        "_SCOPE_SELECTION_POLICY_VERSION",
        selection_policy_version,
    )
    second = service._result_cache_key(
        **{**arguments, "lexical_top_k": 20}
    )
    citation_scope_changed = service._result_cache_key(
        **{
            **arguments,
            "citation_scope_minimums": {"culture": 1},
        }
    )
    citation_scope_preference_changed = service._result_cache_key(
        **{
            **arguments,
            "citation_scope_preferred_terms": {
                "job_level": ("SL1",),
            },
        }
    )
    generation_prefix_targets_changed = service._result_cache_key(
        **{
            **arguments,
            "generation_prefix_scope_targets": {
                "job_level": 1,
                "culture": 1,
            },
        }
    )
    generation_prefix_target_order_changed = service._result_cache_key(
        **{
            **arguments,
            "generation_prefix_scope_targets": {
                "culture": 1,
                "job_level": 1,
            },
        }
    )
    generation_prefix_fill_changed = service._result_cache_key(
        **{
            **arguments,
            "generation_prefix_fill_scopes": ("development_dialog",),
        }
    )
    generation_prefix_semantic_groups_changed = service._result_cache_key(
        **{
            **arguments,
            "generation_prefix_scope_semantic_groups": (
                RetrievalService._generation_prefix_scope_semantic_groups(
                    agent_cfg={
                        "generation_prefix_scope_semantic_groups": {
                            "performance": [
                                {"required_terms": ["WHAT", "HOW"]}
                            ]
                        }
                    },
                    defaults={},
                    available_scopes=["performance"],
                    scope_targets={"performance": 1},
                )
            ),
        }
    )
    rerank_query_changed = service._result_cache_key(
        **{**arguments, "rerank_query": "different rerank query"}
    )
    rerank_instruction_changed = service._result_cache_key(
        **{
            **arguments,
            "rerank_instruction": "Rank different guidance evidence.",
        }
    )
    rerank_unit_limit_changed = service._result_cache_key(
        **{**arguments, "rerank_units_per_parent": 3}
    )
    semantic_group_candidate_changed = service._result_cache_key(
        **{
            **arguments,
            "rerank_semantic_group_candidate_enabled": False,
        }
    )
    compound_evidence_sets_changed = service._result_cache_key(
        **{
            **arguments,
            "rerank_compound_evidence_sets": (("WHAT", "HOW"),),
        }
    )
    rerank_rank_fusion_changed = service._result_cache_key(
        **{**arguments, "rerank_rank_fusion_k": 40}
    )
    rerank_rank_weight_changed = service._result_cache_key(
        **{**arguments, "rerank_rank_weight": 1.0}
    )
    first_stage_rank_weight_changed = service._result_cache_key(
        **{**arguments, "first_stage_rank_weight": 1.0}
    )
    mapped = service._result_cache_key(
        **{
            **arguments,
            "query_specs": [
                _RenderedQuerySpec(
                    query="query",
                    scopes=["performance"],
                    weight=1.0,
                    template_index=0,
                )
            ],
        }
    )
    service.settings.effective_embedding_provider = "bosch"
    embedding_provider_changed = service._result_cache_key(**arguments)
    service.settings.effective_embedding_provider = "local_qwen"
    service.settings.embedding_dimensions = 768
    embedding_dimensions_changed = service._result_cache_key(**arguments)
    service.settings.embedding_dimensions = 1024
    service.settings.rerank_url = "https://models.example/v1/rerank"
    rerank_endpoint_changed = service._result_cache_key(**arguments)
    service.settings.rerank_url = "http://qwen_reranker:8000/v1/completions"
    service.settings.postgres_bm25_tokenizer_name = "kb_jieba_v2"
    tokenizer_changed = service._result_cache_key(**arguments)
    scope_policy_changed = service._result_cache_key(
        **{
            **arguments,
            "scope_search_policies": {
                "general": _ScopeSearchPolicy(
                    dense_mode="ann_preferred",
                    dense_top_k=16,
                    lexical_top_k=16,
                    exact_top_k=16,
                    hybrid_enabled=True,
                    exact_enabled=True,
                )
            },
        }
    )
    assert embedding_provider_changed == first

    assert len(
        {
            first,
            selection_policy_changed,
            second,
            citation_scope_changed,
            citation_scope_preference_changed,
            generation_prefix_targets_changed,
            generation_prefix_target_order_changed,
            generation_prefix_fill_changed,
            generation_prefix_semantic_groups_changed,
            rerank_query_changed,
            rerank_instruction_changed,
            rerank_unit_limit_changed,
            semantic_group_candidate_changed,
            compound_evidence_sets_changed,
            rerank_rank_fusion_changed,
            rerank_rank_weight_changed,
            first_stage_rank_weight_changed,
            mapped,
            embedding_dimensions_changed,
            rerank_endpoint_changed,
            tokenizer_changed,
            scope_policy_changed,
        }
    ) == 22


def test_scope_search_policies_disable_exact_globally_and_keep_large_scope_hnsw():
    config = yaml.safe_load(
        (Path(__file__).parents[1] / "backend/business_config/query.yaml")
        .read_text(encoding="utf-8")
    )
    policies = RetrievalService._resolve_scope_search_policies(
        query_config=config,
        scopes=["general", "employee", "organization_unit"],
        dense_top_k=18,
        lexical_top_k=18,
        exact_top_k=16,
        hybrid_enabled=True,
        exact_enabled=True,
    )

    assert policies["general"].dense_mode == "exact"
    assert policies["general"].dense_top_k == 18
    assert policies["general"].exact_enabled is False
    employee = policies["employee"]
    assert employee.dense_mode == "ann_preferred"
    assert (
        employee.dense_top_k,
        employee.lexical_top_k,
        employee.exact_top_k,
    ) == (28, 28, 16)
    assert employee.hybrid_enabled is True
    assert employee.exact_enabled is False
    organization = policies["organization_unit"]
    assert organization.dense_mode == "ann_preferred"
    assert (
        organization.dense_top_k,
        organization.lexical_top_k,
        organization.exact_top_k,
    ) == (46, 32, 24)
    assert organization.hybrid_enabled is True
    assert organization.exact_enabled is False


def test_mixed_scope_search_uses_each_effective_policy_budget():
    calls: list[dict] = []
    service = object.__new__(RetrievalService)
    service._search_scope = lambda **kwargs: (
        calls.append(kwargs)
        or _SearchJobResult(
            branches=[],
            lexical_error=None,
            semaphore_wait_ms=0.0,
        )
    )
    policies = {
        "general": _ScopeSearchPolicy(
            "exact", 18, 18, 16, True, True
        ),
        "organization_unit": _ScopeSearchPolicy(
            "ann_preferred", 48, 32, 24, True, True
        ),
    }

    service._search_scopes(
        query_index=0,
        query="query",
        query_embedding=[1.0, 0.0],
        scopes=["general", "organization_unit"],
        dense_top_k=18,
        lexical_top_k=18,
        metadata_filter={},
        template_weight=1.0,
        dense_weight=1.0,
        lexical_weight=1.0,
        hybrid_enabled=True,
        scope_search_policies=policies,
    )

    assert [
        (
            call["scope"],
            call["dense_top_k"],
            call["lexical_top_k"],
            call["exact_top_k"],
        )
        for call in calls
    ] == [
        ("general", 18, 18, 16),
        ("organization_unit", 48, 32, 24),
    ]


def test_scope_dense_mode_forces_exact_and_ann_preferred_falls_back():
    import backend.services.retrieval_service as retrieval_module

    calls: list[dict] = []

    class Client:
        def search_hybrid_by_embedding(
            self,
            query,
            query_embedding,
            *,
            exact_enabled=True,
            exact_top_k=16,
            **kwargs,
        ):
            del query, query_embedding
            calls.append({
                **kwargs,
                "exact_enabled": exact_enabled,
                "exact_top_k": exact_top_k,
            })
            return retrieval_module.HybridSearchResult(dense=[], lexical=[])

    service = object.__new__(RetrievalService)
    service.settings = SimpleNamespace(
        collection_name_for_scope=lambda scope: f"kb_{scope}"
    )
    service._search_limiter = threading.BoundedSemaphore(1)
    service._store_for_collection = lambda _collection: Client()
    ready = RetrievalScopeCapability(
        collection_name="kb_general",
        scope="general",
        row_count=100,
        bm25_ready=True,
        ann_dimensions=frozenset({2}),
    )
    not_ready = RetrievalScopeCapability(
        collection_name="kb_organization_unit",
        scope="organization_unit",
        row_count=100,
        bm25_ready=True,
        ann_dimensions=frozenset(),
    )
    service._capabilities = {
        ("kb_general", "general"): ready,
        ("kb_organization_unit", "organization_unit"): not_ready,
    }
    base = dict(
        query_index=0,
        query="query",
        query_embedding=[1.0, 0.0],
        dense_top_k=18,
        lexical_top_k=18,
        metadata_filter={},
        template_weight=1.0,
        dense_weight=1.0,
        lexical_weight=1.0,
        hybrid_enabled=True,
    )

    service._search_scope(
        **base,
        scope="general",
        scope_search_policy=_ScopeSearchPolicy(
            "exact", 18, 18, 16, True, True
        ),
    )
    service._search_scope(
        **base,
        scope="organization_unit",
        scope_search_policy=_ScopeSearchPolicy(
            "ann_preferred", 48, 32, 24, True, True
        ),
    )
    service._capabilities[(
        "kb_organization_unit", "organization_unit"
    )] = RetrievalScopeCapability(
        collection_name="kb_organization_unit",
        scope="organization_unit",
        row_count=100,
        bm25_ready=True,
        ann_dimensions=frozenset({2}),
    )
    service._search_scope(
        **base,
        scope="organization_unit",
        scope_search_policy=_ScopeSearchPolicy(
            "ann_preferred", 48, 32, 24, True, True
        ),
    )

    assert [call["use_ann"] for call in calls] == [False, False, True]
    assert (calls[1]["dense_top_k"], calls[1]["lexical_top_k"]) == (
        48,
        32,
    )
    assert calls[1]["exact_top_k"] == 24


def test_employee_and_organization_unit_use_ann_and_bm25_when_ready():
    import backend.services.retrieval_service as retrieval_module

    calls: list[dict] = []

    class Client:
        def search_hybrid_by_embedding(
            self,
            query,
            query_embedding,
            **kwargs,
        ):
            del query, query_embedding
            calls.append(kwargs)
            return retrieval_module.HybridSearchResult(dense=[], lexical=[])

    service = object.__new__(RetrievalService)
    service.settings = SimpleNamespace(
        collection_name_for_scope=lambda scope: f"kb_{scope}"
    )
    service._search_limiter = threading.BoundedSemaphore(1)
    service._store_for_collection = lambda _collection: Client()
    service._capabilities = {
        (f"kb_{scope}", scope): RetrievalScopeCapability(
            collection_name=f"kb_{scope}",
            scope=scope,
            row_count=100,
            bm25_ready=True,
            retrieval_unit_bm25_ready=True,
            ann_dimensions=frozenset({2}),
        )
        for scope in ("employee", "organization_unit")
    }
    policies = {
        "employee": _ScopeSearchPolicy(
            "ann_preferred", 18, 18, 16, True, True
        ),
        "organization_unit": _ScopeSearchPolicy(
            "ann_preferred", 48, 32, 24, True, True
        ),
    }
    base = dict(
        query_index=0,
        query="query",
        query_embedding=[1.0, 0.0],
        dense_top_k=18,
        lexical_top_k=18,
        metadata_filter={},
        template_weight=1.0,
        dense_weight=1.0,
        lexical_weight=1.0,
        hybrid_enabled=True,
    )

    results = {
        scope: service._search_scope(
            **base,
            scope=scope,
            scope_search_policy=policy,
        )
        for scope, policy in policies.items()
    }

    assert {
        call["scope"]: (call["use_ann"], call["lexical_enabled"])
        for call in calls
    } == {
        "employee": (True, True),
        "organization_unit": (True, True),
    }
    assert {
        scope: [branch.modality for branch in result.branches]
        for scope, result in results.items()
    } == {
        "employee": ["dense", "bm25"],
        "organization_unit": ["dense", "bm25"],
    }


def test_capability_snapshot_requires_threshold_and_complete_bm25_index():
    rows = [
        {
            "collection_name": "kb_general",
            "scope": "general",
            "dimension": 2,
            "row_count": 100,
            "bm25_count": 100,
        }
    ]

    class Connection:
        def execute(self, statement, params=None):
            rendered = str(statement)
            if "FROM kb_chunk_embeddings e" in rendered:
                return _Cursor(rows=rows)
            if "FROM kb_retrieval_unit_embeddings e" in rendered:
                return _Cursor(rows=[])
            return _Cursor(
                rows=[
                    {"index_name": PostgresRepository.hnsw_index_name("kb_general", "general", 2)},
                    {"index_name": PostgresRepository.bm25_index_name("kb_general", "general")},
                ]
            )

    repository = object.__new__(PostgresRepository)
    repository.settings = SimpleNamespace(postgres_ann_min_rows=10_000)

    @contextmanager
    def connection():
        yield Connection()

    repository.connection = connection
    profile = EmbeddingProfileState(
        profile_id="profile-a",
        model_name="embedding",
        dimensions=2,
        status="ready",
        active_build_id="00000000-0000-0000-0000-000000000001",
        source_fingerprint="fingerprint",
        chunk_count=100,
        vector_count=100,
        current_chunk_count=100,
    )
    capability = repository.retrieval_capability_snapshot(profile)[
        ("kb_general", "general")
    ]

    assert capability.row_count == 100
    assert capability.bm25_ready is True
    assert capability.ann_ready(2) is False


def test_capability_snapshot_requires_hnsw_for_large_retrieval_unit_corpus():
    chunk_rows = [
        {
            "collection_name": "kb_employee",
            "scope": "employee",
            "dimension": 2560,
            "row_count": 900,
            "bm25_count": 900,
        }
    ]
    unit_rows = [
        {
            "collection_name": "kb_employee",
            "scope": "employee",
            "dimension": 2560,
            "row_count": 20_000,
        }
    ]
    profile = EmbeddingProfileState(
        profile_id="profile-a",
        model_name="embedding",
        dimensions=2560,
        status="ready",
        active_build_id="00000000-0000-0000-0000-000000000001",
        source_fingerprint="fingerprint",
        chunk_count=900,
        vector_count=20_900,
        current_chunk_count=900,
    )
    unit_hnsw_name = PostgresRepository.retrieval_unit_hnsw_index_name(
        "kb_employee",
        "employee",
        2560,
        profile_id=profile.profile_id,
        build_id=profile.active_build_id,
    )
    bm25_name = PostgresRepository.bm25_index_name(
        "kb_employee",
        "employee",
    )
    retrieval_unit_bm25_name = (
        PostgresRepository.retrieval_unit_bm25_index_name()
    )

    def snapshot(
        *,
        include_unit_hnsw: bool,
        include_retrieval_unit_bm25: bool,
    ):
        class Connection:
            def execute(self, statement, params=None):
                rendered = str(statement)
                if "FROM kb_chunk_embeddings e" in rendered:
                    return _Cursor(rows=chunk_rows)
                if "FROM kb_retrieval_unit_embeddings e" in rendered:
                    return _Cursor(rows=unit_rows)
                indexes = [{"index_name": bm25_name}]
                if include_unit_hnsw:
                    indexes.append({"index_name": unit_hnsw_name})
                if include_retrieval_unit_bm25:
                    indexes.append({"index_name": retrieval_unit_bm25_name})
                return _Cursor(rows=indexes)

        repository = object.__new__(PostgresRepository)
        repository.settings = SimpleNamespace(postgres_ann_min_rows=10_000)

        @contextmanager
        def connection():
            yield Connection()

        repository.connection = connection
        return repository.retrieval_capability_snapshot(profile)[
            ("kb_employee", "employee")
        ]

    missing_unit_bm25 = snapshot(
        include_unit_hnsw=True,
        include_retrieval_unit_bm25=False,
    )
    assert missing_unit_bm25.bm25_ready is False
    assert missing_unit_bm25.retrieval_unit_bm25_ready is False
    assert missing_unit_bm25.ann_ready(2560) is True

    assert snapshot(
        include_unit_hnsw=False,
        include_retrieval_unit_bm25=True,
    ).ann_ready(2560) is False
    ready = snapshot(
        include_unit_hnsw=True,
        include_retrieval_unit_bm25=True,
    )
    assert ready.row_count == 900
    assert ready.bm25_ready is True
    assert ready.retrieval_unit_bm25_ready is True
    assert ready.ann_required(2560) is True
    assert ready.ann_ready(2560) is True


def test_exact_fact_query_reads_units_and_is_scoped_and_version_filtered():
    statements: list[tuple[str, tuple]] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append((str(statement), tuple(params or ())))
            return _Cursor()

    client = object.__new__(PGVectorClient)
    rows = client._exact_rows(
        Connection(),
        terms=["g9", "asr rating"],
        normalized_query="g9 asr rating",
        top_k=16,
        where_sql=" AND (c.index_version = %s)",
        filter_params=["v3"],
        collection_name="kb_job_level",
        scope="job_level",
    )

    sql, params = statements[0]
    assert rows == []
    assert "FROM kb_retrieval_unit_exact_facts f" in sql
    assert "FROM kb_exact_facts f" in sql
    assert "f.document_frequency" in sql
    assert "strpos(q.normalized_query, f.normalized_value)" not in sql
    assert "c.index_version = %s" in sql
    assert params == (
        "kb_job_level",
        "job_level",
        "v3",
        ["g9", "asr rating"],
        "g9 asr rating",
    )


def test_organization_context_exact_runs_only_for_organization_scopes():
    statements: list[str] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append(str(statement))
            return _Cursor()

    client = object.__new__(PGVectorClient)
    organization_rows = client._exact_rows(
        Connection(),
        terms=[],
        normalized_query="mobility engineering",
        top_k=8,
        where_sql="",
        filter_params=[],
        collection_name="kb_organization_unit",
        scope="organization_unit",
    )
    assert organization_rows == []
    assert len(statements) == 1
    compact_statement = "".join(statements[0].split())
    assert (
        "strpos(q.normalized_query,f.normalized_value)"
        not in compact_statement
    )
    assert "regexp_replace(q.normalized_query" in compact_statement
    assert "[^[:alnum:]_/-]+" in compact_statement

    statements.clear()
    general_rows = client._exact_rows(
        Connection(),
        terms=[],
        normalized_query="leadership feedback",
        top_k=8,
        where_sql="",
        filter_params=[],
        collection_name="kb_general",
        scope="general",
    )
    assert general_rows == []
    assert statements == []


def test_exact_idf_prefers_rare_g9_unit_and_keeps_complete_parent():
    def row(
        *,
        chunk_id: str,
        normalized_value: str,
        document_frequency: int,
        unit_id: str | None = None,
        unit_text: str = "",
    ) -> dict:
        return {
            "match_source": "unit" if unit_id else "parent",
            "chunk_id": chunk_id,
            "source_id": f"{chunk_id}.md",
            "title": chunk_id,
            "scope": "job_level",
            "text": (
                "Level: G9\nOutput: broad impact\n"
                "Level: SL1\nOutput: business responsibility"
                if chunk_id == "rare-g9"
                else "WHAT and HOW guidance"
            ),
            "metadata": {},
            "unit_id": unit_id,
            "unit_type": "table" if unit_id else None,
            "unit_index": 0 if unit_id else None,
            "unit_text": unit_text if unit_id else None,
            "unit_search_text": (
                f"Job level table\n{unit_text}" if unit_id else None
            ),
            "unit_metadata": {},
            "fact_type": "business_code",
            "normalized_value": normalized_value,
            "source_quote": normalized_value.upper(),
            "document_frequency": document_frequency,
            "corpus_document_count": 100,
            "match_kind": "phrase",
        }

    rows = [
        row(
            chunk_id="common-performance",
            normalized_value="what",
            document_frequency=87,
        ),
        row(
            chunk_id="common-performance",
            normalized_value="how",
            document_frequency=87,
        ),
        row(
            chunk_id="rare-g9",
            normalized_value="g9",
            document_frequency=7,
            unit_id="ru-g9",
            unit_text="Level: G9\nOutput: broad impact",
        ),
        row(
            chunk_id="rare-g9",
            normalized_value="g9",
            document_frequency=7,
        ),
    ]

    ranked = PGVectorClient._rank_exact_fact_rows(
        rows,
        scope="job_level",
        top_k=8,
    )
    first = PGVectorClient._row_to_chunk(ranked[0])

    assert [item["chunk_id"] for item in ranked] == [
        "rare-g9",
        "common-performance",
    ]
    assert "Level: SL1" in first.text
    assert first.metadata["retrieval_unit"]["unit_id"] == "ru-g9"
    assert first.metadata["retrieval_unit"]["text"] == (
        "Level: G9\nOutput: broad impact"
    )
    assert first.metadata["retrieval_unit"]["exact_facts"][0][
        "source_quote"
    ] == "G9"


def test_exact_semantic_aliases_do_not_duplicate_score_or_inflate_idf():
    def fact(
        fact_type: str,
        normalized_value: str,
        document_frequency: int,
    ) -> dict:
        return {
            "fact_type": fact_type,
            "normalized_value": normalized_value,
            "source_quote": normalized_value,
            "document_frequency": document_frequency,
            "corpus_document_count": 100,
            "match_kind": "phrase",
        }

    raw_what = fact("business_code", "what", 87)
    canonical_what = fact("performance", "what dimension", 1)

    raw_score = PGVectorClient._exact_group_score(
        [raw_what],
        scope="employee",
    )
    alias_score = PGVectorClient._exact_group_score(
        [raw_what, canonical_what],
        scope="employee",
    )

    assert alias_score == pytest.approx(raw_score)


def test_exact_semantic_alias_frequency_keeps_g9_ahead_of_what_how():
    def row(
        *,
        chunk_id: str,
        fact_type: str,
        normalized_value: str,
        document_frequency: int,
    ) -> dict:
        return {
            "match_source": "parent",
            "chunk_id": chunk_id,
            "source_id": f"{chunk_id}.md",
            "title": chunk_id,
            "scope": "employee",
            "text": normalized_value,
            "metadata": {},
            "unit_id": None,
            "unit_type": None,
            "unit_index": None,
            "unit_text": None,
            "unit_search_text": None,
            "unit_metadata": {},
            "fact_type": fact_type,
            "normalized_value": normalized_value,
            "source_quote": normalized_value,
            "document_frequency": document_frequency,
            "corpus_document_count": 100,
            "match_kind": "phrase",
        }

    common_rows = [
        row(
            chunk_id="common-performance",
            fact_type="business_code",
            normalized_value="what",
            document_frequency=87,
        ),
        row(
            chunk_id="common-performance",
            fact_type="performance",
            normalized_value="what dimension",
            document_frequency=1,
        ),
        row(
            chunk_id="common-performance",
            fact_type="business_code",
            normalized_value="how",
            document_frequency=87,
        ),
        row(
            chunk_id="common-performance",
            fact_type="performance",
            normalized_value="how dimension",
            document_frequency=1,
        ),
    ]
    rare_row = row(
        chunk_id="rare-g9",
        fact_type="business_code",
        normalized_value="g9",
        document_frequency=7,
    )

    ranked = PGVectorClient._rank_exact_fact_rows(
        [*common_rows, rare_row],
        scope="employee",
        top_k=8,
    )

    assert [item["chunk_id"] for item in ranked] == [
        "rare-g9",
        "common-performance",
    ]


def test_exact_authoritative_scope_adds_only_the_bounded_scope_bonus():
    fact = {
        "fact_type": "career_element",
        "normalized_value": "cross-functional experience",
        "source_quote": "Cross Function",
        "document_frequency": 3,
        "corpus_document_count": 100,
        "match_kind": "phrase",
    }

    career_score = PGVectorClient._exact_group_score(
        [fact],
        scope="career",
    )
    general_score = PGVectorClient._exact_group_score(
        [fact],
        scope="general",
    )

    assert career_score - general_score == pytest.approx(0.45)
    assert PGVectorClient._exact_authority_bonus(
        fact_type="organization_path",
        normalized_value="bosch > mobility > engineering",
        scope="organization_unit",
    ) == pytest.approx(0.45)


def test_hybrid_scope_search_adds_weighted_exact_branch(monkeypatch):
    import backend.services.retrieval_service as retrieval_module

    calls: list[dict] = []
    exact_chunk = _chunk("g9-exact", 2.0, scope="job_level")

    class Client:
        def __init__(self, collection_name, **_kwargs):
            self.collection_name = collection_name

        def search_hybrid_by_embedding(
            self,
            query,
            query_embedding,
            *,
            exact_enabled=True,
            exact_top_k=16,
            **kwargs,
        ):
            calls.append(
                {
                    **kwargs,
                    "exact_enabled": exact_enabled,
                    "exact_top_k": exact_top_k,
                }
            )
            return retrieval_module.HybridSearchResult(
                dense=[],
                lexical=[],
                exact=[exact_chunk],
            )

    monkeypatch.setattr(retrieval_module, "PGVectorClient", Client)
    service = object.__new__(RetrievalService)
    service.settings = SimpleNamespace(
        collection_name_for_scope=lambda scope: f"kb_{scope}"
    )
    service.embedding_service = object()
    service.repository = object()
    service._search_limiter = threading.BoundedSemaphore(1)
    service._capabilities = None
    service._stores = {}
    service._stores_lock = threading.Lock()

    result = service._search_scope(
        query_index=0,
        query="G9 ASR rating",
        query_embedding=[1.0, 0.0],
        scope="job_level",
        dense_top_k=28,
        lexical_top_k=28,
        exact_top_k=12,
        metadata_filter={"index_version": "v3"},
        template_weight=1.1,
        dense_weight=1.0,
        lexical_weight=1.0,
        exact_weight=1.25,
        hybrid_enabled=True,
    )

    assert calls[0]["exact_enabled"] is True
    assert calls[0]["exact_top_k"] == 12
    assert [branch.modality for branch in result.branches] == [
        "dense",
        "bm25",
        "exact",
    ]
    assert result.branches[-1].chunks == [exact_chunk]
    assert result.branches[-1].weight == pytest.approx(1.1 * 1.25)


def test_missing_bm25_capability_skips_lexical_query_without_exception(monkeypatch):
    import backend.services.retrieval_service as retrieval_module

    calls: list[dict] = []

    class Client:
        def __init__(self, collection_name, embedding_service=None):
            self.collection_name = collection_name

        def search_hybrid_by_embedding(self, query, query_embedding, **kwargs):
            calls.append(kwargs)
            return retrieval_module.HybridSearchResult(dense=[], lexical=[])

    monkeypatch.setattr(retrieval_module, "PGVectorClient", Client)
    service = object.__new__(RetrievalService)
    service.settings = SimpleNamespace(
        collection_name_for_scope=lambda scope: f"kb_{scope}"
    )
    service.embedding_service = object()
    service._search_limiter = threading.BoundedSemaphore(1)
    service._capabilities = {
        ("kb_general", "general"): RetrievalScopeCapability(
            collection_name="kb_general",
            scope="general",
            row_count=100,
            bm25_ready=False,
            ann_dimensions=frozenset(),
        )
    }

    result = service._search_scope(
        query_index=0,
        query="query",
        query_embedding=[1.0, 0.0],
        scope="general",
        dense_top_k=18,
        lexical_top_k=18,
        metadata_filter={},
        template_weight=1.0,
        dense_weight=1.0,
        lexical_weight=1.0,
        hybrid_enabled=True,
    )

    assert calls[0]["lexical_enabled"] is False
    assert calls[0]["use_ann"] is False
    assert result.lexical_executed is False
    assert [branch.modality for branch in result.branches] == ["dense"]


@pytest.mark.asyncio
async def test_retrieval_singleflight_cancellation_removes_waiter_reference():
    cache = RetrievalResultCache()
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def holder() -> None:
        async with cache.singleflight("same-key"):
            holder_entered.set()
            await release_holder.wait()

    async def waiter() -> None:
        async with cache.singleflight("same-key"):
            raise AssertionError("cancelled waiter must not enter")

    holder_task = asyncio.create_task(holder())
    await holder_entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task
    release_holder.set()
    await holder_task

    assert cache._singleflight_locks == {}


@pytest.mark.asyncio
async def test_async_retrieval_singleflight_runs_reranker_once_for_same_key():
    class MemoryCache(RetrievalResultCache):
        def __init__(self):
            super().__init__()
            self.value = None

        async def aget(self, digest):
            return self.value

        async def aset(self, digest, chunks):
            self.value = chunks

    embedding_calls = 0
    rerank_calls = 0
    rerank_inputs = []

    class Embeddings:
        async def aembed_queries(self, queries):
            nonlocal embedding_calls
            embedding_calls += 1
            return [[1.0, 0.0] for _ in queries], [False for _ in queries]

    class AsyncReranker:
        async def arerank(
            self,
            chunks,
            *,
            query,
            top_k,
            instruction=None,
        ):
            nonlocal rerank_calls
            rerank_calls += 1
            rerank_inputs.append((query, instruction))
            await asyncio.sleep(0.02)
            return chunks[:top_k]

    plan = _RetrievalPlan(
        agent_name="guidance",
        query_specs=[
            _RenderedQuerySpec(
                query="query",
                scopes=["general"],
                weight=1.0,
                template_index=0,
            )
        ],
        rerank_query="single rerank query",
        rerank_instruction="Rank guidance evidence.",
        dense_top_k=18,
        lexical_top_k=18,
        rrf_k=60,
        fusion_top_n=46,
        rerank_candidate_top_n=32,
        rerank_units_per_parent=5,
        rerank_semantic_group_candidate_enabled=True,
        rerank_rank_fusion_k=60,
        rerank_rank_weight=12 / 7,
        first_stage_rank_weight=2 / 7,
        final_top_k=8,
        dense_weight=1.0,
        lexical_weight=1.0,
        hybrid_enabled=True,
        rerank_enabled=True,
        query_parallelism=4,
        metadata_filter={},
        cache_key="same-key",
    )
    executor = ThreadPoolExecutor(max_workers=2)
    service = object.__new__(RetrievalService)
    service.result_cache = MemoryCache()
    service.embedding_service = Embeddings()
    service.reranker = AsyncReranker()
    service._executor = executor
    service.settings = SimpleNamespace(rag_rerank_max_concurrency=46)
    service._build_plan = lambda agent_name, context, top_k: plan
    service._search_scope = lambda **kwargs: _SearchJobResult(
        branches=[
            _RankedBranch(
                branch_id="0:general:dense",
                modality="dense",
                scope="general",
                weight=1.0,
                chunks=[_chunk("shared", 0.9)],
            )
        ],
        lexical_error=None,
        semaphore_wait_ms=0.0,
        active_count=1,
    )

    try:
        results = await asyncio.gather(
            *(service.aretrieve("guidance", {}) for _ in range(8))
        )
    finally:
        executor.shutdown(wait=True)

    assert embedding_calls == 1
    assert rerank_calls == 1
    assert rerank_inputs == [
        ("single rerank query", "Rank guidance evidence.")
    ]
    assert all([chunk.chunk_id for chunk in result] == ["shared"] for result in results)
    fusion_metadata = results[0][0].metadata["retrieval_unit_rerank"]
    assert fusion_metadata["rerank_rank_weight"] == pytest.approx(12 / 7)
    assert fusion_metadata["first_stage_rank_weight"] == pytest.approx(2 / 7)


@pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is not configured",
)
def test_postgres_extensions_tokenizer_trigger_and_partial_indexes():
    from psycopg import sql

    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    repository = PostgresRepository(database_url)
    repository.settings = repository.settings.model_copy(
        update={"postgres_ann_min_rows": 1}
    )
    suffix = uuid4().hex[:10]
    collection = f"it_collection_{suffix}"
    scope = f"it_scope_{suffix}"
    chunks = [
        (f"{suffix}-1", "中文绩效反馈和行动计划", "[1,0,0]"),
        (f"{suffix}-2", "English performance feedback and follow-up", "[0.9,0.1,0]"),
        (f"{suffix}-3", "困难对话情绪支持", "[0,1,0]"),
    ]

    with repository.connection() as conn:
        for chunk_id, text, embedding in chunks:
            conn.execute(
                """
                INSERT INTO kb_chunks (
                    chunk_id, collection_name, doc_id, source_id, title, scope,
                    text, metadata, embedding, index_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s::vector, 'it')
                """,
                (
                    chunk_id,
                    collection,
                    f"doc-{chunk_id}",
                    f"source-{chunk_id}",
                    f"title-{chunk_id}",
                    scope,
                    text,
                    embedding,
                ),
            )

    hnsw_name = repository.hnsw_index_name(collection, scope, 3)
    bm25_name = repository.bm25_index_name(collection, scope)
    try:
        summary = repository.maintain_retrieval_indexes()
        assert hnsw_name in summary["hnsw_indexes"]
        assert bm25_name in summary["bm25_indexes"]

        with repository.connection() as conn:
            extension_rows = conn.execute(
                """
                SELECT extname, extversion
                FROM pg_extension
                WHERE extname = ANY(%s)
                """,
                (list(repository.REQUIRED_EXTENSION_VERSIONS),),
            ).fetchall()
            versions = {
                str(row["extname"]): str(row["extversion"])
                for row in extension_rows
            }
            assert all(
                repository.extension_version_is_supported(name, versions.get(name))
                for name in repository.REQUIRED_EXTENSION_VERSIONS
            )

            tokenized = conn.execute(
                "SELECT tokenizer_catalog.tokenize(%s, %s) AS tokens",
                ("中文 Feedback １２３ !!!", repository.settings.postgres_bm25_tokenizer_name),
            ).fetchone()
            assert tokenized["tokens"] is not None

            backfilled = conn.execute(
                "SELECT COUNT(*) AS populated FROM kb_chunks "
                "WHERE collection_name = %s AND scope = %s "
                "AND bm25_embedding IS NOT NULL",
                (collection, scope),
            ).fetchone()
            assert backfilled["populated"] == len(chunks)

            trigger_chunk_id = f"{suffix}-trigger"
            conn.execute(
                """
                INSERT INTO kb_chunks (
                    chunk_id, collection_name, doc_id, source_id, title, scope,
                    text, metadata, embedding, index_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, '[0,0,1]'::vector, 'it')
                """,
                (
                    trigger_chunk_id,
                    collection,
                    f"doc-{trigger_chunk_id}",
                    f"source-{trigger_chunk_id}",
                    f"title-{trigger_chunk_id}",
                    scope,
                    "触发器 trigger token",
                ),
            )
            populated = conn.execute(
                "SELECT bm25_embedding IS NOT NULL AS populated "
                "FROM kb_chunks WHERE chunk_id = %s",
                (trigger_chunk_id,),
            ).fetchone()
            assert populated["populated"] is True

            conn.execute("SET LOCAL enable_seqscan = off")
            dense_plan_rows = conn.execute(
                f"""
                EXPLAIN (COSTS OFF)
                SELECT chunk_id
                FROM kb_chunks
                WHERE collection_name = %s
                  AND scope = %s
                  AND vector_dims(embedding) = 3
                ORDER BY embedding::vector(3) <=> '[1,0,0]'::vector(3)
                LIMIT 2
                """,
                (collection, scope),
            ).fetchall()
            dense_plan = "\n".join(row["QUERY PLAN"] for row in dense_plan_rows)
            assert hnsw_name in dense_plan

            bm25_plan_rows = conn.execute(
                f"""
                EXPLAIN (COSTS OFF)
                SELECT chunk_id
                FROM kb_chunks
                WHERE collection_name = %s
                  AND scope = %s
                  AND bm25_embedding IS NOT NULL
                ORDER BY bm25_embedding <&> bm25_catalog.to_bm25query(
                    'public.{bm25_name}'::regclass,
                    tokenizer_catalog.tokenize(
                        %s,
                        %s
                    )
                )
                LIMIT 2
                """,
                (
                    collection,
                    scope,
                    "绩效反馈",
                    repository.settings.postgres_bm25_tokenizer_name,
                ),
            ).fetchall()
            bm25_plan = "\n".join(row["QUERY PLAN"] for row in bm25_plan_rows)
            assert bm25_name in bm25_plan
    finally:
        with repository.connection() as conn:
            conn.execute(
                "DELETE FROM kb_chunks WHERE collection_name = %s",
                (collection,),
            )
        with repository.autocommit_connection() as conn:
            for index_name in (hnsw_name, bm25_name):
                conn.execute(
                    sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}.{}").format(
                        sql.Identifier("public"),
                        sql.Identifier(index_name),
                    )
                )


def test_pgvector_row_to_chunk_does_not_require_search_text_column():
    chunk = PGVectorClient._row_to_chunk(
        {
            "chunk_id": "chunk-1",
            "source_id": "career/career.md",
            "title": "Career Elements",
            "scope": "career",
            "text": "Cross-functional experience",
            "search_text": (
                "scope: career\nheading: Career Elements\n"
                "Cross-functional experience"
            ),
            "metadata": {"doc_id": "career-doc"},
            "score": 0.8,
        }
    )

    assert chunk.text == "Cross-functional experience"
    assert "search_text" not in chunk.metadata
    assert chunk.metadata["doc_id"] == "career-doc"

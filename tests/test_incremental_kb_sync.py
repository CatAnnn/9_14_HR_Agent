from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.retrieval import RetrievedChunk
from backend.services.retrieval_service import RetrievalService
from backend.vectorstore.index_manager import IndexManager


def _settings(tmp_path):
    return SimpleNamespace(
        data_dir=tmp_path,
        effective_embedding_provider="bosch",
        effective_embedding_model="embedding-model",
        embedding_dimensions=2,
        kb_index_version="v2",
        kb_chunk_size=1200,
        kb_chunk_overlap=120,
        mineru_enabled=True,
        mineru_backend="pipeline",
        mineru_effort="high",
        mineru_parse_method="auto",
        mineru_lang="ch",
        document_vision_enabled=True,
        document_vision_model="vision-model",
        document_vision_prompt_version="v1",
        llm_document_vision_enable_thinking=True,
        collection_name_for_scope=lambda scope: f"kb_{scope}",
    )


def _manager(tmp_path) -> IndexManager:
    manager = object.__new__(IndexManager)
    manager.settings = _settings(tmp_path)
    manager.embedding_service = object()
    manager._image_analysis_summary = {"enabled": True}
    manager._image_analysis_warnings = []
    return manager


def _ready_profile():
    payload = {
        "profile_id": "profile-id",
        "active_build_id": "build-id",
        "status": "ready",
        "ready": True,
    }
    return SimpleNamespace(
        **payload,
        as_dict=lambda: dict(payload),
    )


def _current_state(
    source: dict[str, Any],
    fingerprint: str,
    *,
    chunk_count: int = 1,
) -> dict[str, Any]:
    return {
        "doc_id": source["doc_id"],
        "scope": source["scope"],
        "source_path": str(
            source.get("path") or source["relative_path"]
        ),
        "relative_path": source["relative_path"],
        "content_hash": source["content_hash"],
        "metadata": {
            "chunk_count": chunk_count,
            "ingestion_fingerprint": fingerprint,
            "embedding_model": source.get(
                "embedding_model", "embedding-model"
            ),
            "embedding_dimensions": source["embedding_dimensions"],
            "index_version": source.get("index_version", "v2"),
            "dataset": source.get("dataset", "core"),
        },
        "chunk_count": chunk_count,
        "min_dimension": source["embedding_dimensions"],
        "max_dimension": source["embedding_dimensions"],
        "min_collection_name": source["collection_name"],
        "max_collection_name": source["collection_name"],
    }


def test_incremental_sync_skips_unchanged_documents(tmp_path):
    raw_root = tmp_path / "kb_raw"
    source_path = raw_root / "performance" / "guide.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("unchanged knowledge", encoding="utf-8")
    manager = _manager(tmp_path)
    source = manager._discover_sources(raw_root)[0]
    fingerprint = manager._ingestion_fingerprint()
    maintenance_calls: list[tuple[str, object]] = []
    expected_indexes = {
        "hnsw_indexes": ["employee"],
        "bm25_indexes": ["performance"],
    }

    class Repository:
        @staticmethod
        def knowledge_document_states(index_version=None):
            assert index_version == manager.settings.kb_index_version
            return {
                source["doc_id"]: _current_state(source, fingerprint)
            }

        @staticmethod
        def require_active_embedding_profile(model_name, dimensions):
            return _ready_profile()

        @staticmethod
        def count_missing_bm25_embeddings():
            maintenance_calls.append(("count_missing_bm25", None))
            return 0

        @staticmethod
        def maintain_retrieval_indexes(**kwargs):
            maintenance_calls.append(("maintain", kwargs))
            return expected_indexes

    manager.postgres_repo = Repository()
    manager._build_chunks = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("unchanged documents must not be parsed")
    )
    manager._embed_chunks = lambda chunks: (_ for _ in ()).throw(
        AssertionError("unchanged documents must not be embedded")
    )

    summary = manager._sync_changed_locked(
        raw_root,
        include_resources=False,
    )

    assert summary["changed_document_count"] == 0
    assert summary["unchanged_document_count"] == 1
    assert summary["removed_document_count"] == 0
    assert summary["vector_count"] == 0
    assert maintenance_calls == [
        ("count_missing_bm25", None),
        (
            "maintain",
            {
                "rebuild_lexical": False,
                "drop_legacy_hnsw": True,
            },
        ),
    ]
    assert summary["retrieval_indexes"] == expected_indexes


def test_incremental_sync_adopts_compatible_legacy_documents(tmp_path):
    raw_root = tmp_path / "kb_raw"
    source_path = raw_root / "career" / "guide.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("legacy knowledge", encoding="utf-8")
    manager = _manager(tmp_path)
    source = manager._discover_sources(raw_root)[0]
    fingerprint = manager._ingestion_fingerprint()
    legacy_state = _current_state(source, "")
    stamped: list[tuple[set[str], str]] = []
    maintenance_calls: list[tuple[str, object]] = []
    expected_indexes = {"hnsw_indexes": ["career"]}

    class Repository:
        @staticmethod
        def knowledge_document_states(index_version=None):
            assert index_version == manager.settings.kb_index_version
            return {source["doc_id"]: legacy_state}

        @staticmethod
        def stamp_knowledge_document_fingerprints(doc_ids, value):
            stamped.append((set(doc_ids), value))
            return len(doc_ids)

        @staticmethod
        def require_active_embedding_profile(model_name, dimensions):
            return _ready_profile()

        @staticmethod
        def count_missing_bm25_embeddings():
            maintenance_calls.append(("count_missing_bm25", None))
            return 0

        @staticmethod
        def maintain_retrieval_indexes(**kwargs):
            maintenance_calls.append(("maintain", kwargs))
            return expected_indexes

    manager.postgres_repo = Repository()
    manager._build_chunks = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("compatible legacy documents must not be parsed")
    )
    manager._embed_chunks = lambda chunks: (_ for _ in ()).throw(
        AssertionError("compatible legacy documents must not be embedded")
    )

    summary = manager._sync_changed_locked(
        raw_root,
        include_resources=False,
    )

    assert stamped == [({source["doc_id"]}, fingerprint)]
    assert summary["adopted_document_count"] == 1
    assert summary["changed_document_count"] == 0
    assert summary["vector_count"] == 0
    assert maintenance_calls == [
        ("count_missing_bm25", None),
        (
            "maintain",
            {
                "rebuild_lexical": False,
                "drop_legacy_hnsw": True,
            },
        ),
    ]
    assert summary["retrieval_indexes"] == expected_indexes


def test_incremental_sync_embeds_only_changed_documents(
    monkeypatch,
    tmp_path,
):
    import backend.vectorstore.index_manager as index_manager_module

    raw_root = tmp_path / "kb_raw"
    scope_root = raw_root / "performance"
    scope_root.mkdir(parents=True)
    (scope_root / "stable.md").write_text("stable", encoding="utf-8")
    (scope_root / "changed.md").write_text("new content", encoding="utf-8")
    manager = _manager(tmp_path)
    sources = manager._discover_sources(raw_root)
    by_name = {source["source_id"]: source for source in sources}
    fingerprint = manager._ingestion_fingerprint()
    stable = by_name["performance/stable.md"]
    changed = by_name["performance/changed.md"]
    changed_state = _current_state(changed, fingerprint)
    changed_state["content_hash"] = "old-content-hash"
    events: list[tuple[Any, ...]] = []

    class Connection:
        def execute(self, statement, params=None):
            events.append(("sql", str(statement), params))

    connection = Connection()

    class Repository:
        @staticmethod
        def knowledge_document_states(index_version=None):
            assert index_version == manager.settings.kb_index_version
            return {
                stable["doc_id"]: _current_state(stable, fingerprint),
                changed["doc_id"]: changed_state,
            }

        @contextmanager
        def connection(self):
            events.append(("transaction_begin", connection))
            yield connection
            events.append(("transaction_commit", connection))

        @staticmethod
        def begin_embedding_profile_build(conn, **kwargs):
            events.append(("profile_begin", kwargs))
            return "profile-id", "build-id"

        @staticmethod
        def copy_active_embedding_rows(conn, **kwargs):
            events.append(("profile_copy", kwargs))
            return 1

        @staticmethod
        def activate_embedding_profile_build(conn, **kwargs):
            events.append(("profile_activate", kwargs))
            return _ready_profile()

        @staticmethod
        def replace_knowledge_structure(conn, *, document, chunks):
            events.append(
                ("structure", document["doc_id"], len(chunks), conn)
            )

        @staticmethod
        def count_missing_bm25_embeddings():
            return 0

        @staticmethod
        def maintain_retrieval_indexes(**kwargs):
            events.append(("maintain", kwargs))
            return {"bm25_indexes": ["performance"]}

    manager.postgres_repo = Repository()
    built_source_ids: list[set[str]] = []
    embedded_chunk_ids: list[str] = []
    chunk = RetrievedChunk(
        chunk_id="changed-chunk",
        source_id=changed["source_id"],
        title="changed",
        scope="performance",
        text="new content",
        metadata={
            "doc_id": changed["doc_id"],
            "content_hash": changed["content_hash"],
            "index_version": "v2",
        },
    )
    document = {
        "doc_id": changed["doc_id"],
        "scope": "performance",
        "source_path": str(changed["path"]),
        "relative_path": changed["source_id"],
        "content_hash": changed["content_hash"],
        "chunk_count": 1,
        "ingestion_fingerprint": fingerprint,
    }

    def build_chunks(root, **kwargs):
        assert root == raw_root
        selected = set(kwargs["include_source_ids"])
        built_source_ids.append(selected)
        return {"performance": [chunk]}, [document]

    def embed_chunks(chunks):
        embedded_chunk_ids.extend(item.chunk_id for item in chunks)
        return chunks, [[0.25, 0.75] for _ in chunks]

    manager._build_chunks = build_chunks
    manager._embed_chunks = embed_chunks

    class Store:
        def __init__(self, **kwargs):
            events.append(("store", kwargs["collection_name"]))

        @staticmethod
        def upsert_document(payload, *, connection):
            events.append(("document", payload["doc_id"], connection))

        @staticmethod
        def upsert_chunks(
            chunks,
            embeddings,
            *,
            connection,
            embedding_profile_id,
            embedding_build_id,
        ):
            events.append(("chunks", len(chunks), connection))
            return len(chunks)

    monkeypatch.setattr(index_manager_module, "PGVectorClient", Store)
    monkeypatch.setattr(
        RetrievalService,
        "clear_result_cache",
        staticmethod(lambda: events.append(("cache_clear",))),
    )

    summary = manager._sync_changed_locked(
        raw_root,
        include_resources=False,
    )

    assert built_source_ids == [{"performance/changed.md"}]
    assert embedded_chunk_ids == ["changed-chunk"]
    assert summary["changed_document_count"] == 1
    assert summary["unchanged_document_count"] == 1
    assert summary["vector_count"] == 1
    assert any(event[0] == "maintain" for event in events)
    assert events.index(("transaction_begin", connection)) < next(
        index for index, event in enumerate(events) if event[0] == "document"
    )
    assert next(
        index for index, event in enumerate(events) if event[0] == "chunks"
    ) < events.index(("transaction_commit", connection))


def test_incremental_sync_prunes_removed_documents(monkeypatch, tmp_path):
    raw_root = tmp_path / "kb_raw"
    source_path = raw_root / "career" / "active.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("active", encoding="utf-8")
    manager = _manager(tmp_path)
    source = manager._discover_sources(raw_root)[0]
    fingerprint = manager._ingestion_fingerprint()
    stale_doc_id = "removed.md__stale"
    organization_doc_id = "organization-unit-stays"
    events: list[tuple[Any, ...]] = []

    class Result:
        def __init__(self, *, rows=None, rowcount=0):
            self._rows = rows or []
            self.rowcount = rowcount

        def fetchall(self):
            return self._rows

    class Connection:
        def execute(self, statement, params=None):
            sql = str(statement)
            events.append(("sql", sql, params))
            if "SELECT DISTINCT collection_name" in sql:
                return Result(rows=[])
            if "DELETE FROM kb_chunks WHERE doc_id" in sql:
                return Result(rowcount=2)
            if "DELETE FROM kb_documents" in sql:
                return Result(rowcount=1)
            return Result(rowcount=0)

    connection = Connection()

    class Repository:
        @staticmethod
        def knowledge_document_states(index_version=None):
            assert index_version == manager.settings.kb_index_version
            return {
                source["doc_id"]: _current_state(source, fingerprint),
                stale_doc_id: {
                    "doc_id": stale_doc_id,
                    "scope": "career",
                    "relative_path": "career/removed.md",
                    "content_hash": "removed",
                    "metadata": {},
                    "chunk_count": 1,
                },
                organization_doc_id: {
                    "doc_id": organization_doc_id,
                    "scope": "organization_unit",
                    "source_path": "/old/organization_unit.xlsx",
                    "relative_path": (
                        "organization_unit/organization_unit.xlsx"
                    ),
                    "content_hash": "organization",
                    "metadata": {"dataset": "organization_unit"},
                    "chunk_count": 1,
                },
            }

        @contextmanager
        def connection(self):
            events.append(("transaction_begin", connection))
            yield connection
            events.append(("transaction_commit", connection))

        @staticmethod
        def begin_embedding_profile_build(conn, **kwargs):
            return "profile-id", "build-id"

        @staticmethod
        def copy_active_embedding_rows(conn, **kwargs):
            events.append(("profile_copy", kwargs))
            return 1

        @staticmethod
        def activate_embedding_profile_build(conn, **kwargs):
            return _ready_profile()

        @staticmethod
        def count_missing_bm25_embeddings():
            return 0

        @staticmethod
        def maintain_retrieval_indexes(**kwargs):
            events.append(("maintain", kwargs))
            return {"bm25_indexes": ["career"]}

    manager.postgres_repo = Repository()
    manager._build_chunks = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("unchanged active document must not be parsed")
    )
    manager._embed_chunks = lambda chunks: (_ for _ in ()).throw(
        AssertionError("unchanged active document must not be embedded")
    )
    monkeypatch.setattr(
        RetrievalService,
        "clear_result_cache",
        staticmethod(lambda: events.append(("cache_clear",))),
    )

    summary = manager._sync_changed_locked(
        raw_root,
        include_resources=False,
    )

    assert summary["changed_document_count"] == 0
    assert summary["removed_documents"] == [stale_doc_id]
    copied = next(event for event in events if event[0] == "profile_copy")
    assert organization_doc_id not in copied[1]["excluded_doc_ids"]
    assert summary["pruned_chunks"] == 2
    assert summary["pruned_documents"] == 1
    maintain = next(event for event in events if event[0] == "maintain")
    assert maintain[1]["rebuild_lexical"] is True


def test_ingestion_fingerprint_change_invalidates_document(tmp_path):
    manager = _manager(tmp_path)
    source = {
        "doc_id": "career-guide",
        "scope": "career",
        "relative_path": "career/guide.md",
        "content_hash": "same-hash",
        "collection_name": "kb_career",
        "embedding_dimensions": 2,
    }
    state = _current_state(source, "old-fingerprint")

    assert not manager._source_is_current(
        source,
        state,
        ingestion_fingerprint="new-fingerprint",
    )


def test_startup_sync_releases_advisory_lock_after_failure(tmp_path):
    raw_root = tmp_path / "kb_raw"
    raw_root.mkdir()
    (raw_root / "guide.md").write_text("guide", encoding="utf-8")
    manager = _manager(tmp_path)
    statements: list[str] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append(str(statement))

    class Repository:
        @contextmanager
        def autocommit_connection(self):
            yield Connection()

    manager.postgres_repo = Repository()
    manager._reset_image_analysis_summary = lambda: None
    manager._sync_changed_locked = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("sync failed")
    )

    with pytest.raises(RuntimeError, match="sync failed"):
        manager.sync_changed(raw_root, dataset="core")

    assert "pg_advisory_lock" in statements[0]
    assert "pg_advisory_unlock" in statements[-1]


def test_startup_sync_does_not_wait_when_another_backend_owns_lock(tmp_path):
    raw_root = tmp_path / "kb_raw"
    raw_root.mkdir()
    (raw_root / "guide.md").write_text("guide", encoding="utf-8")
    manager = _manager(tmp_path)
    statements: list[str] = []

    class Result:
        @staticmethod
        def fetchone():
            return {"acquired": False}

    class Connection:
        def execute(self, statement, params=None):
            statements.append(str(statement))
            return Result()

    class Repository:
        @contextmanager
        def autocommit_connection(self):
            yield Connection()

    manager.postgres_repo = Repository()
    manager._reset_image_analysis_summary = lambda: None
    manager._sync_changed_locked = lambda *args, **kwargs: pytest.fail(
        "A backend that did not acquire the lock must not start a sync."
    )

    assert manager.try_sync_changed(raw_root, dataset="core") is None
    assert len(statements) == 1
    assert "pg_try_advisory_lock" in statements[0]
    assert "pg_advisory_unlock" not in statements[0]


def test_check_sources_all_uses_both_roots_and_preserves_source_id(tmp_path):
    core_file = tmp_path / "kb_raw" / "performance" / "guide.md"
    core_file.parent.mkdir(parents=True)
    core_file.write_text("core", encoding="utf-8")
    organization_file = (
        tmp_path
        / "kb_large"
        / "organization_unit"
        / "organization_unit.xlsx"
    )
    organization_file.parent.mkdir(parents=True)
    organization_file.write_bytes(b"placeholder")
    manager = _manager(tmp_path)

    checked = manager.check_sources(dataset="all")

    assert checked["datasets"] == ["core", "organization_unit"]
    assert checked["source_roots"] == {
        "core": str(tmp_path / "kb_raw"),
        "organization_unit": str(tmp_path / "kb_large"),
    }
    assert checked["source_count"] == 2
    organization_source = next(
        source
        for source in checked["sources"]
        if source["dataset"] == "organization_unit"
    )
    assert (
        organization_source["source_id"]
        == "organization_unit/organization_unit.xlsx"
    )


def test_check_sources_rejects_missing_required_root_before_db_access(tmp_path):
    core_file = tmp_path / "kb_raw" / "performance" / "guide.md"
    core_file.parent.mkdir(parents=True)
    core_file.write_text("core", encoding="utf-8")
    manager = _manager(tmp_path)
    manager.postgres_repo = SimpleNamespace(
        knowledge_document_states=lambda **kwargs: pytest.fail(
            "source validation must fail before database access"
        )
    )

    with pytest.raises(FileNotFoundError, match="organization_unit"):
        manager.check_sources(dataset="all")


def test_check_sources_requires_explicit_dataset_for_raw_dir(tmp_path):
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="explicit dataset"):
        manager.check_sources(tmp_path, dataset="all")


def test_check_sources_rejects_duplicate_source_ids_across_roots(tmp_path):
    relative_path = "organization_unit/duplicate.md"
    for root_name in ("kb_raw", "kb_large"):
        source_path = tmp_path / root_name / relative_path
        source_path.parent.mkdir(parents=True)
        source_path.write_text(root_name, encoding="utf-8")
    manager = _manager(tmp_path)

    with pytest.raises(RuntimeError, match="重复.*source_id"):
        manager.check_sources(dataset="all")


def test_incremental_sync_relocates_current_document_without_rebuild(tmp_path):
    raw_root = tmp_path / "kb_large"
    source_path = raw_root / "organization_unit" / "guide.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("organization", encoding="utf-8")
    manager = _manager(tmp_path)
    source = manager._discover_sources(
        raw_root,
        dataset="organization_unit",
    )[0]
    fingerprint = manager._ingestion_fingerprint(
        dataset="organization_unit"
    )
    state = _current_state(source, fingerprint)
    state["source_path"] = str(
        tmp_path / "kb_raw" / "organization_unit" / "guide.md"
    )
    relocations: list[list[dict[str, str]]] = []
    maintenance_calls: list[tuple[str, object]] = []
    expected_indexes = {"hnsw_indexes": ["organization_unit"]}

    class Repository:
        @staticmethod
        def knowledge_document_states(index_version=None):
            return {source["doc_id"]: state}

        @staticmethod
        def require_active_embedding_profile(model_name, dimensions):
            return _ready_profile()

        @staticmethod
        def relocate_knowledge_document_sources(items, *, connection=None):
            assert connection is None
            relocations.append(list(items))
            return len(items)

        @staticmethod
        def count_missing_bm25_embeddings():
            maintenance_calls.append(("count_missing_bm25", None))
            return 0

        @staticmethod
        def maintain_retrieval_indexes(**kwargs):
            maintenance_calls.append(("maintain", kwargs))
            return expected_indexes

    manager.postgres_repo = Repository()
    manager._build_chunks = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("a path-only relocation must not parse documents")
    )
    manager._embed_chunks = lambda chunks: (_ for _ in ()).throw(
        AssertionError("a path-only relocation must not embed documents")
    )

    summary = manager._sync_changed_locked(
        raw_root,
        include_resources=False,
        dataset="organization_unit",
    )

    assert relocations == [[{
        "doc_id": source["doc_id"],
        "source_path": str(source_path),
    }]]
    assert summary["dataset"] == "organization_unit"
    assert summary["changed_document_count"] == 0
    assert summary["relocated_document_count"] == 1
    assert summary["vector_count"] == 0
    assert maintenance_calls == [
        ("count_missing_bm25", None),
        (
            "maintain",
            {
                "rebuild_lexical": False,
                "drop_legacy_hnsw": True,
            },
        ),
    ]
    assert summary["retrieval_indexes"] == expected_indexes


def test_repository_relocates_document_and_chunk_metadata_in_one_connection():
    statements: list[tuple[str, tuple[Any, ...] | None]] = []

    class Result:
        rowcount = 1

    class Connection:
        def execute(self, statement, params=None):
            statements.append((str(statement), params))
            return Result()

    repository = object.__new__(PostgresRepository)
    updated = repository.relocate_knowledge_document_sources(
        [{"doc_id": "doc-1", "source_path": "/data/kb_large/unit.xlsx"}],
        connection=Connection(),
    )

    assert updated == 1
    assert len(statements) == 2
    assert "UPDATE kb_documents" in statements[0][0]
    assert "'{input_path}'" in statements[0][0]
    assert "'{parse_metadata}'" in statements[0][0]
    assert statements[0][1][-1] == "doc-1"
    assert "UPDATE kb_chunks" in statements[1][0]
    assert "'{source_path}'" in statements[1][0]
    assert statements[1][1] == ("/data/kb_large/unit.xlsx", "doc-1")

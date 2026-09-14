from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from backend.config.settings import Settings, get_settings
from backend.exceptions.parser_errors import UnsupportedFileTypeError
from backend.parsers.organization_unit_xlsx import OrganizationUnitXlsxParser
from backend.parsers.parser_router import ParserRouter
from backend.rag.chunking import (
    ORGANIZATION_CHUNKING_VERSION,
    chunk_blocks,
    chunk_organization_text,
    chunk_text,
)
from backend.rag.structured_facts import (
    canonical_content_id,
    contextual_exact_fact_text,
    contextual_search_text,
    extract_exact_facts,
    normalize_content,
)
from backend.repositories.manifest_repository import ManifestRepository
from backend.repositories.postgres_repository import PostgresRepository
from backend.repositories.vector_index_repository import VectorIndexRepository
from backend.schemas.retrieval import RetrievedChunk
from backend.services.embedding_service import EmbeddingService
from backend.vectorstore.embedding_profile import (
    embedding_profile_corpus,
    normalize_embedding_dimensions,
    normalize_embedding_model_name,
    resolve_embedding_dimensions,
)
from backend.vectorstore.pgvector_client import PGVectorClient


class IndexManager:
    """Build KB chunks, embed them, and write them into PostgreSQL pgvector."""

    _SUPPORTED_EXTENSIONS = {".md", ".xlsx"}
    _STARTUP_SYNC_LOCK = "hr_agent_kb_incremental_startup_sync"
    _DATASETS = ("core", "organization_unit")
    _ORGANIZATION_SEARCH_NAVIGATION_SEGMENTS = frozenset(
        {
            "division & subsidiaries",
            "division and subsidiaries",
            "divisions & subsidiaries",
            "divisions and subsidiaries",
            "location",
            "locations",
            "organisation",
            "organisations",
            "organization",
            "organizations",
            "themen",
            "themen a-z",
            "themen von a bis z",
            "themen von a-z",
            "topic index a-z",
            "topics",
            "topics a-z",
            "topics from a to z",
        }
    )

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        embedding_service: Any | None = None,
        initialize_repository: bool = True,
    ):
        self.settings = settings or get_settings()
        self.parser = ParserRouter()
        self.embedding_service = (
            embedding_service
            if embedding_service is not None
            else EmbeddingService(settings=self.settings)
        )
        self.manifest_repo = ManifestRepository()
        self.vector_repo = VectorIndexRepository()
        self.postgres_repo = PostgresRepository(
            database_url=self.settings.admin_database_url,
            initialize=initialize_repository,
        )
        self.last_summary: dict[str, Any] = {}
        self._image_analysis_summary = self._empty_image_analysis_stats()
        self._image_analysis_warnings: list[str] = []

    def sync_changed(
        self,
        raw_dir: Path | None = None,
        *,
        dataset: str = "all",
    ) -> dict[str, Any]:
        summary = self._sync_changed_with_lock(
            raw_dir,
            dataset=dataset,
            wait_for_lock=True,
        )
        if summary is None:  # pragma: no cover - blocking locks always acquire.
            raise RuntimeError("Knowledge base startup lock was not acquired.")
        return summary

    def try_sync_changed(
        self,
        raw_dir: Path | None = None,
        *,
        dataset: str = "all",
    ) -> dict[str, Any] | None:
        """Sync when this process wins the startup lock, otherwise return."""
        return self._sync_changed_with_lock(
            raw_dir,
            dataset=dataset,
            wait_for_lock=False,
        )

    def check_sources(
        self,
        raw_dir: Path | None = None,
        *,
        dataset: str = "all",
    ) -> dict[str, Any]:
        """Validate and describe source files without accessing the database."""
        plan = self._source_plan(raw_dir=raw_dir, dataset=dataset)
        return {
            "dataset": plan["dataset"],
            "datasets": list(plan["datasets"]),
            "source_roots": {
                name: str(path)
                for name, path in plan["source_roots"].items()
            },
            "source_count": len(plan["sources"]),
            "sources": [
                {
                    "dataset": source["dataset"],
                    "source_id": source["source_id"],
                    "scope": source["scope"],
                    "path": str(source["path"]),
                    "collection_name": source["collection_name"],
                    "content_hash": source["content_hash"],
                }
                for source in plan["sources"]
            ],
        }

    def _source_plan(
        self,
        *,
        raw_dir: Path | None,
        dataset: str,
    ) -> dict[str, Any]:
        normalized_dataset = str(dataset or "").strip().lower()
        if normalized_dataset not in {"all", *self._DATASETS}:
            raise ValueError(
                "dataset must be 'all', 'core', or 'organization_unit'."
            )
        if raw_dir is not None and normalized_dataset == "all":
            raise ValueError(
                "raw_dir can override only an explicit dataset; "
                "choose dataset='core' or dataset='organization_unit'."
            )

        datasets = (
            self._DATASETS
            if normalized_dataset == "all"
            else (normalized_dataset,)
        )
        source_roots: dict[str, Path] = {}
        groups: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        for dataset_name in datasets:
            root = (
                Path(raw_dir)
                if raw_dir is not None
                else self._dataset_source_root(dataset_name)
            )
            if not root.is_dir():
                raise FileNotFoundError(
                    f"Required KB source directory does not exist "
                    f"for dataset '{dataset_name}': {root}"
                )
            group_sources = self._discover_sources(
                root,
                dataset=dataset_name,
            )
            source_roots[dataset_name] = root
            groups.append(
                {
                    "dataset": dataset_name,
                    "root": root,
                    "scope_override": None,
                    "source_prefix": "",
                    "sources": group_sources,
                }
            )
            sources.extend(group_sources)

        if not sources:
            roots = ", ".join(str(path) for path in source_roots.values())
            raise RuntimeError(f"没有可索引的 KB 文件: {roots}")
        seen_source_ids: set[str] = set()
        duplicate_source_ids: set[str] = set()
        for source in sources:
            source_id = str(source["source_id"])
            if source_id in seen_source_ids:
                duplicate_source_ids.add(source_id)
            seen_source_ids.add(source_id)
        if duplicate_source_ids:
            duplicates = ", ".join(sorted(duplicate_source_ids))
            raise RuntimeError(
                "知识库数据源之间存在重复的文档 source_id: "
                f"{duplicates}"
            )
        return {
            "dataset": normalized_dataset,
            "datasets": tuple(datasets),
            "source_roots": source_roots,
            "groups": groups,
            "sources": sources,
        }

    def _dataset_source_root(self, dataset: str) -> Path:
        attribute = (
            "kb_core_dir"
            if dataset == "core"
            else "kb_organization_unit_dir"
        )
        configured = getattr(self.settings, attribute, None)
        if configured is not None:
            return Path(configured)
        suffix = "kb_raw" if dataset == "core" else "kb_large"
        return Path(self.settings.data_dir) / suffix

    @staticmethod
    def _state_dataset(state: dict[str, Any]) -> str:
        metadata = state.get("metadata")
        stored_dataset = (
            str(metadata.get("dataset") or "").strip()
            if isinstance(metadata, dict)
            else ""
        )
        if stored_dataset in IndexManager._DATASETS:
            return stored_dataset
        return (
            "organization_unit"
            if str(state.get("scope") or "") == "organization_unit"
            else "core"
        )

    @staticmethod
    def _source_path_changed(
        source: dict[str, Any],
        state: dict[str, Any] | None,
    ) -> bool:
        if not state:
            return False
        metadata = state.get("metadata")
        stored_path = str(state.get("source_path") or "").strip()
        if not stored_path and isinstance(metadata, dict):
            stored_path = str(metadata.get("source_path") or "").strip()
        return bool(stored_path) and stored_path != str(source["path"])

    def _sync_changed_with_lock(
        self,
        raw_dir: Path | None,
        *,
        dataset: str,
        wait_for_lock: bool,
    ) -> dict[str, Any] | None:
        source_plan = self._source_plan(raw_dir=raw_dir, dataset=dataset)
        started_at = time.time()
        self._reset_image_analysis_summary()
        with self.postgres_repo.autocommit_connection() as lock_conn:
            if wait_for_lock:
                lock_conn.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    (self._STARTUP_SYNC_LOCK,),
                )
            else:
                lock_row = lock_conn.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
                    (self._STARTUP_SYNC_LOCK,),
                ).fetchone()
                if not lock_row or not bool(lock_row["acquired"]):
                    return None
            try:
                summary = self._sync_changed_locked(
                    None,
                    include_resources=False,
                    dataset=source_plan["dataset"],
                    source_plan=source_plan,
                )
                summary["elapsed_seconds"] = round(
                    time.time() - started_at,
                    3,
                )
                self.last_summary = summary
                self.manifest_repo.save(summary)
                self.vector_repo.save(summary)
                return summary
            finally:
                lock_conn.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))",
                    (self._STARTUP_SYNC_LOCK,),
                )

    def _sync_changed_locked(
        self,
        raw_root: Path | None,
        *,
        include_resources: bool,
        dataset: str = "core",
        source_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del include_resources
        plan = source_plan or self._source_plan(
            raw_dir=raw_root,
            dataset=dataset,
        )
        source_groups = plan["groups"]
        sources = plan["sources"]
        selected_datasets = set(plan["datasets"])
        summary_raw_root = plan["source_roots"][plan["datasets"][0]]

        existing_states = self.postgres_repo.knowledge_document_states(
            index_version=self.settings.kb_index_version,
        )
        ingestion_fingerprints = {
            dataset_name: self._ingestion_fingerprint(dataset=dataset_name)
            for dataset_name in plan["datasets"]
        }
        legacy_ingestion_fingerprints = (
            self._legacy_ingestion_fingerprints(existing_states)
        )
        current_source_ids = {
            str(source["source_id"])
            for source in sources
            if self._source_is_current(
                source,
                existing_states.get(str(source["doc_id"])),
                ingestion_fingerprint=ingestion_fingerprints[
                    str(source["dataset"])
                ],
                legacy_ingestion_fingerprints=(
                    legacy_ingestion_fingerprints
                    if str(source["dataset"]) == "core"
                    else frozenset()
                ),
            )
        }
        changed_sources = [
            source
            for source in sources
            if str(source["source_id"]) not in current_source_ids
        ]
        changed_source_ids = {
            str(source["source_id"]) for source in changed_sources
        }
        adopted_doc_ids = {
            str(source["doc_id"])
            for source in sources
            if str(source["dataset"]) == "core"
            and self._is_legacy_current_source(
                source,
                existing_states.get(str(source["doc_id"])),
                ingestion_fingerprint=ingestion_fingerprints["core"],
                legacy_ingestion_fingerprints=(
                    legacy_ingestion_fingerprints
                ),
            )
        }
        adopted_document_count = (
            self.postgres_repo.stamp_knowledge_document_fingerprints(
                adopted_doc_ids,
                ingestion_fingerprints["core"],
            )
            if adopted_doc_ids
            else 0
        )
        summary_ingestion_fingerprint = (
            ingestion_fingerprints.get(str(plan["dataset"]))
            or ingestion_fingerprints.get("core")
            or next(iter(ingestion_fingerprints.values()))
        )
        relocations = [
            {
                "doc_id": str(source["doc_id"]),
                "source_path": str(source["path"]),
            }
            for source in sources
            if str(source["source_id"]) in current_source_ids
            and self._source_path_changed(
                source,
                existing_states.get(str(source["doc_id"])),
            )
        ]

        chunks_by_scope: dict[str, list[RetrievedChunk]] = {}
        documents: list[dict[str, Any]] = []
        for group in source_groups:
            root = group["root"]
            scope_override = group["scope_override"]
            source_prefix = group["source_prefix"]
            group_sources = group["sources"]
            selected_source_ids = changed_source_ids.intersection(
                str(source["source_id"]) for source in group_sources
            )
            if not selected_source_ids:
                continue
            group_chunks, group_documents = self._build_chunks(
                root,
                scope_override=scope_override,
                source_prefix=source_prefix,
                include_source_ids=selected_source_ids,
            )
            for chunks in group_chunks.values():
                for chunk in chunks:
                    chunk.metadata["dataset"] = group["dataset"]
            for document in group_documents:
                document["dataset"] = group["dataset"]
            for scope, chunks in group_chunks.items():
                chunks_by_scope.setdefault(scope, []).extend(chunks)
            documents.extend(group_documents)

        changed_doc_ids = {
            str(source["doc_id"]) for source in changed_sources
        }
        built_doc_ids = {str(document["doc_id"]) for document in documents}
        if built_doc_ids != changed_doc_ids:
            missing = sorted(changed_doc_ids - built_doc_ids)
            raise RuntimeError(
                f"增量知识库解析未生成全部文档: {missing}"
            )

        prepared_by_scope = {
            scope: self._embed_chunks(chunks)
            for scope, chunks in sorted(chunks_by_scope.items())
        }
        retrieval_units_by_scope = self._retrieval_units_by_scope(documents)
        prepared_units_by_scope = {
            scope: self._embed_retrieval_units(units)
            for scope, units in sorted(retrieval_units_by_scope.items())
        }

        active_doc_ids = {str(source["doc_id"]) for source in sources}
        active_collection_names = {
            str(source["collection_name"]) for source in sources
        }
        managed_existing_doc_ids = {
            doc_id
            for doc_id, state in existing_states.items()
            if plan["dataset"] == "all"
            or self._state_dataset(state) in selected_datasets
        }
        stale_doc_ids = managed_existing_doc_ids - active_doc_ids
        changed = bool(changed_sources or stale_doc_ids)
        relocated_document_count = 0
        vector_count = 0
        document_prune = {"pruned_chunks": 0, "pruned_documents": 0}
        collection_prune = {
            "pruned_collections": [],
            "pruned_chunks": 0,
            "pruned_documents": 0,
            "dropped_hnsw_indexes": [],
        }

        if changed:
            with self.postgres_repo.connection() as conn:
                profile_id, build_id = (
                    self.postgres_repo.begin_embedding_profile_build(
                        conn,
                        model_name=self.settings.effective_embedding_model,
                        dimensions=resolve_embedding_dimensions(self.settings),
                    )
                )
                self.postgres_repo.copy_active_embedding_rows(
                    conn,
                    profile_id=profile_id,
                    build_id=build_id,
                    excluded_doc_ids=changed_doc_ids | stale_doc_ids,
                )
                copy_unit_embeddings = getattr(
                    self.postgres_repo,
                    "copy_active_retrieval_unit_embedding_rows",
                    None,
                )
                if callable(copy_unit_embeddings):
                    copy_unit_embeddings(
                        conn,
                        profile_id=profile_id,
                        build_id=build_id,
                        excluded_doc_ids=changed_doc_ids | stale_doc_ids,
                    )
                for scope, chunks in sorted(chunks_by_scope.items()):
                    collection_name = (
                        self.settings.collection_name_for_scope(scope)
                    )
                    store = PGVectorClient(
                        embedding_service=self.embedding_service,
                        collection_name=collection_name,
                        repository=self.postgres_repo,
                    )
                    scope_documents = [
                        document
                        for document in documents
                        if document["scope"] == scope
                    ]
                    for document in scope_documents:
                        conn.execute(
                            "DELETE FROM kb_chunks WHERE doc_id = %s",
                            (str(document["doc_id"]),),
                        )
                        store.upsert_document(document, connection=conn)
                    prepared_chunks, embeddings = prepared_by_scope[scope]
                    vector_count += store.upsert_chunks(
                        prepared_chunks,
                        embeddings,
                        connection=conn,
                        embedding_profile_id=profile_id,
                        embedding_build_id=build_id,
                    )
                    prepared_units, unit_embeddings = (
                        prepared_units_by_scope.get(scope, ([], []))
                    )
                    replace_units = getattr(
                        self.postgres_repo,
                        "replace_retrieval_units",
                        None,
                    )
                    if callable(replace_units):
                        replace_units(
                            conn,
                            retrieval_units=prepared_units,
                            refresh_document_frequencies=False,
                        )
                    upsert_unit_embeddings = getattr(
                        self.postgres_repo,
                        "upsert_retrieval_unit_embedding_rows",
                        None,
                    )
                    if prepared_units and callable(upsert_unit_embeddings):
                        upsert_unit_embeddings(
                            conn,
                            profile_id=profile_id,
                            build_id=build_id,
                            retrieval_units=prepared_units,
                            embeddings=unit_embeddings,
                        )
                    for document in scope_documents:
                        self.postgres_repo.replace_knowledge_structure(
                            conn,
                            document=document,
                            chunks=prepared_chunks,
                        )

                if stale_doc_ids:
                    stale_cursor = conn.execute(
                        "DELETE FROM kb_chunks "
                        "WHERE doc_id = ANY(%s::text[])",
                        (sorted(stale_doc_ids),),
                    )
                    document_prune["pruned_chunks"] = max(
                        0, int(stale_cursor.rowcount or 0)
                    )
                    if plan["dataset"] == "all":
                        stale_collection_rows = conn.execute(
                            """
                            SELECT DISTINCT collection_name
                            FROM kb_chunks
                            WHERE index_version = %s
                              AND collection_name <> ALL(%s::text[])
                            ORDER BY collection_name
                            """,
                            (
                                self.settings.kb_index_version,
                                sorted(active_collection_names),
                            ),
                        ).fetchall()
                        collection_prune["pruned_collections"] = [
                            str(row["collection_name"])
                            for row in stale_collection_rows
                        ]
                        collection_cursor = conn.execute(
                            "DELETE FROM kb_chunks "
                            "WHERE index_version = %s "
                            "AND collection_name <> ALL(%s::text[])",
                            (
                                self.settings.kb_index_version,
                                sorted(active_collection_names),
                            ),
                        )
                        collection_prune["pruned_chunks"] = max(
                            0, int(collection_cursor.rowcount or 0)
                        )
                        documents_cursor = conn.execute(
                            """
                            DELETE FROM kb_documents d
                            WHERE NOT EXISTS (
                                SELECT 1
                                FROM kb_chunks c
                                WHERE c.doc_id = d.doc_id
                            )
                            """
                        )
                    else:
                        documents_cursor = conn.execute(
                            """
                            DELETE FROM kb_documents d
                            WHERE d.doc_id = ANY(%s::text[])
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM kb_chunks c
                                  WHERE c.doc_id = d.doc_id
                              )
                            """,
                            (sorted(stale_doc_ids),),
                        )
                    document_prune["pruned_documents"] = max(
                        0, int(documents_cursor.rowcount or 0)
                    )

                if relocations:
                    relocated_document_count = (
                        self.postgres_repo.relocate_knowledge_document_sources(
                            relocations,
                            connection=conn,
                        )
                    )

                refresh_exact_frequencies = getattr(
                    self.postgres_repo,
                    "refresh_retrieval_unit_exact_fact_document_frequencies",
                    None,
                )
                if callable(refresh_exact_frequencies):
                    refresh_exact_frequencies(conn)
                embedding_profile = (
                    self.postgres_repo.activate_embedding_profile_build(
                        conn,
                        profile_id=profile_id,
                        build_id=build_id,
                    )
                )
        else:
            embedding_profile = (
                self.postgres_repo.require_active_embedding_profile(
                    self.settings.effective_embedding_model,
                    resolve_embedding_dimensions(self.settings),
                )
            )
            if relocations:
                relocated_document_count = (
                    self.postgres_repo.relocate_knowledge_document_sources(
                        relocations
                    )
                )

        missing_bm25 = self.postgres_repo.count_missing_bm25_embeddings()
        retrieval_indexes = self.postgres_repo.maintain_retrieval_indexes(
            rebuild_lexical=bool(stale_doc_ids or missing_bm25),
            drop_legacy_hnsw=True,
        )
        from backend.services.retrieval_service import RetrievalService

        RetrievalService.clear_result_cache()

        built_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return {
            "mode": "incremental",
            "dataset": plan["dataset"],
            "datasets": list(plan["datasets"]),
            "source_roots": {
                name: str(path)
                for name, path in plan["source_roots"].items()
            },
            "raw_dir": str(summary_raw_root),
            "resource_dir": None,
            "source_count": len(sources),
            "changed_documents": sorted(changed_source_ids),
            "changed_document_count": len(changed_sources),
            "adopted_document_count": adopted_document_count,
            "relocated_document_count": relocated_document_count,
            "unchanged_document_count": len(sources) - len(changed_sources),
            "removed_documents": sorted(stale_doc_ids),
            "removed_document_count": len(stale_doc_ids),
            "chunk_count": sum(len(chunks) for chunks in chunks_by_scope.values()),
            "vector_count": vector_count,
            "active_scopes": sorted(
                {str(source["scope"]) for source in sources}
            ),
            "pruned_collections": collection_prune["pruned_collections"],
            "pruned_chunks": (
                document_prune["pruned_chunks"]
                + collection_prune["pruned_chunks"]
            ),
            "pruned_documents": (
                document_prune["pruned_documents"]
                + collection_prune["pruned_documents"]
            ),
            "dropped_hnsw_indexes": collection_prune[
                "dropped_hnsw_indexes"
            ],
            "retrieval_indexes": retrieval_indexes,
            "embedding_profile": embedding_profile.as_dict(),
            "embedding_provider": self.settings.effective_embedding_provider,
            "embedding_model": self.settings.effective_embedding_model,
            "embedding_dimensions": resolve_embedding_dimensions(self.settings),
            "index_version": self.settings.kb_index_version,
            "ingestion_fingerprint": summary_ingestion_fingerprint,
            "ingestion_fingerprints": dict(ingestion_fingerprints),
            "image_analysis": dict(self._image_analysis_summary),
            "warnings": list(self._image_analysis_warnings),
            "errors": [],
            "built_at": built_at,
        }

    def rebuild(
        self,
        raw_dir: Path | None = None,
        *,
        dataset: str = "all",
        strict_images: bool = False,
        exact: bool = False,
    ) -> list[dict]:
        source_plan = self._source_plan(raw_dir=raw_dir, dataset=dataset)
        summary_raw_root = source_plan["source_roots"][
            source_plan["datasets"][0]
        ]
        if strict_images:
            raise ValueError(
                "--strict-images is unavailable for the processed-data-only "
                "knowledge pipeline. Index validated Markdown/XLSX instead."
            )
        started_at = time.time()
        self._reset_image_analysis_summary()
        chunks_by_scope: dict[str, list[RetrievedChunk]] = {}
        documents: list[dict[str, Any]] = []
        for group in source_plan["groups"]:
            group_chunks, group_documents = self._build_chunks(
                group["root"],
                scope_override=group["scope_override"],
                source_prefix=group["source_prefix"],
            )
            for scope, chunks in group_chunks.items():
                for chunk in chunks:
                    chunk.metadata["dataset"] = group["dataset"]
                chunks_by_scope.setdefault(scope, []).extend(chunks)
            for document in group_documents:
                document["dataset"] = group["dataset"]
            documents.extend(group_documents)
        if not chunks_by_scope:
            raise RuntimeError(
                f"没有可索引的 KB 文件: {summary_raw_root}"
            )

        prepared_by_scope = {
            scope: self._embed_chunks(chunks)
            for scope, chunks in sorted(chunks_by_scope.items())
        }
        retrieval_units_by_scope = self._retrieval_units_by_scope(documents)
        prepared_units_by_scope = {
            scope: self._embed_retrieval_units(units)
            for scope, units in sorted(retrieval_units_by_scope.items())
        }
        active_scopes = sorted(chunks_by_scope)
        active_collection_names = {
            self.settings.collection_name_for_scope(scope)
            for scope in active_scopes
        }

        vector_count = 0
        collections: dict[str, dict[str, Any]] = {}
        prune_summary = {
            "pruned_collections": [],
            "pruned_chunks": 0,
            "pruned_documents": 0,
            "dropped_hnsw_indexes": [],
        }
        rebuilt_doc_ids = {
            str(document["doc_id"]) for document in documents
        }
        stale_doc_ids: set[str] = set()
        if exact:
            existing_states = self.postgres_repo.knowledge_document_states(
                index_version=self.settings.kb_index_version,
            )
            managed_existing_doc_ids = {
                doc_id
                for doc_id, state in existing_states.items()
                if source_plan["dataset"] == "all"
                or self._state_dataset(state)
                in set(source_plan["datasets"])
            }
            stale_doc_ids = managed_existing_doc_ids - rebuilt_doc_ids

        with self.postgres_repo.connection() as conn:
            profile_id, build_id = (
                self.postgres_repo.begin_embedding_profile_build(
                    conn,
                    model_name=self.settings.effective_embedding_model,
                    dimensions=resolve_embedding_dimensions(self.settings),
                )
            )
            self.postgres_repo.copy_active_embedding_rows(
                conn,
                profile_id=profile_id,
                build_id=build_id,
                excluded_doc_ids=rebuilt_doc_ids | stale_doc_ids,
            )
            copy_unit_embeddings = getattr(
                self.postgres_repo,
                "copy_active_retrieval_unit_embedding_rows",
                None,
            )
            if callable(copy_unit_embeddings):
                copy_unit_embeddings(
                    conn,
                    profile_id=profile_id,
                    build_id=build_id,
                    excluded_doc_ids=rebuilt_doc_ids | stale_doc_ids,
                )
            if exact and source_plan["dataset"] == "all":
                stale_rows = conn.execute(
                    """
                    SELECT DISTINCT collection_name
                    FROM kb_chunks
                    WHERE index_version = %s
                      AND collection_name <> ALL(%s::text[])
                    ORDER BY collection_name
                    """,
                    (
                        self.settings.kb_index_version,
                        sorted(active_collection_names),
                    ),
                ).fetchall()
                prune_summary["pruned_collections"] = [
                    str(row["collection_name"]) for row in stale_rows
                ]

            if exact and stale_doc_ids:
                stale_cursor = conn.execute(
                    "DELETE FROM kb_chunks "
                    "WHERE doc_id = ANY(%s::text[])",
                    (sorted(stale_doc_ids),),
                )
                prune_summary["pruned_chunks"] += max(
                    0, int(stale_cursor.rowcount or 0)
                )

            if source_plan["dataset"] == "all":
                conn.execute(
                    "DELETE FROM kb_chunks "
                    "WHERE index_version = %s "
                    "AND collection_name = ANY(%s::text[])",
                    (
                        self.settings.kb_index_version,
                        sorted(active_collection_names),
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM kb_chunks "
                    "WHERE doc_id = ANY(%s::text[])",
                    (sorted(rebuilt_doc_ids),),
                )
            for scope, chunks in sorted(chunks_by_scope.items()):
                collection_name = (
                    self.settings.collection_name_for_scope(scope)
                )
                store = PGVectorClient(
                    embedding_service=self.embedding_service,
                    collection_name=collection_name,
                    repository=self.postgres_repo,
                )
                for document in documents:
                    if document["scope"] == scope:
                        store.upsert_document(document, connection=conn)
                prepared_chunks, embeddings = prepared_by_scope[scope]
                count = store.upsert_chunks(
                    prepared_chunks,
                    embeddings,
                    connection=conn,
                    embedding_profile_id=profile_id,
                    embedding_build_id=build_id,
                )
                vector_count += count
                prepared_units, unit_embeddings = (
                    prepared_units_by_scope.get(scope, ([], []))
                )
                replace_units = getattr(
                    self.postgres_repo,
                    "replace_retrieval_units",
                    None,
                )
                if callable(replace_units):
                    replace_units(
                        conn,
                        retrieval_units=prepared_units,
                        refresh_document_frequencies=False,
                    )
                upsert_unit_embeddings = getattr(
                    self.postgres_repo,
                    "upsert_retrieval_unit_embedding_rows",
                    None,
                )
                if prepared_units and callable(upsert_unit_embeddings):
                    upsert_unit_embeddings(
                        conn,
                        profile_id=profile_id,
                        build_id=build_id,
                        retrieval_units=prepared_units,
                        embeddings=unit_embeddings,
                    )
                for document in documents:
                    if document["scope"] == scope:
                        self.postgres_repo.replace_knowledge_structure(
                            conn,
                            document=document,
                            chunks=prepared_chunks,
                        )
                collections[scope] = {
                    "collection_name": collection_name,
                    "chunk_count": len(chunks),
                    "vector_count": count,
                }

            if exact and source_plan["dataset"] == "all":
                cursor = conn.execute(
                    "DELETE FROM kb_chunks "
                    "WHERE index_version = %s "
                    "AND collection_name <> ALL(%s::text[])",
                    (
                        self.settings.kb_index_version,
                        sorted(active_collection_names),
                    ),
                )
                prune_summary["pruned_chunks"] += max(
                    0, int(cursor.rowcount or 0)
                )
            if exact and source_plan["dataset"] != "all":
                documents_cursor = conn.execute(
                    """
                    DELETE FROM kb_documents d
                    WHERE d.doc_id = ANY(%s::text[])
                      AND NOT EXISTS (
                          SELECT 1
                          FROM kb_chunks c
                          WHERE c.doc_id = d.doc_id
                      )
                    """,
                    (sorted(stale_doc_ids),),
                )
                prune_summary["pruned_documents"] = max(
                    0, int(documents_cursor.rowcount or 0)
                )
            elif source_plan["dataset"] == "all":
                documents_cursor = conn.execute(
                    """
                    DELETE FROM kb_documents d
                    WHERE NOT EXISTS (
                        SELECT 1 FROM kb_chunks c WHERE c.doc_id = d.doc_id
                    )
                    """
                )
                prune_summary["pruned_documents"] = max(
                    0, int(documents_cursor.rowcount or 0)
                )
            refresh_exact_frequencies = getattr(
                self.postgres_repo,
                "refresh_retrieval_unit_exact_fact_document_frequencies",
                None,
            )
            if callable(refresh_exact_frequencies):
                refresh_exact_frequencies(conn)
            embedding_profile = (
                self.postgres_repo.activate_embedding_profile_build(
                    conn,
                    profile_id=profile_id,
                    build_id=build_id,
                )
            )

        retrieval_indexes = self.postgres_repo.maintain_retrieval_indexes(
            rebuild_lexical=True,
            drop_legacy_hnsw=True,
        )
        built_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        elapsed = round(time.time() - started_at, 3)
        summary = {
            "dataset": source_plan["dataset"],
            "datasets": list(source_plan["datasets"]),
            "source_roots": {
                name: str(path)
                for name, path in source_plan["source_roots"].items()
            },
            "source_count": len(source_plan["sources"]),
            "relocated_document_count": 0,
            "raw_dir": str(summary_raw_root),
            "resource_dir": None,
            "chunk_count": sum(len(v) for v in chunks_by_scope.values()),
            "vector_count": vector_count,
            "exact": exact,
            "active_scopes": active_scopes,
            "collections": collections,
            **prune_summary,
            "database_url_configured": bool(self.settings.database_url),
            "vectorstore_provider": self.settings.vectorstore_provider,
            "embedding_profile": embedding_profile.as_dict(),
            "embedding_provider": self.settings.effective_embedding_provider,
            "embedding_model": self.settings.effective_embedding_model,
            "embedding_dimensions": resolve_embedding_dimensions(self.settings),
            "index_version": self.settings.kb_index_version,
            "retrieval_indexes": retrieval_indexes,
            "image_analysis": dict(self._image_analysis_summary),
            "warnings": list(self._image_analysis_warnings),
            "errors": [],
            "elapsed_seconds": elapsed,
            "built_at": built_at,
        }
        self.last_summary = summary
        self.manifest_repo.save({
            "documents": documents,
            "dataset": source_plan["dataset"],
            "datasets": list(source_plan["datasets"]),
            "source_roots": {
                name: str(path)
                for name, path in source_plan["source_roots"].items()
            },
            "relocated_document_count": 0,
            "exact": exact,
            "active_scopes": active_scopes,
            "collections": collections,
            "embedding_profile": embedding_profile.as_dict(),
            "embedding_provider": self.settings.effective_embedding_provider,
            "embedding_model": self.settings.effective_embedding_model,
            "embedding_dimensions": resolve_embedding_dimensions(self.settings),
            **prune_summary,
            "image_analysis": dict(self._image_analysis_summary),
            "warnings": list(self._image_analysis_warnings),
            "errors": [],
            "built_at": built_at,
        })
        self.vector_repo.save(summary)
        from backend.services.retrieval_service import RetrievalService

        RetrievalService.clear_result_cache()
        return [chunk.model_dump() for chunks in chunks_by_scope.values() for chunk in chunks]

    def build_embedding_profile(
        self,
        *,
        model_name: str,
        dimensions: int,
        provider_mode: str | None = None,
    ) -> dict[str, Any]:
        normalized_model = normalize_embedding_model_name(model_name)
        normalized_dimensions = normalize_embedding_dimensions(dimensions)
        source_corpus = embedding_profile_corpus(
            normalized_model,
            normalized_dimensions,
        )
        resolved_mode = str(
            provider_mode or self.settings.model_provider_mode
        ).strip()
        if resolved_mode not in {"local", "platform"}:
            raise ValueError("provider_mode must be 'local' or 'platform'.")

        updates: dict[str, Any] = {
            "model_provider_mode": resolved_mode,
        }
        if resolved_mode == "local":
            updates["embedding_local_model"] = normalized_model
            updates["embedding_local_dimensions"] = normalized_dimensions
        else:
            updates["embedding_model"] = normalized_model
            updates["embedding_dimensions"] = normalized_dimensions
        profile_settings = self.settings.model_copy(update=updates)
        embedding_service = EmbeddingService(settings=profile_settings)

        source_rows = self.postgres_repo.fetch_embedding_source_chunks(
            model_name=normalized_model,
            dimensions=normalized_dimensions,
        )
        if not source_rows:
            raise RuntimeError(
                "Cannot build an embedding profile for an empty knowledge base."
            )

        fetch_reusable_ids = getattr(
            self.postgres_repo,
            "fetch_active_embedding_source_ids",
            None,
        )
        reusable_chunk_ids: set[str] = set()
        reusable_unit_ids: set[str] = set()
        if callable(fetch_reusable_ids):
            reusable_chunk_ids, reusable_unit_ids = fetch_reusable_ids(
                model_name=normalized_model,
                dimensions=normalized_dimensions,
            )
        source_chunk_ids = {str(row["chunk_id"]) for row in source_rows}
        reusable_chunk_ids.intersection_update(source_chunk_ids)
        rows_to_embed = [
            row
            for row in source_rows
            if str(row["chunk_id"]) not in reusable_chunk_ids
        ]
        chunks = [
            RetrievedChunk(
                chunk_id=row["chunk_id"],
                source_id=row["source_id"],
                title=row["title"],
                scope=row["scope"],
                text=row["text"],
                metadata={
                    **row["metadata"],
                    "doc_id": row["doc_id"],
                    "collection_name": row["collection_name"],
                    "index_version": row["index_version"],
                    "content_hash": row["content_hash"],
                    "search_text": row.get("search_text") or row["text"],
                },
            )
            for row in rows_to_embed
        ]

        embeddings: list[list[float]] = []
        batch_size = max(1, int(profile_settings.kb_ingest_batch_size))
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            batch_embeddings = embedding_service.embed(
                [
                    str(chunk.metadata.get("search_text") or chunk.text)
                    for chunk in batch
                ]
            )
            if len(batch_embeddings) != len(batch):
                raise RuntimeError(
                    "Embedding API returned an unexpected vector count."
                )
            for vector in batch_embeddings:
                if len(vector) != normalized_dimensions:
                    raise RuntimeError(
                        "Embedding API returned an unexpected vector "
                        f"dimension: expected={normalized_dimensions}, "
                        f"actual={len(vector)}."
                    )
            embeddings.extend(batch_embeddings)

        fetch_retrieval_units = getattr(
            self.postgres_repo,
            "fetch_embedding_source_retrieval_units",
            None,
        )
        retrieval_units = (
            list(
                fetch_retrieval_units(
                    model_name=normalized_model,
                    dimensions=normalized_dimensions,
                )
            )
            if callable(fetch_retrieval_units)
            else []
        )
        retrieval_unit_ids = {
            str(unit["unit_id"]) for unit in retrieval_units
        }
        reusable_unit_ids.intersection_update(retrieval_unit_ids)
        retrieval_units_to_embed = [
            unit
            for unit in retrieval_units
            if str(unit["unit_id"]) not in reusable_unit_ids
        ]
        embedded_retrieval_units, retrieval_unit_embeddings = (
            self._embed_retrieval_units(
                retrieval_units_to_embed,
                embedding_service=embedding_service,
                expected_dimensions=normalized_dimensions,
            )
        )

        grouped_indexes: dict[str, list[int]] = {}
        for index, row in enumerate(rows_to_embed):
            grouped_indexes.setdefault(
                str(row["collection_name"]),
                [],
            ).append(index)

        with self.postgres_repo.connection() as conn:
            profile_id, build_id = (
                self.postgres_repo.begin_embedding_profile_build(
                    conn,
                    model_name=normalized_model,
                    dimensions=normalized_dimensions,
                )
            )
            if reusable_chunk_ids:
                copied_chunks = self.postgres_repo.copy_active_embedding_rows(
                    conn,
                    profile_id=profile_id,
                    build_id=build_id,
                )
                if copied_chunks != len(reusable_chunk_ids):
                    raise RuntimeError(
                        "Reusable embedding copy was incomplete: "
                        f"expected={len(reusable_chunk_ids)}, "
                        f"actual={copied_chunks}."
                    )
            if reusable_unit_ids:
                copy_unit_embeddings = getattr(
                    self.postgres_repo,
                    "copy_active_retrieval_unit_embedding_rows",
                    None,
                )
                if not callable(copy_unit_embeddings):
                    raise RuntimeError(
                        "Retrieval-unit embedding reuse is unavailable."
                    )
                copied_units = copy_unit_embeddings(
                    conn,
                    profile_id=profile_id,
                    build_id=build_id,
                )
                if copied_units != len(reusable_unit_ids):
                    raise RuntimeError(
                        "Reusable retrieval-unit embedding copy was "
                        "incomplete: "
                        f"expected={len(reusable_unit_ids)}, "
                        f"actual={copied_units}."
                    )
            for collection_name, indexes in sorted(
                grouped_indexes.items()
            ):
                self.postgres_repo.upsert_embedding_rows(
                    conn,
                    profile_id=profile_id,
                    build_id=build_id,
                    collection_name=collection_name,
                    chunks=[chunks[index] for index in indexes],
                    embeddings=[embeddings[index] for index in indexes],
                )
            upsert_unit_embeddings = getattr(
                self.postgres_repo,
                "upsert_retrieval_unit_embedding_rows",
                None,
            )
            if embedded_retrieval_units and callable(upsert_unit_embeddings):
                upsert_unit_embeddings(
                    conn,
                    profile_id=profile_id,
                    build_id=build_id,
                    retrieval_units=embedded_retrieval_units,
                    embeddings=retrieval_unit_embeddings,
                )
            profile = self.postgres_repo.activate_embedding_profile_build(
                conn,
                profile_id=profile_id,
                build_id=build_id,
            )

        indexes = self.postgres_repo.maintain_retrieval_indexes(
            rebuild_lexical=False,
            drop_legacy_hnsw=True,
            profile=profile,
        )
        from backend.services.retrieval_service import RetrievalService

        RetrievalService.clear_result_cache()
        return {
            "embedding_profile": profile.as_dict(),
            "provider_mode": resolved_mode,
            "source_corpus": source_corpus,
            "vector_count": len(source_rows),
            "embedded_vector_count": len(embeddings),
            "reused_vector_count": len(reusable_chunk_ids),
            "retrieval_unit_vector_count": len(retrieval_units),
            "embedded_retrieval_unit_vector_count": len(
                retrieval_unit_embeddings
            ),
            "reused_retrieval_unit_vector_count": len(reusable_unit_ids),
            "retrieval_indexes": indexes,
        }

    def document_vision_configuration(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "mode": "processed_data_only",
            "model": None,
            "prompt_version": None,
            "enable_thinking": False,
            "stream": False,
            "preflight_enabled": False,
            "fail_on_error": False,
            "kb_index_version": self.settings.kb_index_version,
        }

    def preflight_document_vision(self, raw_dir: Path | None = None) -> dict[str, Any]:
        del raw_dir
        raise RuntimeError(
            "Document vision preflight was removed from the processed-data-only "
            "knowledge pipeline."
        )

    def _validate_strict_image_configuration(self) -> None:
        config = self.document_vision_configuration()
        if not config["enabled"]:
            raise RuntimeError("Strict image rebuild requires DOCUMENT_VISION_ENABLED=true.")
        if not config["enable_thinking"] or not config["stream"]:
            raise RuntimeError(
                "Strict image rebuild requires LLM_DOCUMENT_VISION_ENABLE_THINKING=true "
                "and streamed structured output."
            )
        if not config["preflight_enabled"]:
            raise RuntimeError(
                "Strict image rebuild requires DOCUMENT_VISION_PREFLIGHT_ENABLED=true."
            )

    def _validate_strict_image_summary(self) -> None:
        stats = self._image_analysis_summary
        candidate_count = int(stats.get("candidate_count") or 0)
        preparation_failures = int(stats.get("preparation_failed_count") or 0)
        failed_count = int(stats.get("failed_count") or 0)
        completed_count = int(stats.get("analysis_count") or 0) + int(
            stats.get("cache_hit_count") or 0
        )
        description_count = int(stats.get("description_count") or 0)
        if candidate_count <= 0:
            raise RuntimeError("Strict image rebuild found no document image candidates.")
        if preparation_failures or failed_count:
            raise RuntimeError(
                "Strict image rebuild rejected incomplete image enrichment: "
                f"preparation_failed={preparation_failures}, failed={failed_count}."
            )
        if completed_count <= 0:
            raise RuntimeError("Strict image rebuild produced no validated image analyses.")
        if description_count <= 0:
            raise RuntimeError("Strict image rebuild produced no searchable image descriptions.")

    @classmethod
    def _validate_source_tree(cls, raw_root: Path) -> None:
        unsupported = [
            path
            for path in sorted(raw_root.rglob("*"))
            if path.is_file()
            and path.suffix.lower() not in cls._SUPPORTED_EXTENSIONS
        ]
        if unsupported:
            relative = ", ".join(
                str(path.relative_to(raw_root)) for path in unsupported
            )
            raise UnsupportedFileTypeError(
                "Knowledge indexing accepts only processed Markdown and the "
                "validated organization-unit workbook. Unsupported input: "
                f"{relative}"
            )

    def _build_chunks(
        self,
        raw_root: Path,
        *,
        scope_override: str | None = None,
        source_prefix: str = "",
        include_source_ids: set[str] | None = None,
    ) -> tuple[dict[str, list[RetrievedChunk]], list[dict[str, Any]]]:
        self._validate_source_tree(raw_root)
        chunks_by_scope: dict[str, list[RetrievedChunk]] = {}
        documents: list[dict[str, Any]] = []
        for path in sorted(raw_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self._SUPPORTED_EXTENSIONS:
                continue
            scope, source_id = self._source_identity(
                raw_root,
                path,
                scope_override=scope_override,
                source_prefix=source_prefix,
            )
            source_dataset = (
                "organization_unit"
                if scope == "organization_unit"
                else "core"
            )
            ingestion_fingerprint = self._ingestion_fingerprint(
                dataset=source_dataset
            )
            if (
                include_source_ids is not None
                and source_id not in include_source_ids
            ):
                continue
            doc_id = self._doc_id(source_id)
            content_hash = self._file_hash(path)
            text, parse_metadata = self.parser.parse_file(path)
            parse_metadata = dict(parse_metadata)
            collection_name = self.settings.collection_name_for_scope(scope)
            title = self._resource_title(text, path.stem) if scope_override else path.stem
            structured_records = parse_metadata.pop("structured_records", None)
            if structured_records is not None:
                structured_chunks = self._build_structured_record_chunks(
                    records=structured_records,
                    source_id=source_id,
                    source_path=path,
                    doc_id=doc_id,
                    scope=scope,
                    collection_name=collection_name,
                    content_hash=content_hash,
                    parser_name=str(parse_metadata.get("parser") or "structured_record"),
                    chunk_size=int(
                        parse_metadata.get("chunk_size")
                        or self.settings.kb_chunk_size
                    ),
                    chunk_overlap=int(
                        parse_metadata.get("chunk_overlap")
                        or self.settings.kb_chunk_overlap
                    ),
                )
                if not structured_chunks:
                    raise RuntimeError(f"结构化解析后未生成 chunk: {path}")
                chunks_by_scope.setdefault(scope, []).extend(structured_chunks)
                documents.append({
                    "doc_id": doc_id,
                    "scope": scope,
                    "source_path": str(path),
                    "relative_path": source_id,
                    "content_hash": content_hash,
                    "chunk_count": len(structured_chunks),
                    "chunk_ids": [chunk.chunk_id for chunk in structured_chunks],
                    "collection_name": collection_name,
                    "embedding_model": self.settings.effective_embedding_model,
                    "embedding_dimensions": resolve_embedding_dimensions(self.settings),
                    "index_version": self.settings.kb_index_version,
                    "ingestion_fingerprint": ingestion_fingerprint,
                    "index_status": "indexed",
                    "parse_metadata": parse_metadata,
                    "_sections": self._materialize_record_sections(
                        doc_id=doc_id,
                        source_id=source_id,
                        scope=scope,
                        records=structured_records,
                    ),
                    "_retrieval_units": [],
                })
                continue

            blocks = list(parse_metadata.pop("structured_blocks", None) or [])
            raw_sections = list(parse_metadata.pop("sections", None) or [])
            sections, section_id_map = self._materialize_sections(
                doc_id=doc_id,
                source_id=source_id,
                scope=scope,
                sections=raw_sections,
            )
            chunk_entries = chunk_blocks(
                text=text,
                blocks=blocks,
                chunk_size=self.settings.kb_chunk_size,
                overlap=self.settings.kb_chunk_overlap,
            )
            if not chunk_entries:
                raise RuntimeError(f"解析后未生成 chunk: {path}")
            doc_chunk_ids: list[str] = []
            document_retrieval_units: list[dict[str, Any]] = []
            usable_blocks = [block for block in blocks if str(block.get("text") or "").strip()]
            for entry in chunk_entries:
                idx = int(entry.get("chunk_index") or len(doc_chunk_ids))
                chunk_text = str(entry["text"])
                chunk_id = self._stable_chunk_id(doc_id=doc_id, index=idx, text=chunk_text)
                doc_chunk_ids.append(chunk_id)
                entry_blocks = [
                    usable_blocks[block_index]
                    for block_index in entry.get("block_indexes") or []
                    if isinstance(block_index, int) and 0 <= block_index < len(usable_blocks)
                ]
                image_refs: list[str] = []
                heading_path = [
                    str(value)
                    for value in (entry.get("heading_path") or [])
                    if str(value).strip()
                ]
                parent_section_id = section_id_map.get(
                    str(entry.get("parent_section_local_id") or "")
                )
                chapter_section_id = section_id_map.get(
                    str(entry.get("chapter_local_section_id") or "")
                )
                search_text = contextual_search_text(
                    scope=scope,
                    title=title,
                    heading_path=heading_path,
                    text=chunk_text,
                    table_headers=entry.get("table_headers") or [],
                )
                document_retrieval_units.extend(
                    self._materialize_retrieval_units(
                        raw_units=entry.get("retrieval_units") or [],
                        parent_chunk_id=chunk_id,
                        parent_text=chunk_text,
                        doc_id=doc_id,
                        collection_name=collection_name,
                        scope=scope,
                        title=title,
                        index_version=self.settings.kb_index_version,
                        content_hash=content_hash,
                    )
                )
                chunk = RetrievedChunk(
                    chunk_id=chunk_id,
                    source_id=source_id,
                    title=title,
                    scope=scope,
                    text=chunk_text,
                    metadata={
                        "chunk_id": chunk_id,
                        "doc_id": doc_id,
                        "source_path": str(path),
                        "relative_path": source_id,
                        "scope": scope,
                        "tag": path.stem,
                        "page": entry.get("page"),
                        "section": entry.get("section"),
                        "content_hash": content_hash,
                        "index_version": self.settings.kb_index_version,
                        "chunk_index": idx,
                        "collection_name": collection_name,
                        "parser": parse_metadata.get("parser"),
                        "search_text": search_text,
                        "canonical_content_id": canonical_content_id(chunk_text),
                        "heading_path": heading_path,
                        "parent_section_id": parent_section_id,
                        "chapter_section_id": chapter_section_id,
                        "source_start_line": entry.get("source_start_line"),
                        "source_end_line": entry.get("source_end_line"),
                        "source_start_char": entry.get("source_start_char"),
                        "source_end_char": entry.get("source_end_char"),
                        "block_types": entry.get("block_types") or [],
                        "table_headers": entry.get("table_headers") or [],
                        "atomic_structure": bool(
                            entry.get("atomic_structure")
                        ),
                        "semantic_group_id": entry.get(
                            "semantic_group_id"
                        ),
                        "semantic_group_type": entry.get(
                            "semantic_group_type"
                        ),
                        "semantic_group_title": entry.get(
                            "semantic_group_title"
                        ),
                        "semantic_group_item_count": entry.get(
                            "semantic_group_item_count"
                        ),
                        "semantic_item_count": entry.get(
                            "semantic_item_count"
                        ),
                        "semantic_split": bool(
                            entry.get("semantic_split")
                        ),
                        "oversized_atomic": bool(
                            entry.get("oversized_atomic")
                        ),
                        "exact_facts": extract_exact_facts(
                            chunk_text,
                            metadata={"scope": scope},
                        ),
                        "contains_image_description": bool(image_refs),
                        "image_refs": image_refs,
                        "document_vision_model": self.settings.document_vision_model if image_refs else None,
                    },
                )
                chunks_by_scope.setdefault(scope, []).append(chunk)
            documents.append({
                "doc_id": doc_id,
                "scope": scope,
                "source_path": str(path),
                "relative_path": source_id,
                "content_hash": content_hash,
                "chunk_count": len(doc_chunk_ids),
                "chunk_ids": doc_chunk_ids,
                "collection_name": collection_name,
                "embedding_model": self.settings.effective_embedding_model,
                "embedding_dimensions": resolve_embedding_dimensions(self.settings),
                "index_version": self.settings.kb_index_version,
                "ingestion_fingerprint": ingestion_fingerprint,
                "index_status": "indexed",
                "parse_metadata": parse_metadata,
                "_sections": sections,
                "_retrieval_units": document_retrieval_units,
            })
        return chunks_by_scope, documents

    @staticmethod
    def _retrieval_unit_parent_span(
        *,
        parent_text: str,
        unit_text: str,
        cursor: int,
    ) -> tuple[int | None, int | None]:
        start = parent_text.find(unit_text, max(0, cursor))
        matched_text = unit_text
        if start < 0 and unit_text.startswith("### "):
            _, separator, body = unit_text.partition("\n\n")
            if separator and body:
                matched_text = body
                start = parent_text.find(body, max(0, cursor))
        if start < 0:
            start = parent_text.find(matched_text)
        if start < 0:
            return None, None
        return start, start + len(matched_text)

    @classmethod
    def _materialize_retrieval_units(
        cls,
        *,
        raw_units: list[dict[str, Any]],
        parent_chunk_id: str,
        parent_text: str = "",
        doc_id: str,
        collection_name: str,
        scope: str,
        title: str,
        index_version: str,
        content_hash: str,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        parent_cursor = 0
        for raw_unit in raw_units:
            unit_text = str(raw_unit.get("text") or "").strip()
            if not unit_text:
                continue
            unit_type = str(raw_unit.get("unit_type") or "").strip()
            unit_index = int(raw_unit.get("unit_index") or 0)
            digest = hashlib.sha256(
                "\x00".join(
                    (
                        parent_chunk_id,
                        unit_type,
                        str(unit_index),
                        unit_text,
                    )
                ).encode("utf-8")
            ).hexdigest()
            heading_path = [
                str(value)
                for value in (raw_unit.get("heading_path") or [])
                if str(value).strip()
            ]
            table_headers = [
                str(value)
                for value in (raw_unit.get("table_headers") or [])
                if str(value).strip()
            ]
            parent_text_start, parent_text_end = (
                cls._retrieval_unit_parent_span(
                    parent_text=parent_text,
                    unit_text=unit_text,
                    cursor=parent_cursor,
                )
                if parent_text
                else (None, None)
            )
            if parent_text_end is not None:
                parent_cursor = parent_text_end
            output.append(
                {
                    "unit_id": f"ru_{digest}",
                    "parent_chunk_id": parent_chunk_id,
                    "doc_id": doc_id,
                    "collection_name": collection_name,
                    "scope": scope,
                    "unit_type": unit_type,
                    "unit_index": unit_index,
                    "text": unit_text,
                    "search_text": contextual_search_text(
                        scope=scope,
                        title=title,
                        heading_path=heading_path,
                        text=unit_text,
                        table_headers=table_headers,
                    ),
                    "exact_facts": extract_exact_facts(
                        contextual_exact_fact_text(
                            unit_text,
                            metadata={"heading_path": heading_path},
                        ),
                        metadata={
                            "scope": scope,
                            "heading_path": heading_path,
                        },
                    ),
                    "index_version": str(index_version),
                    "content_hash": content_hash,
                    "metadata": {
                        "semantic_group_id": raw_unit.get(
                            "semantic_group_id"
                        ),
                        "semantic_group_title": raw_unit.get(
                            "semantic_group_title"
                        ),
                        "semantic_item_id": raw_unit.get(
                            "semantic_item_id"
                        ),
                        "parent_text_start": parent_text_start,
                        "parent_text_end": parent_text_end,
                        "parent_text_locator_version": "exact_v1",
                        "heading_path": heading_path,
                        "table_headers": table_headers,
                        "source_start_line": raw_unit.get(
                            "source_start_line"
                        ),
                        "source_end_line": raw_unit.get("source_end_line"),
                        "source_start_char": raw_unit.get(
                            "source_start_char"
                        ),
                        "source_end_char": raw_unit.get("source_end_char"),
                    },
                }
            )
        for index, unit in enumerate(output):
            metadata = dict(unit.get("metadata") or {})
            metadata["previous_unit_id"] = (
                output[index - 1]["unit_id"] if index > 0 else None
            )
            metadata["previous_unit_text"] = (
                output[index - 1]["text"] if index > 0 else None
            )
            metadata["next_unit_id"] = (
                output[index + 1]["unit_id"]
                if index + 1 < len(output)
                else None
            )
            metadata["next_unit_text"] = (
                output[index + 1]["text"]
                if index + 1 < len(output)
                else None
            )
            unit["metadata"] = metadata
        return output

    @staticmethod
    def _retrieval_units_by_scope(
        documents: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = {}
        for document in documents:
            scope = str(document.get("scope") or "")
            units = list(document.get("_retrieval_units") or [])
            if scope and units:
                output.setdefault(scope, []).extend(units)
        return output

    def _discover_sources(
        self,
        raw_root: Path,
        *,
        dataset: str | None = None,
        scope_override: str | None = None,
        source_prefix: str = "",
    ) -> list[dict[str, Any]]:
        self._validate_source_tree(raw_root)
        sources: list[dict[str, Any]] = []
        for path in sorted(raw_root.rglob("*")):
            if (
                not path.is_file()
                or path.suffix.lower() not in self._SUPPORTED_EXTENSIONS
            ):
                continue
            scope, source_id = self._source_identity(
                raw_root,
                path,
                scope_override=scope_override,
                source_prefix=source_prefix,
            )
            sources.append(
                {
                    "path": path,
                    "dataset": dataset or (
                        "organization_unit"
                        if scope == "organization_unit"
                        else "core"
                    ),
                    "scope": scope,
                    "source_id": source_id,
                    "doc_id": self._doc_id(source_id),
                    "relative_path": source_id,
                    "content_hash": self._file_hash(path),
                    "collection_name": (
                        self.settings.collection_name_for_scope(scope)
                    ),
                    "embedding_dimensions": int(
                        resolve_embedding_dimensions(self.settings)
                    ),
                    "embedding_model": str(
                        self.settings.effective_embedding_model
                    ),
                    "index_version": str(self.settings.kb_index_version),
                }
            )
        return sources

    def _ingestion_fingerprint(self, *, dataset: str = "core") -> str:
        return self._serialize_ingestion_fingerprint(
            fingerprint_version="kb-content-v4",
            dataset=dataset,
        )

    def _legacy_ingestion_fingerprints(
        self,
        existing_states: dict[str, dict[str, Any]],
    ) -> frozenset[str]:
        model_dimensions: set[tuple[str, int]] = set()
        for state in existing_states.values():
            metadata = state.get("metadata")
            if not isinstance(metadata, dict):
                continue
            model_name = str(
                metadata.get("embedding_model") or ""
            ).strip()
            try:
                dimensions = int(
                    metadata.get("embedding_dimensions") or 0
                )
            except (TypeError, ValueError):
                continue
            if model_name and dimensions > 0:
                model_dimensions.add((model_name, dimensions))
        model_dimensions.add(
            (
                str(self.settings.effective_embedding_model),
                int(resolve_embedding_dimensions(self.settings)),
            )
        )
        providers = {
            "bosch",
            "local_qwen",
            "openai_compatible",
            str(self.settings.effective_embedding_provider),
        }
        return frozenset(
            self._serialize_ingestion_fingerprint(
                fingerprint_version="kb-ingestion-v1",
                embedding={
                    "provider": provider,
                    "model": model_name,
                    "dimensions": dimensions,
                },
            )
            for model_name, dimensions in model_dimensions
            for provider in providers
        )

    def _serialize_ingestion_fingerprint(
        self,
        *,
        dataset: str = "core",
        fingerprint_version: str,
        embedding: dict[str, Any] | None = None,
    ) -> str:
        settings = self.settings
        organization_dataset = dataset == "organization_unit"
        payload = {
            "fingerprint_version": fingerprint_version,
            "chunking": {
                "index_version": str(
                    getattr(settings, "kb_index_version", "")
                ),
                "chunk_size": (
                    OrganizationUnitXlsxParser.CHUNK_SIZE
                    if organization_dataset
                    else int(getattr(settings, "kb_chunk_size", 0) or 0)
                ),
                "chunk_overlap": (
                    OrganizationUnitXlsxParser.CHUNK_OVERLAP
                    if organization_dataset
                    else int(getattr(settings, "kb_chunk_overlap", 0) or 0)
                ),
            },
            "parser": {
                "mode": "processed_data_only",
                "markdown": "structured-markdown-v4",
                "organization_unit": (
                    {
                        "parser": OrganizationUnitXlsxParser.CONTRACT_VERSION,
                        "chunker": ORGANIZATION_CHUNKING_VERSION,
                        "chunk_size": OrganizationUnitXlsxParser.CHUNK_SIZE,
                        "chunk_overlap": OrganizationUnitXlsxParser.CHUNK_OVERLAP,
                    }
                    if organization_dataset
                    else "organization-unit-xlsx"
                ),
                "retrieval_units": "semantic-items-v2-unit-locators",
            },
        }
        if embedding is not None:
            payload["embedding"] = embedding
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _source_is_current(
        source: dict[str, Any],
        state: dict[str, Any] | None,
        *,
        ingestion_fingerprint: str,
        legacy_ingestion_fingerprints: frozenset[str] = frozenset(),
    ) -> bool:
        if not state:
            return False
        metadata = state.get("metadata")
        if not isinstance(metadata, dict):
            return False
        try:
            metadata_chunk_count = int(metadata.get("chunk_count") or 0)
            actual_chunk_count = int(state.get("chunk_count") or 0)
        except (TypeError, ValueError):
            return False
        expected_collection = str(source["collection_name"])
        stored_fingerprint = str(
            metadata.get("ingestion_fingerprint") or ""
        )
        fingerprint_matches = (
            (
                stored_fingerprint == ingestion_fingerprint
                or stored_fingerprint in legacy_ingestion_fingerprints
            )
            if stored_fingerprint
            else str(metadata.get("index_version") or "")
            == str(source["index_version"])
        )
        return fingerprint_matches and all(
            (
                str(state.get("scope") or "") == str(source["scope"]),
                str(state.get("relative_path") or "")
                == str(source["relative_path"]),
                str(state.get("content_hash") or "")
                == str(source["content_hash"]),
                actual_chunk_count > 0,
                actual_chunk_count == metadata_chunk_count,
                str(state.get("min_collection_name") or "")
                == expected_collection,
                str(state.get("max_collection_name") or "")
                == expected_collection,
            )
        )

    @classmethod
    def _is_legacy_current_source(
        cls,
        source: dict[str, Any],
        state: dict[str, Any] | None,
        *,
        ingestion_fingerprint: str,
        legacy_ingestion_fingerprints: frozenset[str] = frozenset(),
    ) -> bool:
        metadata = state.get("metadata") if state else None
        return (
            isinstance(metadata, dict)
            and str(metadata.get("ingestion_fingerprint") or "")
            != ingestion_fingerprint
            and cls._source_is_current(
                source,
                state,
                ingestion_fingerprint=ingestion_fingerprint,
                legacy_ingestion_fingerprints=(
                    legacy_ingestion_fingerprints
                ),
            )
        )

    def _source_identity(
        self,
        raw_root: Path,
        path: Path,
        *,
        scope_override: str | None,
        source_prefix: str,
    ) -> tuple[str, str]:
        scope = scope_override or self._scope_from_path(raw_root, path)
        relative_source_id = str(path.relative_to(raw_root)).replace(
            "\\", "/"
        )
        source_id = "/".join(
            part.strip("/")
            for part in (source_prefix, relative_source_id)
            if part.strip("/")
        )
        return scope, source_id

    @staticmethod
    def _resource_title(text: str, fallback: str) -> str:
        for line in text.splitlines():
            candidate = line.strip()
            if candidate.startswith("#"):
                heading = candidate.lstrip("#").strip()
                if heading:
                    return heading
        return fallback.replace("-", " ").replace("_", " ").strip()

    def _materialize_record_sections(
        self,
        *,
        doc_id: str,
        source_id: str,
        scope: str,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        for record in records:
            record_id = str(record.get("record_id") or "").strip()
            if not record_id:
                continue
            metadata = dict(record.get("metadata") or {})
            title = str(record.get("title") or "Organization Unit").strip()
            text_value = str(record.get("search_text") or "").strip()
            if not text_value:
                prefix = str(record.get("context_prefix") or "").strip()
                content = str(record.get("content") or "").strip()
                text_value = "\n\n".join(
                    value for value in (prefix, content) if value
                )
            sections.append(
                {
                    "section_id": self._section_id(doc_id, record_id),
                    "doc_id": doc_id,
                    "source_id": source_id,
                    "scope": scope,
                    "index_version": self.settings.kb_index_version,
                    "parent_section_id": None,
                    "chapter_section_id": self._section_id(doc_id, record_id),
                    "title": title,
                    "level": 1,
                    "heading_path": [
                        str(metadata.get("department_path") or title)
                    ],
                    "source_start_line": metadata.get("source_row"),
                    "source_end_line": metadata.get("source_row"),
                    "source_start_char": None,
                    "source_end_char": None,
                    "text": text_value,
                    "metadata": {
                        "record_type": metadata.get("record_type"),
                        "record_id": record_id,
                    },
                }
            )
        return sections

    def _build_structured_record_chunks(
        self,
        *,
        records: Any,
        source_id: str,
        source_path: Path,
        doc_id: str,
        scope: str,
        collection_name: str,
        content_hash: str,
        parser_name: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> list[RetrievedChunk]:
        if not isinstance(records, list):
            raise RuntimeError("结构化解析记录必须是列表")

        output: list[RetrievedChunk] = []
        chunk_size = max(512, int(chunk_size))
        chunk_overlap = max(0, min(int(chunk_overlap), chunk_size // 3))
        ordered_records = sorted(
            records,
            key=lambda item: (
                str(
                    (item.get("metadata") or {}).get(
                        "department_path_normalized"
                    )
                    or ""
                ),
                str(item.get("record_id") or ""),
            ),
        )
        for record in ordered_records:
            if not isinstance(record, dict):
                raise RuntimeError("结构化解析记录必须是对象")
            record_id = str(record.get("record_id") or "").strip()
            prefix = str(record.get("context_prefix") or "").strip()
            if not record_id or not prefix:
                raise RuntimeError("结构化解析记录缺少 record_id 或 context_prefix")

            record_metadata = dict(record.get("metadata") or {})
            body = str(record.get("content") or "").strip()
            if not body:
                body = (
                    "正文状态 / Content Status: 源文件未提供正文，"
                    "或正文仅含不可检索占位内容；仅保留组织层级信息。"
                )
            payload_size = chunk_size - len(prefix) - 2
            if payload_size < 128:
                raise RuntimeError(
                    "组织记录上下文过长，无法在 chunk 上限内保留正文: "
                    f"{record_id}"
                )
            payload_overlap = min(chunk_overlap, payload_size // 3)
            pieces = chunk_organization_text(
                body,
                chunk_size=payload_size,
                overlap=payload_overlap,
            ) or [body]
            title = str(record.get("title") or source_path.stem).strip()
            parent_section_id = self._section_id(doc_id, record_id)
            heading_path = [
                str(record_metadata.get("department_path") or title)
            ]
            search_heading_path = [
                self._compact_organization_search_path(
                    record_metadata,
                    fallback=heading_path[0],
                )
            ]
            retrieval_prefix = str(
                record.get("retrieval_prefix") or ""
            ).strip()

            for record_chunk_index, piece in enumerate(pieces):
                full_text = f"{prefix}\n\n{piece.strip()}"
                chunk_index = len(output)
                chunk_id = self._stable_chunk_id(
                    doc_id=f"{doc_id}:{record_id}",
                    index=record_chunk_index,
                    text=full_text,
                )
                retrieval_piece = (
                    ""
                    if record_metadata.get("status") == "hierarchy_only"
                    else piece.strip()
                )
                retrieval_text = "\n".join(
                    value
                    for value in (retrieval_prefix, retrieval_piece)
                    if value
                )
                search_text = contextual_search_text(
                    scope=scope,
                    title=title,
                    heading_path=search_heading_path,
                    text=retrieval_text,
                )
                output.append(
                    RetrievedChunk(
                        chunk_id=chunk_id,
                        source_id=source_id,
                        title=title,
                        scope=scope,
                        text=full_text,
                        metadata={
                            **record_metadata,
                            "chunk_id": chunk_id,
                            "doc_id": doc_id,
                            "source_path": str(source_path),
                            "relative_path": source_id,
                            "scope": scope,
                            "tag": title,
                            "page": None,
                            "section": record_metadata.get("department_path"),
                            "content_hash": content_hash,
                            "index_version": self.settings.kb_index_version,
                            "chunk_index": chunk_index,
                            "record_chunk_index": record_chunk_index,
                            "record_chunk_count": len(pieces),
                            "collection_name": collection_name,
                            "parser": parser_name,
                            "chunking_contract": ORGANIZATION_CHUNKING_VERSION,
                            "search_text": search_text,
                            "canonical_content_id": canonical_content_id(full_text),
                            "heading_path": heading_path,
                            "parent_section_id": parent_section_id,
                            "chapter_section_id": parent_section_id,
                            "exact_facts": extract_exact_facts(
                                full_text,
                                metadata=record_metadata,
                            ),
                            "contains_image_description": False,
                            "image_refs": [],
                            "document_vision_model": None,
                        },
                    )
                )
        return output

    @classmethod
    def _compact_organization_search_path(
        cls,
        metadata: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        """Remove exact navigation-only levels from retrieval text only."""

        raw_segments = metadata.get("path_segments") or []
        segments = (
            [
                str(segment).strip()
                for segment in raw_segments
                if str(segment).strip()
            ]
            if isinstance(raw_segments, (list, tuple))
            else []
        )
        if not segments:
            return str(fallback or "").strip()

        compact = [segments[0]]
        compact.extend(
            segment
            for segment in segments[1:]
            if normalize_content(segment)
            not in cls._ORGANIZATION_SEARCH_NAVIGATION_SEGMENTS
        )
        if len(compact) == 1 and len(segments) > 1:
            deepest = segments[-1]
            if normalize_content(deepest) != normalize_content(compact[0]):
                compact.append(deepest)
        return " > ".join(compact)

    def _reset_image_analysis_summary(self) -> None:
        self._image_analysis_summary = self._empty_image_analysis_stats()
        self._image_analysis_warnings = []

    def _accumulate_image_analysis_stats(self, stats: dict[str, Any]) -> None:
        self._image_analysis_summary["enabled"] = (
            bool(self._image_analysis_summary.get("enabled")) or bool(stats.get("enabled"))
        )
        for key in (
            "candidate_count",
            "analysis_count",
            "cache_hit_count",
            "description_count",
            "duplicate_count",
            "skipped_count",
            "preparation_failed_count",
            "failed_count",
        ):
            self._image_analysis_summary[key] = int(self._image_analysis_summary.get(key) or 0) + int(
                stats.get(key) or 0
            )
        self._image_analysis_summary["elapsed_seconds"] = round(
            float(self._image_analysis_summary.get("elapsed_seconds") or 0.0)
            + float(stats.get("elapsed_seconds") or 0.0),
            3,
        )

    def _embed_chunks(
        self,
        chunks: list[RetrievedChunk],
    ) -> tuple[list[RetrievedChunk], list[list[float]]]:
        prepared_chunks: list[RetrievedChunk] = []
        prepared_embeddings: list[list[float]] = []
        batch_size = max(1, self.settings.kb_ingest_batch_size)
        for start in range(0, len(chunks), batch_size):
            batch = [chunk for chunk in chunks[start : start + batch_size] if str(chunk.text or "").strip()]
            if not batch:
                continue
            embeddings = self.embedding_service.embed(
                [
                    str(chunk.metadata.get("search_text") or chunk.text)
                    for chunk in batch
                ]
            )
            if len(embeddings) != len(batch):
                raise RuntimeError("Embedding API returned unexpected vector count.")
            prepared_chunks.extend(batch)
            prepared_embeddings.extend(embeddings)
        return prepared_chunks, prepared_embeddings

    def _embed_retrieval_units(
        self,
        retrieval_units: list[dict[str, Any]],
        *,
        embedding_service: EmbeddingService | None = None,
        expected_dimensions: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[list[float]]]:
        prepared_units: list[dict[str, Any]] = []
        prepared_embeddings: list[list[float]] = []
        service = embedding_service or self.embedding_service
        batch_size = max(
            1,
            int(getattr(self.settings, "kb_ingest_batch_size", 64) or 64),
        )
        for start in range(0, len(retrieval_units), batch_size):
            batch = [
                unit
                for unit in retrieval_units[start : start + batch_size]
                if str(unit.get("search_text") or unit.get("text") or "").strip()
            ]
            if not batch:
                continue
            embeddings = service.embed(
                [
                    str(unit.get("search_text") or unit.get("text") or "")
                    for unit in batch
                ]
            )
            if len(embeddings) != len(batch):
                raise RuntimeError(
                    "Embedding API returned unexpected retrieval-unit "
                    "vector count."
                )
            if expected_dimensions is not None:
                for vector in embeddings:
                    if len(vector) != int(expected_dimensions):
                        raise RuntimeError(
                            "Embedding API returned an unexpected retrieval-"
                            "unit vector dimension: "
                            f"expected={int(expected_dimensions)}, "
                            f"actual={len(vector)}."
                        )
            prepared_units.extend(batch)
            prepared_embeddings.extend(embeddings)
        return prepared_units, prepared_embeddings

    @staticmethod
    def _scope_from_path(raw_root: Path, path: Path) -> str:
        relative = path.relative_to(raw_root)
        return relative.parts[0] if len(relative.parts) > 1 else "general"

    def _doc_id(self, source_id: str) -> str:
        safe = source_id.replace("/", "__").replace("\\", "__")
        version = str(self.settings.kb_index_version)
        digest = hashlib.sha1(
            f"{version}:{source_id}".encode("utf-8")
        ).hexdigest()[:10]
        return f"{version}__{safe}__{digest}"

    @staticmethod
    def _section_id(doc_id: str, local_section_id: str) -> str:
        digest = hashlib.sha256(
            f"{doc_id}:{local_section_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"{doc_id}::section::{digest}"

    def _materialize_sections(
        self,
        *,
        doc_id: str,
        source_id: str,
        scope: str,
        sections: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        id_map = {
            str(section.get("local_section_id") or ""): self._section_id(
                doc_id,
                str(section.get("local_section_id") or "document"),
            )
            for section in sections
        }
        output: list[dict[str, Any]] = []
        for section in sections:
            local_id = str(section.get("local_section_id") or "")
            if not local_id:
                continue
            output.append(
                {
                    "section_id": id_map[local_id],
                    "doc_id": doc_id,
                    "source_id": source_id,
                    "scope": scope,
                    "index_version": self.settings.kb_index_version,
                    "parent_section_id": id_map.get(
                        str(section.get("parent_local_section_id") or "")
                    ),
                    "chapter_section_id": id_map.get(
                        str(section.get("chapter_local_section_id") or "")
                    ),
                    "title": str(section.get("title") or ""),
                    "level": int(section.get("level") or 0),
                    "heading_path": list(section.get("heading_path") or []),
                    "source_start_line": section.get("source_start_line"),
                    "source_end_line": section.get("source_end_line"),
                    "source_start_char": section.get("source_start_char"),
                    "source_end_char": section.get("source_end_char"),
                    "text": str(section.get("text") or ""),
                    "metadata": {"synthetic": bool(section.get("synthetic"))},
                }
            )
        return output, id_map

    @staticmethod
    def _empty_image_analysis_stats() -> dict[str, Any]:
        return {
            "enabled": False,
            "candidate_count": 0,
            "analysis_count": 0,
            "cache_hit_count": 0,
            "description_count": 0,
            "duplicate_count": 0,
            "skipped_count": 0,
            "preparation_failed_count": 0,
            "failed_count": 0,
            "elapsed_seconds": 0.0,
            "mode": "processed_data_only",
        }

    @staticmethod
    def _stable_chunk_id(doc_id: str, index: int, text: str) -> str:
        digest = hashlib.sha1(f"{doc_id}:{index}:{text}".encode("utf-8")).hexdigest()[:16]
        return f"{doc_id}::{index}::{digest}"

    @staticmethod
    def _file_hash(path: Path) -> str:
        h = hashlib.sha1()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()

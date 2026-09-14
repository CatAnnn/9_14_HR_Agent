from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from backend.config.settings import get_settings
from backend.rag.structured_facts import exact_query_terms, normalize_content
from backend.repositories.postgres_repository import (
    EmbeddingProfileState,
    PostgresRepository,
)
from backend.schemas.retrieval import RetrievedChunk
from backend.services.embedding_service import EmbeddingService
from backend.vectorstore.embedding_profile import resolve_embedding_dimensions


MAX_RETRIEVAL_UNITS_PER_PARENT = 8
EXACT_PHRASE_BONUS = 0.35
EXACT_CONTEXT_MATCH_BONUS = 0.15
EXACT_AUTHORITY_BONUS = 0.45
EXACT_MAX_MULTI_FACT_BONUS = 0.75
ORGANIZATION_EXACT_SCOPES = frozenset({"employee", "organization_unit"})
_EXACT_SEMANTIC_ALIASES = {
    ("business_code", "asr"): ("performance", "asr rating"),
    ("business_code", "how"): ("performance", "how dimension"),
    ("business_code", "what"): ("performance", "what dimension"),
}


def retrieval_units_from_metadata(
    metadata: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return unique retrieval units while preserving their declared order."""

    resolved = metadata or {}
    raw_units = resolved.get("retrieval_units")
    if not isinstance(raw_units, list):
        unit = resolved.get("retrieval_unit")
        raw_units = [unit] if isinstance(unit, dict) else []

    top_level_search_text = str(resolved.get("search_text") or "").strip()
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_unit in raw_units:
        if not isinstance(raw_unit, dict):
            continue
        unit_id = str(raw_unit.get("unit_id") or "").strip()
        if not unit_id or unit_id in seen:
            continue
        unit = dict(raw_unit)
        search_text = str(unit.get("search_text") or "").strip()
        if not search_text and len(raw_units) == 1:
            search_text = top_level_search_text
        if search_text:
            unit["search_text"] = search_text
        output.append(unit)
        seen.add(unit_id)
    return output


def _retrieval_unit_metadata(unit: dict[str, Any]) -> dict[str, Any]:
    metadata = unit.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            return {}
    return dict(metadata) if isinstance(metadata, dict) else {}


def _retrieval_unit_span(
    unit: dict[str, Any],
) -> tuple[str, int, int] | None:
    metadata = _retrieval_unit_metadata(unit)
    coordinate_pairs = (
        ("parent", "parent_text_start", "parent_text_end"),
        ("source_char", "source_start_char", "source_end_char"),
        ("source_line", "source_start_line", "source_end_line"),
    )
    for coordinate, start_key, end_key in coordinate_pairs:
        try:
            start = int(metadata.get(start_key))
            end = int(metadata.get(end_key))
        except (TypeError, ValueError):
            continue
        if end > start:
            return coordinate, start, end
    return None


def _retrieval_units_are_near_duplicates(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    if str(left.get("unit_id") or "") == str(right.get("unit_id") or ""):
        return True

    left_text = normalize_content(str(left.get("text") or ""))
    right_text = normalize_content(str(right.get("text") or ""))
    if left_text and right_text:
        if left_text == right_text:
            return True
        shorter, longer = sorted((left_text, right_text), key=len)
        if len(shorter) / max(1, len(longer)) >= 0.85 and shorter in longer:
            return True

    left_metadata = _retrieval_unit_metadata(left)
    right_metadata = _retrieval_unit_metadata(right)
    left_group = str(left_metadata.get("semantic_group_id") or "")
    right_group = str(right_metadata.get("semantic_group_id") or "")
    if not left_group or left_group != right_group:
        return False
    left_span = _retrieval_unit_span(left)
    right_span = _retrieval_unit_span(right)
    if not left_span or not right_span or left_span[0] != right_span[0]:
        return False
    overlap = max(
        0,
        min(left_span[2], right_span[2])
        - max(left_span[1], right_span[1]),
    )
    shorter_span = min(
        left_span[2] - left_span[1],
        right_span[2] - right_span[1],
    )
    return shorter_span > 0 and overlap / shorter_span >= 0.8


def select_distinct_retrieval_units(
    units: list[dict[str, Any]],
    *,
    limit: int = MAX_RETRIEVAL_UNITS_PER_PARENT,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, dict) or not str(
            unit.get("unit_id") or ""
        ).strip():
            continue
        candidate = dict(unit)
        if any(
            _retrieval_units_are_near_duplicates(candidate, existing)
            for existing in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= max(1, int(limit)):
            break
    return selected


@dataclass(slots=True)
class HybridSearchResult:
    dense: list[RetrievedChunk]
    lexical: list[RetrievedChunk]
    exact: list[RetrievedChunk] = field(default_factory=list)
    lexical_error: str | None = None


@dataclass(frozen=True, slots=True)
class HybridScopeSearchRequest:
    collection_name: str
    scope: str
    metadata_filter: dict[str, Any]
    use_ann: bool | None
    lexical_enabled: bool
    exact_enabled: bool = True


class PGVectorClient:
    """PostgreSQL + pgvector vector store client.

    KB documents, chunks, embeddings, and metadata are stored in PostgreSQL
    tables and queried with pgvector cosine distance.
    """

    supports_query_vector_literal = True
    _INDEX_ONLY_METADATA_KEYS = frozenset({"search_text", "exact_facts"})

    def __init__(
        self,
        embedding_service: EmbeddingService | None = None,
        collection_name: str | None = None,
        repository: PostgresRepository | None = None,
        embedding_profile: EmbeddingProfileState | None = None,
    ):
        self.settings = get_settings()
        self.embedding_service = embedding_service or EmbeddingService()
        self.collection_name = collection_name or self.settings.collection_name_for_scope("general")
        self.repo = repository or PostgresRepository(initialize=False)
        self.embedding_profile = embedding_profile

    def reset_collection(self) -> None:
        with self.repo.connection() as conn:
            conn.execute("DELETE FROM kb_chunks WHERE collection_name = %s", (self.collection_name,))
            conn.execute(
                """
                DELETE FROM kb_documents d
                WHERE NOT EXISTS (
                    SELECT 1 FROM kb_chunks c WHERE c.doc_id = d.doc_id
                )
                """
            )

    def upsert_document(
        self,
        document: dict[str, Any],
        *,
        connection: Any | None = None,
    ) -> None:
        if connection is not None:
            self._upsert_document_on_connection(connection, document)
            return
        with self.repo.connection() as conn:
            self._upsert_document_on_connection(conn, document)

    @staticmethod
    def _upsert_document_on_connection(
        connection: Any,
        document: dict[str, Any],
    ) -> None:
        public_document = {
            key: value
            for key, value in document.items()
            if not str(key).startswith("_")
        }
        connection.execute(
            """
            INSERT INTO kb_documents (
                doc_id, scope, source_path, relative_path, content_hash, metadata, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (doc_id) DO UPDATE SET
                scope = excluded.scope,
                source_path = excluded.source_path,
                relative_path = excluded.relative_path,
                content_hash = excluded.content_hash,
                metadata = excluded.metadata,
                updated_at = NOW()
            """,
            (
                document["doc_id"],
                document["scope"],
                document["source_path"],
                document["relative_path"],
                document["content_hash"],
                json.dumps(public_document, ensure_ascii=False),
            ),
        )

    def upsert_chunks(
        self,
        chunks: list[RetrievedChunk],
        embeddings: list[list[float]],
        *,
        connection: Any | None = None,
        embedding_profile_id: str | None = None,
        embedding_build_id: str | None = None,
    ) -> int:
        if not chunks:
            return 0
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        dimensions = {len(embedding) for embedding in embeddings if embedding}
        if len(dimensions) != 1:
            raise ValueError(
                "All embeddings in one upsert batch must have the same "
                "positive dimension."
            )
        dimension = dimensions.pop()
        if (
            embedding_build_id
            and dimension != int(resolve_embedding_dimensions(self.settings))
        ):
            raise ValueError(
                "Embedding build dimensions do not match runtime settings."
            )
        if bool(embedding_profile_id) != bool(embedding_build_id):
            raise ValueError(
                "Embedding profile_id and build_id must be provided together."
            )
        if connection is not None:
            self._upsert_chunks_on_connection(
                connection,
                chunks,
                embeddings,
                embedding_profile_id=embedding_profile_id,
                embedding_build_id=embedding_build_id,
            )
            return len(chunks)
        with self.repo.connection() as conn:
            self._upsert_chunks_on_connection(
                conn,
                chunks,
                embeddings,
                embedding_profile_id=embedding_profile_id,
                embedding_build_id=embedding_build_id,
            )
        return len(chunks)

    def _upsert_chunks_on_connection(
        self,
        connection: Any,
        chunks: list[RetrievedChunk],
        embeddings: list[list[float]],
        *,
        embedding_profile_id: str | None = None,
        embedding_build_id: str | None = None,
    ) -> None:
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            index_metadata = dict(chunk.metadata or {})
            metadata = self._to_metadata(chunk)
            connection.execute(
                """
                INSERT INTO kb_chunks (
                    chunk_id, collection_name, doc_id, source_id, title, scope,
                    text, search_text, metadata, embedding, index_version,
                    content_hash, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    %s::vector, %s, %s, NOW()
                )
                ON CONFLICT (chunk_id) DO UPDATE SET
                    collection_name = excluded.collection_name,
                    doc_id = excluded.doc_id,
                    source_id = excluded.source_id,
                    title = excluded.title,
                    scope = excluded.scope,
                    text = excluded.text,
                    search_text = excluded.search_text,
                    metadata = excluded.metadata,
                    embedding = excluded.embedding,
                    index_version = excluded.index_version,
                    content_hash = excluded.content_hash,
                    updated_at = NOW()
                """,
                (
                    chunk.chunk_id,
                    self.collection_name,
                    str(metadata.get("doc_id") or chunk.source_id),
                    chunk.source_id,
                    chunk.title,
                    chunk.scope,
                    chunk.text,
                    str(index_metadata.get("search_text") or chunk.text),
                    json.dumps(metadata, ensure_ascii=False),
                    self.repo.vector_literal(embedding),
                    str(
                        metadata.get("index_version")
                        or self.settings.kb_index_version
                    ),
                    str(metadata.get("content_hash") or ""),
                ),
            )
        if embedding_profile_id and embedding_build_id:
            self.repo.upsert_embedding_rows(
                connection,
                profile_id=embedding_profile_id,
                build_id=embedding_build_id,
                collection_name=self.collection_name,
                chunks=chunks,
                embeddings=embeddings,
            )


    def count(self) -> int:
        with self.repo.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM kb_chunks WHERE collection_name = %s", (self.collection_name,)).fetchone()
        return int(row["n"] if row else 0)

    def fetch_neighbor_chunks(
        self,
        anchors: list[RetrievedChunk],
        *,
        window: int = 1,
    ) -> list[RetrievedChunk]:
        """Fetch adjacent chunks by stored doc_id and chunk_index metadata."""

        neighbor_window = max(0, int(window))
        if neighbor_window == 0:
            return []
        anchor_payload: list[dict[str, Any]] = []
        for anchor in anchors:
            metadata = anchor.metadata or {}
            doc_id = str(metadata.get("doc_id") or "").strip()
            if not doc_id:
                continue
            try:
                chunk_index = int(metadata.get("chunk_index"))
            except (TypeError, ValueError):
                continue
            anchor_payload.append(
                {
                    "anchor_chunk_id": anchor.chunk_id,
                    "doc_id": doc_id,
                    "scope": anchor.scope,
                    "chunk_index": chunk_index,
                    "record_id": (
                        str(metadata.get("record_id")).strip()
                        if metadata.get("record_id")
                        else None
                    ),
                }
            )
        if not anchor_payload:
            return []

        with self.repo.connection() as conn:
            rows = conn.execute(
                """
                WITH anchors AS (
                    SELECT *
                    FROM jsonb_to_recordset(%s::jsonb) AS item(
                        anchor_chunk_id text,
                        doc_id text,
                        scope text,
                        chunk_index integer,
                        record_id text
                    )
                )
                SELECT
                    c.chunk_id,
                    c.source_id,
                    c.title,
                    c.scope,
                    c.text,
                    c.metadata,
                    0.0::double precision AS score,
                    anchors.anchor_chunk_id,
                    ABS(
                        (c.metadata ->> 'chunk_index')::integer
                        - anchors.chunk_index
                    ) AS neighbor_distance
                FROM anchors
                JOIN kb_chunks c
                  ON c.collection_name = %s
                 AND c.doc_id = anchors.doc_id
                 AND c.scope = anchors.scope
                 AND (c.metadata ->> 'chunk_index') ~ '^-?[0-9]+$'
                 AND ABS(
                        (c.metadata ->> 'chunk_index')::integer
                        - anchors.chunk_index
                     ) BETWEEN 1 AND %s
                 AND (
                        anchors.record_id IS NULL
                        OR c.metadata ->> 'record_id' = anchors.record_id
                     )
                ORDER BY
                    anchors.anchor_chunk_id,
                    neighbor_distance,
                    c.chunk_id
                """,
                (
                    json.dumps(anchor_payload, ensure_ascii=False),
                    self.collection_name,
                    neighbor_window,
                ),
            ).fetchall()

        output: list[RetrievedChunk] = []
        for row in rows:
            chunk = self._row_to_chunk(row)
            chunk.metadata = {
                **(chunk.metadata or {}),
                "agentic_neighbor": {
                    "parent_chunk_ids": [str(row["anchor_chunk_id"])],
                    "distance": int(row["neighbor_distance"]),
                },
            }
            output.append(chunk)
        return output

    def _active_embedding_identity(
        self,
        dimension: int,
    ) -> tuple[str, str]:
        profile = getattr(self, "embedding_profile", None)
        if profile is None:
            resolver = getattr(
                self.repo,
                "require_active_embedding_profile",
                None,
            )
            if not callable(resolver):
                profile_id = str(
                    getattr(self.settings, "embedding_profile_id", "test-profile")
                )
                return profile_id, "00000000-0000-0000-0000-000000000000"
            profile = resolver(
                self.settings.effective_embedding_model,
                resolve_embedding_dimensions(self.settings),
            )
            self.embedding_profile = profile
        if int(dimension) != int(profile.dimensions):
            raise ValueError(
                "Query embedding dimension does not match the active vector "
                f"repository: query={dimension}, profile={profile.dimensions}."
            )
        if not profile.ready or not profile.active_build_id:
            raise RuntimeError(
                "The active embedding vector repository is unavailable: "
                f"profile_id={profile.profile_id}, status={profile.status}, "
                f"reason={profile.stale_reason}."
            )
        return profile.profile_id, profile.active_build_id

    def search(self, query: str, top_k: int = 5, metadata_filter: dict | None = None) -> list[RetrievedChunk]:
        if not query.strip():
            raise ValueError("pgvector query cannot be empty.")
        query_embedding = self.embedding_service.embed_query(query)
        if not query_embedding:
            raise RuntimeError("Embedding API did not return a query vector.")
        return self.search_by_embedding(
            query_embedding,
            top_k=top_k,
            metadata_filter=metadata_filter,
        )

    def search_by_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        metadata_filter: dict | None = None,
        *,
        use_ann: bool | None = None,
        query_vector_literal: str | None = None,
    ) -> list[RetrievedChunk]:
        if not query_embedding:
            raise ValueError("pgvector query embedding cannot be empty.")
        dimension = len(query_embedding)
        profile_id, build_id = self._active_embedding_identity(dimension)
        vector = query_vector_literal or self.repo.vector_literal(
            query_embedding
        )
        where_sql, params = self._where_from_filter(
            metadata_filter or {},
            table_alias="c",
        )
        scope_value = (metadata_filter or {}).get("scope")
        dense_scope = (
            str(scope_value) if isinstance(scope_value, str) else None
        )
        ann_enabled = (
            self.settings.postgres_create_hnsw_index
            if use_ann is None
            else bool(use_ann)
        )
        with self.repo.connection() as conn:
            if ann_enabled:
                self._set_hnsw_query_settings(conn)
                self._set_partial_index_plan_settings(conn)
            rows = self._dense_rows(
                conn,
                vector=vector,
                dimension=dimension,
                top_k=top_k,
                where_sql=where_sql,
                filter_params=params,
                profile_id=profile_id,
                build_id=build_id,
                scope=dense_scope,
                use_ann=ann_enabled,
            )
            unit_rows = self._dense_retrieval_unit_rows(
                conn,
                vector=vector,
                dimension=dimension,
                top_k=top_k,
                where_sql=where_sql,
                filter_params=params,
                profile_id=profile_id,
                build_id=build_id,
                scope=dense_scope,
                use_ann=ann_enabled,
            )
            rows = self._collapse_parent_rows(
                [*rows, *unit_rows],
                top_k=top_k,
            )
        return [self._row_to_chunk(row) for row in rows]

    def search_hybrid_by_embedding(
        self,
        query: str,
        query_embedding: list[float],
        *,
        scope: str,
        dense_top_k: int = 16,
        lexical_top_k: int = 16,
        exact_top_k: int = 16,
        metadata_filter: dict | None = None,
        use_ann: bool | None = None,
        lexical_enabled: bool = True,
        exact_enabled: bool = True,
        query_vector_literal: str | None = None,
    ) -> HybridSearchResult:
        if not query.strip():
            raise ValueError("Hybrid retrieval query cannot be empty.")
        if not query_embedding:
            raise ValueError("pgvector query embedding cannot be empty.")

        dimension = len(query_embedding)
        profile_id, build_id = self._active_embedding_identity(dimension)
        vector = query_vector_literal or self.repo.vector_literal(
            query_embedding
        )
        dense_where_sql, dense_params = self._where_from_filter(
            metadata_filter or {},
            table_alias="c",
        )
        lexical_where_sql, lexical_params = self._where_from_filter(
            metadata_filter or {},
            table_alias="c",
        )
        lexical_error: str | None = None
        lexical_rows: list[dict[str, Any]] = []
        exact_rows: list[dict[str, Any]] = []
        terms = exact_query_terms(query) if exact_enabled else []
        normalized_query = normalize_content(query) if exact_enabled else ""
        ann_enabled = (
            self.settings.postgres_create_hnsw_index
            if use_ann is None
            else bool(use_ann)
        )

        lexical_requested = (
            lexical_enabled
            and self.settings.rag_hybrid_search_enabled
            and lexical_top_k > 0
        )

        with self.repo.connection() as conn:
            if ann_enabled:
                self._set_hnsw_query_settings(conn)
            if ann_enabled:
                self._set_partial_index_plan_settings(conn)
            dense_rows = self._dense_rows(
                conn,
                vector=vector,
                dimension=dimension,
                top_k=dense_top_k,
                where_sql=dense_where_sql,
                scope=scope,
                filter_params=dense_params,
                profile_id=profile_id,
                build_id=build_id,
                use_ann=ann_enabled,
            )
            dense_unit_rows = self._dense_retrieval_unit_rows(
                conn,
                vector=vector,
                dimension=dimension,
                top_k=dense_top_k,
                where_sql=dense_where_sql,
                scope=scope,
                filter_params=dense_params,
                profile_id=profile_id,
                build_id=build_id,
                use_ann=ann_enabled,
            )
            dense_rows = self._collapse_parent_rows(
                [*dense_rows, *dense_unit_rows],
                top_k=dense_top_k,
            )
            if exact_enabled and exact_top_k > 0:
                exact_rows = self._exact_rows(
                    conn,
                    terms=terms,
                    normalized_query=normalized_query,
                    top_k=exact_top_k,
                    where_sql=dense_where_sql,
                    filter_params=dense_params,
                    collection_name=self.collection_name,
                    scope=scope,
                )

        if lexical_requested:
            try:
                with self.repo.connection() as lexical_conn:
                    self._set_partial_index_plan_settings(lexical_conn)
                    self._set_bm25_query_settings(lexical_conn)
                    lexical_rows = self._lexical_rows(
                        lexical_conn,
                        query=query,
                        scope=scope,
                        top_k=lexical_top_k,
                        where_sql=lexical_where_sql,
                        filter_params=lexical_params,
                    )
            except Exception as exc:  # noqa: BLE001
                lexical_error = f"{type(exc).__name__}: {str(exc)[:300]}"

            lexical_unit_rows: list[dict[str, Any]] = []
            if self._retrieval_unit_lexical_available():
                try:
                    with self.repo.connection() as lexical_unit_conn:
                        self._set_partial_index_plan_settings(lexical_unit_conn)
                        self._set_bm25_query_settings(lexical_unit_conn)
                        lexical_unit_rows = self._lexical_retrieval_unit_rows(
                            lexical_unit_conn,
                            query=query,
                            scope=scope,
                            top_k=lexical_top_k,
                            where_sql=lexical_where_sql,
                            filter_params=lexical_params,
                        )
                except Exception:  # noqa: BLE001
                    lexical_unit_rows = []
            lexical_rows = self._collapse_parent_rows(
                [*lexical_rows, *lexical_unit_rows],
                top_k=lexical_top_k,
            )

        return HybridSearchResult(
            dense=[self._row_to_chunk(row) for row in dense_rows],
            lexical=[self._row_to_chunk(row) for row in lexical_rows],
            exact=[self._row_to_chunk(row) for row in exact_rows],
            lexical_error=lexical_error,
        )

    def search_hybrid_multi_scope_by_embedding(
        self,
        query: str,
        query_embedding: list[float],
        *,
        requests: list[HybridScopeSearchRequest],
        dense_top_k: int = 16,
        lexical_top_k: int = 16,
        exact_top_k: int = 16,
        query_vector_literal: str | None = None,
    ) -> list[HybridSearchResult]:
        """Run independent scope branches in one explicit database transaction."""

        if not query.strip():
            raise ValueError("Hybrid retrieval query cannot be empty.")
        if not query_embedding:
            raise ValueError("pgvector query embedding cannot be empty.")
        if not requests:
            return []

        dimension = len(query_embedding)
        profile_id, build_id = self._active_embedding_identity(dimension)
        vector = query_vector_literal or self.repo.vector_literal(query_embedding)
        ann_flags = [
            self.settings.postgres_create_hnsw_index
            if request.use_ann is None
            else bool(request.use_ann)
            for request in requests
        ]
        lexical_groups: list[list[dict[str, Any]]] = [
            [] for _ in requests
        ]
        lexical_errors: list[str | None] = [None for _ in requests]
        lexical_indexes = [
            index
            for index, request in enumerate(requests)
            if request.lexical_enabled
            and self.settings.rag_hybrid_search_enabled
            and lexical_top_k > 0
        ]
        if lexical_indexes:
            try:
                with self.repo.connection() as lexical_conn:
                    self._set_partial_index_plan_settings(lexical_conn)
                    self._set_bm25_query_settings(lexical_conn)
                    lexical_groups = self._lexical_multi_scope_rows(
                        lexical_conn,
                        query=query,
                        top_k=lexical_top_k,
                        requests=requests,
                        request_indexes=lexical_indexes,
                    )
            except Exception:  # noqa: BLE001
                for request_index in lexical_indexes:
                    request = requests[request_index]
                    where_sql, params = self._where_from_filter(
                        request.metadata_filter,
                        table_alias="c",
                    )
                    try:
                        with self.repo.connection() as lexical_scope_conn:
                            self._set_partial_index_plan_settings(
                                lexical_scope_conn
                            )
                            self._set_bm25_query_settings(lexical_scope_conn)
                            lexical_groups[request_index] = self._lexical_rows(
                                lexical_scope_conn,
                                query=query,
                                scope=request.scope,
                                top_k=lexical_top_k,
                                where_sql=where_sql,
                                filter_params=params,
                                collection_name=request.collection_name,
                            )
                    except Exception as exc:  # noqa: BLE001
                        lexical_errors[request_index] = (
                            f"{type(exc).__name__}: {str(exc)[:300]}"
                        )

            lexical_unit_groups = [[] for _ in requests]
            if self._retrieval_unit_lexical_available():
                try:
                    with self.repo.connection() as lexical_unit_conn:
                        self._set_partial_index_plan_settings(lexical_unit_conn)
                        self._set_bm25_query_settings(lexical_unit_conn)
                        lexical_unit_groups = (
                            self._lexical_retrieval_unit_multi_scope_rows(
                                lexical_unit_conn,
                                query=query,
                                top_k=lexical_top_k,
                                requests=requests,
                                request_indexes=lexical_indexes,
                            )
                        )
                except Exception:  # noqa: BLE001
                    lexical_unit_groups = [[] for _ in requests]
            lexical_groups = [
                self._collapse_parent_rows(
                    [
                        *lexical_groups[index],
                        *lexical_unit_groups[index],
                    ],
                    top_k=lexical_top_k,
                )
                for index in range(len(requests))
            ]

        with self.repo.connection() as conn:
            if any(ann_flags):
                self._set_hnsw_query_settings(conn)
            if any(ann_flags):
                self._set_partial_index_plan_settings(conn)
            dense_groups = self._dense_multi_scope_rows(
                conn,
                vector=vector,
                dimension=dimension,
                top_k=dense_top_k,
                requests=requests,
                profile_id=profile_id,
                build_id=build_id,
                ann_flags=ann_flags,
            )
            dense_unit_groups = (
                self._dense_retrieval_unit_multi_scope_rows(
                    conn,
                    vector=vector,
                    dimension=dimension,
                    top_k=dense_top_k,
                    requests=requests,
                    profile_id=profile_id,
                    build_id=build_id,
                    ann_flags=ann_flags,
                )
            )
            dense_groups = [
                self._collapse_parent_rows(
                    [*dense_groups[index], *dense_unit_groups[index]],
                    top_k=dense_top_k,
                )
                for index in range(len(requests))
            ]
            terms = exact_query_terms(query)
            normalized_query = normalize_content(query)
            exact_indexes = [
                index
                for index, request in enumerate(requests)
                if request.exact_enabled and exact_top_k > 0
            ]
            exact_groups = (
                self._exact_multi_scope_rows(
                    conn,
                    terms=terms,
                    normalized_query=normalized_query,
                    top_k=exact_top_k,
                    requests=requests,
                    request_indexes=exact_indexes,
                )
                if exact_indexes
                else [[] for _ in requests]
            )

        return [
            HybridSearchResult(
                dense=[self._row_to_chunk(row) for row in dense_groups[index]],
                lexical=[
                    self._row_to_chunk(row) for row in lexical_groups[index]
                ],
                exact=[
                    self._row_to_chunk(row) for row in exact_groups[index]
                ],
                lexical_error=lexical_errors[index],
            )
            for index in range(len(requests))
        ]

    def _dense_multi_scope_rows(
        self,
        conn,
        *,
        vector: str,
        dimension: int,
        top_k: int,
        requests: list[HybridScopeSearchRequest],
        profile_id: str,
        build_id: str,
        ann_flags: list[bool],
    ) -> list[list[dict[str, Any]]]:
        branches: list[str] = []
        branch_params: list[Any] = []
        for request_index, (request, ann_enabled) in enumerate(
            zip(requests, ann_flags, strict=True)
        ):
            where_sql, filter_params = self._where_from_filter(
                request.metadata_filter,
                table_alias="c",
            )
            common_where = f"""
                e.profile_id = %s
                AND e.build_id = %s::uuid
                AND e.collection_name = %s
                AND e.scope = %s
                AND vector_dims(e.embedding) = {dimension}
                {where_sql}
            """
            if ann_enabled and dimension <= 4000:
                vector_type = "halfvec" if dimension > 2000 else "vector"
                distance_expr = (
                    f"e.embedding::{vector_type}({dimension}) "
                    f"<=> %s::{vector_type}({dimension})"
                )
                branches.append(
                    f"""
                    (
                        SELECT {request_index}::integer AS branch_index,
                               c.chunk_id, c.source_id, c.title, c.scope,
                               c.text, c.metadata,
                               1 - ({distance_expr}) AS score
                          FROM kb_chunk_embeddings e
                          JOIN kb_chunks c ON c.chunk_id = e.chunk_id
                         WHERE {common_where}
                         ORDER BY e.embedding::{vector_type}({dimension})
                                  <=> %s::{vector_type}({dimension})
                         LIMIT %s
                    )
                    """
                )
                branch_params.extend(
                    (
                        vector,
                        profile_id,
                        build_id,
                        request.collection_name,
                        request.scope,
                        *filter_params,
                        vector,
                        int(top_k),
                    )
                )
                continue

            branches.append(
                f"""
                (
                    SELECT {request_index}::integer AS branch_index,
                           c.chunk_id, c.source_id, c.title, c.scope,
                           c.text, c.metadata,
                           1 - (e.embedding <=> %s::vector) AS score
                      FROM kb_chunk_embeddings e
                      JOIN kb_chunks c ON c.chunk_id = e.chunk_id
                     WHERE {common_where}
                     ORDER BY score DESC
                     LIMIT %s
                )
                """
            )
            branch_params.extend(
                (
                    vector,
                    profile_id,
                    build_id,
                    request.collection_name,
                    request.scope,
                    *filter_params,
                    int(top_k),
                )
            )

        sql = f"""
            SELECT *
              FROM ({' UNION ALL '.join(branches)}) AS scoped_dense
             ORDER BY branch_index, score DESC
        """
        rows = conn.execute(sql, tuple(branch_params)).fetchall()
        return self._group_scope_rows(rows, len(requests))

    def _dense_retrieval_unit_multi_scope_rows(
        self,
        conn,
        *,
        vector: str,
        dimension: int,
        top_k: int,
        requests: list[HybridScopeSearchRequest],
        profile_id: str,
        build_id: str,
        ann_flags: list[bool],
    ) -> list[list[dict[str, Any]]]:
        branches: list[str] = []
        params: list[Any] = []
        unit_top_k = self._retrieval_unit_overfetch_limit(top_k)
        for request_index, (request, ann_enabled) in enumerate(
            zip(requests, ann_flags, strict=True)
        ):
            where_sql, filter_params = self._where_from_filter(
                request.metadata_filter,
                table_alias="c",
            )
            if ann_enabled and dimension <= 4000:
                vector_type = "halfvec" if dimension > 2000 else "vector"
                distance_expr = (
                    f"e.embedding::{vector_type}({dimension}) "
                    f"<=> %s::{vector_type}({dimension})"
                )
                branches.append(
                    f"""
                    (
                        SELECT {request_index}::integer AS branch_index,
                               c.chunk_id, c.source_id, c.title, c.scope,
                               c.text,
                               c.metadata || jsonb_build_object(
                                   'search_text', u.search_text,
                                   'retrieval_unit', jsonb_build_object(
                                       'unit_id', u.unit_id,
                                       'unit_type', u.unit_type,
                                       'unit_index', u.unit_index,
                                       'text', u.text,
                                       'metadata', u.metadata
                                   )
                               ) AS metadata,
                               1 - ({distance_expr}) AS score
                          FROM kb_retrieval_unit_embeddings e
                          JOIN kb_retrieval_units u
                            ON u.unit_id = e.unit_id
                          JOIN kb_chunks c
                            ON c.chunk_id = u.parent_chunk_id
                         WHERE e.profile_id = %s
                           AND e.build_id = %s::uuid
                           AND e.collection_name = %s
                           AND e.scope = %s
                           AND vector_dims(e.embedding) = {dimension}
                           {where_sql}
                         ORDER BY e.embedding::{vector_type}({dimension})
                                  <=> %s::{vector_type}({dimension})
                         LIMIT %s
                    )
                    """
                )
                params.extend(
                    (
                        vector,
                        profile_id,
                        build_id,
                        request.collection_name,
                        request.scope,
                        *filter_params,
                        vector,
                        unit_top_k,
                    )
                )
                continue
            branches.append(
                f"""
                (
                    SELECT {request_index}::integer AS branch_index,
                           c.chunk_id, c.source_id, c.title, c.scope,
                           c.text,
                           c.metadata || jsonb_build_object(
                               'search_text', u.search_text,
                               'retrieval_unit', jsonb_build_object(
                                   'unit_id', u.unit_id,
                                   'unit_type', u.unit_type,
                                   'unit_index', u.unit_index,
                                   'text', u.text,
                                   'metadata', u.metadata
                               )
                           ) AS metadata,
                           1 - (e.embedding <=> %s::vector) AS score
                      FROM kb_retrieval_unit_embeddings e
                      JOIN kb_retrieval_units u
                        ON u.unit_id = e.unit_id
                      JOIN kb_chunks c
                        ON c.chunk_id = u.parent_chunk_id
                     WHERE e.profile_id = %s
                       AND e.build_id = %s::uuid
                       AND e.collection_name = %s
                       AND e.scope = %s
                       AND vector_dims(e.embedding) = {dimension}
                       {where_sql}
                     ORDER BY score DESC
                     LIMIT %s
                )
                """
            )
            params.extend(
                (
                    vector,
                    profile_id,
                    build_id,
                    request.collection_name,
                    request.scope,
                    *filter_params,
                    unit_top_k,
                )
            )
        if not branches:
            return [[] for _ in requests]
        rows = conn.execute(
            f"""
            SELECT *
              FROM ({' UNION ALL '.join(branches)}) AS scoped_unit_dense
             ORDER BY branch_index, score DESC
            """,
            tuple(params),
        ).fetchall()
        groups = self._group_scope_rows(rows, len(requests))
        return [
            self._diversify_retrieval_unit_rows(
                group,
                top_k=top_k,
            )
            for group in groups
        ]

    def _lexical_multi_scope_rows(
        self,
        conn,
        *,
        query: str,
        top_k: int,
        requests: list[HybridScopeSearchRequest],
        request_indexes: list[int],
    ) -> list[list[dict[str, Any]]]:
        branches: list[str] = []
        params: list[Any] = []
        tokenizer = str(self.settings.postgres_bm25_tokenizer_name)
        for request_index in request_indexes:
            request = requests[request_index]
            where_sql, filter_params = self._where_from_filter(
                request.metadata_filter,
                table_alias="c",
            )
            index_name = self.repo.bm25_index_name(
                request.collection_name,
                request.scope,
            )
            distance_expr = (
                "bm25_embedding <&> bm25_catalog.to_bm25query("
                f"'public.{index_name}'::regclass, "
                "tokenizer_catalog.tokenize(%s, %s))"
            )
            branches.append(
                f"""
                (
                    SELECT {request_index}::integer AS branch_index,
                           c.chunk_id, c.source_id, c.title, c.scope,
                           c.text, c.metadata,
                           -({distance_expr}) AS score
                      FROM kb_chunks c
                     WHERE c.collection_name = %s
                       AND c.bm25_embedding IS NOT NULL
                       {where_sql}
                     ORDER BY {distance_expr}
                     LIMIT %s
                )
                """
            )
            params.extend(
                (
                    query,
                    tokenizer,
                    request.collection_name,
                    *filter_params,
                    query,
                    tokenizer,
                    int(top_k),
                )
            )
        sql = f"""
            SELECT *
              FROM ({' UNION ALL '.join(branches)}) AS scoped_lexical
             ORDER BY branch_index, score DESC
        """
        rows = conn.execute(sql, tuple(params)).fetchall()
        return self._group_scope_rows(rows, len(requests))

    def _lexical_retrieval_unit_multi_scope_rows(
        self,
        conn,
        *,
        query: str,
        top_k: int,
        requests: list[HybridScopeSearchRequest],
        request_indexes: list[int],
    ) -> list[list[dict[str, Any]]]:
        branches: list[str] = []
        params: list[Any] = []
        tokenizer = self.repo.retrieval_unit_bm25_tokenizer_name()
        index_name = self.repo.retrieval_unit_bm25_index_name()
        unit_top_k = self._retrieval_unit_overfetch_limit(top_k)
        distance_expr = (
            "u.bm25_embedding <&> bm25_catalog.to_bm25query("
            f"'public.{index_name}'::regclass, "
            "tokenizer_catalog.tokenize(%s, %s))"
        )
        for request_index in request_indexes:
            request = requests[request_index]
            where_sql, filter_params = self._where_from_filter(
                request.metadata_filter,
                table_alias="c",
            )
            branches.append(
                f"""
                (
                    SELECT {request_index}::integer AS branch_index,
                           c.chunk_id, c.source_id, c.title, c.scope,
                           c.text,
                           c.metadata || jsonb_build_object(
                               'search_text', u.search_text,
                               'retrieval_unit', jsonb_build_object(
                                   'unit_id', u.unit_id,
                                   'unit_type', u.unit_type,
                                   'unit_index', u.unit_index,
                                   'text', u.text,
                                   'metadata', u.metadata
                               )
                           ) AS metadata,
                           -({distance_expr}) AS score
                      FROM kb_retrieval_units u
                      JOIN kb_chunks c
                        ON c.chunk_id = u.parent_chunk_id
                     WHERE u.collection_name = %s
                       AND u.scope = %s
                       AND u.bm25_embedding IS NOT NULL
                       {where_sql}
                     ORDER BY {distance_expr}
                     LIMIT %s
                )
                """
            )
            params.extend(
                (
                    query,
                    tokenizer,
                    request.collection_name,
                    request.scope,
                    *filter_params,
                    query,
                    tokenizer,
                    unit_top_k,
                )
            )
        if not branches:
            return [[] for _ in requests]
        rows = conn.execute(
            f"""
            SELECT *
              FROM ({' UNION ALL '.join(branches)}) AS scoped_unit_lexical
             ORDER BY branch_index, score DESC
            """,
            tuple(params),
        ).fetchall()
        groups = self._group_scope_rows(rows, len(requests))
        return [
            self._diversify_retrieval_unit_rows(
                group,
                top_k=top_k,
            )
            for group in groups
        ]

    def _exact_rows(
        self,
        conn,
        *,
        terms: list[str],
        normalized_query: str = "",
        top_k: int,
        where_sql: str,
        filter_params: list[Any],
        collection_name: str,
        scope: str,
    ) -> list[dict[str, Any]]:
        organization_context_enabled = scope in ORGANIZATION_EXACT_SCOPES
        if top_k <= 0 or not (
            terms or (normalized_query and organization_context_enabled)
        ):
            return []
        rows = conn.execute(
            self._exact_scope_rows_sql(
                where_sql=where_sql,
                organization_context_enabled=organization_context_enabled,
            ),
            self._exact_scope_rows_params(
                collection_name=collection_name,
                scope=scope,
                filter_params=filter_params,
                terms=terms,
                normalized_query=normalized_query,
            ),
        ).fetchall()
        return self._rank_exact_fact_rows(
            rows,
            scope=scope,
            top_k=top_k,
        )

    def _exact_multi_scope_rows(
        self,
        conn,
        *,
        terms: list[str],
        normalized_query: str = "",
        top_k: int,
        requests: list[HybridScopeSearchRequest],
        request_indexes: list[int],
    ) -> list[list[dict[str, Any]]]:
        branches: list[str] = []
        params: list[Any] = []
        effective_request_indexes: list[int] = []
        for request_index in request_indexes:
            request = requests[request_index]
            organization_context_enabled = (
                request.scope in ORGANIZATION_EXACT_SCOPES
            )
            if not terms and not organization_context_enabled:
                continue
            effective_request_indexes.append(request_index)
            where_sql, filter_params = self._where_from_filter(
                request.metadata_filter,
                table_alias="c",
            )
            branches.append(
                "("
                + self._exact_scope_rows_sql(
                    where_sql=where_sql,
                    branch_index=request_index,
                    organization_context_enabled=(
                        organization_context_enabled
                    ),
                )
                + ")"
            )
            params.extend(
                self._exact_scope_rows_params(
                    collection_name=request.collection_name,
                    scope=request.scope,
                    filter_params=filter_params,
                    terms=terms,
                    normalized_query=normalized_query,
                )
            )
        if not branches:
            return [[] for _ in requests]
        rows = conn.execute(
            f"""
            SELECT * FROM ({' UNION ALL '.join(branches)}) AS scoped_exact
            ORDER BY branch_index, chunk_id, unit_index NULLS LAST
            """,
            tuple(params),
        ).fetchall()
        raw_groups = self._group_scope_rows(rows, len(requests))
        requested_indexes = set(effective_request_indexes)
        return [
            self._rank_exact_fact_rows(
                raw_groups[index],
                scope=requests[index].scope,
                top_k=top_k,
            )
            if index in requested_indexes
            else []
            for index in range(len(requests))
        ]

    @staticmethod
    def _exact_scope_rows_params(
        *,
        collection_name: str,
        scope: str,
        filter_params: list[Any],
        terms: list[str],
        normalized_query: str,
    ) -> tuple[Any, ...]:
        return (
            collection_name,
            scope,
            *filter_params,
            terms,
            normalized_query,
        )

    @staticmethod
    def _exact_scope_rows_sql(
        *,
        where_sql: str,
        branch_index: int | None = None,
        organization_context_enabled: bool = False,
    ) -> str:
        branch_column = (
            f"{int(branch_index)}::integer AS branch_index, "
            if branch_index is not None
            else ""
        )
        organization_match = (
            """
                (
                    f.fact_type IN (
                        'organization_code',
                        'organization_path',
                        'organization_unit'
                    )
                    AND char_length(f.normalized_value) >= 2
                    AND strpos(
                        ' ' || regexp_replace(
                            q.normalized_query,
                            '[^[:alnum:]_/-]+',
                            ' ',
                            'g'
                        ) || ' ',
                        ' ' || regexp_replace(
                            f.normalized_value,
                            '[^[:alnum:]_/-]+',
                            ' ',
                            'g'
                        ) || ' '
                    ) > 0
                )
            """
            if organization_context_enabled
            else "FALSE"
        )
        return f"""
            WITH eligible_chunks AS (
                SELECT c.*
                FROM kb_chunks c
                WHERE c.collection_name = %s
                  AND c.scope = %s
                  {where_sql}
            ),
            corpus_stats AS (
                SELECT
                    GREATEST(COUNT(DISTINCT chunk_id), 1)::integer
                        AS document_count
                FROM kb_chunks
            ),
            query_input AS (
                SELECT
                    %s::text[] AS terms,
                    %s::text AS normalized_query
            ),
            parent_frequencies AS (
                SELECT
                    f.fact_type,
                    f.normalized_value,
                    COUNT(DISTINCT f.chunk_id)::integer
                        AS document_frequency
                FROM kb_exact_facts f
                CROSS JOIN query_input q
                WHERE f.normalized_value = ANY(q.terms)
                   OR {organization_match}
                GROUP BY f.fact_type, f.normalized_value
            )
            SELECT
                {branch_column}
                'unit'::text AS match_source,
                c.chunk_id,
                c.source_id,
                c.title,
                c.scope,
                c.text,
                c.metadata,
                u.unit_id,
                u.unit_type,
                u.unit_index,
                u.text AS unit_text,
                u.search_text AS unit_search_text,
                u.metadata AS unit_metadata,
                f.fact_type,
                f.normalized_value,
                f.source_quote,
                f.document_frequency,
                s.document_count AS corpus_document_count,
                CASE
                    WHEN f.normalized_value = ANY(q.terms)
                    THEN 'phrase'
                    ELSE 'context'
                END AS match_kind
            FROM kb_retrieval_unit_exact_facts f
            JOIN kb_retrieval_units u ON u.unit_id = f.unit_id
            JOIN eligible_chunks c ON c.chunk_id = f.parent_chunk_id
            CROSS JOIN corpus_stats s
            CROSS JOIN query_input q
            WHERE f.normalized_value = ANY(q.terms)
               OR {organization_match}

            UNION ALL

            SELECT
                {branch_column}
                'parent'::text AS match_source,
                c.chunk_id,
                c.source_id,
                c.title,
                c.scope,
                c.text,
                c.metadata,
                NULL::text AS unit_id,
                NULL::text AS unit_type,
                NULL::integer AS unit_index,
                NULL::text AS unit_text,
                NULL::text AS unit_search_text,
                '{{}}'::jsonb AS unit_metadata,
                f.fact_type,
                f.normalized_value,
                f.source_quote,
                frequencies.document_frequency,
                s.document_count AS corpus_document_count,
                CASE
                    WHEN f.normalized_value = ANY(q.terms)
                    THEN 'phrase'
                    ELSE 'context'
                END AS match_kind
            FROM kb_exact_facts f
            JOIN eligible_chunks c ON c.chunk_id = f.chunk_id
            JOIN parent_frequencies frequencies
              ON frequencies.fact_type = f.fact_type
             AND frequencies.normalized_value = f.normalized_value
            CROSS JOIN corpus_stats s
            CROSS JOIN query_input q
            WHERE f.normalized_value = ANY(q.terms)
               OR {organization_match}
        """

    @staticmethod
    def _exact_authority_bonus(
        *,
        fact_type: str,
        normalized_value: str,
        scope: str,
    ) -> float:
        if fact_type == "career_element" and scope == "career":
            return EXACT_AUTHORITY_BONUS
        if fact_type == "culture" and scope == "culture":
            return EXACT_AUTHORITY_BONUS
        if fact_type == "performance" and scope == "performance":
            return EXACT_AUTHORITY_BONUS
        if (
            fact_type.startswith("organization_")
            and scope in {"employee", "organization_unit"}
        ):
            return EXACT_AUTHORITY_BONUS
        if fact_type != "business_code":
            return 0.0

        value = normalized_value.casefold()
        if scope == "job_level" and (
            value.startswith("g") or value.startswith("sl")
        ):
            return EXACT_AUTHORITY_BONUS
        if scope == "career" and (
            value.startswith("tp") or value.startswith("etp")
        ):
            return EXACT_AUTHORITY_BONUS
        if scope == "performance" and value in {
            "asr",
            "how",
            "tcl",
            "what",
        }:
            return EXACT_AUTHORITY_BONUS
        if scope in {"performance", "redline"} and value == "pip":
            return EXACT_AUTHORITY_BONUS
        return 0.0

    @classmethod
    def _exact_group_score(
        cls,
        rows: list[dict[str, Any]],
        *,
        scope: str,
    ) -> float:
        if not rows:
            return 0.0
        document_count = max(
            1,
            max(
                int(row.get("corpus_document_count") or 1)
                for row in rows
            ),
        )
        semantic_groups: dict[
            tuple[str, str],
            list[dict[str, Any]],
        ] = {}
        for row in rows:
            fact_type = str(row.get("fact_type") or "")
            normalized_value = str(row.get("normalized_value") or "")
            identity = cls._exact_semantic_identity(
                fact_type=fact_type,
                normalized_value=normalized_value,
            )
            semantic_groups.setdefault(identity, []).append(row)

        identity_scores: dict[tuple[str, str], float] = {}
        for identity, identity_rows in semantic_groups.items():
            document_frequency = max(
                1,
                min(
                    document_count,
                    max(
                        int(row.get("document_frequency") or 1)
                        for row in identity_rows
                    ),
                ),
            )
            idf = (
                math.log(
                    (document_count + 1.0)
                    / (document_frequency + 1.0)
                )
                + 1.0
            )
            phrase_bonus = max(
                EXACT_PHRASE_BONUS
                if str(row.get("match_kind") or "") == "phrase"
                else EXACT_CONTEXT_MATCH_BONUS
                for row in identity_rows
            )
            authority_bonus = max(
                cls._exact_authority_bonus(
                    fact_type=str(row.get("fact_type") or ""),
                    normalized_value=str(
                        row.get("normalized_value") or ""
                    ),
                    scope=scope,
                )
                for row in identity_rows
            )
            identity_scores[identity] = idf + phrase_bonus + authority_bonus
        if not identity_scores:
            return 0.0
        multi_fact_bonus = min(
            EXACT_MAX_MULTI_FACT_BONUS,
            max(0, len(identity_scores) - 1) * 0.25,
        )
        return max(identity_scores.values()) + multi_fact_bonus

    @staticmethod
    def _exact_semantic_identity(
        *,
        fact_type: str,
        normalized_value: str,
    ) -> tuple[str, str]:
        identity = (fact_type, normalized_value)
        return _EXACT_SEMANTIC_ALIASES.get(identity, identity)

    @staticmethod
    def _exact_matches(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        matches: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            fact_type = str(row.get("fact_type") or "")
            normalized_value = str(row.get("normalized_value") or "")
            identity = (fact_type, normalized_value)
            candidate = {
                "fact_type": fact_type,
                "normalized_value": normalized_value,
                "source_quote": str(row.get("source_quote") or ""),
                "document_frequency": max(
                    1,
                    int(row.get("document_frequency") or 1),
                ),
                "match_kind": str(row.get("match_kind") or "phrase"),
            }
            current = matches.get(identity)
            if current is None or (
                candidate["match_kind"] == "phrase"
                and current["match_kind"] != "phrase"
            ):
                matches[identity] = candidate
        return sorted(
            matches.values(),
            key=lambda item: (
                item["match_kind"] != "phrase",
                int(item["document_frequency"]),
                str(item["fact_type"]),
                str(item["normalized_value"]),
            ),
        )

    @classmethod
    def _rank_exact_fact_rows(
        cls,
        rows: list[dict[str, Any]],
        *,
        scope: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        if not rows or top_k <= 0:
            return []

        parent_facts: dict[str, list[dict[str, Any]]] = {}
        unit_facts: dict[
            str,
            dict[str, list[dict[str, Any]]],
        ] = {}
        for raw_row in rows:
            row = dict(raw_row)
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id:
                continue
            parent_facts.setdefault(chunk_id, []).append(row)
            unit_id = str(row.get("unit_id") or "")
            if row.get("match_source") == "unit" and unit_id:
                unit_facts.setdefault(chunk_id, {}).setdefault(
                    unit_id,
                    [],
                ).append(row)

        ranked_rows: list[dict[str, Any]] = []
        for chunk_id, fact_rows in parent_facts.items():
            parent_score = cls._exact_group_score(
                fact_rows,
                scope=scope,
            )
            matching_units = unit_facts.get(chunk_id, {})
            if not matching_units:
                row = dict(fact_rows[0])
                metadata = row.get("metadata") or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                metadata = dict(metadata)
                metadata["exact_matches"] = cls._exact_matches(fact_rows)
                row["metadata"] = metadata
                row["score"] = parent_score
                ranked_rows.append(row)
                continue

            for unit_id, unit_rows in matching_units.items():
                row = dict(unit_rows[0])
                metadata = row.get("metadata") or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                metadata = dict(metadata)
                unit_metadata = row.get("unit_metadata") or {}
                if isinstance(unit_metadata, str):
                    unit_metadata = json.loads(unit_metadata)
                unit_search_text = str(
                    row.get("unit_search_text") or ""
                ).strip()
                retrieval_unit = {
                    "unit_id": unit_id,
                    "unit_type": str(row.get("unit_type") or ""),
                    "unit_index": int(row.get("unit_index") or 0),
                    "text": str(row.get("unit_text") or ""),
                    "metadata": dict(unit_metadata),
                    "exact_facts": cls._exact_matches(unit_rows),
                }
                if unit_search_text:
                    retrieval_unit["search_text"] = unit_search_text
                    metadata["search_text"] = unit_search_text
                metadata["retrieval_unit"] = retrieval_unit
                metadata["exact_matches"] = cls._exact_matches(fact_rows)
                row["metadata"] = metadata
                unit_score = cls._exact_group_score(
                    unit_rows,
                    scope=scope,
                )
                row["score"] = parent_score + unit_score * 0.000001
                ranked_rows.append(row)

        return cls._collapse_parent_rows(
            ranked_rows,
            top_k=top_k,
        )

    @staticmethod
    def _group_scope_rows(
        rows: list[dict[str, Any]],
        group_count: int,
    ) -> list[list[dict[str, Any]]]:
        groups: list[list[dict[str, Any]]] = [[] for _ in range(group_count)]
        for row in rows:
            branch_index = int(row["branch_index"])
            if 0 <= branch_index < group_count:
                groups[branch_index].append(row)
        return groups

    def _set_hnsw_query_settings(self, conn) -> None:
        iterative_scan = self.settings.postgres_hnsw_iterative_scan
        ef_search = int(self.settings.postgres_hnsw_ef_search)
        max_scan_tuples = int(self.settings.postgres_hnsw_max_scan_tuples)
        conn.execute(f"SET LOCAL hnsw.iterative_scan = {iterative_scan}")
        conn.execute(f"SET LOCAL hnsw.ef_search = {ef_search}")
        conn.execute(f"SET LOCAL hnsw.max_scan_tuples = {max_scan_tuples}")

    def _set_bm25_query_settings(self, conn) -> None:
        conn.execute(f"SET LOCAL bm25_catalog.bm25_limit = {int(self.settings.postgres_bm25_limit)}")

    @staticmethod
    def _set_partial_index_plan_settings(conn) -> None:
        conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")

    def _dense_rows(
        self,
        conn,
        *,
        vector: str,
        dimension: int,
        top_k: int,
        where_sql: str,
        filter_params: list[Any],
        profile_id: str,
        build_id: str,
        use_ann: bool | None = None,
        collection_name: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        ann_enabled = (
            self.settings.postgres_create_hnsw_index
            if use_ann is None
            else bool(use_ann)
        )
        resolved_collection = collection_name or self.collection_name
        scope_sql = "AND e.scope = %s" if scope else ""
        identity_params: tuple[Any, ...] = (
            profile_id,
            build_id,
            resolved_collection,
            *((str(scope),) if scope else ()),
        )
        if ann_enabled and scope and dimension <= 4000:
            vector_type = "halfvec" if dimension > 2000 else "vector"
            distance_expr = (
                f"e.embedding::{vector_type}({dimension}) "
                f"<=> %s::{vector_type}({dimension})"
            )
            sql = f"""
                SELECT
                    c.chunk_id, c.source_id, c.title, c.scope, c.text,
                    c.metadata, 1 - ({distance_expr}) AS score
                FROM kb_chunk_embeddings e
                JOIN kb_chunks c ON c.chunk_id = e.chunk_id
                WHERE e.profile_id = %s
                  AND e.build_id = %s::uuid
                  AND e.collection_name = %s
                  {scope_sql}
                  AND vector_dims(e.embedding) = {dimension}
                  {where_sql}
                ORDER BY e.embedding::{vector_type}({dimension})
                         <=> %s::{vector_type}({dimension})
                LIMIT %s
            """
            return conn.execute(
                sql,
                (
                    vector,
                    *identity_params,
                    *filter_params,
                    vector,
                    int(top_k),
                ),
            ).fetchall()

        sql = f"""
            SELECT
                c.chunk_id, c.source_id, c.title, c.scope, c.text,
                c.metadata, 1 - (e.embedding <=> %s::vector) AS score
            FROM kb_chunk_embeddings e
            JOIN kb_chunks c ON c.chunk_id = e.chunk_id
            WHERE e.profile_id = %s
              AND e.build_id = %s::uuid
              AND e.collection_name = %s
              {scope_sql}
              AND vector_dims(e.embedding) = {dimension}
              {where_sql}
            ORDER BY score DESC
            LIMIT %s
        """
        return conn.execute(
            sql,
            (
                vector,
                *identity_params,
                *filter_params,
                int(top_k),
            ),
        ).fetchall()

    def _dense_retrieval_unit_rows(
        self,
        conn,
        *,
        vector: str,
        dimension: int,
        top_k: int,
        where_sql: str,
        filter_params: list[Any],
        profile_id: str,
        build_id: str,
        use_ann: bool | None = None,
        collection_name: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        resolved_collection = collection_name or self.collection_name
        scope_sql = "AND e.scope = %s" if scope else ""
        identity_params: tuple[Any, ...] = (
            profile_id,
            build_id,
            resolved_collection,
            *((str(scope),) if scope else ()),
        )
        ann_enabled = (
            self.settings.postgres_create_hnsw_index
            if use_ann is None
            else bool(use_ann)
        )
        if ann_enabled and scope and dimension <= 4000:
            vector_type = "halfvec" if dimension > 2000 else "vector"
            distance_expr = (
                f"e.embedding::{vector_type}({dimension}) "
                f"<=> %s::{vector_type}({dimension})"
            )
            rows = conn.execute(
                f"""
                SELECT
                    c.chunk_id, c.source_id, c.title, c.scope, c.text,
                    c.metadata || jsonb_build_object(
                        'search_text', u.search_text,
                        'retrieval_unit', jsonb_build_object(
                            'unit_id', u.unit_id,
                            'unit_type', u.unit_type,
                            'unit_index', u.unit_index,
                            'text', u.text,
                            'metadata', u.metadata
                        )
                    ) AS metadata,
                    1 - ({distance_expr}) AS score
                FROM kb_retrieval_unit_embeddings e
                JOIN kb_retrieval_units u ON u.unit_id = e.unit_id
                JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
                WHERE e.profile_id = %s
                  AND e.build_id = %s::uuid
                  AND e.collection_name = %s
                  {scope_sql}
                  AND vector_dims(e.embedding) = {dimension}
                  {where_sql}
                ORDER BY e.embedding::{vector_type}({dimension})
                         <=> %s::{vector_type}({dimension})
                LIMIT %s
                """,
                (
                    vector,
                    *identity_params,
                    *filter_params,
                    vector,
                    self._retrieval_unit_overfetch_limit(top_k),
                ),
            ).fetchall()
            return self._diversify_retrieval_unit_rows(
                rows,
                top_k=top_k,
            )

        rows = conn.execute(
            f"""
            SELECT
                c.chunk_id, c.source_id, c.title, c.scope, c.text,
                c.metadata || jsonb_build_object(
                    'search_text', u.search_text,
                    'retrieval_unit', jsonb_build_object(
                        'unit_id', u.unit_id,
                        'unit_type', u.unit_type,
                        'unit_index', u.unit_index,
                        'text', u.text,
                        'metadata', u.metadata
                    )
                ) AS metadata,
                1 - (e.embedding <=> %s::vector) AS score
            FROM kb_retrieval_unit_embeddings e
            JOIN kb_retrieval_units u ON u.unit_id = e.unit_id
            JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
            WHERE e.profile_id = %s
              AND e.build_id = %s::uuid
              AND e.collection_name = %s
              {scope_sql}
              AND vector_dims(e.embedding) = {dimension}
              {where_sql}
            ORDER BY score DESC
            LIMIT %s
            """,
            (
                vector,
                *identity_params,
                *filter_params,
                self._retrieval_unit_overfetch_limit(top_k),
            ),
        ).fetchall()
        return self._diversify_retrieval_unit_rows(
            rows,
            top_k=top_k,
        )

    def _lexical_rows(
        self,
        conn,
        *,
        query: str,
        scope: str,
        top_k: int,
        where_sql: str,
        filter_params: list[Any],
        collection_name: str | None = None,
    ) -> list[dict[str, Any]]:
        resolved_collection = collection_name or self.collection_name
        index_name = self.repo.bm25_index_name(resolved_collection, scope)
        tokenizer = str(self.settings.postgres_bm25_tokenizer_name)
        distance_expr = (
            "bm25_embedding <&> bm25_catalog.to_bm25query("
            f"'public.{index_name}'::regclass, tokenizer_catalog.tokenize(%s, %s))"
        )
        sql = f"""
            SELECT
                c.chunk_id, c.source_id, c.title, c.scope, c.text, c.metadata,
                -({distance_expr}) AS score
            FROM kb_chunks c
            WHERE c.collection_name = %s
            AND c.bm25_embedding IS NOT NULL
            {where_sql}
            ORDER BY {distance_expr}
            LIMIT %s
        """
        return conn.execute(
            sql,
            (
                query,
                tokenizer,
                resolved_collection,
                *filter_params,
                query,
                tokenizer,
                int(top_k),
            ),
        ).fetchall()

    def _lexical_retrieval_unit_rows(
        self,
        conn,
        *,
        query: str,
        scope: str,
        top_k: int,
        where_sql: str,
        filter_params: list[Any],
        collection_name: str | None = None,
    ) -> list[dict[str, Any]]:
        resolved_collection = collection_name or self.collection_name
        index_name = self.repo.retrieval_unit_bm25_index_name()
        tokenizer = self.repo.retrieval_unit_bm25_tokenizer_name()
        distance_expr = (
            "u.bm25_embedding <&> bm25_catalog.to_bm25query("
            f"'public.{index_name}'::regclass, "
            "tokenizer_catalog.tokenize(%s, %s))"
        )
        rows = conn.execute(
            f"""
            SELECT
                c.chunk_id, c.source_id, c.title, c.scope, c.text,
                c.metadata || jsonb_build_object(
                    'search_text', u.search_text,
                    'retrieval_unit', jsonb_build_object(
                        'unit_id', u.unit_id,
                        'unit_type', u.unit_type,
                        'unit_index', u.unit_index,
                        'text', u.text,
                        'metadata', u.metadata
                    )
                ) AS metadata,
                -({distance_expr}) AS score
            FROM kb_retrieval_units u
            JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
            WHERE u.collection_name = %s
              AND u.scope = %s
              AND u.bm25_embedding IS NOT NULL
              {where_sql}
            ORDER BY {distance_expr}
            LIMIT %s
            """,
            (
                query,
                tokenizer,
                resolved_collection,
                scope,
                *filter_params,
                query,
                tokenizer,
                self._retrieval_unit_overfetch_limit(top_k),
            ),
        ).fetchall()
        return self._diversify_retrieval_unit_rows(
            rows,
            top_k=top_k,
        )

    @staticmethod
    def _retrieval_unit_candidate_limit(top_k: int) -> int:
        resolved_top_k = max(1, int(top_k))
        return max(resolved_top_k + 8, resolved_top_k * 4)

    @classmethod
    def _retrieval_unit_overfetch_limit(cls, top_k: int) -> int:
        return cls._retrieval_unit_candidate_limit(top_k) * 4

    @classmethod
    def _diversify_retrieval_unit_rows(
        cls,
        rows: list[dict[str, Any]],
        *,
        top_k: int,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        selected_units: dict[str, list[dict[str, Any]]] = {}
        candidate_limit = cls._retrieval_unit_candidate_limit(top_k)
        for row in rows:
            chunk_id = str(row.get("chunk_id") or "")
            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            row_units = retrieval_units_from_metadata(dict(metadata))
            if not chunk_id or not row_units:
                continue
            unit = row_units[0]
            parent_units = selected_units.setdefault(chunk_id, [])
            if len(parent_units) >= MAX_RETRIEVAL_UNITS_PER_PARENT:
                continue
            distinct = select_distinct_retrieval_units(
                [*parent_units, unit],
                limit=MAX_RETRIEVAL_UNITS_PER_PARENT,
            )
            if len(distinct) == len(parent_units):
                continue
            parent_units.append(unit)
            selected.append(row)
            if len(selected) >= candidate_limit:
                break
        return selected

    def _retrieval_unit_lexical_available(self) -> bool:
        return callable(
            getattr(
                self.repo,
                "retrieval_unit_bm25_index_name",
                None,
            )
        ) and callable(
            getattr(
                self.repo,
                "retrieval_unit_bm25_tokenizer_name",
                None,
            )
        )

    @staticmethod
    def _collapse_parent_rows(
        rows: list[dict[str, Any]],
        *,
        top_k: int,
    ) -> list[dict[str, Any]]:
        best: dict[str, tuple[tuple[float, int], dict[str, Any]]] = {}
        first_seen: dict[str, int] = {}
        unit_candidates: dict[str, dict[str, dict[str, Any]]] = {}
        unit_position = 0
        for position, row in enumerate(rows):
            chunk_id = str(row["chunk_id"])
            first_seen.setdefault(chunk_id, position)
            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            metadata = dict(metadata)
            row_units = retrieval_units_from_metadata(metadata)
            has_unit = int(bool(row_units))
            row_score = float(row.get("score") or 0.0)
            rank_key = (row_score, has_unit)
            current = best.get(chunk_id)
            if current is None or rank_key > current[0]:
                best[chunk_id] = (rank_key, row)

            for unit in row_units:
                unit_position += 1
                try:
                    retrieval_rank = max(
                        1,
                        int(unit.get("retrieval_rank") or unit_position),
                    )
                except (TypeError, ValueError):
                    retrieval_rank = unit_position
                unit_id = str(unit["unit_id"])
                candidate = {
                    "score": row_score,
                    "rank": retrieval_rank,
                    "position": position,
                    "unit": {**unit, "retrieval_rank": retrieval_rank},
                }
                current_unit = unit_candidates.setdefault(chunk_id, {}).get(
                    unit_id
                )
                candidate_key = (
                    row_score,
                    -retrieval_rank,
                    -position,
                )
                current_key = (
                    (
                        float(current_unit["score"]),
                        -int(current_unit["rank"]),
                        -int(current_unit["position"]),
                    )
                    if current_unit
                    else None
                )
                if current_key is None or candidate_key > current_key:
                    unit_candidates[chunk_id][unit_id] = candidate

        output: list[dict[str, Any]] = []
        for chunk_id, (_, selected_row) in best.items():
            row = dict(selected_row)
            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            metadata = dict(metadata)
            candidates = sorted(
                unit_candidates.get(chunk_id, {}).values(),
                key=lambda candidate: (
                    -float(candidate["score"]),
                    int(candidate["rank"]),
                    int(candidate["position"]),
                    str(candidate["unit"]["unit_id"]),
                ),
            )
            selected_units = select_distinct_retrieval_units(
                [
                    dict(candidate["unit"])
                    for candidate in candidates
                ],
                limit=MAX_RETRIEVAL_UNITS_PER_PARENT,
            )
            if selected_units:
                metadata["retrieval_units"] = selected_units
                metadata["retrieval_unit"] = {
                    key: value
                    for key, value in selected_units[0].items()
                    if key not in {"retrieval_rank", "search_text"}
                }
                search_texts = list(
                    dict.fromkeys(
                        str(unit.get("search_text") or "").strip()
                        for unit in selected_units
                        if str(unit.get("search_text") or "").strip()
                    )
                )
                if search_texts:
                    metadata["search_text"] = "\n\n".join(search_texts)
            row["metadata"] = metadata
            output.append(row)
        output.sort(
            key=lambda row: (
                -float(row.get("score") or 0.0),
                first_seen[str(row["chunk_id"])],
            )
        )
        return output[: max(1, int(top_k))]

    def _where_from_filter(
        self,
        metadata_filter: dict,
        *,
        table_alias: str | None = None,
    ) -> tuple[str, list[Any]]:
        if table_alias not in {None, "", "c"}:
            raise ValueError("Unsupported metadata filter table alias.")
        prefix = f"{table_alias}." if table_alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        direct_columns = {
            "scope": f"{prefix}scope",
            "doc_id": f"{prefix}doc_id",
            "source_id": f"{prefix}source_id",
            "title": f"{prefix}title",
            "index_version": f"{prefix}index_version",
            "content_hash": f"{prefix}content_hash",
        }
        metadata_column = f"{prefix}metadata"
        for key, expected in (metadata_filter or {}).items():
            if key in {"collection", "collections", "collection_name"}:
                continue
            column = direct_columns.get(str(key))
            if column:
                if isinstance(expected, list):
                    clauses.append(f"{column} = ANY(%s)")
                    params.append([str(x) for x in expected])
                else:
                    clauses.append(f"{column} = %s")
                    params.append(str(expected))
                continue
            if isinstance(expected, list):
                if not expected:
                    clauses.append("FALSE")
                    continue
                alternatives = []
                for value in expected:
                    alternatives.append(
                        f"{metadata_column} @> %s::jsonb"
                    )
                    params.append(
                        json.dumps({str(key): value}, ensure_ascii=False)
                    )
                clauses.append("(" + " OR ".join(alternatives) + ")")
            elif isinstance(expected, (str, int, float, bool)):
                clauses.append(f"{metadata_column} @> %s::jsonb")
                params.append(
                    json.dumps({str(key): expected}, ensure_ascii=False)
                )
        if not clauses:
            return "", []
        return " AND " + " AND ".join(f"({c})" for c in clauses), params

    @staticmethod
    def _to_metadata(chunk: RetrievedChunk) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "chunk_id": chunk.chunk_id,
            "source_id": chunk.source_id,
            "title": chunk.title,
            "scope": chunk.scope,
        }
        metadata.update(
            {
                key: value
                for key, value in (chunk.metadata or {}).items()
                if key not in PGVectorClient._INDEX_ONLY_METADATA_KEYS
            }
        )
        return metadata

    @staticmethod
    def _row_to_chunk(row: dict[str, Any]) -> RetrievedChunk:
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return RetrievedChunk(
            chunk_id=str(row["chunk_id"]),
            source_id=str(row["source_id"]),
            title=str(row["title"]),
            scope=str(row["scope"]),
            text=str(row["text"]),
            score=float(row.get("score") or 0.0),
            metadata={
                key: value
                for key, value in dict(metadata).items()
                if key not in {"chunk_id", "source_id", "title", "scope"}
            },
        )

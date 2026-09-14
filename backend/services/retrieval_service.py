from __future__ import annotations

import asyncio
from contextlib import (
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
    nullcontext,
)
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial, wraps
import hashlib
import inspect
from itertools import combinations
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, AsyncIterator
import weakref

from jinja2 import Template

from backend.business_config.loader import get_config_loader
from backend.config.settings import get_settings
from backend.observability.metrics import elapsed_ms, log_metric, now_ms
from backend.rag.parent_context import (
    DEFAULT_GENERATION_FOCUS_MAX_RERANKED_UNITS,
    select_generation_context_focus,
    select_job_level_generation_context,
    select_parent_context,
)
from backend.rag.reranker import Reranker
from backend.rag.structured_facts import (
    contextual_search_text,
    exact_query_terms,
    normalize_content,
)
from backend.redis.retrieval_cache import RetrievalResultCache
from backend.redis.distributed_coordination import (
    RedisDistributedCoordinator,
    get_distributed_coordinator,
)
from backend.repositories.postgres_repository import (
    EmbeddingProfileState,
    PostgresRepository,
    RetrievalScopeCapability,
)
from backend.repositories.vector_index_repository import VectorIndexRepository
from backend.schemas.retrieval import RetrievedChunk
from backend.services.embedding_service import EmbeddingService
from backend.vectorstore.embedding_profile import embedding_profile_id
from backend.services.knowledge_skill_service import (
    ActiveKnowledgeSkill,
    KnowledgeSkillRouter,
    get_knowledge_skill_router,
)
from backend.vectorstore.pgvector_client import (
    HybridScopeSearchRequest,
    HybridSearchResult,
    MAX_RETRIEVAL_UNITS_PER_PARENT,
    PGVectorClient,
    retrieval_units_from_metadata,
    select_distinct_retrieval_units,
)


_REQUIRED_CONFIGURED_SEARCH_SCOPES = frozenset({"organization_unit"})
_DEFAULT_RERANK_UNITS_PER_PARENT = 5
_MIN_RERANK_UNITS_PER_PARENT = 2
_MAX_RERANK_UNITS_PER_PARENT = 5
_DEFAULT_RERANK_SEMANTIC_GROUP_CANDIDATE_ENABLED = False
_DEFAULT_RERANK_COMPOUND_EVIDENCE_SETS: tuple[tuple[str, ...], ...] = ()
_MAX_RERANK_COMPOUND_LEAVES = 3
_MAX_RERANK_COMPOUND_CANDIDATES_PER_PARENT = 1
_MAX_RERANK_COMPOUND_CHARS = 1200
_DEFAULT_RERANK_RANK_FUSION_K = 60
_DEFAULT_RERANK_RANK_WEIGHT = 2.0
_DEFAULT_FIRST_STAGE_RANK_WEIGHT = 1.0
_SCOPE_SELECTION_POLICY_VERSION = "evidence-diversity-v12"
_DEFAULT_DIVERSE_PREFIX_K = 3
_JOB_LEVEL_CODE_PATTERN = re.compile(
    r"^(?:SL\d+(?:G\d+)?|G\d+|P\d+|M\d+)$",
    re.IGNORECASE,
)
_APPLICABLE_JOB_LEVEL_PATTERN = re.compile(
    r"(?:适用(?:当前|下一)?职级|applicable\s+(?:current\s+|next\s+)?level)"
    r"\s*[：:]\s*(SL\d+(?:G\d+)?|G\d+|P\d+|M\d+)",
    re.IGNORECASE,
)
_LOOSE_JOB_LEVEL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(SL|G|P|M)\s*[-‐‑–—]?\s*(\d+)",
    re.IGNORECASE,
)
_PRIMARY_JOB_LEVEL_PATTERN = re.compile(
    r"(?im)^\s*(?:职级|岗位职级|job\s*level|career\s*level|grade)"
    r"\s*[：:]\s*(SL\d+(?:G\d+)?|G\d+|P\d+|M\d+)\b"
    r"|^\s*\|\s*(SL\d+(?:G\d+)?|G\d+|P\d+|M\d+)\s*\|",
    re.IGNORECASE,
)
_NEXT_JOB_LEVEL = {
    "G6": "G7",
    "G7": "G8",
    "G8": "G9",
    "G9": "SL1",
    "SL1": "SL2",
    "SL2": "SL3",
    "SL3": "SL4",
}
_PLAN_RETRIEVAL_AGENTS = {
    "guidance_plan",
    "development_plan_evaluation",
}
_REQUIRED_SCOPE_FALLBACK_QUERY_TEMPLATES = {
    "organization_unit": (
        "{{ profile.department if profile and profile.department else '' }} "
        "{{ profile.reporting_line if profile and profile.reporting_line else '' }} "
        "{{ profile.role if profile and profile.role else '' }} "
        "{{ profile.level if profile and profile.level else '' }} "
        "组织单元 组织路径 上级组织 部门职责 业务范围 核心任务 协作关系"
    ),
}


def _embedding_generation_guard(method):
    """Keep one retrieval request on a single embedding generation."""

    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def async_wrapper(self, *args, **kwargs):
            await self._abegin_embedding_generation_use()
            try:
                return await method(self, *args, **kwargs)
            finally:
                self._end_embedding_generation_use()

        return async_wrapper

    @wraps(method)
    def sync_wrapper(self, *args, **kwargs):
        self._begin_embedding_generation_use()
        try:
            return method(self, *args, **kwargs)
        finally:
            self._end_embedding_generation_use()

    return sync_wrapper


@dataclass(slots=True)
class _RenderedQuerySpec:
    query: str
    scopes: list[str]
    weight: float
    template_index: int


@dataclass(frozen=True, slots=True)
class _ScopeSearchPolicy:
    dense_mode: str
    dense_top_k: int
    lexical_top_k: int
    exact_top_k: int
    hybrid_enabled: bool
    exact_enabled: bool

    def cache_identity(self) -> dict[str, object]:
        return {
            "dense_mode": self.dense_mode,
            "dense_top_k": self.dense_top_k,
            "lexical_top_k": self.lexical_top_k,
            "exact_top_k": self.exact_top_k,
            "hybrid_search_enabled": self.hybrid_enabled,
            "exact_search_enabled": self.exact_enabled,
        }


@dataclass(slots=True)
class _RankedBranch:
    branch_id: str
    modality: str
    scope: str
    weight: float
    chunks: list[RetrievedChunk]


@dataclass(slots=True)
class _SearchJobResult:
    branches: list[_RankedBranch]
    lexical_error: str | None
    semaphore_wait_ms: float
    lexical_executed: int = 0
    active_count: int = 0
    lexical_error_count: int = 0


@dataclass(slots=True)
class _NeighborFetchResult:
    chunks: list[RetrievedChunk]
    semaphore_wait_ms: float
    active_count: int


@dataclass(slots=True)
class _RetrievalPlan:
    agent_name: str
    query_specs: list[_RenderedQuerySpec]
    rerank_query: str
    rerank_instruction: str | None
    dense_top_k: int
    lexical_top_k: int
    rrf_k: int
    fusion_top_n: int
    rerank_candidate_top_n: int
    rerank_units_per_parent: int
    rerank_semantic_group_candidate_enabled: bool
    rerank_rank_fusion_k: int
    rerank_rank_weight: float
    first_stage_rank_weight: float
    final_top_k: int
    dense_weight: float
    lexical_weight: float
    hybrid_enabled: bool
    rerank_enabled: bool
    query_parallelism: int
    metadata_filter: dict[str, Any]
    cache_key: str
    rerank_compound_evidence_sets: tuple[tuple[str, ...], ...] = ()
    scope_search_policies: dict[str, _ScopeSearchPolicy] = field(
        default_factory=dict
    )
    exact_top_k: int = 16
    exact_weight: float = 1.25
    exact_enabled: bool = True
    citation_scope_minimums: dict[str, int] = field(default_factory=dict)
    citation_scope_preferred_terms: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    generation_prefix_scope_targets: dict[str, int] = field(
        default_factory=dict
    )
    generation_prefix_scope_semantic_groups: dict[
        str,
        tuple["_GenerationPrefixSemanticGroup", ...],
    ] = field(default_factory=dict)
    generation_prefix_fill_scopes: tuple[str, ...] = ()
    active_skills: list[ActiveKnowledgeSkill] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _GenerationPrefixSemanticGroup:
    """One required concept family and optional within-family quality signals."""

    required_terms: tuple[str, ...]
    preferred_terms: tuple[str, ...] = ()
    downrank_terms: tuple[str, ...] = ()

    def cache_identity(self) -> dict[str, list[str]]:
        return {
            "required_terms": list(self.required_terms),
            "preferred_terms": list(self.preferred_terms),
            "downrank_terms": list(self.downrank_terms),
        }


class RetrievalService:
    """Coach / Guidance retrieval backed by hybrid PostgreSQL search and reranking."""

    _search_limiters: dict[int, threading.BoundedSemaphore] = {}
    _search_limiters_lock = threading.Lock()
    _rerank_limiters: weakref.WeakKeyDictionary[
        asyncio.AbstractEventLoop,
        dict[int, asyncio.BoundedSemaphore],
    ] = weakref.WeakKeyDictionary()
    _rerank_limiters_lock = threading.Lock()
    _store_cache_init_lock = threading.Lock()
    _activity_lock = threading.Lock()
    _active_searches = 0
    _active_reranks = 0

    def __init__(
        self,
        *,
        executor: Executor | None = None,
        capabilities: dict[
            tuple[str, str],
            RetrievalScopeCapability,
        ] | None = None,
        result_cache: RetrievalResultCache | None = None,
        embedding_service: EmbeddingService | None = None,
        reranker: Reranker | None = None,
        knowledge_skill_router: KnowledgeSkillRouter | None = None,
        coordinator: RedisDistributedCoordinator | None = None,
        repository: PostgresRepository | None = None,
        embedding_profile: EmbeddingProfileState | None = None,
    ):
        self.loader = get_config_loader()
        self.settings = get_settings()
        self.reranker = reranker or Reranker()
        self.embedding_service = embedding_service or EmbeddingService()
        self.vector_repo = VectorIndexRepository()
        self.coordinator = coordinator or get_distributed_coordinator(self.settings)
        self.repository = repository or PostgresRepository(
            initialize=False,
        )
        self._embedding_generation_condition = threading.Condition()
        self._embedding_active_retrievals = 0
        self._embedding_profile_switch_pending = False
        self._embedding_runtime_state = (embedding_profile, capabilities)
        self.embedding_profile = embedding_profile
        self.result_cache = result_cache or RetrievalResultCache(
            self.settings,
            coordinator=self.coordinator,
        )
        self.knowledge_skill_router = (
            knowledge_skill_router or get_knowledge_skill_router()
        )
        self._capabilities = capabilities
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=self.settings.rag_thread_pool_max_workers,
            thread_name_prefix="rag-db",
        )
        self._stores: dict[str, PGVectorClient] = {}
        self._stores_lock = threading.Lock()
        self._search_limiter = self._get_search_limiter(
            self.settings.rag_db_search_max_concurrency
        )

    async def shutdown(self) -> None:
        await self.result_cache.aclose()
        shutdown_embedding = getattr(self.embedding_service, "shutdown", None)
        if callable(shutdown_embedding):
            await shutdown_embedding()
        else:
            await self.embedding_service.query_cache.aclose()
        if self._owns_executor and isinstance(self._executor, ThreadPoolExecutor):
            self._executor.shutdown(wait=True, cancel_futures=True)

    def set_embedding_profile(
        self,
        profile: EmbeddingProfileState,
        capabilities: dict[
            tuple[str, str],
            RetrievalScopeCapability,
        ],
    ) -> bool:
        """Atomically publish a verified active embedding build.

        New searches use a fresh ``PGVectorClient`` created from the new
        profile. Clients already held by in-flight searches remain valid and
        are allowed to finish against their original build.
        """

        if not profile.ready or not profile.active_build_id:
            raise RuntimeError(
                "Cannot activate an embedding profile that is not ready: "
                f"profile_id={profile.profile_id}, status={profile.status}, "
                f"reason={profile.stale_reason}."
            )
        resolved_capabilities = capabilities
        with self._embedding_profile_switch():
            current_profile, _ = self._embedding_runtime_snapshot()
            if current_profile is not None:
                if profile.profile_id != current_profile.profile_id:
                    raise ValueError(
                        "Embedding profile refresh cannot change profile id: "
                        f"current={current_profile.profile_id}, "
                        f"candidate={profile.profile_id}."
                    )
                if int(profile.dimensions) != int(current_profile.dimensions):
                    raise ValueError(
                        "Embedding profile refresh cannot change dimensions: "
                        f"current={current_profile.dimensions}, "
                        f"candidate={profile.dimensions}."
                    )

            build_changed = bool(
                current_profile is None
                or profile.active_build_id
                != current_profile.active_build_id
            )
            # This tuple is the read-side synchronization point. Assigning one
            # immutable tuple keeps profile identity and capabilities coherent
            # for concurrent retrieval requests.
            self._embedding_runtime_state = (
                profile,
                resolved_capabilities,
            )
            # Retain these attributes for diagnostics and existing callers.
            self.embedding_profile = profile
            self._capabilities = resolved_capabilities
            if build_changed:
                with self._stores_lock:
                    self._stores = {}
            return build_changed

    def _embedding_runtime_snapshot(
        self,
    ) -> tuple[
        EmbeddingProfileState | None,
        dict[tuple[str, str], RetrievalScopeCapability] | None,
    ]:
        state = getattr(self, "_embedding_runtime_state", None)
        if state is not None:
            return state
        return (
            getattr(self, "embedding_profile", None),
            getattr(self, "_capabilities", None),
        )

    @contextmanager
    def _embedding_profile_switch(self):
        """Wait for readers and prevent a mixed-generation retrieval."""

        condition = getattr(self, "_embedding_generation_condition", None)
        if condition is None:
            # Compatibility for focused tests and lightweight service doubles
            # created without calling ``__init__``.
            lock = getattr(self, "_embedding_profile_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._embedding_profile_lock = lock
            with lock:
                yield
            return
        with condition:
            while self._embedding_profile_switch_pending:
                condition.wait()
            self._embedding_profile_switch_pending = True
            try:
                while self._embedding_active_retrievals:
                    condition.wait()
                yield
            finally:
                self._embedding_profile_switch_pending = False
                condition.notify_all()

    def _embedding_generation_condition_for_use(self) -> threading.Condition:
        condition = getattr(self, "_embedding_generation_condition", None)
        if condition is not None:
            return condition
        with self._store_cache_init_lock:
            condition = getattr(self, "_embedding_generation_condition", None)
            if condition is None:
                condition = threading.Condition()
                self._embedding_generation_condition = condition
                self._embedding_active_retrievals = 0
                self._embedding_profile_switch_pending = False
        return condition

    def _begin_embedding_generation_use(self) -> None:
        condition = self._embedding_generation_condition_for_use()
        with condition:
            while self._embedding_profile_switch_pending:
                condition.wait()
            self._embedding_active_retrievals += 1

    async def _abegin_embedding_generation_use(self) -> None:
        condition = self._embedding_generation_condition_for_use()
        while True:
            with condition:
                if not self._embedding_profile_switch_pending:
                    self._embedding_active_retrievals += 1
                    return
            # A synchronous wait here would block the event loop while the
            # writer is waiting for older async readers to finish.
            await asyncio.sleep(0.01)

    def _end_embedding_generation_use(self) -> None:
        condition = self._embedding_generation_condition_for_use()
        with condition:
            if self._embedding_active_retrievals <= 0:
                raise RuntimeError("Embedding generation lease is unbalanced.")
            self._embedding_active_retrievals -= 1
            if self._embedding_active_retrievals == 0:
                condition.notify_all()

    @asynccontextmanager
    async def _global_lease(
        self,
        resource: str,
        capacity_setting: str,
        fallback: int,
        *,
        priority: int = 0,
    ) -> AsyncIterator[None]:
        coordinator = getattr(self, "coordinator", None)
        settings = getattr(self, "settings", None)
        capacity = int(getattr(settings, capacity_setting, fallback))
        if coordinator is None or capacity <= 0:
            yield
            return
        async with coordinator.lease(
            resource,
            capacity=capacity,
            priority=max(0, int(priority)),
        ):
            yield

    @staticmethod
    def _retrieval_admission_priority(context: dict[str, Any]) -> int:
        try:
            return max(
                0,
                min(100, int(context.get("_retrieval_admission_priority", 0))),
            )
        except (TypeError, ValueError):
            return 0

    def configured_scopes(self, agent_name: str) -> list[str]:
        """Return the declared scope boundary for one configured retrieval task."""

        query_config = self._query_config()
        defaults = query_config.get("defaults") or {}
        all_queries = (
            query_config.get("queries")
            or query_config.get("agent_queries")
            or {}
        )
        agent_cfg = all_queries.get(agent_name)
        if not isinstance(agent_cfg, dict):
            raise ValueError(
                f"Missing retrieval query config for agent: {agent_name}"
            )
        return self._normalize_scopes(
            agent_cfg.get("scopes") or defaults.get("scopes") or []
        )

    def agentic_search_config(self) -> dict[str, Any]:
        config = self._query_config().get("agentic_search") or {}
        if not isinstance(config, dict):
            raise ValueError("query.yaml agentic_search must be an object")
        return dict(config)

    def retrieval_config_identity(self, agent_name: str) -> dict[str, Any]:
        query_config = self._query_config()
        defaults = query_config.get("defaults") or {}
        all_queries = (
            query_config.get("queries")
            or query_config.get("agent_queries")
            or {}
        )
        agent_cfg = all_queries.get(agent_name)
        if not isinstance(agent_cfg, dict):
            raise ValueError(
                f"Missing retrieval query config for agent: {agent_name}"
            )
        profile_id, build_id = self._embedding_profile_cache_identity()
        return {
            "version": query_config.get("version"),
            "embedding_profile_id": profile_id,
            "embedding_build_id": build_id,
            "defaults": defaults,
            "query": agent_cfg,
            "scope_search_policies": query_config.get(
                "scope_search_policies"
            )
            or {},
        }

    async def aretrieve_neighbors(
        self,
        chunks: list[RetrievedChunk],
        *,
        window: int = 1,
        admission_priority: int = 0,
    ) -> list[RetrievedChunk]:
        """Fetch adjacent chunks without changing the primary retrieval ranking."""

        neighbor_window = max(0, int(window))
        if not chunks or neighbor_window == 0:
            return []
        grouped: dict[str, list[RetrievedChunk]] = {}
        for chunk in chunks:
            metadata = chunk.metadata or {}
            collection_name = str(
                metadata.get("collection_name")
                or self.settings.collection_name_for_scope(chunk.scope)
            )
            grouped.setdefault(collection_name, []).append(chunk)

        loop = asyncio.get_running_loop()
        async def fetch_group(
            collection_name: str,
            anchors: list[RetrievedChunk],
        ) -> _NeighborFetchResult:
            async with self._global_lease(
                "rag-db",
                "rag_global_db_search_max_concurrency",
                int(getattr(self.settings, "rag_db_search_max_concurrency", 1)),
                priority=admission_priority,
            ):
                return await loop.run_in_executor(
                    self._executor,
                    partial(
                        self._fetch_neighbor_group,
                        collection_name,
                        anchors,
                        neighbor_window,
                    ),
                )

        results = await asyncio.gather(
            *(
                fetch_group(collection_name, anchors)
                for collection_name, anchors in grouped.items()
            )
        )
        merged: dict[str, RetrievedChunk] = {}
        total_wait_ms = 0.0
        active_peak = 0
        for result in results:
            total_wait_ms += result.semaphore_wait_ms
            active_peak = max(active_peak, result.active_count)
            for chunk in result.chunks:
                current = merged.get(chunk.chunk_id)
                if current is None:
                    merged[chunk.chunk_id] = chunk
                    continue
                current_neighbor = dict(
                    (current.metadata or {}).get("agentic_neighbor") or {}
                )
                incoming_neighbor = dict(
                    (chunk.metadata or {}).get("agentic_neighbor") or {}
                )
                parent_ids = list(
                    dict.fromkeys(
                        [
                            *list(current_neighbor.get("parent_chunk_ids") or []),
                            *list(incoming_neighbor.get("parent_chunk_ids") or []),
                        ]
                    )
                )
                current.metadata = {
                    **(current.metadata or {}),
                    "agentic_neighbor": {
                        **current_neighbor,
                        "parent_chunk_ids": parent_ids,
                        "distance": min(
                            int(current_neighbor.get("distance") or neighbor_window),
                            int(incoming_neighbor.get("distance") or neighbor_window),
                        ),
                    },
                }
        output = list(merged.values())
        log_metric(
            "rag.agentic.neighbors",
            anchor_count=len(chunks),
            neighbor_window=neighbor_window,
            neighbor_count=len(output),
            rag_db_semaphore_wait_ms=round(total_wait_ms, 2),
            rag_db_active_peak=active_peak,
        )
        return output

    async def arerank_candidates(
        self,
        chunks: list[RetrievedChunk],
        *,
        query: str,
        top_k: int,
        agent_name: str = "agentic_search",
        admission_priority: int = 0,
    ) -> list[RetrievedChunk]:
        """Rerank supplemental evidence under the process-wide limiter."""

        if not chunks:
            return []
        if not query.strip():
            raise ValueError("Agentic evidence rerank query cannot be empty.")
        limiter = self._get_rerank_limiter()
        wait_started = time.perf_counter()
        await limiter.acquire()
        wait_ms = (time.perf_counter() - wait_started) * 1000
        active_count = 0
        started = now_ms()
        try:
            active_count = self._begin_rerank()
            async with self._global_lease(
                "reranker",
                "rag_global_rerank_max_concurrency",
                self.settings.rag_rerank_max_concurrency,
                priority=admission_priority,
            ):
                return await self.reranker.arerank(
                    chunks,
                    query=query,
                    top_k=max(1, min(int(top_k), len(chunks))),
                )
        finally:
            if active_count:
                self._end_rerank()
            limiter.release()
            log_metric(
                "rag.agentic.rerank",
                agent_name=agent_name,
                candidate_count=len(chunks),
                returned_count=min(max(1, int(top_k)), len(chunks)),
                rag_rerank_queue_ms=round(wait_ms, 2),
                rag_rerank_active_peak=active_count,
                rag_rerank_ms=elapsed_ms(started),
                rag_admission_priority=max(0, int(admission_priority)),
            )

    @_embedding_generation_guard
    async def aretrieve(
        self,
        agent_name: str,
        context: dict[str, Any],
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        """Native async retrieval used by request handlers."""

        started = now_ms()
        admission_priority = self._retrieval_admission_priority(context)
        plan = self._build_plan(agent_name, context, top_k)
        if plan is None:
            self._log_empty_result(
                started=started,
                agent_name=agent_name,
                rendered_query_count=0,
                rerank_enabled=False,
            )
            return []

        cached = await self.result_cache.aget(plan.cache_key)
        if cached is not None:
            if self._needs_parent_context(plan.agent_name):
                cached = await self._ahydrate_parent_contexts(
                    cached,
                    admission_priority=admission_priority,
                    applicable_job_levels=self._preferred_job_level_codes(
                        getattr(
                            plan,
                            "citation_scope_preferred_terms",
                            None,
                        )
                    ),
                )
            self._log_cache_hit(started, plan, cached)
            return cached

        async with self.result_cache.singleflight(plan.cache_key):
            cached = await self.result_cache.aget(plan.cache_key)
            if cached is not None:
                if self._needs_parent_context(plan.agent_name):
                    cached = await self._ahydrate_parent_contexts(
                        cached,
                        admission_priority=admission_priority,
                        applicable_job_levels=self._preferred_job_level_codes(
                            getattr(
                                plan,
                                "citation_scope_preferred_terms",
                                None,
                            )
                        ),
                    )
                self._log_cache_hit(started, plan, cached)
                return cached

            candidate_count = 0
            lexical_error_count = 0
            lexical_query_count = 0
            semaphore_wait_ms = 0.0
            embedding_hits = 0
            embedding_lookups = 0
            search_job_count = 0
            rerank_wait_ms = 0.0
            db_active_peak = 0
            rerank_active_peak = 0
            embedding_ms = 0
            db_ms = 0
            rrf_ms = 0
            rerank_ms = 0
            try:
                rendered_queries = [spec.query for spec in plan.query_specs]
                embedding_started = now_ms()
                query_embeddings, cache_hits = (
                    await self.embedding_service.aembed_queries(rendered_queries)
                )
                embedding_ms = elapsed_ms(embedding_started)
                if len(query_embeddings) != len(rendered_queries):
                    raise RuntimeError("RAG query embedding batch size mismatch.")
                embedding_lookups = len(cache_hits)
                embedding_hits = sum(cache_hits)
                query_vectors = [
                    PostgresRepository.vector_literal(embedding)
                    for embedding in query_embeddings
                ]

                search_jobs = [
                    (query_index, spec, embedding, vector)
                    for query_index, (spec, embedding, vector) in enumerate(
                        zip(
                            plan.query_specs,
                            query_embeddings,
                            query_vectors,
                            strict=True,
                        )
                    )
                ]
                search_job_count = sum(
                    len(spec.scopes) for spec in plan.query_specs
                )
                db_round_trip_count = len(search_jobs)
                request_limiter = asyncio.Semaphore(plan.query_parallelism)
                loop = asyncio.get_running_loop()

                async def run_search_job(job):
                    query_index, spec, embedding, vector = job
                    async with request_limiter:
                        async with self._global_lease(
                            "rag-db",
                            "rag_global_db_search_max_concurrency",
                            int(
                                getattr(
                                    self.settings,
                                    "rag_db_search_max_concurrency",
                                    1,
                                )
                            ),
                            priority=admission_priority,
                        ):
                            return await loop.run_in_executor(
                                self._executor,
                                partial(
                                    self._search_scopes,
                                    query_index=query_index,
                                    query=spec.query,
                                    query_embedding=embedding,
                                    query_vector_literal=vector,
                                    scopes=spec.scopes,
                                    dense_top_k=plan.dense_top_k,
                                    lexical_top_k=plan.lexical_top_k,
                                    metadata_filter=plan.metadata_filter,
                                    template_weight=spec.weight,
                                    dense_weight=plan.dense_weight,
                                    lexical_weight=plan.lexical_weight,
                                    hybrid_enabled=plan.hybrid_enabled,
                                    exact_top_k=plan.exact_top_k,
                                    exact_weight=plan.exact_weight,
                                    exact_enabled=plan.exact_enabled,
                                    scope_search_policies=(
                                        plan.scope_search_policies
                                    ),
                                ),
                            )

                db_started = now_ms()
                job_results = await asyncio.gather(
                    *(run_search_job(job) for job in search_jobs)
                )
                db_ms = elapsed_ms(db_started)
                branches: list[_RankedBranch] = []
                for job_result in job_results:
                    branches.extend(job_result.branches)
                    semaphore_wait_ms += job_result.semaphore_wait_ms
                    db_active_peak = max(db_active_peak, job_result.active_count)
                    lexical_query_count += int(job_result.lexical_executed)
                    if job_result.lexical_error:
                        lexical_error_count += max(
                            1,
                            int(job_result.lexical_error_count),
                        )
                        log_metric(
                            "rag.lexical_search.error",
                            agent_name=agent_name,
                            error=job_result.lexical_error,
                        )

                candidate_count = sum(len(branch.chunks) for branch in branches)
                rrf_started = now_ms()
                fused_chunks = self._rrf_fuse(
                    branches,
                    rrf_k=plan.rrf_k,
                    top_n=plan.fusion_top_n,
                    scope_minimums=plan.citation_scope_minimums,
                    scope_preferred_terms=(
                        plan.citation_scope_preferred_terms
                    ),
                )
                rrf_ms = elapsed_ms(rrf_started)
                if not fused_chunks:
                    self._log_empty_result(
                        started=started,
                        agent_name=agent_name,
                        rendered_query_count=len(plan.query_specs),
                        rerank_enabled=plan.rerank_enabled,
                        search_job_count=search_job_count,
                        candidate_count=candidate_count,
                        embedding_hits=embedding_hits,
                        embedding_lookups=embedding_lookups,
                        query_parallelism=plan.query_parallelism,
                        hybrid_enabled=plan.hybrid_enabled,
                        lexical_error_count=lexical_error_count,
                        semaphore_wait_ms=semaphore_wait_ms,
                        active_skills=plan.active_skills,
                    )
                    return []

                query_for_rerank = plan.rerank_query
                if plan.rerank_enabled:
                    rerank_parent_candidates = (
                        self._select_with_scope_minimums(
                            fused_chunks,
                            top_k=max(
                                plan.final_top_k,
                                plan.rerank_candidate_top_n,
                            ),
                            scope_minimums=plan.citation_scope_minimums,
                            scope_preferred_terms=(
                                plan.citation_scope_preferred_terms
                            ),
                        )
                    )
                    rerank_candidates = (
                        self._expand_unit_rerank_candidates(
                            rerank_parent_candidates,
                            max_units_per_parent=(
                                plan.rerank_units_per_parent
                            ),
                            include_semantic_group_candidate=(
                                plan.rerank_semantic_group_candidate_enabled
                            ),
                            **(
                                {
                                    "compound_evidence_sets": (
                                        plan.rerank_compound_evidence_sets
                                    )
                                }
                                if plan.rerank_compound_evidence_sets
                                else {}
                            ),
                        )
                    )
                    limiter = self._get_rerank_limiter()
                    wait_started = time.perf_counter()
                    await limiter.acquire()
                    rerank_wait_ms = (
                        time.perf_counter() - wait_started
                    ) * 1000
                    try:
                        rerank_active_peak = self._begin_rerank()
                        rerank_started = now_ms()
                        async with self._global_lease(
                            "reranker",
                            "rag_global_rerank_max_concurrency",
                            self.settings.rag_rerank_max_concurrency,
                            priority=admission_priority,
                        ):
                            ranked_units = await self.reranker.arerank(
                                rerank_candidates,
                                query=query_for_rerank,
                                top_k=len(rerank_candidates),
                                instruction=plan.rerank_instruction,
                            )
                            ranked_result = (
                                self._aggregate_unit_rerank_results(
                                    rerank_parent_candidates,
                                    ranked_units,
                                    rank_fusion_k=(
                                        plan.rerank_rank_fusion_k
                                    ),
                                    rerank_rank_weight=(
                                        plan.rerank_rank_weight
                                    ),
                                    first_stage_rank_weight=(
                                        plan.first_stage_rank_weight
                                    ),
                                )
                            )
                        rerank_ms = elapsed_ms(rerank_started)
                    finally:
                        if rerank_active_peak:
                            self._end_rerank()
                        limiter.release()
                    baseline_result = ranked_result[: plan.final_top_k]
                    result = self._select_with_scope_minimums(
                        ranked_result,
                        top_k=plan.final_top_k,
                        scope_minimums=plan.citation_scope_minimums,
                        scope_preferred_terms=(
                            plan.citation_scope_preferred_terms
                        ),
                        diverse_prefix_k=min(
                            _DEFAULT_DIVERSE_PREFIX_K,
                            plan.final_top_k,
                        ),
                        generation_prefix_scope_targets=(
                            plan.generation_prefix_scope_targets
                        ),
                        generation_prefix_scope_semantic_groups=(
                            plan.generation_prefix_scope_semantic_groups
                        ),
                        generation_prefix_fill_scopes=(
                            plan.generation_prefix_fill_scopes
                        ),
                    )
                else:
                    rerank_parent_candidates = []
                    rerank_candidates = []
                    baseline_result = fused_chunks[: plan.final_top_k]
                    result = self._select_with_scope_minimums(
                        fused_chunks,
                        top_k=plan.final_top_k,
                        scope_minimums=plan.citation_scope_minimums,
                        scope_preferred_terms=(
                            plan.citation_scope_preferred_terms
                        ),
                        diverse_prefix_k=min(
                            _DEFAULT_DIVERSE_PREFIX_K,
                            plan.final_top_k,
                        ),
                        generation_prefix_scope_targets=(
                            plan.generation_prefix_scope_targets
                        ),
                        generation_prefix_scope_semantic_groups=(
                            plan.generation_prefix_scope_semantic_groups
                        ),
                        generation_prefix_fill_scopes=(
                            plan.generation_prefix_fill_scopes
                        ),
                    )

                original_top_ids = {
                    chunk.chunk_id for chunk in baseline_result
                }
                scope_replacement_count = sum(
                    chunk.chunk_id not in original_top_ids for chunk in result
                )
                if lexical_error_count == 0:
                    await self.result_cache.aset(plan.cache_key, result)
                if self._needs_parent_context(plan.agent_name):
                    result = await self._ahydrate_parent_contexts(
                        result,
                        admission_priority=admission_priority,
                        applicable_job_levels=self._preferred_job_level_codes(
                            getattr(
                                plan,
                                "citation_scope_preferred_terms",
                                None,
                            )
                        ),
                    )
                log_metric(
                    "rag.retrieve",
                    agent_name=agent_name,
                    rag_query_count=search_job_count,
                    rag_db_round_trip_count=db_round_trip_count,
                    rendered_query_count=len(plan.query_specs),
                    rag_dense_query_count=search_job_count,
                    rag_lexical_query_count=lexical_query_count,
                    rag_lexical_error_count=lexical_error_count,
                    rag_candidate_count=candidate_count,
                    rag_merged_count=len(fused_chunks),
                    rag_rerank_candidate_count=len(
                        rerank_parent_candidates
                    ),
                    rag_rerank_unit_candidate_count=len(
                        rerank_candidates
                    ),
                    rag_returned_count=len(result),
                    rag_cache_hit=False,
                    embedding_cache_hit=self._cache_hit_rate(
                        embedding_hits,
                        embedding_lookups,
                    ),
                    embedding_cache_lookup_count=embedding_lookups,
                    rerank_enabled=plan.rerank_enabled,
                    hybrid_search_enabled=plan.hybrid_enabled,
                    rag_query_parallelism=plan.query_parallelism,
                    rag_db_semaphore_wait_ms=round(semaphore_wait_ms, 2),
                    rag_db_active_peak=db_active_peak,
                    rag_rerank_queue_ms=round(rerank_wait_ms, 2),
                    rag_rerank_active_peak=rerank_active_peak,
                    rag_embedding_ms=embedding_ms,
                    rag_db_ms=db_ms,
                    rag_rrf_ms=rrf_ms,
                    rag_rerank_ms=rerank_ms,
                    rag_admission_priority=admission_priority,
                    rag_input_chars=sum(len(query) for query in rendered_queries),
                    rag_rerank_document_chars=(
                        self._rerank_document_chars(rerank_candidates)
                    ),
                    rag_citation_scope_minimums=plan.citation_scope_minimums,
                    rag_citation_scope_replacement_count=scope_replacement_count,
                    rag_ms=elapsed_ms(started),
                    **self._skill_metric_fields(plan.active_skills),
                )
                return result
            except Exception as exc:
                log_metric(
                    "rag.retrieve.error",
                    agent_name=agent_name,
                    rag_query_count=search_job_count,
                    rendered_query_count=len(plan.query_specs),
                    rag_candidate_count=candidate_count,
                    rag_cache_hit=False,
                    embedding_cache_hit=self._cache_hit_rate(
                        embedding_hits,
                        embedding_lookups,
                    ),
                    embedding_cache_lookup_count=embedding_lookups,
                    rerank_enabled=plan.rerank_enabled,
                    rag_query_parallelism=plan.query_parallelism,
                    rag_lexical_error_count=lexical_error_count,
                    rag_db_semaphore_wait_ms=round(semaphore_wait_ms, 2),
                    rag_db_active_peak=db_active_peak,
                    rag_rerank_active_peak=rerank_active_peak,
                    rag_embedding_ms=embedding_ms,
                    rag_db_ms=db_ms,
                    rag_rrf_ms=rrf_ms,
                    rag_rerank_ms=rerank_ms,
                    rag_error_count=1,
                    rag_ms=elapsed_ms(started),
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                    **self._skill_metric_fields(plan.active_skills),
                )
                raise

    def _build_plan(
        self,
        agent_name: str,
        context: dict[str, Any],
        top_k: int | None,
    ) -> _RetrievalPlan | None:
        query_config = self._query_config()
        defaults = query_config.get("defaults") or {}
        all_queries = (
            query_config.get("queries")
            or query_config.get("agent_queries")
            or {}
        )
        agent_cfg = all_queries.get(agent_name)
        if not isinstance(agent_cfg, dict) or agent_cfg.get("enabled", True) is False:
            raise ValueError(
                f"Missing or disabled retrieval query config for agent: {agent_name}"
            )
        templates = (
            agent_cfg.get("query_templates")
            or agent_cfg.get("queries")
            or defaults.get("query_templates")
            or defaults.get("queries")
            or []
        )
        if not isinstance(templates, list) or not templates:
            raise ValueError(f"No query templates configured for agent: {agent_name}")
        configured_scopes = self._normalize_scopes(
            agent_cfg.get("scopes") or defaults.get("scopes") or []
        )
        allowed_scopes = set(configured_scopes)
        active_skills = (
            []
            if context.get("_disable_knowledge_skills")
            else self.knowledge_skill_router.select(agent_name, context)
        )
        skill_templates: list[dict[str, object]] = []
        for raw_template in self.knowledge_skill_router.retrieval_templates(
            active_skills
        ):
            if not isinstance(raw_template, dict):
                continue
            scoped_template = dict(raw_template)
            skill_scopes = self._normalize_scopes(
                scoped_template.get("scopes") or []
            )
            intersected_scopes = [
                scope for scope in skill_scopes if scope in allowed_scopes
            ]
            if not intersected_scopes:
                continue
            scoped_template["scopes"] = intersected_scopes
            skill_templates.append(scoped_template)
        templates = [*templates, *skill_templates]
        declared_scopes = self._declared_scopes(templates, configured_scopes)
        if not declared_scopes:
            raise ValueError(f"No pgvector scopes configured for agent: {agent_name}")
        available_scopes = self._available_scopes(declared_scopes)
        available_scopes = self._retrieval_scopes_for_context(
            agent_name=agent_name,
            agent_cfg=agent_cfg,
            defaults=defaults,
            context=context,
            active_skills=active_skills,
            available_scopes=available_scopes,
        )
        if not available_scopes:
            return None
        query_specs = self._render_query_specs(
            templates=templates,
            context=context,
            fallback_scopes=configured_scopes,
            available_scopes=set(available_scopes),
        )
        if not query_specs:
            raise ValueError(f"Rendered retrieval query is empty for agent: {agent_name}")

        dense_top_k = self._config_int(
            agent_cfg,
            defaults,
            ("dense_top_k", "vector_top_k", "top_k"),
            18,
        )
        lexical_top_k = self._config_int(
            agent_cfg,
            defaults,
            ("lexical_top_k",),
            18,
        )
        rrf_k = self._config_int(agent_cfg, defaults, ("rrf_k",), 60)
        fusion_top_n = self._config_int(
            agent_cfg,
            defaults,
            ("fusion_top_n",),
            46,
        )
        rerank_candidate_top_n = self._config_int(
            agent_cfg,
            defaults,
            ("rerank_candidate_top_n",),
            32,
        )
        rerank_units_per_parent = min(
            _MAX_RERANK_UNITS_PER_PARENT,
            max(
                _MIN_RERANK_UNITS_PER_PARENT,
                self._config_int(
                    agent_cfg,
                    defaults,
                    ("rerank_units_per_parent",),
                    _DEFAULT_RERANK_UNITS_PER_PARENT,
                ),
            ),
        )
        rerank_semantic_group_candidate_enabled = self._config_bool(
            agent_cfg,
            defaults,
            ("rerank_semantic_group_candidate_enabled",),
            _DEFAULT_RERANK_SEMANTIC_GROUP_CANDIDATE_ENABLED,
        )
        rerank_compound_evidence_sets = (
            self._config_compound_evidence_sets(agent_cfg, defaults)
        )
        rerank_rank_fusion_k = self._config_int(
            agent_cfg,
            defaults,
            ("rerank_rank_fusion_k",),
            _DEFAULT_RERANK_RANK_FUSION_K,
        )
        (
            rerank_rank_weight,
            first_stage_rank_weight,
        ) = self._resolve_rerank_rank_weights(agent_cfg, defaults)
        dense_weight = self._config_float(
            agent_cfg,
            defaults,
            ("dense_weight",),
            1.0,
        )
        lexical_weight = self._config_float(
            agent_cfg,
            defaults,
            ("lexical_weight",),
            1.0,
        )
        exact_top_k = self._config_int(
            agent_cfg,
            defaults,
            ("exact_top_k",),
            16,
        )
        exact_weight = self._config_float(
            agent_cfg,
            defaults,
            ("exact_weight",),
            1.25,
        )
        exact_enabled = self._config_bool(
            agent_cfg,
            defaults,
            ("exact_search_enabled",),
            True,
        )
        hybrid_enabled = self._config_bool(
            agent_cfg,
            defaults,
            ("hybrid_search_enabled",),
            self.settings.rag_hybrid_search_enabled,
        )
        scope_search_policies = self._resolve_scope_search_policies(
            query_config=query_config,
            scopes=[
                scope
                for spec in query_specs
                for scope in spec.scopes
            ],
            dense_top_k=dense_top_k,
            lexical_top_k=lexical_top_k,
            exact_top_k=exact_top_k,
            hybrid_enabled=hybrid_enabled,
            exact_enabled=exact_enabled,
        )
        final_top_k = int(
            top_k
            or self._config_int(
                agent_cfg,
                defaults,
                ("rerank_top_n",),
                8,
            )
        )
        rerank_enabled = self._config_bool(
            agent_cfg,
            defaults,
            ("rerank_enabled",),
            True,
        )
        rerank_query, rerank_instruction = self._render_rerank_controls(
            agent_cfg=agent_cfg,
            defaults=defaults,
            context=context,
            query_specs=query_specs,
        )
        metadata_filter = dict(defaults.get("metadata_filter") or {})
        metadata_filter.update(agent_cfg.get("metadata_filter") or {})
        metadata_filter.setdefault(
            "index_version",
            self.settings.kb_index_version,
        )
        citation_scope_minimums = self._citation_scope_minimums(
            agent_name=agent_name,
            agent_cfg=agent_cfg,
            defaults=defaults,
            context=context,
            active_skills=active_skills,
            available_scopes=available_scopes,
            final_top_k=final_top_k,
        )
        generation_prefix_scope_targets = (
            self._generation_prefix_scope_targets(
                agent_name=agent_name,
                agent_cfg=agent_cfg,
                defaults=defaults,
                context=context,
                active_skills=active_skills,
                available_scopes=available_scopes,
                final_top_k=final_top_k,
            )
        )
        generation_prefix_scope_semantic_groups = (
            self._generation_prefix_scope_semantic_groups(
                agent_cfg=agent_cfg,
                defaults=defaults,
                available_scopes=available_scopes,
                scope_targets=generation_prefix_scope_targets,
            )
        )
        generation_prefix_fill_scopes = (
            self._generation_prefix_fill_scopes(
                agent_name=agent_name,
                agent_cfg=agent_cfg,
                defaults=defaults,
                context=context,
                active_skills=active_skills,
                available_scopes=available_scopes,
            )
        )
        preference_scope_targets = dict(citation_scope_minimums)
        for scope, count in generation_prefix_scope_targets.items():
            preference_scope_targets.setdefault(scope, count)
        citation_scope_preferred_terms = (
            self._citation_scope_preferred_terms(
                agent_name=agent_name,
                agent_cfg=agent_cfg,
                defaults=defaults,
                context=context,
                available_scopes=available_scopes,
                rerank_query=rerank_query,
                scope_minimums=preference_scope_targets,
            )
        )
        search_job_count = sum(len(spec.scopes) for spec in query_specs)
        query_parallelism = self._worker_count(
            search_job_count,
            self._config_value(
                agent_cfg,
                defaults,
                ("query_parallelism",),
                8,
            ),
            default=8,
            hard_limit=8,
        )
        cache_key = self._result_cache_key(
            agent_name=agent_name,
            query_specs=query_specs,
            rerank_query=rerank_query,
            rerank_instruction=rerank_instruction,
            dense_top_k=dense_top_k,
            lexical_top_k=lexical_top_k,
            rrf_k=rrf_k,
            fusion_top_n=fusion_top_n,
            rerank_candidate_top_n=rerank_candidate_top_n,
            rerank_units_per_parent=rerank_units_per_parent,
            rerank_semantic_group_candidate_enabled=(
                rerank_semantic_group_candidate_enabled
            ),
            rerank_compound_evidence_sets=rerank_compound_evidence_sets,
            rerank_rank_fusion_k=rerank_rank_fusion_k,
            rerank_rank_weight=rerank_rank_weight,
            first_stage_rank_weight=first_stage_rank_weight,
            final_top_k=final_top_k,
            rerank_enabled=rerank_enabled,
            hybrid_enabled=hybrid_enabled,
            dense_weight=dense_weight,
            lexical_weight=lexical_weight,
            exact_top_k=exact_top_k,
            exact_weight=exact_weight,
            exact_enabled=exact_enabled,
            metadata_filter=metadata_filter,
            active_skill_identities=[
                active.cache_identity() for active in active_skills
            ],
            citation_scope_minimums=citation_scope_minimums,
            citation_scope_preferred_terms=(
                citation_scope_preferred_terms
            ),
            generation_prefix_scope_targets=(
                generation_prefix_scope_targets
            ),
            generation_prefix_scope_semantic_groups=(
                generation_prefix_scope_semantic_groups
            ),
            generation_prefix_fill_scopes=(
                generation_prefix_fill_scopes
            ),
            scope_search_policies=scope_search_policies,
        )
        return _RetrievalPlan(
            agent_name=agent_name,
            query_specs=query_specs,
            rerank_query=rerank_query,
            rerank_instruction=rerank_instruction,
            dense_top_k=dense_top_k,
            lexical_top_k=lexical_top_k,
            rrf_k=rrf_k,
            fusion_top_n=fusion_top_n,
            rerank_candidate_top_n=rerank_candidate_top_n,
            rerank_units_per_parent=rerank_units_per_parent,
            rerank_semantic_group_candidate_enabled=(
                rerank_semantic_group_candidate_enabled
            ),
            rerank_compound_evidence_sets=rerank_compound_evidence_sets,
            rerank_rank_fusion_k=rerank_rank_fusion_k,
            rerank_rank_weight=rerank_rank_weight,
            first_stage_rank_weight=first_stage_rank_weight,
            final_top_k=final_top_k,
            dense_weight=dense_weight,
            lexical_weight=lexical_weight,
            hybrid_enabled=hybrid_enabled,
            rerank_enabled=rerank_enabled,
            exact_top_k=exact_top_k,
            exact_weight=exact_weight,
            exact_enabled=exact_enabled,
            query_parallelism=query_parallelism,
            metadata_filter=metadata_filter,
            citation_scope_minimums=citation_scope_minimums,
            citation_scope_preferred_terms=(
                citation_scope_preferred_terms
            ),
            generation_prefix_scope_targets=(
                generation_prefix_scope_targets
            ),
            generation_prefix_scope_semantic_groups=(
                generation_prefix_scope_semantic_groups
            ),
            generation_prefix_fill_scopes=(
                generation_prefix_fill_scopes
            ),
            cache_key=cache_key,
            active_skills=active_skills,
            scope_search_policies=scope_search_policies,
        )

    def _log_cache_hit(
        self,
        started: float,
        plan: _RetrievalPlan,
        cached: list[RetrievedChunk],
    ) -> None:
        log_metric(
            "rag.retrieve",
            agent_name=plan.agent_name,
            rag_query_count=0,
            rendered_query_count=len(plan.query_specs),
            rag_candidate_count=len(cached),
            rag_returned_count=len(cached),
            rag_cache_hit=True,
            embedding_cache_hit=None,
            embedding_cache_lookup_count=0,
            rerank_enabled=plan.rerank_enabled,
            hybrid_search_enabled=plan.hybrid_enabled,
            rag_ms=elapsed_ms(started),
            **self._skill_metric_fields(plan.active_skills),
        )

    @classmethod
    def _get_process_rerank_limiter(
        cls,
        max_concurrency: int,
    ) -> asyncio.BoundedSemaphore:
        loop = asyncio.get_running_loop()
        limit = max(1, int(max_concurrency))
        with cls._rerank_limiters_lock:
            by_limit = cls._rerank_limiters.get(loop)
            if by_limit is None:
                by_limit = {}
                cls._rerank_limiters[loop] = by_limit
            limiter = by_limit.get(limit)
            if limiter is None:
                limiter = asyncio.BoundedSemaphore(limit)
                by_limit[limit] = limiter
            return limiter

    def _get_rerank_limiter(self) -> asyncio.BoundedSemaphore:
        return self._get_process_rerank_limiter(
            self.settings.rag_rerank_max_concurrency
        )

    @_embedding_generation_guard
    def retrieve(
        self,
        agent_name: str,
        context: dict[str, Any],
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        started = now_ms()
        query_specs: list[_RenderedQuerySpec] = []
        candidate_count = 0
        search_job_count = 0
        lexical_error_count = 0
        embedding_lookups = 0
        embedding_hits = 0
        rerank_enabled = False
        hybrid_enabled = False
        final_top_k = int(top_k or 5)
        query_parallelism = 0
        semaphore_wait_ms = 0.0
        db_active_peak = 0
        rerank_active_peak = 0
        active_skills: list[ActiveKnowledgeSkill] = []

        try:
            query_config = self._query_config()
            defaults = query_config.get("defaults") or {}
            all_queries = query_config.get("queries") or query_config.get("agent_queries") or {}
            agent_cfg = all_queries.get(agent_name)
            if not isinstance(agent_cfg, dict) or agent_cfg.get("enabled", True) is False:
                raise ValueError(
                    f"Missing or disabled retrieval query config for agent: {agent_name}"
                )

            templates = (
                agent_cfg.get("query_templates")
                or agent_cfg.get("queries")
                or defaults.get("query_templates")
                or defaults.get("queries")
                or []
            )
            if not isinstance(templates, list) or not templates:
                raise ValueError(f"No query templates configured for agent: {agent_name}")
            configured_scopes = self._normalize_scopes(
                agent_cfg.get("scopes") or defaults.get("scopes") or []
            )
            allowed_scopes = set(configured_scopes)
            active_skills = (
                []
                if context.get("_disable_knowledge_skills")
                else self.knowledge_skill_router.select(agent_name, context)
            )
            skill_templates: list[dict[str, object]] = []
            for raw_template in self.knowledge_skill_router.retrieval_templates(
                active_skills
            ):
                if not isinstance(raw_template, dict):
                    continue
                scoped_template = dict(raw_template)
                skill_scopes = self._normalize_scopes(
                    scoped_template.get("scopes") or []
                )
                intersected_scopes = [
                    scope for scope in skill_scopes if scope in allowed_scopes
                ]
                if not intersected_scopes:
                    continue
                scoped_template["scopes"] = intersected_scopes
                skill_templates.append(scoped_template)
            templates = [*templates, *skill_templates]
            declared_scopes = self._declared_scopes(templates, configured_scopes)
            if not declared_scopes:
                raise ValueError(f"No pgvector scopes configured for agent: {agent_name}")
            available_scopes = self._available_scopes(declared_scopes)
            available_scopes = self._retrieval_scopes_for_context(
                agent_name=agent_name,
                agent_cfg=agent_cfg,
                defaults=defaults,
                context=context,
                active_skills=active_skills,
                available_scopes=available_scopes,
            )
            if not available_scopes:
                self._log_empty_result(
                    started=started,
                    agent_name=agent_name,
                    rendered_query_count=0,
                    rerank_enabled=False,
                    active_skills=active_skills,
                )
                return []

            query_specs = self._render_query_specs(
                templates=templates,
                context=context,
                fallback_scopes=configured_scopes,
                available_scopes=set(available_scopes),
            )
            if not query_specs:
                raise ValueError(f"Rendered retrieval query is empty for agent: {agent_name}")

            dense_top_k = self._config_int(
                agent_cfg,
                defaults,
                ("dense_top_k", "vector_top_k", "top_k"),
                16,
            )
            lexical_top_k = self._config_int(
                agent_cfg,
                defaults,
                ("lexical_top_k",),
                16,
            )
            rrf_k = self._config_int(agent_cfg, defaults, ("rrf_k",), 60)
            fusion_top_n = self._config_int(
                agent_cfg,
                defaults,
                ("fusion_top_n",),
                60,
            )
            rerank_candidate_top_n = self._config_int(
                agent_cfg,
                defaults,
                ("rerank_candidate_top_n",),
                30,
            )
            rerank_units_per_parent = min(
                _MAX_RERANK_UNITS_PER_PARENT,
                max(
                    _MIN_RERANK_UNITS_PER_PARENT,
                    self._config_int(
                        agent_cfg,
                        defaults,
                        ("rerank_units_per_parent",),
                        _DEFAULT_RERANK_UNITS_PER_PARENT,
                    ),
                ),
            )
            rerank_semantic_group_candidate_enabled = self._config_bool(
                agent_cfg,
                defaults,
                ("rerank_semantic_group_candidate_enabled",),
                _DEFAULT_RERANK_SEMANTIC_GROUP_CANDIDATE_ENABLED,
            )
            rerank_compound_evidence_sets = (
                self._config_compound_evidence_sets(agent_cfg, defaults)
            )
            rerank_rank_fusion_k = self._config_int(
                agent_cfg,
                defaults,
                ("rerank_rank_fusion_k",),
                _DEFAULT_RERANK_RANK_FUSION_K,
            )
            (
                rerank_rank_weight,
                first_stage_rank_weight,
            ) = self._resolve_rerank_rank_weights(agent_cfg, defaults)
            dense_weight = self._config_float(
                agent_cfg,
                defaults,
                ("dense_weight",),
                1.0,
            )
            lexical_weight = self._config_float(
                agent_cfg,
                defaults,
                ("lexical_weight",),
                1.0,
            )
            exact_top_k = self._config_int(
                agent_cfg,
                defaults,
                ("exact_top_k",),
                16,
            )
            exact_weight = self._config_float(
                agent_cfg,
                defaults,
                ("exact_weight",),
                1.25,
            )
            exact_enabled = self._config_bool(
                agent_cfg,
                defaults,
                ("exact_search_enabled",),
                True,
            )
            hybrid_enabled = self._config_bool(
                agent_cfg,
                defaults,
                ("hybrid_search_enabled",),
                self.settings.rag_hybrid_search_enabled,
            )
            scope_search_policies = self._resolve_scope_search_policies(
                query_config=query_config,
                scopes=[
                    scope
                    for spec in query_specs
                    for scope in spec.scopes
                ],
                dense_top_k=dense_top_k,
                lexical_top_k=lexical_top_k,
                exact_top_k=exact_top_k,
                hybrid_enabled=hybrid_enabled,
                exact_enabled=exact_enabled,
            )
            final_top_k = int(
                top_k
                or self._config_int(
                    agent_cfg,
                    defaults,
                    ("rerank_top_n",),
                    5,
                )
            )
            rerank_enabled = self._config_bool(
                agent_cfg,
                defaults,
                ("rerank_enabled",),
                True,
            )
            rerank_query, rerank_instruction = self._render_rerank_controls(
                agent_cfg=agent_cfg,
                defaults=defaults,
                context=context,
                query_specs=query_specs,
            )
            metadata_filter = dict(defaults.get("metadata_filter") or {})
            metadata_filter.update(agent_cfg.get("metadata_filter") or {})
            metadata_filter.setdefault(
                "index_version",
                self.settings.kb_index_version,
            )
            citation_scope_minimums = self._citation_scope_minimums(
                agent_name=agent_name,
                agent_cfg=agent_cfg,
                defaults=defaults,
                context=context,
                active_skills=active_skills,
                available_scopes=available_scopes,
                final_top_k=final_top_k,
            )
            generation_prefix_scope_targets = (
                self._generation_prefix_scope_targets(
                    agent_name=agent_name,
                    agent_cfg=agent_cfg,
                    defaults=defaults,
                    context=context,
                    active_skills=active_skills,
                    available_scopes=available_scopes,
                    final_top_k=final_top_k,
                )
            )
            generation_prefix_scope_semantic_groups = (
                self._generation_prefix_scope_semantic_groups(
                    agent_cfg=agent_cfg,
                    defaults=defaults,
                    available_scopes=available_scopes,
                    scope_targets=generation_prefix_scope_targets,
                )
            )
            generation_prefix_fill_scopes = (
                self._generation_prefix_fill_scopes(
                    agent_name=agent_name,
                    agent_cfg=agent_cfg,
                    defaults=defaults,
                    context=context,
                    active_skills=active_skills,
                    available_scopes=available_scopes,
                )
            )
            preference_scope_targets = dict(citation_scope_minimums)
            for scope, count in generation_prefix_scope_targets.items():
                preference_scope_targets.setdefault(scope, count)
            citation_scope_preferred_terms = (
                self._citation_scope_preferred_terms(
                    agent_name=agent_name,
                    agent_cfg=agent_cfg,
                    defaults=defaults,
                    context=context,
                    available_scopes=available_scopes,
                    rerank_query=rerank_query,
                    scope_minimums=preference_scope_targets,
                )
            )

            cache_key = self._result_cache_key(
                agent_name=agent_name,
                query_specs=query_specs,
                rerank_query=rerank_query,
                rerank_instruction=rerank_instruction,
                dense_top_k=dense_top_k,
                lexical_top_k=lexical_top_k,
                rrf_k=rrf_k,
                fusion_top_n=fusion_top_n,
                rerank_candidate_top_n=rerank_candidate_top_n,
                rerank_units_per_parent=rerank_units_per_parent,
                rerank_semantic_group_candidate_enabled=(
                    rerank_semantic_group_candidate_enabled
                ),
                rerank_compound_evidence_sets=(
                    rerank_compound_evidence_sets
                ),
                rerank_rank_fusion_k=rerank_rank_fusion_k,
                rerank_rank_weight=rerank_rank_weight,
                first_stage_rank_weight=first_stage_rank_weight,
                final_top_k=final_top_k,
                rerank_enabled=rerank_enabled,
                hybrid_enabled=hybrid_enabled,
                dense_weight=dense_weight,
                lexical_weight=lexical_weight,
                exact_top_k=exact_top_k,
                exact_weight=exact_weight,
                exact_enabled=exact_enabled,
                metadata_filter=metadata_filter,
                active_skill_identities=[
                    active.cache_identity() for active in active_skills
                ],
                citation_scope_minimums=citation_scope_minimums,
                citation_scope_preferred_terms=(
                    citation_scope_preferred_terms
                ),
                generation_prefix_scope_targets=(
                    generation_prefix_scope_targets
                ),
                generation_prefix_scope_semantic_groups=(
                    generation_prefix_scope_semantic_groups
                ),
                generation_prefix_fill_scopes=(
                    generation_prefix_fill_scopes
                ),
                scope_search_policies=scope_search_policies,
            )
            cached_result = self._get_cached_result(cache_key)
            if cached_result is not None:
                if self._needs_parent_context(agent_name):
                    cached_result = self._hydrate_parent_contexts(
                        cached_result,
                        applicable_job_levels=self._preferred_job_level_codes(
                            citation_scope_preferred_terms
                        ),
                    )
                log_metric(
                    "rag.retrieve",
                    agent_name=agent_name,
                    rag_query_count=0,
                    rendered_query_count=len(query_specs),
                    rag_candidate_count=len(cached_result),
                    rag_returned_count=len(cached_result),
                    rag_cache_hit=True,
                    embedding_cache_hit=None,
                    embedding_cache_lookup_count=0,
                    rerank_enabled=rerank_enabled,
                    hybrid_search_enabled=hybrid_enabled,
                    rag_ms=elapsed_ms(started),
                    **self._skill_metric_fields(active_skills),
                )
                return cached_result

            rendered_queries = [spec.query for spec in query_specs]
            query_embeddings, cache_hits = self.embedding_service.embed_queries(
                rendered_queries
            )
            if len(query_embeddings) != len(rendered_queries):
                raise RuntimeError("RAG query embedding batch size mismatch.")
            embedding_lookups = len(cache_hits)
            embedding_hits = sum(cache_hits)
            query_vectors = [
                PostgresRepository.vector_literal(embedding)
                for embedding in query_embeddings
            ]

            search_jobs = [
                (query_index, spec, embedding, vector, scope)
                for query_index, (spec, embedding, vector) in enumerate(
                    zip(
                        query_specs,
                        query_embeddings,
                        query_vectors,
                        strict=True,
                    )
                )
                for scope in spec.scopes
            ]
            search_job_count = len(search_jobs)

            branches: list[_RankedBranch] = []
            query_parallelism = 1
            for query_index, spec, embedding, vector, scope in search_jobs:
                job_result = self._search_scope(
                    query_index=query_index,
                    query=spec.query,
                    query_embedding=embedding,
                    query_vector_literal=vector,
                    scope=scope,
                    dense_top_k=dense_top_k,
                    lexical_top_k=lexical_top_k,
                    metadata_filter=metadata_filter,
                    template_weight=spec.weight,
                    dense_weight=dense_weight,
                    lexical_weight=lexical_weight,
                    hybrid_enabled=hybrid_enabled,
                    exact_top_k=exact_top_k,
                    exact_weight=exact_weight,
                    exact_enabled=exact_enabled,
                    scope_search_policy=scope_search_policies[scope],
                )
                branches.extend(job_result.branches)
                semaphore_wait_ms += job_result.semaphore_wait_ms
                db_active_peak = max(db_active_peak, job_result.active_count)
                if job_result.lexical_error:
                    lexical_error_count += 1
                    log_metric(
                        "rag.lexical_search.error",
                        agent_name=agent_name,
                        query_index=query_index,
                        scope=scope,
                        error=job_result.lexical_error,
                    )

            candidate_count = sum(len(branch.chunks) for branch in branches)
            fused_chunks = self._rrf_fuse(
                branches,
                rrf_k=rrf_k,
                top_n=fusion_top_n,
                scope_minimums=citation_scope_minimums,
                scope_preferred_terms=citation_scope_preferred_terms,
            )
            if not fused_chunks:
                self._log_empty_result(
                    started=started,
                    agent_name=agent_name,
                    rendered_query_count=len(query_specs),
                    rerank_enabled=rerank_enabled,
                    search_job_count=search_job_count,
                    candidate_count=candidate_count,
                    embedding_hits=embedding_hits,
                    embedding_lookups=embedding_lookups,
                    query_parallelism=query_parallelism,
                    hybrid_enabled=hybrid_enabled,
                    lexical_error_count=lexical_error_count,
                    semaphore_wait_ms=semaphore_wait_ms,
                    active_skills=active_skills,
                )
                return []

            query_for_rerank = rerank_query
            if rerank_enabled:
                rerank_parent_candidates = self._select_with_scope_minimums(
                    fused_chunks,
                    top_k=max(final_top_k, rerank_candidate_top_n),
                    scope_minimums=citation_scope_minimums,
                    scope_preferred_terms=citation_scope_preferred_terms,
                )
                rerank_candidates = self._expand_unit_rerank_candidates(
                    rerank_parent_candidates,
                    max_units_per_parent=rerank_units_per_parent,
                    include_semantic_group_candidate=(
                        rerank_semantic_group_candidate_enabled
                    ),
                    **(
                        {
                            "compound_evidence_sets": (
                                rerank_compound_evidence_sets
                            )
                        }
                        if rerank_compound_evidence_sets
                        else {}
                    ),
                )
                rerank_parallelism = self._worker_count(
                    len(rerank_candidates),
                    self._config_value(
                        agent_cfg,
                        defaults,
                        ("rerank_parallelism",),
                        8,
                    ),
                    default=8,
                    hard_limit=8,
                )
                rerank_active_peak = self._begin_rerank()
                try:
                    ranked_units = self.reranker.rerank(
                        rerank_candidates,
                        query=query_for_rerank,
                        top_k=len(rerank_candidates),
                        parallelism=rerank_parallelism,
                        instruction=rerank_instruction,
                    )
                    ranked_result = self._aggregate_unit_rerank_results(
                        rerank_parent_candidates,
                        ranked_units,
                        rank_fusion_k=rerank_rank_fusion_k,
                        rerank_rank_weight=rerank_rank_weight,
                        first_stage_rank_weight=first_stage_rank_weight,
                    )
                finally:
                    self._end_rerank()
                baseline_result = ranked_result[:final_top_k]
                result = self._select_with_scope_minimums(
                    ranked_result,
                    top_k=final_top_k,
                    scope_minimums=citation_scope_minimums,
                    scope_preferred_terms=citation_scope_preferred_terms,
                    diverse_prefix_k=min(
                        _DEFAULT_DIVERSE_PREFIX_K,
                        final_top_k,
                    ),
                    generation_prefix_scope_targets=(
                        generation_prefix_scope_targets
                    ),
                    generation_prefix_scope_semantic_groups=(
                        generation_prefix_scope_semantic_groups
                    ),
                    generation_prefix_fill_scopes=(
                        generation_prefix_fill_scopes
                    ),
                )
            else:
                rerank_parallelism = 0
                rerank_parent_candidates = []
                rerank_candidates = []
                baseline_result = fused_chunks[:final_top_k]
                result = self._select_with_scope_minimums(
                    fused_chunks,
                    top_k=final_top_k,
                    scope_minimums=citation_scope_minimums,
                    scope_preferred_terms=citation_scope_preferred_terms,
                    diverse_prefix_k=min(
                        _DEFAULT_DIVERSE_PREFIX_K,
                        final_top_k,
                    ),
                    generation_prefix_scope_targets=(
                        generation_prefix_scope_targets
                    ),
                    generation_prefix_scope_semantic_groups=(
                        generation_prefix_scope_semantic_groups
                    ),
                    generation_prefix_fill_scopes=(
                        generation_prefix_fill_scopes
                    ),
                )

            baseline_top_ids = {
                chunk.chunk_id for chunk in baseline_result
            }
            scope_replacement_count = sum(
                chunk.chunk_id not in baseline_top_ids for chunk in result
            )
            if lexical_error_count == 0:
                self._cache_result(cache_key, result)
            if self._needs_parent_context(agent_name):
                result = self._hydrate_parent_contexts(
                    result,
                    applicable_job_levels=self._preferred_job_level_codes(
                        citation_scope_preferred_terms
                    ),
                )
            log_metric(
                "rag.retrieve",
                agent_name=agent_name,
                rag_query_count=search_job_count,
                rendered_query_count=len(query_specs),
                rag_dense_query_count=search_job_count,
                rag_lexical_query_count=search_job_count if hybrid_enabled else 0,
                rag_lexical_error_count=lexical_error_count,
                rag_candidate_count=candidate_count,
                rag_merged_count=len(fused_chunks),
                rag_rerank_candidate_count=len(
                    rerank_parent_candidates
                ),
                rag_rerank_unit_candidate_count=len(
                    rerank_candidates
                ),
                rag_returned_count=len(result),
                rag_cache_hit=False,
                embedding_cache_hit=self._cache_hit_rate(
                    embedding_hits,
                    embedding_lookups,
                ),
                embedding_cache_lookup_count=embedding_lookups,
                rerank_enabled=rerank_enabled,
                hybrid_search_enabled=hybrid_enabled,
                rag_rerank_parallelism=rerank_parallelism,
                rag_query_parallelism=query_parallelism,
                rag_db_semaphore_wait_ms=round(semaphore_wait_ms, 2),
                rag_db_active_peak=db_active_peak,
                rag_rerank_active_peak=rerank_active_peak,
                rag_citation_scope_minimums=citation_scope_minimums,
                rag_citation_scope_replacement_count=scope_replacement_count,
                rag_ms=elapsed_ms(started),
                **self._skill_metric_fields(active_skills),
            )
            return result
        except Exception as exc:
            log_metric(
                "rag.retrieve.error",
                agent_name=agent_name,
                rag_query_count=search_job_count,
                rendered_query_count=len(query_specs),
                rag_candidate_count=candidate_count,
                rag_cache_hit=False,
                embedding_cache_hit=self._cache_hit_rate(
                    embedding_hits,
                    embedding_lookups,
                ),
                embedding_cache_lookup_count=embedding_lookups,
                rerank_enabled=rerank_enabled,
                rag_query_parallelism=query_parallelism,
                rag_lexical_error_count=lexical_error_count,
                rag_db_semaphore_wait_ms=round(semaphore_wait_ms, 2),
                rag_db_active_peak=db_active_peak,
                rag_rerank_active_peak=rerank_active_peak,
                rag_error_count=1,
                rag_ms=elapsed_ms(started),
                error_type=type(exc).__name__,
                error=str(exc)[:300],
                **self._skill_metric_fields(active_skills),
            )
            raise

    def _search_scope(
        self,
        *,
        query_index: int,
        query: str,
        query_embedding: list[float],
        query_vector_literal: str | None = None,
        scope: str,
        dense_top_k: int,
        lexical_top_k: int,
        metadata_filter: dict[str, Any],
        template_weight: float,
        dense_weight: float,
        lexical_weight: float,
        hybrid_enabled: bool,
        exact_top_k: int = 16,
        exact_weight: float = 1.25,
        exact_enabled: bool = True,
        scope_search_policy: _ScopeSearchPolicy | None = None,
    ) -> _SearchJobResult:
        policy = scope_search_policy or _ScopeSearchPolicy(
            dense_mode="exact",
            dense_top_k=dense_top_k,
            lexical_top_k=lexical_top_k,
            exact_top_k=exact_top_k,
            hybrid_enabled=hybrid_enabled,
            exact_enabled=exact_enabled,
        )
        dense_top_k = policy.dense_top_k
        lexical_top_k = policy.lexical_top_k
        exact_top_k = policy.exact_top_k
        hybrid_enabled = policy.hybrid_enabled
        exact_enabled = policy.exact_enabled
        collection_name = self.settings.collection_name_for_scope(scope)
        _, capabilities = self._embedding_runtime_snapshot()
        capability = (
            capabilities.get((collection_name, scope))
            if capabilities is not None
            else None
        )
        lexical_enabled = (
            hybrid_enabled
            if capabilities is None
            else hybrid_enabled and capability is not None and capability.bm25_ready
        )
        ann_requested = policy.dense_mode == "ann_preferred"
        use_ann = bool(
            ann_requested
            and capability is not None
            and capability.ann_ready(len(query_embedding))
        )
        log_metric(
            "rag.scope_search.policy",
            scope=scope,
            rag_dense_mode=policy.dense_mode,
            rag_ann_requested=ann_requested,
            rag_ann_used=use_ann,
            rag_ann_fallback=ann_requested and not use_ann,
            rag_dense_top_k=dense_top_k,
            rag_lexical_top_k=lexical_top_k,
            rag_exact_top_k=exact_top_k,
            hybrid_search_enabled=hybrid_enabled,
            exact_search_enabled=exact_enabled,
        )
        store = self._store_for_collection(collection_name)
        scope_filter = {**metadata_filter, "scope": scope}
        wait_started = time.perf_counter()
        active_count = 0
        try:
            with self._search_limiter:
                wait_ms = (time.perf_counter() - wait_started) * 1000
                active_count = self._begin_search()
                try:
                    if hybrid_enabled and hasattr(store, "search_hybrid_by_embedding"):
                        hybrid_arguments = {
                            "scope": scope,
                            "dense_top_k": dense_top_k,
                            "lexical_top_k": lexical_top_k,
                            "metadata_filter": scope_filter,
                        }
                        if query_vector_literal is not None and getattr(
                            store, "supports_query_vector_literal", False
                        ):
                            hybrid_arguments["query_vector_literal"] = (
                                query_vector_literal
                            )
                        if capabilities is not None:
                            hybrid_arguments["lexical_enabled"] = (
                                lexical_enabled
                            )
                        hybrid_parameters = inspect.signature(
                            store.search_hybrid_by_embedding
                        ).parameters
                        if "use_ann" in hybrid_parameters or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in hybrid_parameters.values()
                        ):
                            hybrid_arguments["use_ann"] = use_ann
                        if "exact_enabled" in hybrid_parameters:
                            hybrid_arguments["exact_enabled"] = exact_enabled
                        if "exact_top_k" in hybrid_parameters:
                            hybrid_arguments["exact_top_k"] = exact_top_k
                        search_result = store.search_hybrid_by_embedding(
                            query,
                            query_embedding,
                            **hybrid_arguments,
                        )
                    else:
                        dense_arguments = {
                            "top_k": dense_top_k,
                            "metadata_filter": scope_filter,
                        }
                        if query_vector_literal is not None and getattr(
                            store, "supports_query_vector_literal", False
                        ):
                            dense_arguments["query_vector_literal"] = (
                                query_vector_literal
                            )
                        dense_parameters = inspect.signature(
                            store.search_by_embedding
                        ).parameters
                        if "use_ann" in dense_parameters or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in dense_parameters.values()
                        ):
                            dense_arguments["use_ann"] = use_ann
                        dense = store.search_by_embedding(
                            query_embedding,
                            **dense_arguments,
                        )
                        search_result = HybridSearchResult(
                            dense=dense,
                            lexical=[],
                        )
                finally:
                    self._end_search()
        except RuntimeError as exc:
            if "pgvector collection is empty" not in str(exc):
                raise
            wait_ms = (time.perf_counter() - wait_started) * 1000
            search_result = HybridSearchResult(dense=[], lexical=[])

        branches = [
            _RankedBranch(
                branch_id=f"{query_index}:{scope}:dense",
                modality="dense",
                scope=scope,
                weight=template_weight * dense_weight,
                chunks=search_result.dense,
            )
        ]
        if lexical_enabled:
            branches.append(
                _RankedBranch(
                    branch_id=f"{query_index}:{scope}:bm25",
                    modality="bm25",
                    scope=scope,
                    weight=template_weight * lexical_weight,
                    chunks=search_result.lexical,
                )
            )
        if exact_enabled and search_result.exact:
            branches.append(
                _RankedBranch(
                    branch_id=f"{query_index}:{scope}:exact",
                    modality="exact",
                    scope=scope,
                    weight=template_weight * exact_weight,
                    chunks=search_result.exact,
                )
            )
        return _SearchJobResult(
            branches=branches,
            lexical_error=search_result.lexical_error,
            semaphore_wait_ms=wait_ms,
            lexical_executed=lexical_enabled,
            active_count=active_count,
            lexical_error_count=int(bool(search_result.lexical_error)),
        )

    def _search_scopes(
        self,
        *,
        query_index: int,
        query: str,
        query_embedding: list[float],
        query_vector_literal: str | None = None,
        scopes: list[str],
        dense_top_k: int,
        lexical_top_k: int,
        metadata_filter: dict[str, Any],
        template_weight: float,
        dense_weight: float,
        lexical_weight: float,
        hybrid_enabled: bool,
        exact_top_k: int = 16,
        exact_weight: float = 1.25,
        exact_enabled: bool = True,
        scope_search_policies: dict[str, _ScopeSearchPolicy] | None = None,
    ) -> _SearchJobResult:
        fallback_policy = _ScopeSearchPolicy(
            dense_mode="exact",
            dense_top_k=dense_top_k,
            lexical_top_k=lexical_top_k,
            exact_top_k=exact_top_k,
            hybrid_enabled=hybrid_enabled,
            exact_enabled=exact_enabled,
        )
        policies = scope_search_policies or {}
        grouped_scopes: dict[_ScopeSearchPolicy, list[str]] = {}
        for scope in scopes:
            grouped_scopes.setdefault(
                policies.get(scope, fallback_policy), []
            ).append(scope)
        if len(grouped_scopes) > 1:
            return self._combine_search_results(
                [
                    self._search_scopes(
                        query_index=query_index,
                        query=query,
                        query_embedding=query_embedding,
                        query_vector_literal=query_vector_literal,
                        scopes=group,
                        dense_top_k=policy.dense_top_k,
                        lexical_top_k=policy.lexical_top_k,
                        metadata_filter=metadata_filter,
                        template_weight=template_weight,
                        dense_weight=dense_weight,
                        lexical_weight=lexical_weight,
                        hybrid_enabled=policy.hybrid_enabled,
                        exact_top_k=policy.exact_top_k,
                        exact_weight=exact_weight,
                        exact_enabled=policy.exact_enabled,
                        scope_search_policies={
                            scope: policy for scope in group
                        },
                    )
                    for policy, group in grouped_scopes.items()
                ]
            )
        policy = next(iter(grouped_scopes), fallback_policy)
        dense_top_k = policy.dense_top_k
        lexical_top_k = policy.lexical_top_k
        exact_top_k = policy.exact_top_k
        hybrid_enabled = policy.hybrid_enabled
        exact_enabled = policy.exact_enabled
        if len(scopes) == 1 or "_search_scope" in self.__dict__:
            results = [
                self._search_scope(
                    query_index=query_index,
                    query=query,
                    query_embedding=query_embedding,
                    query_vector_literal=query_vector_literal,
                    scope=scope,
                    dense_top_k=dense_top_k,
                    lexical_top_k=lexical_top_k,
                    metadata_filter=metadata_filter,
                    template_weight=template_weight,
                    dense_weight=dense_weight,
                    lexical_weight=lexical_weight,
                    hybrid_enabled=hybrid_enabled,
                    exact_top_k=exact_top_k,
                    exact_weight=exact_weight,
                    exact_enabled=exact_enabled,
                    scope_search_policy=policy,
                )
                for scope in scopes
            ]
            return self._combine_search_results(results)

        _, capabilities = self._embedding_runtime_snapshot()
        requests: list[HybridScopeSearchRequest] = []
        lexical_flags: list[bool] = []
        for scope in scopes:
            collection_name = self.settings.collection_name_for_scope(scope)
            capability = (
                capabilities.get((collection_name, scope))
                if capabilities is not None
                else None
            )
            lexical_enabled = (
                hybrid_enabled
                if capabilities is None
                else (
                    hybrid_enabled
                    and capability is not None
                    and capability.bm25_ready
                )
            )
            ann_requested = policy.dense_mode == "ann_preferred"
            use_ann = bool(
                ann_requested
                and capability is not None
                and capability.ann_ready(len(query_embedding))
            )
            log_metric(
                "rag.scope_search.policy",
                scope=scope,
                rag_dense_mode=policy.dense_mode,
                rag_ann_requested=ann_requested,
                rag_ann_used=use_ann,
                rag_ann_fallback=ann_requested and not use_ann,
                rag_dense_top_k=dense_top_k,
                rag_lexical_top_k=lexical_top_k,
                rag_exact_top_k=exact_top_k,
                hybrid_search_enabled=hybrid_enabled,
                exact_search_enabled=exact_enabled,
            )
            lexical_flags.append(lexical_enabled)
            requests.append(
                HybridScopeSearchRequest(
                    collection_name=collection_name,
                    scope=scope,
                    metadata_filter={**metadata_filter, "scope": scope},
                    use_ann=use_ann,
                    lexical_enabled=lexical_enabled,
                    exact_enabled=exact_enabled,
                )
            )

        store = self._store_for_collection(requests[0].collection_name)
        batch_search = getattr(
            store,
            "search_hybrid_multi_scope_by_embedding",
            None,
        )
        if not callable(batch_search):
            return self._combine_search_results(
                [
                    self._search_scope(
                        query_index=query_index,
                        query=query,
                        query_embedding=query_embedding,
                        query_vector_literal=query_vector_literal,
                        scope=scope,
                        dense_top_k=dense_top_k,
                        lexical_top_k=lexical_top_k,
                        metadata_filter=metadata_filter,
                        template_weight=template_weight,
                        dense_weight=dense_weight,
                        lexical_weight=lexical_weight,
                        hybrid_enabled=hybrid_enabled,
                        exact_top_k=exact_top_k,
                        exact_weight=exact_weight,
                        exact_enabled=exact_enabled,
                        scope_search_policy=policy,
                    )
                    for scope in scopes
                ]
            )

        wait_started = time.perf_counter()
        active_count = 0
        with self._search_limiter:
            wait_ms = (time.perf_counter() - wait_started) * 1000
            active_count = self._begin_search()
            try:
                batch_arguments: dict[str, Any] = {
                    "requests": requests,
                    "dense_top_k": dense_top_k,
                    "lexical_top_k": lexical_top_k,
                    "query_vector_literal": query_vector_literal,
                }
                if "exact_top_k" in inspect.signature(batch_search).parameters:
                    batch_arguments["exact_top_k"] = exact_top_k
                batch_results = batch_search(
                    query,
                    query_embedding,
                    **batch_arguments,
                )
            finally:
                self._end_search()
        if len(batch_results) != len(scopes):
            raise RuntimeError("Multi-scope retrieval result size mismatch.")

        branches: list[_RankedBranch] = []
        lexical_errors: list[str] = []
        for scope, lexical_enabled, search_result in zip(
            scopes,
            lexical_flags,
            batch_results,
            strict=True,
        ):
            branches.append(
                _RankedBranch(
                    branch_id=f"{query_index}:{scope}:dense",
                    modality="dense",
                    scope=scope,
                    weight=template_weight * dense_weight,
                    chunks=search_result.dense,
                )
            )
            if lexical_enabled:
                branches.append(
                    _RankedBranch(
                        branch_id=f"{query_index}:{scope}:bm25",
                        modality="bm25",
                        scope=scope,
                        weight=template_weight * lexical_weight,
                        chunks=search_result.lexical,
                    )
                )
            if exact_enabled and search_result.exact:
                branches.append(
                    _RankedBranch(
                        branch_id=f"{query_index}:{scope}:exact",
                        modality="exact",
                        scope=scope,
                        weight=template_weight * exact_weight,
                        chunks=search_result.exact,
                    )
                )
            if search_result.lexical_error:
                lexical_errors.append(
                    f"{scope}: {search_result.lexical_error}"
                )
        return _SearchJobResult(
            branches=branches,
            lexical_error=" | ".join(lexical_errors) or None,
            semaphore_wait_ms=wait_ms,
            lexical_executed=sum(lexical_flags),
            active_count=active_count,
            lexical_error_count=len(lexical_errors),
        )

    @staticmethod
    def _combine_search_results(
        results: list[_SearchJobResult],
    ) -> _SearchJobResult:
        errors = [result.lexical_error for result in results if result.lexical_error]
        return _SearchJobResult(
            branches=[branch for result in results for branch in result.branches],
            lexical_error=" | ".join(errors) or None,
            semaphore_wait_ms=sum(result.semaphore_wait_ms for result in results),
            lexical_executed=sum(int(result.lexical_executed) for result in results),
            active_count=max((result.active_count for result in results), default=0),
            lexical_error_count=sum(
                max(int(result.lexical_error_count), int(bool(result.lexical_error)))
                for result in results
            ),
        )

    @classmethod
    def _get_search_limiter(
        cls,
        max_concurrency: int,
    ) -> AbstractContextManager[None]:
        limit = int(max_concurrency)
        if limit <= 0:
            return nullcontext()
        with cls._search_limiters_lock:
            limiter = cls._search_limiters.get(limit)
            if limiter is None:
                limiter = threading.BoundedSemaphore(limit)
                cls._search_limiters[limit] = limiter
            return limiter

    def _store_for_collection(self, collection_name: str) -> PGVectorClient:
        stores = getattr(self, "_stores", None)
        stores_lock = getattr(self, "_stores_lock", None)
        if stores is None or stores_lock is None:
            with self._store_cache_init_lock:
                if getattr(self, "_stores", None) is None:
                    self._stores = {}
                if getattr(self, "_stores_lock", None) is None:
                    self._stores_lock = threading.Lock()
                stores = self._stores
                stores_lock = self._stores_lock

        store = stores.get(collection_name)
        if store is not None:
            return store
        with stores_lock:
            store = stores.get(collection_name)
            if store is None:
                arguments: dict[str, Any] = {
                    "embedding_service": self.embedding_service,
                    "collection_name": collection_name,
                }
                repository = getattr(self, "repository", None)
                profile, _ = self._embedding_runtime_snapshot()
                if repository is not None:
                    arguments["repository"] = repository
                if profile is not None:
                    arguments["embedding_profile"] = profile
                parameters = inspect.signature(PGVectorClient).parameters
                accepts_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                if not accepts_kwargs:
                    arguments = {
                        key: value
                        for key, value in arguments.items()
                        if key in parameters
                    }
                store = PGVectorClient(**arguments)
                stores[collection_name] = store
            return store

    def _fetch_neighbor_group(
        self,
        collection_name: str,
        anchors: list[RetrievedChunk],
        window: int,
    ) -> _NeighborFetchResult:
        store = self._store_for_collection(collection_name)
        wait_started = time.perf_counter()
        active_count = 0
        with self._search_limiter:
            wait_ms = (time.perf_counter() - wait_started) * 1000
            active_count = self._begin_search()
            try:
                chunks = store.fetch_neighbor_chunks(anchors, window=window)
            finally:
                self._end_search()
        return _NeighborFetchResult(
            chunks=chunks,
            semaphore_wait_ms=wait_ms,
            active_count=active_count,
        )

    @classmethod
    def _begin_search(cls) -> int:
        with cls._activity_lock:
            cls._active_searches += 1
            return cls._active_searches

    @classmethod
    def _end_search(cls) -> None:
        with cls._activity_lock:
            cls._active_searches = max(0, cls._active_searches - 1)

    @classmethod
    def _begin_rerank(cls) -> int:
        with cls._activity_lock:
            cls._active_reranks += 1
            return cls._active_reranks

    @classmethod
    def _end_rerank(cls) -> None:
        with cls._activity_lock:
            cls._active_reranks = max(0, cls._active_reranks - 1)

    def _render_query_specs(
        self,
        *,
        templates: list[Any],
        context: dict[str, Any],
        fallback_scopes: list[str],
        available_scopes: set[str],
    ) -> list[_RenderedQuerySpec]:
        context = self._template_context(context)
        specs: list[_RenderedQuerySpec] = []
        seen_query_scopes: set[tuple[str, str]] = set()
        declared_scope_weights: dict[str, float] = {}
        for index, raw in enumerate(templates):
            if isinstance(raw, dict):
                template_value = raw.get("template") or raw.get("query") or ""
                scopes = self._normalize_scopes(raw.get("scopes") or fallback_scopes)
                try:
                    weight = float(raw.get("weight", 1.0))
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Invalid query template weight at index {index}"
                    ) from exc
            else:
                template_value = raw
                scopes = list(fallback_scopes)
                weight = 1.0
            if weight <= 0:
                raise ValueError(f"Query template weight must be positive at index {index}")
            for scope in scopes:
                declared_scope_weights[scope] = max(
                    weight,
                    declared_scope_weights.get(scope, 0.0),
                )
            rendered = Template(str(template_value)).render(**context).strip()
            eligible_scopes = [
                scope
                for scope in scopes
                if scope in available_scopes
                and (rendered, scope) not in seen_query_scopes
            ]
            if rendered and eligible_scopes:
                seen_query_scopes.update(
                    (rendered, scope) for scope in eligible_scopes
                )
                specs.append(
                    _RenderedQuerySpec(
                        query=rendered,
                        scopes=eligible_scopes,
                        weight=weight,
                        template_index=index,
                    )
                )
        rendered_scopes = {
            scope for spec in specs for scope in spec.scopes
        }
        for scope in self._required_configured_search_scopes(
            fallback_scopes=fallback_scopes,
            available_scopes=available_scopes,
            context=context,
        ):
            if scope in rendered_scopes:
                continue
            fallback_query = self._required_scope_fallback_query(
                scope=scope,
                context=context,
                specs=specs,
            )
            specs.append(
                _RenderedQuerySpec(
                    query=fallback_query,
                    scopes=[scope],
                    weight=declared_scope_weights.get(scope, 1.0),
                    template_index=len(templates),
                )
            )
        return specs

    @classmethod
    def _render_rerank_controls(
        cls,
        *,
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        context: dict[str, Any],
        query_specs: list[_RenderedQuerySpec],
    ) -> tuple[str, str | None]:
        context = cls._template_context(context)
        raw_query_template = cls._config_value(
            agent_cfg,
            defaults,
            ("rerank_query_template",),
            None,
        )
        rerank_query = ""
        if raw_query_template is not None:
            rerank_query = Template(str(raw_query_template)).render(
                **context
            ).strip()
            if not rerank_query:
                raise ValueError(
                    "Configured rerank_query_template rendered empty."
                )
        else:
            # Custom query configurations remain usable, but the fallback is
            # deliberately a single query rather than a concatenation of all
            # first-stage expansion queries.
            rerank_query = next(
                (
                    spec.query.strip()
                    for spec in query_specs
                    if spec.query.strip()
                ),
                "",
            )
        if not rerank_query:
            raise ValueError("Rendered rerank query is empty.")

        # Preserve one focused rerank query while adding only deterministic,
        # source-controlled canonical terms (for example, 使命必达 ->
        # Commitment to WIN). This lets the reranker connect bilingual labels
        # with their substantive sections without reusing broad recall-query
        # expansions.
        normalized_query = rerank_query.casefold()
        canonical_terms = [
            term
            for term in exact_query_terms(rerank_query)
            if term.casefold() not in normalized_query
        ]
        if canonical_terms:
            rerank_query = (
                f"{rerank_query}；Canonical terms: "
                + "; ".join(canonical_terms)
            )

        raw_instruction = cls._config_value(
            agent_cfg,
            defaults,
            ("rerank_instruction",),
            None,
        )
        rerank_instruction = None
        if raw_instruction is not None:
            rerank_instruction = str(raw_instruction).strip()
            if not rerank_instruction:
                raise ValueError("Configured rerank_instruction is empty.")
        return rerank_query, rerank_instruction

    @staticmethod
    def _normalized_job_level(value: object) -> str:
        match = _LOOSE_JOB_LEVEL_PATTERN.search(str(value or ""))
        if not match:
            return ""
        return f"{match.group(1).upper()}{int(match.group(2))}"

    @classmethod
    def _template_context(
        cls,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        rendered = dict(context)
        raw_profile = context.get("profile") or {}
        if isinstance(raw_profile, dict):
            raw_level = raw_profile.get("normalized_level") or raw_profile.get(
                "level"
            )
        else:
            raw_level = getattr(raw_profile, "normalized_level", None) or getattr(
                raw_profile,
                "level",
                "",
            )
        current_level = cls._normalized_job_level(raw_level)
        rendered["normalized_employee_level"] = current_level
        rendered["next_employee_level"] = _NEXT_JOB_LEVEL.get(
            current_level,
            "",
        )
        return rendered

    @classmethod
    def _applicable_job_levels(
        cls,
        *,
        agent_name: str,
        context: dict[str, Any],
    ) -> tuple[str, ...]:
        rendered = cls._template_context(context)
        current_level = str(
            rendered.get("normalized_employee_level") or ""
        )
        next_level = str(rendered.get("next_employee_level") or "")
        raw_intent = context.get("intent") or {}
        intent_id = (
            str(raw_intent.get("id") or "")
            if isinstance(raw_intent, dict)
            else str(getattr(raw_intent, "id", "") or "")
        )
        intent_id = str(context.get("intent_id") or intent_id).strip()
        if str(agent_name or "").strip() not in _PLAN_RETRIEVAL_AGENTS:
            return (current_level,) if current_level else ()
        if intent_id == "development":
            return (next_level,) if next_level else ()
        if intent_id == "development_improvement":
            return tuple(
                dict.fromkeys(
                    level for level in (current_level, next_level) if level
                )
            )
        return (current_level,) if current_level else ()

    def _required_configured_search_scopes(
        self,
        *,
        fallback_scopes: list[str],
        available_scopes: set[str],
        context: dict[str, Any],
    ) -> list[str]:
        required_scopes = [
            scope
            for scope in fallback_scopes
            if scope in _REQUIRED_CONFIGURED_SEARCH_SCOPES
            and scope in available_scopes
        ]
        requested_scopes = context.get("requested_scopes")
        if requested_scopes is None:
            return required_scopes
        requested_scope_set = set(self._normalize_scopes(requested_scopes))
        return [
            scope for scope in required_scopes if scope in requested_scope_set
        ]

    @staticmethod
    def _required_scope_fallback_query(
        *,
        scope: str,
        context: dict[str, Any],
        specs: list[_RenderedQuerySpec],
    ) -> str:
        template = _REQUIRED_SCOPE_FALLBACK_QUERY_TEMPLATES[scope]
        scope_query = Template(template).render(**context).strip()
        business_query = next(
            (spec.query for spec in specs if spec.query.strip()),
            "",
        )
        return " ".join(
            dict.fromkeys(
                part for part in (scope_query, business_query) if part
            )
        )

    def _declared_scopes(
        self,
        templates: list[Any],
        fallback_scopes: list[str],
    ) -> list[str]:
        declared: list[str] = []
        seen: set[str] = set()
        for raw in templates:
            if isinstance(raw, dict):
                scopes = self._normalize_scopes(raw.get("scopes") or fallback_scopes)
            else:
                scopes = fallback_scopes
            for scope in scopes:
                if scope not in seen:
                    declared.append(scope)
                    seen.add(scope)
        for scope in fallback_scopes:
            if (
                scope in _REQUIRED_CONFIGURED_SEARCH_SCOPES
                and scope not in seen
            ):
                declared.append(scope)
                seen.add(scope)
        return declared

    @staticmethod
    def _retrieval_unit_metadata(
        unit: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = unit.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                return {}
        return dict(metadata) if isinstance(metadata, dict) else {}

    @staticmethod
    def _should_include_adjacent_unit_context(
        parent: RetrievedChunk,
        unit: dict[str, Any],
    ) -> bool:
        unit_type = str(unit.get("unit_type") or "").strip().lower()
        if unit_type == "table":
            return True
        return unit_type == "ordered_sections" and bool(
            (parent.metadata or {}).get("oversized_atomic")
        )

    @classmethod
    def _unit_rerank_search_text(
        cls,
        parent: RetrievedChunk,
        unit: dict[str, Any],
    ) -> str:
        unit_metadata = cls._retrieval_unit_metadata(unit)
        unit_text = str(unit.get("text") or parent.text).strip()
        focused_text = str(unit.get("search_text") or "").strip()
        if not focused_text:
            focused_text = contextual_search_text(
                scope=parent.scope,
                title=parent.title,
                heading_path=unit_metadata.get("heading_path") or (),
                text=unit_text,
                table_headers=unit_metadata.get("table_headers") or (),
            )

        descriptors = [
            f"Retrieval unit type: {str(unit.get('unit_type') or 'parent')}"
        ]
        semantic_item_id = str(
            unit_metadata.get("semantic_item_id") or ""
        ).strip()
        if semantic_item_id:
            descriptors.append(f"Semantic item: {semantic_item_id}")

        exact_facts = [
            fact
            for fact in (unit.get("exact_facts") or [])
            if isinstance(fact, dict)
        ]
        if exact_facts:
            descriptors.append(
                "Exact matched facts:\n"
                + "\n".join(
                    "- "
                    + " | ".join(
                        value
                        for value in (
                            str(fact.get("fact_type") or "").strip(),
                            str(
                                fact.get("normalized_value") or ""
                            ).strip(),
                            str(fact.get("source_quote") or "").strip(),
                        )
                        if value
                    )
                    for fact in exact_facts
                )
            )

        if cls._should_include_adjacent_unit_context(parent, unit):
            adjacent: list[str] = []
            previous_text = str(
                unit_metadata.get("previous_unit_text") or ""
            ).strip()
            next_text = str(
                unit_metadata.get("next_unit_text") or ""
            ).strip()
            if previous_text:
                adjacent.append("[Previous unit]\n" + previous_text)
            if next_text:
                adjacent.append("[Next unit]\n" + next_text)
            if adjacent:
                descriptors.append(
                    "Adjacent context only; it has no retrieval score:\n"
                    + "\n\n".join(adjacent)
                )
        return "\n\n".join([focused_text, *descriptors])

    @staticmethod
    def _compound_term_present(text: str, term: str) -> bool:
        normalized_text = normalize_content(text)
        normalized_term = normalize_content(term)
        if not normalized_text or not normalized_term:
            return False
        if normalized_term.isascii() and all(
            character.isalnum() or character in {" ", "-", "_"}
            for character in normalized_term
        ):
            return bool(
                re.search(
                    rf"(?<![a-z0-9]){re.escape(normalized_term)}"
                    r"(?![a-z0-9])",
                    normalized_text,
                )
            )
        return normalized_term in normalized_text

    @staticmethod
    def _unit_rrf_contribution(
        unit: dict[str, Any],
        *,
        fallback: float,
    ) -> float:
        try:
            raw_contribution = unit.get("rrf_contribution")
            return float(
                fallback
                if raw_contribution is None
                else raw_contribution
            )
        except (TypeError, ValueError):
            return float(fallback)

    @classmethod
    def _semantic_compound_rerank_units(
        cls,
        parent: RetrievedChunk,
        raw_units: list[dict[str, Any]],
        evidence_sets: tuple[tuple[str, ...], ...],
    ) -> list[dict[str, Any]]:
        """Build only declared cross-leaf evidence candidates.

        A semantic compound is not the whole parent. It is the smallest set of
        complete atomic leaves that jointly covers one configured evidence
        set. Single-leaf matches remain ordinary atomic candidates, and an
        over-budget combination is skipped instead of being truncated.
        """

        if not evidence_sets:
            return []
        parent_metadata = dict(parent.metadata or {})
        if not bool(parent_metadata.get("atomic_structure")):
            return []

        distinct_units = select_distinct_retrieval_units(
            raw_units,
            limit=MAX_RETRIEVAL_UNITS_PER_PARENT,
        )
        if len(distinct_units) < 2:
            return []

        unit_group_ids = {
            str(
                cls._retrieval_unit_metadata(unit).get(
                    "semantic_group_id"
                )
                or ""
            ).strip()
            for unit in distinct_units
        }
        if len(unit_group_ids) != 1 or "" in unit_group_ids:
            return []
        semantic_group_id = next(iter(unit_group_ids))
        parent_group_id = str(
            parent_metadata.get("semantic_group_id") or ""
        ).strip()
        if parent_group_id and parent_group_id != semantic_group_id:
            return []

        semantic_group_type = str(
            parent_metadata.get("semantic_group_type") or ""
        ).strip()
        if not semantic_group_type:
            return []

        heading_path = [
            str(value)
            for value in (parent_metadata.get("heading_path") or [])
            if str(value).strip()
        ]
        table_headers = [
            str(value)
            for value in (parent_metadata.get("table_headers") or [])
            if str(value).strip()
        ]
        semantic_group_title = str(
            parent_metadata.get("semantic_group_title")
            or parent_metadata.get("section")
            or ""
        ).strip()
        unit_search_texts = [
            "\n".join(
                value
                for value in (
                    semantic_group_title,
                    str(unit.get("search_text") or "").strip(),
                    str(unit.get("text") or "").strip(),
                )
                if value
            )
            for unit in distinct_units
        ]

        output: list[dict[str, Any]] = []
        seen_unit_combinations: set[tuple[str, ...]] = set()
        for raw_evidence_set in evidence_sets:
            evidence_set = tuple(
                dict.fromkeys(
                    term.strip()
                    for term in raw_evidence_set
                    if str(term).strip()
                )
            )
            if len(evidence_set) < 2:
                continue
            coverage = [
                {
                    term
                    for term in evidence_set
                    if cls._compound_term_present(search_text, term)
                }
                for search_text in unit_search_texts
            ]
            required_terms = set(evidence_set)
            if any(unit_terms >= required_terms for unit_terms in coverage):
                # The atomic leaf already gives the reranker the complete
                # evidence set. Adding a longer sibling would only add noise.
                continue

            viable_combinations: list[
                tuple[float, tuple[int, ...], str]
            ] = []
            for leaf_count in range(
                2,
                min(_MAX_RERANK_COMPOUND_LEAVES, len(distinct_units)) + 1,
            ):
                for indexes in combinations(range(len(distinct_units)), leaf_count):
                    covered_terms = set().union(
                        *(coverage[index] for index in indexes)
                    )
                    if covered_terms < required_terms:
                        continue
                    selected = [distinct_units[index] for index in indexes]
                    body = "\n\n".join(
                        str(unit.get("text") or "").strip()
                        for unit in selected
                        if str(unit.get("text") or "").strip()
                    )
                    compound_text = "\n\n".join(
                        value
                        for value in (
                            f"## {semantic_group_title}"
                            if semantic_group_title
                            else "",
                            body,
                        )
                        if value
                    )
                    if (
                        not compound_text
                        or len(compound_text) > _MAX_RERANK_COMPOUND_CHARS
                    ):
                        continue
                    contribution = sum(
                        cls._unit_rrf_contribution(
                            unit,
                            fallback=float(parent.score),
                        )
                        for unit in selected
                    )
                    viable_combinations.append(
                        (-contribution, indexes, compound_text)
                    )
                if viable_combinations:
                    break
            if not viable_combinations:
                continue

            _, indexes, compound_text = min(
                viable_combinations,
                key=lambda candidate: (
                    candidate[0],
                    candidate[1],
                ),
            )
            selected_units = [distinct_units[index] for index in indexes]
            selected_unit_ids = tuple(
                str(unit.get("unit_id") or "")
                for unit in selected_units
            )
            if selected_unit_ids in seen_unit_combinations:
                continue
            seen_unit_combinations.add(selected_unit_ids)

            source_metadata = [
                cls._retrieval_unit_metadata(unit)
                for unit in selected_units
            ]

            def boundary(key: str, reducer) -> Any:
                values: list[int] = []
                for metadata in source_metadata:
                    try:
                        values.append(int(metadata.get(key)))
                    except (TypeError, ValueError):
                        continue
                return reducer(values) if values else None

            exact_facts: list[dict[str, Any]] = []
            seen_facts: set[str] = set()
            for unit in selected_units:
                for fact in unit.get("exact_facts") or []:
                    if not isinstance(fact, dict):
                        continue
                    fact_key = json.dumps(
                        fact,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    if fact_key in seen_facts:
                        continue
                    exact_facts.append(dict(fact))
                    seen_facts.add(fact_key)

            evidence_identity = "\x1f".join(evidence_set)
            evidence_hash = hashlib.sha256(
                evidence_identity.encode("utf-8")
            ).hexdigest()[:12]
            output.append(
                {
                    "unit_id": (
                        "semantic-compound:"
                        f"{parent.chunk_id}:{semantic_group_id}:"
                        f"{evidence_hash}"
                    ),
                    "unit_type": "semantic_compound",
                    "unit_index": min(
                        int(unit.get("unit_index") or 0)
                        for unit in selected_units
                    ),
                    "text": compound_text,
                    "search_text": contextual_search_text(
                        scope=parent.scope,
                        title=parent.title,
                        heading_path=heading_path,
                        text=compound_text,
                        table_headers=table_headers,
                    ),
                    "exact_facts": exact_facts,
                    "rrf_contribution": max(
                        cls._unit_rrf_contribution(
                            unit,
                            fallback=float(parent.score),
                        )
                        for unit in selected_units
                    ),
                    "metadata": {
                        "semantic_group_id": semantic_group_id,
                        "semantic_group_type": semantic_group_type,
                        "semantic_group_title": semantic_group_title,
                        "semantic_split": bool(
                            parent_metadata.get("semantic_split")
                        ),
                        "compound_unit_ids": list(selected_unit_ids),
                        "covered_terms": list(evidence_set),
                        "evidence_set_complete": True,
                        "complete_semantic_group": False,
                        "heading_path": heading_path,
                        "table_headers": table_headers,
                        "source_start_line": boundary(
                            "source_start_line", min
                        ),
                        "source_end_line": boundary(
                            "source_end_line", max
                        ),
                        "source_start_char": boundary(
                            "source_start_char", min
                        ),
                        "source_end_char": boundary(
                            "source_end_char", max
                        ),
                    },
                }
            )
        return output[:_MAX_RERANK_COMPOUND_CANDIDATES_PER_PARENT]

    @classmethod
    def _expand_unit_rerank_candidates(
        cls,
        parent_candidates: list[RetrievedChunk],
        *,
        max_units_per_parent: int = _DEFAULT_RERANK_UNITS_PER_PARENT,
        include_semantic_group_candidate: bool = False,
        compound_evidence_sets: tuple[tuple[str, ...], ...] = (
            _DEFAULT_RERANK_COMPOUND_EVIDENCE_SETS
        ),
    ) -> list[RetrievedChunk]:
        try:
            requested_unit_limit = int(max_units_per_parent)
        except (TypeError, ValueError):
            requested_unit_limit = _DEFAULT_RERANK_UNITS_PER_PARENT
        resolved_unit_limit = min(
            _MAX_RERANK_UNITS_PER_PARENT,
            max(_MIN_RERANK_UNITS_PER_PARENT, requested_unit_limit),
        )
        output: list[RetrievedChunk] = []
        for parent in parent_candidates:
            raw_units = retrieval_units_from_metadata(parent.metadata)

            def unit_priority(
                indexed_unit: tuple[int, dict[str, Any]],
            ) -> tuple[float, int, int, int, str]:
                original_index, unit = indexed_unit
                try:
                    raw_contribution = unit.get("rrf_contribution")
                    contribution = float(
                        parent.score
                        if raw_contribution is None
                        else raw_contribution
                    )
                except (TypeError, ValueError):
                    contribution = float(parent.score)
                try:
                    retrieval_rank = max(
                        1,
                        int(unit.get("retrieval_rank") or original_index + 1),
                    )
                except (TypeError, ValueError):
                    retrieval_rank = original_index + 1
                try:
                    unit_index = int(unit.get("unit_index") or 0)
                except (TypeError, ValueError):
                    unit_index = 0
                return (
                    -contribution,
                    retrieval_rank,
                    unit_index,
                    original_index,
                    str(unit.get("unit_id") or ""),
                )

            ordered_units = [
                unit
                for _, unit in sorted(
                    enumerate(raw_units),
                    key=unit_priority,
                )
            ]
            semantic_compound_units = (
                cls._semantic_compound_rerank_units(
                    parent,
                    raw_units,
                    compound_evidence_sets,
                )
                if include_semantic_group_candidate
                else []
            )
            units = select_distinct_retrieval_units(
                ordered_units,
                limit=resolved_unit_limit,
            )
            # Compound evidence has a separate, sparse budget. It never
            # consumes one of the Top-N atomic leaf positions.
            units.extend(semantic_compound_units)
            if not units:
                parent_metadata = dict(parent.metadata or {})
                units = [
                    {
                        "unit_id": f"parent:{parent.chunk_id}",
                        "unit_type": "parent",
                        "unit_index": 0,
                        "text": parent.text,
                        "search_text": str(
                            parent_metadata.get("search_text") or ""
                        ),
                        "metadata": {
                            "heading_path": parent_metadata.get(
                                "heading_path"
                            )
                            or [],
                            "table_headers": parent_metadata.get(
                                "table_headers"
                            )
                            or [],
                        },
                        "rrf_contribution": float(parent.score),
                    }
                ]

            for unit in units:
                unit_id = str(unit["unit_id"])
                search_text = cls._unit_rerank_search_text(parent, unit)
                output.append(
                    RetrievedChunk(
                        chunk_id=(
                            f"{parent.chunk_id}::retrieval-unit::{unit_id}"
                        ),
                        source_id=parent.source_id,
                        title=parent.title,
                        scope=parent.scope,
                        text=str(unit.get("text") or parent.text),
                        score=float(
                            parent.score
                            if unit.get("rrf_contribution") is None
                            else unit.get("rrf_contribution")
                        ),
                        metadata={
                            "search_text": search_text,
                            "rerank_parent_chunk_id": parent.chunk_id,
                            "rerank_unit": dict(unit),
                        },
                    )
                )
        return output

    @classmethod
    def _aggregate_unit_rerank_results(
        cls,
        parent_candidates: list[RetrievedChunk],
        ranked_units: list[RetrievedChunk],
        *,
        rank_fusion_k: int = _DEFAULT_RERANK_RANK_FUSION_K,
        rerank_rank_weight: float = _DEFAULT_RERANK_RANK_WEIGHT,
        first_stage_rank_weight: float = (
            _DEFAULT_FIRST_STAGE_RANK_WEIGHT
        ),
    ) -> list[RetrievedChunk]:
        try:
            denominator_offset = max(1, int(rank_fusion_k))
        except (TypeError, ValueError):
            denominator_offset = _DEFAULT_RERANK_RANK_FUSION_K
        (
            effective_rerank_weight,
            effective_first_stage_weight,
        ) = cls._normalize_rerank_rank_weights(
            rerank_rank_weight,
            first_stage_rank_weight,
        )
        parents = {
            parent.chunk_id: parent
            for parent in parent_candidates
        }
        parent_order = {
            parent.chunk_id: index
            for index, parent in enumerate(parent_candidates)
        }
        grouped: dict[str, list[RetrievedChunk]] = {}
        for candidate in ranked_units:
            parent_id = str(
                (candidate.metadata or {}).get(
                    "rerank_parent_chunk_id"
                )
                or ""
            )
            if parent_id in parents:
                grouped.setdefault(parent_id, []).append(candidate)

        first_stage_ranks: dict[str, int] = {}
        for parent_id, parent in parents.items():
            fusion_metadata = (
                (parent.metadata or {}).get("retrieval_fusion") or {}
            )
            try:
                first_stage_rank = max(
                    1,
                    int(fusion_metadata.get("first_stage_rank")),
                )
            except (TypeError, ValueError):
                first_stage_rank = parent_order[parent_id] + 1
            first_stage_ranks[parent_id] = first_stage_rank

        for candidates in grouped.values():
            candidates.sort(
                key=lambda candidate: (
                    -float(candidate.score),
                    candidate.chunk_id,
                )
            )
        reranked_parent_ids = sorted(
            grouped,
            key=lambda parent_id: (
                -float(grouped[parent_id][0].score),
                first_stage_ranks[parent_id],
                parent_id,
            ),
        )
        rerank_ranks = {
            parent_id: rank
            for rank, parent_id in enumerate(reranked_parent_ids, start=1)
        }

        output: list[RetrievedChunk] = []
        for parent_id in reranked_parent_ids:
            candidates = grouped[parent_id]
            selected_units: list[dict[str, Any]] = []
            for candidate in candidates:
                unit = dict(
                    (candidate.metadata or {}).get("rerank_unit") or {}
                )
                if not unit:
                    continue
                unit["raw_rerank_score"] = float(candidate.score)
                unit["rerank_search_text"] = str(
                    (candidate.metadata or {}).get("search_text") or ""
                )
                selected_units.append(unit)
            atomic_units = [
                unit
                for unit in selected_units
                if str(unit.get("unit_type") or "")
                != "semantic_compound"
            ]
            compound_units = [
                unit
                for unit in selected_units
                if str(unit.get("unit_type") or "")
                == "semantic_compound"
            ]
            selected_units = select_distinct_retrieval_units(
                atomic_units,
                limit=_MAX_RERANK_UNITS_PER_PARENT,
            )
            selected_units.extend(
                compound_units[
                    :_MAX_RERANK_COMPOUND_CANDIDATES_PER_PARENT
                ]
            )
            selected_units.sort(
                key=lambda unit: (
                    -float(unit.get("raw_rerank_score") or 0.0),
                    str(unit.get("unit_id") or ""),
                )
            )
            if not selected_units:
                continue

            rerank_rank = rerank_ranks[parent_id]
            first_stage_rank = first_stage_ranks[parent_id]
            rerank_rank_component = (
                effective_rerank_weight
                / (denominator_offset + rerank_rank)
            )
            first_stage_rank_component = (
                effective_first_stage_weight
                / (denominator_offset + first_stage_rank)
            )
            final_score = (
                rerank_rank_component + first_stage_rank_component
            )
            parent = parents[parent_id].model_copy(deep=True)
            first_stage_score = float(parent.score)
            raw_rerank_score = float(
                selected_units[0]["raw_rerank_score"]
            )
            parent.score = final_score
            metadata = dict(parent.metadata or {})
            metadata["retrieval_units"] = selected_units
            metadata["retrieval_unit"] = {
                key: value
                for key, value in selected_units[0].items()
                if key
                not in {
                    "raw_rerank_score",
                    "rerank_search_text",
                    "rrf_contribution",
                    "search_text",
                }
            }
            metadata["search_text"] = selected_units[0][
                "rerank_search_text"
            ]
            metadata["retrieval_unit_rerank"] = {
                "best_unit_id": selected_units[0]["unit_id"],
                "raw_rerank_score": raw_rerank_score,
                "rrf_score": first_stage_score,
                "final_score": final_score,
                "rerank_rank": rerank_rank,
                "first_stage_rank": first_stage_rank,
                "rank_fusion_k": denominator_offset,
                "rerank_rank_weight": effective_rerank_weight,
                "first_stage_rank_weight": effective_first_stage_weight,
                "rerank_rank_component": rerank_rank_component,
                "first_stage_rank_component": (
                    first_stage_rank_component
                ),
                "evaluated_unit_count": len(candidates),
            }
            parent.metadata = metadata
            output.append(parent)

        output.sort(
            key=lambda parent: (
                -float(parent.score),
                rerank_ranks[parent.chunk_id],
                first_stage_ranks[parent.chunk_id],
                parent.chunk_id,
            )
        )
        return output

    @staticmethod
    def _rerank_document_chars(
        candidates: list[RetrievedChunk],
    ) -> int:
        return sum(
            len(str((candidate.metadata or {}).get("search_text") or ""))
            for candidate in candidates
        )

    @classmethod
    def _merge_retrieval_unit_payload(
        cls,
        current: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(current)
        for key, value in incoming.items():
            if key == "metadata":
                metadata = cls._retrieval_unit_metadata(merged)
                incoming_metadata = cls._retrieval_unit_metadata(incoming)
                metadata.update(
                    {
                        name: item
                        for name, item in incoming_metadata.items()
                        if item is not None and item != ""
                    }
                )
                merged["metadata"] = metadata
            elif key == "exact_facts":
                facts = [
                    fact
                    for fact in (
                        list(merged.get("exact_facts") or [])
                        + list(value or [])
                    )
                    if isinstance(fact, dict)
                ]
                seen: set[str] = set()
                unique_facts: list[dict[str, Any]] = []
                for fact in facts:
                    identity = json.dumps(
                        fact,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    unique_facts.append(fact)
                merged["exact_facts"] = unique_facts
            elif (
                value is not None
                and value != ""
                and (
                    key not in merged
                    or merged.get(key) is None
                    or merged.get(key) == ""
                )
            ):
                merged[key] = value
        return merged

    @staticmethod
    def _rrf_fuse(
        branches: list[_RankedBranch],
        *,
        rrf_k: int,
        top_n: int,
        scope_minimums: dict[str, int] | None = None,
        scope_preferred_terms: dict[str, tuple[str, ...]] | None = None,
    ) -> list[RetrievedChunk]:
        scores: dict[str, float] = {}
        chunks: dict[str, RetrievedChunk] = {}
        evidence: dict[str, list[dict[str, Any]]] = {}
        retrieval_unit_scores: dict[str, dict[str, float]] = {}
        retrieval_unit_payloads: dict[
            str, dict[str, dict[str, Any]]
        ] = {}
        retrieval_unit_order: dict[str, dict[str, int]] = {}
        next_unit_order = 0
        denominator_offset = max(1, int(rrf_k))

        for branch in branches:
            for rank, item in enumerate(branch.chunks, start=1):
                contribution = branch.weight / (denominator_offset + rank)
                if item.chunk_id not in chunks:
                    chunks[item.chunk_id] = item.model_copy(deep=True)
                    scores[item.chunk_id] = 0.0
                    evidence[item.chunk_id] = []
                item_metadata = dict(item.metadata or {})
                for unit in retrieval_units_from_metadata(item_metadata):
                    unit_id = str(unit["unit_id"])
                    try:
                        unit_rank = max(
                            1,
                            int(unit.get("retrieval_rank") or rank),
                        )
                    except (TypeError, ValueError):
                        unit_rank = rank
                    unit_contribution = branch.weight / (
                        denominator_offset + unit_rank
                    )
                    unit_scores = retrieval_unit_scores.setdefault(
                        item.chunk_id,
                        {},
                    )
                    unit_scores[unit_id] = (
                        unit_scores.get(unit_id, 0.0) + unit_contribution
                    )
                    payloads = retrieval_unit_payloads.setdefault(
                        item.chunk_id,
                        {},
                    )
                    if unit_id not in payloads:
                        payloads[unit_id] = dict(unit)
                        retrieval_unit_order.setdefault(
                            item.chunk_id,
                            {},
                        )[unit_id] = next_unit_order
                        next_unit_order += 1
                    else:
                        payloads[unit_id] = (
                            RetrievalService._merge_retrieval_unit_payload(
                                payloads[unit_id],
                                unit,
                            )
                        )
                scores[item.chunk_id] += contribution
                evidence[item.chunk_id].append(
                    {
                        "branch_id": branch.branch_id,
                        "modality": branch.modality,
                        "scope": branch.scope,
                        "rank": rank,
                        "raw_score": float(item.score),
                        "weight": branch.weight,
                        "rrf_contribution": contribution,
                    }
                )

        fused: list[RetrievedChunk] = []
        for chunk_id, chunk in chunks.items():
            fusion_score = scores[chunk_id]
            chunk.score = fusion_score
            metadata = dict(chunk.metadata or {})
            unit_scores = retrieval_unit_scores.get(chunk_id, {})
            if unit_scores:
                ordered_unit_ids = sorted(
                    unit_scores,
                    key=lambda unit_id: (
                        -unit_scores[unit_id],
                        retrieval_unit_order[chunk_id][unit_id],
                        unit_id,
                    ),
                )
                ranked_units: list[dict[str, Any]] = []
                for unit_id in ordered_unit_ids:
                    unit = dict(retrieval_unit_payloads[chunk_id][unit_id])
                    unit.pop("retrieval_rank", None)
                    unit["rrf_contribution"] = unit_scores[unit_id]
                    ranked_units.append(unit)
                selected_units = select_distinct_retrieval_units(
                    ranked_units,
                    limit=MAX_RETRIEVAL_UNITS_PER_PARENT,
                )
                metadata["retrieval_units"] = selected_units
                metadata["retrieval_unit"] = {
                    key: value
                    for key, value in selected_units[0].items()
                    if key not in {"rrf_contribution", "search_text"}
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
            chunk.metadata = {
                **metadata,
                "retrieval_fusion": {
                    "rrf_score": fusion_score,
                    "evidence": evidence[chunk_id],
                },
            }
            fused.append(chunk)
        fused.sort(key=lambda item: (-item.score, item.chunk_id))
        for first_stage_rank, item in enumerate(fused, start=1):
            metadata = dict(item.metadata or {})
            fusion_metadata = dict(
                metadata.get("retrieval_fusion") or {}
            )
            fusion_metadata["first_stage_rank"] = first_stage_rank
            metadata["retrieval_fusion"] = fusion_metadata
            item.metadata = metadata
        return RetrievalService._select_with_scope_minimums(
            fused,
            top_k=max(1, int(top_n)),
            scope_minimums=scope_minimums,
            scope_preferred_terms=scope_preferred_terms,
        )

    @staticmethod
    def _select_with_scope_minimums(
        ranked_chunks: list[RetrievedChunk],
        *,
        top_k: int,
        scope_minimums: dict[str, int] | None,
        scope_preferred_terms: dict[str, tuple[str, ...]] | None = None,
        diverse_prefix_k: int = 0,
        generation_prefix_scope_targets: dict[str, int] | None = None,
        generation_prefix_scope_semantic_groups: dict[
            str,
            tuple[_GenerationPrefixSemanticGroup, ...],
        ] | None = None,
        generation_prefix_fill_scopes: tuple[str, ...] | None = None,
    ) -> list[RetrievedChunk]:
        limit = max(1, int(top_k))
        term_match_cache: dict[tuple[str, tuple[str, ...]], set[str]] = {}
        family_conflict_cache: dict[
            tuple[str, tuple[str, ...]], bool
        ] = {}

        def preferred_matches(
            chunk: RetrievedChunk,
            terms: tuple[str, ...],
        ) -> set[str]:
            key = (chunk.chunk_id, terms)
            if key not in term_match_cache:
                term_match_cache[key] = (
                    RetrievalService._preferred_term_matches(chunk, terms)
                )
            return term_match_cache[key]

        def family_conflict(
            chunk: RetrievedChunk,
            terms: tuple[str, ...],
        ) -> bool:
            key = (chunk.chunk_id, terms)
            if key not in family_conflict_cache:
                family_conflict_cache[key] = (
                    RetrievalService._preferred_term_family_conflict(
                        chunk,
                        terms,
                    )
                )
            return family_conflict_cache[key]

        if not scope_minimums:
            return RetrievalService._reorder_selected_prefix(
                ranked_chunks[:limit],
                prefix_k=diverse_prefix_k,
                prefix_scope_targets=(
                    generation_prefix_scope_targets or {}
                ),
                prefix_scope_semantic_groups=(
                    generation_prefix_scope_semantic_groups or {}
                ),
                fill_scopes=generation_prefix_fill_scopes or (),
                scope_preferred_terms=scope_preferred_terms or {},
            )

        active_scope_preferences = {
            scope: tuple(terms)
            for scope, terms in (scope_preferred_terms or {}).items()
            if terms
            and any(
                chunk.scope == scope
                and bool(preferred_matches(chunk, tuple(terms)))
                for chunk in ranked_chunks
            )
        }
        selected_ids: set[str] = set()
        selected_evidence_identities: set[str] = set()
        covered_preferred_terms: dict[str, set[str]] = {}
        for scope, raw_minimum in scope_minimums.items():
            remaining = min(max(0, int(raw_minimum)), limit - len(selected_ids))
            if remaining <= 0:
                break
            candidates = [
                chunk
                for chunk in ranked_chunks
                if chunk.scope == scope and chunk.chunk_id not in selected_ids
            ]
            preferred_terms = tuple(
                active_scope_preferences.get(scope) or ()
            )
            indexed_candidates = list(enumerate(candidates))
            covered_terms = covered_preferred_terms.setdefault(scope, set())
            while remaining > 0 and indexed_candidates:
                ranked_candidates = sorted(
                    indexed_candidates,
                    key=lambda item: (
                        family_conflict(
                            item[1],
                            preferred_terms,
                        ),
                        -len(
                            preferred_matches(
                                item[1],
                                preferred_terms,
                            )
                            - covered_terms
                        ),
                        -len(preferred_matches(item[1], preferred_terms)),
                        RetrievalService._chunk_evidence_identity(item[1])
                        in selected_evidence_identities,
                        item[0],
                    ),
                )
                _, chunk = ranked_candidates[0]
                selected_ids.add(chunk.chunk_id)
                selected_evidence_identities.add(
                    RetrievalService._chunk_evidence_identity(chunk)
                )
                covered_terms.update(
                    preferred_matches(
                        chunk,
                        preferred_terms,
                    )
                )
                indexed_candidates = [
                    item
                    for item in indexed_candidates
                    if item[1].chunk_id != chunk.chunk_id
                ]
                remaining -= 1

        non_conflicting: list[tuple[int, RetrievedChunk]] = []
        conflicting: list[tuple[int, RetrievedChunk]] = []
        for item in enumerate(ranked_chunks):
            chunk = item[1]
            preferred_terms = tuple(
                active_scope_preferences.get(chunk.scope) or ()
            )
            target = (
                conflicting
                if family_conflict(
                    chunk,
                    preferred_terms,
                )
                else non_conflicting
            )
            target.append(item)

        observed_evidence_identities = set(selected_evidence_identities)
        for family_candidates in (non_conflicting, conflicting):
            remaining_candidates = [
                item
                for item in family_candidates
                if item[1].chunk_id not in selected_ids
            ]
            while len(selected_ids) < limit and remaining_candidates:
                ranked_candidates = sorted(
                    remaining_candidates,
                    key=lambda item: (
                        -len(
                            preferred_matches(
                                item[1],
                                tuple(
                                    active_scope_preferences.get(
                                        item[1].scope
                                    )
                                    or ()
                                ),
                            )
                            - covered_preferred_terms.setdefault(
                                item[1].scope,
                                set(),
                            )
                        ),
                        RetrievalService._chunk_evidence_identity(item[1])
                        in observed_evidence_identities,
                        item[0],
                    ),
                )
                _, chunk = ranked_candidates[0]
                selected_ids.add(chunk.chunk_id)
                observed_evidence_identities.add(
                    RetrievalService._chunk_evidence_identity(chunk)
                )
                covered_preferred_terms.setdefault(
                    chunk.scope,
                    set(),
                ).update(
                    preferred_matches(
                        chunk,
                        tuple(
                            active_scope_preferences.get(chunk.scope) or ()
                        ),
                    )
                )
                remaining_candidates = [
                    item
                    for item in remaining_candidates
                    if item[1].chunk_id != chunk.chunk_id
                ]

        selected = [
            chunk
            for chunk in ranked_chunks
            if chunk.chunk_id in selected_ids
        ][:limit]
        return RetrievalService._reorder_selected_prefix(
            selected,
            prefix_k=diverse_prefix_k,
            prefix_scope_targets=(
                generation_prefix_scope_targets or {}
            ),
            prefix_scope_semantic_groups=(
                generation_prefix_scope_semantic_groups or {}
            ),
            fill_scopes=generation_prefix_fill_scopes or (),
            scope_preferred_terms=active_scope_preferences,
        )

    @classmethod
    def _reorder_selected_prefix(
        cls,
        selected_chunks: list[RetrievedChunk],
        *,
        prefix_k: int,
        prefix_scope_targets: dict[str, int],
        prefix_scope_semantic_groups: dict[
            str,
            tuple[_GenerationPrefixSemanticGroup, ...],
        ],
        fill_scopes: tuple[str, ...],
        scope_preferred_terms: dict[str, tuple[str, ...]],
    ) -> list[RetrievedChunk]:
        """Order an explicitly configured generation prefix.

        Citation quotas have already selected the complete result membership.
        Prefix targets are independent: configured scopes are filled in YAML
        declaration order, with exact preferred terms ahead of family-safe and
        original-rank fallbacks.  Target evidence may displace the original
        Top-1 and may exceed the source cap.  Remaining prefix slots prefer the
        configured fill scopes and only avoid a third same-source item when a
        genuine alternative exists.
        """

        window = min(max(0, int(prefix_k)), len(selected_chunks))
        active_targets: dict[str, int] = {}
        for raw_scope, raw_count in prefix_scope_targets.items():
            scope = str(raw_scope).strip()
            try:
                count = max(0, int(raw_count or 0))
            except (TypeError, ValueError):
                continue
            if scope and count > 0:
                active_targets[scope] = count
        if window <= 0 or not active_targets:
            return list(selected_chunks)

        indexed_remaining = list(enumerate(selected_chunks))
        prefix: list[RetrievedChunk] = []
        source_counts: dict[str, int] = {}
        covered_preferred_terms: dict[str, set[str]] = {}
        term_match_cache: dict[tuple[str, tuple[str, ...]], set[str]] = {}
        generation_term_match_cache: dict[
            tuple[str, tuple[str, ...]], set[str]
        ] = {}
        family_conflict_cache: dict[
            tuple[str, tuple[str, ...]], bool
        ] = {}

        def preferred_matches(
            chunk: RetrievedChunk,
            terms: tuple[str, ...],
        ) -> set[str]:
            key = (chunk.chunk_id, terms)
            if key not in term_match_cache:
                term_match_cache[key] = cls._preferred_term_matches(
                    chunk,
                    terms,
                )
            return term_match_cache[key]

        def generation_matches(
            chunk: RetrievedChunk,
            terms: tuple[str, ...],
        ) -> set[str]:
            key = (chunk.chunk_id, terms)
            if key not in generation_term_match_cache:
                generation_term_match_cache[key] = (
                    cls._generation_evidence_term_matches(chunk, terms)
                )
            return generation_term_match_cache[key]

        def family_conflict(
            chunk: RetrievedChunk,
            terms: tuple[str, ...],
        ) -> bool:
            key = (chunk.chunk_id, terms)
            if key not in family_conflict_cache:
                family_conflict_cache[key] = (
                    cls._preferred_term_family_conflict(chunk, terms)
                )
            return family_conflict_cache[key]

        def observe(chunk: RetrievedChunk) -> None:
            source_identity = cls._chunk_source_identity(chunk)
            source_counts[source_identity] = (
                source_counts.get(source_identity, 0) + 1
            )
            preferred_terms = tuple(
                scope_preferred_terms.get(chunk.scope) or ()
            )
            covered_preferred_terms.setdefault(chunk.scope, set()).update(
                preferred_matches(chunk, preferred_terms)
            )

        def take(item: tuple[int, RetrievedChunk]) -> None:
            _, selected = item
            prefix.append(selected)
            observe(selected)
            indexed_remaining[:] = [
                remaining
                for remaining in indexed_remaining
                if remaining[1].chunk_id != selected.chunk_id
            ]

        for scope, count in active_targets.items():
            semantic_slots_taken = 0
            semantic_groups = tuple(
                (prefix_scope_semantic_groups or {}).get(scope) or ()
            )
            for semantic_group in semantic_groups[:count]:
                if len(prefix) >= window:
                    break
                preferred_terms = tuple(
                    scope_preferred_terms.get(scope) or ()
                )
                candidates = [
                    item
                    for item in indexed_remaining
                    if item[1].scope == scope
                    and generation_matches(
                        item[1],
                        semantic_group.required_terms,
                    )
                ]
                if not candidates:
                    # A semantic family may reorder only evidence that is
                    # actually present.  Missing anchors fall back to the
                    # established scope selection instead of promoting an
                    # unrelated chunk.
                    continue
                candidates.sort(
                    key=lambda item: (
                        bool(
                            generation_matches(
                                item[1],
                                semantic_group.downrank_terms,
                            )
                        ),
                        -len(
                            generation_matches(
                                item[1],
                                semantic_group.preferred_terms,
                            )
                        ),
                        family_conflict(item[1], preferred_terms),
                        item[0],
                    )
                )
                take(candidates[0])
                semantic_slots_taken += 1

            for _ in range(max(0, count - semantic_slots_taken)):
                if len(prefix) >= window:
                    break
                preferred_terms = tuple(
                    scope_preferred_terms.get(scope) or ()
                )
                covered_terms = covered_preferred_terms.setdefault(
                    scope,
                    set(),
                )
                candidates = [
                    item
                    for item in indexed_remaining
                    if item[1].scope == scope
                ]
                if not candidates:
                    break
                candidates.sort(
                    key=lambda item: (
                        -len(
                            preferred_matches(
                                item[1],
                                preferred_terms,
                            )
                            - covered_terms
                        ),
                        -len(preferred_matches(item[1], preferred_terms)),
                        family_conflict(
                            item[1],
                            preferred_terms,
                        ),
                        item[0],
                    )
                )
                # Explicit target coverage is stronger than source diversity.
                take(candidates[0])

        normalized_fill_scopes = set(cls._normalize_scopes(fill_scopes))
        while len(prefix) < window and indexed_remaining:
            scoped_candidates = [
                item
                for item in indexed_remaining
                if item[1].scope in normalized_fill_scopes
            ]
            candidate_pool = scoped_candidates or list(indexed_remaining)
            non_conflicting_candidates = [
                item
                for item in candidate_pool
                if not family_conflict(
                    item[1],
                    tuple(
                        scope_preferred_terms.get(item[1].scope) or ()
                    ),
                )
            ]
            if non_conflicting_candidates:
                candidate_pool = non_conflicting_candidates
            candidate_pool.sort(key=lambda item: item[0])
            selected_item = candidate_pool[0]
            selected_source = cls._chunk_source_identity(selected_item[1])
            if source_counts.get(selected_source, 0) >= 2:
                alternative = next(
                    (
                        item
                        for item in candidate_pool
                        if source_counts.get(
                            cls._chunk_source_identity(item[1]),
                            0,
                        )
                        < 2
                    ),
                    None,
                )
                if alternative is not None:
                    selected_item = alternative
            take(selected_item)

        prefix_ids = {chunk.chunk_id for chunk in prefix}
        return [
            *prefix,
            *(
                chunk
                for chunk in selected_chunks
                if chunk.chunk_id not in prefix_ids
            ),
        ]

    @staticmethod
    def _chunk_source_identity(chunk: RetrievedChunk) -> str:
        source_id = normalize_content(chunk.source_id or "")
        if source_id:
            return "\x1f".join((normalize_content(chunk.scope), source_id))
        return f"chunk:{chunk.chunk_id}"

    @classmethod
    def _chunk_coarse_semantic_identity(
        cls,
        chunk: RetrievedChunk,
    ) -> str:
        metadata = dict(chunk.metadata or {})
        canonical_id = normalize_content(
            metadata.get("canonical_content_id") or ""
        )
        if canonical_id:
            return f"canonical:{canonical_id}"
        retrieval_unit = metadata.get("retrieval_unit") or {}
        unit_metadata = (
            cls._retrieval_unit_metadata(retrieval_unit)
            if isinstance(retrieval_unit, dict)
            else {}
        )
        semantic_group_id = normalize_content(
            metadata.get("semantic_group_id")
            or unit_metadata.get("semantic_group_id")
            or ""
        )
        raw_heading_path = metadata.get("heading_path")
        if isinstance(raw_heading_path, (list, tuple)):
            heading_path = " / ".join(
                str(part).strip()
                for part in raw_heading_path
                if str(part).strip()
            )
        else:
            heading_path = str(raw_heading_path or "").strip()
        structural_identity = semantic_group_id or normalize_content(
            heading_path
        )
        source_identity = cls._chunk_source_identity(chunk)
        if structural_identity:
            return "\x1f".join((source_identity, structural_identity))
        return cls._chunk_evidence_identity(chunk)

    @classmethod
    def _chunk_evidence_identity(cls, chunk: RetrievedChunk) -> str:
        metadata = dict(chunk.metadata or {})
        raw_heading_path = metadata.get("heading_path")
        if isinstance(raw_heading_path, (list, tuple)):
            heading_path = " / ".join(
                str(part).strip()
                for part in raw_heading_path
                if str(part).strip()
            )
        else:
            heading_path = str(raw_heading_path or "").strip()
        retrieval_unit = metadata.get("retrieval_unit") or {}
        unit_metadata = (
            cls._retrieval_unit_metadata(retrieval_unit)
            if isinstance(retrieval_unit, dict)
            else {}
        )
        semantic_item_id = normalize_content(
            metadata.get("semantic_item_id")
            or (
                retrieval_unit.get("semantic_item_id")
                if isinstance(retrieval_unit, dict)
                else ""
            )
            or unit_metadata.get("semantic_item_id")
            or ""
        )
        normalized_heading = normalize_content(heading_path)
        semantic_group_id = normalize_content(
            metadata.get("semantic_group_id")
            or unit_metadata.get("semantic_group_id")
            or ""
        )
        if semantic_item_id:
            structural_identity = "\x1e".join(
                value
                for value in (
                    normalized_heading or semantic_group_id,
                    semantic_item_id,
                )
                if value
            )
        else:
            structural_identity = normalized_heading or semantic_group_id
        if not structural_identity:
            span = ":".join(
                str(unit_metadata.get(name) or metadata.get(name) or "")
                for name in ("source_start_line", "source_end_line")
            ).strip(":")
            structural_identity = normalize_content(span)
        source_id = normalize_content(chunk.source_id or "")
        if source_id and structural_identity:
            return "\x1f".join(
                (normalize_content(chunk.scope), source_id, structural_identity)
            )
        return f"chunk:{chunk.chunk_id}"

    @staticmethod
    def _preferred_term_matches(
        chunk: RetrievedChunk,
        preferred_terms: tuple[str, ...],
    ) -> set[str]:
        metadata = dict(chunk.metadata or {})
        unit_texts = [
            str(unit.get("search_text") or unit.get("text") or "")
            for unit in metadata.get("retrieval_units") or []
            if isinstance(unit, dict)
        ]
        document = "\n".join(
            [
                str(chunk.title or ""),
                str(chunk.text or ""),
                str(metadata.get("search_text") or ""),
                *unit_texts,
            ]
        )
        normalized = normalize_content(document)
        canonical_document_terms = set(exact_query_terms(document))
        matches: set[str] = set()
        for term in preferred_terms:
            normalized_term = normalize_content(term)
            if not normalized_term:
                continue
            if normalized_term in canonical_document_terms or re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_term)}"
                r"(?![a-z0-9])",
                normalized,
            ):
                matches.add(normalized_term)
        return matches

    @staticmethod
    def _generation_evidence_term_matches(
        chunk: RetrievedChunk,
        preferred_terms: tuple[str, ...],
    ) -> set[str]:
        """Match terms only in evidence that can reach the generation prompt.

        A reranked parent chunk may contain many unrelated paragraphs and its
        contextual ``search_text`` may contain adjacent units.  Generation,
        however, is focused around at most the first three reranked leaf units.
        Semantic-prefix families therefore use those leaf texts (or an already
        materialized generation override), never the whole parent/search text.
        Chunks without unit metadata retain the established child-text match.
        """

        metadata = dict(chunk.metadata or {})
        generation_override = str(
            metadata.get("generation_context_override") or ""
        ).strip()
        if generation_override:
            document = generation_override
        else:
            raw_units = metadata.get("retrieval_units")
            if raw_units is not None:
                if not isinstance(raw_units, (list, tuple)) or any(
                    not isinstance(unit, dict) for unit in raw_units
                ):
                    return set()
                visible_unit_texts = [
                    str(unit.get("text") or "").strip()
                    for unit in raw_units[
                        :DEFAULT_GENERATION_FOCUS_MAX_RERANKED_UNITS
                    ]
                    if str(unit.get("text") or "").strip()
                ]
                if raw_units and not visible_unit_texts:
                    return set()
                document = "\n".join(visible_unit_texts)
            else:
                raw_best_unit = metadata.get("retrieval_unit")
                if raw_best_unit is not None:
                    if not isinstance(raw_best_unit, dict):
                        return set()
                    document = str(
                        raw_best_unit.get("text") or ""
                    ).strip()
                    if not document:
                        return set()
                else:
                    document = str(chunk.text or "")

        normalized = normalize_content(document)
        canonical_document_terms = set(exact_query_terms(document))
        matches: set[str] = set()
        for term in preferred_terms:
            normalized_term = normalize_content(term)
            if not normalized_term:
                continue
            if normalized_term in canonical_document_terms or re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_term)}"
                r"(?![a-z0-9])",
                normalized,
            ):
                matches.add(normalized_term)
        return matches

    @staticmethod
    def _preferred_term_match_count(
        chunk: RetrievedChunk,
        preferred_terms: tuple[str, ...],
    ) -> int:
        return len(
            RetrievalService._preferred_term_matches(
                chunk,
                preferred_terms,
            )
        )

    @staticmethod
    def _preferred_term_family_conflict(
        chunk: RetrievedChunk,
        preferred_terms: tuple[str, ...],
    ) -> bool:
        if chunk.scope != "job_level":
            return False
        preferred_codes = {
            term.upper()
            for term in preferred_terms
            if _JOB_LEVEL_CODE_PATTERN.fullmatch(term)
        }
        if not preferred_codes:
            return False
        metadata = dict(chunk.metadata or {})
        document = "\n".join(
            (
                str(chunk.title or ""),
                str(chunk.text or ""),
                str(metadata.get("search_text") or ""),
            )
        )
        primary_codes = {
            str(first or second).upper()
            for first, second in _PRIMARY_JOB_LEVEL_PATTERN.findall(document)
            if first or second
        }
        chunk_codes = primary_codes or {
            term.upper()
            for term in exact_query_terms(document)
            if _JOB_LEVEL_CODE_PATTERN.fullmatch(term)
        }
        # A broad table or section that contains an applicable level remains
        # eligible so citation validation can use its complete source text.
        # A standalone row/section for a different level is demoted even when
        # it belongs to the same G/SL or P/M family.  The earlier family-only
        # comparison allowed, for example, SL2 evidence into an SL1
        # improvement/exit plan.
        return bool(chunk_codes) and chunk_codes.isdisjoint(preferred_codes)

    @classmethod
    def _citation_scope_preferred_terms(
        cls,
        *,
        agent_name: str = "",
        agent_cfg: dict[str, Any] | None = None,
        defaults: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        available_scopes: list[str] | None = None,
        rerank_query: str,
        scope_minimums: dict[str, int],
    ) -> dict[str, tuple[str, ...]]:
        exact_terms = tuple(exact_query_terms(rerank_query))
        preferred: dict[str, tuple[str, ...]] = {}
        available = set(available_scopes or scope_minimums)
        raw_configured = cls._config_value(
            agent_cfg or {},
            defaults or {},
            ("citation_scope_preferred_terms",),
            {},
        )
        if isinstance(raw_configured, dict):
            for raw_scope, raw_terms in raw_configured.items():
                scope = str(raw_scope or "").strip()
                if scope not in available or not isinstance(
                    raw_terms,
                    (list, tuple),
                ):
                    continue
                terms = tuple(
                    dict.fromkeys(
                        str(term).strip()
                        for term in raw_terms
                        if str(term).strip()
                    )
                )
                if terms:
                    preferred[scope] = terms

        def extend(scope: str, terms: tuple[str, ...]) -> None:
            if not terms:
                return
            preferred[scope] = tuple(
                dict.fromkeys((*preferred.get(scope, ()), *terms))
            )

        if "job_level" in available:
            structured_codes = RetrievalService._applicable_job_levels(
                agent_name=agent_name,
                context=context or {},
            )
            labelled_codes = tuple(
                dict.fromkeys(
                    match.upper()
                    for match in _APPLICABLE_JOB_LEVEL_PATTERN.findall(
                        rerank_query
                    )
                )
            )
            if agent_name in _PLAN_RETRIEVAL_AGENTS:
                job_level_codes = structured_codes
            else:
                job_level_codes = structured_codes or labelled_codes or tuple(
                    dict.fromkeys(
                        term.upper()
                        for term in exact_terms
                        if _JOB_LEVEL_CODE_PATTERN.fullmatch(term)
                    )
                )
            if job_level_codes:
                extend("job_level", job_level_codes)
        if "culture" in scope_minimums:
            culture_vocabulary = (
                "collaboration to deliver",
                "innovation to shape",
                "customer-centricity to grow",
                "commitment to win",
            )
            context_culture_terms = set(
                exact_query_terms(
                    "\n".join(
                        (
                            str((context or {}).get("supplemental_info") or ""),
                            str((context or {}).get("performance_context") or ""),
                        )
                    )
                )
            )
            explicit_culture_terms = tuple(
                term
                for term in culture_vocabulary
                if term in context_culture_terms
            )
            culture_terms = explicit_culture_terms or tuple(
                term
                for term in culture_vocabulary
                if term in exact_terms
            )
            if culture_terms:
                extend("culture", culture_terms)
        if "performance" in scope_minimums:
            performance_terms = tuple(
                term.upper()
                for term in ("what", "how", "asr")
                if term in exact_terms
            )
            if performance_terms:
                extend("performance", performance_terms)
        return preferred

    @classmethod
    def _citation_scope_minimums(
        cls,
        *,
        agent_name: str = "",
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        context: dict[str, Any],
        active_skills: list[ActiveKnowledgeSkill],
        available_scopes: list[str],
        final_top_k: int,
    ) -> dict[str, int]:
        raw_minimums = cls._config_value(
            agent_cfg,
            defaults,
            ("citation_scope_minimums",),
            {},
        )
        if not isinstance(raw_minimums, dict):
            return {}
        raw_conditions = cls._config_value(
            agent_cfg,
            defaults,
            ("citation_scope_conditions",),
            {},
        )
        conditions = raw_conditions if isinstance(raw_conditions, dict) else {}
        available = set(available_scopes)
        has_company_values = bool(context.get("company_value_terms"))
        has_culture_skill = any(
            "culture" in active.skill.scopes for active in active_skills
        )
        remaining = max(1, int(final_top_k))
        result: dict[str, int] = {}
        for raw_scope, raw_minimum in raw_minimums.items():
            scope = str(raw_scope).strip()
            if not scope or scope not in available:
                continue
            condition = str(conditions.get(scope) or "").strip()
            if condition == "company_values_or_culture_skill" and not (
                has_company_values or has_culture_skill
            ):
                continue
            if (
                condition == "career_elements_applicable"
                and not bool(context.get("career_elements_applicable"))
            ):
                continue
            if (
                condition == "applicable_job_levels"
                and not cls._applicable_job_levels(
                    agent_name=agent_name,
                    context=context,
                )
            ):
                continue
            try:
                minimum = max(0, int(raw_minimum))
            except (TypeError, ValueError):
                continue
            minimum = min(minimum, remaining)
            if minimum <= 0:
                continue
            result[scope] = minimum
            remaining -= minimum
            if remaining <= 0:
                break
        return result

    @classmethod
    def _generation_prefix_scope_targets(
        cls,
        *,
        agent_name: str = "",
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        context: dict[str, Any],
        active_skills: list[ActiveKnowledgeSkill],
        available_scopes: list[str],
        final_top_k: int,
    ) -> dict[str, int]:
        """Resolve explicitly configured generation-prefix scope targets.

        Citation quotas select the complete result membership.  These targets
        only control the order of the short generation prefix and preserve the
        declaration order from query.yaml as the target priority.
        """

        raw_targets = cls._config_value(
            agent_cfg,
            defaults,
            ("generation_prefix_scope_targets",),
            {},
        )
        if not isinstance(raw_targets, dict):
            return {}
        raw_conditions = cls._config_value(
            agent_cfg,
            defaults,
            ("generation_prefix_scope_conditions",),
            {},
        )
        conditions = raw_conditions if isinstance(raw_conditions, dict) else {}
        available = set(available_scopes)
        remaining = min(
            _DEFAULT_DIVERSE_PREFIX_K,
            max(0, int(final_top_k)),
        )
        result: dict[str, int] = {}
        for raw_scope, raw_count in raw_targets.items():
            scope = str(raw_scope).strip()
            if not scope or scope not in available or remaining <= 0:
                continue
            condition = str(conditions.get(scope) or "").strip()
            if not cls._generation_prefix_condition_met(
                condition=condition,
                agent_name=agent_name,
                context=context,
                active_skills=active_skills,
            ):
                continue
            try:
                count = max(0, int(raw_count))
            except (TypeError, ValueError):
                continue
            count = min(count, remaining)
            if count <= 0:
                continue
            result[scope] = count
            remaining -= count
        return result

    @classmethod
    def _generation_prefix_scope_semantic_groups(
        cls,
        *,
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        available_scopes: list[str],
        scope_targets: dict[str, int],
    ) -> dict[str, tuple[_GenerationPrefixSemanticGroup, ...]]:
        """Read optional concept-family priorities for a configured prefix.

        Configuration is intentionally strict: one malformed group, an
        unknown key, or more groups than target slots disables the complete
        scope entry.  Callers then retain the established flat-term ordering.
        """

        raw_groups_by_scope = cls._config_value(
            agent_cfg,
            defaults,
            ("generation_prefix_scope_semantic_groups",),
            {},
        )
        if not isinstance(raw_groups_by_scope, dict):
            return {}

        available = set(available_scopes)
        parsed_by_scope: dict[
            str,
            tuple[_GenerationPrefixSemanticGroup, ...],
        ] = {}

        def parse_terms(
            value: Any,
            *,
            allow_empty: bool,
        ) -> tuple[str, ...] | None:
            if not isinstance(value, (list, tuple)):
                return None
            if any(
                not isinstance(term, str) or not term.strip()
                for term in value
            ):
                return None
            terms = tuple(dict.fromkeys(term.strip() for term in value))
            if not terms and not allow_empty:
                return None
            return terms

        for raw_scope, raw_groups in raw_groups_by_scope.items():
            scope = str(raw_scope or "").strip()
            target_count = int(scope_targets.get(scope, 0) or 0)
            if (
                not scope
                or scope not in available
                or target_count <= 0
                or not isinstance(raw_groups, (list, tuple))
                or not raw_groups
                or len(raw_groups) > target_count
            ):
                continue

            parsed_groups: list[_GenerationPrefixSemanticGroup] = []
            valid_scope = True
            observed_required_terms: set[str] = set()
            for raw_group in raw_groups:
                if (
                    not isinstance(raw_group, dict)
                    or set(raw_group)
                    - {
                        "required_terms",
                        "preferred_terms",
                        "downrank_terms",
                    }
                    or "required_terms" not in raw_group
                ):
                    valid_scope = False
                    break
                required_terms = parse_terms(
                    raw_group.get("required_terms"),
                    allow_empty=False,
                )
                preferred_terms = parse_terms(
                    raw_group.get("preferred_terms", []),
                    allow_empty=True,
                )
                downrank_terms = parse_terms(
                    raw_group.get("downrank_terms", []),
                    allow_empty=True,
                )
                if (
                    required_terms is None
                    or preferred_terms is None
                    or downrank_terms is None
                ):
                    valid_scope = False
                    break
                normalized_required_terms = {
                    normalize_content(term) for term in required_terms
                }
                if (
                    not normalized_required_terms
                    or observed_required_terms & normalized_required_terms
                ):
                    valid_scope = False
                    break
                observed_required_terms.update(normalized_required_terms)
                parsed_groups.append(
                    _GenerationPrefixSemanticGroup(
                        required_terms=required_terms,
                        preferred_terms=preferred_terms,
                        downrank_terms=downrank_terms,
                    )
                )
            if valid_scope and parsed_groups:
                parsed_by_scope[scope] = tuple(parsed_groups)

        return parsed_by_scope

    @classmethod
    def _generation_prefix_fill_scopes(
        cls,
        *,
        agent_name: str = "",
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        context: dict[str, Any] | None = None,
        active_skills: list[ActiveKnowledgeSkill] | None = None,
        available_scopes: list[str],
    ) -> tuple[str, ...]:
        raw_scopes = cls._config_value(
            agent_cfg,
            defaults,
            ("generation_prefix_fill_scopes",),
            (),
        )
        if not isinstance(raw_scopes, (list, tuple, set)):
            return ()
        raw_conditions = cls._config_value(
            agent_cfg,
            defaults,
            ("generation_prefix_scope_conditions",),
            {},
        )
        conditions = raw_conditions if isinstance(raw_conditions, dict) else {}
        available = set(available_scopes)
        return tuple(
            scope
            for scope in cls._normalize_scopes(raw_scopes)
            if scope in available
            and cls._generation_prefix_condition_met(
                condition=str(conditions.get(scope) or "").strip(),
                agent_name=agent_name,
                context=context or {},
                active_skills=active_skills or [],
            )
        )

    @classmethod
    def _generation_prefix_condition_met(
        cls,
        *,
        condition: str,
        agent_name: str,
        context: dict[str, Any],
        active_skills: list[ActiveKnowledgeSkill],
    ) -> bool:
        if condition == "company_values_or_culture_skill":
            return bool(context.get("company_value_terms")) or any(
                "culture" in active.skill.scopes for active in active_skills
            )
        if condition == "career_elements_applicable":
            return bool(context.get("career_elements_applicable"))
        if condition == "applicable_job_levels":
            return bool(
                cls._applicable_job_levels(
                    agent_name=agent_name,
                    context=context,
                )
            )
        return True

    @classmethod
    def _retrieval_scopes_for_context(
        cls,
        *,
        agent_name: str,
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        context: dict[str, Any],
        active_skills: list[ActiveKnowledgeSkill],
        available_scopes: list[str],
    ) -> list[str]:
        """Apply authoritative context conditions before search is executed."""

        raw_conditions = cls._config_value(
            agent_cfg,
            defaults,
            ("retrieval_scope_conditions",),
            {},
        )
        if not isinstance(raw_conditions, dict) or not raw_conditions:
            return list(available_scopes)
        return [
            scope
            for scope in available_scopes
            if cls._generation_prefix_condition_met(
                condition=str(raw_conditions.get(scope) or "").strip(),
                agent_name=agent_name,
                context=context,
                active_skills=active_skills,
            )
        ]

    @staticmethod
    def _needs_parent_context(agent_name: str) -> bool:
        normalized = str(agent_name or "").strip()
        return normalized.startswith("guidance_") or normalized.endswith(
            "_evaluation"
        )

    @staticmethod
    def _preferred_job_level_codes(
        scope_preferred_terms: dict[str, tuple[str, ...]] | None,
    ) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                str(term).upper()
                for term in (
                    (scope_preferred_terms or {}).get("job_level") or ()
                )
                if _JOB_LEVEL_CODE_PATTERN.fullmatch(str(term))
            )
        )

    def _hydrate_parent_contexts(
        self,
        chunks: list[RetrievedChunk],
        *,
        applicable_job_levels: tuple[str, ...] = (),
    ) -> list[RetrievedChunk]:
        fetcher = getattr(self.repository, "fetch_chunk_parent_contexts", None)
        if not chunks or not callable(fetcher):
            return chunks
        try:
            contexts = fetcher([chunk.chunk_id for chunk in chunks])
        except Exception as exc:  # noqa: BLE001
            log_metric(
                "rag.parent_context.error",
                chunk_count=len(chunks),
                error_type=type(exc).__name__,
            )
            return chunks
        if not contexts:
            return chunks
        settings = getattr(self, "settings", None)
        max_chars = int(
            getattr(settings, "kb_parent_context_max_chars", 12_000)
        )
        boundary_scan_chars = int(
            getattr(settings, "kb_parent_context_boundary_scan_chars", 800)
        )
        hydrated: list[RetrievedChunk] = []
        generation_context_owners: dict[str, str] = {}
        duplicate_generation_context_count = 0
        focused_generation_context_count = 0
        focused_generation_chars_saved = 0
        job_level_projection_count = 0
        split_count = 0
        missing_anchor_count = 0
        max_original_chars = 0
        max_selected_chars = 0
        for chunk in chunks:
            context = contexts.get(chunk.chunk_id)
            if not context:
                hydrated.append(chunk)
                continue
            metadata = chunk.metadata or {}
            parent_slice = select_parent_context(
                str(context.get("text") or ""),
                child_text=chunk.text,
                child_source_start_char=metadata.get("source_start_char"),
                child_source_end_char=metadata.get("source_end_char"),
                parent_source_start_char=context.get("source_start_char"),
                max_chars=max_chars,
                boundary_scan_chars=boundary_scan_chars,
            )
            split_count += int(parent_slice.split)
            missing_anchor_count += int(not parent_slice.anchor_found)
            max_original_chars = max(
                max_original_chars,
                parent_slice.original_chars,
            )
            max_selected_chars = max(
                max_selected_chars,
                len(parent_slice.text),
            )
            hydrated_metadata = {
                **metadata,
                "parent_context": parent_slice.text,
                "parent_context_split": parent_slice.split,
                "parent_context_anchor_found": parent_slice.anchor_found,
                "parent_context_original_chars": parent_slice.original_chars,
                "parent_context_offset_start": parent_slice.start_char,
                "parent_context_offset_end": parent_slice.end_char,
                "parent_section_title": str(context.get("title") or ""),
                "parent_heading_path": list(
                    context.get("heading_path") or []
                ),
                "parent_source_start_line": context.get(
                    "source_start_line"
                ),
                "parent_source_end_line": context.get("source_end_line"),
                "parent_source_start_char": context.get(
                    "source_start_char"
                ),
                "parent_source_end_char": context.get("source_end_char"),
            }
            job_level_generation_context = (
                select_job_level_generation_context(
                    parent_slice.text,
                    applicable_job_levels,
                )
                if chunk.scope == "job_level"
                and applicable_job_levels
                else ""
            )
            focused_generation_context = (
                job_level_generation_context
                or select_generation_context_focus(
                    parent_slice.text,
                    hydrated_metadata,
                )
            )
            if focused_generation_context:
                focused_generation_context_count += 1
                focused_generation_chars_saved += max(
                    0,
                    len(parent_slice.text) - len(focused_generation_context),
                )
                hydrated_metadata.update(
                    {
                        "generation_context_override": (
                            focused_generation_context
                        ),
                        "generation_context_focused": True,
                    }
                )
                if job_level_generation_context:
                    job_level_projection_count += 1
                    hydrated_metadata["generation_context_job_levels"] = list(
                        applicable_job_levels
                    )
            normalized_generation_context = normalize_content(
                focused_generation_context or parent_slice.text
            )
            generation_identity = (
                "\x1f".join(
                    (
                        normalize_content(chunk.scope or ""),
                        normalize_content(chunk.source_id or ""),
                        hashlib.sha256(
                            normalized_generation_context.encode("utf-8")
                        ).hexdigest(),
                    )
                )
                if normalized_generation_context
                else ""
            )
            duplicate_owner = (
                generation_context_owners.get(generation_identity)
                if generation_identity
                else None
            )
            if duplicate_owner:
                duplicate_generation_context_count += 1
                hydrated_metadata["generation_context_duplicate_of"] = (
                    duplicate_owner
                )
                # Paragraph focusing already produced a complete contiguous
                # evidence window.  Do not collapse it again to a tiny leaf.
                if not focused_generation_context:
                    retrieval_unit = metadata.get("retrieval_unit") or {}
                    focused_text = ""
                    if isinstance(retrieval_unit, dict):
                        focused_text = str(
                            retrieval_unit.get("text")
                            or retrieval_unit.get("search_text")
                            or ""
                        ).strip()
                    focused_text = (
                        focused_text or str(chunk.text or "").strip()
                    )
                    if focused_text:
                        hydrated_metadata["generation_context_override"] = (
                            focused_text
                        )
            elif generation_identity:
                generation_context_owners[generation_identity] = chunk.chunk_id

            hydrated.append(
                chunk.model_copy(
                    deep=True,
                    update={"metadata": hydrated_metadata},
                )
            )
        if split_count:
            log_metric(
                "rag.parent_context.split",
                chunk_count=len(chunks),
                split_count=split_count,
                missing_anchor_count=missing_anchor_count,
                max_original_chars=max_original_chars,
                max_selected_chars=max_selected_chars,
            )
        if duplicate_generation_context_count:
            log_metric(
                "rag.parent_context.generation_duplicate",
                chunk_count=len(chunks),
                duplicate_count=duplicate_generation_context_count,
            )
        if focused_generation_context_count:
            log_metric(
                "rag.parent_context.generation_focus",
                chunk_count=len(chunks),
                focused_count=focused_generation_context_count,
                generation_chars_saved=focused_generation_chars_saved,
            )
        if job_level_projection_count:
            log_metric(
                "rag.parent_context.job_level_projection",
                chunk_count=len(chunks),
                projected_count=job_level_projection_count,
                applicable_job_levels=list(applicable_job_levels),
            )
        return hydrated

    async def _ahydrate_parent_contexts(
        self,
        chunks: list[RetrievedChunk],
        *,
        admission_priority: int,
        applicable_job_levels: tuple[str, ...] = (),
    ) -> list[RetrievedChunk]:
        if not chunks or not callable(
            getattr(self.repository, "fetch_chunk_parent_contexts", None)
        ):
            return chunks
        loop = asyncio.get_running_loop()
        async with self._global_lease(
            "rag-db",
            "rag_global_db_search_max_concurrency",
            int(getattr(self.settings, "rag_db_search_max_concurrency", 1)),
            priority=admission_priority,
        ):
            return await loop.run_in_executor(
                self._executor,
                partial(
                    self._hydrate_parent_contexts,
                    chunks,
                    applicable_job_levels=applicable_job_levels,
                ),
            )

    @staticmethod
    def _config_value(
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        names: tuple[str, ...],
        fallback: Any,
    ) -> Any:
        for config in (agent_cfg, defaults):
            for name in names:
                if name in config and config[name] is not None:
                    return config[name]
        return fallback

    @classmethod
    def _resolve_scope_search_policies(
        cls,
        *,
        query_config: dict[str, Any],
        scopes: list[str],
        dense_top_k: int,
        lexical_top_k: int,
        exact_top_k: int,
        hybrid_enabled: bool,
        exact_enabled: bool,
    ) -> dict[str, _ScopeSearchPolicy]:
        raw_policies = query_config.get("scope_search_policies") or {}
        if not isinstance(raw_policies, dict):
            raise ValueError("query.yaml scope_search_policies must be an object")
        default_config = (
            raw_policies.get("default")
            or raw_policies.get("_default")
            or {}
        )
        if not isinstance(default_config, dict):
            raise ValueError(
                "query.yaml scope_search_policies.default must be an object"
            )
        nested_scopes = raw_policies.get("scopes") or {}
        if not isinstance(nested_scopes, dict):
            raise ValueError(
                "query.yaml scope_search_policies.scopes must be an object"
            )

        def positive_int(value: object, fallback: int) -> int:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                return max(1, int(fallback))

        def boolean(value: object, fallback: bool) -> bool:
            if value is None:
                return fallback
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

        resolved: dict[str, _ScopeSearchPolicy] = {}
        for scope in cls._normalize_scopes(scopes):
            scope_config = dict(default_config)
            for override in (
                nested_scopes.get(scope),
                raw_policies.get(scope),
            ):
                if override is None:
                    continue
                if not isinstance(override, dict):
                    raise ValueError(
                        "query.yaml scope_search_policies"
                        f".{scope} must be an object"
                    )
                scope_config.update(override)
            dense_mode = str(
                scope_config.get("dense_mode") or "exact"
            ).strip().lower()
            if dense_mode not in {"exact", "ann_preferred"}:
                raise ValueError(
                    "scope search dense_mode must be exact or ann_preferred: "
                    f"{scope}={dense_mode}"
                )
            resolved[scope] = _ScopeSearchPolicy(
                dense_mode=dense_mode,
                dense_top_k=positive_int(
                    scope_config.get("dense_top_k"), dense_top_k
                ),
                lexical_top_k=positive_int(
                    scope_config.get("lexical_top_k"), lexical_top_k
                ),
                exact_top_k=positive_int(
                    scope_config.get("exact_top_k"), exact_top_k
                ),
                hybrid_enabled=boolean(
                    scope_config.get(
                        "hybrid_search_enabled",
                        scope_config.get("hybrid_enabled"),
                    ),
                    hybrid_enabled,
                ),
                exact_enabled=boolean(
                    scope_config.get(
                        "exact_search_enabled",
                        scope_config.get("exact_enabled"),
                    ),
                    exact_enabled,
                ),
            )
        return resolved

    def _query_config(self) -> dict[str, Any]:
        snapshot = getattr(self.loader, "query_config_snapshot", None)
        if callable(snapshot):
            return snapshot()
        return self.loader.query_config()

    @classmethod
    def _config_int(
        cls,
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        names: tuple[str, ...],
        fallback: int,
    ) -> int:
        value = cls._config_value(agent_cfg, defaults, names, fallback)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return max(1, int(fallback))

    @classmethod
    def _config_float(
        cls,
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        names: tuple[str, ...],
        fallback: float,
    ) -> float:
        value = cls._config_value(agent_cfg, defaults, names, fallback)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = fallback
        return max(0.0, parsed)

    @classmethod
    def _config_compound_evidence_sets(
        cls,
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
    ) -> tuple[tuple[str, ...], ...]:
        raw_sets = cls._config_value(
            agent_cfg,
            defaults,
            ("rerank_compound_evidence_sets",),
            _DEFAULT_RERANK_COMPOUND_EVIDENCE_SETS,
        )
        if not raw_sets:
            return ()
        if not isinstance(raw_sets, (list, tuple)):
            raise ValueError(
                "rerank_compound_evidence_sets must be a list of term lists"
            )
        output: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for raw_set in raw_sets:
            if not isinstance(raw_set, (list, tuple)):
                raise ValueError(
                    "each rerank compound evidence set must be a term list"
                )
            evidence_set = tuple(
                dict.fromkeys(
                    str(term).strip()
                    for term in raw_set
                    if str(term).strip()
                )
            )
            if len(evidence_set) < 2:
                raise ValueError(
                    "each rerank compound evidence set needs at least two terms"
                )
            identity = tuple(normalize_content(term) for term in evidence_set)
            if identity in seen:
                continue
            output.append(evidence_set)
            seen.add(identity)
        return tuple(output)

    @staticmethod
    def _normalize_rerank_rank_weights(
        rerank_rank_weight: Any,
        first_stage_rank_weight: Any,
    ) -> tuple[float, float]:
        try:
            rerank_weight = float(rerank_rank_weight)
            first_stage_weight = float(first_stage_rank_weight)
        except (TypeError, ValueError):
            rerank_weight = _DEFAULT_RERANK_RANK_WEIGHT
            first_stage_weight = _DEFAULT_FIRST_STAGE_RANK_WEIGHT
        if (
            not math.isfinite(rerank_weight)
            or not math.isfinite(first_stage_weight)
            or rerank_weight < 0.0
            or first_stage_weight < 0.0
            or max(rerank_weight, first_stage_weight) <= 0.0
        ):
            rerank_weight = _DEFAULT_RERANK_RANK_WEIGHT
            first_stage_weight = _DEFAULT_FIRST_STAGE_RANK_WEIGHT

        # Keep the final score on the same scale as the previous 1:1 RRF
        # while allowing only the relative weighting to affect ordering. Scale
        # by the larger input first so two large finite values cannot overflow
        # while their ratio is normalized.
        largest_weight = max(rerank_weight, first_stage_weight)
        scaled_rerank_weight = rerank_weight / largest_weight
        scaled_first_stage_weight = first_stage_weight / largest_weight
        scale = 2.0 / (
            scaled_rerank_weight + scaled_first_stage_weight
        )
        return (
            scaled_rerank_weight * scale,
            scaled_first_stage_weight * scale,
        )

    @classmethod
    def _resolve_rerank_rank_weights(
        cls,
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
    ) -> tuple[float, float]:
        rerank_weight = cls._config_value(
            agent_cfg,
            defaults,
            ("rerank_rank_weight",),
            _DEFAULT_RERANK_RANK_WEIGHT,
        )
        first_stage_weight = cls._config_value(
            agent_cfg,
            defaults,
            ("first_stage_rank_weight",),
            _DEFAULT_FIRST_STAGE_RANK_WEIGHT,
        )
        return cls._normalize_rerank_rank_weights(
            rerank_weight,
            first_stage_weight,
        )

    @classmethod
    def _config_bool(
        cls,
        agent_cfg: dict[str, Any],
        defaults: dict[str, Any],
        names: tuple[str, ...],
        fallback: bool,
    ) -> bool:
        value = cls._config_value(agent_cfg, defaults, names, fallback)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _worker_count(
        job_count: int,
        configured: object,
        default: int,
        hard_limit: int | None = None,
    ) -> int:
        if job_count <= 0:
            return 1
        try:
            requested = int(configured or default)
        except (TypeError, ValueError):
            requested = default
        maximum = max(1, int(hard_limit)) if hard_limit is not None else job_count
        return max(1, min(job_count, max(1, requested), maximum))

    @staticmethod
    def _cache_hit_rate(hits: int, lookups: int) -> float | None:
        if lookups <= 0:
            return None
        return round(hits / lookups, 4)

    @staticmethod
    def _skill_metric_fields(
        active_skills: list[ActiveKnowledgeSkill],
    ) -> dict[str, object]:
        return {
            "knowledge_skill_active_count": len(active_skills),
            "knowledge_skill_ids": ",".join(
                active.skill.id for active in active_skills
            ),
            "knowledge_skill_stale_ids": ",".join(
                active.skill.id for active in active_skills if active.stale
            ),
            "knowledge_skill_query_count": sum(
                len(active.skill.retrieval_queries) for active in active_skills
            ),
            "knowledge_skill_core_chars": sum(
                len(active.core_knowledge) for active in active_skills
            ),
        }

    def _available_scopes(self, configured_scopes: list[str]) -> list[str]:
        indexed_scopes = self._indexed_scopes()
        if not indexed_scopes:
            indexed_scopes = self._raw_kb_scopes(self.settings.data_dir / "kb_raw")
            if self._raw_kb_scopes(self.settings.data_dir / "resources"):
                indexed_scopes.add("resources")
        return [scope for scope in configured_scopes if scope in indexed_scopes]

    def _result_cache_key(
        self,
        *,
        agent_name: str,
        query_specs: list[_RenderedQuerySpec],
        rerank_query: str,
        rerank_instruction: str | None,
        dense_top_k: int,
        lexical_top_k: int,
        rrf_k: int,
        fusion_top_n: int,
        rerank_candidate_top_n: int,
        final_top_k: int,
        rerank_enabled: bool,
        hybrid_enabled: bool,
        dense_weight: float,
        lexical_weight: float,
        metadata_filter: dict[str, Any],
        exact_top_k: int = 16,
        exact_weight: float = 1.25,
        exact_enabled: bool = True,
        active_skill_identities: list[dict[str, object]] | None = None,
        citation_scope_minimums: dict[str, int] | None = None,
        citation_scope_preferred_terms: (
            dict[str, tuple[str, ...]] | None
        ) = None,
        generation_prefix_scope_targets: dict[str, int] | None = None,
        generation_prefix_scope_semantic_groups: dict[
            str,
            tuple[_GenerationPrefixSemanticGroup, ...],
        ] | None = None,
        generation_prefix_fill_scopes: tuple[str, ...] | None = None,
        scope_search_policies: dict[str, _ScopeSearchPolicy] | None = None,
        rerank_units_per_parent: int = _DEFAULT_RERANK_UNITS_PER_PARENT,
        rerank_semantic_group_candidate_enabled: bool = (
            _DEFAULT_RERANK_SEMANTIC_GROUP_CANDIDATE_ENABLED
        ),
        rerank_compound_evidence_sets: tuple[tuple[str, ...], ...] = (
            _DEFAULT_RERANK_COMPOUND_EVIDENCE_SETS
        ),
        rerank_rank_fusion_k: int = _DEFAULT_RERANK_RANK_FUSION_K,
        rerank_rank_weight: float = _DEFAULT_RERANK_RANK_WEIGHT,
        first_stage_rank_weight: float = (
            _DEFAULT_FIRST_STAGE_RANK_WEIGHT
        ),
    ) -> str:
        profile_id, build_id = self._embedding_profile_cache_identity()
        payload = {
            "scope_selection_policy_version": (
                _SCOPE_SELECTION_POLICY_VERSION
            ),
            "agent_name": agent_name,
            "query_specs": [
                {
                    "query": spec.query,
                    "scopes": spec.scopes,
                    "weight": spec.weight,
                    "template_index": spec.template_index,
                }
                for spec in query_specs
            ],
            "rerank_query": rerank_query,
            "rerank_instruction": rerank_instruction,
            "hybrid_enabled": hybrid_enabled,
            "dense_top_k": dense_top_k,
            "lexical_top_k": lexical_top_k,
            "dense_weight": dense_weight,
            "lexical_weight": lexical_weight,
            "exact_top_k": exact_top_k,
            "exact_weight": exact_weight,
            "exact_enabled": exact_enabled,
            "rrf_k": rrf_k,
            "fusion_top_n": fusion_top_n,
            "rerank_candidate_top_n": rerank_candidate_top_n,
            "rerank_units_per_parent": rerank_units_per_parent,
            "rerank_semantic_group_candidate_enabled": (
                rerank_semantic_group_candidate_enabled
            ),
            "rerank_compound_evidence_sets": [
                list(evidence_set)
                for evidence_set in rerank_compound_evidence_sets
            ],
            "rerank_rank_fusion_k": rerank_rank_fusion_k,
            "rerank_rank_weight": rerank_rank_weight,
            "first_stage_rank_weight": first_stage_rank_weight,
            "final_top_k": final_top_k,
            "rerank_enabled": rerank_enabled,
            "metadata_filter": metadata_filter,
            "citation_scope_minimums": citation_scope_minimums or {},
            "citation_scope_preferred_terms": {
                scope: list(terms)
                for scope, terms in sorted(
                    (citation_scope_preferred_terms or {}).items()
                )
            },
            # Target declaration order is meaningful when the three-slot
            # generation prefix cannot cover every configured scope.
            "generation_prefix_scope_targets": [
                [scope, count]
                for scope, count in (
                    generation_prefix_scope_targets or {}
                ).items()
            ],
            "generation_prefix_scope_semantic_groups": {
                scope: [group.cache_identity() for group in groups]
                for scope, groups in sorted(
                    (
                        generation_prefix_scope_semantic_groups or {}
                    ).items()
                )
            },
            "generation_prefix_fill_scopes": list(
                generation_prefix_fill_scopes or ()
            ),
            "scope_search_policies": {
                scope: policy.cache_identity()
                for scope, policy in sorted(
                    (scope_search_policies or {}).items()
                )
            },
            "tokenizer_name": self.settings.postgres_bm25_tokenizer_name,
            "index_version": self.settings.kb_index_version,
            "embedding_profile_id": profile_id,
            "embedding_build_id": build_id,
            "rerank_provider": self.settings.effective_rerank_provider,
            "rerank_model": self.settings.effective_rerank_model,
            "rerank_endpoint": self.settings.rerank_url,
            "rerank_document_policy": Reranker.DOCUMENT_POLICY_VERSION,
            "active_skills": active_skill_identities or [],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _embedding_profile_cache_identity(self) -> tuple[str, str | None]:
        profile, _ = self._embedding_runtime_snapshot()
        if profile is not None:
            return profile.profile_id, profile.active_build_id
        configured_profile_id = getattr(
            self.settings,
            "embedding_profile_id",
            None,
        )
        if configured_profile_id:
            return str(configured_profile_id), None
        defaults = get_settings()
        return (
            embedding_profile_id(
                getattr(
                    self.settings,
                    "effective_embedding_model",
                    defaults.effective_embedding_model,
                ),
                getattr(
                    self.settings,
                    "effective_embedding_dimensions",
                    getattr(
                        self.settings,
                        "embedding_dimensions",
                        defaults.effective_embedding_dimensions,
                    ),
                ),
            ),
            None,
        )

    def _get_cached_result(self, cache_key: str) -> list[RetrievedChunk] | None:
        return self.result_cache.get(cache_key)

    def _cache_result(self, cache_key: str, chunks: list[RetrievedChunk]) -> None:
        self.result_cache.set(cache_key, chunks)

    @classmethod
    def clear_result_cache(cls) -> None:
        RetrievalResultCache().clear()

    def _indexed_scopes(self) -> set[str]:
        metadata = self.vector_repo.load()
        collections = metadata.get("collections") or {}
        indexed: set[str] = set()
        if not isinstance(collections, dict):
            return indexed
        for scope, info in collections.items():
            if not isinstance(info, dict):
                continue
            count = int(info.get("vector_count") or info.get("chunk_count") or 0)
            if count > 0:
                indexed.add(str(scope))
        return indexed

    @staticmethod
    def _raw_kb_scopes(raw_root: Path) -> set[str]:
        supported = {".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx"}
        if not raw_root.exists():
            return set()
        scopes: set[str] = set()
        for path in raw_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in supported:
                continue
            relative = path.relative_to(raw_root)
            scopes.add(relative.parts[0] if len(relative.parts) > 1 else "general")
        return scopes

    @staticmethod
    def _normalize_scopes(raw_scopes: list | tuple | set) -> list[str]:
        scopes: list[str] = []
        seen: set[str] = set()
        for raw in raw_scopes or []:
            scope = str(raw).strip()
            if scope and scope not in seen:
                scopes.append(scope)
                seen.add(scope)
        return scopes

    def _log_empty_result(
        self,
        *,
        started: float,
        agent_name: str,
        rendered_query_count: int,
        rerank_enabled: bool,
        search_job_count: int = 0,
        candidate_count: int = 0,
        embedding_hits: int = 0,
        embedding_lookups: int = 0,
        query_parallelism: int = 0,
        hybrid_enabled: bool = False,
        lexical_error_count: int = 0,
        semaphore_wait_ms: float = 0.0,
        active_skills: list[ActiveKnowledgeSkill] | None = None,
    ) -> None:
        log_metric(
            "rag.retrieve",
            agent_name=agent_name,
            rag_query_count=search_job_count,
            rendered_query_count=rendered_query_count,
            rag_candidate_count=candidate_count,
            rag_returned_count=0,
            rag_cache_hit=False,
            embedding_cache_hit=self._cache_hit_rate(
                embedding_hits,
                embedding_lookups,
            ),
            embedding_cache_lookup_count=embedding_lookups,
            rerank_enabled=rerank_enabled,
            hybrid_search_enabled=hybrid_enabled,
            rag_query_parallelism=query_parallelism,
            rag_lexical_error_count=lexical_error_count,
            rag_db_semaphore_wait_ms=round(semaphore_wait_ms, 2),
            rag_ms=elapsed_ms(started),
            **self._skill_metric_fields(active_skills or []),
        )

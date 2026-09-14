from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from backend.api.routes.health import capabilities
from backend.repositories.postgres_repository import (
    EmbeddingProfileState,
    RetrievalScopeCapability,
)
from backend.schemas.retrieval import RetrievedChunk
from backend.services.agentic_evidence_service import (
    AgenticEvidencePlanner,
    AgenticSearchConfig,
)
from backend.services.application_runtime import ApplicationRuntime
from backend.services.retrieval_service import RetrievalService
from backend.tools.rag_search_tool import RAG_TOOL_AGENT_NAME


def _profile(
    build_id: str,
    *,
    dimensions: int = 2,
    chunk_count: int = 10,
) -> EmbeddingProfileState:
    return EmbeddingProfileState(
        profile_id="profile-a",
        model_name="embedding-model",
        dimensions=dimensions,
        status="ready",
        active_build_id=build_id,
        source_fingerprint=f"fingerprint-{build_id}",
        chunk_count=chunk_count,
        vector_count=chunk_count,
        current_chunk_count=chunk_count,
    )


def _scope_capability(row_count: int) -> RetrievalScopeCapability:
    return RetrievalScopeCapability(
        collection_name="hr_agent_kb_general",
        scope="general",
        row_count=row_count,
        bm25_ready=True,
        ann_dimensions=frozenset({2}),
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        effective_embedding_provider="local_qwen",
        effective_embedding_model="embedding-model",
        effective_embedding_dimensions=2,
        embedding_dimensions=2,
        embedding_uses_local_runtime=True,
        effective_rerank_provider="local_qwen",
        effective_rerank_model="reranker-model",
        rerank_uses_local_runtime=True,
        tts_enabled=False,
        tts_provider="fish_vllm_omni",
        tts_model="tts-model",
        tts_model_revision="test-revision",
        tts_stage_0_max_num_seqs=8,
        tts_stage_1_max_num_seqs=4,
    )


class _Warmup:
    def snapshot(self) -> dict[str, object]:
        return {
            "status": "completed",
            "duration_ms": 10,
            "targets": [
                {"kind": "embedding", "status": "completed"},
                {"kind": "reranker", "status": "completed"},
            ],
        }


class _RetrievalRecorder:
    def __init__(self) -> None:
        self.calls: list[
            tuple[
                EmbeddingProfileState,
                dict[tuple[str, str], RetrievalScopeCapability],
            ]
        ] = []

    def set_embedding_profile(
        self,
        profile: EmbeddingProfileState,
        capabilities_snapshot: dict[
            tuple[str, str], RetrievalScopeCapability
        ],
    ) -> None:
        self.calls.append((profile, capabilities_snapshot))


def _runtime(
    *,
    profile: EmbeddingProfileState,
    repository: object,
    capability: RetrievalScopeCapability,
) -> tuple[ApplicationRuntime, _RetrievalRecorder]:
    runtime = object.__new__(ApplicationRuntime)
    runtime.settings = _settings()
    runtime.repository = repository
    runtime.embedding_profile = profile
    runtime.retrieval_capabilities = {
        (capability.collection_name, capability.scope): capability
    }
    runtime._embedding_capability_refresh_pending = not (
        runtime._retrieval_indexes_ready(
            profile,
            runtime.retrieval_capabilities,
        )
    )
    runtime._embedding_profile_refresh_lock = threading.Lock()
    retrieval = _RetrievalRecorder()
    runtime.retrieval_service = retrieval
    return runtime, retrieval


def test_runtime_refresh_switches_new_requests_and_health_to_activated_build():
    old_profile = _profile("build-old", chunk_count=10)
    new_profile = _profile("build-new", chunk_count=63_892)
    old_capability = _scope_capability(10)
    new_capability = _scope_capability(63_892)

    class Repository:
        def __init__(self) -> None:
            self.full_refresh_calls = 0

        @staticmethod
        def active_embedding_build_id(model_name: str, dimensions: int) -> str:
            assert model_name == "embedding-model"
            assert dimensions == 2
            return "build-new"

        def require_active_embedding_profile(
            self,
            model_name: str,
            dimensions: int,
        ) -> EmbeddingProfileState:
            assert model_name == "embedding-model"
            assert dimensions == 2
            self.full_refresh_calls += 1
            return new_profile

        @staticmethod
        def retrieval_capability_snapshot(
            profile: EmbeddingProfileState,
        ) -> dict[tuple[str, str], RetrievalScopeCapability]:
            assert profile is new_profile
            return {
                (new_capability.collection_name, new_capability.scope): (
                    new_capability
                )
            }

    repository = Repository()
    runtime, retrieval = _runtime(
        profile=old_profile,
        repository=repository,
        capability=old_capability,
    )

    assert runtime._refresh_embedding_profile_once() is True
    assert repository.full_refresh_calls == 1
    assert runtime.embedding_profile is new_profile
    assert runtime.retrieval_capabilities == retrieval.calls[0][1]
    assert retrieval.calls == [(new_profile, runtime.retrieval_capabilities)]

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(runtime=runtime, model_warmup=_Warmup())
        )
    )
    payload = capabilities(request)

    assert payload["embedding"]["active_build_id"] == "build-new"
    assert payload["embedding"]["profile_chunk_count"] == 63_892
    assert payload["embedding"]["profile_vector_count"] == 63_892
    assert payload["bm25"]["ready_scope_count"] == 1


def test_runtime_refresh_uses_lightweight_probe_and_noops_for_same_build():
    profile = _profile("build-current")
    capability = _scope_capability(10)

    class Repository:
        probe_calls = 0

        def active_embedding_build_id(
            self,
            model_name: str,
            dimensions: int,
        ) -> str:
            assert model_name == "embedding-model"
            assert dimensions == 2
            self.probe_calls += 1
            return "build-current"

        @staticmethod
        def require_active_embedding_profile(*args, **kwargs):
            raise AssertionError("unchanged builds must not load full profile state")

        @staticmethod
        def retrieval_capability_snapshot(*args, **kwargs):
            raise AssertionError("unchanged builds must not rescan capabilities")

    repository = Repository()
    runtime, retrieval = _runtime(
        profile=profile,
        repository=repository,
        capability=capability,
    )

    assert runtime._refresh_embedding_profile_once() is False
    assert runtime._refresh_embedding_profile_once() is False
    assert repository.probe_calls == 2
    assert retrieval.calls == []
    assert runtime.embedding_profile is profile


def test_runtime_rechecks_capabilities_until_large_scope_ann_is_ready():
    profile = _profile("build-current", chunk_count=20_000)
    pending_capability = RetrievalScopeCapability(
        collection_name="hr_agent_kb_organization_unit",
        scope="organization_unit",
        row_count=20_000,
        bm25_ready=True,
        ann_dimensions=frozenset(),
    )
    ready_capability = RetrievalScopeCapability(
        collection_name="hr_agent_kb_organization_unit",
        scope="organization_unit",
        row_count=20_000,
        bm25_ready=True,
        ann_dimensions=frozenset({2}),
    )
    capability_snapshots = iter(
        [pending_capability, ready_capability]
    )

    class Repository:
        @staticmethod
        def active_embedding_build_id(model_name: str, dimensions: int) -> str:
            return "build-current"

        @staticmethod
        def require_active_embedding_profile(*args, **kwargs):
            raise AssertionError("same build must not reload every vector count")

        @staticmethod
        def retrieval_capability_snapshot(
            candidate: EmbeddingProfileState,
        ) -> dict[tuple[str, str], RetrievalScopeCapability]:
            assert candidate is profile
            capability = next(capability_snapshots)
            return {
                (
                    capability.collection_name,
                    capability.scope,
                ): capability
            }

    runtime, retrieval = _runtime(
        profile=profile,
        repository=Repository(),
        capability=pending_capability,
    )

    assert runtime._embedding_capability_refresh_pending is True
    assert runtime._refresh_embedding_profile_once() is False
    assert runtime._embedding_capability_refresh_pending is True
    assert retrieval.calls == []
    assert runtime._refresh_embedding_profile_once() is True
    assert runtime._embedding_capability_refresh_pending is False
    assert retrieval.calls == [
        (profile, runtime.retrieval_capabilities)
    ]


def test_runtime_requires_ann_for_large_retrieval_unit_corpus():
    profile = _profile("build-current", chunk_count=900)
    runtime = object.__new__(ApplicationRuntime)
    runtime.settings = SimpleNamespace(
        rag_hybrid_search_enabled=True,
        postgres_create_hnsw_index=True,
        postgres_ann_min_rows=10_000,
    )
    pending = RetrievalScopeCapability(
        collection_name="hr_agent_kb_employee",
        scope="employee",
        row_count=900,
        bm25_ready=True,
        ann_dimensions=frozenset(),
        ann_required_dimensions=frozenset({2}),
    )
    ready = RetrievalScopeCapability(
        collection_name="hr_agent_kb_employee",
        scope="employee",
        row_count=900,
        bm25_ready=True,
        ann_dimensions=frozenset({2}),
        ann_required_dimensions=frozenset({2}),
    )

    assert runtime._retrieval_indexes_ready(
        profile,
        {(pending.collection_name, pending.scope): pending},
    ) is False
    assert runtime._retrieval_indexes_ready(
        profile,
        {(ready.collection_name, ready.scope): ready},
    ) is True


def test_runtime_refresh_rejects_incompatible_dimensions_without_mutation():
    old_profile = _profile("build-old")
    incompatible_profile = _profile("build-new", dimensions=3)
    old_capability = _scope_capability(10)

    class Repository:
        @staticmethod
        def active_embedding_build_id(model_name: str, dimensions: int) -> str:
            return "build-new"

        @staticmethod
        def require_active_embedding_profile(
            model_name: str,
            dimensions: int,
        ) -> EmbeddingProfileState:
            return incompatible_profile

        @staticmethod
        def retrieval_capability_snapshot(*args, **kwargs):
            raise AssertionError("invalid profiles must be rejected before rescan")

    runtime, retrieval = _runtime(
        profile=old_profile,
        repository=Repository(),
        capability=old_capability,
    )

    assert runtime._refresh_embedding_profile_once() is False
    assert runtime.embedding_profile is old_profile
    assert runtime.retrieval_capabilities == {
        (old_capability.collection_name, old_capability.scope): old_capability
    }
    assert retrieval.calls == []


def test_retrieval_profile_swap_recreates_stores_and_guards_dimensions():
    old_profile = _profile("build-old")
    new_profile = _profile("build-new")
    old_capability = _scope_capability(10)
    new_capability = _scope_capability(20)

    service = object.__new__(RetrievalService)
    service.embedding_profile = old_profile
    old_capabilities = {
        (old_capability.collection_name, old_capability.scope): old_capability
    }
    service._capabilities = old_capabilities
    service._embedding_runtime_state = (old_profile, old_capabilities)
    service._embedding_profile_lock = threading.Lock()
    old_store = SimpleNamespace(embedding_profile=old_profile)
    service._stores = {"hr_agent_kb_general": old_store}
    service._stores_lock = threading.Lock()
    service.embedding_service = object()
    service.repository = object()

    new_capabilities = {
        (new_capability.collection_name, new_capability.scope): new_capability
    }
    service.set_embedding_profile(new_profile, new_capabilities)

    assert service.embedding_profile is new_profile
    assert service._capabilities is new_capabilities
    assert service._stores == {}
    assert old_store.embedding_profile is old_profile

    new_store = service._store_for_collection("hr_agent_kb_general")
    assert new_store.embedding_profile is new_profile
    assert new_store._active_embedding_identity(2) == (
        "profile-a",
        "build-new",
    )
    with pytest.raises(ValueError, match="dimension"):
        new_store._active_embedding_identity(3)

    incompatible_profile = _profile("build-bad", dimensions=3)
    with pytest.raises(ValueError, match="dimension"):
        service.set_embedding_profile(incompatible_profile, new_capabilities)
    assert service.embedding_profile is new_profile
    assert service._stores["hr_agent_kb_general"] is new_store


def test_profile_switch_waits_for_old_generation_before_admitting_new_reader():
    old_profile = _profile("build-old")
    new_profile = _profile("build-new")
    capability = _scope_capability(10)
    capabilities_snapshot = {
        (capability.collection_name, capability.scope): capability
    }

    service = object.__new__(RetrievalService)
    service.embedding_profile = old_profile
    service._capabilities = capabilities_snapshot
    service._embedding_runtime_state = (old_profile, capabilities_snapshot)
    service._embedding_generation_condition = threading.Condition()
    service._embedding_active_retrievals = 0
    service._embedding_profile_switch_pending = False
    service._stores = {}
    service._stores_lock = threading.Lock()

    service._begin_embedding_generation_use()
    writer_done = threading.Event()

    def switch_profile() -> None:
        service.set_embedding_profile(new_profile, capabilities_snapshot)
        writer_done.set()

    writer = threading.Thread(target=switch_profile)
    writer.start()
    deadline = time.monotonic() + 1
    while not service._embedding_profile_switch_pending:
        assert time.monotonic() < deadline
        time.sleep(0.001)

    admitted_profile: list[EmbeddingProfileState] = []
    reader_done = threading.Event()

    def enter_new_reader() -> None:
        service._begin_embedding_generation_use()
        try:
            admitted_profile.append(service.embedding_profile)
        finally:
            service._end_embedding_generation_use()
            reader_done.set()

    reader = threading.Thread(target=enter_new_reader)
    reader.start()
    assert reader_done.wait(0.05) is False
    assert service.embedding_profile is old_profile

    service._end_embedding_generation_use()
    assert writer_done.wait(1)
    assert reader_done.wait(1)
    writer.join(timeout=1)
    reader.join(timeout=1)

    assert admitted_profile == [new_profile]
    assert service._embedding_active_retrievals == 0


def test_retrieval_and_agentic_cache_identities_change_with_active_build():
    old_profile = _profile("build-old")
    new_profile = _profile("build-new")
    capability = _scope_capability(10)
    capabilities_snapshot = {
        (capability.collection_name, capability.scope): capability
    }

    service = object.__new__(RetrievalService)
    service.embedding_profile = old_profile
    service._capabilities = capabilities_snapshot
    service._embedding_runtime_state = (old_profile, capabilities_snapshot)
    service._embedding_profile_lock = threading.Lock()
    service._stores = {}
    service._stores_lock = threading.Lock()
    service.loader = SimpleNamespace(
        query_config=lambda: {
            "version": "test-v1",
            "defaults": {},
            "queries": {
                RAG_TOOL_AGENT_NAME: {"scopes": ["general"]},
            },
            "scope_search_policies": {},
        }
    )

    class PlannerSettings:
        chat_url = "https://model.example/chat"
        llm_provider = "bosch_openai_compatible"
        kb_index_version = "v3"
        effective_embedding_provider = "local_qwen"
        effective_embedding_model = "embedding-model"
        effective_rerank_provider = "local_qwen"
        effective_rerank_model = "reranker-model"

        @staticmethod
        def model_for_task(task_name: str) -> str:
            return f"model-{task_name}"

    planner = object.__new__(AgenticEvidencePlanner)
    planner.settings = PlannerSettings()
    planner.retrieval = service
    planner.config = AgenticSearchConfig()
    initial_chunks = [
        RetrievedChunk(
            chunk_id="chunk-1",
            source_id="source-1",
            title="Evidence",
            scope="general",
            text="Leadership feedback evidence",
            score=0.9,
        )
    ]
    key_arguments = {
        "flow": "guidance",
        "dimension": "opening",
        "retrieval_name": "guidance_opening",
        "objective": "prepare feedback",
        "context": {"intent": "development"},
        "allowed_scopes": ["general"],
        "initial_chunks": initial_chunks,
    }

    old_identity = service.retrieval_config_identity(RAG_TOOL_AGENT_NAME)
    old_agentic_key = planner._cache_key(**key_arguments)

    service.set_embedding_profile(new_profile, capabilities_snapshot)

    new_identity = service.retrieval_config_identity(RAG_TOOL_AGENT_NAME)
    new_agentic_key = planner._cache_key(**key_arguments)

    assert old_identity["embedding_profile_id"] == "profile-a"
    assert old_identity["embedding_build_id"] == "build-old"
    assert new_identity["embedding_profile_id"] == "profile-a"
    assert new_identity["embedding_build_id"] == "build-new"
    assert new_identity != old_identity
    assert new_agentic_key != old_agentic_key

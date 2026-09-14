from types import SimpleNamespace

from backend.api.routes.health import capabilities
from backend.repositories.postgres_repository import RetrievalScopeCapability


class _Warmup:
    def snapshot(self):
        return {
            "status": "completed",
            "duration_ms": 1250,
            "targets": [
                {"kind": "chat", "status": "completed"},
                {"kind": "embedding", "status": "completed"},
                {"kind": "reranker", "status": "completed"},
                {"kind": "asr_realtime", "status": "completed"},
                {"kind": "tts", "status": "completed"},
            ],
        }


def test_capabilities_reports_runtime_retrieval_and_model_statuses():
    retrieval = RetrievalScopeCapability(
        collection_name="hr_agent_kb_performance",
        scope="performance",
        row_count=12,
        bm25_ready=True,
        ann_dimensions=frozenset(),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                runtime=SimpleNamespace(
                    retrieval_capabilities={
                        (retrieval.collection_name, retrieval.scope): retrieval
                    },
                    embedding_profile=SimpleNamespace(
                        profile_id="profile-a",
                        active_build_id="build-a",
                        status="ready",
                        ready=True,
                        current_chunk_count=827,
                        vector_count=827,
                    ),
                    settings=SimpleNamespace(
                        effective_embedding_provider="bosch",
                        effective_embedding_model="embedding-model",
                        embedding_dimensions=1024,
                        embedding_uses_local_runtime=False,
                        effective_rerank_provider="local_qwen",
                        effective_rerank_model="rerank-model",
                        rerank_uses_local_runtime=True,
                        tts_enabled=True,
                        tts_provider="fish_vllm_omni",
                        tts_model="/app/checkpoints/s2-pro",
                        tts_model_revision="test-revision",
                        tts_stage_0_max_num_seqs=8,
                        tts_stage_1_max_num_seqs=4,
                    ),
                ),
                model_warmup=_Warmup(),
            )
        )
    )

    payload = capabilities(request)

    assert payload["status"] == "ok"
    assert payload["database"] == {"status": "ready"}
    assert payload["bm25"] == {
        "status": "ready",
        "scope_count": 1,
        "ready_scope_count": 1,
        "missing_scopes": [],
    }
    assert payload["embedding"] == {
        "status": "ready",
        "target_count": 1,
        "provider": "bosch",
        "model": "embedding-model",
        "dimensions": 1024,
        "runtime": "api",
        "profile_id": "profile-a",
        "active_build_id": "build-a",
        "profile_status": "ready",
        "profile_ready": True,
        "profile_chunk_count": 827,
        "profile_vector_count": 827,
    }
    assert payload["reranker"] == {
        "status": "ready",
        "target_count": 1,
        "provider": "local_qwen",
        "model": "rerank-model",
        "runtime": "local",
    }
    assert payload["asr"] == {
        "status": "ready",
        "target_count": 1,
        "enabled": True,
        "provider": "local",
        "model": "Qwen/Qwen3-ASR-1.7B",
        "runtime": "local",
    }
    assert payload["remote_model"] == {"status": "ready", "target_count": 1}
    assert payload["tts"] == {
        "status": "ready",
        "target_count": 1,
        "enabled": True,
        "provider": "fish_vllm_omni",
        "model": "/app/checkpoints/s2-pro",
        "model_revision": "test-revision",
        "stage_0_max_num_seqs": 8,
        "stage_1_max_num_seqs": 4,
    }
    assert payload["model_warmup"] == {
        "status": "completed",
        "duration_ms": 1250,
    }


def test_capabilities_reports_intentionally_disabled_tts_without_degradation():
    retrieval = RetrievalScopeCapability(
        collection_name="hr_agent_kb_performance",
        scope="performance",
        row_count=12,
        bm25_ready=True,
        ann_dimensions=frozenset(),
    )
    settings = SimpleNamespace(
        effective_embedding_provider="bosch",
        effective_embedding_model="embedding-model",
        embedding_dimensions=1024,
        embedding_uses_local_runtime=False,
        effective_rerank_provider="bosch",
        effective_rerank_model="rerank-model",
        rerank_uses_local_runtime=False,
        tts_enabled=False,
        tts_provider="fish_vllm_omni",
        tts_model="/app/checkpoints/s2-pro",
        tts_model_revision="test-revision",
        tts_stage_0_max_num_seqs=8,
        tts_stage_1_max_num_seqs=4,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                runtime=SimpleNamespace(
                    retrieval_capabilities={
                        (retrieval.collection_name, retrieval.scope): retrieval
                    },
                    settings=settings,
                ),
                model_warmup=_Warmup(),
            )
        )
    )

    payload = capabilities(request)

    assert payload["status"] == "ok"
    assert payload["tts"] == {
        "status": "disabled",
        "target_count": 0,
        "enabled": False,
        "provider": "fish_vllm_omni",
        "model": "/app/checkpoints/s2-pro",
        "model_revision": "test-revision",
        "stage_0_max_num_seqs": 8,
        "stage_1_max_num_seqs": 4,
    }


def test_capabilities_reports_degraded_bm25_and_failed_model_without_errors():
    retrieval = RetrievalScopeCapability(
        collection_name="hr_agent_kb_performance",
        scope="performance",
        row_count=12,
        bm25_ready=False,
        ann_dimensions=frozenset(),
    )
    warmup = SimpleNamespace(
        snapshot=lambda: {
            "status": "completed_with_errors",
            "duration_ms": 900,
            "targets": [
                {
                    "kind": "embedding",
                    "status": "failed",
                    "error": "secret endpoint details",
                }
            ],
        }
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                runtime=SimpleNamespace(
                    retrieval_capabilities={
                        (retrieval.collection_name, retrieval.scope): retrieval
                    }
                ),
                model_warmup=warmup,
            )
        )
    )

    payload = capabilities(request)

    assert payload["status"] == "degraded"
    assert payload["bm25"]["missing_scopes"] == ["performance"]
    assert payload["embedding"] == {"status": "degraded", "target_count": 1}
    assert "secret endpoint details" not in str(payload)

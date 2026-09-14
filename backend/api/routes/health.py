from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Depends, Request

from backend.api.dependencies import get_app_settings
from backend.config.settings import Settings
from backend.observability.user_activity_middleware import active_workload_tracker
from backend.vectorstore.embedding_profile import resolve_embedding_dimensions

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health(settings: Settings = Depends(get_app_settings)) -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }


@router.get("/autoscaling")
def autoscaling_workload() -> dict[str, int | float]:
    """Return only process-local connection counts used for backend scaling."""

    return active_workload_tracker.snapshot()


def _warmup_capability(
    targets: list[dict[str, Any]],
    kinds: Iterable[str],
) -> dict[str, Any]:
    accepted_kinds = set(kinds)
    matched = [target for target in targets if target.get("kind") in accepted_kinds]
    statuses = {str(target.get("status") or "unknown") for target in matched}
    if not matched:
        status = "not_configured"
    elif statuses & {"failed", "cancelled"}:
        status = "degraded"
    elif statuses & {"pending", "running"}:
        status = "warming"
    elif statuses == {"completed"}:
        status = "ready"
    else:
        status = "unknown"
    return {"status": status, "target_count": len(matched)}


@router.get("/capabilities")
def capabilities(request: Request) -> dict[str, Any]:
    runtime = getattr(request.app.state, "runtime", None)
    warmup_service = getattr(request.app.state, "model_warmup", None)
    warmup_snapshot = (
        warmup_service.snapshot()
        if warmup_service is not None
        else {"status": "unavailable", "targets": []}
    )
    targets = list(warmup_snapshot.get("targets") or [])

    retrieval_capabilities = (
        list(runtime.retrieval_capabilities.values())
        if runtime is not None
        else []
    )
    scope_count = len(retrieval_capabilities)
    bm25_ready_count = sum(item.bm25_ready for item in retrieval_capabilities)
    bm25_missing_scopes = sorted(
        item.scope for item in retrieval_capabilities if not item.bm25_ready
    )
    database_status = "ready" if runtime is not None else "starting"
    bm25_status = (
        "ready"
        if scope_count > 0 and bm25_ready_count == scope_count
        else "degraded"
    )

    service_capabilities = {
        "embedding": _warmup_capability(targets, {"embedding"}),
        "reranker": _warmup_capability(targets, {"reranker"}),
        "asr": _warmup_capability(targets, {"asr_realtime", "asr_http"}),
        "remote_model": _warmup_capability(targets, {"chat"}),
        "tts": _warmup_capability(targets, {"tts"}),
    }
    runtime_settings = getattr(runtime, "settings", None)
    embedding_profile = getattr(runtime, "embedding_profile", None)
    if runtime_settings is not None:
        service_capabilities["embedding"].update(
            {
                "provider": runtime_settings.effective_embedding_provider,
                "model": runtime_settings.effective_embedding_model,
                "dimensions": resolve_embedding_dimensions(runtime_settings),
                "runtime": (
                    "local"
                    if runtime_settings.embedding_uses_local_runtime
                    else "api"
                ),
            }
        )
        if embedding_profile is not None:
            service_capabilities["embedding"].update(
                {
                    "profile_id": embedding_profile.profile_id,
                    "active_build_id": embedding_profile.active_build_id,
                    "profile_status": embedding_profile.status,
                    "profile_ready": embedding_profile.ready,
                    "profile_chunk_count": (
                        embedding_profile.current_chunk_count
                    ),
                    "profile_vector_count": (
                        embedding_profile.vector_count
                    ),
                }
            )
        service_capabilities["reranker"].update(
            {
                "provider": runtime_settings.effective_rerank_provider,
                "model": runtime_settings.effective_rerank_model,
                "runtime": (
                    "local"
                    if runtime_settings.rerank_uses_local_runtime
                    else "api"
                ),
            }
        )
        asr_enabled = bool(getattr(runtime_settings, "asr_enabled", True))
        asr_provider = str(
            getattr(runtime_settings, "asr_provider_mode", "local")
        )
        if not asr_enabled:
            service_capabilities["asr"] = {
                "status": "disabled",
                "target_count": 0,
            }
        elif asr_provider == "browser":
            service_capabilities["asr"] = {
                "status": "ready",
                "target_count": 0,
            }
        service_capabilities["asr"].update(
            {
                "enabled": asr_enabled,
                "provider": asr_provider,
                "model": (
                    getattr(
                        runtime_settings,
                        "asr_realtime_model",
                        "Qwen/Qwen3-ASR-1.7B",
                    )
                    if asr_provider == "local"
                    else (
                        "browser-web-speech"
                        if asr_provider == "browser"
                        else getattr(
                            runtime_settings,
                            "asr_http_model",
                            "qwen3-asr-flash",
                        )
                    )
                ),
                "runtime": (
                    "local"
                    if asr_provider == "local"
                    else asr_provider
                ),
            }
        )
        if not runtime_settings.tts_enabled:
            service_capabilities["tts"] = {
                "status": "disabled",
                "target_count": 0,
            }
        service_capabilities["tts"].update(
            {
                "enabled": runtime_settings.tts_enabled,
                "provider": runtime_settings.tts_provider,
                "model": runtime_settings.tts_model,
                "model_revision": runtime_settings.tts_model_revision,
                "stage_0_max_num_seqs": runtime_settings.tts_stage_0_max_num_seqs,
                "stage_1_max_num_seqs": runtime_settings.tts_stage_1_max_num_seqs,
            }
        )
    service_statuses = {
        capability["status"] for capability in service_capabilities.values()
    }
    if (
        database_status != "ready"
        or bm25_status == "degraded"
        or "degraded" in service_statuses
    ):
        overall_status = "degraded"
    elif (
        warmup_snapshot.get("status") in {"pending", "running"}
        or "warming" in service_statuses
    ):
        overall_status = "warming"
    else:
        overall_status = "ok"

    return {
        "status": overall_status,
        "database": {"status": database_status},
        "bm25": {
            "status": bm25_status,
            "scope_count": scope_count,
            "ready_scope_count": bm25_ready_count,
            "missing_scopes": bm25_missing_scopes,
        },
        **service_capabilities,
        "model_warmup": {
            "status": str(warmup_snapshot.get("status") or "unknown"),
            "duration_ms": warmup_snapshot.get("duration_ms"),
        },
    }

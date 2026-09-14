from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


RAG_CONCURRENCY_GRID = (16, 32, 48, 64, 98, 0)
RERANK_CONCURRENCY_GRID = (4, 6, 7, 8)
MODEL_CONCURRENCY_GRID = (16, 24, 32, 40, 0)


def select_concurrency_profile(
    results: Iterable[Mapping[str, Any]],
    *,
    workflow_db_workers: int = 8,
    throughput_tolerance: float = 0.05,
    postgres_pool_cap: int = 128,
) -> dict[str, Any]:
    """Select the lowest-P95 zero-error profile within the throughput tolerance."""

    rows = [dict(row) for row in results]
    successful = [
        row
        for row in rows
        if float(row.get("error_rate", 0.0)) == 0.0
        and int(row.get("error_count", 0)) == 0
        and float(row.get("throughput", 0.0)) > 0.0
    ]
    if not successful:
        raise ValueError("No zero-error benchmark profile with positive throughput.")

    best_throughput = max(float(row["throughput"]) for row in successful)
    tolerance = min(1.0, max(0.0, float(throughput_tolerance)))
    throughput_floor = best_throughput * (1.0 - tolerance)
    eligible = [
        row
        for row in successful
        if float(row["throughput"]) >= throughput_floor
    ]

    def sort_key(row: Mapping[str, Any]) -> tuple[float, float, int, int, int]:
        rag_limit = int(row.get("rag_concurrency", 0))
        rag_capacity_rank = rag_limit if rag_limit > 0 else 1_000_000
        model_limit = int(row.get("model_concurrency", 0))
        model_capacity_rank = model_limit if model_limit > 0 else 1_000_000
        return (
            float(row["p95_ms"]),
            -float(row["throughput"]),
            rag_capacity_rank,
            int(row["rerank_concurrency"]),
            model_capacity_rank,
        )

    selected = min(eligible, key=sort_key)
    rag_concurrency = int(selected["rag_concurrency"])
    rag_thread_pool_workers = rag_concurrency if rag_concurrency > 0 else 98
    workflow_workers = max(1, int(workflow_db_workers))
    pool_size = min(
        max(1, int(postgres_pool_cap)),
        rag_thread_pool_workers + workflow_workers + 16,
    )
    return {
        **selected,
        "best_throughput": best_throughput,
        "throughput_floor": throughput_floor,
        "throughput_tolerance": tolerance,
        "rag_thread_pool_workers": rag_thread_pool_workers,
        "workflow_db_workers": workflow_workers,
        "postgres_pool_max_size": pool_size,
    }


def environment_overrides(profile: Mapping[str, Any]) -> dict[str, str]:
    rag_admission_limit = int(profile["rag_concurrency"])
    rag_thread_pool_workers = int(
        profile.get("rag_thread_pool_workers")
        or (rag_admission_limit if rag_admission_limit > 0 else 98)
    )
    return {
        "RAG_DB_SEARCH_MAX_CONCURRENCY": str(rag_admission_limit),
        "RAG_THREAD_POOL_MAX_WORKERS": str(rag_thread_pool_workers),
        "RAG_RERANK_MAX_CONCURRENCY": str(int(profile["rerank_concurrency"])),
        "RAG_GLOBAL_RERANK_MAX_CONCURRENCY": str(
            int(profile["rerank_concurrency"])
        ),
        "MODEL_TOTAL_MAX_CONCURRENCY": str(int(profile["model_concurrency"])),
        "WORKFLOW_DB_THREAD_POOL_MAX_WORKERS": str(
            int(profile["workflow_db_workers"])
        ),
        "POSTGRES_POOL_MAX_SIZE": str(int(profile["postgres_pool_max_size"])),
    }

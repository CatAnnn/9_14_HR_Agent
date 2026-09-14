from __future__ import annotations

from pathlib import Path

import pytest

from backend.config.settings import Settings
from backend.services.concurrency_tuning import (
    MODEL_CONCURRENCY_GRID,
    RAG_CONCURRENCY_GRID,
    RERANK_CONCURRENCY_GRID,
    environment_overrides,
    select_concurrency_profile,
)


def test_concurrency_grids_and_runtime_defaults_match_the_tuning_contract():
    assert RAG_CONCURRENCY_GRID == (16, 32, 48, 64, 98, 0)
    assert RERANK_CONCURRENCY_GRID == (4, 6, 7, 8)
    assert MODEL_CONCURRENCY_GRID == (16, 24, 32, 40, 0)
    assert Settings.model_fields["workflow_db_thread_pool_max_workers"].default == 16
    assert Settings.model_fields["postgres_pool_max_size"].default == 122
    assert Settings.model_fields["redis_max_connections"].default == 0
    assert Settings.model_fields["embedding_api_batch_size"].default == 46
    assert Settings.model_fields["auth_max_active_sessions"].default == 0
    assert Settings.model_fields["rag_db_search_max_concurrency"].default == 0
    assert Settings.model_fields["rag_rerank_max_concurrency"].default == 4
    assert Settings.model_fields["rag_global_db_search_max_concurrency"].default == 0
    assert Settings.model_fields["rag_global_rerank_max_concurrency"].default == 4
    assert Settings.model_fields["workflow_global_db_max_concurrency"].default == 64


def test_elastic_backend_pgbouncer_and_nginx_contract_is_pinned() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deployment/compose/compose.services.yml").read_text(
        encoding="utf-8"
    )
    pgbouncer = (root / "deployment/pgbouncer/pgbouncer.ini").read_text(
        encoding="utf-8"
    )
    nginx = (root / "frontend/nginx.conf").read_text(encoding="utf-8")

    assert "replicas: ${BACKEND_REPLICAS:-2}" in compose
    assert 'BACKEND_AUTOSCALER_MIN_REPLICAS: "${BACKEND_AUTOSCALER_MIN_REPLICAS:-2}"' in compose
    assert 'BACKEND_AUTOSCALER_MAX_REPLICAS: "${BACKEND_AUTOSCALER_MAX_REPLICAS:-8}"' in compose
    assert 'BACKEND_AUTOSCALER_START_HEALTH_TIMEOUT_SECONDS: "${BACKEND_AUTOSCALER_START_HEALTH_TIMEOUT_SECONDS:-120}"' in compose
    assert 'BACKEND_AUTOSCALER_SCALE_DOWN_PROBE_INTERVAL_SECONDS: "${BACKEND_AUTOSCALER_SCALE_DOWN_PROBE_INTERVAL_SECONDS:-1}"' in compose
    assert 'BACKEND_AUTOSCALER_RECONCILE_LEASE_SECONDS: "${BACKEND_AUTOSCALER_RECONCILE_LEASE_SECONDS:-1500}"' in compose
    assert 'BACKEND_AUTOSCALER_WATCHDOG_STALE_SECONDS: "${BACKEND_AUTOSCALER_WATCHDOG_STALE_SECONDS:-30}"' in compose
    assert "stop_grace_period: 420s" in compose
    assert "REDIS_MAX_CONNECTIONS:-0" in compose
    assert "EMBEDDING_API_BATCH_SIZE:-46" in compose
    assert "RAG_DB_SEARCH_MAX_CONCURRENCY:-0" in compose
    assert "RAG_GLOBAL_DB_SEARCH_MAX_CONCURRENCY:-0" in compose
    assert "RAG_RERANK_MAX_CONCURRENCY:-4" in compose
    assert "RAG_GLOBAL_RERANK_MAX_CONCURRENCY:-4" in compose
    assert "AUTH_MAX_ACTIVE_SESSIONS:-0" in compose
    assert "WORKFLOW_GLOBAL_DB_MAX_CONCURRENCY:-64" in compose
    assert "percona/percona-pgbouncer:1.25.2-4-amd64@sha256:" in compose
    assert 'entrypoint: ["/usr/bin/pgbouncer"]' in compose
    assert "bash -c 'exec 3<>/dev/tcp/127.0.0.1/6432'" in compose
    assert "pool_mode = transaction" in pgbouncer
    assert "max_client_conn = 4000" in pgbouncer
    assert "default_pool_size = 256" in pgbouncer
    assert "reserve_pool_size = 64" in pgbouncer
    assert "max_db_connections = 320" in pgbouncer
    assert "map $cookie_hragent_session $backend_affinity_key" in nginx
    assert "hash $backend_affinity_key consistent;" in nginx
    assert "least_conn;" not in nginx
    assert "server backend:7111 resolve;" in nginx
    assert "proxy_next_upstream error timeout http_502 http_503 http_504;" in nginx
    assert "proxy_next_upstream_tries 3;" in nginx
    assert "proxy_connect_timeout 86400s;" not in nginx
    assert nginx.count("proxy_connect_timeout 15s;") == 4


def test_selects_lowest_p95_zero_error_profile_within_five_percent_throughput():
    profile = select_concurrency_profile(
        [
            {
                "rag_concurrency": 32,
                "rerank_concurrency": 16,
                "model_concurrency": 24,
                "throughput": 100.0,
                "p95_ms": 900.0,
                "error_rate": 0.0,
            },
            {
                "rag_concurrency": 48,
                "rerank_concurrency": 24,
                "model_concurrency": 32,
                "throughput": 96.0,
                "p95_ms": 700.0,
                "error_rate": 0.0,
            },
            {
                "rag_concurrency": 64,
                "rerank_concurrency": 32,
                "model_concurrency": 40,
                "throughput": 101.0,
                "p95_ms": 500.0,
                "error_rate": 0.01,
            },
            {
                "rag_concurrency": 16,
                "rerank_concurrency": 8,
                "model_concurrency": 16,
                "throughput": 90.0,
                "p95_ms": 400.0,
                "error_rate": 0.0,
            },
        ],
        workflow_db_workers=8,
    )

    assert profile["rag_concurrency"] == 48
    assert profile["rerank_concurrency"] == 24
    assert profile["model_concurrency"] == 32
    assert profile["postgres_pool_max_size"] == 72
    assert environment_overrides(profile) == {
        "RAG_DB_SEARCH_MAX_CONCURRENCY": "48",
        "RAG_THREAD_POOL_MAX_WORKERS": "48",
        "RAG_RERANK_MAX_CONCURRENCY": "24",
        "RAG_GLOBAL_RERANK_MAX_CONCURRENCY": "24",
        "MODEL_TOTAL_MAX_CONCURRENCY": "32",
        "WORKFLOW_DB_THREAD_POOL_MAX_WORKERS": "8",
        "POSTGRES_POOL_MAX_SIZE": "72",
    }


def test_postgres_pool_recommendation_is_capped_at_128():
    profile = select_concurrency_profile(
        [
            {
                "rag_concurrency": 120,
                "rerank_concurrency": 32,
                "model_concurrency": 40,
                "throughput": 1.0,
                "p95_ms": 1.0,
                "error_rate": 0.0,
            }
        ],
        workflow_db_workers=16,
    )

    assert profile["postgres_pool_max_size"] == 128


def test_unlimited_rag_admission_keeps_a_finite_worker_pool():
    profile = select_concurrency_profile(
        [
            {
                "rag_concurrency": 0,
                "rerank_concurrency": 98,
                "model_concurrency": 0,
                "throughput": 1.0,
                "p95_ms": 1.0,
                "error_rate": 0.0,
            }
        ],
        workflow_db_workers=8,
    )

    assert profile["rag_concurrency"] == 0
    assert profile["rag_thread_pool_workers"] == 98
    assert profile["postgres_pool_max_size"] == 122
    assert environment_overrides(profile)["RAG_DB_SEARCH_MAX_CONCURRENCY"] == "0"
    assert environment_overrides(profile)["RAG_THREAD_POOL_MAX_WORKERS"] == "98"


def test_selection_rejects_profiles_with_errors_or_zero_throughput():
    with pytest.raises(ValueError, match="No zero-error benchmark profile"):
        select_concurrency_profile(
            [
                {
                    "rag_concurrency": 32,
                    "rerank_concurrency": 16,
                    "model_concurrency": 24,
                    "throughput": 0.0,
                    "p95_ms": 1.0,
                    "error_rate": 0.0,
                },
                {
                    "rag_concurrency": 48,
                    "rerank_concurrency": 24,
                    "model_concurrency": 32,
                    "throughput": 10.0,
                    "p95_ms": 1.0,
                    "error_count": 1,
                },
            ]
        )

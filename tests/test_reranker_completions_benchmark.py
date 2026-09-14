from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from backend.services.rerank_service import RerankService
from scripts.benchmark_reranker_completions import (
    RequestObservation,
    build_documents,
    build_payload,
    build_payload_service,
    completion_url,
    percentile,
    run_level,
    summarize_observations,
    validate_completion_response,
)


def _service() -> RerankService:
    service = RerankService.__new__(RerankService)
    service.settings = SimpleNamespace(effective_rerank_model="Qwen/Qwen3-Reranker-4B")
    return service


def _completion_body(count: int) -> dict[str, object]:
    return {
        "choices": [
            {
                "index": index,
                "text": "yes",
                "logprobs": {
                    "top_logprobs": [{"yes": -0.1, "no": -2.1}],
                },
            }
            for index in range(count)
        ]
    }


def test_default_workload_shape_is_exact() -> None:
    documents = build_documents(count=116, total_characters=93_800)

    assert len(documents) == 116
    assert sum(len(document) for document in documents) == 93_800
    assert min(map(len, documents)) > 0


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:7116", "http://127.0.0.1:7116/v1/completions"),
        ("http://reranker:8000/v1/", "http://reranker:8000/v1/completions"),
        (
            "http://reranker:8000/v1/completions",
            "http://reranker:8000/v1/completions",
        ),
    ],
)
def test_completion_url_accepts_server_v1_or_full_url(
    base_url: str,
    expected: str,
) -> None:
    assert completion_url(base_url) == expected


def test_payload_is_built_by_the_legacy_completion_contract() -> None:
    service = _service()
    documents = ["first", "second"]

    payload = build_payload(service, query="query", documents=documents)

    assert payload == service._local_qwen_payload(query="query", documents=documents)
    assert payload["model"] == "Qwen/Qwen3-Reranker-4B"
    assert len(payload["prompt"]) == 2
    assert payload["max_tokens"] == 1
    assert payload["allowed_token_ids"] == [9693, 2152]


def test_payload_service_pins_the_local_model_independent_of_runtime_mode() -> None:
    service = build_payload_service(" Qwen/Qwen3-Reranker-4B ")

    payload = build_payload(service, query="query", documents=["document"])

    assert payload["model"] == "Qwen/Qwen3-Reranker-4B"
    assert payload["allowed_token_ids"] == [9693, 2152]


def test_response_validation_requires_complete_unique_choices() -> None:
    service = _service()
    validate_completion_response(service, _completion_body(2), expected_count=2)

    incomplete = _completion_body(1)
    with pytest.raises(ValueError, match="expected 2"):
        validate_completion_response(service, incomplete, expected_count=2)

    duplicated = _completion_body(2)
    duplicated["choices"][1]["index"] = 0  # type: ignore[index]
    with pytest.raises(ValueError, match="duplicated"):
        validate_completion_response(service, duplicated, expected_count=2)

    malformed = _completion_body(1)
    malformed["choices"][0]["logprobs"] = None  # type: ignore[index]
    with pytest.raises(ValueError, match="missing logprobs"):
        validate_completion_response(service, malformed, expected_count=1)


def test_percentiles_and_error_metrics_are_reported() -> None:
    observations = [
        RequestObservation(duration_ms=10),
        RequestObservation(duration_ms=20),
        RequestObservation(duration_ms=30),
        RequestObservation(duration_ms=40, error="HTTP 500"),
    ]

    summary = summarize_observations(
        observations,
        wall_time_seconds=2.0,
        document_count=116,
        document_characters=93_800,
    )

    assert percentile([10, 20, 30], 0.50) == 20
    assert summary["p50_ms"] == 20
    assert summary["p95_ms"] == 30
    assert summary["p99_ms"] == 30
    assert summary["throughput_requests_per_second"] == 1.5
    assert summary["throughput_documents_per_second"] == 174.0
    assert summary["throughput_document_characters_per_second"] == 140_700.0
    assert summary["error_count"] == 1
    assert summary["error_samples"] == ["HTTP 500"]


def test_run_level_uses_warmup_and_measured_rounds_with_strict_http_validation() -> None:
    service = _service()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = __import__("json").loads(request.content)
        return httpx.Response(200, json=_completion_body(len(payload["prompt"])))

    async def run() -> dict[str, object]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await run_level(
                client,
                url="http://reranker/v1/completions",
                service=service,
                documents=["one", "two"],
                query="query",
                concurrency=4,
                warmup_rounds=1,
                measured_rounds=2,
            )

    report = asyncio.run(run())

    assert calls == 12
    assert report["warmup"]["request_count"] == 4  # type: ignore[index]
    assert report["request_count"] == 8
    assert report["success_count"] == 8
    assert report["error_count"] == 0
    assert report["p50_ms"] is not None

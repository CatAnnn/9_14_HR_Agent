from __future__ import annotations

import json
import math

import httpx
import pytest

from scripts import benchmark_reranker_shadow as benchmark


def _legacy_choice(index: int, score: float) -> dict[str, object]:
    return {
        "index": index,
        "text": "yes" if score >= 0.5 else "no",
        "logprobs": {
            "top_logprobs": [
                {
                    "yes": math.log(score),
                    "no": math.log(1.0 - score),
                }
            ]
        },
    }


def test_main_is_offline_by_default(monkeypatch, capsys) -> None:
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("asyncio.run must not be called in dry-run mode")

    monkeypatch.setattr(benchmark.asyncio, "run", unexpected_run)

    assert benchmark.main([]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert report["network_calls_enabled"] is False
    assert report["workload"]["documents_per_request"] == 12


def test_documents_per_request_cycles_sources_with_stable_suffixes(
    monkeypatch, capsys
) -> None:
    workload = benchmark.Workload(
        query="query",
        documents=("first", "second"),
        instruction="instruction",
    )

    assert benchmark.resize_workload(workload, None) is workload
    resized = benchmark.resize_workload(workload, 5)

    assert resized.documents == (
        "first\n[benchmark-document:0001]",
        "second\n[benchmark-document:0002]",
        "first\n[benchmark-document:0003]",
        "second\n[benchmark-document:0004]",
        "first\n[benchmark-document:0005]",
    )
    shaped = benchmark.resize_workload(workload, 5, 203)
    assert len(shaped.documents) == 5
    assert [len(document) for document in shaped.documents] == [41, 41, 41, 40, 40]
    assert sum(len(document) for document in shaped.documents) == 203
    assert shaped == benchmark.resize_workload(workload, 5, 203)
    with pytest.raises(benchmark.BenchmarkError):
        benchmark.resize_workload(workload, 5, 4)

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("asyncio.run must not be called in dry-run mode")

    monkeypatch.setattr(benchmark.asyncio, "run", unexpected_run)
    assert (
        benchmark.main(
            [
                "--side",
                "native",
                "--documents-per-request",
                "5",
                "--document-characters-per-request",
                "203",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["configuration"]["side"] == "native"
    assert report["workload"]["documents_per_request"] == 5
    assert report["workload"]["document_characters"] == 203


def test_payloads_use_the_same_query_and_documents() -> None:
    workload = benchmark.Workload(
        query="绩效结果如何说明？",
        documents=("先说明结果。", "引用事实。", "确认理解。"),
        instruction="检索绩效沟通知识。",
    )

    legacy = benchmark.build_legacy_payload(workload, benchmark.DEFAULT_MODEL)
    native = benchmark.build_native_payload(workload, benchmark.DEFAULT_MODEL)

    assert len(legacy["prompt"]) == len(workload.documents)
    assert all(workload.query in prompt for prompt in legacy["prompt"])
    assert all(
        document in prompt
        for document, prompt in zip(workload.documents, legacy["prompt"], strict=True)
    )
    assert legacy["allowed_token_ids"] == [9693, 2152]
    assert native["query"] == workload.query
    assert native["documents"] == list(workload.documents)
    assert native["instruction"] == workload.instruction
    assert native["top_n"] == len(workload.documents)


def test_score_parsers_restore_original_candidate_order() -> None:
    legacy = {
        "choices": [
            _legacy_choice(2, 0.2),
            _legacy_choice(0, 0.8),
            _legacy_choice(1, 0.5),
        ]
    }
    native = {
        "results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.4},
            {"index": 2, "relevance_score": 0.1},
        ]
    }

    assert benchmark.extract_legacy_scores(legacy, 3) == pytest.approx((0.8, 0.5, 0.2))
    assert benchmark.extract_native_scores(native, 3) == pytest.approx((0.4, 0.9, 0.1))


def test_rank_metrics_cover_identical_and_reversed_rankings() -> None:
    assert benchmark.rank_indexes((0.2, 0.9, 0.4)) == (1, 2, 0)
    assert benchmark.spearman_rank_correlation((0, 1, 2, 3), (0, 1, 2, 3)) == 1.0
    assert benchmark.spearman_rank_correlation((0, 1, 2, 3), (3, 2, 1, 0)) == -1.0
    assert benchmark.percentile((1.0, 2.0, 3.0, 4.0), 0.50) == 2.0
    assert benchmark.percentile((1.0, 2.0, 3.0, 4.0), 0.95) == 4.0


@pytest.mark.asyncio
async def test_ab_benchmark_reports_performance_and_quality_without_leaking_key(
    monkeypatch,
) -> None:
    workload = benchmark.Workload(
        query="哪些证据支持绩效评价？",
        documents=("交付结果", "可观察行为", "天气预报"),
    )
    legacy_scores = (0.9, 0.7, 0.1)
    native_scores = (0.8, 0.6, 0.2)
    calls = {"legacy": 0, "native": 0}
    secret = "do-not-print-this-key"
    monkeypatch.setenv("SHADOW_BENCHMARK_KEY", secret)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {secret}"
        body = json.loads(request.content)
        if request.url.path == "/v1/completions":
            calls["legacy"] += 1
            assert len(body["prompt"]) == len(workload.documents)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        _legacy_choice(index, score)
                        for index, score in enumerate(legacy_scores)
                    ]
                },
            )
        calls["native"] += 1
        assert body["query"] == workload.query
        assert body["documents"] == list(workload.documents)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": score}
                    for index, score in enumerate(native_scores)
                ]
            },
        )

    config = benchmark.BenchmarkConfig(
        legacy_endpoint="http://shadow.test/v1/completions",
        native_endpoint="http://shadow.test/v1/rerank",
        concurrency=2,
        rounds=2,
        warmup_rounds=0,
        top_k=2,
        api_key_env="SHADOW_BENCHMARK_KEY",
    )
    report = await benchmark.run_ab_benchmark(
        config,
        workload,
        transport=httpx.MockTransport(handler),
    )

    assert calls == {"legacy": 4, "native": 4}
    assert report["legacy"]["candidate_count"] == 12
    assert report["native"]["candidate_count"] == 12
    assert report["legacy"]["candidates_per_second"] > 0
    assert report["native"]["p50_ms"] >= 0
    assert report["comparison"]["top_k_overlap_ratio"] == 1.0
    assert report["comparison"]["spearman_rank_correlation"] == 1.0
    assert secret not in json.dumps(report)


@pytest.mark.parametrize("side", ("legacy", "native"))
@pytest.mark.asyncio
async def test_single_side_benchmark_only_calls_selected_protocol(side: str) -> None:
    workload = benchmark.Workload(
        query="哪些证据支持绩效评价？",
        documents=("交付结果", "可观察行为", "天气预报"),
    )
    expected_scores = (0.9, 0.7, 0.1)
    calls = {"legacy": 0, "native": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/completions":
            assert side == "legacy", "single native run called the legacy endpoint"
            calls["legacy"] += 1
            assert len(body["prompt"]) == len(workload.documents)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        _legacy_choice(index, score)
                        for index, score in enumerate(expected_scores)
                    ]
                },
            )
        assert request.url.path == "/v1/rerank"
        assert side == "native", "single legacy run called the native endpoint"
        calls["native"] += 1
        assert body["documents"] == list(workload.documents)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": score}
                    for index, score in enumerate(expected_scores)
                ]
            },
        )

    config = benchmark.BenchmarkConfig(
        legacy_endpoint=(
            "http://shadow.test/v1/completions"
            if side == "legacy"
            else "unused legacy endpoint"
        ),
        native_endpoint=(
            "http://shadow.test/v1/rerank"
            if side == "native"
            else "unused native endpoint"
        ),
        concurrency=2,
        rounds=2,
        warmup_rounds=1,
        top_k=2,
        side=side,
    )
    report = await benchmark.run_side_benchmark(
        config,
        workload,
        transport=httpx.MockTransport(handler),
    )

    assert calls[side] == 5
    assert calls["native" if side == "legacy" else "legacy"] == 0
    assert report["side"] == side
    assert report[side]["candidate_count"] == 12
    assert report[side]["ranking"] == [0, 1, 2]
    assert report[side]["average_scores"] == pytest.approx(expected_scores)
    assert ("native" if side == "legacy" else "legacy") not in report


@pytest.mark.asyncio
async def test_http_failure_redacts_url_credentials_query_and_response_body() -> None:
    endpoint = "http://user:password@shadow.test/v1/rerank?api_key=url-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="gateway echoed server-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(benchmark.BenchmarkError) as caught:
            await benchmark._request_scores(
                client,
                kind="native",
                endpoint=endpoint,
                payload={},
                expected_count=2,
            )

    message = str(caught.value)
    assert "http://shadow.test/v1/rerank" in message
    assert "password" not in message
    assert "url-secret" not in message
    assert "server-secret" not in message

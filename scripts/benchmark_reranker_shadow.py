#!/usr/bin/env python3
"""Offline-by-default A/B benchmark for legacy and native vLLM reranking.

The legacy side calls ``/v1/completions`` and derives a relevance probability
from the first-token ``yes``/``no`` log probabilities.  The native side calls
vLLM's pooling-model ``/v1/rerank`` API.  Both sides receive the same query and
documents.

No HTTP request is made unless ``--run`` is supplied.  Authentication can only
be read from an environment variable; its value is never included in reports
or error messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx


DEFAULT_MODEL = "Qwen/Qwen3-Reranker-4B"
DEFAULT_LEGACY_ENDPOINT = "http://127.0.0.1:7116/v1/completions"
DEFAULT_NATIVE_ENDPOINT = "http://127.0.0.1:7123/v1/rerank"
DEFAULT_INSTRUCTION = (
    "Given an HR performance feedback query, retrieve the most relevant "
    "knowledge base passages."
)
DEFAULT_QUERY = "如何依据事实向员工说明绩效结果与岗位预期之间的差距？"
DEFAULT_DOCUMENTS = (
    "绩效沟通应先清晰说明结果，再用可观察的交付事实对照既定目标和岗位要求。",
    "管理者需要区分事实、判断和感受，引用具体项目、时间节点、质量与业务影响。",
    "员工可以与经理共同确认下一阶段行动、责任人、检查节点和可验证的完成标准。",
    "出现防御情绪时，应先倾听和确认理解，再回到事实与绩效标准继续讨论。",
    "办公室绿植需要根据光照条件调整浇水频率，冬季通常应减少浇水。",
    "岗位期望应来自职责范围、职级标准和已对齐目标，不能在面谈中临时增加。",
    "有效反馈应描述具体行为及影响，避免使用‘态度不好’等无法验证的标签。",
    "年度团建可以安排交通、餐饮和场地，并提前收集参与人员的饮食偏好。",
    "如果员工不理解评级，管理者应邀请其复述核心结论并回应尚存的具体疑问。",
    "发展计划应与真实差距相关，并明确员工行动、经理支持资源及后续复盘时间。",
    "项目延期需要结合范围变化、依赖条件、风险预警和最终交付结果综合分析。",
    "会议室预订系统支持按楼层、容量和设备条件筛选可用房间。",
)

_QWEN_RERANK_PREFIX = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the Query and the Instruct provided. "
    'Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n"
    "<|im_start|>user\n"
)
_QWEN_RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_ALLOWED_TOKEN_IDS_BY_MODEL: dict[str, tuple[int, int]] = {
    DEFAULT_MODEL: (9693, 2152),
}


class BenchmarkError(RuntimeError):
    """Expected benchmark failure whose message is safe to print."""


@dataclass(frozen=True)
class Workload:
    query: str
    documents: tuple[str, ...]
    instruction: str = DEFAULT_INSTRUCTION


@dataclass(frozen=True)
class BenchmarkConfig:
    legacy_endpoint: str = DEFAULT_LEGACY_ENDPOINT
    native_endpoint: str = DEFAULT_NATIVE_ENDPOINT
    legacy_model: str = DEFAULT_MODEL
    native_model: str = DEFAULT_MODEL
    concurrency: int = 1
    rounds: int = 3
    warmup_rounds: int = 1
    top_k: int = 5
    timeout_seconds: float = 120.0
    api_key_env: str = ""
    trust_env_proxy: bool = False
    side: str = "both"


@dataclass(frozen=True)
class RequestSample:
    latency_ms: float
    scores: tuple[float, ...]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare legacy vLLM completions reranking with a native vLLM "
            "pooling rerank endpoint. The default dry run performs no HTTP calls."
        )
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Explicitly allow HTTP requests. Without this flag the CLI is offline.",
    )
    parser.add_argument("--legacy-endpoint", default=DEFAULT_LEGACY_ENDPOINT)
    parser.add_argument("--native-endpoint", default=DEFAULT_NATIVE_ENDPOINT)
    parser.add_argument("--legacy-model", default=DEFAULT_MODEL)
    parser.add_argument("--native-model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--side",
        choices=("both", "legacy", "native"),
        default="both",
        help=(
            "Benchmark both live endpoints, or one endpoint at a time for "
            "same-GPU sequential comparisons."
        ),
    )
    parser.add_argument("--concurrency", type=_positive_int, default=1)
    parser.add_argument("--rounds", type=_positive_int, default=3)
    parser.add_argument("--warmup-rounds", type=_non_negative_int, default=1)
    parser.add_argument("--top-k", type=_positive_int, default=5)
    parser.add_argument("--timeout-seconds", type=_positive_float, default=120.0)
    parser.add_argument(
        "--api-key-env",
        default="",
        help="Environment variable containing a shared API key; never pass a key literal.",
    )
    parser.add_argument(
        "--trust-env-proxy",
        action="store_true",
        help="Allow httpx to use proxy variables from the process environment.",
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        help='Optional UTF-8 JSON: {"query": "...", "documents": ["..."]}.',
    )
    parser.add_argument(
        "--documents-per-request",
        type=_positive_int,
        help=(
            "Optionally expand the loaded documents to this count by cycling "
            "them with deterministic benchmark-only index suffixes."
        ),
    )
    parser.add_argument(
        "--document-characters-per-request",
        type=_positive_int,
        help=(
            "Optionally pad or truncate the selected documents to this exact "
            "total character count."
        ),
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path.")
    return parser.parse_args(argv)


def _safe_endpoint(endpoint: str) -> str:
    """Remove credentials, query parameters, and fragments before reporting a URL."""

    parts = urlsplit(endpoint)
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parts.port is not None:
        netloc = f"{hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _validate_endpoint(endpoint: str) -> None:
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise BenchmarkError("Endpoint must be an absolute HTTP(S) URL.")


def load_workload(path: Path | None) -> Workload:
    if path is None:
        return Workload(query=DEFAULT_QUERY, documents=DEFAULT_DOCUMENTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError("Unable to read a valid UTF-8 JSON workload file.") from exc
    if not isinstance(raw, dict):
        raise BenchmarkError("Workload JSON must be an object.")
    query = raw.get("query")
    documents = raw.get("documents")
    instruction = raw.get("instruction", DEFAULT_INSTRUCTION)
    if not isinstance(query, str) or not query.strip():
        raise BenchmarkError("Workload query must be a non-empty string.")
    if (
        not isinstance(documents, list)
        or len(documents) < 2
        or any(not isinstance(item, str) or not item.strip() for item in documents)
    ):
        raise BenchmarkError("Workload documents must contain at least two non-empty strings.")
    if not isinstance(instruction, str) or not instruction.strip():
        raise BenchmarkError("Workload instruction must be a non-empty string.")
    return Workload(
        query=query.strip(),
        documents=tuple(item.strip() for item in documents),
        instruction=instruction.strip(),
    )


def resize_workload(
    workload: Workload,
    document_count: int | None,
    document_characters: int | None = None,
) -> Workload:
    """Build a deterministic candidate count without requiring a large JSON file."""

    if document_count is None and document_characters is None:
        return workload
    documents = workload.documents
    if document_count is not None:
        documents = tuple(
            (
                f"{workload.documents[index % len(workload.documents)]}"
                f"\n[benchmark-document:{index + 1:04d}]"
            )
            for index in range(document_count)
        )
    if document_characters is not None:
        if document_characters < len(documents):
            raise BenchmarkError(
                "Document character count must be at least the document count."
            )
        base_length, remainder = divmod(document_characters, len(documents))
        resized_documents: list[str] = []
        for index, document in enumerate(documents):
            target_length = base_length + (1 if index < remainder else 0)
            repetitions = math.ceil(target_length / len(document))
            resized_documents.append((document * repetitions)[:target_length])
        documents = tuple(resized_documents)
    return Workload(
        query=workload.query,
        documents=documents,
        instruction=workload.instruction,
    )


def build_legacy_prompt(query: str, document: str, instruction: str) -> str:
    pair = f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}"
    return f"{_QWEN_RERANK_PREFIX}{pair}{_QWEN_RERANK_SUFFIX}"


def build_legacy_payload(workload: Workload, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": [
            build_legacy_prompt(workload.query, document, workload.instruction)
            for document in workload.documents
        ],
        "max_tokens": 1,
        "temperature": 0,
        "logprobs": 20,
        "echo": False,
    }
    allowed_token_ids = _ALLOWED_TOKEN_IDS_BY_MODEL.get(model)
    if allowed_token_ids is not None:
        payload["allowed_token_ids"] = list(allowed_token_ids)
    return payload


def build_native_payload(workload: Workload, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "query": workload.query,
        "documents": list(workload.documents),
        "instruction": workload.instruction,
        # Full results are required to calculate whole-ranking correlation.
        "top_n": len(workload.documents),
    }


def _token_logprob(step: dict[str, Any], expected: str) -> float | None:
    for token, value in step.items():
        if str(token).strip().lower() != expected:
            continue
        raw = value.get("logprob") if isinstance(value, dict) else value
        return None if raw is None else float(raw)
    return None


def _yes_probability(yes_logprob: float, no_logprob: float) -> float:
    maximum = max(yes_logprob, no_logprob)
    yes_exp = math.exp(yes_logprob - maximum)
    no_exp = math.exp(no_logprob - maximum)
    return yes_exp / (yes_exp + no_exp)


def _legacy_choice_score(choice: dict[str, Any]) -> float:
    text = str(choice.get("text") or "").strip().lower()
    logprobs = choice.get("logprobs")
    top_logprobs = logprobs.get("top_logprobs") if isinstance(logprobs, dict) else None
    if isinstance(top_logprobs, list) and top_logprobs and isinstance(top_logprobs[0], dict):
        yes_logprob = _token_logprob(top_logprobs[0], "yes")
        no_logprob = _token_logprob(top_logprobs[0], "no")
        if yes_logprob is not None or no_logprob is not None:
            return _yes_probability(
                yes_logprob if yes_logprob is not None else -20.0,
                no_logprob if no_logprob is not None else -20.0,
            )
    if text.startswith("yes"):
        return 1.0
    if text.startswith("no"):
        return 0.0
    raise BenchmarkError("Legacy response did not contain usable yes/no scores.")


def extract_legacy_scores(data: dict[str, Any], expected_count: int) -> tuple[float, ...]:
    choices = data.get("choices")
    if not isinstance(choices, list):
        raise BenchmarkError("Legacy response does not contain a choices list.")
    scores: dict[int, float] = {}
    for fallback_index, choice in enumerate(choices):
        if not isinstance(choice, dict):
            raise BenchmarkError("Legacy response contains an invalid choice.")
        index = int(choice.get("index", fallback_index))
        if not 0 <= index < expected_count or index in scores:
            raise BenchmarkError("Legacy response contains invalid or duplicate indexes.")
        scores[index] = _legacy_choice_score(choice)
    if len(scores) != expected_count:
        raise BenchmarkError("Legacy response did not score every candidate.")
    return tuple(scores[index] for index in range(expected_count))


def extract_native_scores(data: dict[str, Any], expected_count: int) -> tuple[float, ...]:
    results: Any = data.get("results")
    if results is None and isinstance(data.get("data"), dict):
        results = data["data"].get("results")
    if not isinstance(results, list):
        raise BenchmarkError("Native response does not contain a results list.")
    scores: dict[int, float] = {}
    for item in results:
        if not isinstance(item, dict):
            raise BenchmarkError("Native response contains an invalid result.")
        index = int(item.get("index", -1))
        raw_score = item.get("relevance_score", item.get("score"))
        if raw_score is None or not 0 <= index < expected_count or index in scores:
            raise BenchmarkError("Native response contains invalid scores or indexes.")
        scores[index] = float(raw_score)
    if len(scores) != expected_count:
        raise BenchmarkError("Native response did not score every candidate.")
    return tuple(scores[index] for index in range(expected_count))


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def rank_indexes(scores: Sequence[float]) -> tuple[int, ...]:
    return tuple(sorted(range(len(scores)), key=lambda index: (-scores[index], index)))


def spearman_rank_correlation(left: Sequence[int], right: Sequence[int]) -> float:
    if len(left) != len(right) or set(left) != set(right):
        raise ValueError("rankings must be permutations of the same candidates")
    count = len(left)
    if count < 2:
        return 1.0
    left_rank = {candidate: rank for rank, candidate in enumerate(left, start=1)}
    right_rank = {candidate: rank for rank, candidate in enumerate(right, start=1)}
    squared_differences = sum(
        (left_rank[candidate] - right_rank[candidate]) ** 2 for candidate in left
    )
    return 1.0 - (6.0 * squared_differences) / (count * (count * count - 1))


def _headers(api_key_env: str) -> tuple[dict[str, str], bool]:
    headers = {"accept": "application/json", "Content-Type": "application/json"}
    if not api_key_env:
        return headers, False
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise BenchmarkError("The configured API-key environment variable is empty.")
    headers["Authorization"] = f"Bearer {api_key}"
    return headers, True


async def _request_scores(
    client: httpx.AsyncClient,
    *,
    kind: str,
    endpoint: str,
    payload: dict[str, Any],
    expected_count: int,
) -> RequestSample:
    started = time.perf_counter()
    try:
        response = await client.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        raise BenchmarkError(
            f"{kind} transport failure at {_safe_endpoint(endpoint)} ({type(exc).__name__})."
        ) from exc
    latency_ms = (time.perf_counter() - started) * 1000.0
    if response.status_code >= 400:
        # Do not print response bodies: gateways sometimes echo credentials.
        raise BenchmarkError(
            f"{kind} endpoint {_safe_endpoint(endpoint)} returned HTTP {response.status_code}."
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise BenchmarkError(f"{kind} endpoint returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise BenchmarkError(f"{kind} endpoint returned a non-object JSON response.")
    if kind == "legacy":
        scores = extract_legacy_scores(data, expected_count)
    else:
        scores = extract_native_scores(data, expected_count)
    return RequestSample(latency_ms=latency_ms, scores=scores)


async def _measure_endpoint(
    client: httpx.AsyncClient,
    *,
    kind: str,
    endpoint: str,
    payload: dict[str, Any],
    expected_count: int,
    concurrency: int,
    rounds: int,
) -> tuple[dict[str, Any], tuple[float, ...]]:
    samples: list[RequestSample] = []
    wall_started = time.perf_counter()
    for _ in range(rounds):
        samples.extend(
            await asyncio.gather(
                *(
                    _request_scores(
                        client,
                        kind=kind,
                        endpoint=endpoint,
                        payload=payload,
                        expected_count=expected_count,
                    )
                    for _ in range(concurrency)
                )
            )
        )
    wall_seconds = max(time.perf_counter() - wall_started, 1e-9)
    request_count = len(samples)
    average_scores = tuple(
        sum(sample.scores[index] for sample in samples) / request_count
        for index in range(expected_count)
    )
    latencies = [sample.latency_ms for sample in samples]
    report = {
        "request_count": request_count,
        "candidate_count": request_count * expected_count,
        "wall_seconds": round(wall_seconds, 6),
        "p50_ms": round(percentile(latencies, 0.50), 3),
        "p95_ms": round(percentile(latencies, 0.95), 3),
        "requests_per_second": round(request_count / wall_seconds, 3),
        "candidates_per_second": round(
            (request_count * expected_count) / wall_seconds, 3
        ),
    }
    return report, average_scores


async def run_ab_benchmark(
    config: BenchmarkConfig,
    workload: Workload,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    _validate_endpoint(config.legacy_endpoint)
    _validate_endpoint(config.native_endpoint)
    headers, auth_configured = _headers(config.api_key_env)
    legacy_payload = build_legacy_payload(workload, config.legacy_model)
    native_payload = build_native_payload(workload, config.native_model)
    limits = httpx.Limits(
        max_connections=max(4, config.concurrency * 2),
        max_keepalive_connections=max(2, config.concurrency),
    )
    async with httpx.AsyncClient(
        headers=headers,
        timeout=config.timeout_seconds,
        limits=limits,
        transport=transport,
        trust_env=config.trust_env_proxy,
        follow_redirects=False,
    ) as client:
        for _ in range(config.warmup_rounds):
            await _request_scores(
                client,
                kind="legacy",
                endpoint=config.legacy_endpoint,
                payload=legacy_payload,
                expected_count=len(workload.documents),
            )
            await _request_scores(
                client,
                kind="native",
                endpoint=config.native_endpoint,
                payload=native_payload,
                expected_count=len(workload.documents),
            )
        # Measure sequentially so two instances do not steal GPU time from each other.
        legacy_report, legacy_scores = await _measure_endpoint(
            client,
            kind="legacy",
            endpoint=config.legacy_endpoint,
            payload=legacy_payload,
            expected_count=len(workload.documents),
            concurrency=config.concurrency,
            rounds=config.rounds,
        )
        native_report, native_scores = await _measure_endpoint(
            client,
            kind="native",
            endpoint=config.native_endpoint,
            payload=native_payload,
            expected_count=len(workload.documents),
            concurrency=config.concurrency,
            rounds=config.rounds,
        )

    legacy_ranking = rank_indexes(legacy_scores)
    native_ranking = rank_indexes(native_scores)
    top_k = min(config.top_k, len(workload.documents))
    overlap_count = len(set(legacy_ranking[:top_k]) & set(native_ranking[:top_k]))
    legacy_throughput = legacy_report["candidates_per_second"]
    native_throughput = native_report["candidates_per_second"]
    return {
        "schema_version": "reranker-shadow-ab.v1",
        "mode": "live",
        "network_calls_enabled": True,
        "configuration": {
            "legacy_endpoint": _safe_endpoint(config.legacy_endpoint),
            "native_endpoint": _safe_endpoint(config.native_endpoint),
            "legacy_model": config.legacy_model,
            "native_model": config.native_model,
            "concurrency": config.concurrency,
            "rounds": config.rounds,
            "warmup_rounds": config.warmup_rounds,
            "authentication_configured": auth_configured,
            "trust_env_proxy": config.trust_env_proxy,
        },
        "workload": {
            "language": "zh-CN",
            "documents_per_request": len(workload.documents),
            "query_characters": len(workload.query),
            "document_characters": sum(len(item) for item in workload.documents),
            "top_k": top_k,
        },
        "legacy": {
            **legacy_report,
            "ranking": list(legacy_ranking),
        },
        "native": {
            **native_report,
            "ranking": list(native_ranking),
        },
        "comparison": {
            "native_candidates_per_second_speedup": round(
                native_throughput / legacy_throughput, 4
            )
            if legacy_throughput > 0
            else None,
            "native_p95_latency_reduction_percent": round(
                (legacy_report["p95_ms"] - native_report["p95_ms"])
                / legacy_report["p95_ms"]
                * 100.0,
                3,
            )
            if legacy_report["p95_ms"] > 0
            else None,
            "top_k_overlap_count": overlap_count,
            "top_k_overlap_ratio": round(overlap_count / top_k, 4),
            "spearman_rank_correlation": round(
                spearman_rank_correlation(legacy_ranking, native_ranking), 6
            ),
        },
    }


async def run_side_benchmark(
    config: BenchmarkConfig,
    workload: Workload,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Measure one protocol so legacy and native can share one GPU sequentially."""

    side = config.side
    if side not in {"legacy", "native"}:
        raise BenchmarkError("Single-side benchmark requires legacy or native side.")
    endpoint = (
        config.legacy_endpoint if side == "legacy" else config.native_endpoint
    )
    model = config.legacy_model if side == "legacy" else config.native_model
    _validate_endpoint(endpoint)
    headers, auth_configured = _headers(config.api_key_env)
    payload = (
        build_legacy_payload(workload, model)
        if side == "legacy"
        else build_native_payload(workload, model)
    )
    limits = httpx.Limits(
        max_connections=max(4, config.concurrency),
        max_keepalive_connections=max(2, config.concurrency),
    )
    async with httpx.AsyncClient(
        headers=headers,
        timeout=config.timeout_seconds,
        limits=limits,
        transport=transport,
        trust_env=config.trust_env_proxy,
        follow_redirects=False,
    ) as client:
        for _ in range(config.warmup_rounds):
            await _request_scores(
                client,
                kind=side,
                endpoint=endpoint,
                payload=payload,
                expected_count=len(workload.documents),
            )
        side_report, average_scores = await _measure_endpoint(
            client,
            kind=side,
            endpoint=endpoint,
            payload=payload,
            expected_count=len(workload.documents),
            concurrency=config.concurrency,
            rounds=config.rounds,
        )

    top_k = min(config.top_k, len(workload.documents))
    return {
        "schema_version": "reranker-shadow-side.v1",
        "mode": "live",
        "network_calls_enabled": True,
        "side": side,
        "configuration": {
            "endpoint": _safe_endpoint(endpoint),
            "model": model,
            "concurrency": config.concurrency,
            "rounds": config.rounds,
            "warmup_rounds": config.warmup_rounds,
            "authentication_configured": auth_configured,
            "trust_env_proxy": config.trust_env_proxy,
        },
        "workload": {
            "language": "zh-CN",
            "documents_per_request": len(workload.documents),
            "query_characters": len(workload.query),
            "document_characters": sum(len(item) for item in workload.documents),
            "top_k": top_k,
        },
        side: {
            **side_report,
            "ranking": list(rank_indexes(average_scores)),
            "average_scores": list(average_scores),
        },
    }


def dry_run_report(config: BenchmarkConfig, workload: Workload) -> dict[str, Any]:
    if config.side in {"both", "legacy"}:
        _validate_endpoint(config.legacy_endpoint)
    if config.side in {"both", "native"}:
        _validate_endpoint(config.native_endpoint)
    return {
        "schema_version": "reranker-shadow-ab.v1",
        "mode": "dry-run",
        "network_calls_enabled": False,
        "message": "No HTTP request was made. Pass --run to execute the A/B benchmark.",
        "configuration": {
            "legacy_endpoint": _safe_endpoint(config.legacy_endpoint),
            "native_endpoint": _safe_endpoint(config.native_endpoint),
            "legacy_model": config.legacy_model,
            "native_model": config.native_model,
            "concurrency": config.concurrency,
            "rounds": config.rounds,
            "warmup_rounds": config.warmup_rounds,
            "side": config.side,
            "authentication_configured": bool(
                config.api_key_env and os.environ.get(config.api_key_env)
            ),
            "trust_env_proxy": config.trust_env_proxy,
        },
        "workload": {
            "language": "zh-CN",
            "documents_per_request": len(workload.documents),
            "query_characters": len(workload.query),
            "document_characters": sum(len(item) for item in workload.documents),
            "top_k": min(config.top_k, len(workload.documents)),
        },
    }


def _config_from_args(args: argparse.Namespace) -> BenchmarkConfig:
    return BenchmarkConfig(
        legacy_endpoint=args.legacy_endpoint,
        native_endpoint=args.native_endpoint,
        legacy_model=args.legacy_model,
        native_model=args.native_model,
        concurrency=args.concurrency,
        rounds=args.rounds,
        warmup_rounds=args.warmup_rounds,
        top_k=args.top_k,
        timeout_seconds=args.timeout_seconds,
        api_key_env=args.api_key_env,
        trust_env_proxy=args.trust_env_proxy,
        side=args.side,
    )


def _emit(report: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = _config_from_args(args)
        workload = resize_workload(
            load_workload(args.input_file),
            args.documents_per_request,
            args.document_characters_per_request,
        )
        if args.run:
            report = asyncio.run(
                run_ab_benchmark(config, workload)
                if config.side == "both"
                else run_side_benchmark(config, workload)
            )
        else:
            report = dry_run_report(config, workload)
        _emit(report, args.output)
        return 0
    except BenchmarkError as exc:
        _emit(
            {
                "schema_version": "reranker-shadow-ab.v1",
                "mode": "error",
                "error": str(exc),
            },
            args.output,
        )
        return 2
    except Exception as exc:  # Keep unexpected library errors secret-safe too.
        _emit(
            {
                "schema_version": "reranker-shadow-ab.v1",
                "mode": "error",
                "error": f"Unexpected benchmark failure ({type(exc).__name__}).",
            },
            args.output,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

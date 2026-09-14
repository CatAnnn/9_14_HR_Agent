from __future__ import annotations

"""Benchmark the retained legacy Qwen reranker ``/v1/completions`` contract.

The benchmark intentionally builds every request through
``RerankService._local_qwen_payload`` so changes to the application prompt or
allowed-token contract are reflected here automatically.
"""

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.rerank_service import RerankService  # noqa: E402


DEFAULT_CONCURRENCY_LEVELS = (4, 6, 7, 8)
DEFAULT_MODEL = "Qwen/Qwen3-Reranker-4B"
DEFAULT_DOCUMENT_COUNT = 116
DEFAULT_DOCUMENT_CHARACTERS = 93_800
DEFAULT_QUERY = (
    "请判断以下材料是否有助于经理基于客观事实、绩效标准和员工实际情况，"
    "准备一次清晰且可执行的绩效反馈对话。"
)
_DOCUMENT_SEED = (
    "绩效反馈应核对员工目标、实际产出、岗位要求、可观察行为及其对客户、"
    "业务和协作的影响。经理需要区分已经达到的结果与仍需改进的差距，"
    "说明判断依据，并邀请员工补充事实和观点。"
)


@dataclass(frozen=True)
class RequestObservation:
    duration_ms: float
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def completion_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("base URL cannot be empty")
    if normalized.endswith("/v1/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/completions"
    return f"{normalized}/v1/completions"


def redact_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))


def build_documents(*, count: int, total_characters: int) -> list[str]:
    if count <= 0:
        raise ValueError("document count must be positive")
    if total_characters < count:
        raise ValueError("total document characters must be at least the document count")

    base_length, remainder = divmod(total_characters, count)
    documents: list[str] = []
    for index in range(count):
        target_length = base_length + (1 if index < remainder else 0)
        prefix = f"材料 {index + 1:03d}："
        source = prefix + _DOCUMENT_SEED
        repetitions = math.ceil(target_length / len(source))
        document = (source * repetitions)[:target_length]
        documents.append(document)

    if sum(len(document) for document in documents) != total_characters:
        raise AssertionError("generated document character count is inconsistent")
    return documents


def build_payload(
    service: RerankService,
    *,
    query: str,
    documents: list[str],
) -> dict[str, Any]:
    return service._local_qwen_payload(query=query, documents=documents)


def build_payload_service(model: str) -> RerankService:
    """Build only the legacy completion payload/scoring surface for the local model.

    The application may currently run in ``MODEL_PROVIDER_MODE=platform``.
    Instantiating its normal settings in that mode would silently put the
    platform model name in a request sent to the local vLLM endpoint.  This
    deliberately narrow service keeps the compatibility payload implementation
    identical while pinning the local model under test.
    """

    normalized_model = model.strip()
    if not normalized_model:
        raise ValueError("model cannot be empty")
    service = RerankService.__new__(RerankService)
    service.settings = SimpleNamespace(effective_rerank_model=normalized_model)
    return service


def validate_completion_response(
    service: RerankService,
    data: Any,
    *,
    expected_count: int,
) -> None:
    if not isinstance(data, dict):
        raise ValueError("response body must be a JSON object")
    choices = data.get("choices")
    if not isinstance(choices, list):
        raise ValueError("response choices must be a list")
    if len(choices) != expected_count:
        raise ValueError(
            f"response returned {len(choices)} choices; expected {expected_count}"
        )

    indexes: set[int] = set()
    for fallback_index, choice in enumerate(choices):
        if not isinstance(choice, dict):
            raise ValueError(f"choice {fallback_index} must be a JSON object")
        raw_index = choice.get("index", fallback_index)
        if isinstance(raw_index, bool):
            raise ValueError(f"choice {fallback_index} has an invalid boolean index")
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"choice {fallback_index} has an invalid index") from exc
        if index < 0 or index >= expected_count:
            raise ValueError(f"choice index {index} is outside the expected range")
        if index in indexes:
            raise ValueError(f"choice index {index} is duplicated")
        indexes.add(index)

        logprobs = choice.get("logprobs")
        if not isinstance(logprobs, dict):
            raise ValueError(f"choice {index} is missing logprobs")
        top_logprobs = logprobs.get("top_logprobs")
        if not isinstance(top_logprobs, list) or not top_logprobs:
            raise ValueError(f"choice {index} is missing top_logprobs")
        if not isinstance(top_logprobs[0], dict):
            raise ValueError(f"choice {index} has malformed top_logprobs")

    expected_indexes = set(range(expected_count))
    if indexes != expected_indexes:
        raise ValueError("response choices do not cover every input prompt")

    scores = service._extract_qwen_completion_scores(data, expected_count)
    if len(scores) != expected_count or any(not math.isfinite(score) for score in scores):
        raise ValueError("response choices did not produce a complete finite score set")


async def execute_request(
    client: httpx.AsyncClient,
    *,
    url: str,
    payload: dict[str, Any],
    service: RerankService,
    expected_count: int,
) -> RequestObservation:
    started = time.perf_counter()
    error: str | None = None
    try:
        response = await client.post(url, json=payload)
        if response.status_code != 200:
            body = response.text.replace("\n", " ")[:500]
            raise ValueError(f"HTTP {response.status_code}: {body}")
        try:
            data = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("response body is not valid JSON") from exc
        validate_completion_response(service, data, expected_count=expected_count)
    except Exception as exc:  # Keep the matrix running and report every failed request.
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = (time.perf_counter() - started) * 1000
    return RequestObservation(duration_ms=duration_ms, error=error)


async def _run_round(
    client: httpx.AsyncClient,
    *,
    url: str,
    service: RerankService,
    documents: list[str],
    query: str,
    concurrency: int,
    phase: str,
    round_index: int,
) -> list[RequestObservation]:
    requests = []
    for slot in range(concurrency):
        request_query = f"{query}\n[benchmark:{phase}:{concurrency}:{round_index}:{slot}]"
        payload = build_payload(service, query=request_query, documents=documents)
        requests.append(
            execute_request(
                client,
                url=url,
                payload=payload,
                service=service,
                expected_count=len(documents),
            )
        )
    return list(await asyncio.gather(*requests))


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    if not 0 < fraction <= 1:
        raise ValueError("percentile fraction must be in (0, 1]")
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 3)


def summarize_observations(
    observations: Sequence[RequestObservation],
    *,
    wall_time_seconds: float,
    document_count: int,
    document_characters: int | None = None,
) -> dict[str, Any]:
    successful = [item.duration_ms for item in observations if item.succeeded]
    errors = [item.error for item in observations if item.error is not None]
    success_count = len(successful)
    wall_time = max(0.0, wall_time_seconds)
    throughput = success_count / wall_time if wall_time > 0 else 0.0
    summary = {
        "request_count": len(observations),
        "success_count": success_count,
        "error_count": len(errors),
        "p50_ms": percentile(successful, 0.50),
        "p95_ms": percentile(successful, 0.95),
        "p99_ms": percentile(successful, 0.99),
        "mean_ms": round(statistics.fmean(successful), 3) if successful else None,
        "min_ms": round(min(successful), 3) if successful else None,
        "max_ms": round(max(successful), 3) if successful else None,
        "wall_time_seconds": round(wall_time, 3),
        "throughput_requests_per_second": round(throughput, 3),
        "throughput_documents_per_second": round(throughput * document_count, 3),
        "error_samples": errors[:10],
    }
    if document_characters is not None:
        summary["throughput_document_characters_per_second"] = round(
            throughput * document_characters,
            3,
        )
    return summary


async def run_level(
    client: httpx.AsyncClient,
    *,
    url: str,
    service: RerankService,
    documents: list[str],
    query: str,
    concurrency: int,
    warmup_rounds: int,
    measured_rounds: int,
) -> dict[str, Any]:
    warmup_observations: list[RequestObservation] = []
    warmup_started = time.perf_counter()
    for round_index in range(warmup_rounds):
        warmup_observations.extend(
            await _run_round(
                client,
                url=url,
                service=service,
                documents=documents,
                query=query,
                concurrency=concurrency,
                phase="warmup",
                round_index=round_index,
            )
        )
    warmup_wall_seconds = time.perf_counter() - warmup_started

    observations: list[RequestObservation] = []
    measured_started = time.perf_counter()
    for round_index in range(measured_rounds):
        observations.extend(
            await _run_round(
                client,
                url=url,
                service=service,
                documents=documents,
                query=query,
                concurrency=concurrency,
                phase="measured",
                round_index=round_index,
            )
        )
    measured_wall_seconds = time.perf_counter() - measured_started

    return {
        "concurrency": concurrency,
        "warmup_rounds": warmup_rounds,
        "measured_rounds": measured_rounds,
        "warmup": summarize_observations(
            warmup_observations,
            wall_time_seconds=warmup_wall_seconds,
            document_count=len(documents),
            document_characters=sum(len(item) for item in documents),
        ),
        **summarize_observations(
            observations,
            wall_time_seconds=measured_wall_seconds,
            document_count=len(documents),
            document_characters=sum(len(item) for item in documents),
        ),
    }


async def run_matrix(
    *,
    base_url: str,
    levels: Sequence[int],
    warmup_rounds: int,
    measured_rounds: int,
    document_count: int,
    document_characters: int,
    query: str,
    timeout_seconds: float,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> dict[str, Any]:
    selected_levels = tuple(levels)
    if not selected_levels or any(level not in DEFAULT_CONCURRENCY_LEVELS for level in selected_levels):
        raise ValueError("levels must be selected from 4, 6, 7, and 8")
    if warmup_rounds < 0:
        raise ValueError("warmup rounds cannot be negative")
    if measured_rounds <= 0:
        raise ValueError("measured rounds must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")

    url = completion_url(base_url)
    documents = build_documents(
        count=document_count,
        total_characters=document_characters,
    )
    service = build_payload_service(model)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    limits = httpx.Limits(
        max_connections=max(selected_levels),
        max_keepalive_connections=max(selected_levels),
    )
    timeout = httpx.Timeout(timeout_seconds)

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(headers=headers, limits=limits, timeout=timeout) as client:
        for concurrency in selected_levels:
            results.append(
                await run_level(
                    client,
                    url=url,
                    service=service,
                    documents=documents,
                    query=query,
                    concurrency=concurrency,
                    warmup_rounds=warmup_rounds,
                    measured_rounds=measured_rounds,
                )
            )

    return {
        "format_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": redact_url(url),
        "model": service.settings.effective_rerank_model,
        "concurrency_levels": list(selected_levels),
        "warmup_rounds_per_level": warmup_rounds,
        "measured_rounds_per_level": measured_rounds,
        "documents_per_request": len(documents),
        "document_characters_per_request": sum(len(item) for item in documents),
        "results": results,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark the legacy Qwen reranker /v1/completions payload at "
            "concurrency levels 4, 6, 7, and 8."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("RERANK_BENCHMARK_BASE_URL", "http://127.0.0.1:7116"),
        help="Server root, /v1 root, or full /v1/completions URL.",
    )
    parser.add_argument(
        "--level",
        action="append",
        type=int,
        choices=DEFAULT_CONCURRENCY_LEVELS,
        help="Level to run; repeat to select levels. Defaults to 4/6/7/8.",
    )
    parser.add_argument("--warmup-rounds", type=int, default=1)
    parser.add_argument("--rounds", type=_positive_int, default=3)
    parser.add_argument("--documents", type=_positive_int, default=DEFAULT_DOCUMENT_COUNT)
    parser.add_argument(
        "--document-characters",
        type=_positive_int,
        default=DEFAULT_DOCUMENT_CHARACTERS,
    )
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--api-key", default=os.getenv("RERANK_BENCHMARK_API_KEY"))
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = asyncio.run(
            run_matrix(
                base_url=args.base_url,
                levels=args.level or DEFAULT_CONCURRENCY_LEVELS,
                warmup_rounds=args.warmup_rounds,
                measured_rounds=args.rounds,
                document_count=args.documents,
                document_characters=args.document_characters,
                query=args.query,
                timeout_seconds=args.timeout_seconds,
                model=args.model,
                api_key=args.api_key,
            )
        )
    except (ValueError, httpx.HTTPError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    return 1 if any(result["error_count"] for result in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())

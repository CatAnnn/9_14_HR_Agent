from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit
import uuid

from loadtests.cost import estimate_cost_cny, parse_rates


MATRICES: dict[str, tuple[int, ...]] = {
    "llm": (1, 8, 16, 32, 64, 96, 128, 192, 256, 400),
    "embedding": (1, 16, 32, 64, 98, 128, 196, 256, 400),
    "reranker": (1, 8, 16, 24, 32, 46, 64, 92, 128, 196),
}


def redact_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))


def request_count_for(concurrency: int) -> int:
    return max(50, concurrency * 2)


def build_aiperf_command(
    *,
    target: str,
    base_url: str,
    model: str,
    concurrency: int,
    artifact_dir: Path,
    run_id: str,
) -> list[str]:
    endpoint_type = {
        "llm": "chat",
        "embedding": "embeddings",
        "reranker": "cohere_rankings",
    }[target]
    endpoint = {
        "llm": "/v1/chat/completions",
        "embedding": "/v1/embeddings",
        "reranker": "/v1/rerank",
    }[target]
    command = [
        "aiperf",
        "profile",
        "--model",
        model,
        "--url",
        base_url,
        "--endpoint-type",
        endpoint_type,
        "--endpoint",
        endpoint,
        "--concurrency",
        str(concurrency),
        "--request-count",
        str(request_count_for(concurrency)),
        "--workers-max",
        str(min(concurrency, 64)),
        "--artifact-dir",
        str(artifact_dir),
        "--profile-export-file",
        "aiperf",
        "--header",
        f"X-Load-Test-Run-ID:{run_id}",
        "--ui-type",
        "none",
        "--tokenizer",
        "builtin",
    ]
    if target == "llm":
        command.extend(
            [
                "--streaming",
                "--isl",
                os.getenv("AIPERF_LLM_ISL", "2048"),
                "--osl",
                os.getenv("AIPERF_LLM_OSL", "512"),
            ]
        )
    elif target == "embedding":
        command.extend(["--isl", os.getenv("AIPERF_EMBEDDING_ISL", "384")])
    else:
        command.extend(
            [
                "--rankings-passages-mean",
                os.getenv("AIPERF_RERANK_PASSAGES", "32"),
                "--rankings-passages-prompt-token-mean",
                os.getenv("AIPERF_RERANK_PASSAGE_TOKENS", "256"),
                "--rankings-query-prompt-token-mean",
                os.getenv("AIPERF_RERANK_QUERY_TOKENS", "64"),
            ]
        )
    if os.getenv("AIPERF_API_KEY"):
        command.extend(["--api-key", os.environ["AIPERF_API_KEY"]])
    return command


def estimate_level_cost(
    *,
    model: str,
    concurrency: int,
) -> float:
    return estimate_cost_cny(
        model=model,
        input_tokens=int(os.getenv("AIPERF_ESTIMATED_INPUT_TOKENS", "2048")),
        output_tokens=int(os.getenv("AIPERF_ESTIMATED_OUTPUT_TOKENS", "512")),
        request_count=request_count_for(concurrency),
        rates=parse_rates(os.getenv("LOADTEST_MODEL_PRICING")),
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_matrix(
    *,
    target: str,
    base_url: str,
    model: str,
    artifact_root: Path,
    mode: str,
    allow_live_models: bool,
    max_cost_cny: float,
    estimated_cost_cny_per_request: float = 0.0,
    cost_free_endpoint: bool = False,
    levels: Sequence[int] | None = None,
    dry_run: bool = False,
) -> int:
    if target not in MATRICES:
        raise ValueError(f"unknown target: {target}")
    selected = tuple(levels or MATRICES[target])
    if not selected or any(value not in MATRICES[target] for value in selected):
        raise ValueError("levels must be selected from the declared concurrency matrix")
    if mode == "live" and (not allow_live_models or max_cost_cny <= 0):
        raise RuntimeError(
            "live runs require --allow-live-models and a positive --max-cost-cny"
        )
    if estimated_cost_cny_per_request < 0:
        raise ValueError("estimated per-request cost cannot be negative")
    if (
        mode == "live"
        and target in {"embedding", "reranker"}
        and estimated_cost_cny_per_request == 0
        and not cost_free_endpoint
    ):
        raise RuntimeError(
            "live Embedding/Reranker runs require "
            "--estimated-cost-cny-per-request or --cost-free-endpoint"
        )
    run_id = os.getenv("LOADTEST_RUN_ID") or f"aiperf-{uuid.uuid4().hex[:12]}"
    reserved_cost = 0.0
    runs: list[dict[str, object]] = []
    for concurrency in selected:
        if mode != "live":
            estimated = 0.0
        elif target == "llm":
            estimated = estimate_level_cost(model=model, concurrency=concurrency)
        else:
            estimated = request_count_for(concurrency) * estimated_cost_cny_per_request
        if reserved_cost + estimated > max_cost_cny and mode == "live":
            raise RuntimeError(
                "next AIPerf level would exceed the configured CNY cost ceiling"
            )
        reserved_cost += estimated
        artifact_dir = artifact_root / run_id / target / f"concurrency-{concurrency}"
        command = build_aiperf_command(
            target=target,
            base_url=base_url,
            model=model,
            concurrency=concurrency,
            artifact_dir=artifact_dir,
            run_id=run_id,
        )
        record = {
            "target": target,
            "concurrency": concurrency,
            "request_count": request_count_for(concurrency),
            "estimated_cost_cny": round(estimated, 6),
            "status": "dry_run" if dry_run else "running",
        }
        runs.append(record)
        if not dry_run:
            completed = subprocess.run(command, check=False)
            record["exit_code"] = completed.returncode
            record["status"] = "passed" if completed.returncode == 0 else "failed"
            if completed.returncode != 0:
                break
    manifest = {
        "format_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "target": target,
        "model": model,
        "base_url": redact_url(base_url),
        "cost_free_endpoint": cost_free_endpoint,
        "estimated_cost_cny_per_request": estimated_cost_cny_per_request,
        "reserved_cost_cny": round(reserved_cost, 6),
        "runs": runs,
    }
    _write_json(artifact_root / run_id / target / "run-manifest.json", manifest)
    return 0 if all(item["status"] in {"passed", "dry_run"} for item in runs) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the pinned AIPerf concurrency matrices.")
    parser.add_argument("--target", choices=tuple(MATRICES), required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", choices=("stub", "live"), default="stub")
    parser.add_argument("--allow-live-models", action="store_true")
    parser.add_argument("--max-cost-cny", type=float, default=0.0)
    parser.add_argument(
        "--estimated-cost-cny-per-request",
        type=float,
        default=float(os.getenv("AIPERF_ESTIMATED_COST_CNY_PER_REQUEST", "0")),
        help="Required for priced live Embedding/Reranker endpoints.",
    )
    parser.add_argument(
        "--cost-free-endpoint",
        action="store_true",
        help="Explicitly declare a live Embedding/Reranker endpoint free of charge.",
    )
    parser.add_argument("--level", type=int, action="append")
    parser.add_argument("--artifact-root", type=Path, default=Path("loadtests/results"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return run_matrix(
        target=args.target,
        base_url=args.base_url,
        model=args.model,
        artifact_root=args.artifact_root,
        mode=args.mode,
        allow_live_models=args.allow_live_models,
        max_cost_cny=args.max_cost_cny,
        estimated_cost_cny_per_request=args.estimated_cost_cny_per_request,
        cost_free_endpoint=args.cost_free_endpoint,
        levels=args.level,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())

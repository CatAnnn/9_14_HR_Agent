from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.repositories.postgres_repository import PostgresRepository  # noqa: E402
from backend.schemas.state import RehearsalRuntimeContext, SessionState  # noqa: E402
from backend.services.application_runtime import ApplicationRuntime  # noqa: E402
from backend.services.executor_utils import run_db_with_context  # noqa: E402


DEFAULT_TURNS = [
    "我们先回顾一下你本周期的主要目标和实际完成情况。",
    "你认为目前结果与目标之间最主要的差距是什么？",
    "这些差距背后的原因和你需要的支持分别是什么？",
    "接下来你准备采取哪些具体行动，如何衡量进展？",
    "我们确认一下行动、支持资源和下一次检查的时间。",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark Guidance, five rehearsal turns, and Coach with cloned "
            "sessions. This invokes live configured models and modifies caches."
        )
    )
    parser.add_argument("--source-session-id", required=True)
    parser.add_argument("--concurrency", default="1,10,30")
    parser.add_argument("--turns-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-benchmark-sessions", action="store_true")
    parser.add_argument("--confirm-live-model-cost", action="store_true")
    parser.add_argument("--allow-cache-clear", action="store_true")
    return parser.parse_args()


def parse_concurrency(raw: str) -> list[int]:
    values = sorted({int(value.strip()) for value in raw.split(",") if value.strip()})
    if not values or any(value <= 0 for value in values):
        raise ValueError("Concurrency values must be positive integers.")
    return values


def load_turns(path: Path | None) -> list[str]:
    if path is None:
        return list(DEFAULT_TURNS)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Turns file must contain a JSON list.")
    turns = [str(value).strip() for value in payload if str(value).strip()]
    if len(turns) != 5:
        raise ValueError("Core-flow benchmark requires exactly five manager turns.")
    return turns


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 3)


def benchmark_clone(source: SessionState) -> SessionState:
    now = datetime.now(timezone.utc)
    state = source.model_copy(deep=True)
    state.session_id = f"benchmark-{uuid4().hex}"
    state.stage = "setup_ready"
    state.run_mode = "guidance_then_rehearsal"
    state.setup_ready = True
    state.guidance_report_id = None
    state.coach_report_id = None
    state.rehearsal_context = RehearsalRuntimeContext()
    state.conversation = []
    state.user_turn_count = 0
    state.warnings = []
    state.created_at = now
    state.updated_at = now
    state.ended_at = None
    return state


def cleanup_sessions(repository: PostgresRepository, session_ids: list[str]) -> None:
    if not session_ids:
        return
    with repository.connection() as conn:
        conn.execute(
            "DELETE FROM guidance_reports WHERE session_id = ANY(%s)",
            (session_ids,),
        )
        conn.execute(
            "DELETE FROM coach_reports WHERE session_id = ANY(%s)",
            (session_ids,),
        )
        conn.execute(
            "DELETE FROM sessions WHERE session_id = ANY(%s)",
            (session_ids,),
        )


async def run_one(
    runtime: ApplicationRuntime,
    source: SessionState,
    turns: list[str],
) -> dict[str, Any]:
    state = benchmark_clone(source)
    await run_db_with_context(
        runtime.workflow_db_executor,
        "benchmark.session.create",
        runtime.session_service.save_session,
        state,
    )
    timings: dict[str, float] = {}
    started = time.perf_counter()
    try:
        stage_started = time.perf_counter()
        await runtime.guidance_service.generate(state.session_id)
        timings["guidance_ms"] = (time.perf_counter() - stage_started) * 1000

        stage_started = time.perf_counter()
        for turn in turns:
            await runtime.rehearsal_service.send_manager_message(
                state.session_id,
                turn,
            )
        timings["rehearsal_five_turns_ms"] = (
            time.perf_counter() - stage_started
        ) * 1000

        stage_started = time.perf_counter()
        await runtime.coach_service.generate(state.session_id)
        timings["coach_ms"] = (time.perf_counter() - stage_started) * 1000
        timings["total_ms"] = (time.perf_counter() - started) * 1000
        return {
            "session_id": state.session_id,
            "timings": timings,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        timings["total_ms"] = (time.perf_counter() - started) * 1000
        return {
            "session_id": state.session_id,
            "timings": timings,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


async def run_group(
    runtime: ApplicationRuntime,
    source: SessionState,
    turns: list[str],
    concurrency: int,
    cache_state: str,
) -> dict[str, Any]:
    wall_started = time.perf_counter()
    outcomes = await asyncio.gather(
        *(run_one(runtime, source, turns) for _ in range(concurrency))
    )
    wall_seconds = max(0.001, time.perf_counter() - wall_started)
    successful = [item for item in outcomes if item["error"] is None]
    total_values = [item["timings"]["total_ms"] for item in successful]
    settings = runtime.settings
    return {
        "cache_state": cache_state,
        "session_concurrency": concurrency,
        "rag_concurrency": settings.rag_db_search_max_concurrency,
        "rerank_concurrency": settings.rag_rerank_max_concurrency,
        "model_concurrency": settings.model_total_max_concurrency,
        "workflow_db_workers": settings.workflow_db_thread_pool_max_workers,
        "postgres_pool_max_size": settings.postgres_pool_max_size,
        "request_count": len(outcomes),
        "success_count": len(successful),
        "error_count": len(outcomes) - len(successful),
        "error_rate": round((len(outcomes) - len(successful)) / len(outcomes), 6),
        "throughput": round(len(successful) / wall_seconds, 6),
        "p50_ms": percentile(total_values, 0.50),
        "p95_ms": percentile(total_values, 0.95),
        "guidance_p95_ms": percentile(
            [item["timings"]["guidance_ms"] for item in successful],
            0.95,
        ),
        "rehearsal_five_turns_p95_ms": percentile(
            [item["timings"]["rehearsal_five_turns_ms"] for item in successful],
            0.95,
        ),
        "coach_p95_ms": percentile(
            [item["timings"]["coach_ms"] for item in successful],
            0.95,
        ),
        "wall_seconds": round(wall_seconds, 3),
        "error_examples": [
            item["error"] for item in outcomes if item["error"] is not None
        ][:5],
        "benchmark_session_ids": [item["session_id"] for item in outcomes],
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    runtime = ApplicationRuntime()
    runtime.start()
    repository = PostgresRepository(initialize=False)
    created_session_ids: list[str] = []
    try:
        source = await run_db_with_context(
            runtime.workflow_db_executor,
            "benchmark.source_session.read",
            runtime.session_service.get_session,
            args.source_session_id,
        )
        if not source.setup_ready:
            raise ValueError("Source session must have completed setup.")
        turns = load_turns(args.turns_file)
        results: list[dict[str, Any]] = []
        for concurrency in parse_concurrency(args.concurrency):
            await asyncio.to_thread(runtime.retrieval_cache.clear)
            cold = await run_group(
                runtime,
                source,
                turns,
                concurrency,
                "cold",
            )
            created_session_ids.extend(cold.pop("benchmark_session_ids"))
            results.append(cold)

            hot = await run_group(
                runtime,
                source,
                turns,
                concurrency,
                "hot",
            )
            created_session_ids.extend(hot.pop("benchmark_session_ids"))
            results.append(hot)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_session_id": args.source_session_id,
            "manager_turn_count": len(turns),
            "results": results,
        }
    finally:
        if not args.keep_benchmark_sessions:
            await run_db_with_context(
                runtime.workflow_db_executor,
                "benchmark.sessions.cleanup",
                cleanup_sessions,
                repository,
                created_session_ids,
            )
        await runtime.shutdown()
        PostgresRepository.close_connection_pools()


def main() -> int:
    args = parse_args()
    if not args.confirm_live_model_cost:
        raise SystemExit(
            "Refusing to call live models without --confirm-live-model-cost."
        )
    if not args.allow_cache_clear:
        raise SystemExit(
            "Cold-cache benchmarking clears the shared RAG cache; "
            "pass --allow-cache-clear during an approved benchmark window."
        )
    report = asyncio.run(run_benchmark(args))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if all(row["error_count"] == 0 for row in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

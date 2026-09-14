from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import resource
import sys
import time
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hold direct PostgreSQL connections for deployment capacity validation."
    )
    parser.add_argument("--connections", type=int, default=1000)
    parser.add_argument("--duration-seconds", type=int, default=300)
    parser.add_argument("--connect-concurrency", type=int, default=128)
    parser.add_argument("--sample-interval-seconds", type=float, default=5.0)
    return parser.parse_args()


def file_descriptor_count() -> int | None:
    proc_path = Path("/proc/self/fd")
    if not proc_path.exists():
        return None
    return sum(1 for _ in proc_path.iterdir())


async def connect_one(
    database_url: str,
    *,
    semaphore: asyncio.Semaphore,
    application_name: str,
):
    import psycopg

    async with semaphore:
        return await psycopg.AsyncConnection.connect(
            database_url,
            autocommit=True,
            application_name=application_name,
            connect_timeout=30,
        )


async def close_connections(connections: list[Any]) -> None:
    await asyncio.gather(
        *(connection.close() for connection in connections),
        return_exceptions=True,
    )


async def run_stress(args: argparse.Namespace) -> dict[str, Any]:
    target = max(1, int(args.connections))
    duration = max(1, int(args.duration_seconds))
    connect_limit = max(1, min(target, int(args.connect_concurrency)))
    sample_interval = max(0.1, float(args.sample_interval_seconds))
    database_url = str(get_settings().database_url)
    application_name = f"hr_agent_direct_stress_{uuid4().hex[:10]}"
    semaphore = asyncio.Semaphore(connect_limit)
    connections: list[Any] = []
    failures: list[str] = []
    samples: list[dict[str, Any]] = []
    started = time.perf_counter()

    connect_results = await asyncio.gather(
        *(
            connect_one(
                database_url,
                semaphore=semaphore,
                application_name=application_name,
            )
            for _ in range(target)
        ),
        return_exceptions=True,
    )
    for result in connect_results:
        if isinstance(result, BaseException):
            failures.append(f"{type(result).__name__}: {str(result)[:200]}")
        else:
            connections.append(result)

    try:
        deadline = time.monotonic() + duration
        while connections and time.monotonic() < deadline:
            cursor = await connections[0].execute(
                """
                SELECT COUNT(*) AS active_count
                FROM pg_stat_activity
                WHERE application_name = %s
                """,
                (application_name,),
            )
            row = await cursor.fetchone()
            active_count = int(row[0] if row else 0)
            samples.append(
                {
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "active_database_connections": active_count,
                    "client_file_descriptors": file_descriptor_count(),
                    "client_max_rss_kb": resource.getrusage(
                        resource.RUSAGE_SELF
                    ).ru_maxrss,
                }
            )
            await asyncio.sleep(min(sample_interval, max(0.0, deadline - time.monotonic())))
    finally:
        await close_connections(connections)

    minimum_active = min(
        (sample["active_database_connections"] for sample in samples),
        default=0,
    )
    return {
        "requested_connections": target,
        "opened_connections": len(connections),
        "failed_connections": len(failures),
        "failure_examples": failures[:10],
        "duration_seconds": duration,
        "connect_concurrency": connect_limit,
        "application_name": application_name,
        "peak_active_database_connections": max(
            (sample["active_database_connections"] for sample in samples),
            default=0,
        ),
        "minimum_active_database_connections": minimum_active,
        "peak_client_file_descriptors": max(
            (
                sample["client_file_descriptors"]
                for sample in samples
                if sample["client_file_descriptors"] is not None
            ),
            default=None,
        ),
        "peak_client_max_rss_kb": max(
            (sample["client_max_rss_kb"] for sample in samples),
            default=0,
        ),
        "samples": samples,
        "passed": (
            len(connections) == target
            and not failures
            and bool(samples)
            and minimum_active == target
        ),
    }


def main() -> int:
    args = parse_args()
    report = asyncio.run(run_stress(args))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

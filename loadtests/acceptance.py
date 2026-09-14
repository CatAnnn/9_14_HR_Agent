from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


REQUIRED_SYSTEM_METRICS = (
    "event_loop_lag_p95_ms",
    "database_connection_wait_p95_ms",
    "connection_pool_exhaustions",
    "deadlocks",
    "queue_depth_at_stop",
    "queue_depth_after_drain",
    "throughput_rps",
    "single_backend_throughput_rps",
    "single_user_p95_ms",
    "thirty_user_p95_ms",
)


def load_system_metrics(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("system metrics root must be a JSON object")
    return payload


def evaluate_system_metrics(metrics: Mapping[str, Any]) -> list[str]:
    missing = [name for name in REQUIRED_SYSTEM_METRICS if name not in metrics]
    if missing:
        return [f"system metrics omitted required field: {name}" for name in missing]

    failures: list[str] = []
    if float(metrics["event_loop_lag_p95_ms"]) >= 100.0:
        failures.append("event loop lag P95 is not below 100ms")
    if float(metrics["database_connection_wait_p95_ms"]) >= 100.0:
        failures.append("database connection wait P95 is not below 100ms")
    if int(metrics["connection_pool_exhaustions"]) != 0:
        failures.append("database connection pool exhaustion was observed")
    if int(metrics["deadlocks"]) != 0:
        failures.append("database deadlocks were observed")
    if int(metrics["queue_depth_after_drain"]) != 0:
        failures.append("server task queue did not drain to zero")
    if int(metrics["queue_depth_after_drain"]) > int(metrics["queue_depth_at_stop"]):
        failures.append("server task queue grew after load injection stopped")

    baseline_throughput = float(metrics["single_backend_throughput_rps"])
    if baseline_throughput <= 0:
        failures.append("single-backend throughput baseline must be positive")
    elif float(metrics["throughput_rps"]) < baseline_throughput * 2.5:
        failures.append("system throughput did not reach 2.5x the single-backend baseline")

    single_user_p95 = float(metrics["single_user_p95_ms"])
    if single_user_p95 <= 0:
        failures.append("single-user P95 baseline must be positive")
    elif float(metrics["thirty_user_p95_ms"]) > single_user_p95 * 1.05:
        failures.append("30-user P95 regressed by more than 5% from the single-user baseline")
    return failures


def validate_run(
    *,
    locust_summary_path: Path,
    system_metrics_path: Path,
) -> list[str]:
    locust_summary = json.loads(locust_summary_path.read_text(encoding="utf-8"))
    failures = list((locust_summary.get("acceptance") or {}).get("failures") or [])
    failures.extend(evaluate_system_metrics(load_system_metrics(system_metrics_path)))
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate final 400-user acceptance artifacts.")
    parser.add_argument("--locust-summary", type=Path, required=True)
    parser.add_argument("--system-metrics", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    failures = validate_run(
        locust_summary_path=args.locust_summary,
        system_metrics_path=args.system_metrics,
    )
    payload = {"passed": not failures, "failures": failures}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

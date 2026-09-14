from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
from typing import Any

from loadtests.acceptance import evaluate_system_metrics, load_system_metrics


def _entry_payload(entry: Any) -> dict[str, Any]:
    return {
        "method": entry.method,
        "name": entry.name,
        "requests": entry.num_requests,
        "failures": entry.num_failures,
        "average_ms": entry.avg_response_time,
        "median_ms": entry.median_response_time,
        "p95_ms": entry.get_response_time_percentile(0.95),
        "p99_ms": entry.get_response_time_percentile(0.99),
        "max_ms": entry.max_response_time,
        "requests_per_second": entry.current_rps,
    }


def build_locust_summary(
    environment: Any,
    coordinator: Any,
    *,
    run_id: str,
) -> dict[str, Any]:
    total = environment.stats.total
    samples = coordinator.worker_samples()
    cpu_values = [
        float(sample.get("cpu_percent") or 0.0)
        for sample in samples
    ]
    runner = environment.runner
    entries = sorted(
        environment.stats.entries.values(),
        key=lambda item: (item.method, item.name),
    )
    network_entries = [
        entry
        for entry in entries
        if entry.method not in {"SSE_PHASE", "WORKFLOW"}
    ]
    return {
        "format_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shape": os.getenv("LOADTEST_SHAPE", "smoke"),
        "model_mode": os.getenv("LOADTEST_MODEL_MODE", "stub"),
        "host": os.getenv("LOCUST_HOST", ""),
        "python": platform.python_version(),
        "worker_count": int(
            coordinator.peak_worker_count()
            or getattr(runner, "worker_count", 1)
            or 1
        ),
        "progress": coordinator.progress(),
        "workflow_failures": coordinator.failure_counts(),
        "failure_samples": coordinator.failure_samples(),
        "load_generator": {
            "sample_count": len(samples),
            "max_cpu_percent": max(cpu_values, default=0.0),
            "samples_over_70_percent": sum(
                value >= 70.0 for value in cpu_values
            ),
            "samples": samples,
        },
        "total": _entry_payload(total),
        "network_total": {
            "requests": sum(entry.num_requests for entry in network_entries),
            "failures": sum(entry.num_failures for entry in network_entries),
        },
        "requests": [_entry_payload(entry) for entry in entries],
        "errors": [
            {
                "method": error.method,
                "name": error.name,
                "error": str(error.error)[:500],
                "occurrences": error.occurrences,
            }
            for error in environment.stats.errors.values()
        ],
    }


def evaluate_locust_summary(
    summary: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    network_total = summary.get("network_total") or summary["total"]
    requests = int(network_total["requests"])
    failed = int(network_total["failures"])
    failure_rate = failed / requests if requests else 1.0
    maximum_error_rate = float(
        os.getenv("LOADTEST_MAX_ERROR_RATE", "0.01")
    )
    if failure_rate >= maximum_error_rate:
        failures.append(
            f"network error rate {failure_rate:.4%} is not below "
            f"{maximum_error_rate:.2%}"
        )
    progress = summary.get("progress") or {}
    attempted = int(progress.get("attempted", 0))
    completed = int(progress.get("completed", 0))
    workflow_failed = int(progress.get("failed", 0))
    workflow_failure_rate = workflow_failed / attempted if attempted else 1.0
    if workflow_failure_rate >= maximum_error_rate:
        failures.append(
            f"workflow failure rate {workflow_failure_rate:.4%} is not below "
            f"{maximum_error_rate:.2%}"
        )
    if attempted != completed + workflow_failed:
        failures.append(
            "accepted workflows did not all finish with success or an explicit error"
        )
    if int(progress.get("active", 0)) != 0:
        failures.append(
            "workflow queue did not drain to zero active sessions"
        )
    if completed < 1:
        failures.append("no complete workflow finished")
    if summary.get("shape") in {"staircase", "spike", "soak"} and int(
        progress.get("max_active", 0)
    ) < 400:
        failures.append(
            "test never reached 400 simultaneously active workflows"
        )
    if (summary.get("workflow_failures") or {}).get("isolation", 0):
        failures.append(
            "cross-user or cross-session isolation failure detected"
        )
    load_generator = summary.get("load_generator") or {}
    if int(load_generator.get("samples_over_70_percent", 0)) > int(
        os.getenv("LOADTEST_MAX_CPU_SAMPLES_OVER_70", "2")
    ):
        failures.append(
            "load generator CPU remained at or above 70%"
        )
    return failures


def _system_metrics_acceptance(
    *,
    shape: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    configured = os.getenv("LOADTEST_SYSTEM_METRICS_FILE", "").strip()
    require_raw = os.getenv("LOADTEST_REQUIRE_SYSTEM_METRICS", "").strip().lower()
    required = (
        require_raw == "true"
        if require_raw
        else shape in {"staircase", "spike", "soak"}
    )
    if not configured:
        return (
            None,
            ["final 400-user acceptance requires LOADTEST_SYSTEM_METRICS_FILE"]
            if required
            else [],
        )
    path = Path(configured)
    if not path.is_file():
        return None, [f"system metrics file does not exist: {path}"]
    try:
        metrics = load_system_metrics(path)
        return metrics, evaluate_system_metrics(metrics)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, [f"system metrics could not be validated: {type(exc).__name__}"]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def write_locust_artifacts(
    environment: Any,
    coordinator: Any,
    *,
    run_id: str,
) -> list[str]:
    result_dir = (
        Path(os.getenv("LOADTEST_RESULTS_DIR", "loadtests/results"))
        / run_id
    )
    summary = build_locust_summary(
        environment,
        coordinator,
        run_id=run_id,
    )
    failures = evaluate_locust_summary(summary)
    system_metrics, system_failures = _system_metrics_acceptance(
        shape=str(summary["shape"]),
    )
    failures.extend(system_failures)
    summary["system_metrics"] = system_metrics
    summary["acceptance"] = {
        "passed": not failures,
        "failures": failures,
    }
    write_json(result_dir / "locust-summary.json", summary)
    write_json(
        result_dir / "failure-summary.json",
        {
            "run_id": run_id,
            "request_failures": summary["errors"],
            "workflow_failure_samples": summary["failure_samples"],
            "acceptance_failures": failures,
        },
    )
    manifest = {
        "format_version": 1,
        "run_id": run_id,
        "generated_at": summary["generated_at"],
        "shape": summary["shape"],
        "model_mode": summary["model_mode"],
        "account_pool_size": int(
            os.getenv("LOADTEST_ACCOUNT_POOL_SIZE", "480")
        ),
        "workflow_user_ceiling": 400,
        "expected_workers": int(
            os.getenv("LOADTEST_EXPECTED_WORKERS", "1")
        ),
        "actual_workers": summary["worker_count"],
        "artifacts": [
            "locust-summary.json",
            "failure-summary.json",
            "locust.html",
            "locust_stats.csv",
        ],
        "acceptance": summary["acceptance"],
    }
    write_json(result_dir / "run-manifest.json", manifest)
    return failures

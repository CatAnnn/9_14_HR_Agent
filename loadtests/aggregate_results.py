from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"artifact root must be a JSON object: {path}")
    return payload


def aggregate(
    *,
    run_dir: Path,
    require_aiperf: bool,
    require_asr: bool,
) -> dict[str, Any]:
    locust_path = run_dir / "locust-summary.json"
    if not locust_path.is_file():
        raise FileNotFoundError(f"missing Locust summary: {locust_path}")
    locust = _read(locust_path)
    failures = list((locust.get("acceptance") or {}).get("failures") or [])

    aiperf: dict[str, Any] = {}
    for target in ("llm", "embedding", "reranker"):
        path = run_dir / target / "run-manifest.json"
        if not path.is_file():
            if require_aiperf:
                failures.append(f"missing AIPerf {target} manifest")
            continue
        payload = _read(path)
        aiperf[target] = payload
        if any(item.get("status") != "passed" for item in payload.get("runs") or []):
            failures.append(f"AIPerf {target} matrix contains failed or incomplete levels")

    asr: list[dict[str, Any]] = []
    asr_dir = run_dir / "asr"
    if asr_dir.is_dir():
        for path in sorted(asr_dir.glob("concurrency-*.json")):
            payload = _read(path)
            asr.append(payload)
            if not bool((payload.get("acceptance") or {}).get("passed")):
                failures.append(f"ASR acceptance failed: {path.name}")
    if require_asr:
        observed = {int(item.get("concurrency") or 0) for item in asr}
        missing = {8, 32, 40, 64} - observed
        for concurrency in sorted(missing):
            failures.append(f"missing ASR concurrency-{concurrency} artifact")

    return {
        "format_version": 1,
        "run_id": str(locust.get("run_id") or run_dir.name),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workflow_user_ceiling": 400,
        "account_pool_size": 480,
        "locust": {
            "shape": locust.get("shape"),
            "model_mode": locust.get("model_mode"),
            "worker_count": locust.get("worker_count"),
            "progress": locust.get("progress"),
            "total": locust.get("total"),
            "network_total": locust.get("network_total"),
        },
        "aiperf": aiperf,
        "asr": asr,
        "artifacts": sorted(
            str(path.relative_to(run_dir))
            for path in run_dir.rglob("*")
            if path.is_file() and path.name != "run-manifest.json"
        ),
        "acceptance": {"passed": not failures, "failures": failures},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate Locust, AIPerf, and ASR artifacts.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--require-aiperf", action="store_true")
    parser.add_argument("--require-asr", action="store_true")
    args = parser.parse_args(argv)
    payload = aggregate(
        run_dir=args.run_dir,
        require_aiperf=args.require_aiperf,
        require_asr=args.require_asr,
    )
    output = args.run_dir / "run-manifest.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"artifact": str(output), "passed": payload["acceptance"]["passed"]}))
    return 0 if payload["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

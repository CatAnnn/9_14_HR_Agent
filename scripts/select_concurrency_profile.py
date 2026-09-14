from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.concurrency_tuning import (  # noqa: E402
    environment_overrides,
    select_concurrency_profile,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select a zero-error concurrency profile from benchmark JSON."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--workflow-db-workers", type=int, default=8)
    parser.add_argument("--throughput-tolerance", type=float, default=0.05)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Benchmark JSON must be a list or contain a results list.")
    profile = select_concurrency_profile(
        rows,
        workflow_db_workers=args.workflow_db_workers,
        throughput_tolerance=args.throughput_tolerance,
    )
    result = {
        "selected_profile": profile,
        "environment": environment_overrides(profile),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

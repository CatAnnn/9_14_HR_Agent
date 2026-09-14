from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings  # noqa: E402
from backend.observability.metrics import shutdown_metrics_logger  # noqa: E402
from backend.services.http_client import close_shared_async_client  # noqa: E402
from backend.services.langchain_llm_service import LangChainLLMService  # noqa: E402


class ModelSchemaPreflightOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Verify Model Farm JSON Schema support before backend release."
    )
    parser.add_argument("--attempts", type=int, default=20)
    parser.add_argument("--primary-model", default=settings.guidance_model)
    parser.add_argument("--retry-model", default=settings.model_retry_race_model)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    return parser


def _error_code(error: BaseException) -> str:
    return str(getattr(error, "code", "") or type(error).__name__)


async def run_schema_preflight(
    *,
    service: LangChainLLMService,
    models: list[tuple[str, str]],
    attempts: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    results: list[dict[str, Any]] = []
    for role, model in models:
        succeeded = 0
        failures: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            try:
                output = await service.ainvoke_structured_single(
                    prompt='Return {"status":"ok"}.',
                    schema=ModelSchemaPreflightOutput,
                    task_name="schema_preflight",
                    model=model,
                    temperature=0.0,
                    max_tokens=32,
                    timeout_seconds=timeout_seconds,
                    enable_thinking=False,
                    stream=False,
                    structured_transport="json_schema",
                    json_schema_strict=False,
                )
                if output.status != "ok":
                    raise ValueError("unexpected preflight status")
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {
                        "attempt": attempt,
                        "error_code": _error_code(exc),
                    }
                )
        results.append(
            {
                "role": role,
                "model": model,
                "attempts": attempts,
                "succeeded": succeeded,
                "failed": len(failures),
                "failures": failures,
            }
        )
    return {
        "event": "model_json_schema_preflight",
        "passed": all(item["failed"] == 0 for item in results),
        "strict": False,
        "structured_transport": "json_schema",
        "results": results,
    }


async def _async_main(args: argparse.Namespace) -> int:
    try:
        report = await run_schema_preflight(
            service=LangChainLLMService(),
            models=[
                ("primary", str(args.primary_model).strip()),
                ("retry", str(args.retry_model).strip()),
            ],
            attempts=int(args.attempts),
            timeout_seconds=float(args.timeout_seconds),
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["passed"] else 1
    finally:
        await close_shared_async_client()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    finally:
        shutdown_metrics_logger()


if __name__ == "__main__":
    raise SystemExit(main())

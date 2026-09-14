from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings  # noqa: E402
from backend.observability.metrics import shutdown_metrics_logger  # noqa: E402
from backend.services.http_client import close_shared_async_client  # noqa: E402
from backend.services.retrieval_service import RetrievalService  # noqa: E402
from backend.tools.rag_search_tool import (  # noqa: E402
    ALLOWED_RAG_SCOPES,
    create_rag_tool_agent,
)


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Verify the standalone model RAG tool call and ToolMessage loop."
    )
    parser.add_argument(
        "--query",
        default="博世高绩效文化下，管理者应如何开展绩效反馈？",
    )
    parser.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        choices=ALLOWED_RAG_SCOPES,
        help="Limit the preflight to an approved scope; may be repeated.",
    )
    parser.add_argument(
        "--model",
        default=settings.model_for_task("model_rag_tool"),
    )
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser


def _tool_payload(message: ToolMessage) -> dict[str, Any] | None:
    content = message.content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return content if isinstance(content, dict) else None


async def run_rag_tool_preflight(
    *,
    query: str,
    scopes: list[str] | None,
    model: str,
    timeout_seconds: float,
    retrieval_service: RetrievalService | None = None,
) -> dict[str, Any]:
    owned_retrieval = retrieval_service is None
    retrieval = retrieval_service or RetrievalService()
    started = time.perf_counter()
    try:
        agent = create_rag_tool_agent(retrieval, explicit_model=model)
        user_request = query
        if scopes:
            user_request = (
                f"请只在这些知识域中检索后回答：{','.join(scopes)}。问题：{query}"
            )
        async with asyncio.timeout(timeout_seconds):
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": user_request}]}
            )

        messages = result.get("messages", []) if isinstance(result, dict) else []
        tool_call_count = sum(
            len(message.tool_calls)
            for message in messages
            if isinstance(message, AIMessage)
        )
        tool_payloads = [
            payload
            for message in messages
            if isinstance(message, ToolMessage)
            for payload in [_tool_payload(message)]
            if payload is not None
        ]
        used_scopes = sorted(
            {
                str(scope)
                for payload in tool_payloads
                for scope in payload.get("scopes", [])
            }
        )
        chunk_ids = [
            str(match.get("chunk_id"))
            for payload in tool_payloads
            for match in payload.get("matches", [])
            if isinstance(match, dict) and match.get("chunk_id")
        ]
        final_message = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, AIMessage) and not message.tool_calls
            ),
            None,
        )
        final_chars = len(str(final_message.content)) if final_message else 0
        tool_failed = any(
            payload.get("status") == "error" for payload in tool_payloads
        )
        passed = bool(
            tool_call_count and tool_payloads and final_chars and not tool_failed
        )
        return {
            "event": "rag_model_tool_preflight",
            "passed": passed,
            "model": model,
            "tool_call_count": tool_call_count,
            "scopes": used_scopes,
            "chunk_ids": chunk_ids,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "final_response_chars": final_chars,
        }
    finally:
        if owned_retrieval:
            await retrieval.shutdown()


async def _async_main(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    try:
        try:
            report = await run_rag_tool_preflight(
                query=str(args.query),
                scopes=list(args.scopes or []),
                model=str(args.model),
                timeout_seconds=float(args.timeout_seconds),
            )
        except Exception:  # noqa: BLE001
            report = {
                "passed": False,
                "tool_call_count": 0,
                "scopes": [],
                "chunk_ids": [],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        terminal_report = {
            "tool_call_count": report["tool_call_count"],
            "scopes": report["scopes"],
            "chunk_ids": report["chunk_ids"],
            "elapsed_ms": report["elapsed_ms"],
        }
        print(json.dumps(terminal_report, ensure_ascii=False, sort_keys=True))
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

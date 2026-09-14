from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from typing import Any, AsyncIterator
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from loadtests.model_stub.schema_values import value_for_schema


app = FastAPI(title="06_emotion deterministic Model Farm stub")
_active = 0
_total = 0
_lock = asyncio.Lock()


def _env_ms(name: str, default: float) -> float:
    return max(0.0, float(os.getenv(name, str(default)))) / 1000.0


def _failure_rate() -> float:
    return min(1.0, max(0.0, float(os.getenv("MODEL_STUB_ERROR_RATE", "0"))))


def _should_fail(request_id: str) -> bool:
    bucket = int(hashlib.sha256(request_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < _failure_rate()


async def _enter() -> None:
    global _active, _total
    async with _lock:
        _active += 1
        _total += 1


async def _leave() -> None:
    global _active
    async with _lock:
        _active = max(0, _active - 1)


def _schema_contract(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    response_format = payload.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return None
    config = response_format.get("json_schema") or {}
    schema = config.get("schema") or {}
    return str(config.get("name") or schema.get("title") or "structured_output"), schema


def _response_text(payload: dict[str, Any], *, model: str) -> str:
    contract = _schema_contract(payload)
    if contract is not None:
        schema_name, schema = contract
        canonical_name = str(schema.get("title") or schema_name)
        retry_schemas = {
            value.strip()
            for value in os.getenv(
                "MODEL_STUB_RETRY_RACE_SCHEMAS",
                (
                    "IntentPerformanceDraftOutput,GuidanceStartOutput,"
                    "GuidanceEmotionOutput,GuidanceRequirementOutput,"
                    "GuidancePlanOutput,CoachDimensionModelOutput"
                ),
            ).split(",")
            if value.strip()
        }
        retry_model = os.getenv("MODEL_STUB_RETRY_MODEL", "loadtest-retry")
        if (
            os.getenv("MODEL_STUB_TRIGGER_RETRY_RACE", "false").lower() == "true"
            and model != retry_model
            and canonical_name in retry_schemas
        ):
            return '{"loadtest_malformed":'
        return json.dumps(
            value_for_schema(canonical_name, schema),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return "我理解你的反馈。为了确保事实一致，请先说明最关键的结果与依据，我们再确认下一步行动。"


def _usage(payload: dict[str, Any], text: str) -> dict[str, int]:
    serialized = json.dumps(payload.get("messages") or [], ensure_ascii=False)
    prompt_tokens = max(1, math.ceil(len(serialized) / 3.2))
    completion_tokens = max(1, math.ceil(len(text) / 3.2))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _chat_chunk(request_id: str, model: str, text: str, *, nested: bool) -> dict[str, Any]:
    chunk = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return {"data": chunk} if nested else chunk


async def _stream_chat(
    *,
    request_id: str,
    model: str,
    text: str,
    nested: bool,
    payload: dict[str, Any],
) -> AsyncIterator[bytes]:
    try:
        await asyncio.sleep(_env_ms("MODEL_STUB_TTFT_MS", 80))
        part_size = max(1, int(os.getenv("MODEL_STUB_CHUNK_CHARS", "24")))
        for position in range(0, len(text), part_size):
            chunk = _chat_chunk(
                request_id,
                model,
                text[position : position + part_size],
                nested=nested,
            )
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
            await asyncio.sleep(_env_ms("MODEL_STUB_ITL_MS", 8))
        final = {
            "id": request_id,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": _usage(payload, text),
        }
        if nested:
            final = {"data": final}
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
    finally:
        await _leave()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "active_requests": _active, "total_requests": _total}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": "loadtest-chat", "object": "model", "owned_by": "loadtest"},
            {"id": "loadtest-embedding", "object": "model", "owned_by": "loadtest"},
            {"id": "loadtest-reranker", "object": "model", "owned_by": "loadtest"},
            {"id": "loadtest-retry", "object": "model", "owned_by": "loadtest"},
        ],
    }


@app.get("/metrics")
async def metrics() -> StreamingResponse:
    body = (
        "# TYPE model_stub_active_requests gauge\n"
        f"model_stub_active_requests {_active}\n"
        "# TYPE model_stub_requests_total counter\n"
        f"model_stub_requests_total {_total}\n"
    )
    return StreamingResponse(iter([body.encode()]), media_type="text/plain; version=0.0.4")


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    x_load_test_run_id: str | None = Header(default=None),
    x_model_stub_envelope: str | None = Header(default=None),
) -> Response:
    payload = await request.json()
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    if _should_fail(request_id):
        raise HTTPException(status_code=503, detail="deterministic injected model failure")
    await _enter()
    model = str(payload.get("model") or "loadtest-chat")
    text = _response_text(payload, model=model)
    nested = str(x_model_stub_envelope or os.getenv("MODEL_STUB_ENVELOPE", "openai")).lower() == "bosch"
    if payload.get("stream"):
        return StreamingResponse(
            _stream_chat(
                request_id=request_id,
                model=model,
                text=text,
                nested=nested,
                payload=payload,
            ),
            media_type="text/event-stream",
            headers={"X-Load-Test-Run-ID": x_load_test_run_id or ""},
        )
    try:
        await asyncio.sleep(_env_ms("MODEL_STUB_RESPONSE_MS", 120))
        response = {
            "id": request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": _usage(payload, text),
        }
        return JSONResponse({"data": response} if nested else response)
    finally:
        await _leave()


def _embedding(text: str, dimensions: int) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 8) for value in values]


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> dict[str, Any]:
    payload = await request.json()
    inputs = payload.get("input", payload.get("texts"))
    values = inputs if isinstance(inputs, list) else [inputs]
    dimensions = max(1, min(16_000, int(payload.get("dimensions") or 1024)))
    await _enter()
    try:
        await asyncio.sleep(_env_ms("MODEL_STUB_EMBEDDING_MS", 20))
        return {
            "object": "list",
            "model": str(payload.get("model") or "loadtest-embedding"),
            "data": [
                {"object": "embedding", "index": index, "embedding": _embedding(str(value), dimensions)}
                for index, value in enumerate(values)
            ],
            "usage": {
                "prompt_tokens": sum(max(1, len(str(value)) // 3) for value in values),
                "total_tokens": sum(max(1, len(str(value)) // 3) for value in values),
            },
        }
    finally:
        await _leave()


@app.post("/v1/rerank")
async def rerank(request: Request) -> dict[str, Any]:
    payload = await request.json()
    documents = list(payload.get("documents") or [])
    query_terms = set(str(payload.get("query") or "").casefold().split())
    top_n = int(payload.get("top_n") or len(documents))
    await _enter()
    try:
        await asyncio.sleep(_env_ms("MODEL_STUB_RERANK_MS", 40))
        scored = []
        for index, document in enumerate(documents):
            document_text = document.get("text", "") if isinstance(document, dict) else document
            terms = set(str(document_text).casefold().split())
            overlap = len(query_terms & terms)
            score = overlap + 1.0 / (index + 2.0)
            scored.append({"index": index, "relevance_score": round(score, 8)})
        scored.sort(key=lambda item: (-item["relevance_score"], item["index"]))
        return {"model": str(payload.get("model") or "loadtest-reranker"), "results": scored[:top_n]}
    finally:
        await _leave()

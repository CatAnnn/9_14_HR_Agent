from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(600, connect=20),
        limits=httpx.Limits(max_connections=512, max_keepalive_connections=256),
    )
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


app = FastAPI(title="AIPerf Bosch response adapter", lifespan=lifespan)


def _upstream_url(path: str) -> str:
    base = os.getenv("BOSCH_ADAPTER_UPSTREAM", "").rstrip("/")
    if not base:
        raise HTTPException(503, "BOSCH_ADAPTER_UPSTREAM is not configured")
    return f"{base}/{path.lstrip('/')}"


def _configured_auth_headers() -> dict[str, str]:
    api_key = os.getenv("BOSCH_ADAPTER_API_KEY", "").strip()
    if not api_key:
        return {}
    header = os.getenv("BOSCH_ADAPTER_API_KEY_HEADER", "Authorization").strip()
    scheme = os.getenv("BOSCH_ADAPTER_AUTH_SCHEME", "Bearer").strip()
    if not header or any(character in header for character in "\r\n:"):
        raise HTTPException(500, "Bosch adapter authentication header is invalid")
    value = f"{scheme} {api_key}" if scheme else api_key
    return {header: value}


def _headers(request: Request) -> dict[str, str]:
    headers = {"Content-Type": "application/json", **_configured_auth_headers()}
    authorization = request.headers.get("authorization")
    if authorization and "Authorization" not in headers:
        headers["Authorization"] = authorization
    run_id = request.headers.get("x-load-test-run-id")
    if run_id:
        headers["X-Load-Test-Run-ID"] = run_id
    return headers


def _unwrap(payload: object) -> object:
    while isinstance(payload, dict) and set(payload) == {"data"}:
        payload = payload["data"]
    return payload


async def _stream(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                if line:
                    yield f"{line}\n".encode()
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                yield b"data: [DONE]\n\n"
                continue
            try:
                payload = _unwrap(json.loads(raw))
                serialized = json.dumps(payload, ensure_ascii=False)
            except json.JSONDecodeError:
                serialized = raw
            yield f"data: {serialized}\n\n".encode()
    finally:
        await response.aclose()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["POST"])
async def proxy(path: str, request: Request):
    if _client is None:
        raise HTTPException(503, "adapter client is not ready")
    body = await request.body()
    upstream = await _client.send(
        _client.build_request(
            "POST",
            _upstream_url(path),
            headers=_headers(request),
            content=body,
        ),
        stream=True,
    )
    content_type = upstream.headers.get("content-type", "")
    if upstream.status_code >= 400:
        content = await upstream.aread()
        await upstream.aclose()
        return JSONResponse(
            {"error": "upstream request failed"},
            status_code=upstream.status_code,
            headers={"X-Upstream-Error-Length": str(len(content))},
        )
    if "text/event-stream" in content_type:
        return StreamingResponse(_stream(upstream), media_type="text/event-stream")
    content = await upstream.aread()
    await upstream.aclose()
    try:
        payload = _unwrap(json.loads(content))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(502, "upstream returned invalid JSON") from exc
    return JSONResponse(payload)

from __future__ import annotations

import httpx
import pytest

import backend.services.http_client as http_client


@pytest.mark.asyncio
async def test_async_http_pool_trace_records_wait(monkeypatch):
    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        http_client,
        "record_http_pool_wait",
        lambda pool, duration_ms, *, outcome: observed.append((pool, outcome)),
    )
    request = httpx.Request("GET", "https://example.test")

    await http_client._install_async_pool_trace(
        request,
        pool_name="model_farm",
    )
    trace = request.extensions["trace"]
    await trace("connection_pool.wait_for_connection.started", {})
    await trace("connection_pool.wait_for_connection.complete", {})

    assert observed == [("model_farm", "success")]


def test_sync_http_pool_trace_preserves_existing_trace(monkeypatch):
    request = httpx.Request("GET", "https://example.test")
    existing = object()
    request.extensions["trace"] = existing

    http_client._install_sync_pool_trace(
        request,
        pool_name="speech",
    )

    assert request.extensions["trace"] is existing

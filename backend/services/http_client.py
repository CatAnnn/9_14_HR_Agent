from __future__ import annotations

from dataclasses import dataclass
import threading
import time

import httpx

from backend.observability.metrics import record_http_pool_wait


_client_lock = threading.Lock()
_shared_async_clients: dict[str, httpx.AsyncClient] = {}
_shared_sync_clients: dict[str, httpx.Client] = {}


@dataclass(frozen=True, slots=True)
class _PoolConfig:
    max_connections: int
    max_keepalive_connections: int
    keepalive_expiry: float
    trust_env: bool = True


_POOL_CONFIGS = {
    "model_farm": _PoolConfig(128, 128, 120.0),
    "embedding": _PoolConfig(98, 98, 120.0),
    "reranker": _PoolConfig(98, 98, 120.0),
    "local_inference": _PoolConfig(64, 32, 120.0),
    "controller": _PoolConfig(16, 8, 30.0, trust_env=False),
    # ASR sessions are intermittent. Keeping their TLS connections around
    # avoids paying a fresh handshake after a manager pauses between turns.
    "speech": _PoolConfig(32, 16, 300.0),
}


def _normalized_pool_name(pool: str) -> str:
    name = str(pool or "model_farm").strip().lower()
    if name == "default":
        name = "model_farm"
    if name not in _POOL_CONFIGS:
        raise ValueError(f"Unknown shared HTTP client pool: {pool!r}")
    return name


def _limits(config: _PoolConfig) -> httpx.Limits:
    return httpx.Limits(
        max_connections=config.max_connections,
        max_keepalive_connections=config.max_keepalive_connections,
        keepalive_expiry=config.keepalive_expiry,
    )


async def _install_async_pool_trace(
    request: httpx.Request,
    *,
    pool_name: str,
) -> None:
    if "trace" in request.extensions:
        return
    wait_started: float | None = None

    async def trace(event_name: str, _info: dict) -> None:
        nonlocal wait_started
        if event_name == "connection_pool.wait_for_connection.started":
            wait_started = time.perf_counter()
        elif event_name in {
            "connection_pool.wait_for_connection.complete",
            "connection_pool.wait_for_connection.failed",
        } and wait_started is not None:
            record_http_pool_wait(
                pool_name,
                (time.perf_counter() - wait_started) * 1000,
                outcome=(
                    "success"
                    if event_name.endswith(".complete")
                    else "error"
                ),
            )
            wait_started = None

    request.extensions["trace"] = trace


def _install_sync_pool_trace(
    request: httpx.Request,
    *,
    pool_name: str,
) -> None:
    if "trace" in request.extensions:
        return
    wait_started: float | None = None

    def trace(event_name: str, _info: dict) -> None:
        nonlocal wait_started
        if event_name == "connection_pool.wait_for_connection.started":
            wait_started = time.perf_counter()
        elif event_name in {
            "connection_pool.wait_for_connection.complete",
            "connection_pool.wait_for_connection.failed",
        } and wait_started is not None:
            record_http_pool_wait(
                pool_name,
                (time.perf_counter() - wait_started) * 1000,
                outcome=(
                    "success"
                    if event_name.endswith(".complete")
                    else "error"
                ),
            )
            wait_started = None

    request.extensions["trace"] = trace


def get_shared_async_client(pool: str = "model_farm") -> httpx.AsyncClient:
    """Return a process-wide async client for the selected traffic class."""

    name = _normalized_pool_name(pool)
    config = _POOL_CONFIGS[name]
    with _client_lock:
        client = _shared_async_clients.get(name)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=None,
                limits=_limits(config),
                trust_env=config.trust_env,
                event_hooks={
                    "request": [
                        lambda request: _install_async_pool_trace(
                            request,
                            pool_name=name,
                        )
                    ]
                },
            )
            _shared_async_clients[name] = client
        return client


def get_shared_sync_client(pool: str = "model_farm") -> httpx.Client:
    """Return a process-wide thread-safe client for the selected traffic class."""

    name = _normalized_pool_name(pool)
    config = _POOL_CONFIGS[name]
    with _client_lock:
        client = _shared_sync_clients.get(name)
        if client is None or client.is_closed:
            client = httpx.Client(
                timeout=None,
                limits=_limits(config),
                trust_env=config.trust_env,
                event_hooks={
                    "request": [
                        lambda request: _install_sync_pool_trace(
                            request,
                            pool_name=name,
                        )
                    ]
                },
            )
            _shared_sync_clients[name] = client
        return client


async def close_shared_async_client() -> None:
    """Close and detach every shared async client during shutdown."""

    with _client_lock:
        clients = tuple(_shared_async_clients.values())
        _shared_async_clients.clear()
    for client in clients:
        if not client.is_closed:
            try:
                await client.aclose()
            except RuntimeError as exc:
                # A client may outlive its original loop during test reloads.
                # It is already unusable once that loop has closed; detaching it
                # must not prevent the current application loop from starting.
                if str(exc) != "Event loop is closed":
                    raise


def close_shared_sync_client() -> None:
    """Close and detach every shared synchronous client during shutdown."""

    with _client_lock:
        clients = tuple(_shared_sync_clients.values())
        _shared_sync_clients.clear()
    for client in clients:
        if not client.is_closed:
            client.close()

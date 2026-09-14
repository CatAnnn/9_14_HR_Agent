from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import logging
import threading
from typing import Any

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from backend.config.settings import Settings, get_settings
from backend.redis.distributed_coordination import (
    RedisDistributedCoordinator,
    get_distributed_coordinator,
)
from backend.schemas.retrieval import RetrievedChunk


logger = logging.getLogger(__name__)


class RetrievalResultCache:
    """Redis-backed RAG result cache with process-local async single-flight."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        coordinator: RedisDistributedCoordinator | None = None,
    ):
        self.settings = settings or get_settings()
        self.coordinator = coordinator or get_distributed_coordinator(self.settings)
        self._sync_client: Redis | None | bool = None
        self._async_client: AsyncRedis | None | bool = None
        self._singleflight_guard = threading.Lock()
        self._singleflight_locks: dict[tuple[int, str], tuple[asyncio.Lock, int]] = {}

    def get(self, digest: str) -> list[RetrievedChunk] | None:
        client = self._redis()
        if client is None:
            return None
        try:
            return self._decode(client.get(self.key(digest)))
        except (RedisError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("RAG result cache read failed: %s", exc)
            self._disable_sync_client(client)
            return None

    def set(
        self,
        digest: str,
        chunks: list[RetrievedChunk],
        *,
        cache_empty: bool = False,
    ) -> None:
        if not chunks and not cache_empty:
            return
        client = self._redis()
        if client is None:
            return
        try:
            client.setex(
                self.key(digest),
                max(1, int(self.settings.rag_result_cache_ttl_seconds)),
                self._encode(chunks),
            )
        except (RedisError, TypeError, ValueError) as exc:
            logger.warning("RAG result cache write failed: %s", exc)
            self._disable_sync_client(client)

    async def aget(self, digest: str) -> list[RetrievedChunk] | None:
        client = self._aredis()
        if client is None:
            return None
        try:
            return self._decode(await client.get(self.key(digest)))
        except (RedisError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Async RAG result cache read failed: %s", exc)
            await self._disable_async_client(client)
            return None

    async def aset(
        self,
        digest: str,
        chunks: list[RetrievedChunk],
        *,
        cache_empty: bool = False,
    ) -> None:
        if not chunks and not cache_empty:
            return
        client = self._aredis()
        if client is None:
            return
        try:
            await client.setex(
                self.key(digest),
                max(1, int(self.settings.rag_result_cache_ttl_seconds)),
                self._encode(chunks),
            )
        except (RedisError, TypeError, ValueError) as exc:
            logger.warning("Async RAG result cache write failed: %s", exc)
            await self._disable_async_client(client)

    def clear(self) -> int:
        client = self._redis()
        if client is None:
            return 0
        pattern = f"{self.key('')}*"
        removed = 0
        try:
            batch: list[str] = []
            for key in client.scan_iter(match=pattern, count=500):
                batch.append(str(key))
                if len(batch) >= 500:
                    removed += int(client.delete(*batch))
                    batch.clear()
            if batch:
                removed += int(client.delete(*batch))
            return removed
        except RedisError as exc:
            logger.warning("RAG result cache invalidation failed: %s", exc)
            self._disable_sync_client(client)
            return removed

    @asynccontextmanager
    async def singleflight(self, digest: str) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        map_key = (id(loop), digest)
        with self._singleflight_guard:
            lock, references = self._singleflight_locks.get(
                map_key,
                (asyncio.Lock(), 0),
            )
            self._singleflight_locks[map_key] = (lock, references + 1)
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            async with self.coordinator.lease(
                f"rag-singleflight:{digest}",
                capacity=1,
                timeout_seconds=(
                    self.settings.distributed_session_lock_timeout_seconds
                ),
            ):
                yield
        finally:
            if acquired:
                lock.release()
            with self._singleflight_guard:
                current = self._singleflight_locks.get(map_key)
                if current is not None:
                    current_lock, references = current
                    if references <= 1:
                        self._singleflight_locks.pop(map_key, None)
                    else:
                        self._singleflight_locks[map_key] = (
                            current_lock,
                            references - 1,
                        )

    async def aclose(self) -> None:
        client = self._async_client
        self._async_client = False
        if isinstance(client, AsyncRedis):
            await client.aclose()
        sync_client = self._sync_client
        self._sync_client = False
        if isinstance(sync_client, Redis):
            sync_client.close()

    def key(self, digest: str) -> str:
        prefix = self.settings.rag_result_cache_key_prefix.strip() or "hr_agent:rag_result"
        return f"{prefix}:{digest}"

    def _redis(self) -> Redis | None:
        if not self.settings.rag_result_cache_enabled or not self.settings.redis_url.strip():
            return None
        if self._sync_client is False:
            return None
        if self._sync_client is None:
            try:
                client = Redis.from_url(
                    self.settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=self.settings.redis_connect_timeout_seconds,
                    socket_timeout=self.settings.redis_socket_timeout_seconds,
                    max_connections=self.settings.effective_redis_max_connections,
                )
                client.ping()
                self._sync_client = client
            except RedisError as exc:
                logger.warning("Redis RAG result cache unavailable: %s", exc)
                self._sync_client = False
                return None
        return self._sync_client

    def _aredis(self) -> AsyncRedis | None:
        if not self.settings.rag_result_cache_enabled or not self.settings.redis_url.strip():
            return None
        if self._async_client is False:
            return None
        if self._async_client is None:
            self._async_client = AsyncRedis.from_url(
                self.settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=self.settings.redis_connect_timeout_seconds,
                socket_timeout=self.settings.redis_socket_timeout_seconds,
                max_connections=self.settings.effective_redis_max_connections,
            )
        return self._async_client

    def _disable_sync_client(self, client: Redis) -> None:
        if self._sync_client is client:
            self._sync_client = False
        try:
            client.close()
        except RedisError:
            pass

    async def _disable_async_client(self, client: AsyncRedis) -> None:
        if self._async_client is client:
            self._async_client = False
        try:
            await client.aclose()
        except RedisError:
            pass

    @staticmethod
    def _encode(chunks: list[RetrievedChunk]) -> str:
        return json.dumps(
            [chunk.model_dump(mode="json") for chunk in chunks],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode(raw: Any) -> list[RetrievedChunk] | None:
        if not raw:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, list):
            return None
        return [RetrievedChunk.model_validate(item) for item in payload]

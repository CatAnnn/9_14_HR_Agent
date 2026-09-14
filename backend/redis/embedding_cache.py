from __future__ import annotations

import hashlib
import json
import logging

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from backend.config.settings import get_settings
from backend.vectorstore.embedding_profile import embedding_profile_id


logger = logging.getLogger(__name__)


class QueryEmbeddingCache:
    """Redis cache for query embeddings used by pgvector searches.

    This cache is intentionally scoped to query embeddings only. KB ingest still
    writes document embeddings directly to PostgreSQL and does not populate Redis.
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self._client: Redis | None | bool = None
        self._async_client: AsyncRedis | None | bool = None
        self.last_hit: bool | None = None

    def get(self, query: str) -> list[float] | None:
        self.last_hit = False
        client = self._redis()
        if client is None:
            return None
        try:
            raw = client.get(self.key_for_query(query))
            if not raw:
                return None
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                return None
            self.last_hit = True
            return [float(item) for item in parsed]
        except (RedisError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Query embedding cache read failed: %s", exc)
            return None

    def set(self, query: str, embedding: list[float]) -> None:
        client = self._redis()
        if client is None:
            return
        try:
            key = self.key_for_query(query)
            payload = json.dumps([float(item) for item in embedding], separators=(",", ":"))
            ttl = int(self.settings.embedding_cache_ttl_seconds or 0)
            if ttl > 0:
                client.setex(key, ttl, payload)
            else:
                client.set(key, payload)
        except (RedisError, TypeError, ValueError) as exc:
            logger.warning("Query embedding cache write failed: %s", exc)

    async def aget(self, query: str) -> list[float] | None:
        return (await self.aget_many([query]))[0]

    async def aget_many(self, queries: list[str]) -> list[list[float] | None]:
        if not queries:
            return []
        client = self._aredis()
        if client is None:
            return [None] * len(queries)
        try:
            raw_values = await client.mget(
                [self.key_for_query(query) for query in queries]
            )
            return [self._decode_embedding(raw) for raw in raw_values]
        except (RedisError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Async query embedding cache batch read failed: %s", exc)
            return [None] * len(queries)

    async def aset(self, query: str, embedding: list[float]) -> None:
        await self.aset_many({query: embedding})

    async def aset_many(self, embeddings: dict[str, list[float]]) -> None:
        if not embeddings:
            return
        client = self._aredis()
        if client is None:
            return
        try:
            ttl = int(self.settings.embedding_cache_ttl_seconds or 0)
            pipeline = client.pipeline(transaction=False)
            for query, embedding in embeddings.items():
                key = self.key_for_query(query)
                payload = json.dumps(
                    [float(item) for item in embedding],
                    separators=(",", ":"),
                )
                if ttl > 0:
                    pipeline.setex(key, ttl, payload)
                else:
                    pipeline.set(key, payload)
            await pipeline.execute()
        except (RedisError, TypeError, ValueError) as exc:
            logger.warning("Async query embedding cache batch write failed: %s", exc)

    async def aclose(self) -> None:
        client = self._async_client
        self._async_client = False
        if isinstance(client, AsyncRedis):
            await client.aclose()
        sync_client = self._client
        self._client = False
        if isinstance(sync_client, Redis):
            sync_client.close()

    def key_for_query(self, query: str) -> str:
        normalized = " ".join(str(query or "").split())
        profile_id = getattr(self.settings, "embedding_profile_id", None)
        if not profile_id:
            defaults = get_settings()
            profile_id = embedding_profile_id(
                getattr(
                    self.settings,
                    "effective_embedding_model",
                    defaults.effective_embedding_model,
                ),
                getattr(
                    self.settings,
                    "effective_embedding_dimensions",
                    getattr(
                        self.settings,
                        "embedding_dimensions",
                        defaults.effective_embedding_dimensions,
                    ),
                ),
            )
        source = "|".join([str(profile_id), normalized])
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        prefix = self.settings.embedding_cache_key_prefix.strip() or "hr_agent:query_embedding"
        return f"{prefix}:{digest}"

    @staticmethod
    def _decode_embedding(raw: str | bytes | None) -> list[float] | None:
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                return None
            return [float(item) for item in parsed]
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _redis(self) -> Redis | None:
        if not self.settings.embedding_cache_enabled or not self.settings.redis_url.strip():
            return None
        if self._client is False:
            return None
        if self._client is None:
            try:
                client = Redis.from_url(
                    self.settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=1,
                    socket_timeout=1,
                    max_connections=self.settings.effective_redis_max_connections,
                )
                client.ping()
                self._client = client
            except RedisError as exc:
                logger.warning("Redis query embedding cache unavailable: %s", exc)
                self._client = False
                return None
        return self._client

    def _aredis(self) -> AsyncRedis | None:
        if not self.settings.embedding_cache_enabled or not self.settings.redis_url.strip():
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

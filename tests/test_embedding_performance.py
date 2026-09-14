from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.redis.embedding_cache import QueryEmbeddingCache
from backend.services.embedding_service import EmbeddingService


@pytest.mark.asyncio
async def test_embedding_cache_batches_reads_and_pipeline_writes(monkeypatch) -> None:
    calls: list[tuple] = []

    class Pipeline:
        def setex(self, key, ttl, value):
            calls.append(("setex", key, ttl, value))

        async def execute(self):
            calls.append(("execute",))

    class Redis:
        async def mget(self, keys):
            calls.append(("mget", tuple(keys)))
            return ["[1,2]", None]

        def pipeline(self, *, transaction):
            calls.append(("pipeline", transaction))
            return Pipeline()

    cache = QueryEmbeddingCache.__new__(QueryEmbeddingCache)
    cache.settings = SimpleNamespace(
        effective_embedding_provider="local_qwen",
        effective_embedding_model="embedding-model",
        embedding_url="http://embedding/v1/embeddings",
        embedding_dimensions=2,
        embedding_cache_key_prefix="test:embedding",
        embedding_cache_ttl_seconds=60,
    )
    monkeypatch.setattr(cache, "_aredis", lambda: Redis())

    values = await cache.aget_many(["first", "second"])
    await cache.aset_many({"first": [1.0, 2.0], "second": [3.0, 4.0]})

    assert values == [[1.0, 2.0], None]
    assert [call[0] for call in calls].count("mget") == 1
    assert [call[0] for call in calls].count("pipeline") == 1
    assert [call[0] for call in calls].count("execute") == 1
    assert [call[0] for call in calls].count("setex") == 2


def test_embedding_cache_key_changes_with_dimensions() -> None:
    cache = QueryEmbeddingCache.__new__(QueryEmbeddingCache)
    cache.settings = SimpleNamespace(
        effective_embedding_provider="bosch",
        effective_embedding_model="embedding-model",
        embedding_url="https://models.example/v1/embeddings",
        embedding_dimensions=1024,
        embedding_cache_key_prefix="test:embedding",
    )

    first = cache.key_for_query("same query")
    cache.settings.embedding_dimensions = 768
    second = cache.key_for_query("same query")

    assert first != second


@pytest.mark.asyncio
async def test_query_embedding_singleflight_reuses_one_model_batch(monkeypatch) -> None:
    model_calls: list[tuple[str, ...]] = []
    writes: list[dict[str, list[float]]] = []

    class Cache:
        async def aget_many(self, queries):
            return [None] * len(queries)

        async def aset_many(self, values):
            writes.append(dict(values))

        async def aclose(self):
            return None

    service = EmbeddingService.__new__(EmbeddingService)
    service.settings = SimpleNamespace(effective_embedding_model="embedding-model")
    service.query_cache = Cache()
    service._query_embedding_lock = asyncio.Lock()
    service._query_embedding_inflight = {}
    service._query_embedding_tasks = set()

    async def aembed(queries):
        model_calls.append(tuple(queries))
        await asyncio.sleep(0.01)
        return [[float(index + 1)] for index, _query in enumerate(queries)]

    monkeypatch.setattr(service, "aembed", aembed)
    monkeypatch.setattr(
        "backend.services.embedding_service.log_metric",
        lambda *_args, **_kwargs: None,
    )

    first, second = await asyncio.gather(
        service.aembed_queries(["same", "other"]),
        service.aembed_queries(["same", "other"]),
    )

    assert first == second == ([[1.0], [2.0]], [False, False])
    assert model_calls == [("same", "other")]
    assert writes == [{"same": [1.0], "other": [2.0]}]
    assert service._query_embedding_inflight == {}
    await service.shutdown()

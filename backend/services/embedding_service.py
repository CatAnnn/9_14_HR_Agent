from __future__ import annotations

import asyncio
from collections.abc import Iterator
import math
from contextlib import nullcontext
from typing import Any

from backend.config.settings import Settings, get_settings
from backend.exceptions.llm_errors import LLMError
from backend.observability.metrics import elapsed_ms, log_metric, now_ms
from backend.services.http_client import get_shared_async_client, get_shared_sync_client
from backend.services.model_api_auth import ModelAPIAuth
from backend.services.local_model_runtime import (
    LocalModelRuntimeError,
    get_local_model_runtime_recycler,
)
from backend.redis.embedding_cache import QueryEmbeddingCache
from backend.vectorstore.embedding_profile import resolve_embedding_dimensions


class EmbeddingService:
    """Embedding API adapter. Endpoint and API key are mandatory."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.auth = ModelAPIAuth(self.settings)
        self.query_cache = QueryEmbeddingCache(self.settings)
        self.runtime_recycler = get_local_model_runtime_recycler()
        self._query_embedding_lock = asyncio.Lock()
        self._query_embedding_inflight: dict[str, asyncio.Future[list[float]]] = {}
        self._query_embedding_tasks: set[asyncio.Task[None]] = set()

    def embed_query(self, text: str) -> list[float]:
        started = now_ms()
        query = str(text or "").strip()
        if not query:
            return []
        cached = self.query_cache.get(query)
        if cached is not None:
            log_metric(
                "embedding.query",
                embedding_cache_hit=True,
                embedding_model=self.settings.effective_embedding_model,
                embedding_ms=elapsed_ms(started),
            )
            return cached
        embeddings = self.embed([query])
        if len(embeddings) != 1:
            raise LLMError("Embedding API did not return exactly one query vector.")
        self.query_cache.set(query, embeddings[0])
        log_metric(
            "embedding.query",
            embedding_cache_hit=False,
            embedding_model=self.settings.effective_embedding_model,
            embedding_ms=elapsed_ms(started),
        )
        return embeddings[0]

    def embed_queries(self, texts: list[str]) -> tuple[list[list[float]], list[bool]]:
        """Embed distinct RAG queries in one request while preserving cache hits."""

        started = now_ms()
        queries = [str(text or "").strip() for text in texts]
        if not queries:
            return [], []
        if any(not query for query in queries):
            raise ValueError("Embedding batch contains an empty query.")

        vectors_by_query: dict[str, list[float]] = {}
        cache_hits_by_query: dict[str, bool] = {}
        missing_queries: list[str] = []
        for query in dict.fromkeys(queries):
            cached = self.query_cache.get(query)
            if cached is not None:
                vectors_by_query[query] = cached
                cache_hits_by_query[query] = True
            else:
                missing_queries.append(query)
                cache_hits_by_query[query] = False

        if missing_queries:
            embeddings = self.embed(missing_queries)
            if len(embeddings) != len(missing_queries):
                raise LLMError(
                    "Embedding API did not return the expected number of query vectors."
                )
            for query, embedding in zip(missing_queries, embeddings, strict=True):
                if not embedding:
                    raise LLMError("Embedding API returned an empty query vector.")
                vectors_by_query[query] = embedding
                self.query_cache.set(query, embedding)

        hits = [cache_hits_by_query[query] for query in queries]
        log_metric(
            "embedding.query_batch",
            embedding_model=self.settings.effective_embedding_model,
            embedding_query_count=len(queries),
            embedding_unique_query_count=len(vectors_by_query),
            embedding_cache_hit_count=sum(hits),
            embedding_cache_miss_count=len(hits) - sum(hits),
            embedding_ms=elapsed_ms(started),
        )
        return [vectors_by_query[query] for query in queries], hits

    async def aembed_queries(
        self,
        texts: list[str],
    ) -> tuple[list[list[float]], list[bool]]:
        """Async cached query embedding batch used by request-time RAG."""

        started = now_ms()
        queries = [str(text or "").strip() for text in texts]
        if not queries:
            return [], []
        if any(not query for query in queries):
            raise ValueError("Embedding batch contains an empty query.")

        unique_queries = list(dict.fromkeys(queries))
        bulk_get = getattr(self.query_cache, "aget_many", None)
        if callable(bulk_get):
            cached_values = await bulk_get(unique_queries)
        else:
            cached_values = await asyncio.gather(
                *(self.query_cache.aget(query) for query in unique_queries)
            )
        vectors_by_query: dict[str, list[float]] = {}
        cache_hits_by_query: dict[str, bool] = {}
        missing_queries: list[str] = []
        for query, cached in zip(unique_queries, cached_values, strict=True):
            if cached is None:
                missing_queries.append(query)
                cache_hits_by_query[query] = False
            else:
                vectors_by_query[query] = cached
                cache_hits_by_query[query] = True

        if missing_queries:
            embeddings = await self._resolve_missing_query_embeddings(missing_queries)
            for query, embedding in zip(missing_queries, embeddings, strict=True):
                vectors_by_query[query] = embedding

        hits = [cache_hits_by_query[query] for query in queries]
        log_metric(
            "embedding.query_batch",
            embedding_model=self.settings.effective_embedding_model,
            embedding_query_count=len(queries),
            embedding_unique_query_count=len(unique_queries),
            embedding_cache_hit_count=sum(hits),
            embedding_cache_miss_count=len(hits) - sum(hits),
            embedding_ms=elapsed_ms(started),
        )
        return [vectors_by_query[query] for query in queries], hits

    async def _resolve_missing_query_embeddings(
        self,
        queries: list[str],
    ) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        futures: dict[str, asyncio.Future[list[float]]] = {}
        owned: dict[str, asyncio.Future[list[float]]] = {}
        async with self._query_embedding_lock:
            for query in queries:
                future = self._query_embedding_inflight.get(query)
                if future is None:
                    future = loop.create_future()
                    future.add_done_callback(self._consume_future_exception)
                    self._query_embedding_inflight[query] = future
                    owned[query] = future
                futures[query] = future

        if owned:
            producer = asyncio.create_task(
                self._produce_query_embeddings(owned),
                name="query-embedding-singleflight",
            )
            self._query_embedding_tasks.add(producer)
            producer.add_done_callback(self._query_embedding_tasks.discard)

        return list(
            await asyncio.gather(
                *(asyncio.shield(futures[query]) for query in queries)
            )
        )

    async def _produce_query_embeddings(
        self,
        owned: dict[str, asyncio.Future[list[float]]],
    ) -> None:
        queries = list(owned)
        try:
            embeddings = await self.aembed(queries)
            if len(embeddings) != len(queries):
                raise LLMError(
                    "Embedding API did not return the expected number of query vectors."
                )
            values: dict[str, list[float]] = {}
            for query, embedding in zip(queries, embeddings, strict=True):
                if not embedding:
                    raise LLMError("Embedding API returned an empty query vector.")
                values[query] = embedding

            bulk_set = getattr(self.query_cache, "aset_many", None)
            if callable(bulk_set):
                await bulk_set(values)
            else:
                await asyncio.gather(
                    *(self.query_cache.aset(query, value) for query, value in values.items())
                )
            for query, value in values.items():
                future = owned[query]
                if not future.done():
                    future.set_result(value)
        except asyncio.CancelledError:
            for future in owned.values():
                if not future.done():
                    future.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            for future in owned.values():
                if not future.done():
                    future.set_exception(exc)
        finally:
            async with self._query_embedding_lock:
                for query, future in owned.items():
                    if self._query_embedding_inflight.get(query) is future:
                        self._query_embedding_inflight.pop(query, None)

    async def shutdown(self) -> None:
        tasks = tuple(self._query_embedding_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.query_cache.aclose()

    @staticmethod
    def _consume_future_exception(future: asyncio.Future[list[float]]) -> None:
        if future.cancelled():
            return
        future.exception()

    async def aembed(
        self,
        texts: list[str],
        *,
        ensure_local_runtime: bool = True,
    ) -> list[list[float]]:
        if ensure_local_runtime and self.settings.embedding_uses_local_runtime:
            try:
                async with self.runtime_recycler.async_lease("qwen_embedding"):
                    return await self.aembed(texts, ensure_local_runtime=False)
            except LocalModelRuntimeError as exc:
                raise LLMError(f"Local embedding service is unavailable: {exc}") from exc
        cleaned = [str(text) for text in texts if text is not None and str(text).strip()]
        if not cleaned:
            return []
        url = self.settings.embedding_url
        if not url:
            raise LLMError("Embedding API endpoint is not configured.")
        if self.settings.embedding_uses_local_runtime:
            headers = {"Content-Type": "application/json"}
        else:
            headers = await self.auth.async_headers(self.settings.api_key)
            headers.setdefault("Content-Type", "application/json")
        client = get_shared_async_client(
            "local_inference"
            if self.settings.embedding_uses_local_runtime
            else "embedding"
        )
        embeddings: list[list[float]] = []
        for batch_index, batch in enumerate(self._request_batches(cleaned), start=1):
            payload = self._request_payload(batch)
            last_error: Exception | None = None
            for attempt in range(self.settings.llm_max_retries + 1):
                try:
                    resp = await client.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=self.settings.llm_timeout_seconds,
                    )
                    if resp.status_code >= 400:
                        raise LLMError(
                            f"Embedding API HTTP {resp.status_code}: {resp.text[:1000]}"
                        )
                    batch_embeddings = self._finalize_embeddings(resp.json())
                    if len(batch_embeddings) != len(batch):
                        raise LLMError(
                            "Embedding API returned an unexpected vector count "
                            f"for batch {batch_index}."
                        )
                    embeddings.extend(batch_embeddings)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt >= self.settings.llm_max_retries:
                        raise LLMError(
                            f"Embedding invocation failed for batch {batch_index}: {last_error}"
                        ) from exc
        return embeddings

    def embed(self, texts: list[str]) -> list[list[float]]:
        cleaned = [str(text) for text in texts if text is not None and str(text).strip()]
        if not cleaned:
            return []
        url = self.settings.embedding_url
        if not url:
            raise LLMError("Embedding API endpoint is not configured.")
        headers = self._request_headers()
        uses_local_qwen = self.settings.embedding_uses_local_runtime
        runtime_lease = (
            self.runtime_recycler.lease("qwen_embedding") if uses_local_qwen else nullcontext()
        )
        try:
            with runtime_lease:
                client = get_shared_sync_client(
                    "local_inference" if uses_local_qwen else "embedding"
                )
                embeddings: list[list[float]] = []
                for batch_index, batch in enumerate(
                    self._request_batches(cleaned),
                    start=1,
                ):
                    payload = self._request_payload(batch)
                    last_error: Exception | None = None
                    for attempt in range(self.settings.llm_max_retries + 1):
                        try:
                            resp = client.post(
                                url,
                                headers=headers,
                                json=payload,
                                timeout=self.settings.llm_timeout_seconds,
                            )
                            if resp.status_code >= 400:
                                raise LLMError(
                                    f"Embedding API HTTP {resp.status_code}: {resp.text[:1000]}"
                                )
                            batch_embeddings = self._finalize_embeddings(resp.json())
                            if len(batch_embeddings) != len(batch):
                                raise LLMError(
                                    "Embedding API returned an unexpected vector count "
                                    f"for batch {batch_index}."
                                )
                            embeddings.extend(batch_embeddings)
                            break
                        except Exception as exc:  # noqa: BLE001
                            last_error = exc
                            if attempt >= self.settings.llm_max_retries:
                                raise LLMError(
                                    "Embedding invocation failed for batch "
                                    f"{batch_index}: {last_error}"
                                ) from exc
                return embeddings
        except LocalModelRuntimeError as exc:
            raise LLMError(f"Local embedding service is unavailable: {exc}") from exc

    def _request_batches(self, texts: list[str]) -> Iterator[list[str]]:
        if self.settings.embedding_uses_local_runtime:
            batch_size = len(texts)
        else:
            batch_size = int(self.settings.embedding_api_batch_size)
            # Bosch's embeddings endpoint rejects input batches larger than 10.
            # Keep the configured logical batch size while splitting each HTTP
            # request at the provider boundary so no text is dropped.
            if self.settings.embedding_provider == "bosch":
                batch_size = min(batch_size, 10)
        for start in range(0, len(texts), max(1, batch_size)):
            yield texts[start : start + batch_size]

    def _request_headers(self) -> dict[str, str]:
        if self.settings.embedding_uses_local_runtime:
            return {"Content-Type": "application/json"}
        headers = self.auth.sync_headers(self.settings.api_key)
        headers.setdefault("Content-Type", "application/json")
        return headers

    def _request_payload(self, texts: list[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.effective_embedding_model,
            "input": texts,
        }
        # vLLM 0.8.5 does not reliably honor the OpenAI dimensions field.
        # Qwen3's MRL reduction is applied and normalized after the local response.
        if not self.settings.embedding_uses_local_runtime:
            payload["dimensions"] = resolve_embedding_dimensions(self.settings)
        return payload

    def _finalize_embeddings(self, data: dict[str, Any]) -> list[list[float]]:
        embeddings = self._extract_embeddings(data)
        expected = int(resolve_embedding_dimensions(self.settings))
        model = self.settings.effective_embedding_model
        can_reduce_local_qwen = (
            self.settings.embedding_uses_local_runtime
            and "qwen3-embedding" in model.casefold()
        )
        finalized: list[list[float]] = []
        for position, embedding in enumerate(embeddings):
            if not isinstance(embedding, list) or not embedding:
                raise LLMError(
                    f"Embedding response item {position} is not a non-empty vector."
                )
            try:
                vector = [float(value) for value in embedding]
            except (TypeError, ValueError) as exc:
                raise LLMError(
                    f"Embedding response item {position} contains a non-numeric value."
                ) from exc
            if not all(math.isfinite(value) for value in vector):
                raise LLMError(
                    f"Embedding response item {position} contains a non-finite value."
                )
            actual = len(vector)
            if actual == expected:
                finalized.append(vector)
                continue
            if can_reduce_local_qwen and 32 <= expected < actual:
                reduced = vector[:expected]
                norm = math.sqrt(math.fsum(value * value for value in reduced))
                if not math.isfinite(norm) or norm <= 0:
                    raise LLMError(
                        f"Embedding response item {position} cannot be normalized."
                    )
                finalized.append([value / norm for value in reduced])
                continue
            raise LLMError(
                "Embedding dimension mismatch: "
                f"model={model}, expected={expected}, actual={actual}."
            )
        return finalized

    @staticmethod
    def _extract_embeddings(data: dict[str, Any]) -> list[list[float]]:
        items = data.get("data")
        if isinstance(items, dict):
            items = items.get("data") or items.get("embeddings")
        if not isinstance(items, list):
            raise LLMError(f"Unable to extract embeddings from response keys: {list(data.keys())}")
        parsed = []
        for position, item in enumerate(items):
            if not isinstance(item, dict) or "embedding" not in item:
                raise LLMError("Embedding response item does not contain embedding.")
            parsed.append((int(item.get("index", position)), item["embedding"]))
        parsed.sort(key=lambda pair: pair[0])
        return [embedding for _, embedding in parsed]

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from backend.exceptions.llm_errors import LLMError
from backend.schemas.retrieval import RetrievedChunk
from backend.rag.structured_facts import contextual_search_text
from backend.services.rerank_service import RerankService


class Reranker:
    DOCUMENT_POLICY_VERSION = "retrieval_unit_compound_evidence_v6"

    def __init__(self):
        self.service = RerankService()

    def rerank(
        self,
        chunks: list[RetrievedChunk],
        query: str | None = None,
        top_k: int | None = None,
        parallelism: int = 1,
        instruction: str | None = None,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        if not query or not query.strip():
            raise LLMError("Reranker requires a non-empty query in strict mode.")
        worker_count = max(1, min(int(parallelism or 1), len(chunks)))
        if worker_count <= 1:
            return self._rerank_batch(
                chunks,
                query,
                top_k,
                instruction=instruction,
            )
        batches = self._split_chunks(chunks, worker_count)
        per_batch_top_k = top_k if top_k and top_k > 0 else None
        output: list[RetrievedChunk] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    self._rerank_batch,
                    batch,
                    query,
                    per_batch_top_k,
                    instruction=instruction,
                )
                for batch in batches
            ]
            for future in as_completed(futures):
                output.extend(future.result())
        output.sort(key=lambda chunk: chunk.score, reverse=True)
        return output[: top_k or len(output)]

    async def arerank(
        self,
        chunks: list[RetrievedChunk],
        query: str | None = None,
        top_k: int | None = None,
        instruction: str | None = None,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        if not query or not query.strip():
            raise LLMError("Reranker requires a non-empty query in strict mode.")
        ranked = await self.service.arerank(
            query,
            [self._document_text(chunk) for chunk in chunks],
            top_n=top_k or len(chunks),
            instruction=instruction,
        )
        output: list[RetrievedChunk] = []
        for index, relevance_score in ranked:
            if 0 <= index < len(chunks):
                chunk = chunks[index].model_copy(deep=True)
                chunk.score = relevance_score
                output.append(chunk)
        return output[: top_k or len(output)]

    def _rerank_batch(
        self,
        chunks: list[RetrievedChunk],
        query: str,
        top_k: int | None,
        *,
        instruction: str | None,
    ) -> list[RetrievedChunk]:
        ranked = self.service.rerank(
            query,
            [self._document_text(chunk) for chunk in chunks],
            top_n=top_k or len(chunks),
            instruction=instruction,
        )
        output: list[RetrievedChunk] = []
        for index, relevance_score in ranked:
            if 0 <= index < len(chunks):
                chunk = chunks[index]
                chunk.score = relevance_score
                output.append(chunk)
        return output[: top_k or len(output)]

    @staticmethod
    def _document_text(chunk: RetrievedChunk) -> str:
        metadata = chunk.metadata or {}
        search_text = str(metadata.get("search_text") or "").strip()
        if search_text:
            return search_text
        return contextual_search_text(
            scope=chunk.scope,
            title=chunk.title,
            heading_path=metadata.get("heading_path") or (),
            text=chunk.text,
            table_headers=metadata.get("table_headers") or (),
        )

    @staticmethod
    def _split_chunks(chunks: list[RetrievedChunk], batch_count: int) -> list[list[RetrievedChunk]]:
        batch_count = max(1, min(batch_count, len(chunks)))
        batch_size = (len(chunks) + batch_count - 1) // batch_count
        return [chunks[index : index + batch_size] for index in range(0, len(chunks), batch_size)]

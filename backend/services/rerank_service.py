from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from backend.config.settings import get_settings
from backend.exceptions.llm_errors import LLMError
from backend.services.http_client import get_shared_async_client, get_shared_sync_client
from backend.services.local_model_runtime import (
    LocalModelRuntimeError,
    get_local_model_runtime_recycler,
)
from backend.services.model_api_auth import ModelAPIAuth


_QWEN_RERANK_INSTRUCTION = "Given an HR performance feedback query, retrieve the most relevant knowledge base passages."
_QWEN_RERANK_PREFIX = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the Query and the Instruct provided. "
    'Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n"
    "<|im_start|>user\n"
)
_QWEN_RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_QWEN_RERANK_MAX_DOCUMENT_CHARS = 7000
_QWEN_RERANK_ALLOWED_TOKEN_IDS_BY_MODEL: dict[str, tuple[int, int]] = {
    # Resolved with this model's tokenizer as prescribed by the official
    # Qwen3-Reranker vLLM example: yes=9693, no=2152.
    "Qwen/Qwen3-Reranker-4B": (9693, 2152),
}
_LOCAL_RERANK_PATHS = (
    "/v1/completions",
    "/v1/rerank",
    "/v1/score",
    "/completions",
    "/rerank",
    "/score",
)
_LOCAL_RERANK_TARGET_PATH = {
    "completion": "/v1/completions",
    "rerank": "/v1/rerank",
    "score": "/score",
}


class RerankService:
    """Rerank API adapter for Bosch, compatible APIs, and local Qwen/vLLM."""

    def __init__(self):
        self.settings = get_settings()
        self.auth = ModelAPIAuth()
        self.runtime_recycler = get_local_model_runtime_recycler()

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        *,
        instruction: str | None = None,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        if not query.strip():
            raise LLMError("Rerank query cannot be empty.")
        if self.settings.rerank_uses_local_runtime:
            return self._rerank_local_qwen(
                query=query,
                documents=documents,
                top_n=top_n,
                instruction=instruction,
            )
        url = self.settings.rerank_url
        if not url:
            raise LLMError("Rerank API endpoint is not configured.")
        payload = {
            "model": self.settings.effective_rerank_model,
            "query": query,
            "documents": documents,
            "return_documents": False,
        }
        if top_n is not None:
            payload["top_n"] = top_n
        headers = self._request_headers()
        client = get_shared_sync_client("reranker")
        resp = client.post(
            url,
            headers=headers,
            json=payload,
            timeout=self.settings.llm_timeout_seconds,
        )
        if resp.status_code >= 400:
            raise LLMError(f"Rerank API HTTP {resp.status_code}: {resp.text[:1000]}")
        return self._extract_ranked_indexes(resp.json())

    async def arerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        *,
        instruction: str | None = None,
        ensure_local_runtime: bool = True,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        if not query.strip():
            raise LLMError("Rerank query cannot be empty.")
        if ensure_local_runtime and self.settings.rerank_uses_local_runtime:
            try:
                async with self.runtime_recycler.async_lease("qwen_reranker"):
                    return await self.arerank(
                        query,
                        documents,
                        top_n,
                        instruction=instruction,
                        ensure_local_runtime=False,
                    )
            except LocalModelRuntimeError as exc:
                raise LLMError(f"Local rerank service is unavailable: {exc}") from exc

        if self.settings.rerank_uses_local_runtime:
            protocol = self.settings.rerank_local_protocol
            url = self._local_qwen_api_url(protocol)
            if not url:
                raise LLMError("Local Qwen rerank endpoint is not configured.")
            payload = self._local_qwen_request_payload(
                query,
                documents,
                top_n=top_n,
                protocol=protocol,
                instruction=instruction,
            )
            headers = {"Content-Type": "application/json"}
            local_qwen = True
        else:
            url = self.settings.rerank_url
            if not url:
                raise LLMError("Rerank API endpoint is not configured.")
            payload = {
                "model": self.settings.effective_rerank_model,
                "query": query,
                "documents": documents,
                "return_documents": False,
            }
            if top_n is not None:
                payload["top_n"] = top_n
            headers = await self.auth.async_headers(self.settings.api_key)
            headers.setdefault("Content-Type", "application/json")
            local_qwen = False

        client = get_shared_async_client(
            "local_inference" if local_qwen else "reranker"
        )
        resp = await client.post(
            url,
            headers=headers,
            json=payload,
            timeout=self.settings.llm_timeout_seconds,
        )
        if resp.status_code >= 400:
            provider_name = "Local Qwen rerank" if local_qwen else "Rerank API"
            raise LLMError(f"{provider_name} HTTP {resp.status_code}: {resp.text[:1000]}")
        if local_qwen:
            return self._extract_local_qwen_ranking(
                resp.json(),
                expected_count=len(documents),
                top_n=top_n,
                protocol=protocol,
            )
        return self._extract_ranked_indexes(resp.json())

    def _rerank_local_qwen(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        *,
        instruction: str | None = None,
    ) -> list[tuple[int, float]]:
        protocol = self.settings.rerank_local_protocol
        url = self._local_qwen_api_url(protocol)
        if not url:
            raise LLMError("Local Qwen rerank endpoint is not configured.")
        try:
            with self.runtime_recycler.lease("qwen_reranker"):
                payload = self._local_qwen_request_payload(
                    query,
                    documents,
                    top_n=top_n,
                    protocol=protocol,
                    instruction=instruction,
                )
                client = get_shared_sync_client("local_inference")
                resp = client.post(
                    url,
                    headers=self._request_headers(),
                    json=payload,
                    timeout=self.settings.llm_timeout_seconds,
                )
                if resp.status_code >= 400:
                    raise LLMError(f"Local Qwen rerank HTTP {resp.status_code}: {resp.text[:1000]}")
                ranked = self._extract_local_qwen_ranking(
                    resp.json(),
                    expected_count=len(documents),
                    top_n=top_n,
                    protocol=protocol,
                )
        except LocalModelRuntimeError as exc:
            raise LLMError(f"Local rerank service is unavailable: {exc}") from exc

        return ranked

    def _local_qwen_request_payload(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None,
        protocol: str,
        instruction: str | None = None,
    ) -> dict[str, Any]:
        if protocol == "completion":
            return self._local_qwen_payload(
                query,
                documents,
                instruction=instruction,
            )

        native_documents = [self._qwen_rerank_document(item) for item in documents]
        resolved_instruction = self._qwen_rerank_instruction(instruction)
        if protocol == "rerank":
            payload: dict[str, Any] = {
                "model": self.settings.effective_rerank_model,
                "query": query.strip(),
                "documents": native_documents,
                "return_documents": False,
                "instruction": resolved_instruction,
            }
            if top_n is not None:
                payload["top_n"] = max(0, int(top_n))
            return payload
        if protocol == "score":
            return {
                "model": self.settings.effective_rerank_model,
                "queries": query.strip(),
                "documents": native_documents,
                "instruction": resolved_instruction,
            }
        raise LLMError(f"Unsupported local rerank protocol: {protocol}")

    def _local_qwen_payload(
        self,
        query: str,
        documents: list[str],
        *,
        instruction: str | None = None,
    ) -> dict[str, Any]:
        model = self.settings.effective_rerank_model
        payload: dict[str, Any] = {
            "model": model,
            "prompt": [
                self._qwen_rerank_prompt(
                    query=query,
                    document=document,
                    instruction=instruction,
                )
                for document in documents
            ],
            "max_tokens": 1,
            "temperature": 0,
            "logprobs": 20,
            "echo": False,
        }
        allowed_token_ids = _QWEN_RERANK_ALLOWED_TOKEN_IDS_BY_MODEL.get(model)
        if allowed_token_ids:
            payload["allowed_token_ids"] = list(allowed_token_ids)
        return payload

    def _local_qwen_completion_url(self) -> str:
        return self._local_qwen_api_url("completion")

    def _local_qwen_api_url(self, protocol: str) -> str:
        target_path = _LOCAL_RERANK_TARGET_PATH.get(protocol)
        if target_path is None:
            raise LLMError(f"Unsupported local rerank protocol: {protocol}")
        raw_url = self.settings.rerank_url.strip()
        if not raw_url:
            return ""
        parsed = urlsplit(raw_url)
        path = parsed.path.rstrip("/")
        for known_path in _LOCAL_RERANK_PATHS:
            if path.endswith(known_path):
                prefix = path[: -len(known_path)].rstrip("/")
                path = f"{prefix}{target_path}"
                break
        else:
            if path in {"", "/"}:
                path = target_path
            else:
                path = parsed.path
        return urlunsplit(
            (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
        )

    def _request_headers(self) -> dict[str, str]:
        if self.settings.rerank_uses_local_runtime:
            return {"Content-Type": "application/json"}
        headers = self.auth.sync_headers(self.settings.api_key)
        headers.setdefault("Content-Type", "application/json")
        return headers

    @staticmethod
    def _qwen_rerank_prompt(
        query: str,
        document: str,
        *,
        instruction: str | None = None,
    ) -> str:
        document_text = RerankService._qwen_rerank_document(document)
        resolved_instruction = RerankService._qwen_rerank_instruction(
            instruction
        )
        pair = f"<Instruct>: {resolved_instruction}\n<Query>: {query.strip()}\n<Document>: {document_text}"
        return f"{_QWEN_RERANK_PREFIX}{pair}{_QWEN_RERANK_SUFFIX}"

    @staticmethod
    def _qwen_rerank_instruction(instruction: str | None) -> str:
        resolved = str(instruction or "").strip()
        return resolved or _QWEN_RERANK_INSTRUCTION

    @staticmethod
    def _qwen_rerank_document(document: str) -> str:
        document_text = str(document or "").strip()
        if len(document_text) > _QWEN_RERANK_MAX_DOCUMENT_CHARS:
            return document_text[:_QWEN_RERANK_MAX_DOCUMENT_CHARS]
        return document_text

    @staticmethod
    def _extract_ranked_indexes(data: dict[str, Any]) -> list[tuple[int, float]]:
        results = data.get("results")
        nested_data = data.get("data")
        if results is None and isinstance(nested_data, dict):
            results = nested_data.get("results")
        if results is None and isinstance(nested_data, list):
            results = nested_data
        if not isinstance(results, list):
            raise LLMError(f"Unable to extract rerank results from response keys: {list(data.keys())}")
        ranked: list[tuple[int, float]] = []
        for fallback_index, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            raw_score = item.get("relevance_score")
            if raw_score is None:
                raw_score = item.get("score")
            if raw_score is None:
                raise LLMError("Rerank response item does not contain a relevance score.")
            ranked.append((int(item.get("index", fallback_index)), float(raw_score)))
        return ranked

    @classmethod
    def _extract_local_qwen_ranking(
        cls,
        data: dict[str, Any],
        *,
        expected_count: int,
        top_n: int | None,
        protocol: str,
    ) -> list[tuple[int, float]]:
        if protocol == "completion":
            scores = cls._extract_qwen_completion_scores(
                data,
                expected_count=expected_count,
            )
            ranked = list(enumerate(scores))
        elif protocol in {"rerank", "score"}:
            ranked = cls._extract_ranked_indexes(data)
        else:
            raise LLMError(f"Unsupported local rerank protocol: {protocol}")
        ranked.sort(key=lambda item: item[1], reverse=True)
        if top_n is not None:
            ranked = ranked[: max(0, int(top_n))]
        return [(int(index), float(score)) for index, score in ranked]

    @classmethod
    def _extract_qwen_completion_scores(cls, data: dict[str, Any], expected_count: int) -> list[float]:
        choices = data.get("choices")
        if not isinstance(choices, list):
            raise LLMError(f"Unable to extract local Qwen rerank choices from response keys: {list(data.keys())}")
        scores = [0.0] * expected_count
        for fallback_index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            index = int(choice.get("index", fallback_index))
            if 0 <= index < expected_count:
                scores[index] = cls._qwen_choice_score(choice)
        return scores

    @classmethod
    def _qwen_choice_score(cls, choice: dict[str, Any]) -> float:
        text = str(choice.get("text") or "").strip().lower()
        logprobs = choice.get("logprobs") if isinstance(choice.get("logprobs"), dict) else {}
        top_logprobs = logprobs.get("top_logprobs") if isinstance(logprobs, dict) else None
        if isinstance(top_logprobs, list) and top_logprobs:
            first_step = top_logprobs[0]
            if isinstance(first_step, dict):
                yes_logprob = cls._token_logprob(first_step, {"yes"})
                no_logprob = cls._token_logprob(first_step, {"no"})
                if yes_logprob is not None or no_logprob is not None:
                    return cls._yes_probability(yes_logprob if yes_logprob is not None else -20.0, no_logprob if no_logprob is not None else -20.0)
        if text.startswith("yes"):
            return 1.0
        if text.startswith("no"):
            return 0.0
        return 0.0

    @staticmethod
    def _token_logprob(step_logprobs: dict[str, Any], normalized_tokens: set[str]) -> float | None:
        for token, value in step_logprobs.items():
            if str(token).strip().lower() not in normalized_tokens:
                continue
            if isinstance(value, dict):
                raw = value.get("logprob")
            else:
                raw = value
            if raw is None:
                return None
            return float(raw)
        return None

    @staticmethod
    def _yes_probability(yes_logprob: float, no_logprob: float) -> float:
        maximum = max(yes_logprob, no_logprob)
        yes_exp = math.exp(yes_logprob - maximum)
        no_exp = math.exp(no_logprob - maximum)
        denominator = yes_exp + no_exp
        if denominator <= 0:
            return 0.0
        return yes_exp / denominator

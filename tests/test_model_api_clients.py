import json
import math
import threading
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from backend.services import embedding_service as embedding_module
from backend.services import langchain_llm_service as llm_module
from backend.services import rerank_service as rerank_module
from backend.services.embedding_service import EmbeddingService
from backend.services.local_model_runtime import (
    LocalModelRuntimeError,
    LocalModelRuntimeRecycler,
)
from backend.services.local_model_runtime_controller import DockerModelRuntimeController
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.langchain_llm_service import ModelFarmLangChainChatModel
from backend.services.rerank_service import RerankService
from backend.config.settings import Settings
from backend.exceptions.llm_errors import LLMError, StructuredOutputError


def test_model_adapter_extracts_standard_openai_content():
    data = {"choices": [{"message": {"content": "hello"}}], "usage": {"total_tokens": 3}}
    assert ModelFarmLangChainChatModel._extract_content(data) == "hello"
    assert ModelFarmLangChainChatModel._extract_usage(data) == {"total_tokens": 3}


def test_model_adapter_extracts_bosch_messages_content():
    data = {"data": {"messages": [{"role": "assistant", "content": "你好"}]}}
    assert ModelFarmLangChainChatModel._extract_content(data) == "你好"


def test_model_adapter_routes_profile_model():
    settings = Settings(chat_model="", profile_model="profile-test")
    model = ModelFarmLangChainChatModel(
        settings=settings,
        task_name="profile",
        response_format="json_object",
    )
    payload = model._build_payload([HumanMessage(content="hi")])

    assert payload["model"] == "profile-test"
    assert payload["response_format"] in ("json_object", {"type": "json_object"})


@pytest.mark.asyncio
async def test_model_adapter_rotates_endpoint_on_retry(monkeypatch):
    requested_urls: list[str] = []

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.text = "temporary failure" if status_code >= 400 else ""

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class Client:
        async def post(self, url, **kwargs):
            requested_urls.append(url)
            return Response(500 if len(requested_urls) == 1 else 200)

    settings = Settings(
        chat_api_endpoint="https://model-a.example/chat",
        chat_api_endpoints="https://model-b.example/chat",
        llm_max_retries=1,
    )
    model = ModelFarmLangChainChatModel(settings=settings, max_retries=1)

    async def headers(_api_key):
        return {}

    monkeypatch.setattr(model._auth, "async_headers", headers)
    monkeypatch.setattr(llm_module, "get_shared_async_client", lambda: Client())
    monkeypatch.setattr(
        ModelFarmLangChainChatModel,
        "_log_llm_metric",
        lambda *args, **kwargs: None,
    )

    result = await model._agenerate([HumanMessage(content="hello")])

    assert result.generations[0].message.content == "ok"
    assert requested_urls == [
        "https://model-a.example/chat",
        "https://model-b.example/chat",
    ]


@pytest.mark.asyncio
async def test_streaming_adapter_does_not_retry_after_emitting_text(monkeypatch):
    requested_urls: list[str] = []

    class Response:
        status_code = 200

    class Client:
        @asynccontextmanager
        async def stream(self, _method, url, **_kwargs):
            requested_urls.append(url)
            yield Response()

    settings = Settings(
        chat_api_endpoint="https://model-a.example/chat",
        chat_api_endpoints="https://model-b.example/chat",
        llm_max_retries=1,
    )
    model = ModelFarmLangChainChatModel(settings=settings, max_retries=1)

    async def headers(_api_key):
        return {}

    async def stream_deltas(_response):
        yield "partial"
        raise RuntimeError("stream interrupted")

    monkeypatch.setattr(model._auth, "async_headers", headers)
    monkeypatch.setattr(model, "_iter_stream_deltas", stream_deltas)
    monkeypatch.setattr(llm_module, "get_shared_async_client", lambda: Client())
    monkeypatch.setattr(
        ModelFarmLangChainChatModel,
        "_log_llm_metric",
        lambda *args, **kwargs: None,
    )

    parts: list[str] = []
    with pytest.raises(LLMError, match="streaming failed"):
        async for part in model.astream_text([HumanMessage(content="hello")]):
            parts.append(part)

    assert parts == ["partial"]
    assert requested_urls == ["https://model-a.example/chat"]


def test_embedding_extracts_v2_data_shape():
    data = {"model": "m", "data": [{"index": 1, "embedding": [0.2]}, {"index": 0, "embedding": [0.1]}]}
    assert EmbeddingService._extract_embeddings(data) == [[0.1], [0.2]]


def test_local_qwen_embedding_applies_configured_mrl_dimensions() -> None:
    service = EmbeddingService.__new__(EmbeddingService)
    service.settings = SimpleNamespace(
        embedding_dimensions=32,
        embedding_uses_local_runtime=True,
        effective_embedding_model="Qwen/Qwen3-Embedding-4B",
    )
    raw = [float(index + 1) for index in range(64)]

    result = service._finalize_embeddings(
        {"data": [{"index": 0, "embedding": raw}]}
    )

    assert len(result[0]) == 32
    assert math.sqrt(sum(value * value for value in result[0])) == pytest.approx(1.0)


def test_embedding_rejects_a_dimension_mismatch() -> None:
    service = EmbeddingService.__new__(EmbeddingService)
    service.settings = SimpleNamespace(
        embedding_dimensions=3,
        embedding_uses_local_runtime=False,
        effective_embedding_model="embedding-model",
    )

    with pytest.raises(LLMError, match="expected=3, actual=2"):
        service._finalize_embeddings(
            {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
        )


def test_rerank_extracts_results():
    data = {"results": [{"index": 2, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.6}]}
    assert RerankService._extract_ranked_indexes(data) == [(2, 0.8), (0, 0.6)]


def test_rerank_extracts_compatible_nested_score_results():
    data = {"data": [{"index": 1, "score": 0.75}, {"index": 0, "score": 0.25}]}
    assert RerankService._extract_ranked_indexes(data) == [(1, 0.75), (0, 0.25)]


@pytest.mark.parametrize("provider", ["bosch", "openai_compatible"])
def test_platform_embedding_uses_api_without_local_runtime(monkeypatch, provider):
    captured: dict[str, object] = {"lease_calls": 0}

    class Auth:
        def sync_headers(self, api_key):
            captured["api_key"] = api_key
            return {"Authorization": "Bearer embedding-key"}

    class Runtime:
        def lease(self, _service_name):
            captured["lease_calls"] = int(captured["lease_calls"]) + 1
            raise AssertionError("Platform embedding must not acquire a local lease.")

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

    class Client:
        def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return Response()

    def client_for(pool_name):
        captured["pool_name"] = pool_name
        return Client()

    service = EmbeddingService.__new__(EmbeddingService)
    service.settings = Settings(
        model_provider_mode="platform",
        embedding_provider=provider,
        embedding_api_endpoint="https://models.example/v1/embeddings",
        api_key="embedding-key",
        embedding_model="embedding-model",
        embedding_dimensions=2,
        llm_max_retries=0,
    )
    service.auth = Auth()
    service.runtime_recycler = Runtime()
    monkeypatch.setattr(embedding_module, "get_shared_sync_client", client_for)

    result = service.embed(["employee development"])

    assert result == [[0.1, 0.2]]
    assert captured["pool_name"] == "embedding"
    assert captured["lease_calls"] == 0
    assert captured["api_key"] == "embedding-key"
    assert captured["url"] == "https://models.example/v1/embeddings"
    assert captured["json"] == {
        "model": "embedding-model",
        "input": ["employee development"],
        "dimensions": 2,
    }
    assert captured["headers"] == {
        "Authorization": "Bearer embedding-key",
        "Content-Type": "application/json",
    }


def test_platform_embedding_splits_oversized_sync_batches(monkeypatch) -> None:
    requests: list[list[str]] = []

    class Auth:
        @staticmethod
        def sync_headers(_api_key):
            return {}

    class Response:
        status_code = 200
        text = ""

        def __init__(self, texts: list[str]):
            self.texts = texts

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [float(text)]}
                    for index, text in enumerate(self.texts)
                ]
            }

    class Client:
        @staticmethod
        def post(_url, **kwargs):
            texts = list(kwargs["json"]["input"])
            requests.append(texts)
            return Response(texts)

    service = EmbeddingService.__new__(EmbeddingService)
    service.settings = Settings(
        model_provider_mode="platform",
        embedding_provider="bosch",
        embedding_api_endpoint="https://models.example/v2/embeddings",
        api_key="embedding-key",
        embedding_model="embedding-model",
        embedding_dimensions=1,
        embedding_api_batch_size=2,
        llm_max_retries=0,
    )
    service.auth = Auth()
    service.runtime_recycler = object()
    monkeypatch.setattr(
        embedding_module,
        "get_shared_sync_client",
        lambda _pool_name: Client(),
    )

    result = service.embed(["0", "1", "2", "3", "4"])

    assert requests == [["0", "1"], ["2", "3"], ["4"]]
    assert result == [[0.0], [1.0], [2.0], [3.0], [4.0]]


def test_bosch_embedding_enforces_provider_batch_limit(monkeypatch) -> None:
    requests: list[list[str]] = []

    class Auth:
        @staticmethod
        def sync_headers(_api_key):
            return {}

    class Response:
        status_code = 200
        text = ""

        def __init__(self, texts: list[str]):
            self.texts = texts

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [float(text)]}
                    for index, text in enumerate(self.texts)
                ]
            }

    class Client:
        @staticmethod
        def post(_url, **kwargs):
            texts = list(kwargs["json"]["input"])
            requests.append(texts)
            return Response(texts)

    service = EmbeddingService.__new__(EmbeddingService)
    service.settings = Settings(
        model_provider_mode="platform",
        embedding_provider="bosch",
        embedding_api_endpoint="https://models.example/v2/embeddings",
        api_key="embedding-key",
        embedding_model="embedding-model",
        embedding_dimensions=1,
        embedding_api_batch_size=46,
        llm_max_retries=0,
    )
    service.auth = Auth()
    service.runtime_recycler = object()
    monkeypatch.setattr(
        embedding_module,
        "get_shared_sync_client",
        lambda _pool_name: Client(),
    )

    inputs = [str(index) for index in range(46)]
    result = service.embed(inputs)

    assert [len(batch) for batch in requests] == [10, 10, 10, 10, 6]
    assert result == [[float(index)] for index in range(46)]


@pytest.mark.parametrize("provider", ["bosch", "openai_compatible"])
def test_platform_reranker_uses_api_without_local_runtime(monkeypatch, provider):
    captured: dict[str, object] = {"lease_calls": 0}

    class Auth:
        def sync_headers(self, api_key):
            captured["api_key"] = api_key
            return {"Authorization": "Bearer rerank-key"}

    class Runtime:
        def lease(self, _service_name):
            captured["lease_calls"] = int(captured["lease_calls"]) + 1
            raise AssertionError("Platform reranker must not acquire a local lease.")

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"results": [{"index": 1, "relevance_score": 0.9}]}

    class Client:
        def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return Response()

    def client_for(pool_name):
        captured["pool_name"] = pool_name
        return Client()

    service = RerankService.__new__(RerankService)
    service.settings = Settings(
        model_provider_mode="platform",
        rerank_provider=provider,
        rerank_api_endpoint="https://models.example/v1/rerank",
        api_key="rerank-key",
        rerank_model="rerank-model",
    )
    service.auth = Auth()
    service.runtime_recycler = Runtime()
    monkeypatch.setattr(rerank_module, "get_shared_sync_client", client_for)

    result = service.rerank("query", ["first", "second"], top_n=1)

    assert result == [(1, 0.9)]
    assert captured["pool_name"] == "reranker"
    assert captured["lease_calls"] == 0
    assert captured["api_key"] == "rerank-key"
    assert captured["url"] == "https://models.example/v1/rerank"
    assert captured["json"] == {
        "model": "rerank-model",
        "query": "query",
        "documents": ["first", "second"],
        "return_documents": False,
        "top_n": 1,
    }
    assert captured["headers"] == {
        "Authorization": "Bearer rerank-key",
        "Content-Type": "application/json",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["bosch", "openai_compatible"])
async def test_platform_embedding_async_uses_api_without_local_runtime(
    monkeypatch,
    provider,
):
    captured: dict[str, object] = {"lease_calls": 0}

    class Auth:
        async def async_headers(self, api_key):
            captured["api_key"] = api_key
            return {"Authorization": "Bearer embedding-key"}

    class Runtime:
        def async_lease(self, _service_name):
            captured["lease_calls"] = int(captured["lease_calls"]) + 1
            raise AssertionError("Platform embedding must not acquire a local lease.")

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"data": {"embeddings": [{"index": 0, "embedding": [0.3, 0.4]}]}}

    class Client:
        async def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return Response()

    def client_for(pool_name):
        captured["pool_name"] = pool_name
        return Client()

    service = EmbeddingService.__new__(EmbeddingService)
    service.settings = Settings(
        model_provider_mode="platform",
        embedding_provider=provider,
        embedding_api_endpoint="https://models.example/v1/embeddings",
        api_key="embedding-key",
        embedding_model="embedding-model",
        embedding_dimensions=2,
        llm_max_retries=0,
    )
    service.auth = Auth()
    service.runtime_recycler = Runtime()
    monkeypatch.setattr(embedding_module, "get_shared_async_client", client_for)

    result = await service.aembed(["employee development"])

    assert result == [[0.3, 0.4]]
    assert captured["pool_name"] == "embedding"
    assert captured["lease_calls"] == 0
    assert captured["json"] == {
        "model": "embedding-model",
        "input": ["employee development"],
        "dimensions": 2,
    }


@pytest.mark.asyncio
async def test_platform_embedding_splits_oversized_async_batches(monkeypatch) -> None:
    requests: list[list[str]] = []

    class Auth:
        @staticmethod
        async def async_headers(_api_key):
            return {}

    class Response:
        status_code = 200
        text = ""

        def __init__(self, texts: list[str]):
            self.texts = texts

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [float(text)]}
                    for index, text in enumerate(self.texts)
                ]
            }

    class Client:
        @staticmethod
        async def post(_url, **kwargs):
            texts = list(kwargs["json"]["input"])
            requests.append(texts)
            return Response(texts)

    service = EmbeddingService.__new__(EmbeddingService)
    service.settings = Settings(
        model_provider_mode="platform",
        embedding_provider="bosch",
        embedding_api_endpoint="https://models.example/v2/embeddings",
        api_key="embedding-key",
        embedding_model="embedding-model",
        embedding_dimensions=1,
        embedding_api_batch_size=2,
        llm_max_retries=0,
    )
    service.auth = Auth()
    service.runtime_recycler = object()
    monkeypatch.setattr(
        embedding_module,
        "get_shared_async_client",
        lambda _pool_name: Client(),
    )

    result = await service.aembed(["0", "1", "2", "3", "4"])

    assert requests == [["0", "1"], ["2", "3"], ["4"]]
    assert result == [[0.0], [1.0], [2.0], [3.0], [4.0]]


@pytest.mark.asyncio
async def test_bosch_embedding_enforces_provider_batch_limit_async(monkeypatch) -> None:
    requests: list[list[str]] = []

    class Auth:
        @staticmethod
        async def async_headers(_api_key):
            return {}

    class Response:
        status_code = 200
        text = ""

        def __init__(self, texts: list[str]):
            self.texts = texts

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [float(text)]}
                    for index, text in enumerate(self.texts)
                ]
            }

    class Client:
        @staticmethod
        async def post(_url, **kwargs):
            texts = list(kwargs["json"]["input"])
            requests.append(texts)
            return Response(texts)

    service = EmbeddingService.__new__(EmbeddingService)
    service.settings = Settings(
        model_provider_mode="platform",
        embedding_provider="bosch",
        embedding_api_endpoint="https://models.example/v2/embeddings",
        api_key="embedding-key",
        embedding_model="embedding-model",
        embedding_dimensions=1,
        embedding_api_batch_size=46,
        llm_max_retries=0,
    )
    service.auth = Auth()
    service.runtime_recycler = object()
    monkeypatch.setattr(
        embedding_module,
        "get_shared_async_client",
        lambda _pool_name: Client(),
    )

    inputs = [str(index) for index in range(46)]
    result = await service.aembed(inputs)

    assert [len(batch) for batch in requests] == [10, 10, 10, 10, 6]
    assert result == [[float(index)] for index in range(46)]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["bosch", "openai_compatible"])
async def test_platform_reranker_async_uses_api_without_local_runtime(
    monkeypatch,
    provider,
):
    captured: dict[str, object] = {"lease_calls": 0}

    class Auth:
        async def async_headers(self, api_key):
            captured["api_key"] = api_key
            return {"Authorization": "Bearer rerank-key"}

    class Runtime:
        def async_lease(self, _service_name):
            captured["lease_calls"] = int(captured["lease_calls"]) + 1
            raise AssertionError("Platform reranker must not acquire a local lease.")

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"data": {"results": [{"index": 0, "score": 0.8}]}}

    class Client:
        async def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return Response()

    def client_for(pool_name):
        captured["pool_name"] = pool_name
        return Client()

    service = RerankService.__new__(RerankService)
    service.settings = Settings(
        model_provider_mode="platform",
        rerank_provider=provider,
        rerank_api_endpoint="https://models.example/v1/rerank",
        api_key="rerank-key",
        rerank_model="rerank-model",
    )
    service.auth = Auth()
    service.runtime_recycler = Runtime()
    monkeypatch.setattr(rerank_module, "get_shared_async_client", client_for)

    result = await service.arerank("query", ["first", "second"], top_n=1)

    assert result == [(0, 0.8)]
    assert captured["pool_name"] == "reranker"
    assert captured["lease_calls"] == 0
    assert captured["json"] == {
        "model": "rerank-model",
        "query": "query",
        "documents": ["first", "second"],
        "return_documents": False,
        "top_n": 1,
    }


def test_platform_api_failures_do_not_fall_back_to_local_runtime(monkeypatch):
    class Auth:
        @staticmethod
        def sync_headers(_api_key):
            return {"Authorization": "Bearer key"}

    class Runtime:
        def lease(self, _service_name):
            raise AssertionError("Platform API failure must not acquire a local lease.")

    class Response:
        status_code = 503
        text = "unavailable"

    class Client:
        @staticmethod
        def post(_url, **_kwargs):
            return Response()

    monkeypatch.setattr(embedding_module, "get_shared_sync_client", lambda _name: Client())
    monkeypatch.setattr(rerank_module, "get_shared_sync_client", lambda _name: Client())

    embedding = EmbeddingService.__new__(EmbeddingService)
    embedding.settings = Settings(
        model_provider_mode="platform",
        embedding_provider="openai_compatible",
        embedding_api_endpoint="https://models.example/v1/embeddings",
        api_key="key",
        llm_max_retries=0,
    )
    embedding.auth = Auth()
    embedding.runtime_recycler = Runtime()

    reranker = RerankService.__new__(RerankService)
    reranker.settings = Settings(
        model_provider_mode="platform",
        rerank_provider="bosch",
        rerank_api_endpoint="https://models.example/v1/rerank",
        api_key="key",
    )
    reranker.auth = Auth()
    reranker.runtime_recycler = Runtime()

    with pytest.raises(LLMError, match="Embedding API HTTP 503"):
        embedding.embed(["query"])
    with pytest.raises(LLMError, match="Rerank API HTTP 503"):
        reranker.rerank("query", ["document"])


def test_local_qwen_embedding_and_rerank_do_not_require_api_key():
    embedding = EmbeddingService.__new__(EmbeddingService)
    embedding.settings = Settings(model_provider_mode="local", api_key="")
    embedding.auth = None
    assert embedding._request_headers() == {"Content-Type": "application/json"}

    rerank = RerankService.__new__(RerankService)
    rerank.settings = Settings(model_provider_mode="local", api_key="")
    rerank.auth = None
    assert rerank._request_headers() == {"Content-Type": "application/json"}


def test_platform_providers_disable_local_runtime_services():
    recycler = LocalModelRuntimeRecycler(
        Settings(
            model_provider_mode="platform",
            asr_provider_mode="local",
            embedding_provider="bosch",
            rerank_provider="openai_compatible",
            local_model_pinned_services="qwen_embedding,qwen_reranker,qwen3_asr",
        )
    )

    assert recycler.configured_services == frozenset({"qwen3_asr"})
    assert recycler.pinned_services == frozenset({"qwen3_asr"})
    with pytest.raises(LocalModelRuntimeError, match="disabled by provider"):
        with recycler.lease("qwen_embedding"):
            pass
    with pytest.raises(LocalModelRuntimeError, match="disabled by provider"):
        recycler.ensure_resident("qwen_reranker")


def test_runtime_start_stops_only_running_services_disabled_by_configuration(
    monkeypatch,
):
    recycler = LocalModelRuntimeRecycler(
        Settings(
            model_provider_mode="platform",
            asr_provider_mode="browser",
            tts_enabled=False,
            local_model_auto_recycle_enabled=True,
            local_model_controller_url="http://model-controller:7120",
        )
    )
    states = {
        "fish_tts": {"exists": True, "running": True},
        "qwen3_asr": {"exists": True, "running": True},
        "qwen_embedding": {"exists": True, "running": True},
        "qwen_reranker": {"exists": True, "running": False},
    }
    stopped: list[str] = []
    monkeypatch.setattr(recycler, "_controller_state", states.__getitem__)
    monkeypatch.setattr(
        recycler,
        "_stop_service",
        lambda service_name: stopped.append(service_name) or True,
    )
    monkeypatch.setattr(recycler, "_ensure_monitor_started", lambda: None)

    recycler.start()

    assert stopped == ["fish_tts", "qwen3_asr", "qwen_embedding"]


def test_runtime_start_preserves_enabled_fish_service(monkeypatch):
    recycler = LocalModelRuntimeRecycler(
        Settings(
            model_provider_mode="platform",
            asr_provider_mode="browser",
            tts_enabled=True,
            local_model_auto_recycle_enabled=True,
            local_model_controller_url="http://model-controller:7120",
        )
    )
    inspected: list[str] = []
    monkeypatch.setattr(
        recycler,
        "_controller_state",
        lambda service_name: inspected.append(service_name)
        or {"exists": False, "running": False},
    )
    monkeypatch.setattr(recycler, "_ensure_monitor_started", lambda: None)

    recycler.start()

    assert "fish_tts" not in inspected


def test_local_qwen_completion_scores_from_logprobs():
    data = {
        "choices": [
            {"index": 1, "text": " no", "logprobs": {"top_logprobs": [{"yes": -5.0, "no": -0.1}]}},
            {"index": 0, "text": " yes", "logprobs": {"top_logprobs": [{"yes": -0.2, "no": -4.0}]}},
        ]
    }

    scores = RerankService._extract_qwen_completion_scores(data, expected_count=2)

    assert scores[0] > scores[1]
    assert scores[0] > 0.9
    assert scores[1] < 0.1


def test_local_qwen_payload_matches_official_vllm_contract():
    service = RerankService.__new__(RerankService)
    service.settings = Settings(model_provider_mode="local")

    payload = service._local_qwen_payload(
        "performance evidence",
        ["objective delivery result", "unrelated cafeteria notice"],
        instruction="Rank explicit performance evidence.",
    )

    assert payload["model"] == "Qwen/Qwen3-Reranker-4B"
    assert payload["max_tokens"] == 1
    assert payload["temperature"] == 0
    assert payload["logprobs"] == 20
    assert payload["allowed_token_ids"] == [9693, 2152]
    assert payload["echo"] is False
    assert len(payload["prompt"]) == 2
    assert all(
        "<Instruct>: Rank explicit performance evidence." in prompt
        for prompt in payload["prompt"]
    )


def test_local_qwen_prompt_keeps_a_shared_prefix_for_prefix_caching():
    first = RerankService._qwen_rerank_prompt("same query", "first document")
    second = RerankService._qwen_rerank_prompt("same query", "second document")

    first_prefix = first.split("<Document>:", 1)[0]
    second_prefix = second.split("<Document>:", 1)[0]

    assert first_prefix == second_prefix
    assert first.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    assert second.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")


def test_local_qwen_prompt_uses_default_instruction_for_blank_override():
    prompt = RerankService._qwen_rerank_prompt(
        "query",
        "document",
        instruction="   ",
    )

    assert "<Instruct>: Given an HR performance feedback query" in prompt


@pytest.mark.parametrize(
    ("endpoint", "protocol", "expected"),
    [
        (
            "http://reranker:8000/v1/completions",
            "completion",
            "http://reranker:8000/v1/completions",
        ),
        (
            "http://reranker:8000/v1/completions",
            "rerank",
            "http://reranker:8000/v1/rerank",
        ),
        (
            "http://reranker:8000/v1/rerank/",
            "score",
            "http://reranker:8000/score",
        ),
        (
            "http://reranker:8000/score",
            "completion",
            "http://reranker:8000/v1/completions",
        ),
    ],
)
def test_local_qwen_protocol_normalizes_known_endpoint_paths(
    endpoint,
    protocol,
    expected,
):
    service = RerankService.__new__(RerankService)
    service.settings = Settings(
        model_provider_mode="local",
        rerank_local_api_endpoint=endpoint,
        rerank_local_protocol=protocol,
    )

    assert service._local_qwen_api_url(protocol) == expected


def test_local_qwen_native_payloads_preserve_instruction_and_length_boundary():
    service = RerankService.__new__(RerankService)
    service.settings = Settings(model_provider_mode="local")
    documents = [" first ", "x" * 7100]

    rerank_payload = service._local_qwen_request_payload(
        " query ",
        documents,
        top_n=1,
        protocol="rerank",
        instruction="Rank career-development evidence.",
    )
    score_payload = service._local_qwen_request_payload(
        " query ",
        documents,
        top_n=1,
        protocol="score",
        instruction="Rank career-development evidence.",
    )

    assert rerank_payload["query"] == "query"
    assert rerank_payload["documents"][0] == "first"
    assert len(rerank_payload["documents"][1]) == 7000
    assert rerank_payload["instruction"] == "Rank career-development evidence."
    assert rerank_payload["top_n"] == 1
    assert score_payload["queries"] == "query"
    assert score_payload["documents"] == rerank_payload["documents"]
    assert score_payload["instruction"] == rerank_payload["instruction"]


def test_local_qwen_native_rerank_uses_vllm_contract(monkeypatch):
    captured = {}

    class Runtime:
        @contextmanager
        def lease(self, service_name):
            captured["lease"] = service_name
            yield

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            }

    class Client:
        def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return Response()

    service = RerankService.__new__(RerankService)
    service.settings = Settings(
        model_provider_mode="local",
        rerank_local_protocol="rerank",
        rerank_local_api_endpoint="http://shadow:8000/v1/completions",
    )
    service.runtime_recycler = Runtime()
    monkeypatch.setattr(
        rerank_module,
        "get_shared_sync_client",
        lambda _pool_name: Client(),
    )

    assert service.rerank(
        "query",
        ["first", "second"],
        top_n=1,
        instruction="Rank task-specific evidence.",
    ) == [(1, 0.9)]
    assert captured["lease"] == "qwen_reranker"
    assert captured["url"] == "http://shadow:8000/v1/rerank"
    assert captured["json"]["query"] == "query"
    assert captured["json"]["documents"] == ["first", "second"]
    assert captured["json"]["top_n"] == 1
    assert captured["json"]["instruction"] == "Rank task-specific evidence."


@pytest.mark.asyncio
async def test_local_qwen_native_rerank_async_uses_vllm_contract(monkeypatch):
    captured = {}

    class Runtime:
        @asynccontextmanager
        async def async_lease(self, service_name):
            captured["lease"] = service_name
            yield

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            }

    class Client:
        async def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return Response()

    service = RerankService.__new__(RerankService)
    service.settings = Settings(
        model_provider_mode="local",
        rerank_local_protocol="rerank",
        rerank_local_api_endpoint="http://qwen-reranker:8000/v1/completions",
    )
    service.runtime_recycler = Runtime()
    monkeypatch.setattr(
        rerank_module,
        "get_shared_async_client",
        lambda pool_name: captured.update({"pool_name": pool_name}) or Client(),
    )

    result = await service.arerank(
        "query",
        ["first", "second"],
        top_n=1,
        instruction="Rank task-specific evidence.",
    )

    assert result == [(1, 0.9)]
    assert captured["lease"] == "qwen_reranker"
    assert captured["pool_name"] == "local_inference"
    assert captured["url"] == "http://qwen-reranker:8000/v1/rerank"
    assert captured["json"]["query"] == "query"
    assert captured["json"]["documents"] == ["first", "second"]
    assert captured["json"]["top_n"] == 1
    assert captured["json"]["instruction"] == "Rank task-specific evidence."


def test_local_qwen_native_score_accepts_data_without_indexes():
    assert RerankService._extract_local_qwen_ranking(
        {"data": [{"score": 0.2}, {"score": 0.9}]},
        expected_count=2,
        top_n=1,
        protocol="score",
    ) == [(1, 0.9)]


def test_local_model_runtime_recycler_builds_compose_stop_command():
    settings = Settings(
        local_model_auto_recycle_enabled=True,
        local_model_docker_command="docker",
        local_model_docker_compose_file="docker-compose.yml",
        local_model_docker_project_dir="/tmp/hr-agent-test",
        local_model_stop_timeout_seconds=12,
    )
    recycler = LocalModelRuntimeRecycler(settings)

    command = recycler._build_stop_command("qwen_embedding")

    assert command is not None
    assert command.argv == ["docker", "compose", "-f", "docker-compose.yml", "stop", "qwen_embedding"]
    assert command.cwd == "/tmp/hr-agent-test"
    assert command.timeout_seconds == 12


def test_local_model_runtime_recycler_supports_qwen3_asr():
    recycler = LocalModelRuntimeRecycler(
        Settings(
            local_model_auto_recycle_enabled=True,
            local_model_docker_command="docker",
            local_model_docker_compose_file="docker-compose.yml",
            local_model_docker_project_dir="/tmp/hr-agent-test",
        )
    )

    command = recycler._build_stop_command("qwen3_asr")

    assert command is not None
    assert command.argv[-2:] == ["stop", "qwen3_asr"]


def test_local_model_runtime_lease_prevents_idle_stop(monkeypatch):
    recycler = LocalModelRuntimeRecycler(
        Settings(
            model_provider_mode="local",
            local_model_auto_recycle_enabled=True,
            local_model_auto_start_enabled=True,
            local_model_pinned_services="qwen3_asr",
        )
    )
    starts: list[str] = []
    stops: list[str] = []
    monkeypatch.setattr(recycler, "_ensure_monitor_started", lambda: None)
    monkeypatch.setattr(recycler, "_ensure_service_ready", starts.append)
    monkeypatch.setattr(recycler, "_stop_service", lambda service: stops.append(service) or True)

    with recycler.lease("qwen_embedding"):
        recycler._last_used_at["qwen_embedding"] = 0
        recycler._stop_if_idle("qwen_embedding")
        assert recycler._active_uses["qwen_embedding"] == 1
        assert stops == []

    recycler._last_used_at["qwen_embedding"] = 0
    recycler._stop_if_idle("qwen_embedding")

    assert starts == ["qwen_embedding"]
    assert stops == ["qwen_embedding"]
    assert recycler._active_uses["qwen_embedding"] == 0


def test_overlapping_local_model_leases_share_one_readiness_check(monkeypatch):
    recycler = LocalModelRuntimeRecycler(
        Settings(
            model_provider_mode="local",
            local_model_auto_recycle_enabled=True,
            local_model_auto_start_enabled=True,
        )
    )
    starts: list[str] = []
    first_entered = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    monkeypatch.setattr(recycler, "_ensure_monitor_started", lambda: None)
    monkeypatch.setattr(recycler, "_ensure_service_ready", starts.append)

    def first_worker() -> None:
        with recycler.lease("qwen_reranker"):
            first_entered.set()
            release_first.wait(timeout=1)

    def second_worker() -> None:
        with recycler.lease("qwen_reranker"):
            pass
        second_finished.set()

    first = threading.Thread(target=first_worker)
    second = threading.Thread(target=second_worker)
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    assert second_finished.wait(timeout=1)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert starts == ["qwen_reranker"]
    assert recycler._active_uses["qwen_reranker"] == 0


def test_local_model_runtime_starts_stopped_service_and_waits_for_health(monkeypatch):
    recycler = LocalModelRuntimeRecycler(
        Settings(
            local_model_auto_recycle_enabled=True,
            asr_provider_mode="local",
            local_model_auto_start_enabled=True,
            local_model_controller_url="http://model-controller:7120",
            local_model_start_timeout_seconds=1,
            local_model_health_poll_interval_seconds=0.001,
        )
    )
    states = iter(
        [
            {"exists": True, "running": False, "status": "exited", "health": None},
            {"exists": True, "running": True, "status": "running", "health": "starting"},
            {"exists": True, "running": True, "status": "running", "health": "healthy"},
        ]
    )
    requests: list[tuple[str, str]] = []
    monkeypatch.setattr(recycler, "_controller_state", lambda _service: next(states))
    monkeypatch.setattr(
        recycler,
        "_controller_request",
        lambda method, path: requests.append((method, path)),
    )

    recycler._ensure_service_ready("qwen3_asr")

    assert requests == [("POST", "/services/qwen3_asr/start")]


def test_docker_runtime_controller_filters_by_project_and_service(monkeypatch):
    controller = DockerModelRuntimeController(
        project_name="06",
        socket_path="/var/run/docker.sock",
    )
    captured: dict = {}

    class FakeResponse:
        text = ""

        @staticmethod
        def json():
            return []

    def fake_request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(controller, "_docker_request", fake_request)

    state = controller.state("qwen3_asr")
    filters = json.loads(captured["params"]["filters"])

    assert state["exists"] is False
    assert filters == {
        "label": [
            "com.docker.compose.project=06",
            "com.docker.compose.service=qwen3_asr",
            "com.docker.compose.oneoff=False",
        ]
    }
    with pytest.raises(ValueError, match="Unsupported local model service"):
        controller.state("postgres")


def test_docker_runtime_controller_never_stops_newer_oneoff_container(monkeypatch):
    controller = DockerModelRuntimeController(
        project_name="06",
        socket_path="/var/run/docker.sock",
    )
    containers = [
        {
            "Id": "managed-container",
            "Created": 10,
            "Labels": {
                "com.docker.compose.project": "06",
                "com.docker.compose.service": "qwen3_asr",
                "com.docker.compose.oneoff": "False",
            },
        },
        {
            "Id": "oneoff-container",
            "Created": 20,
            "Labels": {
                "com.docker.compose.project": "06",
                "com.docker.compose.service": "qwen3_asr",
                "com.docker.compose.oneoff": "True",
            },
        },
    ]
    requests: list[tuple[str, str]] = []

    class FakeResponse:
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_request(method, path, **kwargs):
        requests.append((method, path))
        if path == "/containers/json":
            filters = json.loads(kwargs["params"]["filters"])
            assert "com.docker.compose.oneoff=False" in filters["label"]
            return FakeResponse(containers)
        if path == "/containers/managed-container/json":
            return FakeResponse(
                {
                    "State": {
                        "Running": True,
                        "Status": "running",
                        "Health": {"Status": "healthy"},
                    }
                }
            )
        if path == "/containers/managed-container/stop":
            return FakeResponse({})
        raise AssertionError(f"Unexpected Docker request: {method} {path}")

    monkeypatch.setattr(controller, "_docker_request", fake_request)

    controller.stop("qwen3_asr")

    assert ("POST", "/containers/managed-container/stop") in requests
    assert all("oneoff-container" not in path for _, path in requests)


def test_local_model_recycler_never_stops_newer_oneoff_container(monkeypatch):
    recycler = LocalModelRuntimeRecycler(
        Settings(
            local_model_auto_recycle_enabled=True,
            local_model_controller_url="",
            local_model_docker_project_name="06",
        )
    )
    containers = [
        {
            "Id": "managed-container",
            "Created": 10,
            "Labels": {
                "com.docker.compose.project": "06",
                "com.docker.compose.service": "qwen_embedding",
                "com.docker.compose.oneoff": "False",
            },
        },
        {
            "Id": "oneoff-container",
            "Created": 20,
            "Labels": {
                "com.docker.compose.project": "06",
                "com.docker.compose.service": "qwen_embedding",
                "com.docker.compose.oneoff": "True",
            },
        },
    ]
    requests: list[tuple[str, str]] = []

    class FakeResponse:
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_request(method, path, **kwargs):
        requests.append((method, path))
        if path == "/containers/json":
            filters = json.loads(kwargs["params"]["filters"])
            assert "com.docker.compose.oneoff=False" in filters["label"]
            return FakeResponse(containers)
        if path == "/containers/managed-container/json":
            return FakeResponse(
                {
                    "State": {
                        "Running": True,
                        "Status": "running",
                        "Health": {"Status": "healthy"},
                    }
                }
            )
        if path == "/containers/managed-container/stop":
            return FakeResponse({})
        raise AssertionError(f"Unexpected Docker request: {method} {path}")

    monkeypatch.setattr(recycler, "_docker_socket_exists", lambda: True)
    monkeypatch.setattr(recycler, "_docker_request", fake_request)

    assert recycler._stop_service("qwen_embedding") is True
    assert ("POST", "/containers/managed-container/stop") in requests
    assert all("oneoff-container" not in path for _, path in requests)


def test_docker_runtime_controller_allows_fish_tts(monkeypatch):
    controller = DockerModelRuntimeController(
        project_name="06",
        socket_path="/var/run/docker.sock",
    )
    captured: dict = {}

    class FakeResponse:
        @staticmethod
        def json():
            return []

    def fake_request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(controller, "_docker_request", fake_request)

    state = controller.state("fish_tts")
    filters = json.loads(captured["params"]["filters"])

    assert state["exists"] is False
    assert "com.docker.compose.service=fish_tts" in filters["label"]
    assert "com.docker.compose.oneoff=False" in filters["label"]


def test_bosch_openai_compatible_auto_response_format_uses_openai_payload():
    model = ModelFarmLangChainChatModel(
        settings=Settings(
            llm_provider="bosch_openai_compatible",
            llm_response_format_style="auto",
        )
    )
    assert model._format_response_format("json_schema") == {"type": "json_schema"}


@pytest.mark.parametrize("tool_choice", ["auto", "none", "required"])
def test_bosch_openai_compatible_tool_choice_uses_openai_payload(tool_choice):
    model = ModelFarmLangChainChatModel(
        settings=Settings(
            llm_provider="bosch_openai_compatible",
            llm_response_format_style="auto",
        )
    ).bind_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "structured_output",
                    "description": "Return structured output",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice=tool_choice,
    )

    payload = model._build_payload([HumanMessage(content="hi")])

    assert payload["tool_choice"] == tool_choice
    assert "toolChoice" not in payload


def test_bosch_openai_compatible_explicit_tool_name_falls_back_to_required():
    tool_choice = "ExampleStructuredResponse"
    model = ModelFarmLangChainChatModel(
        settings=Settings(
            llm_provider="bosch_openai_compatible",
            llm_response_format_style="auto",
        )
    ).bind_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "ExampleStructuredResponse",
                    "description": "Return structured output",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice=tool_choice,
    )

    payload = model._build_payload([HumanMessage(content="hi")])

    assert payload["tool_choice"] == "required"
    assert "toolChoice" not in payload


@pytest.mark.parametrize(
    "provider",
    ["openai_compatible", "bosch_openai_compatible"],
)
def test_model_adapter_serializes_tool_call_and_tool_message_round_trip(provider):
    model = ModelFarmLangChainChatModel(
        settings=Settings(llm_provider=provider)
    ).bind_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "search_hr_knowledge_base",
                    "description": "Search HR knowledge",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="auto",
    )
    messages = [
        HumanMessage(content="search"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_hr_knowledge_base",
                    "args": {"query": "绩效反馈", "scopes": ["performance"]},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content='{"status":"success","matches":[]}',
            tool_call_id="call-1",
            name="search_hr_knowledge_base",
        ),
    ]

    payload = model._build_payload(messages)

    assistant = payload["messages"][1]
    tool_message = payload["messages"][2]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert (
        assistant["tool_calls"][0]["function"]["name"]
        == "search_hr_knowledge_base"
    )
    assert json.loads(
        assistant["tool_calls"][0]["function"]["arguments"]
    ) == {"query": "绩效反馈", "scopes": ["performance"]}
    assert tool_message == {
        "role": "tool",
        "content": '{"status":"success","matches":[]}',
        "tool_call_id": "call-1",
        "name": "search_hr_knowledge_base",
    }
    assert payload["tool_choice"] == "auto"
    assert "response_format" not in payload


def test_model_adapter_extracts_tool_call_argument_variants():
    camel_case_calls = ModelFarmLangChainChatModel._extract_tool_calls(
        {
            "choices": [
                {
                    "message": {
                        "toolCalls": [
                            {
                                "name": "ExampleStructuredResponse",
                                "input": {"value": "ok"},
                            }
                        ]
                    }
                }
            ]
        }
    )
    assert camel_case_calls[0]["args"] == {"value": "ok"}

    legacy_calls = ModelFarmLangChainChatModel._extract_tool_calls(
        {
            "choices": [
                {
                    "message": {
                        "function_call": {
                            "name": "ExampleStructuredResponse",
                            "arguments": '{"value":"ok"}',
                        }
                    }
                }
            ]
        }
    )
    assert legacy_calls[0]["args"] == {"value": "ok"}


def test_bosch_messages_auto_response_format_uses_native_payload():
    model = ModelFarmLangChainChatModel(
        settings=Settings(
            llm_provider="bosch_messages",
            llm_response_format_style="auto",
        )
    )
    assert model._format_response_format("json_schema") == "json_schema"


def test_stream_line_payload_handles_sse_data_lines():
    assert ModelFarmLangChainChatModel._stream_line_payload('data: {"a": 1}') == '{"a": 1}'
    assert ModelFarmLangChainChatModel._stream_line_payload('data: [DONE]') == '[DONE]'
    assert ModelFarmLangChainChatModel._stream_line_payload(': keepalive') is None


def test_stream_delta_extracts_openai_delta_content():
    data = {"choices": [{"delta": {"content": "你"}}]}
    assert ModelFarmLangChainChatModel._extract_stream_content_delta(data) == "你"


def test_stream_delta_extracts_bosch_nested_message_content():
    data = {"data": {"messages": [{"role": "assistant", "content": "你好"}]}}
    assert ModelFarmLangChainChatModel._extract_stream_content_delta(data) == "你好"


def test_stream_extracts_openai_and_bosch_nested_thinking_content():
    assert ModelFarmLangChainChatModel._extract_stream_thinking_fragment(
        {"choices": [{"delta": {"reasoning_content": "分析"}}]}
    ) == "分析"
    assert ModelFarmLangChainChatModel._extract_stream_thinking_fragment(
        {"data": {"choices": [{"delta": {"thinkingContent": "继续"}}]}}
    ) == "继续"


def test_stream_extracts_openai_and_bosch_finish_reasons():
    assert ModelFarmLangChainChatModel._extract_stream_finish_reasons(
        {"choices": [{"finish_reason": "length"}]}
    ) == ["length"]
    assert ModelFarmLangChainChatModel._extract_stream_finish_reasons(
        {"data": {"choices": [{"finishReason": "tool_calls"}]}}
    ) == ["tool_calls"]


def test_stream_aggregate_rejects_missing_terminal_marker_when_required():
    with pytest.raises(StructuredOutputError) as exc_info:
        ModelFarmLangChainChatModel._aggregate_stream_payloads(
            [{"choices": [{"delta": {"content": "{\"value\":\"ok\"}"}}]}],
            require_complete=True,
        )

    assert exc_info.value.code == "truncated_stream"
    assert "without [DONE] or a finish reason" in str(exc_info.value)


def test_stream_aggregate_replaces_cumulative_json_delta_snapshot():
    response = ModelFarmLangChainChatModel._aggregate_stream_payloads(
        [
            {"choices": [{"delta": {"content": '```json\n{"value":'}}]},
            {
                "data": {
                    "choices": [
                        {
                            "delta": {
                                "content": '```json\n{"value":"ok"}\n```'
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            },
            {"_stream_done": True},
        ],
        require_complete=True,
    )

    assert response["choices"][0]["message"]["content"] == (
        '```json\n{"value":"ok"}\n```'
    )


def test_stream_aggregate_classifies_missing_tool_name_as_truncated_output():
    with pytest.raises(StructuredOutputError) as exc_info:
        ModelFarmLangChainChatModel._aggregate_stream_payloads(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": '{"value":"ok"}'},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ]
        )

    assert exc_info.value.code == "truncated_stream"


def test_stream_aggregate_classifies_empty_stream_as_missing_output():
    with pytest.raises(StructuredOutputError) as exc_info:
        ModelFarmLangChainChatModel._aggregate_stream_payloads(
            [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        )

    assert exc_info.value.code == "missing_output"


@pytest.mark.asyncio
async def test_stream_rejects_finish_only_length_frame_even_with_valid_tool_json(monkeypatch):
    class Response:
        status_code = 200

        async def aiter_lines(self):
            yield (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
                '"function":{"name":"Result","arguments":"{\\\"value\\\":\\\"ok\\\"}"}}]}}]}'
            )
            yield 'data: {"data":{"choices":[{"delta":{},"finish_reason":"length"}]}}'
            yield "data: [DONE]"

    class StreamContext:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def stream(self, *args, **kwargs):
            return StreamContext()

    monkeypatch.setattr(llm_module, "get_shared_async_client", lambda: Client())
    monkeypatch.setattr(
        ModelFarmLangChainChatModel,
        "_log_llm_metric",
        lambda *args, **kwargs: None,
    )
    model = ModelFarmLangChainChatModel(
        settings=Settings(llm_provider="bosch_openai_compatible"),
        task_name="guidance",
        explicit_model="glm-5.2",
        max_retries=0,
    )

    with pytest.raises(StructuredOutputError) as exc_info:
        await model.ainvoke_stream_message([HumanMessage(content="prepare")])

    assert exc_info.value.code == "truncated_stream"
    assert "finish_reason=length" in str(exc_info.value)


@pytest.mark.asyncio
async def test_guidance_stream_records_ttft_thinking_and_tool_call_completion(monkeypatch):
    class Response:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"reasoning_content":"分析"}}]}'
            yield 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"Result","arguments":"{\\"val"}}]}}]}'
            yield 'data: {"data":{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ue\\":\\"ok\\"}"}}]}}]}}'
            yield "data: [DONE]"

    class StreamContext:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def stream(self, *args, **kwargs):
            return StreamContext()

    timings: dict[str, object] = {}
    logged: dict[str, object] = {}
    elapsed_values = iter((10, 20, 25, 30, 40))
    monkeypatch.setattr(llm_module, "get_shared_async_client", lambda: Client())
    monkeypatch.setattr(llm_module, "elapsed_ms", lambda _started: next(elapsed_values))
    monkeypatch.setattr(
        llm_module,
        "record_llm_stream_timing",
        lambda **fields: timings.update(fields),
    )
    monkeypatch.setattr(
        llm_module,
        "log_metric",
        lambda event, **fields: logged.update({"event": event, **fields}),
    )
    monkeypatch.setattr(
        ModelFarmLangChainChatModel,
        "_log_llm_metric",
        lambda *args, **kwargs: None,
    )
    model = ModelFarmLangChainChatModel(
        settings=Settings(llm_provider="bosch_openai_compatible"),
        task_name="guidance",
        explicit_model="glm-5.2",
        max_retries=0,
    )

    message = await model.ainvoke_stream_message(
        [HumanMessage(content="prepare")],
        record_stream_timing=True,
    )

    assert message.tool_calls[0]["args"] == {"value": "ok"}
    assert timings["ttft_ms"] == 10
    assert timings["thinking_complete_ms"] == 20
    assert timings["tool_call_complete_ms"] == 30
    assert logged["event"] == "llm.stream_timing"
    assert logged["complete"] is True


@pytest.mark.asyncio
async def test_structured_stream_extractors_run_once_per_payload(monkeypatch):
    class Response:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"{\\"value\\":"}}]}'
            yield 'data: {"data":{"choices":[{"delta":{"content":"\\"ok\\"}"},"finish_reason":"stop"}]}}'
            yield "data: [DONE]"

    class StreamContext:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def stream(self, *args, **kwargs):
            return StreamContext()

    content_updates: list[str] = []
    counts = {"content": 0, "thinking": 0, "tools": 0, "usage": 0, "finish": 0}
    depths = {key: 0 for key in counts}
    extractors = {
        "content": "_extract_stream_content_fragment",
        "thinking": "_extract_stream_thinking_fragment",
        "tools": "_extract_stream_tool_call_fragments",
        "usage": "_extract_stream_usage",
        "finish": "_extract_stream_finish_reasons",
    }
    for key, name in extractors.items():
        original = getattr(ModelFarmLangChainChatModel, name)

        def wrapper(data, *, _key=key, _original=original):
            if depths[_key] == 0:
                counts[_key] += 1
            depths[_key] += 1
            try:
                return _original(data)
            finally:
                depths[_key] -= 1

        monkeypatch.setattr(
            ModelFarmLangChainChatModel,
            name,
            staticmethod(wrapper),
        )
    monkeypatch.setattr(llm_module, "get_shared_async_client", lambda: Client())
    monkeypatch.setattr(
        ModelFarmLangChainChatModel,
        "_log_llm_metric",
        lambda *args, **kwargs: None,
    )
    model = ModelFarmLangChainChatModel(
        settings=Settings(llm_provider="bosch_openai_compatible"),
        task_name="guidance",
        explicit_model="glm-5.2",
        max_retries=0,
    )

    message = await model.ainvoke_stream_message(
        [HumanMessage(content="prepare")],
        on_content_update=content_updates.append,
    )

    assert message.content == '{"value":"ok"}'
    assert content_updates == ['{"value":', '{"value":"ok"}']
    assert counts == {"content": 2, "thinking": 2, "tools": 2, "usage": 2, "finish": 2}


def test_json_schema_generation_is_cached_by_schema_and_transport_options():
    schema_calls: list[int] = []

    class CachedSchema(BaseModel):
        value: str

        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            schema_calls.append(1)
            return super().model_json_schema(*args, **kwargs)

    helper = LangChainLLMService._json_schema_response_format
    helper.cache_clear()
    try:
        first = helper(CachedSchema, schema_name="cached_schema", strict=False)
        second = helper(CachedSchema, schema_name="cached_schema", strict=False)
    finally:
        helper.cache_clear()

    assert first is second
    assert schema_calls == [1]


def test_stream_aggregate_reassembles_fragmented_openai_and_bosch_tool_calls():
    payloads = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "Multimodal",
                                    "arguments": '{"val',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "data": {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "Result",
                                        "arguments": 'ue":"ok"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        },
        {"usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
        {"data": {"choices": [{"finish_reason": "tool_calls"}]}},
    ]

    response = ModelFarmLangChainChatModel._aggregate_stream_payloads(payloads)
    calls = ModelFarmLangChainChatModel._extract_tool_calls(response)

    assert calls == [
        {
            "name": "MultimodalResult",
            "args": {"value": "ok"},
            "id": "call_1",
            "type": "tool_call",
        }
    ]
    assert response["usage"]["total_tokens"] == 14
    assert response["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_aggregate_does_not_replace_usage_with_empty_payload():
    response = ModelFarmLangChainChatModel._aggregate_stream_payloads(
        [
            {
                "choices": [{"delta": {"content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
            {"choices": [{"finish_reason": "stop"}], "usage": {}},
        ]
    )

    assert response["usage"] == {"prompt_tokens": 2, "completion_tokens": 1}


def test_stream_aggregate_reassembles_bosch_nested_content_json():
    response = ModelFarmLangChainChatModel._aggregate_stream_payloads(
        [
            {"data": {"choices": [{"delta": {"content": '{"value":'}}]}},
            {"data": {"choices": [{"delta": {"content": '"ok"}'}}]}},
        ]
    )

    assert response["choices"][0]["message"]["content"] == '{"value":"ok"}'


def test_stream_aggregate_rejects_embedded_errors():
    with pytest.raises(LLMError, match="stream error"):
        ModelFarmLangChainChatModel._aggregate_stream_payloads(
            [{"data": {"error": {"code": "invalid_request", "message": "bad input"}}}]
        )


@pytest.mark.asyncio
async def test_stream_payload_reader_rejects_malformed_json():
    class Response:
        async def aiter_lines(self):
            yield "data: {not-json}"

    model = ModelFarmLangChainChatModel(settings=Settings())
    with pytest.raises(LLMError, match="non-JSON SSE data"):
        async for _ in model._iter_stream_payloads(Response()):
            pass


def test_multimodal_payload_preserves_content_blocks_and_disables_thinking():
    settings = Settings(
        llm_provider="bosch_openai_compatible",
        llm_enable_thinking=True,
    )
    model = ModelFarmLangChainChatModel(
        settings=settings,
        task_name="document_vision",
        explicit_model="qwen3.7-plus",
        enable_thinking=False,
    )
    content = [
        {"type": "text", "text": "describe"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AA=="},
        },
    ]

    payload = model._build_payload([HumanMessage(content=content)])

    assert payload["model"] == "qwen3.7-plus"
    assert payload["messages"][0]["content"] == content
    assert payload["enable_thinking"] is False
    assert model._identifying_params["endpoint"] == settings.chat_url


def test_task_thinking_route_controls_payload_and_budget():
    settings = Settings(
        llm_enable_thinking=False,
        llm_thinking_budget=8192,
        llm_guidance_enable_thinking=True,
        llm_employee_reply_enable_thinking=False,
    )

    guidance_payload = ModelFarmLangChainChatModel(
        settings=settings,
        task_name="guidance",
    )._build_payload([HumanMessage(content="prepare")])
    employee_payload = ModelFarmLangChainChatModel(
        settings=settings,
        task_name="employee_reply",
    )._build_payload([HumanMessage(content="reply")])

    assert guidance_payload["enable_thinking"] is True
    assert guidance_payload["thinking_budget"] == 8192
    assert employee_payload["enable_thinking"] is False
    assert "thinking_budget" not in employee_payload


class MultimodalResult(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_multimodal_structured_invocation_reuses_structured_service(monkeypatch):
    service = LangChainLLMService(
        settings=Settings(llm_document_vision_enable_thinking=False)
    )
    captured = {}
    content = [
        {"type": "text", "text": "describe"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AA=="},
        },
    ]

    async def fake_structured(**kwargs):
        captured.update(kwargs)
        return MultimodalResult(value="ok")

    monkeypatch.setattr(service, "ainvoke_structured", fake_structured)

    result = await service.ainvoke_multimodal_structured(
        content=content,
        schema=MultimodalResult,
        model="qwen3.7-plus",
    )

    assert result.value == "ok"
    assert captured["prompt"] == content
    assert captured["model"] == "qwen3.7-plus"
    assert captured["task_name"] == "document_vision"
    assert captured["enable_thinking"] is None


@pytest.mark.asyncio
async def test_multimodal_thinking_routes_to_streamed_single_structured(monkeypatch):
    service = LangChainLLMService(
        settings=Settings(llm_document_vision_enable_thinking=True)
    )
    captured = {}

    async def fake_single(**kwargs):
        captured.update(kwargs)
        return MultimodalResult(value="ok")

    monkeypatch.setattr(service, "ainvoke_structured_single", fake_single)

    result = await service.ainvoke_multimodal_structured(
        content=[{"type": "text", "text": "describe"}],
        schema=MultimodalResult,
        task_name="document_vision",
        model="qwen3.7-plus",
        enable_thinking=True,
    )

    assert result.value == "ok"
    assert captured["stream"] is True
    assert captured["enable_thinking"] is True
    assert captured["task_name"] == "document_vision"

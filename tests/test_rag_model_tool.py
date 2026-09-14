from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError
import pytest

from backend.business_config.loader import get_config_loader
from backend.config.settings import Settings
from backend.schemas.retrieval import RetrievedChunk
from backend.services.knowledge_skill_service import KNOWLEDGE_SKILL_AGENTS
from backend.services.langchain_llm_service import ModelFarmLangChainChatModel
from backend.services.retrieval_service import RetrievalService
from backend.tools import rag_search_tool as rag_tool_module
from backend.tools.rag_search_tool import (
    ALLOWED_RAG_SCOPES,
    RAG_TOOL_AGENT_NAME,
    RAG_TOOL_FINAL_TOP_K,
    RAG_TOOL_MAX_CALLS,
    RAG_TOOL_NAME,
    RagSearchInput,
    create_rag_search_tool,
    create_rag_tool_agent,
)
from scripts import preflight_rag_tool


def _chunk(index: int, *, scope: str = "performance") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{index}",
        source_id=f"source-{index}",
        title=f"title-{index}",
        scope=scope,
        text=f"complete text {index}",
        score=1.0 - index / 100,
        metadata={"internal": "not returned"},
    )


class _FakeRetrieval:
    def __init__(self, chunks=None, error: Exception | None = None):
        self.chunks = list(chunks or [])
        self.error = error
        self.calls = []

    async def aretrieve(self, agent_name, context, top_k=None):
        self.calls.append((agent_name, context, top_k))
        if self.error:
            raise self.error
        return self.chunks


@pytest.mark.asyncio
async def test_rag_tool_uses_existing_pipeline_and_returns_safe_top_eight_json():
    retrieval = _FakeRetrieval([_chunk(index) for index in range(10)])
    tool = create_rag_search_tool(retrieval)

    raw = await tool.ainvoke(
        {
            "query": "绩效反馈应该如何开展？",
            "scopes": ["performance", "performance"],
        }
    )
    payload = json.loads(raw)

    assert retrieval.calls == [
        (
            RAG_TOOL_AGENT_NAME,
            {
                "tool_query": "绩效反馈应该如何开展？",
                "requested_scopes": ["performance"],
            },
            RAG_TOOL_FINAL_TOP_K,
        )
    ]
    assert payload["status"] == "success"
    assert payload["is_untrusted_reference"] is True
    assert payload["result_count"] == 8
    assert [item["chunk_id"] for item in payload["matches"]] == [
        f"chunk-{index}" for index in range(8)
    ]
    assert payload["matches"][0] == {
        "chunk_id": "chunk-0",
        "source_id": "source-0",
        "title": "title-0",
        "scope": "performance",
        "text": "complete text 0",
        "score": 1.0,
    }


@pytest.mark.asyncio
async def test_rag_tool_preserves_full_query_and_defaults_to_all_scopes():
    retrieval = _FakeRetrieval()
    tool = create_rag_search_tool(retrieval)
    query = "知识" * 10000

    payload = json.loads(await tool.ainvoke({"query": query}))

    assert retrieval.calls[0][1]["tool_query"] == query
    assert retrieval.calls[0][1]["requested_scopes"] == list(ALLOWED_RAG_SCOPES)
    assert payload["status"] == "no_results"
    assert payload["query"] == query


@pytest.mark.asyncio
async def test_rag_tool_can_mark_background_admission_without_changing_output():
    retrieval = _FakeRetrieval([_chunk(0)])
    tool = create_rag_search_tool(retrieval, admission_priority=10)

    payload = json.loads(
        await tool.ainvoke({"query": "绩效证据", "scopes": ["performance"]})
    )

    assert retrieval.calls[0][1]["_retrieval_admission_priority"] == 10
    assert payload["status"] == "success"
    assert payload["matches"][0]["chunk_id"] == "chunk-0"


@pytest.mark.asyncio
async def test_rag_tool_enforces_runtime_scope_boundary_and_disables_skills():
    retrieval = _FakeRetrieval([_chunk(0)])
    tool = create_rag_search_tool(
        retrieval,
        allowed_scopes=["performance", "general"],
        disable_knowledge_skills=True,
    )

    denied = json.loads(
        await tool.ainvoke(
            {"query": "政策", "scopes": ["redline"]}
        )
    )
    allowed = json.loads(
        await tool.ainvoke(
            {"query": "绩效", "scopes": ["performance"]}
        )
    )

    assert denied["status"] == "error"
    assert denied["error"]["code"] == "scope_not_allowed"
    assert retrieval.calls == [
        (
            RAG_TOOL_AGENT_NAME,
            {
                "tool_query": "绩效",
                "requested_scopes": ["performance"],
                "_disable_knowledge_skills": True,
            },
            RAG_TOOL_FINAL_TOP_K,
        )
    ]
    assert allowed["status"] == "success"


def test_rag_tool_schema_forbids_unapproved_arguments_and_scopes():
    schema = RagSearchInput.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"query", "scopes"}
    with pytest.raises(ValidationError):
        RagSearchInput.model_validate(
            {"query": "test", "collection": "kb_chunks"}
        )
    with pytest.raises(ValidationError):
        RagSearchInput.model_validate(
            {"query": "test", "scopes": ["general; DROP TABLE kb_chunks"]}
        )


@pytest.mark.asyncio
async def test_rag_tool_errors_do_not_expose_internal_details():
    secret = "postgresql://admin:password@postgres/hr_agent"
    tool = create_rag_search_tool(
        _FakeRetrieval(error=RuntimeError(f"database failed: {secret}"))
    )

    raw = await tool.ainvoke({"query": "test", "scopes": ["general"]})
    payload = json.loads(raw)

    assert payload["status"] == "error"
    assert payload["error"]["code"] == "knowledge_search_failed"
    assert secret not in raw
    assert "RuntimeError" not in raw


class _NoSkills:
    def __init__(self):
        self.calls = []

    def select(self, agent_name, context):
        self.calls.append((agent_name, context))
        return []

    @staticmethod
    def retrieval_templates(_active_skills):
        return []


def test_model_rag_tool_config_is_explicit_and_renders_only_requested_scopes():
    router = _NoSkills()
    service = object.__new__(RetrievalService)
    service.loader = get_config_loader()
    service.settings = SimpleNamespace(
        rag_hybrid_search_enabled=False,
        postgres_bm25_tokenizer_name="test-tokenizer",
        kb_index_version="test-index",
        effective_embedding_provider="local_qwen",
        effective_embedding_model="test-embedding",
        embedding_url="http://embedding/v1/embeddings",
        effective_rerank_provider="local_qwen",
        effective_rerank_model="test-reranker",
        rerank_url="http://reranker/v1/completions",
    )
    service.knowledge_skill_router = router
    service._available_scopes = lambda configured: configured

    plan = service._build_plan(
        RAG_TOOL_AGENT_NAME,
        {
            "tool_query": "职业发展与绩效反馈",
            "requested_scopes": ["career", "performance"],
        },
        RAG_TOOL_FINAL_TOP_K,
    )

    assert plan is not None
    assert [(spec.query, spec.scopes) for spec in plan.query_specs] == [
        ("职业发展与绩效反馈", ["career"]),
        ("职业发展与绩效反馈", ["performance"]),
    ]
    assert plan.dense_top_k == 28
    assert plan.lexical_top_k == 28
    assert plan.rrf_k == 60
    assert plan.fusion_top_n == 46
    assert plan.rerank_candidate_top_n == 32
    assert plan.final_top_k == 8
    assert plan.hybrid_enabled is True
    assert plan.rerank_enabled is True
    assert plan.query_parallelism == 2
    assert plan.active_skills == []
    assert router.calls[0][0] == RAG_TOOL_AGENT_NAME
    assert RAG_TOOL_AGENT_NAME not in KNOWLEDGE_SKILL_AGENTS


class _FakeModel:
    def __init__(self):
        self.bind_calls = []

    def bind_tools(self, tools, *, tool_choice=None, **_kwargs):
        self.bind_calls.append((tools, tool_choice))
        return self


class _FakeLLMService:
    def __init__(self, model):
        self.model = model
        self.chat_model_kwargs = None

    def chat_model(self, **kwargs):
        self.chat_model_kwargs = kwargs
        return self.model


class _FakeAgent:
    def __init__(self):
        self.config = None

    def with_config(self, config):
        self.config = config
        return self


def test_agent_factory_uses_auto_tools_without_response_format(monkeypatch):
    captured = {}
    fake_agent = _FakeAgent()

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return fake_agent

    monkeypatch.setattr(rag_tool_module, "create_agent", fake_create_agent)
    fake_model = _FakeModel()
    llm_service = _FakeLLMService(fake_model)

    result = create_rag_tool_agent(
        _FakeRetrieval(),
        llm_service=llm_service,
    )

    assert result is fake_agent
    assert llm_service.chat_model_kwargs["response_format"] is None
    assert captured["model"] is fake_model
    assert captured["response_format"] is None
    assert [tool.name for tool in captured["tools"]] == [RAG_TOOL_NAME]
    assert type(captured["middleware"][0]).__name__ == "_AutoToolChoiceMiddleware"
    limiter = captured["middleware"][1]
    assert limiter.tool_name == RAG_TOOL_NAME
    assert limiter.run_limit == RAG_TOOL_MAX_CALLS
    assert limiter.exit_behavior == "error"
    assert fake_agent.config == {"recursion_limit": 10}

    with pytest.raises(ValueError, match="between 1 and 4"):
        create_rag_tool_agent(_FakeRetrieval(), model=fake_model, max_tool_calls=5)


@pytest.mark.asyncio
async def test_real_langchain_agent_closes_the_two_turn_tool_loop(monkeypatch):
    captured_payloads = []

    async def fake_agenerate(self, messages, **_kwargs):
        captured_payloads.append(self._build_payload(messages))
        if len(captured_payloads) == 1:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": RAG_TOOL_NAME,
                        "args": {
                            "query": "绩效反馈",
                            "scopes": ["performance"],
                        },
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            message = AIMessage(content="final answer")
        return ChatResult(generations=[ChatGeneration(message=message)])

    monkeypatch.setattr(
        ModelFarmLangChainChatModel,
        "_agenerate",
        fake_agenerate,
    )
    retrieval = _FakeRetrieval([_chunk(0)])
    model = ModelFarmLangChainChatModel(
        settings=Settings(llm_provider="bosch_openai_compatible")
    )
    agent = create_rag_tool_agent(retrieval, model=model)

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "如何开展绩效反馈？"}]}
    )

    assert len(captured_payloads) == 2
    assert retrieval.calls[0][0] == RAG_TOOL_AGENT_NAME
    assert captured_payloads[0]["tool_choice"] == "auto"
    assert "response_format" not in captured_payloads[0]
    assert "response_format" not in captured_payloads[1]
    second_roles = [row["role"] for row in captured_payloads[1]["messages"]]
    assert second_roles[-2:] == ["assistant", "tool"]
    assert captured_payloads[1]["messages"][-1]["tool_call_id"] == "call-1"
    assert result["messages"][-1].content == "final answer"


@pytest.mark.asyncio
async def test_preflight_reports_only_call_metadata(monkeypatch):
    tool_payload = {
        "status": "success",
        "scopes": ["performance"],
        "matches": [{"chunk_id": "chunk-1", "text": "secret evidence"}],
    }

    class FakeAgent:
        async def ainvoke(self, _request):
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": RAG_TOOL_NAME,
                                "args": {
                                    "query": "绩效",
                                    "scopes": ["performance"],
                                },
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    ToolMessage(
                        content=json.dumps(tool_payload),
                        tool_call_id="call-1",
                    ),
                    AIMessage(content="final answer"),
                ]
            }

    monkeypatch.setattr(
        preflight_rag_tool,
        "create_rag_tool_agent",
        lambda *_args, **_kwargs: FakeAgent(),
    )
    report = await preflight_rag_tool.run_rag_tool_preflight(
        query="绩效",
        scopes=["performance"],
        model="test-model",
        timeout_seconds=1,
        retrieval_service=_FakeRetrieval(),
    )

    assert report["passed"] is True
    assert report["tool_call_count"] == 1
    assert report["scopes"] == ["performance"]
    assert report["chunk_ids"] == ["chunk-1"]
    assert "secret evidence" not in json.dumps(report)
    assert "final answer" not in json.dumps(report)


@pytest.mark.asyncio
async def test_preflight_cli_failure_output_does_not_leak_internal_error(
    monkeypatch,
    capsys,
):
    secret = "postgresql://admin:password@postgres/hr_agent"

    async def fail_preflight(**_kwargs):
        raise RuntimeError(secret)

    async def close_client():
        return None

    monkeypatch.setattr(
        preflight_rag_tool,
        "run_rag_tool_preflight",
        fail_preflight,
    )
    monkeypatch.setattr(
        preflight_rag_tool,
        "close_shared_async_client",
        close_client,
    )

    status = await preflight_rag_tool._async_main(
        SimpleNamespace(
            query="test",
            scopes=["general"],
            model="test-model",
            timeout_seconds=1,
        )
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert status == 1
    assert set(payload) == {
        "tool_call_count",
        "scopes",
        "chunk_ids",
        "elapsed_ms",
    }
    assert secret not in output

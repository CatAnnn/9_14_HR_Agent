from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
import pytest

from backend.business_config.loader import get_config_loader
from backend.schemas.retrieval import RetrievedChunk
from backend.services.agentic_evidence_service import (
    AgenticEvidencePlanner,
    AgenticSearchConfig,
)


def _chunk(
    chunk_id: str,
    *,
    scope: str = "performance",
    chunk_index: int = 0,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id=f"source-{chunk_id}",
        title=f"title-{chunk_id}",
        scope=scope,
        text=f"evidence {chunk_id}",
        score=0.8,
        metadata={
            "doc_id": f"doc-{scope}",
            "chunk_index": chunk_index,
            "collection_name": f"kb-{scope}",
        },
    )


class _Cache:
    def __init__(self):
        self.values: dict[str, list[RetrievedChunk]] = {}
        self.set_calls: list[tuple[str, bool]] = []

    async def aget(self, key):
        value = self.values.get(key)
        if value is None:
            return None
        return [chunk.model_copy(deep=True) for chunk in value]

    async def aset(self, key, chunks, *, cache_empty=False):
        self.set_calls.append((key, cache_empty))
        if chunks or cache_empty:
            self.values[key] = [
                chunk.model_copy(deep=True) for chunk in chunks
            ]

    @asynccontextmanager
    async def singleflight(self, _key):
        yield


class _Retrieval:
    def __init__(self):
        self.result_cache = _Cache()
        self.calls: list[tuple[str, dict, int | None]] = []
        self.neighbor_calls: list[tuple[list[str], int]] = []
        self.rerank_calls: list[tuple[list[str], str, int]] = []
        self.neighbors: list[RetrievedChunk] = []
        self.results_by_query: dict[str, list[RetrievedChunk]] = {}

    @staticmethod
    def agentic_search_config():
        return AgenticSearchConfig().model_dump(mode="json")

    @staticmethod
    def configured_scopes(_agent_name):
        return ["performance", "general"]

    @staticmethod
    def retrieval_config_identity(agent_name):
        return {"version": "test", "query": agent_name}

    async def aretrieve(self, agent_name, context, top_k=None):
        self.calls.append((agent_name, dict(context), top_k))
        query = str(context["tool_query"])
        scope = str(context["requested_scopes"][0])
        if query in self.results_by_query:
            return [
                chunk.model_copy(deep=True)
                for chunk in self.results_by_query[query]
            ]
        return [_chunk(f"tool-{query}", scope=scope)]

    async def aretrieve_neighbors(
        self,
        chunks,
        *,
        window=1,
        admission_priority=0,
    ):
        self.neighbor_calls.append(
            ([chunk.chunk_id for chunk in chunks], window)
        )
        return [chunk.model_copy(deep=True) for chunk in self.neighbors]

    async def arerank_candidates(
        self,
        chunks,
        *,
        query,
        top_k,
        agent_name,
        admission_priority=0,
    ):
        self.rerank_calls.append(
            ([chunk.chunk_id for chunk in chunks], query, top_k)
        )
        return [
            chunk.model_copy(deep=True)
            for chunk in list(reversed(chunks))[:top_k]
        ]


class _Scheduler:
    def __init__(self):
        self.categories: list[str] = []

    @asynccontextmanager
    async def slot(self, **kwargs):
        self.categories.append(str(kwargs["category"]))
        yield SimpleNamespace(queue_ms=0.0, active_count=1)


class _Model:
    def __init__(self, responses, *, wait: asyncio.Event | None = None):
        self.responses = list(responses)
        self.wait = wait
        self.bind_calls = []
        self.message_history = []

    def bind_tools(self, tools, *, tool_choice=None, **_kwargs):
        self.bind_calls.append((tools, tool_choice))
        return self

    async def ainvoke(self, messages):
        self.message_history.append(list(messages))
        if self.wait is not None:
            await self.wait.wait()
        if not self.responses:
            raise AssertionError("unexpected planner model call")
        return self.responses.pop(0)


class _LLMService:
    def __init__(self, model):
        self.model = model
        self.kwargs = []

    def chat_model(self, **kwargs):
        self.kwargs.append(kwargs)
        return self.model


class _Settings:
    rag_agentic_search_mode = "enabled"
    rag_agentic_search_shadow_sample_rate = 1.0
    rag_agentic_search_active_sample_rate = 1.0
    rag_agentic_search_timeout_seconds = 1.0
    chat_url = "https://model.example/chat"
    llm_provider = "bosch_openai_compatible"
    kb_index_version = "v2"
    effective_embedding_provider = "bosch"
    effective_embedding_model = "embedding"
    effective_rerank_provider = "bosch"
    effective_rerank_model = "reranker"

    @staticmethod
    def model_for_task(task_name):
        return f"model-{task_name}"

    @staticmethod
    def temperature_for_task(_task_name):
        return 0.0

    @staticmethod
    def max_tokens_for_task(_task_name):
        return 1024

    @staticmethod
    def enable_thinking_for_task(_task_name):
        return False


def _tool_call(call_id: str, query: str, scopes: list[str]):
    return {
        "name": "search_hr_knowledge_base",
        "args": {"query": query, "scopes": scopes},
        "id": call_id,
        "type": "tool_call",
    }


def _planner(
    responses,
    *,
    retrieval: _Retrieval | None = None,
    settings: _Settings | None = None,
    wait: asyncio.Event | None = None,
):
    retrieval = retrieval or _Retrieval()
    model = _Model(responses, wait=wait)
    scheduler = _Scheduler()
    llm = _LLMService(model)
    planner = AgenticEvidencePlanner(
        retrieval=retrieval,
        model_scheduler=scheduler,
        result_cache=retrieval.result_cache,
        llm_service=llm,
        settings=settings or _Settings(),
        config=AgenticSearchConfig(),
    )
    return planner, retrieval, model, scheduler, llm


async def _enhance(planner, baseline):
    return await planner.enhance(
        flow="guidance",
        session_id="session-1",
        dimension="start",
        retrieval_name="guidance_start",
        objective="开场与目的",
        context={"profile": {"role": "Engineer"}},
        initial_chunks=baseline,
    )


@pytest.mark.asyncio
async def test_zero_tool_calls_preserve_baseline_and_cache_empty_result():
    planner, retrieval, model, scheduler, llm = _planner(
        [AIMessage(content="evidence_sufficient")]
    )
    baseline = [_chunk("baseline")]

    first = await _enhance(planner, baseline)
    second = await _enhance(planner, baseline)

    assert [chunk.chunk_id for chunk in first] == ["baseline"]
    assert [chunk.chunk_id for chunk in second] == ["baseline"]
    assert retrieval.calls == []
    assert len(model.message_history) == 1
    assert retrieval.result_cache.set_calls[0][1] is True
    assert llm.kwargs[0]["task_name"] == "guidance_evidence"
    assert llm.kwargs[0]["response_format"] is None
    assert llm.kwargs[0]["max_retries"] == 0
    assert model.bind_calls[0][1] == "auto"
    assert scheduler.categories == ["guidance_evidence"]


@pytest.mark.asyncio
async def test_first_round_three_parallel_calls_and_one_follow_up_add_top_four():
    first = AIMessage(
        content="",
        tool_calls=[
            _tool_call("call-1", "q1", ["performance"]),
            _tool_call("call-2", "q2", ["general"]),
            _tool_call("call-3", "q3", ["performance"]),
        ],
    )
    second = AIMessage(
        content="",
        tool_calls=[_tool_call("call-4", "q4", ["general"])],
    )
    retrieval = _Retrieval()
    retrieval.neighbors = [
        _chunk("neighbor-1", scope="performance", chunk_index=1),
        _chunk("neighbor-2", scope="general", chunk_index=1),
    ]
    planner, retrieval, model, scheduler, _llm = _planner(
        [first, second],
        retrieval=retrieval,
    )
    baseline = [_chunk("baseline")]

    result = await _enhance(planner, baseline)

    assert result[0] is baseline[0]
    assert len(result) == 5
    assert len(retrieval.calls) == 4
    assert all(call[2] == 8 for call in retrieval.calls)
    assert all(
        call[1]["_disable_knowledge_skills"] is True
        for call in retrieval.calls
    )
    assert retrieval.neighbor_calls[0][1] == 1
    assert retrieval.rerank_calls[0][2] == 4
    assert scheduler.categories == [
        "guidance_evidence",
        "guidance_evidence",
    ]
    assert any(
        isinstance(message, ToolMessage)
        for message in model.message_history[1]
    )
    assert all(
        chunk.metadata["agentic_search"]["selected_for_context"] is True
        for chunk in result[1:]
    )


@pytest.mark.asyncio
async def test_tool_call_cap_is_four_and_baseline_duplicate_is_never_replaced():
    first = AIMessage(
        content="",
        tool_calls=[
            _tool_call(f"call-{index}", f"q{index}", ["performance"])
            for index in range(1, 6)
        ],
    )
    second = AIMessage(
        content="",
        tool_calls=[
            _tool_call("call-6", "q6", ["general"]),
            _tool_call("call-7", "q7", ["general"]),
        ],
    )
    retrieval = _Retrieval()
    baseline = _chunk("baseline")
    retrieval.results_by_query["q1"] = [
        baseline.model_copy(deep=True),
        _chunk("new-q1"),
    ]
    planner, retrieval, _model, _scheduler, _llm = _planner(
        [first, second],
        retrieval=retrieval,
    )

    result = await _enhance(planner, [baseline])

    assert len(retrieval.calls) == 4
    assert [call[1]["tool_query"] for call in retrieval.calls] == [
        "q1",
        "q2",
        "q3",
        "q6",
    ]
    assert result[0] is baseline
    assert [chunk.chunk_id for chunk in result].count("baseline") == 1
    assert len(result) <= 5


@pytest.mark.asyncio
async def test_single_tool_call_can_end_without_follow_up_search():
    planner, retrieval, model, _scheduler, _llm = _planner(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call-1", "q1", ["performance"])
                ],
            ),
            AIMessage(content="evidence_sufficient"),
        ]
    )

    result = await _enhance(planner, [_chunk("baseline")])

    assert len(retrieval.calls) == 1
    assert len(model.message_history) == 2
    assert [chunk.chunk_id for chunk in result] == [
        "baseline",
        "tool-q1",
    ]


@pytest.mark.asyncio
async def test_duplicate_queries_are_not_executed_across_rounds():
    planner, retrieval, _model, _scheduler, _llm = _planner(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call-1", "same query", ["performance"]),
                    _tool_call("call-2", " same   query ", ["performance"]),
                    _tool_call("call-3", "different", ["general"]),
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call-4", "SAME QUERY", ["performance"])
                ],
            ),
        ]
    )

    await _enhance(planner, [_chunk("baseline")])

    assert [call[1]["tool_query"] for call in retrieval.calls] == [
        "same query",
        "different",
    ]


@pytest.mark.asyncio
async def test_scope_escape_fails_open_without_running_retrieval():
    planner, retrieval, _model, _scheduler, _llm = _planner(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call-1", "policy", ["redline"])
                ],
            )
        ]
    )
    baseline = [_chunk("baseline")]

    result = await _enhance(planner, baseline)

    assert result == baseline
    assert retrieval.calls == []


@pytest.mark.asyncio
async def test_timeout_fails_open_and_cancels_model_wait():
    wait = asyncio.Event()
    settings = _Settings()
    settings.rag_agentic_search_timeout_seconds = 0.01
    planner, retrieval, _model, _scheduler, _llm = _planner(
        [AIMessage(content="late")],
        settings=settings,
        wait=wait,
    )
    baseline = [_chunk("baseline")]

    result = await _enhance(planner, baseline)

    assert result == baseline
    assert retrieval.calls == []


@pytest.mark.asyncio
async def test_request_cancellation_propagates_instead_of_returning_fallback():
    wait = asyncio.Event()
    planner, _retrieval, _model, _scheduler, _llm = _planner(
        [AIMessage(content="late")],
        wait=wait,
    )
    task = asyncio.create_task(_enhance(planner, [_chunk("baseline")]))
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_shadow_mode_returns_immediately_and_shutdown_cancels_background():
    wait = asyncio.Event()
    settings = _Settings()
    settings.rag_agentic_search_mode = "shadow"
    settings.rag_agentic_search_timeout_seconds = 10.0
    planner, _retrieval, _model, _scheduler, _llm = _planner(
        [AIMessage(content="evidence_sufficient")],
        settings=settings,
        wait=wait,
    )
    baseline = [_chunk("baseline")]

    result = await _enhance(planner, baseline)
    await asyncio.sleep(0)

    assert result == baseline
    assert planner._background_tasks
    assert planner.report_version_suffix == ""
    await planner.shutdown()
    assert planner._background_tasks == set()


def test_active_mode_changes_report_version_but_shadow_does_not():
    enabled, *_ = _planner([AIMessage(content="ok")])
    shadow_settings = _Settings()
    shadow_settings.rag_agentic_search_mode = "shadow"
    shadow, *_ = _planner(
        [AIMessage(content="ok")],
        settings=shadow_settings,
    )

    assert enabled.report_version_suffix.startswith("-agentic-")
    assert shadow.report_version_suffix == ""


def test_query_yaml_agentic_search_contract_is_strict_and_bounded():
    raw = get_config_loader().query_config_snapshot()["agentic_search"]
    config = AgenticSearchConfig.model_validate(raw)

    assert config.enabled_flows == ("guidance", "coach")
    assert config.max_tool_calls == 4
    assert config.max_rounds == 2
    assert config.first_round_max_parallel_calls == 3
    assert config.tool_top_k == 8
    assert config.neighbor_window == 1
    assert config.added_context_top_k == 4
    with pytest.raises(ValueError):
        AgenticSearchConfig.model_validate({**raw, "unknown": True})

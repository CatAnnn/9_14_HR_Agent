from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.api.routes.resource_chat import send_resource_chat_message
from backend.schemas.auth import AuthUserResponse
from backend.schemas.resource_chat import ResourceChatRequest
from backend.schemas.retrieval import RetrievedChunk
from backend.services.resource_chat_service import (
    ResourceChatService,
    ResourceKnowledgeRepository,
)


class FakeLLMService:
    def __init__(self):
        self.calls: list[dict] = []

    async def ainvoke_text(self, **kwargs):
        self.calls.append(kwargs)
        return "先描述具体行为及影响，再与员工确认下一步行动。"


class FakeScheduler:
    def __init__(self):
        self.calls: list[dict] = []

    @asynccontextmanager
    async def slot(self, **kwargs):
        self.calls.append(kwargs)
        yield None


class FakeRetrieval:
    def __init__(self, chunks: list[RetrievedChunk]):
        self.chunks = chunks
        self.calls: list[tuple[str, dict, int | None]] = []

    async def aretrieve(self, agent_name, context, top_k=None):
        self.calls.append((agent_name, context, top_k))
        return self.chunks


def settings(tmp_path):
    return SimpleNamespace(
        data_dir=tmp_path,
        chat_url="http://model.test/chat",
        model_for_task=lambda _: "test-model",
        max_tokens_for_task=lambda _: 700,
        timeout_for_task=lambda _: 5.0,
    )


def test_repository_only_loads_supported_files_from_its_own_root(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "feedback.md").write_text(
        "# 反馈方法\n\n使用 SBI 描述情境、行为和影响。",
        encoding="utf-8",
    )
    (resources / "ignored.json").write_text(
        '{"secret": "不应进入知识库"}',
        encoding="utf-8",
    )
    (tmp_path / "employee.txt").write_text(
        "员工私有档案不应进入资源知识库",
        encoding="utf-8",
    )

    repository = ResourceKnowledgeRepository(resources)
    matches = repository.retrieve("怎样使用 SBI 反馈")

    assert [item.source_id for item in matches] == ["feedback.md"]
    assert "员工私有档案" not in matches[0].content
    assert not repository.retrieve("不应进入知识库")


def test_repository_hot_reloads_changed_resource_files(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    source = resources / "coaching.md"
    source.write_text("# 辅导\n\n先明确目标。", encoding="utf-8")
    repository = ResourceKnowledgeRepository(resources)

    assert repository.retrieve("明确目标")
    source.write_text("# 辅导\n\n使用 GROW 探索现状和选项。", encoding="utf-8")

    assert repository.retrieve("GROW 探索现状")
    assert not repository.retrieve("明确目标")


@pytest.mark.asyncio
async def test_service_grounds_model_prompt_in_retrieved_resources(tmp_path):
    llm = FakeLLMService()
    scheduler = FakeScheduler()
    retrieval = FakeRetrieval([
        RetrievedChunk(
            chunk_id="resources/feedback.md::0",
            source_id="resources/feedback.md",
            title="反馈方法",
            scope="resources",
            text="SBI 包含情境、行为和影响。",
        )
    ])
    service = ResourceChatService(
        settings(tmp_path),
        retrieval=retrieval,
        llm_service=llm,
        model_scheduler=scheduler,
    )

    answer, sources = await service.answer(
        "SBI 反馈怎么说？",
        history=[("user", "我需要准备一次反馈"), ("assistant", "你想讨论什么？")],
        requester="manager@example.com",
    )

    assert answer.startswith("先描述具体行为")
    assert [item.source_id for item in sources] == ["resources/feedback.md"]
    assert "SBI 包含情境、行为和影响" in llm.calls[0]["prompt"]
    assert "我需要准备一次反馈" in llm.calls[0]["prompt"]
    assert scheduler.calls[0]["category"] == "interactive"
    assert "manager@example.com" not in scheduler.calls[0]["session_id"]
    assert retrieval.calls == [
        (
            "resource_chat",
            {"user_query": "SBI 反馈怎么说？", "_disable_knowledge_skills": True},
            4,
        )
    ]


@pytest.mark.asyncio
async def test_service_does_not_call_model_when_retrieval_has_no_match(tmp_path):
    llm = FakeLLMService()
    service = ResourceChatService(
        settings(tmp_path),
        retrieval=FakeRetrieval([]),
        llm_service=llm,
        model_scheduler=FakeScheduler(),
    )

    answer, sources = await service.answer("食堂今天供应什么？")

    assert "没有在资源知识库中找到" in answer
    assert sources == []
    assert llm.calls == []


@pytest.mark.asyncio
async def test_service_uses_english_ui_locale_for_greeting_and_no_match(tmp_path):
    service = ResourceChatService(
        settings(tmp_path),
        retrieval=FakeRetrieval([]),
        llm_service=FakeLLMService(),
        model_scheduler=FakeScheduler(),
    )

    greeting, _ = await service.answer("你好", locale="en")
    fallback, _ = await service.answer("食堂今天供应什么？", locale="en")

    assert greeting.startswith("Hello")
    assert fallback.startswith("I could not find content")


@pytest.mark.asyncio
async def test_english_prompt_preserves_source_title_and_text(tmp_path):
    llm = FakeLLMService()
    chunk = RetrievedChunk(
        chunk_id="resources/feedback.md::0",
        source_id="resources/feedback.md",
        title="反馈方法",
        scope="resources",
        text="SBI 包含情境、行为和影响。",
    )
    service = ResourceChatService(
        settings(tmp_path),
        retrieval=FakeRetrieval([chunk]),
        llm_service=llm,
        model_scheduler=FakeScheduler(),
    )

    await service.answer("How should I give feedback?", locale="en")

    prompt = llm.calls[0]["prompt"]
    assert "Write the entire answer in natural English" in prompt
    assert 'title="反馈方法"' in prompt
    assert "SBI 包含情境、行为和影响。" in prompt


@pytest.mark.asyncio
async def test_route_maps_accept_language_and_keeps_source_title() -> None:
    captured = {}

    class Service:
        async def answer(self, message, **kwargs):
            captured.update(message=message, **kwargs)
            return "English answer.", [
                RetrievedChunk(
                    chunk_id="source::0",
                    source_id="source",
                    title="原始来源标题",
                    scope="resources",
                    text="原始 chunk 内容。",
                )
            ]

    response = await send_resource_chat_message(
        ResourceChatRequest(message="Question"),
        accept_language="en-US,en;q=0.9",
        current_user=AuthUserResponse(email="manager@example.com"),
        service=Service(),
    )

    assert captured["locale"] == "en"
    assert response.answer == "English answer."
    assert response.sources[0].title == "原始来源标题"

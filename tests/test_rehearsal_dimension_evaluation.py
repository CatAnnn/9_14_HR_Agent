from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError

from backend.config.settings import get_settings
from backend.schemas.conversation import ConversationTurn
from backend.schemas.rehearsal_dimensions import RehearsalDimensionEvaluation
from backend.schemas.state import (
    REHEARSAL_DIMENSION_COVERAGE_VERSION,
    RehearsalRuntimeContext,
    SessionState,
)
from backend.services.rehearsal_dimension_evaluation_service import (
    RehearsalDimensionEvaluationService,
)
from backend.services.rehearsal_service import RehearsalService


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    @asynccontextmanager
    async def slot(self, **kwargs):
        self.calls.append(kwargs)
        yield


class _LLM:
    def __init__(self, output=None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def ainvoke_structured_single(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.output


def test_dimension_schema_deduplicates_and_uses_canonical_order() -> None:
    output = RehearsalDimensionEvaluation(
        covered_dimensions=["plan", "start", "plan", "emotion"],
    )

    assert output.covered_dimensions == ["start", "emotion", "plan"]
    repeated = RehearsalDimensionEvaluation(
        covered_dimensions=[
            "plan",
            "start",
            "plan",
            "emotion",
            "plan",
            "start",
        ],
    )
    assert repeated.covered_dimensions == ["start", "emotion", "plan"]
    with pytest.raises(ValidationError):
        RehearsalDimensionEvaluation(covered_dimensions=["unknown"])


@pytest.mark.asyncio
async def test_evaluator_uses_only_manager_messages_for_coverage() -> None:
    settings = get_settings()
    llm = _LLM(
        output=RehearsalDimensionEvaluation(
            covered_dimensions=["start", "plan"],
        )
    )
    scheduler = _Scheduler()
    service = RehearsalDimensionEvaluationService(
        settings=settings,
        llm_service=llm,
        model_scheduler=scheduler,
    )
    manager_message = "我们先明确下一步的时间节点。"
    state = SessionState(
        session_id="dimension-session",
        rehearsal_context=RehearsalRuntimeContext(
            covered_dimensions=["emotion"],
        ),
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text="这个季度目标是完成三个关键交付。",
            ),
            ConversationTurn(
                turn_index=2,
                speaker="employee",
                text="我现在很焦虑，也希望公司认可我的成长诉求。",
            ),
        ],
    )

    result = await service.evaluate_manager_turn(state, manager_message)

    assert result == ["start", "plan"]
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["schema"] is RehearsalDimensionEvaluation
    assert call["task_name"] == "rehearsal_dimensions"
    assert call["structured_transport"] == "json_schema"
    assert call["json_schema_strict"] is False
    assert call["model"] == settings.model_for_task("rehearsal_dimensions")
    assert call["temperature"] == 0
    assert call["max_tokens"] == 256
    assert "tools" not in call
    assert "tool_choice" not in call
    assert call["prompt"].count(manager_message) == 1
    assert "这个季度目标是完成三个关键交付。" in call["prompt"]
    assert "我现在很焦虑，也希望公司认可我的成长诉求。" not in call["prompt"]
    assert "previous_covered_dimensions" not in call["prompt"]
    assert "只评估 previous_manager_messages" in call["prompt"]
    assert "不是只做关键词匹配" in call["prompt"]
    assert "start（开场定调）" in call["prompt"]
    assert "requirement（产出与标准）" in call["prompt"]
    assert "plan（发展计划）" in call["prompt"]
    assert len(scheduler.calls) == 1
    assert scheduler.calls[0]["category"] == "interactive"


@pytest.mark.asyncio
async def test_verified_manager_coverage_accumulates_beyond_prompt_window() -> None:
    llm = _LLM(
        output=RehearsalDimensionEvaluation(
            covered_dimensions=["plan"],
        )
    )
    service = RehearsalDimensionEvaluationService(
        settings=get_settings(),
        llm_service=llm,
        model_scheduler=_Scheduler(),
    )
    state = SessionState(
        session_id="dimension-long-session",
        rehearsal_context=RehearsalRuntimeContext(
            covered_dimensions=["start"],
            dimension_coverage_version=REHEARSAL_DIMENSION_COVERAGE_VERSION,
        ),
        conversation=[
            ConversationTurn(
                turn_index=index,
                speaker="manager",
                text=f"[manager-history-{index:03d}]",
            )
            for index in range(1, 14)
        ],
    )

    result = await service.evaluate_manager_turn(
        state,
        "我们明确下一步。",
    )

    assert result == ["start", "plan"]
    prompt = llm.calls[0]["prompt"]
    assert "[manager-history-001]" not in prompt
    assert "[manager-history-003]" in prompt
    assert "[manager-history-013]" in prompt


@pytest.mark.asyncio
async def test_evaluator_failure_returns_no_guess_or_keyword_fallback() -> None:
    service = RehearsalDimensionEvaluationService(
        settings=get_settings(),
        llm_service=_LLM(error=RuntimeError("gateway unavailable")),
        model_scheduler=_Scheduler(),
    )
    state = SessionState(
        session_id="dimension-failure",
        rehearsal_context=RehearsalRuntimeContext(
            covered_dimensions=["requirement"],
        ),
    )

    result = await service.evaluate_manager_turn(
        state,
        "绩效、情绪、诉求、下一步计划都只是关键词。",
    )

    assert result is None
    assert state.rehearsal_context.covered_dimensions == ["requirement"]


def test_dimension_coverage_is_persisted_in_session_state() -> None:
    state = SessionState(
        session_id="dimension-persistence",
        rehearsal_context=RehearsalRuntimeContext(
            covered_dimensions=["start", "plan"],
            dimension_coverage_version=REHEARSAL_DIMENSION_COVERAGE_VERSION,
        ),
    )

    restored = SessionState.model_validate(state.model_dump(mode="json"))

    assert restored.rehearsal_context.covered_dimensions == ["start", "plan"]
    assert (
        restored.rehearsal_context.dimension_coverage_version
        == REHEARSAL_DIMENSION_COVERAGE_VERSION
    )


def test_legacy_dimension_ids_are_read_without_breaking_old_sessions() -> None:
    restored = SessionState.model_validate(
        {
            "session_id": "legacy-dimensions",
            "rehearsal_context": {
                "covered_dimensions": ["fact", "motivation", "emotion", "plan"],
                "dimension_coverage_version": 1,
            },
        }
    )

    assert restored.rehearsal_context.covered_dimensions == [
        "start",
        "requirement",
        "emotion",
        "plan",
    ]
    assert restored.rehearsal_context.dimension_coverage_version == 1


@pytest.mark.asyncio
async def test_successful_evaluation_marks_manager_only_coverage() -> None:
    async def complete() -> list[str]:
        return ["start"]

    state = SessionState(
        session_id="dimension-migration-success",
        rehearsal_context=RehearsalRuntimeContext(
            covered_dimensions=["emotion"],
        ),
    )
    service = RehearsalService()

    changed = await service._finish_dimension_evaluation(
        state,
        asyncio.create_task(complete()),
    )

    assert changed is True
    assert state.rehearsal_context.covered_dimensions == ["start"]
    assert (
        state.rehearsal_context.dimension_coverage_version
        == REHEARSAL_DIMENSION_COVERAGE_VERSION
    )


@pytest.mark.asyncio
async def test_unexpected_evaluator_error_preserves_core_rehearsal_state() -> None:
    async def fail() -> list[str]:
        raise RuntimeError("unexpected evaluator failure")

    state = SessionState(
        session_id="dimension-task-failure",
        rehearsal_context=RehearsalRuntimeContext(
            covered_dimensions=["start"],
            dimension_coverage_version=REHEARSAL_DIMENSION_COVERAGE_VERSION,
        ),
    )
    service = RehearsalService()
    task = asyncio.create_task(fail())

    changed = await service._finish_dimension_evaluation(state, task)

    assert changed is True
    assert state.rehearsal_context.covered_dimensions == ["start"]
    assert state.warnings == ["对话维度模型评估暂不可用，本轮保留已有状态。"]


@pytest.mark.asyncio
async def test_failed_legacy_evaluation_hides_unverified_coverage() -> None:
    async def fail() -> list[str]:
        raise RuntimeError("unexpected evaluator failure")

    state = SessionState(
        session_id="dimension-migration-failure",
        rehearsal_context=RehearsalRuntimeContext(
            covered_dimensions=["emotion"],
        ),
    )
    service = RehearsalService()

    changed = await service._finish_dimension_evaluation(
        state,
        asyncio.create_task(fail()),
    )

    assert changed is True
    assert state.rehearsal_context.covered_dimensions == []
    assert state.rehearsal_context.dimension_coverage_version == 0
    assert state.warnings == [
        "对话维度模型评估暂不可用，已隐藏旧口径统计，后续成功评估后恢复。"
    ]

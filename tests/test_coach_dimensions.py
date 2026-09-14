from __future__ import annotations

import pytest

from backend.agents.coach_agent.dimension_evaluator import (
    COACH_DIMENSION_SPECS,
    DimensionEvaluator,
    validate_coach_runtime,
)
from backend.business_config.loader import get_config_loader
from backend.exceptions.llm_errors import StructuredOutputError
from backend.schemas.intent import IntentConfig, IntentResult
from backend.schemas.profile import EmployeeProfile
from backend.schemas.conversation import ConversationTurn
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.state import SessionState
from backend.schemas.task import CoachDimensionModelOutput


def test_coach_runtime_validates_all_dimension_configs_and_templates():
    assert validate_coach_runtime().startswith("coach-")
    assert [spec.task_id for spec in COACH_DIMENSION_SPECS] == [
        "opening_evaluation",
        "emotion_evaluation",
        "output_expectations_evaluation",
        "development_plan_evaluation",
    ]


def test_plan_intent_mapping_uses_user_selection_ids():
    loader = get_config_loader()
    dimension = loader.coach_config("plan.yaml")["dimension"]
    intent_ids = set(loader.intents())

    assert dimension["intent_selection_mapping"] == {
        intent_id: intent_id for intent_id in intent_ids
    }
    assert set(dimension["intent_focus"]) == intent_ids


def test_plan_context_includes_g9_career_elements_and_full_intent_config():
    spec = next(
        item
        for item in COACH_DIMENSION_SPECS
        if item.task_id == "development_plan_evaluation"
    )
    state = SessionState(
        session_id="coach-career-elements",
        employee_profile=EmployeeProfile(
            level="G9",
            current_career_elements=["Cross Function"],
        ),
        intent=IntentResult(
            intent_id="development",
            confidence=1.0,
            reason="test",
            config=IntentConfig(id="development", name="发展型反馈"),
        ),
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text="我们把下一阶段的重点和跟进方式一起确认下来。",
            )
        ],
    )

    prepared = DimensionEvaluator(
        spec,
        llm_service=object(),
    ).prepare_evaluation(state)

    assert prepared.invocation is not None
    context = prepared.invocation.context
    assert context.intent_config is not None
    assert context.intent_config.id == "development"
    assert context.career_elements_applicable is True
    assert context.current_career_elements == ["Cross Function"]
    assert "context.intent_config" not in prepared.invocation.prompt
    assert "context.intent.config" not in prepared.invocation.prompt
    assert "context.intent_id" in prepared.invocation.prompt
    assert "context.dimension_config.intent_focus" in prepared.invocation.prompt


def test_coach_prompt_deduplication_preserves_complete_citation_chunk_pool():
    shared_context = "仅在模型 Prompt 中保留一次的完整生成上下文。"
    chunks = [
        RetrievedChunk(
            chunk_id="dedup-primary-chunk",
            source_id="performance/performance.md",
            title="Performance",
            scope="performance",
            text="第一条原始命中证据",
            score=0.9,
            metadata={
                "generation_context_override": shared_context,
                "parent_context": "第一条可供引用校验的完整来源正文",
            },
        ),
        RetrievedChunk(
            chunk_id="dedup-secondary-hidden-chunk",
            source_id="performance/performance.md",
            title="Performance",
            scope="performance",
            text="第二条原始命中证据",
            score=0.8,
            metadata={
                "generation_context_override": shared_context,
                "parent_context": "第二条可供引用校验的完整来源正文",
            },
        ),
    ]
    state = SessionState(
        session_id="coach-prompt-citation-separation",
        intent=IntentResult(
            intent_id="development",
            confidence=1.0,
            reason="test",
            config=IntentConfig(id="development", name="发展型反馈"),
        ),
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text="我们先核对当前事实。",
            )
        ],
    )

    prepared = DimensionEvaluator(
        COACH_DIMENSION_SPECS[0],
        llm_service=object(),
    ).prepare_evaluation(state, chunks)

    assert prepared.invocation is not None
    invocation = prepared.invocation
    assert [
        chunk.chunk_id for chunk in invocation.context.retrieved_chunks
    ] == ["dedup-primary-chunk"]
    assert "dedup-primary-chunk" in invocation.prompt
    assert "dedup-secondary-hidden-chunk" not in invocation.prompt
    assert [chunk.chunk_id for chunk in invocation.citation_chunks] == [
        "dedup-primary-chunk",
        "dedup-secondary-hidden-chunk",
    ]
    assert invocation.citation_chunks[1].metadata["parent_context"] == (
        "第二条可供引用校验的完整来源正文"
    )


def test_all_coach_dimensions_share_semantic_judgement_policy():
    shared_policy = (
        "不得在内部使用固定关键词、固定短语、正则命中或简单字符串包含关系代替语义识别"
    )

    for spec in COACH_DIMENSION_SPECS:
        prompt = DimensionEvaluator(spec, llm_service=object()).prompts.render(
            spec.prompt_template,
            context_json="{}",
        )
        assert shared_policy in prompt


def test_all_coach_dimensions_use_one_shared_submission_contract():
    for spec in COACH_DIMENSION_SPECS:
        prompt = DimensionEvaluator(spec, llm_service=object()).prompts.render(
            spec.prompt_template,
            context_json="{}",
        )
        assert prompt.count("【通用提交检查】") == 1
        assert prompt.count("【只读上下文 JSON】") == 1
        assert "【提交响应前静默检查】" not in prompt
        assert "【生成前检查】" not in prompt
        assert "【最终约束】" not in prompt


def test_output_expectations_does_not_require_an_employee_turn_before_model_judgement():
    spec = next(
        item
        for item in COACH_DIMENSION_SPECS
        if item.task_id == "output_expectations_evaluation"
    )

    assert spec.requires_employee is False


@pytest.mark.asyncio
async def test_dimension_evaluator_uses_backend_derived_dimension_score():
    spec = COACH_DIMENSION_SPECS[0]
    config = get_config_loader().coach_config(spec.config_filename)
    calls = []

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            return CoachDimensionModelOutput(
                status="success",
                score=4,
                summary="开场结论清楚，但理解确认不足。",
                strengths=[
                    "Manager 清楚说明了本次沟通的核心结论。",
                    "Manager 及时把对话聚焦到需要共同核对的事实。",
                ],
                basis="有明确结论，缺少开放式理解确认。",
                issues=[],
            )

    state = SessionState(
        session_id="dimension-test",
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text="今天我们直接讨论本周期绩效结果。",
            )
        ],
    )
    result = await DimensionEvaluator(spec, llm_service=LLM()).evaluate(state)

    assert result.score == 4
    assert result.dimension_scores[0].basis
    assert calls[0]["task_name"] == "coach_evaluator"
    assert calls[0]["schema"] is CoachDimensionModelOutput
    assert calls[0]["structured_transport"] == "json_schema"
    assert calls[0]["json_schema_strict"] is False



@pytest.mark.asyncio
async def test_output_expectations_uses_semantic_applicability_instead_of_markers():
    spec = next(
        item
        for item in COACH_DIMENSION_SPECS
        if item.task_id == "output_expectations_evaluation"
    )
    calls = []

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            return CoachDimensionModelOutput(
                status="success",
                score=4,
                summary="管理者说明了结果差距和职级要求，但过程行为标准仍不够完整。",
                strengths=[
                    "Manager 使用具体交付数据说明了当前结果。",
                    "Manager 将结果与岗位要求关联，帮助 Employee 理解评价依据。",
                ],
                basis="管理者使用交付数据、G9 要求和历史表现回应了员工的预期落差。",
                issues=[],
            )

    result = await DimensionEvaluator(spec, llm_service=LLM()).evaluate(
        SessionState(
            session_id="output-applicability",
            conversation=[
                ConversationTurn(
                    turn_index=1,
                    speaker="employee",
                    text="这和我年初以为会拿到的结果不是一回事，我得先消化一下。",
                ),
                ConversationTurn(
                    turn_index=2,
                    speaker="manager",
                    text="85%的岗位已经完成，但高端岗位长期交付不足，目前只符合G9基本要求。",
                ),
            ],
        )
    )

    assert result.status == "success"
    assert result.score == 4
    assert len(calls) == 1
    assert "这和我年初以为会拿到的结果不是一回事" in calls[0]["prompt"]
    assert "本维度不以 Employee 先出现情绪、质疑、防御或反驳为前提" in calls[0]["prompt"]


@pytest.mark.asyncio
async def test_output_expectations_allows_model_to_judge_manager_only_evidence():
    spec = next(
        item
        for item in COACH_DIMENSION_SPECS
        if item.task_id == "output_expectations_evaluation"
    )
    calls = []

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            return CoachDimensionModelOutput(
                status="success",
                score=3,
                summary="管理者说明了结果与岗位要求，但证据覆盖仍不完整。",
                strengths=[
                    "Manager 主动说明了当前结果与岗位要求之间的关系。",
                    "Manager 将讨论聚焦在可继续核对的绩效事实。",
                ],
                basis="模型依据 Manager 的完整表达识别到产出与标准讨论。",
                issues=[
                    {
                        "turn_index": 1,
                        "quote": "大部分岗位已经交付",
                        "diagnostic_dimension_id": "result_breakthrough",
                        "problem": "只说明了当前交付情况，没有形成可验证的历史突破判断。",
                        "impact": "员工难以理解当前结果与上一周期相比是否取得实质进展。",
                        "suggestion": "大部分岗位已经交付；我们再把本周期与上一周期的关键结果放在一起核对，明确哪些方面确实取得了突破。",
                        "reason": "补充同口径的历史比较，可以让结果判断更完整、更可验证。",
                    }
                ],
            )

    result = await DimensionEvaluator(spec, llm_service=LLM()).evaluate(
        SessionState(
            session_id="output-manager-only",
            conversation=[
                ConversationTurn(
                    turn_index=1,
                    speaker="manager",
                    text="大部分岗位已经交付，但复杂岗位长期没有达到这个层级应有的结果。",
                )
            ],
        )
    )

    assert result.status == "success"
    assert result.score == 3
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dimension_evaluator_rejects_low_score_without_evidence_backed_issue():
    spec = next(
        item
        for item in COACH_DIMENSION_SPECS
        if item.task_id == "emotion_evaluation"
    )

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput(
                status="success",
                score=2,
                summary="员工表达明显顾虑后，Manager 直接转回结论，未完成情绪承接。",
                strengths=[],
                basis="Manager 忽略员工顾虑并继续推进自己的结论，因此评为2分。",
                issues=[
                    {
                        "turn_index": 2,
                        "quote": "这句原话并不存在。",
                        "diagnostic_dimension_id": "joint_exploration",
                        "problem": "Manager 没有继续探索员工顾虑。",
                        "impact": "对话难以形成有效承接。",
                        "suggestion": "我先听听你最难接受的是哪一部分。",
                        "reason": "继续探索可以明确员工的真实关切。",
                    }
                ],
            )

    state = SessionState(
        session_id="low-score-empty-issues",
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="employee",
                text="这个评价和我的预期差距很大，我现在很难接受。",
            ),
            ConversationTurn(
                turn_index=2,
                speaker="manager",
                text="先不谈这个，我们继续说下一步计划。",
            ),
        ],
    )

    with pytest.raises(StructuredOutputError) as exc_info:
        await DimensionEvaluator(spec, llm_service=LLM()).evaluate(state)

    assert exc_info.value.code == "business_validation"
    assert "must return at least one Manager-evidence-backed issue" in str(
        exc_info.value
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [4, 5])
async def test_dimension_evaluator_keeps_high_score_after_invalid_issue_is_filtered(
    score,
):
    spec = next(
        item
        for item in COACH_DIMENSION_SPECS
        if item.task_id == "emotion_evaluation"
    )

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput(
                status="success",
                score=score,
                summary="Manager 已完成主要情绪承接。",
                strengths=["Manager 给 Employee 留出了表达顾虑的空间。"],
                basis="Manager 的回应整体符合本维度要求。",
                issues=[
                    {
                        "turn_index": 2,
                        "quote": "这句原话并不存在。",
                        "diagnostic_dimension_id": "joint_exploration",
                        "problem": "这一问题没有可绑定证据。",
                        "impact": "不应进入最终报告。",
                        "suggestion": "我先听听你的具体顾虑。",
                        "reason": "用于验证局部过滤。",
                    }
                ],
            )

    result = await DimensionEvaluator(spec, llm_service=LLM()).evaluate(
        SessionState(
            session_id=f"high-score-filtered-issue-{score}",
            conversation=[
                ConversationTurn(
                    turn_index=1,
                    speaker="employee",
                    text="这个结果和我的预期有差距。",
                ),
                ConversationTurn(
                    turn_index=2,
                    speaker="manager",
                    text="我理解你有落差，我们先把你最关心的部分说清楚。",
                ),
            ],
        )
    )

    assert result.score == score
    assert result.improvement_points == []

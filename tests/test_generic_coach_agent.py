from __future__ import annotations

import json

import pytest

from backend.agents.coach_agent.generic_agent import GenericCoachAgent
from backend.business_config.loader import get_config_loader
from backend.exceptions.llm_errors import StructuredOutputError
from backend.schemas.conversation import ConversationTurn
from backend.schemas.prompt_context import CoachPromptContext
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.task import CoachDimensionModelOutput


async def _run(
    agent: GenericCoachAgent,
    *,
    task_id: str = "opening_evaluation",
    task_name: str = "开场定调与绩效结果对齐评估",
    prompt_template: str = "coach/start.jinja2",
    dimension_config: dict | None = None,
    retry: bool = False,
    retry_model: str | None = None,
    retrieved_chunks: list[dict] | None = None,
    profile: dict | None = None,
    intent: dict | None = None,
    conversation: list[dict] | None = None,
):
    return await agent.run_llm_task(
        task_id=task_id,
        task_name=task_name,
        prompt_template=prompt_template,
        task_model_name="coach_evaluator",
        retry=retry,
        retry_model=retry_model,
        profile=profile or {},
        intent=intent or {},
        conversation=conversation or [
            {
                "turn_index": 1,
                "speaker": "manager",
                "text": "我们先看事实。",
            }
        ],
        opening_dimension_config=dimension_config or {},
        retrieved_chunks=retrieved_chunks or [],
    )


def test_legacy_prompt_vars_preserve_intent_and_career_elements_context():
    context = GenericCoachAgent._context_from_prompt_vars(
        {
            "profile": {
                "level": "G9",
                "current_career_elements": ["Cross Function"],
            },
            "intent": {
                "id": "development",
                "name": "发展型反馈",
            },
            "performance_context": "已确认的当前表现",
            "conversation": [],
        }
    )

    assert context.intent_id == "development"
    assert context.intent_config is not None
    assert context.intent_config.id == "development"
    assert context.performance_context == "已确认的当前表现"
    assert context.career_elements_applicable is True
    assert context.current_career_elements == ["Cross Function"]


def _valid_payload() -> dict:
    return {
        "status": "success",
        "score": 3,
        "summary": "整体表达较清晰，但需要补充事实证据。",
        "strengths": [
            "Manager 主动把讨论拉回到可核对的事实，帮助双方聚焦具体议题。",
            "Manager 的表达直接而清楚，为后续澄清绩效差距建立了基础。",
        ],
        "basis": "Manager 提出了事实议题，但缺少完整的理解核对。",
        "issues": [
            {
                "turn_index": 1,
                "quote": "我们先看事实。",
                "diagnostic_dimension_id": "communication_tone",
                "problem": "没有先说明沟通目的。",
                "impact": "员工可能难以理解讨论边界。",
                "suggestion": "我想先说明今天沟通的目的，再一起核对事实。",
                "reason": "先交代目的可以降低突兀感。",
            }
        ],
    }


@pytest.mark.asyncio
async def test_generic_coach_agent_injects_fixed_fields_after_model_validation():
    calls = []

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            return CoachDimensionModelOutput.model_validate(_valid_payload())

    result = await _run(GenericCoachAgent(llm_service=LLM()))

    assert result.task_id == "opening_evaluation"
    assert result.task_name == "开场定调与绩效结果对齐评估"
    assert result.strengths == _valid_payload()["strengths"]
    assert result.evidence[0].turn_index == 1
    assert result.improvement_points == [
        "没有先说明沟通目的（影响：员工可能难以理解讨论边界）"
    ]
    assert result.better_phrases[0].original == "我们先看事实。"
    assert calls[0]["model"] is None
    assert calls[0]["schema"] is CoachDimensionModelOutput
    assert calls[0]["structured_transport"] == "json_schema"
    assert calls[0]["json_schema_strict"] is False
    assert callable(calls[0]["payload_normalizer"])
    assert "tools" not in calls[0]
    assert "tool_choice" not in calls[0]
    context_json = calls[0]["prompt"].split("context=", 1)[1].split(
        "\n【上下文结束】",
        1,
    )[0]
    assert json.loads(context_json)["conversation"] == [
        {
            "turn_index": 1,
            "speaker": "manager",
            "text": "我们先看事实。",
        }
    ]


def test_generic_coach_agent_normalizes_inapplicable_insufficient_information_fields():
    payload = _valid_payload()
    payload.update(
        {
            "status": "insufficient_information",
            "score": 3,
            "summary": "当前对话信息不足，无法形成可靠评分。",
            "basis": "经理发言存在明显转写噪声，尚无可核验的沟通内容。",
        }
    )

    normalized, repair_steps = GenericCoachAgent._normalize_model_payload(payload)
    result = CoachDimensionModelOutput.model_validate(normalized)

    assert result.status == "insufficient_information"
    assert result.score is None
    assert result.strengths == []
    assert result.issues == []
    assert repair_steps == [
        "cleared_insufficient_information_score",
        "cleared_insufficient_information_strengths",
        "cleared_insufficient_information_issues",
    ]


def test_generic_coach_agent_downgrades_scoreless_success_without_inventing_score():
    payload = _valid_payload()
    payload["score"] = None

    normalized, repair_steps = GenericCoachAgent._normalize_model_payload(payload)
    result = CoachDimensionModelOutput.model_validate(normalized)

    assert result.status == "insufficient_information"
    assert result.score is None
    assert result.strengths == []
    assert result.issues == []
    assert repair_steps == [
        "downgraded_scoreless_success_to_insufficient_information",
        "cleared_insufficient_information_strengths",
        "cleared_insufficient_information_issues",
    ]


def test_generic_coach_agent_does_not_normalize_success_payload():
    payload = _valid_payload()

    normalized, repair_steps = GenericCoachAgent._normalize_model_payload(payload)

    assert normalized == payload
    assert repair_steps == []


@pytest.mark.asyncio
async def test_generic_coach_agent_fills_fixed_dimension_fields_and_level():
    config = get_config_loader().coach_config("start.yaml")
    dimension = config["dimension"]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
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

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        dimension_config=config,
    )

    assert result.task_id == "opening_evaluation"
    assert result.task_name == "开场定调与绩效结果对齐评估"
    assert len(result.dimension_scores) == 1
    assert result.dimension_scores[0].id == dimension["id"]
    assert result.dimension_scores[0].name == dimension["name"]
    assert result.dimension_scores[0].score == 4
    assert result.dimension_scores[0].level == dimension["score_scale"][4]["label"]
    assert result.dimension_scores[0].basis == "有明确结论，缺少开放式理解确认。"


@pytest.mark.asyncio
async def test_generic_coach_agent_drops_issue_without_exact_manager_evidence():
    payload = _valid_payload()
    payload["issues"][0]["quote"] = "这句话没有出现。"

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(GenericCoachAgent(llm_service=LLM()))

    assert result.score == 3
    assert result.improvement_points == []
    assert result.better_phrases == []


@pytest.mark.asyncio
async def test_generic_coach_agent_drops_only_the_unverifiable_issue():
    payload = _valid_payload()
    payload["issues"] = [
        {
            **payload["issues"][0],
            "quote": "这句 Manager 从未说过。",
        },
        {
            **payload["issues"][0],
            "diagnostic_dimension_id": "performance_result_alignment",
            "quote": "我们先看事实。",
            "problem": "没有说明要核对哪些绩效事实。",
            "suggestion": "我们先核对关键目标、实际结果和具体差距。",
        },
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(GenericCoachAgent(llm_service=LLM()))

    assert result.improvement_points == [
        "没有说明要核对哪些绩效事实（影响：员工可能难以理解讨论边界）"
    ]
    assert [item.quote for item in result.evidence] == ["我们先看事实。"]
    assert [item.original for item in result.better_phrases] == ["我们先看事实。"]


@pytest.mark.asyncio
async def test_generic_coach_agent_rebinds_typographic_quote_to_exact_manager_text():
    payload = _valid_payload()
    payload["issues"][0]["quote"] = (
        "Ｇ９肯定要有一些不一样的东西，要创新、要有突破"
    )

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        conversation=[
            {
                "turn_index": 1,
                "speaker": "manager",
                "text": "G9肯定要有一些不一样的东西，要创新，要有突破。",
            }
        ],
    )

    exact_quote = "G9肯定要有一些不一样的东西，要创新，要有突破"
    assert result.evidence[0].quote == exact_quote
    assert result.better_phrases[0].original == exact_quote


@pytest.mark.asyncio
async def test_generic_coach_agent_rebinds_narrow_asr_filler_quote_drift():
    payload = _valid_payload()
    payload["issues"][0]["quote"] = "整体招聘交付我觉得还可以常规岗位基本上也都完成了"

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        conversation=[
            {
                "turn_index": 1,
                "speaker": "manager",
                "text": "整体招聘交付我觉得，嗯，还可以，常规岗位基本上也都完成了。",
            }
        ],
    )

    exact_quote = "整体招聘交付我觉得，嗯，还可以，常规岗位基本上也都完成了"
    assert result.evidence[0].quote == exact_quote
    assert result.better_phrases[0].original == exact_quote


@pytest.mark.asyncio
async def test_generic_coach_agent_rebinds_filler_added_only_by_model_quote():
    payload = _valid_payload()
    payload["issues"][0]["quote"] = "整体招聘交付我觉得，呃，还可以，常规岗位基本上也都完成了"

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        conversation=[
            {
                "turn_index": 1,
                "speaker": "manager",
                "text": "整体招聘交付我觉得还可以，常规岗位基本上也都完成了。",
            }
        ],
    )

    exact_quote = "整体招聘交付我觉得还可以，常规岗位基本上也都完成了"
    assert result.evidence[0].quote == exact_quote
    assert result.better_phrases[0].original == exact_quote


@pytest.mark.asyncio
async def test_generic_coach_agent_drops_semantic_quote_rewrite():
    payload = _valid_payload()
    payload["issues"][0]["quote"] = "高端岗位并不存在明显缺口"

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        conversation=[
            {
                "turn_index": 1,
                "speaker": "manager",
                "text": "高端岗位还是存在比较明显的缺口。",
            }
        ],
    )

    assert result.improvement_points == []


def test_evidence_normalization_preserves_business_symbols_and_word_characters():
    normalized, _ = GenericCoachAgent._normalized_evidence_text(
        "嗯，金额额外增加20%，区间为1.5-2.0。"
    )

    assert normalized == "金额额外增加20%区间为1.5-2.0"


@pytest.mark.asyncio
async def test_generic_coach_agent_drops_quote_that_drops_business_symbol():
    payload = _valid_payload()
    payload["issues"][0]["quote"] = "明年高端岗位招聘周期需要提升20的目标"

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        conversation=[
            {
                "turn_index": 1,
                "speaker": "manager",
                "text": "明年高端岗位招聘周期需要提升20%的目标。",
            }
        ],
    )

    assert result.improvement_points == []


@pytest.mark.asyncio
async def test_generic_coach_agent_deduplicates_shared_evidence_without_dropping_issues():
    payload = _valid_payload()
    payload["issues"].append(
        {
            "turn_index": 1,
            "quote": "我们先看事实。",
            "diagnostic_dimension_id": "performance_result_alignment",
            "problem": "没有说明需要核对哪些具体事实。",
            "impact": "员工无法提前组织对应证据。",
            "suggestion": "我们先核对目标结果、关键案例和具体影响。",
            "reason": "明确核对范围可以提高讨论效率。",
        }
    )

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(GenericCoachAgent(llm_service=LLM()))

    assert len(result.evidence) == 1
    assert result.evidence[0].quote == "我们先看事实。"
    assert len(result.improvement_points) == 2
    assert len(result.better_phrases) == 2
    assert result.improvement_points[1].startswith("没有说明需要核对哪些具体事实")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_id", "task_name", "prompt_template", "diagnostic_dimension_id"),
    [
        (
            "opening_evaluation",
            "开场定调与绩效结果对齐评估",
            "coach/start.jinja2",
            "performance_result_alignment",
        ),
        (
            "emotion_evaluation",
            "情绪承接",
            "coach/emotion.jinja2",
            "joint_exploration",
        ),
        (
            "output_expectations_evaluation",
            "回归产出与标准",
            "coach/requirement.jinja2",
            "result_breakthrough",
        ),
    ],
)
async def test_fixed_issue_preserves_sparse_diagnostic_dimension_id(
    task_id,
    task_name,
    prompt_template,
    diagnostic_dimension_id,
):
    payload = _valid_payload()
    payload["issues"][0]["diagnostic_dimension_id"] = diagnostic_dimension_id

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id=task_id,
        task_name=task_name,
        prompt_template=prompt_template,
    )

    assert len(result.better_phrases) == 1
    assert (
        result.better_phrases[0].diagnostic_dimension_id
        == diagnostic_dimension_id
    )


@pytest.mark.asyncio
async def test_emotion_issue_accepts_single_fallback_without_dimension_id():
    payload = _valid_payload()
    payload["issues"][0]["diagnostic_dimension_id"] = None

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="emotion_evaluation",
        task_name="情绪承接",
        prompt_template="coach/emotion.jinja2",
    )

    assert len(result.better_phrases) == 1
    assert result.better_phrases[0].diagnostic_dimension_id is None
    assert result.better_phrases[0].suggestion == payload["issues"][0]["suggestion"]


@pytest.mark.asyncio
async def test_emotion_issue_accepts_fallback_after_fixed_dimensions():
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    payload["issues"] = [
        {
            **first_issue,
            "diagnostic_dimension_id": "listening_and_emotion_understanding",
        },
        {
            **first_issue,
            "diagnostic_dimension_id": None,
            "problem": "还存在一个无法准确归入固定维度的情绪沟通问题。",
            "suggestion": "我想先停一下，确认刚才的沟通方式有没有让你更难表达。",
        },
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="emotion_evaluation",
        task_name="情绪承接",
        prompt_template="coach/emotion.jinja2",
    )

    assert [
        phrase.diagnostic_dimension_id for phrase in result.better_phrases
    ] == ["listening_and_emotion_understanding", None]


@pytest.mark.asyncio
async def test_emotion_issue_drops_unknown_diagnostic_dimension_id_only():
    diagnostic_dimension_id = "unknown_emotion_dimension"
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    payload["issues"] = [
        {**first_issue, "diagnostic_dimension_id": "joint_exploration"},
        {
            **first_issue,
            "diagnostic_dimension_id": diagnostic_dimension_id,
            "problem": "未知维度问题应被局部删除。",
        },
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="emotion_evaluation",
        task_name="情绪承接",
        prompt_template="coach/emotion.jinja2",
    )

    assert len(result.improvement_points) == 1
    assert result.better_phrases[0].diagnostic_dimension_id == "joint_exploration"


@pytest.mark.asyncio
async def test_emotion_issue_moves_fallback_after_fixed_dimensions():
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    payload["issues"] = [
        {**first_issue, "diagnostic_dimension_id": None},
        {
            **first_issue,
            "diagnostic_dimension_id": "joint_exploration",
            "problem": "固定维度问题出现在兜底问题之后。",
        },
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="emotion_evaluation",
        task_name="情绪承接",
        prompt_template="coach/emotion.jinja2",
    )

    assert [
        phrase.diagnostic_dimension_id for phrase in result.better_phrases
    ] == ["joint_exploration", None]


@pytest.mark.asyncio
async def test_emotion_issue_keeps_only_one_fallback():
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    payload["issues"] = [
        {**first_issue, "diagnostic_dimension_id": None},
        {
            **first_issue,
            "diagnostic_dimension_id": None,
            "problem": "第二个未分类问题不应被接受。",
        },
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="emotion_evaluation",
        task_name="情绪承接",
        prompt_template="coach/emotion.jinja2",
    )

    assert len(result.improvement_points) == 1
    assert result.better_phrases[0].diagnostic_dimension_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnostic_dimension_id",
    [None, "job_level_requirements", "unknown_opening_dimension"],
)
async def test_opening_issue_drops_missing_or_unknown_diagnostic_dimension_id(
    diagnostic_dimension_id,
):
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    payload["issues"] = [
        first_issue,
        {
            **first_issue,
            "diagnostic_dimension_id": diagnostic_dimension_id,
            "problem": "这一无效维度问题不应影响合法问题。",
        },
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(GenericCoachAgent(llm_service=LLM()))

    assert len(result.improvement_points) == 1
    assert result.better_phrases[0].diagnostic_dimension_id == "communication_tone"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnostic_dimension_id",
    [None, "unknown_development_dimension", "career_elements"],
)
async def test_development_plan_issue_drops_missing_unknown_or_inapplicable_dimension(
    diagnostic_dimension_id,
):
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    payload["issues"] = [
        {**first_issue, "diagnostic_dimension_id": "job_level_requirements"},
        {
            **first_issue,
            "diagnostic_dimension_id": diagnostic_dimension_id,
            "problem": "这一无效维度问题不应影响合法问题。",
        },
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="development_plan_evaluation",
        task_name="总结与差异化发展计划评估",
        prompt_template="coach/plan.jinja2",
    )

    assert len(result.improvement_points) == 1
    assert (
        result.better_phrases[0].diagnostic_dimension_id
        == "job_level_requirements"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("diagnostic_dimension_id", "profile", "intent"),
    [
        ("job_level_requirements", {}, {}),
        (
            "career_elements",
            {"level": "G9"},
            {"id": "development", "name": "发展型反馈"},
        ),
    ],
)
async def test_development_plan_issue_accepts_applicable_fixed_dimension(
    diagnostic_dimension_id,
    profile,
    intent,
):
    payload = _valid_payload()
    payload["issues"][0]["diagnostic_dimension_id"] = diagnostic_dimension_id

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="development_plan_evaluation",
        task_name="总结与差异化发展计划评估",
        prompt_template="coach/plan.jinja2",
        profile=profile,
        intent=intent,
    )

    assert result.better_phrases[0].diagnostic_dimension_id == diagnostic_dimension_id


@pytest.mark.asyncio
async def test_fixed_diagnostic_issues_are_stably_ordered_for_display():
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    payload["issues"] = [
        {**first_issue, "diagnostic_dimension_id": "joint_exploration"},
        {
            **first_issue,
            "diagnostic_dimension_id": "listening_and_emotion_understanding",
            "problem": "没有先承接员工已经表达的担忧。",
        },
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="emotion_evaluation",
        task_name="情绪承接",
        prompt_template="coach/emotion.jinja2",
    )

    assert [
        phrase.diagnostic_dimension_id for phrase in result.better_phrases
    ] == ["listening_and_emotion_understanding", "joint_exploration"]


@pytest.mark.asyncio
async def test_fixed_diagnostic_issues_allow_eight_distinct_issues_in_one_dimension():
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    payload["issues"] = [
        {
            **first_issue,
            "turn_index": index + 1,
            "quote": f"我们具体讨论第 {index + 1} 个问题。",
            "diagnostic_dimension_id": "joint_exploration",
            "problem": f"共同探索中的独立问题 {index + 1}。",
            "suggestion": f"我们先具体讨论第 {index + 1} 个问题。",
        }
        for index in range(8)
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="emotion_evaluation",
        task_name="情绪承接",
        prompt_template="coach/emotion.jinja2",
        conversation=[
            {
                "turn_index": index + 1,
                "speaker": "manager",
                "text": f"我们具体讨论第 {index + 1} 个问题。",
            }
            for index in range(8)
        ],
    )

    assert len(result.improvement_points) == 8
    assert [
        phrase.diagnostic_dimension_id for phrase in result.better_phrases
    ] == ["joint_exploration"] * 8


@pytest.mark.asyncio
async def test_fixed_diagnostic_issues_allow_reused_manager_evidence_for_distinct_problems():
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    payload["issues"] = [
        {**first_issue, "diagnostic_dimension_id": "joint_exploration"},
        {
            **first_issue,
            "diagnostic_dimension_id": "joint_exploration",
            "problem": "把同一句原话机械拆成了第二个问题。",
            "suggestion": "我们继续把这个问题拆开讨论。",
        },
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="emotion_evaluation",
        task_name="情绪承接",
        prompt_template="coach/emotion.jinja2",
    )

    assert len(result.improvement_points) == 2
    assert len(result.evidence) == 1
    assert [
        phrase.diagnostic_dimension_id for phrase in result.better_phrases
    ] == ["joint_exploration", "joint_exploration"]


@pytest.mark.asyncio
async def test_fixed_diagnostic_issues_keep_more_than_eight_issues_in_one_dimension():
    payload = _valid_payload()
    first_issue = payload["issues"][0]
    calls = []
    payload["issues"] = [
        {
            **first_issue,
            "turn_index": index + 1,
            "quote": f"我们先具体讨论第 {index} 个问题。",
            "diagnostic_dimension_id": "joint_exploration",
            "problem": f"共同探索中的独立问题 {index}。",
            "suggestion": f"我们先具体讨论第 {index} 个问题。",
        }
        for index in range(9)
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        task_id="emotion_evaluation",
        task_name="情绪承接",
        prompt_template="coach/emotion.jinja2",
        conversation=[
            {
                "turn_index": index + 1,
                "speaker": "manager",
                "text": f"我们先具体讨论第 {index} 个问题。",
            }
            for index in range(9)
        ],
    )

    assert len(result.improvement_points) == 9
    assert len(result.better_phrases) == 9
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "malformed_json",
        "schema_validation",
        "missing_output",
        "conflicting_tool_calls",
        "conflicting_content_objects",
        "conflicting_wrapped_objects",
        "conflicting_tool_arguments",
    ],
)
async def test_generic_coach_agent_leaves_retry_orchestration_to_service(error_code):
    calls = []

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            raise StructuredOutputError(error_code, "invalid output")

    with pytest.raises(StructuredOutputError) as exc_info:
        await _run(GenericCoachAgent(llm_service=LLM()))

    assert exc_info.value.code == error_code
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_generic_coach_agent_retry_mode_is_one_corrected_attempt():
    calls = []

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            return CoachDimensionModelOutput.model_validate(_valid_payload())

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        retry=True,
        retry_model="deepseek-v4-pro",
    )

    assert result.status == "success"
    assert len(calls) == 1
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["enable_thinking"] is False
    assert calls[0]["model"] == "deepseek-v4-pro"
    assert "上一次输出因结构格式、业务校验、超时或临时网关错误被拒绝" in calls[0]["prompt"]
    assert "匹配响应 Schema" in calls[0]["prompt"]
    assert "调用工具" not in calls[0]["prompt"]


@pytest.mark.asyncio
async def test_generic_coach_agent_does_not_retry_timeout():
    calls = []

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            raise StructuredOutputError("timeout", "inference timed out")

    with pytest.raises(StructuredOutputError) as exc_info:
        await _run(GenericCoachAgent(llm_service=LLM()))

    assert exc_info.value.code == "timeout"
    assert len(calls) == 1


def test_generic_coach_agent_bounds_conversation_and_rag_text_without_dropping_items():
    agent = GenericCoachAgent(llm_service=object())
    conversation = [
        ConversationTurn(turn_index=index, speaker="manager", text=str(index) * 100)
        for index in range(1, 5)
    ]
    chunks = [
        RetrievedChunk(
            chunk_id=f"chunk-{index}",
            source_id=f"source-{index}",
            title=f"title-{index}",
            scope="general",
            text="知识" * 100,
        )
        for index in range(4)
    ]

    conversation_payload = agent.conversation_payload(
        conversation,
        max_text_chars=120,
    )
    chunk_payload = agent.chunks_payload(chunks, max_text_chars=160)

    assert len(conversation_payload) == len(conversation)
    assert len(chunk_payload) == len(chunks)
    assert sum(len(item["text"]) for item in conversation_payload) <= 120
    assert sum(len(item["text"]) for item in chunk_payload) <= 160
    assert [item["chunk_id"] for item in chunk_payload] == [
        chunk.chunk_id for chunk in chunks
    ]


def test_generic_coach_agent_bounds_parent_context_around_retrieved_child_chunks():
    long_child = "长命中片段" * 12
    short_child = "短命中片段"
    chunks = [
        RetrievedChunk(
            chunk_id="long-child",
            source_id="source-long",
            title="Long",
            scope="performance",
            text=long_child,
            metadata={
                "parent_context": "前文" * 180 + long_child + "后文" * 180,
            },
        ),
        RetrievedChunk(
            chunk_id="short-child",
            source_id="source-short",
            title="Short",
            scope="culture",
            text=short_child,
            metadata={
                "parent_context": "开头" * 180 + short_child + "结尾" * 180,
            },
        ),
    ]

    payload = GenericCoachAgent.chunks_payload(chunks, max_text_chars=180)

    assert sum(len(item["text"]) for item in payload) <= 180
    assert long_child in payload[0]["text"]
    assert short_child in payload[1]["text"]
    assert payload[0]["text"] != chunks[0].metadata["parent_context"]
    assert payload[1]["text"] != chunks[1].metadata["parent_context"]


def test_generic_coach_agent_keeps_legacy_prefix_budget_when_parent_lacks_child():
    parent_context = "父级上下文" * 80
    chunk = RetrievedChunk(
        chunk_id="missing-child",
        source_id="source-missing",
        title="Missing",
        scope="general",
        text="未出现在父级上下文中的命中片段",
        metadata={"parent_context": parent_context},
    )

    payload = GenericCoachAgent.chunks_payload([chunk], max_text_chars=80)

    assert len(payload[0]["text"]) <= 80
    assert payload[0]["text"].startswith(parent_context[:20])
    assert payload[0]["text"].endswith("[内容已按上下文预算截断]")


@pytest.mark.asyncio
async def test_generic_coach_agent_links_knowledge_refs_with_full_text():
    payload = _valid_payload()
    payload["summary_knowledge_chunk_ids"] = ["chunk-coach"]
    payload["basis_knowledge_chunk_ids"] = ["chunk-coach"]
    payload["issues"][0]["knowledge_chunk_ids"] = ["chunk-coach"]
    original_text = "复盘知识库完整原文" * 80

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        retrieved_chunks=[
            {
                "chunk_id": "chunk-coach",
                "source_id": "source-coach",
                "title": "反馈沟通标准",
                "scope": "feedback",
                "text": original_text,
                "score": 0.9,
            }
        ],
    )

    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation["quote"] == original_text
    assert citation["targets"] == [
        "summary",
        "dimension_scores.0.basis",
        "improvement_points.0",
    ]


@pytest.mark.asyncio
async def test_generic_coach_agent_keeps_field_specific_exact_anchors():
    payload = _valid_payload()
    source_lines = {
        "summary": "事实证据应支撑沟通结论。",
        "basis": "理解核对用于确认双方是否形成一致认识。",
        "problem": "说明沟通目的有助于界定讨论边界。",
        "impact": "讨论边界不清会增加员工理解负担。",
        "suggestion": "先说明沟通目的，再共同核对事实。",
        "reason": "先交代目的可以降低突兀感。",
    }
    payload["summary_citation_refs"] = [
        {
            "source_ref": "chunk-coach",
            "highlight_text": "需要补充事实证据",
            "source_quote": source_lines["summary"],
        }
    ]
    payload["basis_citation_refs"] = [
        {
            "source_ref": "chunk-coach",
            "highlight_text": "缺少完整的理解核对",
            "source_quote": source_lines["basis"],
        }
    ]
    issue = payload["issues"][0]
    issue["problem_citation_refs"] = [
        {
            "source_ref": "chunk-coach",
            "highlight_text": "没有先说明沟通目的",
            "source_quote": source_lines["problem"],
        }
    ]
    issue["impact_citation_refs"] = [
        {
            "source_ref": "chunk-coach",
            "highlight_text": "员工可能难以理解讨论边界",
            "source_quote": source_lines["impact"],
        }
    ]
    issue["suggestion_citation_refs"] = [
        {
            "source_ref": "chunk-coach",
            "highlight_text": "先说明今天沟通的目的",
            "source_quote": source_lines["suggestion"],
        }
    ]
    issue["reason_citation_refs"] = [
        {
            "source_ref": "chunk-coach",
            "highlight_text": "先交代目的可以降低突兀感",
            "source_quote": source_lines["reason"],
        }
    ]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        retrieved_chunks=[
            {
                "chunk_id": "chunk-coach",
                "source_id": "development-dialog.md",
                "title": "沟通方法",
                "scope": "development_dialog",
                "text": "\n".join(source_lines.values()),
                "score": 0.9,
            }
        ],
    )

    assert len(result.citations) == 1
    anchors = result.citations[0]["anchors"]
    assert [anchor["target"] for anchor in anchors] == [
        "summary",
        "dimension_scores.0.basis",
        "improvement_points.0",
        "improvement_points.0",
        "better_phrases.0.suggestion",
        "better_phrases.0.reason",
    ]
    assert [anchor["highlight_text"] for anchor in anchors] == [
        "需要补充事实证据",
        "缺少完整的理解核对",
        "没有先说明沟通目的",
        "员工可能难以理解讨论边界",
        "先说明今天沟通的目的",
        "先交代目的可以降低突兀感",
    ]


@pytest.mark.asyncio
async def test_generic_coach_agent_keeps_more_than_four_valid_citations():
    payload = _valid_payload()
    highlights = [f"第{index}项可核验事实" for index in range(1, 6)]
    payload["summary"] = "、".join(highlights)
    payload["summary_citation_refs"] = [
        {
            "source_ref": f"performance-{index}",
            "highlight_text": highlight,
            "source_quote": f"绩效评价应核对{highlight}。",
        }
        for index, highlight in enumerate(highlights, start=1)
    ]
    calls = []

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(
        GenericCoachAgent(llm_service=LLM()),
        retrieved_chunks=[
            {
                "chunk_id": f"performance-{index}",
                "source_id": f"performance-{index}.md",
                "title": f"绩效依据 {index}",
                "scope": "performance",
                "text": f"绩效评价应核对{highlight}。",
                "score": 0.9,
            }
            for index, highlight in enumerate(highlights, start=1)
        ],
    )

    assert len(calls) == 1
    assert len(result.citations) == 5
    assert {
        citation["anchors"][0]["target"] for citation in result.citations
    } == {"summary"}


@pytest.mark.asyncio
async def test_generic_coach_agent_drops_unavailable_legacy_knowledge_chunk_id():
    payload = _valid_payload()
    payload["summary_knowledge_chunk_ids"] = ["missing-chunk"]

    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            return CoachDimensionModelOutput.model_validate(payload)

    result = await _run(GenericCoachAgent(llm_service=LLM()))

    assert result.citations == []


def _finalize_development_plan_payload(
    payload: dict,
    *,
    task_id: str = "development_plan_evaluation",
    career_elements_applicable: bool = True,
    include_career_source: bool = True,
):
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    for issue in payload.get("issues", []):
        issue["diagnostic_dimension_id"] = "job_level_requirements"
    source_quote = (
        "Career Elements 将 Cross-functional move 列为职业要素之一。"
    )
    chunk = RetrievedChunk(
        chunk_id="career-exact",
        source_id="career-elements.md",
        title="Career Elements",
        scope="career",
        text=source_quote,
        score=0.9,
    )
    context = CoachPromptContext.model_validate(
        {
            "conversation": [
                {
                    "turn_index": 1,
                    "speaker": "manager",
                    "text": "我们先看事实。",
                }
            ],
            "retrieved_chunks": (
                [chunk.model_dump(mode="python", exclude={"metadata"})]
                if include_career_source
                else []
            ),
            "dimension_config": {
                "dimension": {
                    "id": "summary_and_intent_specific_development_plan",
                    "name": "总结与差异化发展计划评估",
                }
            },
            "career_elements_applicable": career_elements_applicable,
        }
    )
    return GenericCoachAgent._finalize_task_result(
        CoachDimensionModelOutput.model_validate(payload),
        task_id=task_id,
        task_name="总结与差异化发展计划评估",
        context=context,
        citation_chunks=[chunk] if include_career_source else [],
        evidence_conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text="我们先看事实。",
            )
        ],
    )


def test_development_plan_succeeds_without_optional_career_elements_advice():
    result = _finalize_development_plan_payload(_valid_payload())

    assert result.status == "success"
    assert result.career_elements_advice == []


def test_development_plan_keeps_uncited_optional_career_elements_advice():
    payload = _valid_payload()
    payload["career_elements_advice"] = [
        {
            "element": "跨职能经历",
            "suggestion": "安排一次跨部门项目实践。",
            "reason": "用于拓展跨团队协作经验。",
        }
    ]

    result = _finalize_development_plan_payload(payload)

    assert result.status == "success"
    assert len(result.career_elements_advice) == 1
    assert result.career_elements_advice[0].element == "跨职能经历"
    assert result.citations == []


def test_development_plan_keeps_career_advice_when_optional_citation_is_invalid():
    payload = _valid_payload()
    payload["career_elements_advice"] = [
        {
            "element": "跨职能经历",
            "suggestion": "安排一次跨部门项目实践。",
            "reason": "用于拓展跨团队协作经验。",
            "element_citation_refs": [
                {
                    "source_ref": "career-exact",
                    "highlight_text": "跨职能经历",
                    "source_quote": "这段文字并不存在于知识来源中。",
                }
            ],
        }
    ]

    result = _finalize_development_plan_payload(payload)

    assert len(result.career_elements_advice) == 1
    assert result.career_elements_advice[0].element == "跨职能经历"
    assert result.citations == []


@pytest.mark.parametrize(
    ("task_id", "career_elements_applicable", "include_career_source"),
    [
        ("opening_evaluation", True, True),
        ("development_plan_evaluation", False, True),
        ("development_plan_evaluation", True, False),
    ],
)
def test_career_advice_still_requires_an_applicable_development_plan_context(
    task_id: str,
    career_elements_applicable: bool,
    include_career_source: bool,
):
    payload = _valid_payload()
    payload["score"] = 4
    payload["issues"] = []
    payload["career_elements_advice"] = [
        {
            "element": "跨职能经历",
            "suggestion": "安排一次跨部门项目实践。",
            "reason": "用于拓展跨团队协作经验。",
        }
    ]

    result = _finalize_development_plan_payload(
        payload,
        task_id=task_id,
        career_elements_applicable=career_elements_applicable,
        include_career_source=include_career_source,
    )

    assert result.career_elements_advice == []


def test_development_plan_keeps_career_advice_with_element_anchor_only():
    payload = _valid_payload()
    payload["career_elements_advice"] = [
        {
            "element": "核对 Career Elements 的跨职能经历要求",
            "suggestion": "结合岗位目标安排一次跨部门项目实践。",
            "reason": "通过真实项目验证跨团队协作经验。",
            "element_citation_refs": [
                {
                    "source_ref": "career-exact",
                    "highlight_text": "核对 Career Elements 的跨职能经历要求",
                    "source_quote": (
                        "Career Elements 将 Cross-functional move "
                        "列为职业要素之一。"
                    ),
                }
            ],
        }
    ]

    result = _finalize_development_plan_payload(payload)

    assert result.status == "success"
    assert len(result.career_elements_advice) == 1
    assert len(result.citations) == 1
    assert result.citations[0]["scope"] == "career"
    assert result.citations[0]["anchors"][0]["target"] == (
        "career_elements_advice.0.element"
    )


def test_development_plan_keeps_more_than_three_valid_career_advice_items():
    payload = _valid_payload()
    payload["career_elements_advice"] = [
        {
            "element": f"核对 Career Elements 的跨职能经历要求 {index}",
            "suggestion": f"结合岗位目标安排第 {index} 次跨部门项目实践。",
            "reason": "通过真实项目验证跨团队协作经验。",
            "element_citation_refs": [
                {
                    "source_ref": "career-exact",
                    "highlight_text": (
                        f"核对 Career Elements 的跨职能经历要求 {index}"
                    ),
                    "source_quote": (
                        "Career Elements 将 Cross-functional move "
                        "列为职业要素之一。"
                    ),
                }
            ],
        }
        for index in range(4)
    ]

    result = _finalize_development_plan_payload(payload)

    assert len(result.career_elements_advice) == 4
    assert [item.element[-1] for item in result.career_elements_advice] == [
        "0",
        "1",
        "2",
        "3",
    ]

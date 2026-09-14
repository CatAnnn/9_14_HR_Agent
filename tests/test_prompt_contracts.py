from __future__ import annotations

import json

from backend.agents.coach_agent.dimension_evaluator import validate_coach_runtime
from backend.agents.guidance_agent import validate_guidance_runtime
from backend.business_config.loader import BusinessConfigLoader
from backend.schemas.prompt_context import (
    CoachPromptContext,
    GuidancePromptContext,
    PromptCompanyValue,
    PromptCompanyValues,
    prompt_context_json,
)
from backend.services.prompt_service import PromptService


def test_all_guidance_and_coach_templates_render_under_strict_undefined():
    assert validate_guidance_runtime().startswith("guidance-")
    assert validate_coach_runtime().startswith("coach-")


def test_coach_dimension_templates_use_coach_base_contract():
    prompts = PromptService()
    assert prompts.env.loader is not None
    base_source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "coach/_dimension_evaluation.jinja2",
    )

    assert "strengths 输出 2～4 句" in base_source
    assert "【通用提交检查】" in base_source

    for template_path in (
        "coach/start.jinja2",
        "coach/emotion.jinja2",
        "coach/requirement.jinja2",
        "coach/plan.jinja2",
    ):
        source, _, _ = prompts.env.loader.get_source(prompts.env, template_path)
        assert (
            '{% include "coach/_dimension_evaluation.jinja2" %}' in source
        ), template_path
        assert '{% include "guidance/' not in source, template_path
        assert "strengths 输出 2～4 句" not in source, template_path
        assert "【提交响应前静默检查】" not in source, template_path
        assert "【生成前检查】" not in source, template_path
        assert "【最终约束】" not in source, template_path


def test_coach_base_contract_rejects_low_score_without_real_issue():
    prompts = PromptService()
    assert prompts.env.loader is not None
    source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "coach/_dimension_evaluation.jinja2",
    )

    assert "score 为 1、2 或 3 时，issues 至少输出一个" in source
    assert "不表示每个固定子维度都必须有问题" in source


def test_fixed_diagnostic_prompts_require_stable_dimension_ids():
    prompts = PromptService()
    assert prompts.env.loader is not None

    opening_source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "coach/start.jinja2",
    )
    for dimension_id in (
        "communication_tone",
        "performance_result_alignment",
    ):
        assert dimension_id in opening_source
    assert "issues 最多输出 16 个" in opening_source
    assert "diagnostic_dimension_id 必须继续为 null 或省略" not in opening_source

    emotion_source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "coach/emotion.jinja2",
    )
    for dimension_id in (
        "listening_and_emotion_understanding",
        "empathic_summary",
        "joint_exploration",
    ):
        assert dimension_id in emotion_source

    requirement_source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "coach/requirement.jinja2",
    )
    for dimension_id in (
        "goal_direction_correctness",
        "result_breakthrough",
        "high_performance_culture",
    ):
        assert dimension_id in requirement_source
    assert "全部 issues 最多 24 个" in requirement_source

    plan_source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "coach/plan.jinja2",
    )
    for dimension_id in (
        "job_level_requirements",
        "career_elements",
    ):
        assert dimension_id in plan_source
    assert "issues 最多 16 个" in plan_source

    for source in (emotion_source, requirement_source):
        assert "dimension_judgment" not in source
        assert "leading_question" not in source
        assert "3 个 bullet" not in source


def test_emotion_prompt_allows_one_evidence_backed_unclassified_issue_only():
    prompts = PromptService()
    assert prompts.env.loader is not None

    emotion_source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "coach/emotion.jinja2",
    )
    base_source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "coach/_dimension_evaluation.jinja2",
    )

    assert "【无法准确归类的真实问题兜底】" in emotion_source
    assert "diagnostic_dimension_id 为 null 或省略" in emotion_source
    assert "最多一个；" in emotion_source
    assert "位于所有固定维度 issues 之后" not in emotion_source
    assert "不得用兜底 issue 逃避一个已经能够明确归类的问题" in emotion_source
    assert "唯一例外是情绪承接专属提示允许" in base_source
    assert "总数中最多 1 个 issue 作为兜底项" in base_source
    assert "同一固定子维度可以有多条 issue，但最多 8 条" in base_source
    assert "score 为 1、2 或 3 时，issues 至少输出一个" in base_source


def test_guidance_plan_keeps_career_elements_distinct_from_job_level_capabilities():
    prompts = PromptService()
    assert prompts.env.loader is not None
    source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "guidance/plan.jinja2",
    )

    official_elements = (
        "Cross-divisional experience",
        "Cross-functional experience",
        "International experience",
        "Associate leadership experience",
        "Project leadership experience",
    )
    for element in official_elements:
        assert element in source

    assert "等 Career Elements 核心发展维度形成分析" not in source
    assert "普通岗级能力作为 Career Element" in source


def test_coach_plan_uses_available_intent_fields():
    prompts = PromptService()
    assert prompts.env.loader is not None
    source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "coach/plan.jinja2",
    )

    assert "context.intent.config" not in source
    assert "context.intent_config" not in source
    assert "context.intent_id" in source
    assert "context.dimension_config" in source
    assert 'diagnostic_dimension_id="job_level_requirements"' in source
    assert 'diagnostic_dimension_id="career_elements"' in source
    assert "\n2. dimension_judgment" not in source


def test_guidance_prompt_uses_correct_asr_rating_term():
    prompts = PromptService()
    assert prompts.env.loader is not None
    source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "guidance/guidance.jinja2",
    )

    assert "ASR rating" in source
    assert "ARS rating" not in source
    assert "ars rating" not in source


def test_employee_prompt_keeps_personality_plan_completeness_context_gated():
    prompts = PromptService()
    assert prompts.env.loader is not None

    source, _, _ = prompts.env.loader.get_source(
        prompts.env,
        "employee/reply.jinja2",
    )
    assert "不得为了避免“过于完整”而机械缩短" in source
    assert "当前问题明确要求或自然涉及方案、步骤、执行或下一步" in source
    assert "不得仅因话题进入行动阶段就补成完整计划" in source
    assert "可以自然给出与问题粒度匹配的完整计划" in source
    assert "“本档具体线索”是概率性职场例子" in source
    assert "“本档特有校准”是当前档位相对相邻档最具体的差异" in source
    assert "不得套用相邻档" in source
    assert "父维度和 facet 不得双重放大" in source
    assert "“情境调节”只帮助判断倾向是否容易显现" in source
    assert "“禁止推论”必须遵守" in source
    assert "本档作用方式" not in source


def test_prompt_contexts_are_json_and_exclude_legacy_model_variables():
    guidance_context = GuidancePromptContext(
        supplemental_info="完整补充信息",
        company_values=PromptCompanyValues(
            enabled=True,
            version="culture-v1",
            company_name="测试公司",
            culture_name={"en": "Test Culture", "zh": "测试文化"},
            values=[
                PromptCompanyValue(
                    id="respect",
                    name="Respect",
                    name_zh="尊重",
                    definition="尊重事实和对方。",
                    desired_behaviors_zh=["先听取对方说明。"],
                )
            ],
        ),
    )
    guidance_json = prompt_context_json(guidance_context)
    guidance_payload = json.loads(guidance_json)
    assert guidance_payload["supplemental_info"] == "完整补充信息"
    assert guidance_payload["company_values"]["culture_name"]["zh"] == "测试文化"
    assert guidance_payload["company_values"]["values"][0]["name_zh"] == "尊重"
    assert guidance_payload["company_values"]["values"][0][
        "desired_behaviors_zh"
    ] == ["先听取对方说明。"]
    assert "source_refs" not in guidance_json
    assert "conversation" not in guidance_payload
    assert "emotion_state" not in guidance_payload

    prompts = PromptService()
    for template in (
        "guidance/start.jinja2",
        "guidance/emotion.jinja2",
        "guidance/requirement.jinja2",
        "guidance/plan.jinja2",
    ):
        rendered = prompts.render(
            template,
            guidance_group_range="1-3",
            guidance_detail_range="2-4",
            guidance_point_title_max_length=36,
            guidance_point_summary_max_length=80,
            guidance_point_detail_max_length=100,
            context_json=guidance_json,
        )
        for legacy_name in (
            "emotion_state",
            "improvement_points",
            "better_phrases",
            "plan_dimension_config",
            "section_requirements_json",
            "guidance_list_range",
        ):
            assert legacy_name not in rendered

    coach_json = prompt_context_json(
        CoachPromptContext(intent_id="development", dimension_config={})
    )
    for template in (
        "coach/start.jinja2",
        "coach/emotion.jinja2",
        "coach/requirement.jinja2",
        "coach/plan.jinja2",
    ):
        rendered = prompts.render(template, context_json=coach_json)
        for legacy_name in (
            "CoachTaskResult",
            "dimension_scores",
            "improvement_points",
            "better_phrases",
            "extra.core_outputs",
            "调用工具",
        ):
            assert legacy_name not in rendered


def test_prompt_versions_change_with_prompt_or_query_contract(monkeypatch):
    loader = BusinessConfigLoader()
    state = {"prompt": "prompt-v1", "query": "query-v1"}
    monkeypatch.setattr(
        loader,
        "coach_config",
        lambda filename: {"version": "v1", "filename": filename},
    )
    monkeypatch.setattr(
        loader,
        "_prompt_bundle",
        lambda paths: {path: state["prompt"] for path in paths},
    )
    monkeypatch.setattr(
        loader,
        "_query_contract",
        lambda names: {"names": names, "value": state["query"]},
    )

    coach_v1 = loader.coach_version()
    guidance_v1 = loader.guidance_version()
    state["prompt"] = "prompt-v2"
    loader.clear_cache()
    coach_v2 = loader.coach_version()
    guidance_v2 = loader.guidance_version()
    assert coach_v2 != coach_v1
    assert guidance_v2 != guidance_v1

    state["query"] = "query-v2"
    loader.clear_cache()
    assert loader.coach_version() != coach_v2
    assert loader.guidance_version() != guidance_v2

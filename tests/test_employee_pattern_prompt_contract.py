from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.prompt_service import PromptService


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    "employee/reply.jinja2",
    "employee/reply_test.jinja2",
)
TEMPLATE_PATHS = tuple(
    PROJECT_ROOT / "backend/prompts" / template for template in TEMPLATES
)
GUIDANCE = json.dumps(
    {
        "main_psychological_activity": "在维护专业判断时重新评估新证据",
        "response_tendency": "conditional_acceptance",
        "expression_guidance": "只承认得到证据支持的部分",
        "checks": ["不要把局部接受扩大为整体接受"],
    },
    ensure_ascii=False,
)


@pytest.mark.parametrize("template_name", TEMPLATES)
def test_pattern_guidance_is_optional_and_only_rendered_when_non_empty(
    template_name: str,
) -> None:
    prompts = PromptService()

    without_guidance = prompts.render(template_name)
    with_guidance = prompts.render(
        template_name,
        pattern_response_guidance=GUIDANCE,
    )

    assert "【Psychological Pattern Dynamics——本轮隐藏回应指导】" not in (
        without_guidance
    )
    assert "在维护专业判断时重新评估新证据" not in without_guidance
    assert with_guidance.count(
        "【Psychological Pattern Dynamics——本轮隐藏回应指导】"
    ) == 1
    assert "在维护专业判断时重新评估新证据" in with_guidance
    assert "不得在本模板中重新选择、搜索、推断、组合或列举任何 Pattern" in (
        with_guidance
    )
    assert "不得把仍然存在的内部立场自动转换成再次外显" in with_guidance
    assert "它不是本轮回复的内容议程" in with_guidance
    assert "Manager 提出具体问题时，先直接回答该问题" in with_guidance
    assert (
        "回应倾向、语气、直接程度、信息密度和承诺边界" in with_guidance
    )
    assert "`main_psychological_activity` 只供内部理解" in with_guidance
    assert "`checks` 只能作为禁止性边界检查" in with_guidance
    assert "允许不使用该项" in with_guidance
    assert (
        "自动加入感谢或认可、重述既有立场、行动计划或追问" in with_guidance
    )
    assert "更不得把它们组织成固定组合" in with_guidance


def test_employee_templates_share_one_pattern_guidance_variable_contract() -> None:
    for template_path in TEMPLATE_PATHS:
        source = template_path.read_text(encoding="utf-8")
        assert (
            "{% set pattern_response_guidance = "
            "pattern_response_guidance | default('') %}"
        ) in source
        assert source.count("{{ pattern_response_guidance }}") == 1
        assert source.count("{% if pattern_response_guidance %}") == 1


def test_test_prompt_does_not_allow_pattern_to_choose_reply_content() -> None:
    source = TEMPLATE_PATHS[1].read_text(encoding="utf-8")

    assert "Psychological Pattern Dynamics 只控制正文选择" not in source
    assert "Psychological Pattern Dynamics 不决定正文内容" in source


def test_production_prompt_consolidates_dispute_and_acceptance_without_replacing_them() -> None:
    source = TEMPLATE_PATHS[0].read_text(encoding="utf-8")

    assert source.count("【Acceptance 与 Dispute——唯一权威规则】") == 1
    for removed_duplicate_heading in (
        "【反驳触发与跨轮状态控制】",
        "【分歧状态】",
        "【未解决分歧的残留影响】",
        "【语义重复判断】",
        "【反驳推进原则】",
        "【反驳内容轮换】",
        "【行动阶段的回应与立场连续性】",
        "【员工狡辩与反驳方法】",
        "【真实防御方式】",
        "【反驳策略去重】",
        "【接受方式】",
    ):
        assert removed_duplicate_heading not in source

    for preserved_semantics in (
        "客观事实、原因解释、责任判断、问题范围与程度、能力判断、绩效或发展含义以及行动要求",
        "接受、部分接受、保留、不接受或暂时无法判断",
        "负面反馈本身不自动产生反驳",
        "每轮最多表达一个与本轮新增内容直接相关的核心方向",
        "尚未表达、已表达未回应、部分回应、已回应、暂时搁置、重新激活",
        "没有会改变原判断的新事实依据、因果解释、责任变化、实际影响或评价含义时",
        "是否接受行动、是否接受其评价前提、是否承担责任及承诺程度",
    ):
        assert preserved_semantics in source

    assert "不得创建或删除分歧" in source
    assert "不得直接改变任何层面的接受状态" in source


@pytest.mark.parametrize("template_name", TEMPLATES)
def test_pattern_internal_details_are_explicitly_excluded_from_all_outputs(
    template_name: str,
) -> None:
    rendered = PromptService().render(
        template_name,
        pattern_response_guidance=GUIDANCE,
    )

    for forbidden_internal_detail in (
        "Pattern 名称",
        "Pattern ID",
        "strength/强度",
        "trigger/Trigger",
        "interaction",
        "Behavioral Tendency",
        "内部心理活动分析",
        "字段名",
        "internal checklist/内部 checks",
    ):
        assert forbidden_internal_detail in rendered
    assert "不得出现或影射" in rendered or "不输出或影射" in rendered

    if template_name.endswith("reply_test.jinja2"):
        assert "不属于可披露的输入依据" in rendered
        assert "员工正文和句后依据都不得出现或影射" in rendered

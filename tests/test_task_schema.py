import pytest

from backend.schemas.task import (
    CoachDimensionIssue,
    CoachDimensionModelOutput,
    CoachTaskResult,
)


def test_coach_dimension_issue_normalizes_blank_diagnostic_id_to_null():
    issue = CoachDimensionIssue(
        turn_index=1,
        quote="我们先看事实。",
        diagnostic_dimension_id="   ",
        problem="没有说明需要核对的范围。",
        impact="员工难以准备对应证据。",
        suggestion="我们先核对目标结果和关键案例。",
        reason="明确范围可以提高讨论效率。",
    )

    assert issue.diagnostic_dimension_id is None


def test_coach_dimension_output_keeps_all_valid_strength_sentences_on_success():
    for strengths in (
        [],
        ["Manager 清楚说明了沟通目的。"],
        [
            "Manager 清楚说明了沟通目的。",
            "Manager 使用事实帮助双方聚焦讨论。",
        ],
        ["第一句。", "第二句。", "第三句。", "第四句。"],
    ):
        result = CoachDimensionModelOutput(
            status="success",
            score=4,
            summary="Manager 已完成本维度的关键沟通。",
            strengths=strengths,
            basis="Manager 的实际表达覆盖了本维度的主要要求。",
        )
        assert result.strengths == strengths

    overflow = CoachDimensionModelOutput(
        status="success",
        score=4,
        summary="Manager 已完成本维度的关键沟通。",
        strengths=["第一句。", "第二句。", "第三句。", "第四句。", "第五句。"],
        basis="Manager 的实际表达覆盖了本维度的主要要求。",
    )
    assert overflow.strengths == [
        "第一句。",
        "第二句。",
        "第三句。",
        "第四句。",
        "第五句。",
    ]


@pytest.mark.parametrize("score", [0, 6])
def test_coach_dimension_output_rejects_score_outside_five_point_scale(score):
    with pytest.raises(ValueError, match="score"):
        CoachDimensionModelOutput(
            status="success",
            score=score,
            summary="Manager 已完成本维度的关键沟通。",
            basis="Manager 的实际表达覆盖了本维度的主要要求。",
        )


def test_coach_dimension_output_keeps_issue_overflow_without_validation_failure():
    issue = {
        "turn_index": 1,
        "quote": "我们先看事实。",
        "problem": "没有说明需要核对的范围。",
        "impact": "员工难以准备对应证据。",
        "suggestion": "我们先核对目标结果和关键案例。",
        "reason": "明确范围可以提高讨论效率。",
    }

    result = CoachDimensionModelOutput(
        status="success",
        score=3,
        summary="当前仍有多个需要改进的问题。",
        basis="Manager 的实际回应存在多项不足。",
        issues=[dict(issue) for _ in range(25)],
    )
    assert len(result.issues) == 25

    overflow = CoachDimensionModelOutput(
        status="success",
        score=3,
        summary="当前仍有多个需要改进的问题。",
        basis="Manager 的实际回应存在多项不足。",
        issues=[dict(issue) for _ in range(26)],
    )
    assert len(overflow.issues) == 26
    assert "maxItems" not in CoachDimensionModelOutput.model_json_schema()[
        "properties"
    ]["issues"]


def test_coach_dimension_output_filters_optional_citations_individually():
    references = [
        {"source_ref": "missing-fields"},
        {
            "source_ref": "chunk-1",
            "highlight_text": "第一处引用",
            "source_quote": "第一处来源原文。",
            "harmless_extra": "ignored",
        },
        *[
            {
                "source_ref": f"chunk-{index}",
                "highlight_text": f"第{index}处引用",
                "source_quote": f"第{index}处来源原文。",
            }
            for index in range(2, 7)
        ],
    ]
    issue = {
        "turn_index": 1,
        "quote": "我们先看事实。",
        "diagnostic_dimension_id": "communication_tone",
        "problem": "没有说明需要核对的范围。",
        "impact": "员工难以准备对应证据。",
        "suggestion": "我们先核对目标结果和关键案例。",
        "reason": "明确范围可以提高讨论效率。",
        "problem_citation_refs": references,
        "speaker": "manager",
    }

    result = CoachDimensionModelOutput.model_validate(
        {
            "status": "success",
            "score": 3,
            "summary": "当前仍有需要改进的问题。",
            "basis": "Manager 的实际回应存在不足。",
            "summary_citation_refs": references,
            "issues": [issue],
        }
    )

    assert len(result.summary_citation_refs) == 6
    assert len(result.issues[0].problem_citation_refs) == 6
    assert result.summary_citation_refs[0].source_ref == "chunk-1"
    assert result.summary_citation_refs[0].model_extra is None
    assert not hasattr(result.issues[0], "speaker")


def test_coach_dimension_output_keeps_career_advice_overflow():
    result = CoachDimensionModelOutput.model_validate(
        {
            "status": "success",
            "score": 4,
            "summary": "发展讨论已经开始。",
            "basis": "Manager 已经进入后续发展安排。",
            "career_elements_advice": [
                {
                    "element": f"Career Element {index}",
                    "suggestion": f"开展第 {index} 项实践。",
                    "reason": "用于形成相关经历。",
                }
                for index in range(4)
            ],
        }
    )

    assert len(result.career_elements_advice) == 4
    assert "maxItems" not in CoachDimensionModelOutput.model_json_schema()[
        "properties"
    ]["career_elements_advice"]


def test_coach_dimension_output_keeps_strengths_empty_when_information_is_insufficient():
    result = CoachDimensionModelOutput(
        status="insufficient_information",
        score=None,
        summary="当前对话尚未进入可评估阶段。",
        basis="没有足够的本维度对话内容。",
    )

    assert result.strengths == []

    with pytest.raises(ValueError, match="strengths must be empty"):
        CoachDimensionModelOutput(
            status="insufficient_information",
            score=None,
            summary="当前对话尚未进入可评估阶段。",
            strengths=["Manager 保持了基本礼貌。", "Manager 完成了简短回应。"],
            basis="没有足够的本维度对话内容。",
        )


def test_coach_task_result_normalizes_common_model_output_aliases():
    result = CoachTaskResult.model_validate(
        {
            "task_id": "redline_check",
            "task_name": "话术红线检测",
            "status": "completed",
            "summary": "未发现明显红线。",
            "dimension_scores": {},
            "evidence": {},
            "strengths": "有事实回顾",
            "improvement_points": None,
            "risks": {},
            "better_phrases": {},
            "citations": {},
        }
    )

    assert result.status == "success"
    assert result.dimension_scores == []
    assert result.evidence == []
    assert result.strengths == ["有事实回顾"]
    assert result.improvement_points == []
    assert result.risks == []
    assert result.better_phrases == []
    assert result.citations == []


def test_coach_task_result_normalizes_simplified_scores_risks_and_phrases():
    result = CoachTaskResult.model_validate(
        {
            "task_id": "performance_evaluation",
            "task_name": "绩效反馈质量评估",
            "summary": "需要更聚焦事实。",
            "dimension_scores": {"fact_based": 20, "action_plan": 0},
            "risks": ["可能激化防御情绪"],
            "better_phrases": [
                {
                    "diagnostic_dimension_id": "joint_exploration",
                    "original_context": "你这个月没做好",
                    "suggested_phrase": "我们先看本月目标和完成数据之间的差距。",
                }
            ],
        }
    )

    assert result.dimension_scores[0].id == "fact_based"
    assert result.dimension_scores[0].score == 20
    assert result.risks[0].explanation == "可能激化防御情绪"
    assert result.better_phrases[0].original == "你这个月没做好"
    assert result.better_phrases[0].suggestion == "我们先看本月目标和完成数据之间的差距。"
    assert (
        result.better_phrases[0].diagnostic_dimension_id
        == "joint_exploration"
    )


def test_coach_task_result_normalizes_string_citations():
    result = CoachTaskResult.model_validate(
        {
            "task_id": "rubric_evaluation",
            "task_name": "Rubric 综合评估",
            "summary": "ok",
            "citations": ["performance_feedback_basics.md"],
        }
    )

    assert result.citations == [{"source": "performance_feedback_basics.md"}]


def test_coach_task_result_normalizes_dimension_score_aliases_from_model_output():
    result = CoachTaskResult.model_validate(
        {
            "task_id": "rubric_evaluation",
            "task_name": "Rubric 综合评估",
            "summary": "ok",
            "dimension_scores": [
                {
                    "dimension_id": "opening_structure",
                    "dimension_name": "开场与结构",
                    "score": 8,
                    "feedback": "开场清晰，但需要更明确谈话结构。",
                }
            ],
        }
    )

    score = result.dimension_scores[0]
    assert score.id == "opening_structure"
    assert score.name == "开场与结构"
    assert score.score == 8
    assert score.comment == "开场清晰，但需要更明确谈话结构。"



def test_coach_task_result_normalizes_strength_and_improvement_point_objects():
    result = CoachTaskResult.model_validate(
        {
            "task_id": "rubric_evaluation",
            "task_name": "Rubric 行为评分",
            "summary": "需要进一步明确行动计划。",
            "strengths": [
                "  能够回顾事实  ",
                {"point": "  有主动倾听  ", "evidence": "对象内部证据不转移"},
                {"point": "   "},
            ],
            "improvement_points": [
                {
                    "point": "  需要给出明确的改进时间节点  ",
                    "evidence": "经理只提出要求，没有约定检查日期",
                },
                "  明确后续跟进人  ",
                {"point": ""},
            ],
            "evidence": [
                {
                    "turn_index": 2,
                    "speaker": "manager",
                    "quote": "后续再看。",
                }
            ],
        }
    )

    assert result.strengths == ["能够回顾事实", "有主动倾听"]
    assert result.improvement_points == ["需要给出明确的改进时间节点", "明确后续跟进人"]
    assert len(result.evidence) == 1
    assert result.evidence[0].quote == "后续再看。"

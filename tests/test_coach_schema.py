from backend.schemas.coach import CoachReport
from backend.schemas.task import CoachTaskResult


def test_coach_report_preserves_dimension_problems_and_improvements():
    report = CoachReport.model_validate(
        {
            "session_id": "s1",
            "coach_version": "coach-v1",
            "task_results": [
                {
                    "task_id": "opening_evaluation",
                    "task_name": "开场定调与结果对齐",
                    "status": "success",
                    "score": 4,
                    "summary": "目标清楚，但事实说明不够具体。",
                    "dimension_scores": [
                        {
                            "id": "opening",
                            "name": "开场定调与结果对齐",
                            "score": 4,
                            "level": "良好",
                            "basis": "说明了目标，但没有引用结果数据。",
                        }
                    ],
                    "improvement_points": ["补充目标值与实际结果。"],
                    "better_phrases": [
                        {
                            "original": "你的结果不够好",
                            "suggestion": "本期目标为 100，实际结果为 82，我们先看差距原因。",
                            "reason": "把笼统判断改为可核对事实。",
                        }
                    ],
                }
            ],
        }
    )

    result = report.task_results[0]
    assert result.score == 4
    assert result.dimension_scores[0].level == "良好"
    assert result.improvement_points == ["补充目标值与实际结果。"]
    assert result.better_phrases[0].suggestion.startswith("本期目标")


def test_coach_report_schema_contains_only_direct_report_fields():
    report = CoachReport(
        session_id="s1",
        task_results=[
            CoachTaskResult(
                task_id="emotion_evaluation",
                task_name="情绪承接",
                status="failed",
                summary="评估失败。",
            )
        ],
    )

    assert set(report.model_dump()) == {
        "session_id",
        "coach_version",
        "locale",
        "status",
        "task_results",
        "disclaimer",
    }
    assert report.task_results[0].status == "failed"

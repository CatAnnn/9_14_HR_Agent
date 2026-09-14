import pytest

from backend.agents.coach_agent.coach_orchestrator import COACH_TASK_SPECS, CoachOrchestrator
from backend.schemas.state import SessionState
from backend.schemas.task import CoachTaskResult


def _result(task_id: str, task_name: str, score: int = 4) -> CoachTaskResult:
    return CoachTaskResult(
        task_id=task_id,
        task_name=task_name,
        score=score,
        summary=f"{task_name}评估完成。",
        improvement_points=["需要补充具体事实。"],
        better_phrases=[
            {
                "original": "你做得不好",
                "suggestion": "我们先看目标与实际结果之间的差距。",
                "reason": "使用事实描述，避免笼统定性。",
            }
        ],
    )


def test_direct_report_keeps_fixed_four_dimension_order():
    reversed_results = [
        _result(task_id, task_name, score=index + 2)
        for index, (task_id, task_name) in enumerate(reversed(COACH_TASK_SPECS))
    ]

    report = CoachOrchestrator.build_report("s1", reversed_results)

    assert report.status == "success"
    assert [result.task_id for result in report.task_results] == [
        task_id for task_id, _ in COACH_TASK_SPECS
    ]
    assert all(result.improvement_points for result in report.task_results)
    assert all(result.better_phrases for result in report.task_results)


def test_direct_report_fills_missing_dimensions_with_failed_results():
    task_id, task_name = COACH_TASK_SPECS[0]

    report = CoachOrchestrator.build_report("s1", [_result(task_id, task_name)])

    assert report.status == "partial"
    assert len(report.task_results) == 4
    assert report.task_results[0].status == "success"
    assert all(result.status == "failed" for result in report.task_results[1:])


@pytest.mark.asyncio
async def test_orchestrator_calls_only_four_dimension_evaluators():
    calls: list[str] = []

    class Evaluator:
        def __init__(self, task_id: str, task_name: str):
            self.task_id = task_id
            self.task_name = task_name

        async def evaluate(self, state, retrieved_chunks):
            calls.append(self.task_id)
            return _result(self.task_id, self.task_name)

    orchestrator = object.__new__(CoachOrchestrator)
    orchestrator.evaluators = {
        task_id: Evaluator(task_id, task_name)
        for task_id, task_name in COACH_TASK_SPECS
    }

    report = await orchestrator.run(SessionState(session_id="s1"))

    assert len(calls) == 4
    assert set(calls) == {task_id for task_id, _ in COACH_TASK_SPECS}
    assert len(report.task_results) == 4
    assert not hasattr(orchestrator, "report_generator")

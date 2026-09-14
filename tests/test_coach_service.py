import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from backend.business_config.loader import BusinessConfigLoader
from backend.agents.coach_agent.coach_orchestrator import COACH_TASK_SPECS, CoachOrchestrator
from backend.schemas.coach import CoachReport
from backend.schemas.conversation import ConversationTurn
from backend.schemas.state import SessionState
from backend.schemas.task import CoachTaskResult
from backend.schemas.retrieval import RetrievedChunk
from backend.services.coach_service import CoachService


@pytest.mark.asyncio
async def test_coach_report_allows_empty_kb_chunks_and_records_warning():
    state = SessionState(
        session_id="s1",
        setup_ready=True,
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text="我们来复盘绩效差距。"),
            ConversationTurn(turn_index=2, speaker="employee", text="我觉得资源不足。"),
        ],
    )
    saved = {}

    class SessionService:
        def get_session(self, session_id):
            assert session_id == "s1"
            return state

        def save_session(self, next_state):
            saved["state"] = next_state
            return next_state

    class ReportRepo:
        def save_coach(self, report):
            saved["report"] = report

    class Retrieval:
        def retrieve(self, task_id, context):
            return []

    class Orchestrator:
        async def run_task(self, task_id, next_state, retrieved_chunks):
            assert next_state is state
            assert retrieved_chunks == []
            return CoachTaskResult(
                task_id=task_id,
                task_name=dict(COACH_TASK_SPECS)[task_id],
                score=4,
                summary="评估完成。",
            )

        @staticmethod
        def build_report(requested_session_id, task_results, *, locale="zh-CN"):
            return CoachOrchestrator.build_report(
                requested_session_id,
                task_results,
                locale=locale,
            )

    service = CoachService()
    service.session_service = SessionService()
    service.report_repo = ReportRepo()
    service.retrieval = Retrieval()
    service.orchestrator = Orchestrator()

    report = await service.generate("s1")

    assert report.session_id == "s1"
    assert state.stage == "report_ready"
    assert state.coach_report_id == "s1"
    assert any("development_plan_evaluation" in warning for warning in state.warnings)
    assert saved["report"] is report
    assert saved["state"] is state


def test_coach_report_status_is_partial_when_any_task_failed():
    task_results = [
        CoachTaskResult(task_id="opening_evaluation", task_name="开场定调与绩效结果对齐评估", status="success", score=4, summary="ok"),
        CoachTaskResult(task_id="development_plan_evaluation", task_name="总结与差异化发展计划评估", status="failed", summary="结构化输出失败"),
    ]

    normalized = CoachOrchestrator.build_report("s1", task_results)

    assert normalized.status == "partial"
    assert len(normalized.task_results) == 4
    assert [result.task_id for result in normalized.task_results] == [
        "opening_evaluation",
        "emotion_evaluation",
        "output_expectations_evaluation",
        "development_plan_evaluation",
    ]
    assert normalized.task_results[-1] is task_results[-1]


def test_low_score_without_an_improvement_issue_is_not_a_complete_checkpoint():
    inconsistent = CoachTaskResult(
        task_id="emotion_evaluation",
        task_name="情绪承接评估",
        status="success",
        score=2,
        summary="Manager 没有承接员工情绪。",
        improvement_points=[],
    )
    compatible_score_four = inconsistent.model_copy(update={"score": 4})

    assert CoachService._task_result_complete(inconsistent) is False
    assert CoachService._task_result_complete(compatible_score_four) is True


@pytest.mark.parametrize("score", [None, 0, 6])
def test_success_with_missing_or_invalid_score_is_not_complete(score):
    result = CoachTaskResult(
        task_id="emotion_evaluation",
        task_name="情绪承接评估",
        status="success",
        score=score,
        summary="旧结果缺少合法分数。",
    )

    assert CoachService._task_result_complete(result) is False


def test_get_rejects_a_stored_low_score_report_without_an_improvement_issue():
    config_loader = BusinessConfigLoader()
    inconsistent = CoachTaskResult(
        task_id="emotion_evaluation",
        task_name="情绪承接评估",
        status="success",
        score=2,
        summary="Manager 没有承接员工情绪。",
        improvement_points=[],
    )
    report = CoachReport(
        session_id="s1",
        coach_version=config_loader.coach_version(),
        task_results=[
            CoachTaskResult(
                task_id=task_id,
                task_name=task_name,
                status="insufficient_information",
                summary="测试结果。",
            )
            if task_id != "emotion_evaluation"
            else inconsistent
            for task_id, task_name in COACH_TASK_SPECS
        ],
    )

    class ReportRepo:
        @staticmethod
        def get_coach(session_id):
            assert session_id == "s1"
            return report

    service = CoachService()
    service.report_repo = ReportRepo()
    service.config_loader = config_loader
    service.session_service = SimpleNamespace(
        get_session=lambda _session_id: SessionState(session_id="s1")
    )

    with pytest.raises(KeyError, match="Current Coach report is unavailable"):
        service.get("s1")


@pytest.mark.asyncio
async def test_coach_report_returns_cached_report_when_report_id_exists():
    state = SessionState(
        session_id="s1",
        setup_ready=True,
        coach_report_id="s1",
        conversation=[ConversationTurn(turn_index=1, speaker="manager", text="我们来复盘。")],
    )
    config_loader = BusinessConfigLoader()
    cached = CoachReport(
        session_id="s1",
        coach_version=config_loader.coach_version(),
        task_results=[
            CoachTaskResult(
                task_id=task_id,
                task_name=task_name,
                status="insufficient_information",
                summary="测试缓存结果。",
            )
            for task_id, task_name in COACH_TASK_SPECS
        ],
    )

    class SessionService:
        def get_session(self, session_id):
            assert session_id == "s1"
            return state

    class ReportRepo:
        def get_coach(self, session_id):
            assert session_id == "s1"
            return cached

    class Retrieval:
        def retrieve(self, task_id, context):
            raise AssertionError("cached report should skip retrieval")

    class Orchestrator:
        async def run(self, next_state, retrieved_chunks_by_task):
            raise AssertionError("cached report should skip orchestration")

    service = CoachService()
    service.session_service = SessionService()
    service.report_repo = ReportRepo()
    service.retrieval = Retrieval()
    service.orchestrator = Orchestrator()
    service.config_loader = config_loader

    report = await service.generate("s1")

    assert report is cached

@pytest.mark.asyncio
async def test_concurrent_coach_report_generation_reuses_inflight_result():
    session_id = "concurrent-s1"
    state = SessionState(
        session_id=session_id,
        setup_ready=True,
        conversation=[ConversationTurn(turn_index=1, speaker="manager", text="我们来复盘绩效差距。")],
    )
    saved = {}
    run_calls = 0

    class SessionService:
        def get_session(self, requested_session_id):
            assert requested_session_id == session_id
            return state

        def save_session(self, next_state):
            saved["state"] = next_state
            return next_state

    class ReportRepo:
        def get_coach(self, requested_session_id):
            assert requested_session_id == session_id
            if "report" not in saved:
                raise KeyError(requested_session_id)
            return saved["report"]

        def save_coach(self, report):
            saved["report"] = report
            return report

    class Retrieval:
        def retrieve(self, task_id, context):
            return []

    class Orchestrator:
        async def run_task(self, task_id, next_state, retrieved_chunks):
            nonlocal run_calls
            run_calls += 1
            await asyncio.sleep(0.05)
            return CoachTaskResult(
                task_id=task_id,
                task_name=dict(COACH_TASK_SPECS)[task_id],
                score=4,
                summary="评估完成。",
            )

        @staticmethod
        def build_report(requested_session_id, task_results, *, locale="zh-CN"):
            return CoachOrchestrator.build_report(
                requested_session_id,
                task_results,
                locale=locale,
            )

    def make_service():
        service = CoachService()
        service.session_service = SessionService()
        service.report_repo = ReportRepo()
        service.retrieval = Retrieval()
        service.orchestrator = Orchestrator()
        return service

    first, second = await asyncio.gather(
        make_service().generate(session_id),
        make_service().generate(session_id),
    )

    assert run_calls == 4
    assert first is saved["report"]
    assert second is saved["report"]
    assert state.stage == "report_ready"
    assert state.coach_report_id == session_id



@pytest.mark.asyncio
async def test_coach_task_pipeline_starts_llm_before_other_retrievals_finish():
    state = SessionState(
        session_id="s1",
        setup_ready=True,
        conversation=[ConversationTurn(turn_index=1, speaker="manager", text="我们来复盘绩效差距。")],
    )
    active = 0
    max_active = 0
    lock = threading.Lock()
    retrieval_finished: dict[str, float] = {}
    llm_started: dict[str, float] = {}

    class Retrieval:
        def retrieve(self, task_id, context):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.02 if task_id == "opening_evaluation" else 0.12)
                return [
                    RetrievedChunk(
                        chunk_id=task_id,
                        source_id="source",
                        title="title",
                        scope="policy",
                        text="chunk",
                    )
                ]
            finally:
                with lock:
                    retrieval_finished[task_id] = time.monotonic()
                    active -= 1

    class Orchestrator:
        async def run_task(self, task_id, next_state, retrieved_chunks):
            llm_started[task_id] = time.monotonic()
            await asyncio.sleep(0.01)
            return CoachTaskResult(
                task_id=task_id,
                task_name=dict(COACH_TASK_SPECS)[task_id],
                score=4,
                summary="评估完成。",
            )

    service = CoachService()
    service.retrieval = Retrieval()
    service.orchestrator = Orchestrator()
    context = service._generation_context(state)

    outcomes = await asyncio.gather(
        *(
            service._run_task_pipeline("s1", state, context, task_id, task_name)
            for task_id, task_name in COACH_TASK_SPECS
        )
    )

    assert {outcome.task_id for outcome in outcomes} == {task_id for task_id, _ in COACH_TASK_SPECS}
    assert max_active > 1
    assert llm_started["opening_evaluation"] < min(
        retrieval_finished[task_id]
        for task_id, _ in COACH_TASK_SPECS
        if task_id != "opening_evaluation"
    )


@pytest.mark.asyncio
async def test_coach_generate_offloads_rag_preparation_to_worker_thread():
    session_id = "threaded-generate"
    state = SessionState(
        session_id=session_id,
        setup_ready=True,
        conversation=[ConversationTurn(turn_index=1, speaker="manager", text="请复盘本次面谈。")],
    )
    main_thread_id = threading.get_ident()
    retrieval_thread_ids: list[int] = []
    saved = {}

    class SessionService:
        def get_session(self, requested_session_id):
            assert requested_session_id == session_id
            return state

        def save_session(self, next_state):
            saved["state"] = next_state
            return next_state

    class ReportRepo:
        def save_coach(self, report):
            saved["report"] = report

    class Retrieval:
        def retrieve(self, task_id, context):
            retrieval_thread_ids.append(threading.get_ident())
            return []

    class Orchestrator:
        async def run_task(self, task_id, next_state, retrieved_chunks):
            return CoachTaskResult(
                task_id=task_id,
                task_name=dict(COACH_TASK_SPECS)[task_id],
                score=4,
                summary="评估完成。",
            )

        @staticmethod
        def build_report(requested_session_id, task_results, *, locale="zh-CN"):
            return CoachOrchestrator.build_report(
                requested_session_id,
                task_results,
                locale=locale,
            )

    service = CoachService()
    service.session_service = SessionService()
    service.report_repo = ReportRepo()
    service.retrieval = Retrieval()
    service.orchestrator = Orchestrator()

    report = await service.generate(session_id)

    assert report.session_id == session_id
    assert len(retrieval_thread_ids) == 4
    assert all(thread_id != main_thread_id for thread_id in retrieval_thread_ids)
    assert saved["report"] is report
    assert saved["state"] is state


@pytest.mark.asyncio
async def test_coach_stream_offloads_rag_preparation_to_worker_thread():
    session_id = "threaded-stream"
    state = SessionState(
        session_id=session_id,
        setup_ready=True,
        conversation=[ConversationTurn(turn_index=1, speaker="manager", text="请复盘本次面谈。")],
    )
    main_thread_id = threading.get_ident()
    retrieval_thread_ids: list[int] = []
    saved = {}

    class SessionService:
        def get_session(self, requested_session_id):
            assert requested_session_id == session_id
            return state

        def save_session(self, next_state):
            saved["state"] = next_state
            return next_state

    class ReportRepo:
        def save_coach(self, report):
            saved["report"] = report

    class Retrieval:
        def retrieve(self, task_id, context):
            retrieval_thread_ids.append(threading.get_ident())
            return []

    class Orchestrator:
        async def run_task(self, task_id, next_state, retrieved_chunks):
            return CoachTaskResult(
                task_id=task_id,
                task_name=dict(COACH_TASK_SPECS)[task_id],
                score=4,
                summary="评估完成。",
            )

        @staticmethod
        def build_report(requested_session_id, task_results, *, locale="zh-CN"):
            return CoachOrchestrator.build_report(
                requested_session_id,
                task_results,
                locale=locale,
            )

    service = CoachService()
    service.session_service = SessionService()
    service.report_repo = ReportRepo()
    service.retrieval = Retrieval()
    service.orchestrator = Orchestrator()

    events = [event async for event in service.stream_generate(session_id)]

    assert events[0] == {"event": "start", "cached": False}
    assert events[-1]["event"] == "done"
    assert len(retrieval_thread_ids) == 4
    assert all(thread_id != main_thread_id for thread_id in retrieval_thread_ids)


@pytest.mark.asyncio
async def test_coach_stream_emits_only_four_dimension_results_and_done():
    session_id = "direct-report-stream"
    state = SessionState(
        session_id=session_id,
        setup_ready=True,
        conversation=[ConversationTurn(turn_index=1, speaker="manager", text="请复盘本次面谈。")],
    )
    saved = {}
    task_names = dict(COACH_TASK_SPECS)

    class SessionService:
        def get_session(self, requested_session_id):
            assert requested_session_id == session_id
            return state

        def save_session(self, next_state):
            saved["state"] = next_state
            return next_state

    class ReportRepo:
        def save_coach(self, report):
            saved["report"] = report

    class Retrieval:
        def retrieve(self, task_id, context):
            return []

    class Orchestrator:
        async def run_task(self, task_id, next_state, retrieved_chunks):
            assert next_state is state
            return CoachTaskResult(
                task_id=task_id,
                task_name=task_names[task_id],
                score=4,
                summary="评估完成。",
                improvement_points=["需要补充事实。"],
                better_phrases=[
                    {
                        "suggestion": "我们先对齐事实。",
                        "reason": "让反馈更具体。",
                    }
                ],
            )

        @staticmethod
        def build_report(requested_session_id, task_results, *, locale="zh-CN"):
            return CoachOrchestrator.build_report(
                requested_session_id,
                task_results,
                locale=locale,
            )

    service = CoachService()
    service.session_service = SessionService()
    service.report_repo = ReportRepo()
    service.retrieval = Retrieval()
    service.orchestrator = Orchestrator()

    events = [event async for event in service.stream_generate(session_id)]
    event_names = [event["event"] for event in events]

    assert event_names.count("task_start") == 4
    assert event_names.count("task_done") == 4
    assert all("pipeline_ms" in event for event in events if event["event"] == "task_done")
    assert not any(name.startswith("section_") for name in event_names)
    assert event_names[-1] == "done"
    assert len(events[-1]["report"]["task_results"]) == 4
    assert len(saved["report"].task_results) == 4
    assert saved["state"].stage == "report_ready"


def test_business_rag_parallelism_stays_centralized_with_isolated_tool_limits():
    config = BusinessConfigLoader().query_config()

    assert all(
        "query_parallelism" not in query and "rerank_parallelism" not in query
        for name, query in config["queries"].items()
        if name not in {"intent_performance", "model_rag_tool", "resource_chat"}
    )
    assert config["queries"]["model_rag_tool"]["query_parallelism"] == 8
    assert config["queries"]["model_rag_tool"]["rerank_parallelism"] == 8
    assert config["queries"]["resource_chat"]["query_parallelism"] == 1
    assert config["queries"]["resource_chat"]["rerank_parallelism"] == 4


@pytest.mark.asyncio
async def test_coach_generate_resumes_completed_dimension_checkpoints():
    session_id = "checkpoint-resume"
    state = SessionState(
        session_id=session_id,
        setup_ready=True,
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text="请复盘本次面谈。")
        ],
    )
    task_names = dict(COACH_TASK_SPECS)
    checkpoint = CoachTaskResult(
        task_id="opening_evaluation",
        task_name=task_names["opening_evaluation"],
        status="insufficient_information",
        summary="已完成的检查点。",
    )
    retrieved: list[str] = []
    evaluated: list[str] = []
    checkpoint_writes: list[str] = []
    saved: dict[str, object] = {}

    class SessionService:
        def get_session(self, requested_session_id):
            assert requested_session_id == session_id
            return state

        def save_session(self, next_state):
            saved["state"] = next_state
            return next_state

    class ReportRepo:
        def get_coach_task_results(self, requested_session_id, input_fingerprint):
            assert requested_session_id == session_id
            assert len(input_fingerprint) == 64
            return {checkpoint.task_id: checkpoint}

        def save_coach_task_result(self, **kwargs):
            checkpoint_writes.append(kwargs["result"].task_id)

        def save_coach(self, report):
            saved["report"] = report

    class Retrieval:
        async def aretrieve(self, task_id, context):
            retrieved.append(task_id)
            return []

    class Orchestrator:
        async def run_task(self, task_id, next_state, retrieved_chunks):
            evaluated.append(task_id)
            return CoachTaskResult(
                task_id=task_id,
                task_name=task_names[task_id],
                status="insufficient_information",
                summary="本次执行完成。",
            )

        @staticmethod
        def build_report(requested_session_id, task_results, *, locale="zh-CN"):
            return CoachOrchestrator.build_report(
                requested_session_id,
                task_results,
                locale=locale,
            )

    service = CoachService(
        session_service=SessionService(),
        report_repo=ReportRepo(),
        retrieval=Retrieval(),
        orchestrator=Orchestrator(),
    )
    report = await service.generate(session_id)

    expected_pending = {
        task_id for task_id, _ in COACH_TASK_SPECS if task_id != checkpoint.task_id
    }
    assert set(retrieved) == expected_pending
    assert set(evaluated) == expected_pending
    assert set(checkpoint_writes) == expected_pending
    assert report.status == "success"
    assert saved["report"] is report
    assert state.stage == "report_ready"


@pytest.mark.asyncio
async def test_coach_stream_does_not_save_partial_report_and_marks_failed_task():
    session_id = "checkpoint-partial"
    state = SessionState(
        session_id=session_id,
        setup_ready=True,
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text="请复盘本次面谈。")
        ],
    )
    task_names = dict(COACH_TASK_SPECS)
    checkpoint_writes: list[str] = []

    class SessionService:
        def get_session(self, requested_session_id):
            return state

        def save_session(self, next_state):
            raise AssertionError("partial report must not mark the session ready")

    class ReportRepo:
        def get_coach_task_results(self, requested_session_id, input_fingerprint):
            return {}

        def save_coach_task_result(self, **kwargs):
            checkpoint_writes.append(kwargs["result"].task_id)

        def save_coach(self, report):
            raise AssertionError("partial report must not be persisted as final")

    class Retrieval:
        async def aretrieve(self, task_id, context):
            return []

    class Orchestrator:
        async def run_task(self, task_id, next_state, retrieved_chunks):
            return CoachTaskResult(
                task_id=task_id,
                task_name=task_names[task_id],
                status=("failed" if task_id == "emotion_evaluation" else "success"),
                score=None if task_id == "emotion_evaluation" else 4,
                summary="评估失败。" if task_id == "emotion_evaluation" else "评估完成。",
            )

        @staticmethod
        def build_report(requested_session_id, task_results, *, locale="zh-CN"):
            return CoachOrchestrator.build_report(
                requested_session_id,
                task_results,
                locale=locale,
            )

    service = CoachService(
        session_service=SessionService(),
        report_repo=ReportRepo(),
        retrieval=Retrieval(),
        orchestrator=Orchestrator(),
    )
    events = [event async for event in service.stream_generate(session_id)]

    failed_events = [event for event in events if event["event"] == "task_error"]
    assert [event["task_id"] for event in failed_events] == ["emotion_evaluation"]
    assert events[-1]["event"] == "done"
    assert events[-1]["complete"] is False
    assert state.stage != "report_ready"
    assert set(checkpoint_writes) == {task_id for task_id, _ in COACH_TASK_SPECS}

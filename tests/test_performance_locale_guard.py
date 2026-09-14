from types import SimpleNamespace

import pytest

from backend.agents.employee_agent import EmployeeAgent
from backend.exceptions.workflow_errors import SetupNotReadyError
from backend.schemas.conversation import ConversationTurn
from backend.schemas.intent import IntentGoalPerformanceItem, IntentResult
from backend.schemas.state import SessionState
from backend.services import coach_service as coach_service_module
from backend.services import guidance_service as guidance_service_module
from backend.services.coach_service import CoachService
from backend.services.guidance_service import GuidanceService
from backend.services.session_service import SessionService
from backend.workflows.guards import ensure_rehearsal_allowed


class _StateRepository:
    def __init__(self, state: SessionState):
        self.state = state

    def get(self, session_id: str) -> SessionState:
        assert session_id == self.state.session_id
        return self.state

    def save(self, state: SessionState) -> SessionState:
        self.state = state
        return state


def _switched_state() -> tuple[SessionState, SessionService]:
    original = SessionState(
        session_id="performance-locale-guard",
        locale="zh-CN",
        stage="report_ready",
        setup_ready=True,
        run_mode="rehearsal_report",
        guidance_report_id="guidance-zh",
        coach_report_id="coach-zh",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text="我们来复盘。")
        ],
        intent=IntentResult(
            intent_id="development",
            performance_locale="zh-CN",
            performance_context="经理确认过的中文业务表现",
            performance_items=[
                IntentGoalPerformanceItem(
                    goal="目标达成总览",
                    current_performance="经理确认过的具体结果",
                )
            ],
        ),
    )
    repository = _StateRepository(original)
    service = SessionService(
        repo=repository,
        runtime_recycler=SimpleNamespace(),
    )
    return service.update_locale(original.session_id, locale="en"), service


def test_locale_switch_preserves_confirmed_performance_but_blocks_employee() -> None:
    state, service = _switched_state()

    assert state.intent is not None
    assert state.intent.performance_locale == "zh-CN"
    assert state.intent.performance_context == "经理确认过的中文业务表现"
    assert state.intent.performance_items[0].current_performance == "经理确认过的具体结果"

    with pytest.raises(SetupNotReadyError, match="current language"):
        ensure_rehearsal_allowed(state)
    with pytest.raises(SetupNotReadyError, match="current language"):
        EmployeeAgent._build_retrieval_context(state, "Please respond.")
    with pytest.raises(SetupNotReadyError, match="current language"):
        EmployeeAgent._build_reply_prompt(
            state,
            "Please respond.",
            test_prompt_enabled=False,
        )

    restored = service.update_locale(state.session_id, locale="zh-CN")
    ensure_rehearsal_allowed(restored)
    assert restored.intent is not None
    assert restored.intent.performance_context == "经理确认过的中文业务表现"


@pytest.mark.asyncio
async def test_guidance_and_coach_reject_mismatched_performance_before_generation() -> None:
    state, _service = _switched_state()
    session_service = SimpleNamespace(get_session=lambda _session_id: state)

    guidance = GuidanceService.__new__(GuidanceService)
    guidance.executor = None
    guidance.session_service = session_service
    with pytest.raises(SetupNotReadyError, match="current language"):
        await guidance.generate(state.session_id)

    coach = CoachService.__new__(CoachService)
    coach.executor = None
    coach.session_service = session_service
    with pytest.raises(SetupNotReadyError, match="current language"):
        await coach.generate(state.session_id)


@pytest.mark.asyncio
async def test_streams_emit_locale_guard_error_without_starting_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _service = _switched_state()
    session_service = SimpleNamespace(get_session=lambda _session_id: state)

    async def _run_db_direct(_executor, _operation, func, *args):
        return func(*args)

    monkeypatch.setattr(
        guidance_service_module,
        "run_db_with_context",
        _run_db_direct,
    )
    monkeypatch.setattr(
        coach_service_module,
        "run_db_with_context",
        _run_db_direct,
    )

    guidance = GuidanceService.__new__(GuidanceService)
    guidance.executor = None
    guidance.session_service = session_service
    guidance_events = [
        event async for event in guidance.stream_generate(state.session_id)
    ]

    coach = CoachService.__new__(CoachService)
    coach.executor = None
    coach.session_service = session_service
    coach_events = [event async for event in coach.stream_generate(state.session_id)]

    assert guidance_events == [
        {
            "event": "error",
            "message": (
                "The session language has changed. Confirm the employee "
                "performance content in the current language before continuing."
            ),
        }
    ]
    assert coach_events == guidance_events

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.business_config.loader import get_config_loader
from backend.schemas.intent import (
    PERFORMANCE_SECTION_TITLES,
    IntentGoalPerformanceItem,
    IntentResult,
    build_performance_context,
)
from backend.schemas.profile import EmployeeProfile
from backend.schemas.simulation import (
    BigFivePersonality,
    EmployeeStateTransitionStructuredOutput,
    EmotionState,
    EmotionTransitionStructuredOutput,
    MotivationState,
    PatternResponseGuidance,
    PsychologicalPatternActivation,
    PsychologicalPatternActivationDecision,
    PsychologicalPatternDynamicsStructuredOutput,
    PsychologicalPatternInteraction,
    PsychologicalPatternState,
    VADVector,
)
from backend.schemas.state import SessionState
from backend.services.rehearsal_service import RehearsalService
from backend.services.setup_service import SetupService


def _pattern_state() -> PsychologicalPatternState:
    return PsychologicalPatternState(
        last_processed_manager_turn=3,
        patterns=[
            PsychologicalPatternActivation(
                pattern_id="egocentric bias",
                strength="medium_high",
                change="maintained",
                trigger_type="negative_capability_evaluation",
                trigger_turn=3,
            )
        ],
        interactions=[],
    )


class _MemorySessionService:
    def __init__(self, state: SessionState) -> None:
        self.state = state
        self.saved: list[SessionState] = []

    def get_session(self, _session_id: str) -> SessionState:
        return self.state

    def save_session(self, state: SessionState) -> SessionState:
        self.state = state
        self.saved.append(state)
        return state


class _IntentAgent:
    @staticmethod
    async def recognize(**kwargs) -> IntentResult:
        return IntentResult(intent_id=kwargs["intent_id"], confidence=1.0)


class _InitialEmotionTransition:
    @staticmethod
    def initial_state(**_kwargs) -> EmotionState:
        return EmotionState()


class _SummaryReset:
    def __init__(self) -> None:
        self.session_ids: list[str] = []

    def reset_generation(self, session_id: str) -> None:
        self.session_ids.append(session_id)


class _RuntimeLeaseRelease:
    def __init__(self) -> None:
        self.session_ids: list[str] = []

    def release_session_lease(self, session_id: str) -> None:
        self.session_ids.append(session_id)


class _RetryInitialEmotionTransition:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, BigFivePersonality | None]] = []
        self.initial_emotion = EmotionState(
            current_vad=VADVector(valence=0.12, arousal=-0.08, dominance=0.16),
            current_anchor_id="neutral",
            previous_anchor_id="neutral",
            last_reason_summary="重新初始化",
        )

    def initial_state(
        self,
        intent_id: str | None,
        personality: BigFivePersonality | None = None,
    ) -> EmotionState:
        self.calls.append((intent_id, personality))
        return self.initial_emotion.model_copy(deep=True)


def _employee_profile(*, role: str = "软件工程师") -> EmployeeProfile:
    return EmployeeProfile(
        employee_id="employee-1",
        employee_alias="测试员工",
        role=role,
        review_cycle="2026 H1",
        conversation_topic="绩效反馈",
        performance_rating="4",
        key_goals=["按计划完成项目交付"],
    )


def _performance_items() -> list[IntentGoalPerformanceItem]:
    return [
        IntentGoalPerformanceItem(
            goal=title,
            current_performance=f"{title}的已确认内容。",
        )
        for title in PERFORMANCE_SECTION_TITLES
    ]


def _setup_service(state: SessionState) -> SetupService:
    service = object.__new__(SetupService)
    service.session_service = _MemorySessionService(state)
    service.intent_agent = _IntentAgent()
    service.loader = get_config_loader()
    service.executor = None
    service.emotion_transition = _InitialEmotionTransition()
    return service


def test_pattern_state_is_hidden_from_api_but_round_trips_in_persistence():
    state = SessionState(
        session_id="pattern-private",
        psychological_pattern_state=_pattern_state(),
    )

    assert "psychological_pattern_state" not in state.model_dump(mode="json")
    assert "psychological_pattern_state" not in (
        SessionState.model_json_schema(mode="serialization")["properties"]
    )

    app = FastAPI()

    @app.get("/state", response_model=SessionState)
    def get_state() -> SessionState:
        return state

    with TestClient(app) as client:
        response = client.get("/state")
    assert response.status_code == 200
    assert "psychological_pattern_state" not in response.json()

    payload = state.persistence_payload()
    assert payload["psychological_pattern_state"] == {
        "last_processed_manager_turn": 3,
        "patterns": [
            {
                "pattern_id": "egocentric bias",
                "strength": "medium_high",
                "change": "maintained",
                "trigger_type": "negative_capability_evaluation",
                "trigger_turn": 3,
            }
        ],
        "interactions": [],
    }
    restored = SessionState.model_validate(payload)
    assert restored.psychological_pattern_state == state.psychological_pattern_state


def test_pattern_state_can_be_reset_to_none_without_legacy_session_breakage():
    state = SessionState(
        session_id="pattern-reset",
        psychological_pattern_state=_pattern_state(),
    )

    state.psychological_pattern_state = None

    assert "psychological_pattern_state" not in state.persistence_payload()
    legacy = SessionState.model_validate({"session_id": "legacy-no-pattern-state"})
    assert legacy.psychological_pattern_state is None


def test_confirm_profile_preserves_equal_profile_state_and_resets_on_change():
    profile = _employee_profile()
    state = SessionState(
        session_id="profile-pattern-reset",
        employee_profile=profile,
        psychological_pattern_state=_pattern_state(),
    )
    service = _setup_service(state)

    unchanged = service.confirm_profile(
        state.session_id,
        profile.model_copy(deep=True),
    )
    assert unchanged.psychological_pattern_state == _pattern_state()

    changed = service.confirm_profile(
        state.session_id,
        _employee_profile(role="高级软件工程师"),
    )
    assert changed.psychological_pattern_state is None


@pytest.mark.asyncio
async def test_confirm_intent_or_performance_change_resets_pattern_state():
    items = _performance_items()
    state = SessionState(
        session_id="intent-pattern-reset",
        employee_profile=_employee_profile(),
        intent=IntentResult(
            intent_id="improvement",
            performance_items=items,
            performance_context=build_performance_context(items),
        ),
        psychological_pattern_state=_pattern_state(),
    )
    service = _setup_service(state)

    unchanged = await service.confirm_intent(
        state.session_id,
        intent_id="improvement",
        performance_items=items,
    )
    assert unchanged.psychological_pattern_state == _pattern_state()

    changed_items = [
        *items[:-1],
        items[-1].model_copy(
            update={
                "current_performance": (
                    items[-1].current_performance + " 新增已确认事实。"
                )
            }
        ),
    ]
    performance_changed = await service.confirm_intent(
        state.session_id,
        intent_id="improvement",
        performance_items=changed_items,
    )
    assert performance_changed.psychological_pattern_state is None

    performance_changed.psychological_pattern_state = _pattern_state()
    intent_changed = await service.confirm_intent(
        state.session_id,
        intent_id="exit",
        performance_items=changed_items,
    )
    assert intent_changed.psychological_pattern_state is None


def test_confirm_simulation_resets_pattern_state():
    state = SessionState(
        session_id="simulation-pattern-reset",
        psychological_pattern_state=_pattern_state(),
    )
    service = _setup_service(state)

    result = service.confirm_simulation(
        state.session_id,
        personality=BigFivePersonality(),
        primary_motive_id="commerce",
        secondary_motive_ids=["recognition"],
    )

    assert result.psychological_pattern_state is None


def test_retry_rehearsal_resets_all_dynamic_simulation_state():
    personality = BigFivePersonality(
        openness=72,
        conscientiousness=66,
        extraversion=41,
        agreeableness=28,
        neuroticism=63,
    )
    state = SessionState(
        session_id="retry-pattern-reset",
        stage="rehearsal",
        run_mode="rehearsal_report",
        intent=IntentResult(intent_id="improvement"),
        personality=personality,
        motivation=MotivationState(
            primary_motive_id="commerce",
            secondary_motive_ids=["recognition"],
            primary_score=81.0,
            secondary_scores={"recognition": 17.0},
            last_change_reason="旧对话改变了动机满足度",
            has_manager_response=True,
        ),
        emotion_state=EmotionState(
            current_vad=VADVector(
                valence=-0.74,
                arousal=0.82,
                dominance=-0.61,
            ),
            current_anchor_id="anxious",
            previous_anchor_id="neutral",
            last_vad_delta=VADVector(
                valence=-0.4,
                arousal=0.5,
                dominance=-0.3,
            ),
            has_manager_response=True,
        ),
        psychological_pattern_state=_pattern_state(),
    )
    sessions = _MemorySessionService(state)
    summaries = _SummaryReset()
    leases = _RuntimeLeaseRelease()
    emotions = _RetryInitialEmotionTransition()
    service = object.__new__(RehearsalService)
    service.session_service = sessions
    service.summary_service = summaries
    service.runtime_recycler = leases
    service.emotion_transition = emotions

    result = service.retry_rehearsal(state.session_id)

    assert result.psychological_pattern_state is None
    assert result.motivation is not None
    assert result.motivation.primary_motive_id == "commerce"
    assert result.motivation.secondary_motive_ids == ["recognition"]
    assert result.motivation.primary_score == 50.0
    assert result.motivation.secondary_scores == {"recognition": 50.0}
    assert result.motivation.total_satisfaction == 50.0
    assert result.motivation.last_change_reason is None
    assert result.motivation.has_manager_response is False
    assert result.emotion_state == emotions.initial_emotion
    assert result.emotion_state.has_manager_response is False
    assert result.emotion_state.last_vad_delta == VADVector()
    assert emotions.calls == [("improvement", personality)]
    assert sessions.saved == [result]
    assert summaries.session_ids == [state.session_id]
    assert leases.session_ids == [state.session_id]


def test_clear_runtime_context_resets_pattern_state():
    state = SessionState(
        session_id="runtime-context-pattern-reset",
        setup_ready=True,
        run_mode="rehearsal_report",
        psychological_pattern_state=_pattern_state(),
    )
    sessions = _MemorySessionService(state)
    summaries = _SummaryReset()
    service = object.__new__(RehearsalService)
    service.session_service = sessions
    service.summary_service = summaries

    result = service.update_runtime_context(
        state.session_id,
        clear_context=True,
    )

    assert result.psychological_pattern_state is None
    assert sessions.saved == [result]
    assert summaries.session_ids == [state.session_id]


def test_pattern_model_output_schema_only_contains_consumed_fields():
    decision_schema = PsychologicalPatternActivationDecision.model_json_schema()
    dynamics_schema = PsychologicalPatternDynamicsStructuredOutput.model_json_schema()
    guidance_schema = PatternResponseGuidance.model_json_schema()

    assert set(decision_schema["properties"]) == {
        "pattern_id",
        "strength",
        "trigger_type",
        "major_change_justified",
    }
    assert set(dynamics_schema["properties"]) == {
        "major_new_information",
        "patterns",
        "interactions",
        "response_guidance",
    }
    assert guidance_schema["properties"]["main_psychological_activity"][
        "maxLength"
    ] == 200
    assert guidance_schema["properties"]["response_tendency"]["maxLength"] == 80
    assert guidance_schema["properties"]["expression_guidance"]["maxLength"] == 300
    assert guidance_schema["properties"]["checks"]["maxItems"] == 2
    assert guidance_schema["properties"]["checks"]["items"]["maxLength"] == 120
    assert "不得把它当成员工回复的内容议程" in guidance_schema["properties"][
        "main_psychological_activity"
    ]["description"]
    assert "不得新增话题" in guidance_schema["properties"]["response_tendency"][
        "description"
    ]
    assert "语气、直接程度、信息密度和承诺边界" in guidance_schema[
        "properties"
    ]["expression_guidance"]["description"]
    assert "禁止性边界检查" in guidance_schema["properties"]["checks"][
        "description"
    ]


def test_unified_transition_adds_patterns_without_changing_emotion_contract():
    emotion_schema = EmotionTransitionStructuredOutput.model_json_schema()
    unified_schema = EmployeeStateTransitionStructuredOutput.model_json_schema()

    assert "pattern_dynamics" not in emotion_schema["properties"]
    assert "pattern_dynamics" in unified_schema["properties"]

    result = EmployeeStateTransitionStructuredOutput(
        vad_delta=VADVector(valence=-0.2, arousal=0.1, dominance=-0.1),
        transition_strategy="expected_value",
        reason_summary="情绪变化保持渐进。",
        pattern_dynamics=PsychologicalPatternDynamicsStructuredOutput(
            major_new_information=True,
            patterns=[
                PsychologicalPatternActivationDecision(
                    pattern_id="egocentric bias",
                    strength="strong",
                    trigger_type="negative_capability_evaluation",
                    major_change_justified=True,
                )
            ],
            interactions=[
                PsychologicalPatternInteraction(
                    from_pattern_id="egocentric bias",
                    to_pattern_id="self-serving bias",
                    type="reinforce",
                )
            ],
            response_guidance=PatternResponseGuidance(
                main_psychological_activity="维护专业能力评价",
                response_tendency="reserve",
                expression_guidance="承认具体事实，但保留对整体评价的意见。",
                checks=["不扩大局部接受", "不重复旧争议"],
            ),
        ),
    )

    assert result.vad_delta.valence == -0.2
    assert result.pattern_dynamics.major_new_information is True
    assert result.pattern_dynamics.patterns[0].major_change_justified is True


def test_pattern_state_enforces_compact_collection_bounds():
    pattern = PsychologicalPatternActivation(
        pattern_id="active",
        strength="weak",
        change="first_activated",
        trigger_type="manager_statement",
        trigger_turn=1,
    )

    with pytest.raises(ValidationError):
        PsychologicalPatternState(patterns=[pattern] * 6)

    interaction = PsychologicalPatternInteraction(
        from_pattern_id="active",
        to_pattern_id="careful",
        type="modulate",
    )
    with pytest.raises(ValidationError):
        PsychologicalPatternState(interactions=[interaction] * 5)

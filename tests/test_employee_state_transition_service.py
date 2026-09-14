from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

import backend.services.employee_state_transition_service as transition_module
from backend.schemas.conversation import ConversationTurn
from backend.schemas.conversation_summary import ConversationPromptContext
from backend.schemas.intent import IntentConfig, IntentResult
from backend.schemas.profile import EmployeeProfile
from backend.schemas.simulation import (
    MotivationState,
    PatternResponseGuidance,
    PsychologicalPatternActivation,
    PsychologicalPatternActivationDecision,
    PsychologicalPatternDynamicsStructuredOutput,
    PsychologicalPatternInteraction,
    PsychologicalPatternState,
)
from backend.schemas.state import SessionState
from backend.services.emotion_transition_service import EmotionTransitionService
from backend.services.employee_state_transition_service import (
    EmployeeStateTransitionService,
)
from backend.services.psychological_pattern_catalog import (
    PsychologicalPatternCatalogSnapshot,
)
from backend.workflows.nodes import RehearsalNodes


class _ImmediateScheduler:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @asynccontextmanager
    async def slot(self, **kwargs):
        self.calls.append(kwargs)
        yield SimpleNamespace(queue_ms=0.0)


def _service() -> EmployeeStateTransitionService:
    service = object.__new__(EmployeeStateTransitionService)
    service.model_scheduler = _ImmediateScheduler()
    return service


def _catalog(*pattern_ids: str) -> PsychologicalPatternCatalogSnapshot:
    payload = [
        {
            "pattern_id": pattern_id,
            "construct_name": f"name of {pattern_id}",
            "description": f"description of {pattern_id}",
            "core_mechanisms": f"mechanism of {pattern_id}",
            "real_world_manifestation": f"manifestation of {pattern_id}",
        }
        for pattern_id in pattern_ids
    ]
    return PsychologicalPatternCatalogSnapshot(
        available=True,
        source_mtime_ns=1,
        catalog_version="test-version",
        render_patterns=json.dumps(payload, ensure_ascii=False),
        valid_pattern_ids=frozenset(pattern_ids),
    )


def _decision(
    pattern_id: str,
    *,
    strength: str = "medium",
    major_change_justified: bool = False,
) -> PsychologicalPatternActivationDecision:
    return PsychologicalPatternActivationDecision(
        pattern_id=pattern_id,
        strength=strength,
        trigger_type="manager_feedback",
        major_change_justified=major_change_justified,
    )


def _activation(
    pattern_id: str,
    *,
    strength: str,
    trigger_turn: int = 1,
) -> PsychologicalPatternActivation:
    return PsychologicalPatternActivation(
        pattern_id=pattern_id,
        strength=strength,
        change="maintained",
        trigger_type="manager_feedback",
        trigger_turn=trigger_turn,
    )


def _dynamics(
    *,
    patterns: list[PsychologicalPatternActivationDecision] | None = None,
    interactions: list[PsychologicalPatternInteraction] | None = None,
    major_new_information: bool = False,
    guidance: PatternResponseGuidance | None = None,
) -> PsychologicalPatternDynamicsStructuredOutput:
    return PsychologicalPatternDynamicsStructuredOutput(
        major_new_information=major_new_information,
        patterns=patterns or [],
        interactions=interactions or [],
        response_guidance=guidance or PatternResponseGuidance(),
    )


@pytest.mark.parametrize("selection_count", [0, 1, 5])
def test_pattern_selection_supports_zero_one_and_five(selection_count):
    pattern_ids = [f"pattern-{index}" for index in range(5)]
    state = SessionState(session_id="selection")

    guidance, active_count, interaction_count = _service()._apply_pattern_dynamics(
        state,
        _dynamics(
            patterns=[_decision(pattern_id) for pattern_id in pattern_ids[:selection_count]]
        ),
        _catalog(*pattern_ids),
        manager_turn=2,
    )

    assert active_count == selection_count
    assert interaction_count == 0
    assert len(state.psychological_pattern_state.patterns) == selection_count
    # An active internal Pattern does not by itself create a Reply agenda.
    assert guidance == ""


def test_payload_normalization_caps_patterns_at_five_and_interactions_at_four():
    service = _service()
    raw_patterns = [
        {
            "pattern_id": f"pattern-{index}",
            "strength": "medium",
            "trigger_type": "manager_feedback",
            "change": "strengthened",
            "trigger_turn": 99,
            "trigger": "旧版临时触发说明",
            "behavioral_tendency": "旧版临时行为倾向",
            "major_change_justified": True,
        }
        for index in range(7)
    ]
    raw_interactions = [
        {
            "from_pattern_id": "pattern-0",
            "to_pattern_id": f"pattern-{index}",
            "type": "reinforce",
        }
        for index in range(1, 7)
    ]

    normalized, _ = service._normalize_pattern_payload(
        {
            "event_interpretation": "旧版事件解释",
            "patterns": raw_patterns,
            "interactions": raw_interactions,
            "response_guidance": {
                "main_psychological_activity": "主" * 250,
                "response_tendency": "倾" * 100,
                "expression_guidance": "表" * 350,
                "checks": ["检" * 150 for _ in range(4)],
            },
        },
        manager_turn=3,
    )

    assert [item["pattern_id"] for item in normalized["patterns"]] == [
        f"pattern-{index}" for index in range(5)
    ]
    assert set(normalized["patterns"][0]) == {
        "pattern_id",
        "strength",
        "trigger_type",
        "major_change_justified",
    }
    assert "event_interpretation" not in normalized
    assert len(normalized["interactions"]) == 4
    guidance = normalized["response_guidance"]
    assert len(guidance["main_psychological_activity"]) == 200
    assert len(guidance["response_tendency"]) == 80
    assert len(guidance["expression_guidance"]) == 300
    assert len(guidance["checks"]) == 2
    assert all(len(item) == 120 for item in guidance["checks"])


def test_unknown_pattern_ids_are_discarded():
    state = SessionState(session_id="unknown")

    _, active_count, _ = _service()._apply_pattern_dynamics(
        state,
        _dynamics(patterns=[_decision("known"), _decision("invented")]),
        _catalog("known"),
        manager_turn=2,
    )

    assert active_count == 1
    assert [item.pattern_id for item in state.psychological_pattern_state.patterns] == [
        "known"
    ]


def test_first_activation_may_start_at_strong():
    state = SessionState(session_id="first-strong")

    _service()._apply_pattern_dynamics(
        state,
        _dynamics(patterns=[_decision("known", strength="strong")]),
        _catalog("known"),
        manager_turn=2,
    )

    activation = state.psychological_pattern_state.patterns[0]
    assert activation.strength == "strong"
    assert activation.change == "first_activated"


@pytest.mark.parametrize(
    (
        "previous_strength",
        "requested_strength",
        "major_new_information",
        "major_change_justified",
        "expected_strength",
        "expected_change",
    ),
    [
        ("weak", "strong", False, False, "medium", "strengthened"),
        ("strong", "weak", False, False, "medium_high", "weakened"),
        ("weak", "strong", True, True, "medium_high", "strengthened"),
        ("strong", "weak", True, True, "medium", "weakened"),
        ("weak", "strong", True, False, "medium", "strengthened"),
    ],
)
def test_existing_strength_changes_are_bounded(
    previous_strength,
    requested_strength,
    major_new_information,
    major_change_justified,
    expected_strength,
    expected_change,
):
    state = SessionState(
        session_id="bounded",
        psychological_pattern_state=PsychologicalPatternState(
            last_processed_manager_turn=1,
            patterns=[_activation("known", strength=previous_strength)],
        ),
    )

    _service()._apply_pattern_dynamics(
        state,
        _dynamics(
            patterns=[
                _decision(
                    "known",
                    strength=requested_strength,
                    major_change_justified=major_change_justified,
                )
            ],
            major_new_information=major_new_information,
        ),
        _catalog("known"),
        manager_turn=2,
    )

    activation = state.psychological_pattern_state.patterns[0]
    assert activation.strength == expected_strength
    assert activation.change == expected_change


def test_untriggered_pattern_is_retained_without_automatic_decay():
    state = SessionState(
        session_id="maintain",
        psychological_pattern_state=PsychologicalPatternState(
            last_processed_manager_turn=3,
            patterns=[_activation("known", strength="strong", trigger_turn=1)],
        ),
    )

    _service()._apply_pattern_dynamics(
        state,
        _dynamics(),
        _catalog("known"),
        manager_turn=4,
    )

    activation = state.psychological_pattern_state.patterns[0]
    assert activation.strength == "strong"
    assert activation.change == "maintained"
    assert activation.trigger_turn == 1


def test_inactive_pattern_with_a_new_trigger_is_marked_reactivated():
    state = SessionState(
        session_id="reactivated",
        psychological_pattern_state=PsychologicalPatternState(
            last_processed_manager_turn=4,
            patterns=[_activation("known", strength="medium", trigger_turn=2)],
        ),
    )

    _service()._apply_pattern_dynamics(
        state,
        _dynamics(patterns=[_decision("known", strength="medium_high")]),
        _catalog("known"),
        manager_turn=5,
    )

    activation = state.psychological_pattern_state.patterns[0]
    assert activation.change == "reactivated"
    assert activation.strength == "medium_high"
    assert activation.trigger_turn == 5


def test_interactions_only_connect_active_patterns_and_reject_self_or_duplicates():
    state = SessionState(session_id="interactions")
    interactions = [
        PsychologicalPatternInteraction(
            from_pattern_id="one", to_pattern_id="two", type="reinforce"
        ),
        PsychologicalPatternInteraction(
            from_pattern_id="one", to_pattern_id="one", type="modulate"
        ),
        PsychologicalPatternInteraction(
            from_pattern_id="one", to_pattern_id="inactive", type="conflict"
        ),
        PsychologicalPatternInteraction(
            from_pattern_id="one", to_pattern_id="two", type="reinforce"
        ),
    ]

    _, _, interaction_count = _service()._apply_pattern_dynamics(
        state,
        _dynamics(
            patterns=[_decision("one"), _decision("two")],
            interactions=interactions,
        ),
        _catalog("one", "two", "inactive"),
        manager_turn=2,
    )

    assert interaction_count == 1
    assert state.psychological_pattern_state.interactions == [interactions[0]]


def test_removed_catalog_ids_and_their_interactions_are_pruned():
    state = SessionState(
        session_id="prune",
        psychological_pattern_state=PsychologicalPatternState(
            last_processed_manager_turn=3,
            patterns=[
                _activation("retained", strength="medium"),
                _activation("removed", strength="strong"),
            ],
            interactions=[
                PsychologicalPatternInteraction(
                    from_pattern_id="retained",
                    to_pattern_id="removed",
                    type="conflict",
                )
            ],
        ),
    )

    _service()._prune_removed_catalog_patterns(state, frozenset({"retained"}))

    assert [item.pattern_id for item in state.psychological_pattern_state.patterns] == [
        "retained"
    ]
    assert state.psychological_pattern_state.interactions == []


def test_response_guidance_does_not_expose_pattern_identity_or_strength():
    pattern_id = "reactance"
    catalog = PsychologicalPatternCatalogSnapshot(
        available=True,
        render_patterns=json.dumps(
            [
                {
                    "pattern_id": pattern_id,
                    "construct_name": "Psychological Reactance",
                    "description": "description",
                    "core_mechanisms": "mechanism",
                    "real_world_manifestation": "manifestation",
                }
            ]
        ),
        valid_pattern_ids=frozenset({pattern_id}),
    )
    state = SessionState(session_id="guidance")
    dynamics = _dynamics(
        patterns=[_decision(pattern_id, strength="strong")],
        guidance=PatternResponseGuidance(
            main_psychological_activity=(
                "Psychological Reactance 与 reactance 目前是 strong"
            ),
            response_tendency="保持 medium_high 的 reactance",
            expression_guidance="不要说 Psychological Reactance",
            checks=["确认 reactance 不是 weak"],
        ),
    )

    guidance, _, _ = _service()._apply_pattern_dynamics(
        state,
        dynamics,
        catalog,
        manager_turn=2,
    )

    assert "main_psychological_activity" not in json.loads(guidance)
    lowered = guidance.casefold()
    assert pattern_id not in lowered
    assert "psychological reactance" not in lowered
    for strength in ("weak", "medium", "medium_high", "strong"):
        assert strength not in lowered


def test_internal_main_activity_alone_is_not_sent_to_employee_reply():
    state = SessionState(session_id="internal-main-only")

    guidance, active_count, _ = _service()._apply_pattern_dynamics(
        state,
        _dynamics(
            patterns=[_decision("known")],
            guidance=PatternResponseGuidance(
                main_psychological_activity="在内部维持对事实的判断",
            ),
        ),
        _catalog("known"),
        manager_turn=2,
    )

    assert active_count == 1
    assert guidance == ""


def test_response_guidance_is_rebounded_after_sensitive_term_expansion():
    pattern_id = "x"
    catalog = PsychologicalPatternCatalogSnapshot(
        available=True,
        render_patterns=json.dumps(
            [
                {
                    "pattern_id": pattern_id,
                    "construct_name": pattern_id,
                    "description": "description",
                    "core_mechanisms": "mechanism",
                    "real_world_manifestation": "manifestation",
                }
            ]
        ),
        valid_pattern_ids=frozenset({pattern_id}),
    )
    guidance = PatternResponseGuidance(
        main_psychological_activity=pattern_id * 200,
        response_tendency=pattern_id * 80,
        expression_guidance=pattern_id * 300,
        checks=[pattern_id * 120, pattern_id * 120],
    )

    sanitized = _service()._sanitized_guidance(
        guidance,
        catalog,
        {pattern_id},
    )

    assert sanitized is not None
    assert len(sanitized.main_psychological_activity) <= 200
    assert len(sanitized.response_tendency) <= 80
    assert len(sanitized.expression_guidance) <= 300
    assert len(sanitized.checks) <= 2
    assert all(len(item) <= 120 for item in sanitized.checks)


class _DelegatingEmotionTransition:
    def __init__(self, result: SessionState):
        self.result = result
        self.calls: list[tuple[SessionState, str]] = []

    async def update_after_manager_message(
        self,
        state: SessionState,
        manager_message: str,
    ) -> SessionState:
        self.calls.append((state, manager_message))
        return self.result


class _RecordingEmotionTransition:
    def __init__(self, reason: str = "emotion branch") -> None:
        self.reason = reason
        self.calls: list[tuple[SessionState, str]] = []

    async def update_after_manager_message(
        self,
        state: SessionState,
        manager_message: str,
    ) -> SessionState:
        self.calls.append((state, manager_message))
        if state.emotion_state is not None:
            state.emotion_state.last_reason_summary = self.reason
        return state


class _FailIfReadCatalog:
    async def asnapshot(self):
        raise AssertionError("catalog must not be read while the feature is disabled")


@pytest.mark.asyncio
async def test_disabled_feature_fully_delegates_to_existing_emotion_flow():
    incoming = SessionState(session_id="incoming")
    delegated = SessionState(session_id="delegated")
    emotion = _DelegatingEmotionTransition(delegated)
    service = _service()
    service.settings = _parallel_settings(enabled=False)
    service.emotion_transition = emotion
    service.catalog = _FailIfReadCatalog()

    result = await service.update_after_manager_message(incoming, "manager text")

    assert emotion.calls == [(incoming, "manager text")]
    assert result.state is delegated
    assert result.pattern_response_guidance == ""
    assert result.pattern_dynamics_applied is False


@pytest.mark.asyncio
async def test_unavailable_catalog_does_not_block_existing_emotion_flow():
    class UnavailableCatalog:
        calls = 0

        async def asnapshot(self):
            self.calls += 1
            return PsychologicalPatternCatalogSnapshot(available=False)

    incoming = SessionState(session_id="incoming")
    delegated = SessionState(session_id="delegated")
    emotion = _DelegatingEmotionTransition(delegated)
    catalog = UnavailableCatalog()
    service = _service()
    service.settings = _parallel_settings()
    service.emotion_transition = emotion
    service.catalog = catalog

    result = await service.update_after_manager_message(incoming, "manager text")

    assert catalog.calls == 1
    assert emotion.calls == [(incoming, "manager text")]
    assert result.state is delegated
    assert result.pattern_response_guidance == ""
    assert result.pattern_dynamics_applied is False

class _StaticCatalog:
    def __init__(self, snapshot: PsychologicalPatternCatalogSnapshot):
        self.snapshot = snapshot
        self.calls = 0

    async def asnapshot(self) -> PsychologicalPatternCatalogSnapshot:
        self.calls += 1
        return self.snapshot


class _VisibleSummary:
    async def context_for_reply(self, state: SessionState) -> ConversationPromptContext:
        return ConversationPromptContext(
            summary_text="仅包含更早轮次的摘要",
            raw_turns=list(state.conversation),
        )


def _parallel_settings(
    *,
    enabled: bool = True,
    history_turns: int = 8,
    supplemental_max_chars: int = 2_000,
) -> SimpleNamespace:
    return SimpleNamespace(
        psychological_pattern_dynamics_enabled=enabled,
        psychological_pattern_history_turns=history_turns,
        psychological_pattern_supplemental_max_chars=supplemental_max_chars,
        employee_dialogue_history_mode="ecp_labels",
        chat_url="",
        model_for_task=lambda task_name: f"{task_name}-model",
        temperature_for_task=lambda task_name: 0.0,
        max_tokens_for_task=lambda task_name: (
            1024
            if task_name == "psychological_pattern_dynamics"
            else 512
        ),
        timeout_for_task=lambda task_name: 12.0,
        enable_thinking_for_task=lambda task_name: False,
    )


def _pattern_output(pattern_id: str) -> PsychologicalPatternDynamicsStructuredOutput:
    return _dynamics(
        patterns=[_decision(pattern_id, strength="strong")],
        guidance=PatternResponseGuidance(
            main_psychological_activity="维护对评价的判断",
            response_tendency="澄清",
            expression_guidance="先回应具体信息，再说明保留意见",
            checks=["不要把情绪变化等同于接受评价", "只回应本轮新信息"],
        ),
    )


@pytest.mark.asyncio
async def test_enabled_available_catalog_runs_independent_branches_with_correct_context(
    monkeypatch,
):
    manager_message = "CURRENT_MANAGER_MESSAGE_7F4C"
    pattern_id = "known-pattern"
    emotion_model = EmotionTransitionService()
    emotion = _RecordingEmotionTransition()
    state = SessionState(
        session_id="parallel-success",
        emotion_state=emotion_model.initial_state(None),
        motivation=MotivationState(
            primary_motive_id="autonomy",
            primary_score=37,
        ),
        conversation=[
            ConversationTurn(turn_index=1, speaker="employee", text="earlier employee"),
            ConversationTurn(turn_index=2, speaker="manager", text="earlier manager"),
            ConversationTurn(turn_index=3, speaker="system", text="context event only"),
            ConversationTurn(
                turn_index=4,
                speaker="manager",
                text=manager_message,
            ),
        ],
        user_turn_count=2,
    )
    catalog = _StaticCatalog(_catalog(pattern_id))

    class CapturingPatternLLM:
        def __init__(self):
            self.calls: list[dict] = []

        async def ainvoke_structured_single(self, **kwargs):
            self.calls.append(kwargs)
            return _pattern_output(pattern_id)

    llm = CapturingPatternLLM()
    scheduler = _ImmediateScheduler()
    monkeypatch.setattr(transition_module, "LangChainLLMService", lambda: llm)
    monkeypatch.setattr(transition_module, "log_metric", lambda *args, **kwargs: None)
    service = EmployeeStateTransitionService(
        settings=_parallel_settings(),
        emotion_transition=emotion,
        catalog=catalog,
        summary_service=_VisibleSummary(),
        model_scheduler=scheduler,
    )

    result = await service.update_after_manager_message(state, manager_message)

    assert catalog.calls == 1
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["schema"] is PsychologicalPatternDynamicsStructuredOutput
    assert call["task_name"] == "psychological_pattern_dynamics"
    assert call["max_tokens"] == 1024
    assert call["model"] == "psychological_pattern_dynamics-model"
    assert call["structured_transport"] == "json_schema"
    assert call["json_schema_strict"] is False
    assert [item["model"] for item in scheduler.calls] == [
        "emotion_transition-model",
        "psychological_pattern_dynamics-model",
    ]
    prompt = call["prompt"]
    assert prompt.count(manager_message) == 1
    assert '"speaker":"SELF"' in prompt
    assert '"speaker":"PARTNER"' in prompt
    assert '"speaker":"system"' in prompt
    assert "system 记录只能作为上下文事件" in prompt
    pattern_context = json.loads(prompt.split("\npattern_context=", 1)[1])
    assert "psychological_patterns" not in pattern_context
    assert pattern_context["latest_manager_message"] == manager_message
    assert pattern_context["emotion_t_minus_1"]["current_anchor_id"] == "neutral"
    assert pattern_context["motivation_t_minus_1"]["primary_score"] == 37.0
    assert pattern_context["motivation_t_minus_1"]["primary_motive_id"] == "autonomy"
    recent_history = pattern_context["recent_egocentric_conversation"]
    assert [turn["speaker"] for turn in recent_history] == ["SELF", "PARTNER", "system"]
    assert all(set(turn) == {"turn_index", "speaker", "text"} for turn in recent_history)
    assert recent_history[-1]["text"] == "context event only"
    assert result.pattern_dynamics_applied is True
    assert result.state.emotion_state.last_reason_summary == "emotion branch"
    activation = result.state.psychological_pattern_state.patterns[0]
    assert activation.pattern_id == pattern_id
    assert activation.strength == "strong"


@pytest.mark.asyncio
async def test_emotion_and_pattern_model_branches_are_in_flight_concurrently(
    monkeypatch,
):
    emotion_started = asyncio.Event()
    pattern_started = asyncio.Event()
    base_emotion = EmotionTransitionService()

    class CoordinatedEmotionTransition:
        async def update_after_manager_message(self, state, manager_message):
            emotion_started.set()
            await asyncio.wait_for(pattern_started.wait(), timeout=0.5)
            state.emotion_state.last_reason_summary = "parallel emotion complete"
            return state

    class CoordinatedPatternLLM:
        async def ainvoke_structured_single(self, **kwargs):
            pattern_started.set()
            await asyncio.wait_for(emotion_started.wait(), timeout=0.5)
            return _pattern_output("known-pattern")

    monkeypatch.setattr(
        transition_module,
        "LangChainLLMService",
        CoordinatedPatternLLM,
    )
    monkeypatch.setattr(transition_module, "log_metric", lambda *args, **kwargs: None)
    state = SessionState(
        session_id="concurrent-branches",
        emotion_state=base_emotion.initial_state(None),
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text="本轮反馈")
        ],
    )
    service = EmployeeStateTransitionService(
        settings=_parallel_settings(),
        emotion_transition=CoordinatedEmotionTransition(),
        catalog=_StaticCatalog(_catalog("known-pattern")),
        summary_service=_VisibleSummary(),
        model_scheduler=_ImmediateScheduler(),
    )

    result = await service.update_after_manager_message(state, "本轮反馈")

    assert emotion_started.is_set()
    assert pattern_started.is_set()
    assert result.state.emotion_state.last_reason_summary == "parallel emotion complete"
    assert result.pattern_dynamics_applied is True


@pytest.mark.asyncio
async def test_pattern_prompt_excludes_hidden_manager_intent_but_keeps_performance_facts(
    monkeypatch,
):
    manager_message = "我们只讨论已经确认的工作事实。"
    performance_fact = "已确认事实：员工按期完成了客户迁移。"
    hidden_values = (
        "PRIVATE_EXIT_INTENT_91A7",
        "PRIVATE_MANAGER_REASON_91A7",
        "PRIVATE_CONFIG_ID_91A7",
        "PRIVATE_CONFIG_NAME_91A7",
        "PRIVATE_CONFIG_DESCRIPTION_91A7",
    )
    emotion = EmotionTransitionService()
    state = SessionState(
        session_id="joint-intent-isolation",
        emotion_state=emotion.initial_state(None),
        intent=IntentResult(
            intent_id=hidden_values[0],
            confidence=0.987654,
            reason=hidden_values[1],
            config=IntentConfig(
                id=hidden_values[2],
                name=hidden_values[3],
                description=hidden_values[4],
            ),
            performance_context=performance_fact,
        ),
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text=manager_message,
            )
        ],
        user_turn_count=1,
    )

    # Direct emotion-only callers retain the existing Manager-intent behavior.
    legacy_emotion_prompt = emotion._build_prompt(state, manager_message)
    assert all(value in legacy_emotion_prompt for value in hidden_values)
    assert '"confidence": 0.987654' in legacy_emotion_prompt

    class CapturingPatternLLM:
        def __init__(self):
            self.calls: list[dict] = []

        async def ainvoke_structured_single(self, **kwargs):
            self.calls.append(kwargs)
            return _pattern_output("known-pattern")

    llm = CapturingPatternLLM()
    monkeypatch.setattr(transition_module, "LangChainLLMService", lambda: llm)
    monkeypatch.setattr(transition_module, "log_metric", lambda *args, **kwargs: None)
    service = EmployeeStateTransitionService(
        settings=_parallel_settings(),
        emotion_transition=_RecordingEmotionTransition(),
        catalog=_StaticCatalog(_catalog("known-pattern")),
        summary_service=_VisibleSummary(),
        model_scheduler=_ImmediateScheduler(),
    )

    await service.update_after_manager_message(state, manager_message)

    assert len(llm.calls) == 1
    pattern_prompt = llm.calls[0]["prompt"]
    assert all(value not in pattern_prompt for value in hidden_values)
    assert '"intent_id":' not in pattern_prompt
    assert '"confidence":' not in pattern_prompt
    assert '"reason":' not in pattern_prompt
    assert '"config":' not in pattern_prompt
    assert pattern_prompt.count(performance_fact) == 1
    pattern_context = json.loads(pattern_prompt.split("\npattern_context=", 1)[1])
    assert pattern_context["employee_visible_performance_context"] == performance_fact
    assert state.intent is not None
    assert state.intent.intent_id == hidden_values[0]


@pytest.mark.asyncio
async def test_pattern_llm_failure_does_not_retry_or_block_employee_reply(monkeypatch):
    pattern_id = "existing-pattern"
    emotion_model = EmotionTransitionService()
    previous_pattern_state = PsychologicalPatternState(
        last_processed_manager_turn=1,
        patterns=[_activation(pattern_id, strength="strong", trigger_turn=1)],
    )
    state = SessionState(
        session_id="pattern-failure",
        emotion_state=emotion_model.initial_state(None),
        psychological_pattern_state=previous_pattern_state.model_copy(deep=True),
    )

    class FailingPatternLLM:
        def __init__(self):
            self.calls = 0

        async def ainvoke_structured_single(self, **kwargs):
            self.calls += 1
            raise RuntimeError("pattern model failed")

    llm = FailingPatternLLM()
    monkeypatch.setattr(transition_module, "LangChainLLMService", lambda: llm)
    monkeypatch.setattr(transition_module, "log_metric", lambda *args, **kwargs: None)
    transition = EmployeeStateTransitionService(
        settings=_parallel_settings(),
        emotion_transition=_RecordingEmotionTransition(),
        catalog=_StaticCatalog(_catalog(pattern_id)),
        summary_service=_VisibleSummary(),
        model_scheduler=_ImmediateScheduler(),
    )

    class MotivationPassthrough:
        async def update_after_manager_message(self, current_state, manager_message):
            return current_state

    reply_calls: list[tuple[SessionState, str]] = []

    async def employee_reply(current_state, manager_message):
        reply_calls.append((current_state, manager_message))
        return "员工仍然完成了回复"

    nodes = object.__new__(RehearsalNodes)
    nodes.employee_state_transition = transition
    nodes.motivation_scoring = MotivationPassthrough()
    nodes.employee_agent = SimpleNamespace(reply=employee_reply)

    updated = await nodes.employee_reply_node(state, "这是一条普通经理反馈")

    assert llm.calls == 1
    assert updated.psychological_pattern_state == previous_pattern_state
    assert len(reply_calls) == 1
    assert reply_calls[0][1] == "这是一条普通经理反馈"
    assert [turn.speaker for turn in updated.conversation] == ["manager", "employee"]
    assert updated.conversation[-1].text == "员工仍然完成了回复"
    assert any("RuntimeError" in warning for warning in updated.warnings)


@pytest.mark.asyncio
async def test_catalog_access_exception_never_blocks_the_legacy_emotion_flow(
):
    class RaisingCatalog:
        async def asnapshot(self):
            raise OSError("catalog stat failed")

    incoming = SessionState(session_id="catalog-error")
    delegated = SessionState(session_id="catalog-error-delegated")
    emotion = _DelegatingEmotionTransition(delegated)
    service = _service()
    service.settings = _parallel_settings()
    service.emotion_transition = emotion
    service.catalog = RaisingCatalog()

    result = await service.update_after_manager_message(
        incoming,
        "manager text",
    )

    assert result.state is delegated
    assert emotion.calls == [(incoming, "manager text")]
    assert result.pattern_dynamics_applied is False


def test_pattern_history_removes_runtime_updates_but_keeps_other_system_events():
    turns = [
        ConversationTurn(
            turn_index=1,
            speaker="system",
            text="obsolete runtime note",
            metadata={"type": "rehearsal_context_update"},
        ),
        ConversationTurn(
            turn_index=2,
            speaker="system",
            text="other context event",
            metadata={"type": "other_event"},
        ),
        ConversationTurn(
            turn_index=3,
            speaker="manager",
            text="manager message",
        ),
    ]

    filtered = _service()._pattern_history_turns(turns)

    assert [turn.text for turn in filtered] == [
        "other context event",
        "manager message",
    ]


@pytest.mark.asyncio
async def test_pattern_context_has_its_own_compact_history_and_supplemental_limits():
    manager_message = "LATEST_MANAGER_MESSAGE"
    turns = [
        ConversationTurn(
            turn_index=index,
            speaker="manager" if index % 2 else "employee",
            text=f"historical-turn-{index}",
        )
        for index in range(1, 13)
    ]
    turns.append(
        ConversationTurn(
            turn_index=13,
            speaker="manager",
            text=manager_message,
        )
    )
    previous_patterns = [
        _activation(
            f"previous-pattern-{index}",
            strength="medium",
            trigger_turn=12,
        )
        for index in range(5)
    ]
    state = SessionState(
        session_id="compact-pattern-context",
        employee_profile=EmployeeProfile(
            employee_alias="测试员工",
            role="软件工程师",
            source_profile_text="SOURCE_PROFILE_TEXT_MUST_NOT_APPEAR",
            supplemental_info="PROFILE_SUPPLEMENT|" + "乙" * 500,
        ),
        supplemental_info="SESSION_SUPPLEMENT|" + "甲" * 1_900,
        psychological_pattern_state=PsychologicalPatternState(
            last_processed_manager_turn=12,
            patterns=previous_patterns,
        ),
        conversation=turns,
    )
    service = _service()
    service.settings = _parallel_settings(
        history_turns=8,
        supplemental_max_chars=2_000,
    )
    service.summary_service = _VisibleSummary()

    context = await service._build_pattern_context(state, manager_message)

    assert context["conversation_summary"] == "仅包含更早轮次的摘要"
    assert context["latest_manager_message"] == manager_message
    assert [
        item["text"] for item in context["recent_egocentric_conversation"]
    ] == [f"historical-turn-{index}" for index in range(5, 13)]
    assert len(context["recent_egocentric_conversation"]) == 8
    assert "source_profile_text" not in context["employee_visible_profile"]
    assert "supplemental_info" not in context["employee_visible_profile"]
    supplemental = context["employee_visible_supplemental_info"]
    assert len(supplemental) == 2_000
    assert "SESSION_SUPPLEMENT" in supplemental
    assert "PROFILE_SUPPLEMENT" in supplemental
    assert len(
        context["previous_psychological_pattern_state"]["patterns"]
    ) == 5


@pytest.mark.asyncio
async def test_pattern_context_projects_only_semantic_state_and_turn_fields():
    manager_message = "LATEST_MANAGER_MESSAGE"
    emotion_state = EmotionTransitionService().initial_state(None)
    emotion_state.reply_emotion_guidance = "PREVIOUS_REPLY_GUIDANCE_MUST_NOT_APPEAR"
    facet_context = {
        "openness": {
            "score": 67,
            "facets": {"intellectual_curiosity": 71},
        }
    }
    history: list[ConversationTurn] = []
    for turn_index in range(1, 9):
        metadata = {
            "rehearsal_timing": {
                "attempt_id": f"attempt-{turn_index}",
                "raw_operational_payload": "计" * 3_540,
            },
            "reply_emotion_guidance": "HISTORICAL_GUIDANCE_MUST_NOT_APPEAR",
            "updated_at": "2026-08-28T08:00:00Z",
            "unrelated_metadata": "UNRELATED_METADATA_MUST_NOT_APPEAR",
        }
        if turn_index == 1:
            metadata["emotion_snapshot"] = {
                "valence": -0.2,
                "arousal": 0.3,
                "dominance": -0.1,
                "anchor_id": "guarded",
                "previous_anchor_id": "neutral",
                "vad_delta": {
                    "valence": -0.2,
                    "arousal": 0.4,
                    "dominance": -0.1,
                },
                "transition_intensity": 0.4,
                "reason_summary": "manager challenged the result",
                "reply_emotion_guidance": "SNAPSHOT_GUIDANCE_MUST_NOT_APPEAR",
                "updated_at": "2026-08-28T08:00:00Z",
            }
        history.append(
            ConversationTurn(
                turn_index=turn_index,
                speaker="manager" if turn_index % 2 else "employee",
                text=f"historical-turn-{turn_index}",
                metadata=metadata,
            )
        )
    history.append(
        ConversationTurn(
            turn_index=9,
            speaker="manager",
            text=manager_message,
        )
    )
    state = SessionState(
        session_id="compact-pattern-operational-metadata",
        motivation=MotivationState(primary_motive_id="autonomy"),
        emotion_state=emotion_state,
        conversation=history,
    )
    state.personality_facets = SimpleNamespace(model_context=lambda: facet_context)
    state.rehearsal_context.runtime_notes = ["current runtime note"]
    service = _service()
    service.settings = _parallel_settings(history_turns=8)
    service.summary_service = _VisibleSummary()

    raw_history_chars = len(
        json.dumps(
            [turn.model_dump(mode="json") for turn in history[:-1]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    context = await service._build_pattern_context(state, manager_message)

    recent = context["recent_egocentric_conversation"]
    assert len(recent) == 8
    assert set(recent[0]) == {
        "turn_index",
        "speaker",
        "text",
        "emotion_snapshot",
    }
    assert all(
        set(turn) == {"turn_index", "speaker", "text"}
        for turn in recent[1:]
    )
    assert recent[0]["emotion_snapshot"] == {
        "valence": -0.2,
        "arousal": 0.3,
        "dominance": -0.1,
        "anchor_id": "guarded",
        "previous_anchor_id": "neutral",
        "vad_delta": {
            "valence": -0.2,
            "arousal": 0.4,
            "dominance": -0.1,
        },
        "transition_intensity": 0.4,
        "reason_summary": "manager challenged the result",
    }
    assert context["motivation_t_minus_1"] == state.motivation.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"updated_at"},
    )
    assert context["emotion_t_minus_1"] == emotion_state.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"reply_emotion_guidance", "updated_at"},
    )
    assert context["applicable_personality_facets"] == facet_context
    assert context["current_effective_dynamic_context"] == {
        "runtime_notes": ["current runtime note"]
    }

    projected_history_chars = len(
        json.dumps(recent, ensure_ascii=False, separators=(",", ":"))
    )
    assert projected_history_chars < raw_history_chars * 0.1
    serialized = json.dumps(context, ensure_ascii=False)
    for operational_key in (
        "created_at",
        "rehearsal_timing",
        "reply_emotion_guidance",
        "updated_at",
        "UNRELATED_METADATA_MUST_NOT_APPEAR",
    ):
        assert operational_key not in serialized
    assert state.conversation[0].metadata["rehearsal_timing"][
        "raw_operational_payload"
    ] == "计" * 3_540


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_point", "expected_pattern_calls"),
    [
        ("context", 0),
        ("prompt", 0),
        ("reducer", 1),
        ("metrics", 1),
    ],
)
async def test_pattern_stage_failure_is_transactional_and_nonstream_reply_continues(
    monkeypatch,
    failure_point,
    expected_pattern_calls,
):
    retained_id = "retained-pattern"
    removed_id = "removed-pattern"
    emotion_model = EmotionTransitionService()
    state = SessionState(
        session_id=f"isolated-{failure_point}",
        emotion_state=emotion_model.initial_state(None),
        psychological_pattern_state=PsychologicalPatternState(
            last_processed_manager_turn=1,
            patterns=[
                _activation(retained_id, strength="strong", trigger_turn=1),
                _activation(removed_id, strength="medium", trigger_turn=1),
            ],
        ),
    )

    class CountingPatternLLM:
        def __init__(self):
            self.calls = 0

        async def ainvoke_structured_single(self, **kwargs):
            self.calls += 1
            return _pattern_output(retained_id)

    llm = CountingPatternLLM()
    monkeypatch.setattr(transition_module, "LangChainLLMService", lambda: llm)
    monkeypatch.setattr(transition_module, "log_metric", lambda *args, **kwargs: None)
    transition = EmployeeStateTransitionService(
        settings=_parallel_settings(),
        emotion_transition=_RecordingEmotionTransition(),
        catalog=_StaticCatalog(_catalog(retained_id)),
        summary_service=_VisibleSummary(),
        model_scheduler=_ImmediateScheduler(),
    )

    def raise_stage_error(*args, **kwargs):
        raise RuntimeError(f"{failure_point} failed")

    async def raise_async_stage_error(*args, **kwargs):
        raise RuntimeError(f"{failure_point} failed")

    if failure_point == "context":
        monkeypatch.setattr(
            transition,
            "_build_pattern_context",
            raise_async_stage_error,
        )
    elif failure_point == "prompt":
        monkeypatch.setattr(transition, "_build_pattern_prompt", raise_stage_error)
    elif failure_point == "reducer":
        monkeypatch.setattr(
            transition,
            "_apply_pattern_dynamics",
            raise_stage_error,
        )
    else:
        monkeypatch.setattr(transition, "_record_metrics", raise_stage_error)

    class MotivationPassthrough:
        async def update_after_manager_message(self, current_state, manager_message):
            return current_state

    replies: list[str] = []

    async def employee_reply(current_state, manager_message):
        replies.append(manager_message)
        return "员工回复仍然生成"

    nodes = object.__new__(RehearsalNodes)
    nodes.employee_state_transition = transition
    nodes.motivation_scoring = MotivationPassthrough()
    nodes.employee_agent = SimpleNamespace(reply=employee_reply)

    updated = await nodes.employee_reply_node(state, "这是一条普通反馈")

    assert llm.calls == expected_pattern_calls
    assert replies == ["这是一条普通反馈"]
    assert updated.conversation[-1].text == "员工回复仍然生成"
    assert updated.emotion_state.last_reason_summary == "emotion branch"
    assert [
        item.pattern_id
        for item in updated.psychological_pattern_state.patterns
    ] == [retained_id]
    assert updated.psychological_pattern_state.patterns[0].strength == "strong"
    assert updated.psychological_pattern_state.last_processed_manager_turn == 1
    if failure_point != "metrics":
        assert any("RuntimeError" in warning for warning in updated.warnings)


def test_guidance_screening_uses_inactive_catalog_identities_too():
    catalog = PsychologicalPatternCatalogSnapshot(
        available=True,
        render_patterns=json.dumps(
            [
                {
                    "pattern_id": "active-id",
                    "construct_name": "Active Construct",
                },
                {
                    "pattern_id": "inactive-secret-id",
                    "construct_name": "Inactive Secret Construct",
                },
            ]
        ),
        valid_pattern_ids=frozenset({"active-id", "inactive-secret-id"}),
    )

    sanitized = _service()._sanitized_guidance(
        PatternResponseGuidance(
            main_psychological_activity="正在维护 Inactive Secret Construct",
            response_tendency="继续澄清 inactive-secret-id 对应的担忧",
            expression_guidance="只表达员工可见的信息",
            checks=[],
        ),
        catalog,
        {"active-id"},
    )

    rendered = json.dumps(sanitized.model_dump(mode="json"), ensure_ascii=False)
    assert "Inactive Secret Construct" not in rendered
    assert "inactive-secret-id" not in rendered


@pytest.mark.parametrize(
    "internal_marker",
    [
        "Trigger=经理提出新要求",
        "behavioral_tendency: 防御",
        "interaction=reinforce",
        "relative_strength=strong",
        "内部推演：先抵抗再接受",
    ],
)
def test_internal_reasoning_marker_drops_entire_guidance(internal_marker):
    state = SessionState(session_id="fail-closed-guidance")

    guidance, active_count, _ = _service()._apply_pattern_dynamics(
        state,
        _dynamics(
            patterns=[_decision("known")],
            guidance=PatternResponseGuidance(
                main_psychological_activity="维护对事实的判断",
                response_tendency="澄清",
                expression_guidance=internal_marker,
                checks=["只回应员工已知信息"],
            ),
        ),
        _catalog("known"),
        manager_turn=2,
    )

    assert active_count == 1
    assert guidance == ""


def test_pattern_catalog_is_one_stable_prefix_ahead_of_dynamic_context():
    service = _service()
    catalog = _catalog("stable-pattern")

    first = service._build_pattern_prompt(
        {"dynamic": "DYNAMIC_CONTEXT_ONE"},
        catalog,
    )
    second = service._build_pattern_prompt(
        {"dynamic": "DYNAMIC_CONTEXT_TWO"},
        catalog,
    )

    prefix = service._pattern_cache_prefix(catalog)
    assert first.startswith(prefix)
    assert second.startswith(prefix)
    assert first[: len(prefix)] == second[: len(prefix)]
    assert "DYNAMIC_CONTEXT_ONE" not in prefix
    assert catalog.render_patterns in prefix
    assert first.count(catalog.render_patterns) == 1
    assert catalog.render_patterns not in first[len(prefix) :]


def test_pattern_cache_prefix_contains_one_central_usage_safety_block():
    prefix = _service()._pattern_cache_prefix(_catalog("known"))

    assert prefix.count("[PATTERN_USAGE_SAFETY]") == 1
    assert prefix.index("[PATTERN_USAGE_SAFETY]") < prefix.index(
        "psychological_patterns="
    )
    assert "不得从单轮话语反推、新增或修改人格、能力、动机、道德品质" in prefix
    assert "Pattern 只是内部概率性解释假设" in prefix
    assert "工作设计问题不得归咎个人" in prefix
    assert "不得重复激活或叠加强度" in prefix
    assert "不得作为称呼、输出标签或道德结论" in prefix
    assert "具体问题和当前沟通目的先决定员工说什么" in prefix
    assert "main_psychological_activity 只供内部汇总" in prefix
    assert "checks 只能是防止越界的禁止性检查" in prefix
    assert "不得为了输出而凑指导" in prefix


def test_pattern_cache_prefix_changes_with_catalog_snapshot():
    first = _service()._pattern_cache_prefix(_catalog("first-pattern"))
    second = _service()._pattern_cache_prefix(_catalog("second-pattern"))

    assert first != second
    assert first.endswith(EmployeeStateTransitionService._PATTERN_CACHE_PREFIX_END)

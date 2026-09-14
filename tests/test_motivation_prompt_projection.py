from __future__ import annotations

from datetime import datetime, timezone
import json

from backend.schemas.conversation import ConversationTurn
from backend.schemas.intent import IntentResult
from backend.schemas.simulation import MotivationState
from backend.schemas.state import RehearsalRuntimeContext, SessionState
from backend.services.motivation_scoring_service import MotivationScoringService


def _turn(
    turn_index: int,
    speaker: str,
    text: str,
    *,
    metadata: dict | None = None,
) -> ConversationTurn:
    return ConversationTurn(
        turn_index=turn_index,
        speaker=speaker,
        text=text,
        created_at=datetime(2026, 8, 31, turn_index, tzinfo=timezone.utc),
        metadata=metadata or {},
    )


def _prompt_context(prompt: str) -> dict:
    prefix, separator, payload = prompt.rpartition("context=")
    assert prefix
    assert separator
    return json.loads(payload)


def test_motivation_prompt_keeps_only_prior_dialogue_semantics() -> None:
    latest_manager_message = "请说明下一步需要什么支持。"
    operational_metadata = {
        "input": "voice",
        "rehearsal_timing": {
            "attempt_id": "must-not-reach-motivation",
            "raw_operational_payload": "计" * 3_540,
        },
        "emotion_snapshot": {
            "valence": -0.2,
            "reason_summary": "不得作为动机评分历史输入。",
        },
    }
    turns = [
        _turn(1, "manager", latest_manager_message, metadata=operational_metadata),
        _turn(2, "employee", "我希望职责和回报能够匹配。", metadata=operational_metadata),
        _turn(
            3,
            "system",
            "已更新本轮模拟设定。",
            metadata={"type": "rehearsal_context_update", **operational_metadata},
        ),
        _turn(4, "manager", "我先核对你的职责范围。", metadata=operational_metadata),
        _turn(5, "employee", "可以，我补充今年承担的工作。", metadata=operational_metadata),
        _turn(6, "manager", "这些证据我会带入校准。", metadata=operational_metadata),
        _turn(7, "employee", "我最关心的是这部分被看见。", metadata=operational_metadata),
        _turn(8, "manager", "我会和 HRBP 核对。", metadata=operational_metadata),
        _turn(9, "employee", "好的，我等你的反馈。", metadata=operational_metadata),
        _turn(10, "manager", latest_manager_message, metadata=operational_metadata),
    ]
    state = SessionState(
        session_id="motivation-projection",
        intent=IntentResult(
            intent_id="development",
            confidence=0.91,
            reason="不应进入动机评分 Prompt。",
            performance_context="完整绩效背景不应进入动机评分 Prompt。",
        ),
        motivation=MotivationState(
            primary_motive_id="commerce",
            secondary_motive_ids=["security"],
            last_change_reason="历史模型解释不应递归注入。",
        ),
        rehearsal_context=RehearsalRuntimeContext(
            runtime_notes=["员工当前优先关注明确的薪酬反馈时间点。"],
        ),
        conversation=turns,
    )
    original_turns = [
        turn.model_dump(mode="json", exclude_none=True) for turn in turns
    ]
    original_state = state.model_dump(mode="json")

    prompt = MotivationScoringService()._build_prompt(
        state,
        latest_manager_message,
    )
    context = _prompt_context(prompt)

    assert [turn["turn_index"] for turn in context["conversation"]] == [
        1,
        2,
        4,
        5,
        6,
        7,
        8,
        9,
    ]
    assert all(
        set(turn) == {"turn_index", "speaker", "text"}
        for turn in context["conversation"]
    )
    assert context["conversation"][0]["text"] == latest_manager_message
    assert context["latest_manager_message"] == latest_manager_message
    assert context["intent"] == {"intent_id": "development"}
    assert set(context["motives"]) == {"commerce", "security"}
    assert set(context["motivation"]) == {
        "primary_motive_id",
        "secondary_motive_ids",
        "primary_score",
        "secondary_scores",
        "has_manager_response",
    }
    assert context["current_runtime_context"] == {
        "runtime_notes": ["员工当前优先关注明确的薪酬反馈时间点。"],
    }
    assert "完整绩效背景不应进入动机评分 Prompt" not in prompt
    assert "历史模型解释不应递归注入" not in prompt

    serialized_history = json.dumps(context["conversation"], ensure_ascii=False)
    for forbidden in (
        "created_at",
        "metadata",
        "rehearsal_timing",
        "raw_operational_payload",
        "emotion_snapshot",
        "reason_summary",
        "input",
        "已更新本轮模拟设定",
    ):
        assert forbidden not in serialized_history
    assert [
        turn.model_dump(mode="json", exclude_none=True) for turn in turns
    ] == original_turns
    assert state.model_dump(mode="json") == original_state

    clean_state = state.model_copy(deep=True)
    for turn in clean_state.conversation:
        turn.metadata = {}
    clean_prompt = MotivationScoringService()._build_prompt(
        clean_state,
        latest_manager_message,
    )
    assert clean_prompt == prompt

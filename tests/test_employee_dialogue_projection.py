from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

import pytest
from pydantic import ValidationError

from backend.agents.employee_agent import EmployeeAgent
from backend.config.settings import Settings
from backend.schemas.conversation import ConversationTurn, EmotionTurnSnapshot
from backend.schemas.conversation_summary import ConversationPromptContext
from backend.schemas.state import SessionState
from backend.services.prompt_service import PromptService


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EMPLOYEE_TEMPLATE_PATHS = (
    PROJECT_ROOT / "backend/prompts/employee/reply.jinja2",
    PROJECT_ROOT / "backend/prompts/employee/reply_test.jinja2",
)


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
        created_at=datetime(2026, 8, 22, turn_index, tzinfo=timezone.utc),
        metadata=metadata or {},
    )


def test_reply_snapshot_projection_matches_the_complete_snapshot_schema():
    snapshot = EmotionTurnSnapshot(
        valence=-0.2,
        arousal=0.4,
        dominance=-0.1,
        anchor_id="guarded",
        previous_anchor_id="neutral",
        vad_delta={"valence": -0.2, "arousal": 0.4, "dominance": -0.1},
        transition_intensity=0.4,
        reason_summary="经理质疑了结果。",
    ).model_dump(exclude_none=True)

    projected = EmployeeAgent._reply_history_conversation(
        [
            _turn(
                1,
                "manager",
                "先核对结果。",
                metadata={
                    "emotion_snapshot": snapshot,
                    "rehearsal_timing": {"sentinel": "MUST_DROP"},
                },
            )
        ]
    )

    assert tuple(EmployeeAgent._REPLY_HISTORY_EMOTION_SNAPSHOT_FIELDS) == tuple(
        EmotionTurnSnapshot.model_fields
    )
    assert projected == [
        {
            "turn_index": 1,
            "speaker": "manager",
            "text": "先核对结果。",
            "emotion_snapshot": snapshot,
        }
    ]


def test_reply_history_projection_keeps_only_semantic_fields_and_input_turns():
    turns = [
        _turn(
            1,
            "manager",
            "先复盘目标。",
            metadata={
                "source": "typed",
                "rehearsal_timing": {
                    "attempt_id": "reply-attempt-1",
                    "raw_operational_payload": "计" * 3_540,
                },
                "emotion_snapshot": {
                    "valence": -0.2,
                    "arousal": 0.4,
                    "dominance": -0.1,
                    "anchor_id": "guarded",
                    "reason_summary": "经理质疑了结果。",
                    "reply_emotion_guidance": "历史指导不得进入 Reply 历史。",
                    "updated_at": "2026-08-22T00:00:00Z",
                },
            },
        ),
        _turn(2, "employee", "我想先核对数据。", metadata={"vad": {"v": -0.2}}),
        _turn(
            3,
            "system",
            "已更新本轮模拟设定。",
            metadata={"type": "rehearsal_context_update", "version": 2},
        ),
    ]
    original_payloads = [
        turn.model_dump(mode="json", exclude_none=True) for turn in turns
    ]

    projected = EmployeeAgent._employee_egocentric_conversation(
        turns,
        "另一条当前发言",
        history_mode="ecp_labels",
    )

    assert projected == [
        {
            "turn_index": 1,
            "speaker": "PARTNER",
            "text": "先复盘目标。",
            "emotion_snapshot": {
                "valence": -0.2,
                "arousal": 0.4,
                "dominance": -0.1,
                "anchor_id": "guarded",
                "reason_summary": "经理质疑了结果。",
            },
        },
        {
            "turn_index": 2,
            "speaker": "SELF",
            "text": "我想先核对数据。",
        },
        {
            "turn_index": 3,
            "speaker": "system",
            "text": "已更新本轮模拟设定。",
        },
    ]
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "created_at" not in serialized
    assert "rehearsal_timing" not in serialized
    assert "raw_operational_payload" not in serialized
    assert "reply_emotion_guidance" not in serialized
    assert "updated_at" not in serialized
    assert "source" not in serialized
    assert "metadata" not in serialized
    assert [turn.model_dump(mode="json", exclude_none=True) for turn in turns] == (
        original_payloads
    )


def test_reply_retrieval_history_uses_the_same_operational_metadata_boundary():
    turns = [
        _turn(
            1,
            "manager",
            "先核对事实。",
            metadata={
                "rehearsal_timing": {"raw_operational_payload": "计" * 3_540},
                "emotion_snapshot": {
                    "valence": -0.1,
                    "anchor_id": "reserved",
                    "updated_at": "2026-08-22T00:00:00Z",
                },
            },
        ),
        _turn(2, "employee", "我先说明当前结果。"),
    ]
    state = SessionState(session_id="reply-retrieval-projection", conversation=turns)

    context = EmployeeAgent._build_retrieval_context(
        state,
        "请继续。",
    )

    assert context["conversation"] == [
        {
            "turn_index": 1,
            "speaker": "manager",
            "text": "先核对事实。",
            "emotion_snapshot": {
                "valence": -0.1,
                "anchor_id": "reserved",
            },
        },
        {
            "turn_index": 2,
            "speaker": "employee",
            "text": "我先说明当前结果。",
        },
    ]
    serialized = json.dumps(context["conversation"], ensure_ascii=False)
    assert "created_at" not in serialized
    assert "rehearsal_timing" not in serialized
    assert "raw_operational_payload" not in serialized
    assert turns[0].metadata["rehearsal_timing"]["raw_operational_payload"]


def test_ecp_projection_removes_only_matching_manager_tail():
    turns = [
        _turn(1, "manager", "这句话重复出现。"),
        _turn(2, "employee", "前一轮回复。"),
        _turn(3, "manager", "这句话重复出现。"),
    ]

    projected = EmployeeAgent._employee_egocentric_conversation(
        turns,
        "这句话重复出现。",
        history_mode="ecp_labels",
    )

    assert [(item["speaker"], item["text"]) for item in projected] == [
        ("PARTNER", "这句话重复出现。"),
        ("SELF", "前一轮回复。"),
    ]
    assert len(turns) == 3


def test_legacy_projection_keeps_old_labels_behind_the_new_interface():
    turns = [
        _turn(
            1,
            "manager",
            "先说事实。",
            metadata={
                "rehearsal_timing": {"raw_operational_payload": "计" * 3_540},
                "emotion_snapshot": {"valence": -0.1, "anchor_id": "reserved"},
            },
        ),
        _turn(2, "employee", "我在听。"),
        _turn(3, "system", "系统记录。"),
    ]

    projected = EmployeeAgent._employee_egocentric_conversation(
        turns,
        "当前新发言。",
        history_mode="legacy_labels",
    )
    mapping = EmployeeAgent._employee_dialogue_role_mapping("legacy_labels")

    assert [item["speaker"] for item in projected] == [
        "manager",
        "employee",
        "system",
    ]
    assert projected[0] == {
        "turn_index": 1,
        "speaker": "manager",
        "text": "先说事实。",
        "emotion_snapshot": {"valence": -0.1, "anchor_id": "reserved"},
    }
    assert "created_at" not in json.dumps(projected, ensure_ascii=False)
    assert "rehearsal_timing" not in json.dumps(projected, ensure_ascii=False)
    assert mapping["SELF"] == "当前扮演的员工"
    assert mapping["PARTNER"] == "经理"
    assert mapping["employee"].startswith("当前扮演的员工")
    assert mapping["manager"].startswith("经理")


def test_both_employee_templates_receive_the_same_new_dialogue_interface(
    monkeypatch,
):
    captured: list[tuple[str, dict]] = []

    def capture_render(_service, template_name: str, **context) -> str:
        captured.append((template_name, context))
        return template_name

    monkeypatch.setattr(PromptService, "render", capture_render)
    latest_message = "请说明当前交付差距。"
    turns = [
        _turn(
            1,
            "manager",
            "此前也讨论过交付差距。",
            metadata={"rehearsal_timing": {"raw_operational_payload": "计" * 3_540}},
        ),
        _turn(
            2,
            "employee",
            "我会补充事实。",
            metadata={"rehearsal_timing": {"attempt_id": "reply-attempt-2"}},
        ),
        _turn(3, "manager", latest_message, metadata={"input": "voice"}),
    ]
    state = SessionState(session_id="employee-ecp", conversation=turns)
    prompt_context = ConversationPromptContext(
        summary_text="此前已核对目标口径。",
        raw_turns=turns,
    )

    for test_prompt_enabled in (False, True):
        EmployeeAgent._build_reply_prompt(
            state,
            latest_message,
            [],
            conversation_context=prompt_context,
            test_prompt_enabled=test_prompt_enabled,
            dialogue_history_mode="ecp_labels",
        )

    assert [item[0] for item in captured] == [
        "employee/reply.jinja2",
        "employee/reply_test.jinja2",
    ]
    assert set(captured[0][1]) == set(captured[1][1])
    for _, context in captured:
        assert "conversation" not in context
        assert "latest_manager_message" not in context
        assert context["conversation_summary"] == "此前已核对目标口径。"
        assert context["summary_emotion_continuity_json"] == "{}"
        assert context["latest_partner_message"] == latest_message
        assert json.loads(context["dialogue_role_mapping"]) == {
            "PARTNER": "经理",
            "SELF": "当前扮演的员工",
        }
        history = json.loads(context["egocentric_conversation"])
        assert [
            (item["speaker"], item["text"])
            for item in history
        ] == [
            ("PARTNER", "此前也讨论过交付差距。"),
            ("SELF", "我会补充事实。"),
        ]
        assert all(
            set(item) == {"turn_index", "speaker", "text"}
            for item in history
        )
        assert "created_at" not in context["egocentric_conversation"]
        assert "rehearsal_timing" not in context["egocentric_conversation"]


def test_employee_templates_do_not_reference_old_dialogue_variables():
    old_variable = re.compile(r"\b(?:conversation|latest_manager_message)\b")
    jinja_tag = re.compile(r"(?:\{\{.*?\}\}|\{%.*?%\})", re.DOTALL)

    for template_path in EMPLOYEE_TEMPLATE_PATHS:
        template = template_path.read_text(encoding="utf-8")
        tags = jinja_tag.findall(template)
        assert all(not old_variable.search(tag) for tag in tags)
        for variable in (
            "dialogue_role_mapping",
            "egocentric_conversation",
            "latest_partner_message",
            "conversation_summary",
            "summary_emotion_continuity_json",
        ):
            assert re.search(rf"\b{variable}\b", template)
        assert "PARTNER 当前发言：" in template
        assert "reason_summary` 是历史内部推断" in template
        assert "不是对话事实、当前话题选择依据或需要复述的内容" in template
        assert "包括同一话题内对做法、步骤或实施方式的继续追问" in template
        assert "连续性首先是内部状态和表达倾向的延续" in template
        assert "不等于再次口头复述完整立场" in template
        assert "认可—转折—保留—并行推进—稍后整理或对齐" in template
        assert "本规则不限制回复句数、做法数量或必要的展开程度" in template
        assert "直接答案 + 一个不可缺少的细节" not in template
        assert "最多使用两个语义动作" not in template
        assert "通常用两到三个自然短句" not in template


def test_employee_dialogue_history_mode_settings_and_deployment_contract(
    monkeypatch,
):
    assert Settings.model_fields["employee_dialogue_history_mode"].default == (
        "ecp_labels"
    )

    monkeypatch.setenv("EMPLOYEE_DIALOGUE_HISTORY_MODE", "legacy_labels")
    assert Settings().employee_dialogue_history_mode == "legacy_labels"
    with pytest.raises(ValidationError):
        Settings(employee_dialogue_history_mode="unsupported")

    env_example = (PROJECT_ROOT / "backend/config/.env.example").read_text(
        encoding="utf-8"
    )
    compose = (PROJECT_ROOT / "deployment/compose/compose.services.yml").read_text(
        encoding="utf-8"
    )
    assert "EMPLOYEE_DIALOGUE_HISTORY_MODE=ecp_labels" in env_example
    assert (
        'EMPLOYEE_DIALOGUE_HISTORY_MODE: "${EMPLOYEE_DIALOGUE_HISTORY_MODE:-ecp_labels}"'
        in compose
    )

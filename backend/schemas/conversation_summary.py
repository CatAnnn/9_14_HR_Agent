from __future__ import annotations

from dataclasses import dataclass, field

from backend.schemas.conversation import ConversationTurn
from backend.schemas.locale import SessionLocale


@dataclass(frozen=True, slots=True)
class ConversationSummaryRecord:
    session_id: str
    generation: int = 0
    summary_text: str = ""
    covered_through_turn_index: int = 0
    model_name: str | None = None
    prompt_version: str = "v1"


@dataclass(frozen=True, slots=True)
class ConversationPromptContext:
    output_locale: SessionLocale = "zh-CN"
    summary_text: str = ""
    covered_through_turn_index: int = 0
    raw_turns: list[ConversationTurn] = field(default_factory=list)
    summary_emotion_continuity: dict[str, object] = field(default_factory=dict)

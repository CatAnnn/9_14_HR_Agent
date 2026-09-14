from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field, field_validator

from backend.schemas.conversation import ConversationTurn
from backend.schemas.profile import EmployeeProfile
from backend.schemas.intent import IntentResult
from backend.schemas.personality_facets import PersonalityFacetState
from backend.schemas.rehearsal_dimensions import RehearsalDimensionId
from backend.schemas.locale import SessionLocale
from backend.schemas.simulation import (
    BigFivePersonality,
    EmotionState,
    MotivationState,
    PsychologicalPatternState,
)

SessionStage = Literal["created", "profile_ready", "setup_ready", "guidance_ready", "rehearsal", "report_ready", "ended"]
RunMode = Literal["guidance_only", "guidance_then_rehearsal", "rehearsal_report"]
REHEARSAL_DIMENSION_COVERAGE_VERSION = 2


class RehearsalRuntimeContext(BaseModel):
    runtime_notes: list[str] = Field(default_factory=list)
    speech_voice: str | None = None
    speech_seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    covered_dimensions: list[RehearsalDimensionId] = Field(default_factory=list)
    dimension_coverage_version: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("covered_dimensions", mode="before")
    @classmethod
    def migrate_legacy_dimension_ids(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        aliases = {
            "fact": "start",
            "motivation": "requirement",
        }
        return [
            aliases.get(item, item) if isinstance(item, str) else item
            for item in value
        ]

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


class SessionState(BaseModel):
    session_id: str
    # Missing values in persisted legacy sessions intentionally default to Chinese.
    locale: SessionLocale = "zh-CN"
    revision: int = Field(default=0, ge=0, exclude=True)
    stage: SessionStage = "created"
    run_mode: RunMode = "guidance_then_rehearsal"
    employee_profile: EmployeeProfile | None = None
    supplemental_info: str | None = None
    intent: IntentResult | None = None
    personality: BigFivePersonality | None = None
    personality_facets: PersonalityFacetState | None = Field(
        default=None,
        exclude=True,
    )
    motivation: MotivationState | None = None
    emotion_state: EmotionState | None = None
    psychological_pattern_state: PsychologicalPatternState | None = Field(
        default=None,
        exclude=True,
    )
    setup_ready: bool = False
    guidance_report_id: str | None = None
    coach_report_id: str | None = None
    rehearsal_context: RehearsalRuntimeContext = Field(default_factory=RehearsalRuntimeContext)
    conversation: list[ConversationTurn] = Field(default_factory=list)
    user_turn_count: int = 0
    rehearsal_ended_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)

    def persistence_payload(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        if self.personality_facets is not None:
            payload["personality_facets"] = self.personality_facets.model_dump(
                mode="json"
            )
        if self.psychological_pattern_state is not None:
            payload["psychological_pattern_state"] = (
                self.psychological_pattern_state.model_dump(mode="json")
            )
        return payload

    def supplemental_info_excerpt(self, max_chars: int = 8000) -> str:
        text = str(self.supplemental_info or "").strip()
        if not text or len(text) <= max_chars:
            return text
        return f"{text[:max_chars]}\n\n[额外提供的信息较长，以上为前 {max_chars} 字；完整内容保存在 session.supplemental_info。]"

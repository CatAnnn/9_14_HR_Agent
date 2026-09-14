from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.schemas.locale import SessionLocale
from backend.schemas.simulation import BigFivePersonality


class AdminTestConversationTurn(BaseModel):
    speaker: Literal["manager", "employee"]
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("conversation text must not be empty")
        return text


class AdminTestWorkflowCreateRequest(BaseModel):
    locale: SessionLocale = "zh-CN"
    destination: Literal["rehearsal", "report"]
    intent_id: str = Field(min_length=1)
    personality: BigFivePersonality
    primary_motive_id: str = Field(min_length=1)
    secondary_motive_ids: list[str] = Field(default_factory=list, max_length=2)
    conversation: list[AdminTestConversationTurn] = Field(default_factory=list)

    @field_validator("intent_id", "primary_motive_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("secondary_motive_ids")
    @classmethod
    def normalize_secondary_motive_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("secondary motive ids must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("secondary motive ids must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_report_conversation(self) -> "AdminTestWorkflowCreateRequest":
        if self.primary_motive_id in self.secondary_motive_ids:
            raise ValueError("primary motive and secondary motives must be different")
        if self.destination == "report":
            speakers = {turn.speaker for turn in self.conversation}
            if "manager" not in speakers or "employee" not in speakers:
                raise ValueError(
                    "report testing requires at least one manager turn and one employee turn"
                )
        return self

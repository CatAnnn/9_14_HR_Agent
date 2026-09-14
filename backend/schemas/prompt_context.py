from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.intent import IntentConfig, IntentResult
from backend.schemas.personality_behavior import PersonalityBehaviorProfile
from backend.schemas.profile import EmployeeProfile
from backend.schemas.simulation import BigFivePersonality
from backend.schemas.locale import SessionLocale


class PromptContextModel(BaseModel):
    """Internal, typed payload serialized into a model prompt as JSON."""

    model_config = ConfigDict(extra="forbid")


class PromptRetrievedChunk(PromptContextModel):
    chunk_id: str
    source_id: str
    title: str
    scope: str
    text: str
    score: float = 0.0


class PromptKnowledgeSkill(PromptContextModel):
    id: str
    version: str
    source_ref: str = ""
    priority: int
    matched_triggers: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    core_knowledge: str = ""
    usage_rules: list[str] = Field(default_factory=list)
    stale: bool = False
    stale_source_count: int = 0


class PromptMotive(PromptContextModel):
    id: str
    name: str
    description: str = ""


class GuidanceMotives(PromptContextModel):
    primary: PromptMotive | None = None
    secondary: list[PromptMotive] = Field(default_factory=list)


class PromptCultureName(PromptContextModel):
    en: str = ""
    zh: str = ""


class PromptCompanyValue(PromptContextModel):
    id: str
    name: str
    name_zh: str = ""
    definition: str
    desired_behaviors: list[str] = Field(default_factory=list)
    desired_behaviors_zh: list[str] = Field(default_factory=list)
    anti_patterns: list[str] = Field(default_factory=list)
    manager_applications: list[str] = Field(default_factory=list)


class PromptCompanyValues(PromptContextModel):
    enabled: bool = False
    version: str | None = None
    company_name: str | None = None
    culture_name: PromptCultureName = Field(default_factory=PromptCultureName)
    values: list[PromptCompanyValue] = Field(default_factory=list)


class IntentPerformancePromptContext(PromptContextModel):
    output_locale: SessionLocale = "zh-CN"
    intent: dict[str, str]
    employee_profile: dict[str, Any]
    expected_goal_count: int = Field(ge=1)
    supplemental_info: str = ""
    eligibility: dict[str, Any] = Field(default_factory=dict)
    performance_inference: dict[str, Any] = Field(default_factory=dict)
    retrieved_chunks: list[PromptRetrievedChunk] = Field(default_factory=list)
    organization_unit_chunks: list[PromptRetrievedChunk] = Field(
        default_factory=list
    )


class GuidancePromptContext(PromptContextModel):
    output_locale: SessionLocale = "zh-CN"
    profile: EmployeeProfile | None = None
    supplemental_info: str = ""
    performance_context: str = ""
    intent: IntentResult | None = None
    personality: BigFivePersonality | None = None
    personality_behavior_profile: PersonalityBehaviorProfile | None = None
    motivation: GuidanceMotives = Field(default_factory=GuidanceMotives)
    retrieved_chunks: list[PromptRetrievedChunk] = Field(default_factory=list)
    company_values: PromptCompanyValues = Field(default_factory=PromptCompanyValues)
    culture_chunks: list[PromptRetrievedChunk] = Field(default_factory=list)
    active_skills: list[PromptKnowledgeSkill] = Field(default_factory=list)
    knowledge_base_folders: list[str] = Field(default_factory=list)
    career_elements_applicable: bool = False
    current_career_elements: list[str] = Field(default_factory=list)


class PromptConversationTurn(PromptContextModel):
    turn_index: int
    speaker: str
    text: str


class CoachPromptContext(PromptContextModel):
    output_locale: SessionLocale = "zh-CN"
    profile: EmployeeProfile | None = None
    supplemental_info: str = ""
    performance_context: str = ""
    conversation: list[PromptConversationTurn] = Field(default_factory=list)
    intent_id: str = ""
    intent_config: IntentConfig | None = None
    personality_behavior_profile: PersonalityBehaviorProfile | None = None
    retrieved_chunks: list[PromptRetrievedChunk] = Field(default_factory=list)
    active_skills: list[PromptKnowledgeSkill] = Field(default_factory=list)
    knowledge_base_folders: list[str] = Field(default_factory=list)
    dimension_config: dict[str, Any] = Field(default_factory=dict)
    career_elements_applicable: bool = False
    current_career_elements: list[str] = Field(default_factory=list)


def prompt_context_json(context: PromptContextModel) -> str:
    return json.dumps(
        context.model_dump(mode="json", exclude_none=False),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

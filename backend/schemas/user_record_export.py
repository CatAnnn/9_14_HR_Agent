from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.schemas.locale import SessionLocale


class UserRecordExportRequest(BaseModel):
    user_emails: list[str] = Field(default_factory=list, max_length=1000)
    session_ids: list[str] = Field(default_factory=list, max_length=5000)
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("user_emails")
    @classmethod
    def normalize_user_emails(cls, values: list[str]) -> list[str]:
        normalized = list(
            dict.fromkeys(value.strip().lower() for value in values if value.strip())
        )
        if any("@" not in value for value in normalized):
            raise ValueError("用户邮箱格式无效")
        return normalized

    @field_validator("session_ids")
    @classmethod
    def normalize_session_ids(cls, values: list[str]) -> list[str]:
        normalized = list(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )
        if any(
            len(value) > 256 or any(ord(character) < 32 for character in value)
            for value in normalized
        ):
            raise ValueError("会话 ID 格式无效")
        return normalized

    @model_validator(mode="after")
    def validate_date_range(self) -> UserRecordExportRequest:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("结束日期不能早于开始日期")
        return self


class AdminExportConversationItem(BaseModel):
    session_id: str
    user_email: str
    user_display_name: str | None = None
    employee_name: str | None = None
    intent_id: str | None = None
    stage: str | None = None
    session_created_at: datetime
    conversation_started_at: datetime | None = None
    conversation_ended_at: datetime | None = None
    turn_count: int = Field(ge=0)
    has_conversation: bool
    has_rehearsal: bool


class AdminExportConversationListResponse(BaseModel):
    items: list[AdminExportConversationItem] = Field(default_factory=list)


class _AdminUserSessionReadModel(BaseModel):
    """Explicit allowlist for content shown in the read-only admin viewer."""

    model_config = ConfigDict(extra="forbid")


class AdminUserSessionIntentPerformanceItem(_AdminUserSessionReadModel):
    goal: str
    current_performance: str


class AdminUserSessionIntent(_AdminUserSessionReadModel):
    intent_id: str | None = None
    name: str | None = None
    performance_context: str | None = None
    performance_items: list[AdminUserSessionIntentPerformanceItem] = Field(
        default_factory=list
    )


class AdminUserSessionMotivation(_AdminUserSessionReadModel):
    primary_motive_id: str | None = None
    secondary_motive_ids: list[str] = Field(default_factory=list)


class AdminUserSessionProfileFact(_AdminUserSessionReadModel):
    description: str
    impact: str | None = None
    evidence_source: str | None = None


class AdminUserSessionSensitiveConstraint(_AdminUserSessionReadModel):
    status: str | None = None
    business_impact_summary: str | None = None


class AdminUserSessionEmployeeProfile(_AdminUserSessionReadModel):
    name: str | None = None
    employee_alias: str | None = None
    employee_id: str | None = None
    role: str | None = None
    department: str | None = None
    level: str | None = None
    reporting_line: str | None = None
    performance_rating: str | None = None
    tcl: str | None = None
    review_cycle: str | None = None
    conversation_topic: str | None = None
    key_goals: list[str] = Field(default_factory=list)
    facts: list[AdminUserSessionProfileFact] = Field(default_factory=list)
    past_ratings: list[str] = Field(default_factory=list)
    historical_feedback: list[str] = Field(default_factory=list)
    previous_improvement_discussion: str | None = None
    management_actions: list[str] = Field(default_factory=list)
    has_pip: str | None = None
    involves_promotion_salary_transfer: str | None = None
    employee_status_summary: str | None = None
    sensitive_constraints: dict[str, AdminUserSessionSensitiveConstraint] = Field(
        default_factory=dict
    )
    source_profile_text: str | None = None
    supplemental_info: str | None = None
    current_career_elements: list[str] = Field(default_factory=list)


class AdminUserSessionPersonality(_AdminUserSessionReadModel):
    openness: float | None = Field(default=None, ge=0, le=100)
    conscientiousness: float | None = Field(default=None, ge=0, le=100)
    extraversion: float | None = Field(default=None, ge=0, le=100)
    agreeableness: float | None = Field(default=None, ge=0, le=100)
    neuroticism: float | None = Field(default=None, ge=0, le=100)


class AdminUserSessionConversationTurn(_AdminUserSessionReadModel):
    turn_index: int = Field(ge=0)
    speaker: Literal["manager", "employee"]
    text: str
    created_at: datetime | None = None


class AdminUserSessionGuidancePoint(_AdminUserSessionReadModel):
    title: str | None = None
    summary: str | None = None
    details: list[str] = Field(default_factory=list)


class AdminUserSessionGuidanceDimensions(_AdminUserSessionReadModel):
    start: list[AdminUserSessionGuidancePoint] = Field(default_factory=list)
    emotion: list[AdminUserSessionGuidancePoint] = Field(default_factory=list)
    requirement: list[AdminUserSessionGuidancePoint] = Field(default_factory=list)
    plan: list[AdminUserSessionGuidancePoint] = Field(default_factory=list)


class AdminUserSessionGuidance(_AdminUserSessionReadModel):
    purpose: str | None = None
    opening_suggestion: str | None = None
    risk_preview: list[str] = Field(default_factory=list)
    response_strategies: list[str] = Field(default_factory=list)
    safer_phrases: list[str] = Field(default_factory=list)
    dimension_points: AdminUserSessionGuidanceDimensions | None = None


class AdminUserSessionCoachBetterPhrase(_AdminUserSessionReadModel):
    diagnostic_dimension_id: str | None = None
    original: str | None = None
    suggestion: str | None = None
    reason: str | None = None


class AdminUserSessionCareerAdvice(_AdminUserSessionReadModel):
    element: str | None = None
    suggestion: str | None = None
    reason: str | None = None


class AdminUserSessionCoachTask(_AdminUserSessionReadModel):
    task_id: str
    task_name: str
    status: str
    score: int | None = None
    summary: str | None = None
    basis: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    improvement_points: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    better_phrases: list[AdminUserSessionCoachBetterPhrase] = Field(
        default_factory=list
    )
    career_elements_advice: list[AdminUserSessionCareerAdvice] = Field(
        default_factory=list
    )


class AdminUserSessionDetail(_AdminUserSessionReadModel):
    read_only: Literal[True] = True
    session_id: str
    user_email: str
    user_display_name: str | None = None
    stage: str | None = None
    run_mode: str | None = None
    locale: SessionLocale = "zh-CN"
    session_created_at: datetime
    session_updated_at: datetime
    rehearsal_ended_at: datetime | None = None
    employee_profile: AdminUserSessionEmployeeProfile | None = None
    supplemental_info: str | None = None
    intent: AdminUserSessionIntent | None = None
    personality: AdminUserSessionPersonality | None = None
    motivation: AdminUserSessionMotivation | None = None
    runtime_notes: list[str] = Field(default_factory=list)
    conversation: list[AdminUserSessionConversationTurn] = Field(default_factory=list)
    guidance: AdminUserSessionGuidance | None = None
    coach_tasks: list[AdminUserSessionCoachTask] = Field(default_factory=list)

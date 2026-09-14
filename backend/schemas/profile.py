from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field

from backend.utils.rating_values import (
    extract_profile_rating,
    extract_profile_tcl,
    normalize_performance_rating,
    normalize_tcl,
)

CompletenessLevel = Literal["required", "strong_recommended", "optional"]
SensitiveFlag = Literal["yes", "no", "unknown"]


class FactItem(BaseModel):
    description: str
    impact: str | None = None
    evidence_source: str | None = None


class SensitiveConstraint(BaseModel):
    status: SensitiveFlag = "unknown"
    business_impact_summary: str | None = None


class EmployeeProfileExtractionOutput(BaseModel):
    """Only fields the profile extraction model is allowed to produce."""

    employee_alias: str | None = None
    role: str | None = None
    department: str | None = None
    level: str | None = None
    reporting_line: str | None = None

    performance_rating: str | None = None
    tcl: str | None = None
    review_cycle: str | None = None
    conversation_topic: str | None = None

    key_goals: list[str] = Field(default_factory=list)
    facts: list[FactItem] = Field(default_factory=list)

    past_ratings: list[str] = Field(default_factory=list)
    historical_feedback: list[str] = Field(default_factory=list)
    previous_improvement_discussion: SensitiveFlag = "unknown"

    management_actions: list[str] = Field(default_factory=list)
    has_pip: SensitiveFlag = "unknown"
    involves_promotion_salary_transfer: SensitiveFlag = "unknown"

    employee_status_summary: str | None = None
    sensitive_constraints: dict[str, SensitiveConstraint] = Field(default_factory=dict)


class EmployeeProfile(EmployeeProfileExtractionOutput):
    employee_id: str | None = None
    source_profile_text: str | None = None
    supplemental_info: str | None = None
    current_career_elements: list[str] = Field(default_factory=list)

    def missing_required_fields(self) -> list[str]:
        required = [
            "employee_alias",
            "role",
            "review_cycle",
            "conversation_topic",
        ]
        missing = [field for field in required if not getattr(self, field)]
        has_rating_or_tcl = any(
            (
                normalize_performance_rating(self.performance_rating) is not None,
                normalize_tcl(self.tcl) is not None,
                extract_profile_rating(self.source_profile_text) is not None,
                extract_profile_tcl(self.source_profile_text) is not None,
            )
        )
        if not has_rating_or_tcl:
            missing.append("performance_rating_or_tcl")
        if not self.key_goals and not self.facts:
            missing.append("key_goals_or_facts")
        return missing

    def is_ready_for_setup(self) -> bool:
        return len(self.missing_required_fields()) == 0

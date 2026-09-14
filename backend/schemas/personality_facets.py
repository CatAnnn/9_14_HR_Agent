from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictFacetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpennessFacets(_StrictFacetModel):
    fantasy: int = Field(ge=0, le=100)
    aesthetics: int = Field(ge=0, le=100)
    feelings: int = Field(ge=0, le=100)
    actions: int = Field(ge=0, le=100)
    ideas: int = Field(ge=0, le=100)
    values: int = Field(ge=0, le=100)


class ConscientiousnessFacets(_StrictFacetModel):
    competence: int = Field(ge=0, le=100)
    order: int = Field(ge=0, le=100)
    dutifulness: int = Field(ge=0, le=100)
    achievement_striving: int = Field(ge=0, le=100)
    self_discipline: int = Field(ge=0, le=100)
    deliberation: int = Field(ge=0, le=100)


class ExtraversionFacets(_StrictFacetModel):
    warmth: int = Field(ge=0, le=100)
    gregariousness: int = Field(ge=0, le=100)
    assertiveness: int = Field(ge=0, le=100)
    activity: int = Field(ge=0, le=100)
    excitement_seeking: int = Field(ge=0, le=100)
    positive_emotions: int = Field(ge=0, le=100)


class AgreeablenessFacets(_StrictFacetModel):
    trust: int = Field(ge=0, le=100)
    straightforwardness: int = Field(ge=0, le=100)
    altruism: int = Field(ge=0, le=100)
    compliance: int = Field(ge=0, le=100)
    modesty: int = Field(ge=0, le=100)
    tender_mindedness: int = Field(ge=0, le=100)


class NeuroticismFacets(_StrictFacetModel):
    anxiety: int = Field(ge=0, le=100)
    angry_hostility: int = Field(ge=0, le=100)
    depression: int = Field(ge=0, le=100)
    self_consciousness: int = Field(ge=0, le=100)
    impulsiveness: int = Field(ge=0, le=100)
    vulnerability: int = Field(ge=0, le=100)


class OpennessFacetGroup(_StrictFacetModel):
    score: int = Field(ge=0, le=100)
    facets: OpennessFacets


class ConscientiousnessFacetGroup(_StrictFacetModel):
    score: int = Field(ge=0, le=100)
    facets: ConscientiousnessFacets


class ExtraversionFacetGroup(_StrictFacetModel):
    score: int = Field(ge=0, le=100)
    facets: ExtraversionFacets


class AgreeablenessFacetGroup(_StrictFacetModel):
    score: int = Field(ge=0, le=100)
    facets: AgreeablenessFacets


class NeuroticismFacetGroup(_StrictFacetModel):
    score: int = Field(ge=0, le=100)
    facets: NeuroticismFacets


class PersonalityFacetGenerationOutput(_StrictFacetModel):
    openness: OpennessFacetGroup
    conscientiousness: ConscientiousnessFacetGroup
    extraversion: ExtraversionFacetGroup
    agreeableness: AgreeablenessFacetGroup
    neuroticism: NeuroticismFacetGroup


class PersonalityFacetState(PersonalityFacetGenerationOutput):
    input_signature: str = Field(min_length=64, max_length=64)
    source: Literal["model", "deterministic_fallback"]
    generator_version: str
    model_name: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def model_context(self) -> dict[str, object]:
        return self.model_dump(
            mode="json",
            include={
                "openness",
                "conscientiousness",
                "extraversion",
                "agreeableness",
                "neuroticism",
            },
        )

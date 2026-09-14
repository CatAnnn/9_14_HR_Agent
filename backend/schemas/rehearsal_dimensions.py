from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


RehearsalDimensionId = Literal["start", "emotion", "requirement", "plan"]
REHEARSAL_DIMENSION_ORDER: tuple[RehearsalDimensionId, ...] = (
    "start",
    "emotion",
    "requirement",
    "plan",
)


class RehearsalDimensionEvaluation(BaseModel):
    """Minimal structured output for cumulative rehearsal-dimension coverage."""

    model_config = ConfigDict(extra="forbid")

    covered_dimensions: list[RehearsalDimensionId] = Field(
        default_factory=list,
    )

    @field_validator("covered_dimensions")
    @classmethod
    def normalize_order(
        cls,
        value: list[RehearsalDimensionId],
    ) -> list[RehearsalDimensionId]:
        selected = set(value)
        return [
            dimension
            for dimension in REHEARSAL_DIMENSION_ORDER
            if dimension in selected
        ]

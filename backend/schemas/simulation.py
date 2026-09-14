from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ScoreDelta = Annotated[float, Field(ge=-100.0, le=100.0)]
PatternStrength = Literal["weak", "medium", "medium_high", "strong"]
PatternChange = Literal[
    "first_activated",
    "strengthened",
    "weakened",
    "maintained",
    "reactivated",
]
PatternInteractionType = Literal["reinforce", "conflict", "modulate"]
PatternGuidanceCheck = Annotated[str, Field(max_length=120)]


class BigFivePersonality(BaseModel):
    openness: int = Field(default=50, ge=0, le=100)
    conscientiousness: int = Field(default=50, ge=0, le=100)
    extraversion: int = Field(default=50, ge=0, le=100)
    agreeableness: int = Field(default=50, ge=0, le=100)
    neuroticism: int = Field(default=50, ge=0, le=100)


class MotiveOption(BaseModel):
    id: str
    name: str
    dimension: str
    description: str = ""
    examples: list[str] = Field(default_factory=list)


class VADVector(BaseModel):
    valence: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description="Pleasure/valence delta or coordinate in the closed interval [-1, 1].",
    )
    arousal: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description="Activation/arousal delta or coordinate in the closed interval [-1, 1].",
    )
    dominance: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description="Control/dominance delta or coordinate in the closed interval [-1, 1].",
    )


class EmotionAnchor(BaseModel):
    id: str
    name: str
    description: str = ""
    vad: VADVector = Field(default_factory=VADVector)


class VADAxisWeights(BaseModel):
    valence: float = Field(default=1.0, gt=0.0)
    arousal: float = Field(default=1.0, gt=0.0)
    dominance: float = Field(default=1.0, gt=0.0)


class EmotionTransitionModelConfig(BaseModel):
    algorithm: Literal["mecot_vad_v1"] = "mecot_vad_v1"
    axis_weights: VADAxisWeights = Field(default_factory=VADAxisWeights)
    distance_temperature: float = Field(default=0.5, gt=0.0)
    stay_bias: float = 0.3
    slow_process_weight: float = Field(default=6.0, ge=0.0)
    personality_strength: float = Field(default=1.5, ge=0.0)
    neutral_delta_epsilon: float = Field(default=0.05, ge=0.0, le=1.0)
    decisive_intensity_threshold: float = Field(default=0.35, ge=0.0, lt=1.0)
    decisive_probability_sharpness: float = Field(default=4.0, ge=1.0, le=32.0)
    min_transition_rate: float = Field(default=0.45, ge=0.0, le=1.0)
    max_transition_rate: float = Field(default=0.75, ge=0.0, le=1.0)
    max_axis_step: float = Field(default=0.35, gt=0.0, le=1.0)
    sampling_enabled: bool = False

    @model_validator(mode="after")
    def validate_thresholds(self) -> "EmotionTransitionModelConfig":
        if self.decisive_intensity_threshold < self.neutral_delta_epsilon:
            raise ValueError("decisive_intensity_threshold must be >= neutral_delta_epsilon")
        if self.max_transition_rate < self.min_transition_rate:
            raise ValueError("max_transition_rate must be >= min_transition_rate")
        return self


class MotivationState(BaseModel):
    primary_motive_id: str
    secondary_motive_ids: list[str] = Field(default_factory=list, max_length=2)
    primary_score: float = 50.0
    secondary_scores: dict[str, float] = Field(default_factory=dict)
    total_satisfaction: float = 50.0
    last_change_reason: str | None = None
    has_manager_response: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("primary_score")
    @classmethod
    def clamp_primary_score(cls, value: float) -> float:
        return max(-100.0, min(100.0, float(value)))

    @model_validator(mode="after")
    def normalize_scores(self) -> "MotivationState":
        unique_secondary = []
        for motive_id in self.secondary_motive_ids:
            if motive_id not in unique_secondary:
                unique_secondary.append(motive_id)
        self.secondary_motive_ids = unique_secondary[:2]
        for motive_id in self.secondary_motive_ids:
            self.secondary_scores[motive_id] = max(-100.0, min(100.0, float(self.secondary_scores.get(motive_id, 50.0))))
        if self.secondary_motive_ids:
            secondary_average = sum(
                self.secondary_scores.get(motive_id, 50.0) for motive_id in self.secondary_motive_ids
            ) / len(self.secondary_motive_ids)
            total_satisfaction = self.primary_score * 0.7 + secondary_average * 0.3
        else:
            self.secondary_scores = {}
            total_satisfaction = self.primary_score
        self.total_satisfaction = max(-100.0, min(100.0, total_satisfaction))
        return self


class EmotionState(BaseModel):
    current_vad: VADVector = Field(default_factory=VADVector)
    current_anchor_id: str | None = None
    previous_anchor_id: str | None = None
    last_vad_delta: VADVector = Field(default_factory=VADVector)
    transition_intensity: float = Field(default=0.0, ge=0.0, le=1.0)
    transition_strategy: Literal["expected_value", "maximum_probability", "sampling"] = "expected_value"
    last_reason_summary: str | None = None
    reply_emotion_guidance: str | None = None
    has_manager_response: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PsychologicalPatternActivation(BaseModel):
    """Compact cross-turn state for one psychological pattern."""

    pattern_id: str = Field(min_length=1, max_length=256)
    strength: PatternStrength
    change: PatternChange
    trigger_type: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    trigger_turn: int = Field(ge=1)


class PsychologicalPatternInteraction(BaseModel):
    """A material relationship between two active patterns."""

    from_pattern_id: str = Field(min_length=1, max_length=256)
    to_pattern_id: str = Field(min_length=1, max_length=256)
    type: PatternInteractionType


class PsychologicalPatternState(BaseModel):
    """Persisted, non-explanatory Pattern Dynamics state."""

    last_processed_manager_turn: int | None = Field(default=None, ge=1)
    patterns: list[PsychologicalPatternActivation] = Field(
        default_factory=list,
        max_length=5,
    )
    interactions: list[PsychologicalPatternInteraction] = Field(
        default_factory=list,
        max_length=4,
    )


class PsychologicalPatternActivationDecision(BaseModel):
    """Minimal model decision used to update one psychological pattern."""

    pattern_id: str = Field(min_length=1, max_length=256)
    strength: PatternStrength
    trigger_type: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    major_change_justified: bool = False


class PatternResponseGuidance(BaseModel):
    """Compact Pattern summary plus ephemeral Employee Reply modulation."""

    main_psychological_activity: str = Field(
        default="",
        max_length=200,
        description=(
            "Pattern 模块对本轮主导心理活动的内部汇总；不得把它当成员工回复的"
            "内容议程或要求员工外显。没有相关影响时留空。"
        ),
    )
    response_tendency: str = Field(
        default="",
        max_length=80,
        description=(
            "只描述如何回应已经由当前问题和事实选中的内容，不得新增话题、"
            "旧立场、感谢、计划或提问；没有相关影响时留空。"
        ),
    )
    expression_guidance: str = Field(
        default="",
        max_length=300,
        description=(
            "只调节已选内容的语气、直接程度、信息密度和承诺边界，不得规定"
            "固定话术、言语动作顺序或完整回应结构；没有相关影响时留空。"
        ),
    )
    checks: list[PatternGuidanceCheck] = Field(
        default_factory=list,
        max_length=2,
        description=(
            "至多两条禁止性边界检查，只用于防止已选回复越界，不得成为新增的"
            "表达任务。没有必要时为空。"
        ),
    )


class PsychologicalPatternDynamicsStructuredOutput(BaseModel):
    """Pattern Dynamics portion of the unified state-transition response."""

    major_new_information: bool = False
    patterns: list[PsychologicalPatternActivationDecision] = Field(
        default_factory=list,
        max_length=5,
    )
    interactions: list[PsychologicalPatternInteraction] = Field(
        default_factory=list,
        max_length=4,
    )
    response_guidance: PatternResponseGuidance = Field(
        default_factory=PatternResponseGuidance
    )


class MotivationScoringStructuredOutput(BaseModel):
    """One-turn motive satisfaction score changes."""

    primary_score_delta: ScoreDelta = Field(
        description="Required one-turn score change for the selected primary motive in [-100, 100].",
    )
    secondary_score_deltas: dict[str, ScoreDelta] = Field(
        default_factory=dict,
        description="Score changes keyed only by the selected secondary motive IDs.",
    )
    reason_summary: str = ""


class EmotionTransitionStructuredOutput(BaseModel):
    """One-turn VAD transition direction and response guidance."""

    vad_delta: VADVector = Field(
        description="Required one-turn valence, arousal, and dominance deltas, each in [-1, 1].",
    )
    transition_strategy: Literal["expected_value", "maximum_probability", "sampling"] = Field(
        description="Must be exactly expected_value, maximum_probability, or sampling.",
    )
    appraisal_tags: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Zero to eight YAML-defined contextual appraisal tags supported by explicit "
            "conversation evidence; tags never establish objective responsibility or truth."
        ),
    )
    reason_summary: str = ""


class EmployeeStateTransitionStructuredOutput(EmotionTransitionStructuredOutput):
    """Unified transition output used only when Pattern Dynamics is enabled."""

    pattern_dynamics: PsychologicalPatternDynamicsStructuredOutput

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.personality_facets import (
    AgreeablenessFacets,
    ConscientiousnessFacets,
    ExtraversionFacets,
    NeuroticismFacets,
    OpennessFacets,
)
from backend.schemas.simulation import BigFivePersonality

PersonalityBandId = Literal[
    "very_low",
    "clear_low",
    "mild_low",
    "neutral",
    "mild_high",
    "clear_high",
    "very_high",
]
PersonalityBandDirection = Literal["low", "neutral", "high"]

PERSONALITY_BAND_IDS: tuple[PersonalityBandId, ...] = (
    "very_low",
    "clear_low",
    "mild_low",
    "neutral",
    "mild_high",
    "clear_high",
    "very_high",
)


VERBATIM_PERSONALITY_SOURCE_SHA256 = (
    "160096170dde3b08f05938aab08dfef4ea66002bf62a6c0f06283ff8260ca5ac"
)

PERSONALITY_FACET_MODELS = {
    "openness": OpennessFacets,
    "conscientiousness": ConscientiousnessFacets,
    "extraversion": ExtraversionFacets,
    "agreeableness": AgreeablenessFacets,
    "neuroticism": NeuroticismFacets,
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonalityScoreBand(_StrictModel):
    id: PersonalityBandId
    min_score: int = Field(ge=0, le=100)
    max_score: int = Field(ge=0, le=100)
    label: str = Field(min_length=1)
    direction: PersonalityBandDirection
    band_explanation: str = Field(min_length=1)
    intensity_guidance: str = Field(min_length=1)


class PersonalityBandDescriptions(_StrictModel):
    """Seven explicit, reviewable descriptions for one dimension or facet."""

    very_low: str = Field(min_length=1)
    clear_low: str = Field(min_length=1)
    mild_low: str = Field(min_length=1)
    neutral: str = Field(min_length=1)
    mild_high: str = Field(min_length=1)
    clear_high: str = Field(min_length=1)
    very_high: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_descriptions(self) -> "PersonalityBandDescriptions":
        descriptions = tuple(self.model_dump(mode="python").values())
        if len(set(descriptions)) != len(descriptions):
            raise ValueError("personality band descriptions must be unique")
        return self


class PersonalityBandCalibrations(_StrictModel):
    """Trait-specific behavioral calibration for each score band."""

    very_low: str = Field(min_length=8, max_length=200)
    clear_low: str = Field(min_length=8, max_length=200)
    mild_low: str = Field(min_length=8, max_length=200)
    neutral: str = Field(min_length=8, max_length=200)
    mild_high: str = Field(min_length=8, max_length=200)
    clear_high: str = Field(min_length=8, max_length=200)
    very_high: str = Field(min_length=8, max_length=200)

    @model_validator(mode="after")
    def validate_unique_calibrations(self) -> "PersonalityBandCalibrations":
        calibrations = tuple(self.model_dump(mode="python").values())
        if len(set(calibrations)) != len(calibrations):
            raise ValueError("personality band calibrations must be unique")
        return self


class PersonalityDirectionalBehavior(_StrictModel):
    internal_tendency: str = Field(min_length=1)
    attention_and_judgment: str = Field(min_length=1)
    observable_expression: str = Field(min_length=1)
    decision_and_interaction: str = Field(min_length=1)
    workplace_manifestations: list[str] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def validate_manifestations(self) -> "PersonalityDirectionalBehavior":
        if len(set(self.workplace_manifestations)) != len(
            self.workplace_manifestations
        ):
            raise ValueError("personality manifestations must be unique")
        return self


class PersonalityContextualModifiers(_StrictModel):
    more_visible_when: str = Field(min_length=1)
    less_visible_when: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_distinct_contexts(self) -> "PersonalityContextualModifiers":
        if self.more_visible_when == self.less_visible_when:
            raise ValueError("personality visibility contexts must differ")
        return self


class PersonalityTraitSpecification(_StrictModel):
    """One stable construct with two endpoints and a context-sensitive midpoint."""

    construct_definition: str = Field(min_length=1)
    activation_context: str = Field(min_length=1)
    low_profile: PersonalityDirectionalBehavior
    neutral_tendency: str = Field(min_length=1)
    high_profile: PersonalityDirectionalBehavior
    band_calibrations: PersonalityBandCalibrations
    contextual_modifiers: PersonalityContextualModifiers
    non_implications: list[str] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def validate_directional_profiles(self) -> "PersonalityTraitSpecification":
        if self.low_profile == self.high_profile:
            raise ValueError("low and high personality profiles must differ")
        if set(self.low_profile.workplace_manifestations) & set(
            self.high_profile.workplace_manifestations
        ):
            raise ValueError(
                "low and high personality manifestations must not overlap"
            )
        if len(set(self.non_implications)) != len(self.non_implications):
            raise ValueError("personality non-implications must be unique")
        return self


def render_personality_band_description(
    specification: PersonalityTraitSpecification,
    band: PersonalityScoreBand,
) -> str:
    """Deterministically compile one canonical trait into one visible band."""

    if band.direction == "low":
        behavior = specification.low_profile
    elif band.direction == "high":
        behavior = specification.high_profile
    else:
        behavior = None

    if behavior is None:
        internal_tendency = specification.neutral_tendency
        attention_and_judgment = (
            "本项不单独提高低向或高向信息的权重，按当前事实和直接问题判断。"
        )
        observable_expression = (
            "不需要为了展示中间分数制造固定措辞或额外内容。"
        )
        decision_and_interaction = (
            "本项不单独加速、推迟或补全决定与互动，由实际情境决定。"
        )
        manifestations = (
            "当前为中间区间，不预设低向或高向的固定行为信号。",
        )
    else:
        internal_tendency = behavior.internal_tendency
        attention_and_judgment = behavior.attention_and_judgment
        observable_expression = behavior.observable_expression
        decision_and_interaction = behavior.decision_and_interaction
        manifestations = tuple(behavior.workplace_manifestations)

    marker = f"【{band.min_score}–{band.max_score}｜{band.label}】"
    sections = [
            marker,
            "\n\n".join(
                (
                    "【本档人格解释】",
                    specification.construct_definition,
                    band.band_explanation,
                )
            ),
            "\n".join(
                (
                    "【本档行为表现】",
                    f"* 触发场景：{specification.activation_context}",
                    f"* 内部倾向：{internal_tendency}",
                    f"* 注意与判断：{attention_and_judgment}",
                    f"* 可观察外显：{observable_expression}",
                    f"* 决策与互动：{decision_and_interaction}",
                    f"* 强度呈现：{band.intensity_guidance}",
                )
            ),
            "\n".join(
                ("【本档具体线索】",)
                + tuple(f"* {item}" for item in manifestations)
            ),
    ]
    calibration = getattr(specification.band_calibrations, band.id)
    sections.append(f"【本档特有校准】\n* {calibration}")
    sections.extend(
        (
            "\n".join(
                (
                    "【情境调节】",
                    "* 更容易显现："
                    f"{specification.contextual_modifiers.more_visible_when}",
                    "* 较少显现："
                    f"{specification.contextual_modifiers.less_visible_when}",
                )
            ),
            "\n".join(
                ("【禁止推论】",)
                + tuple(f"* {item}" for item in specification.non_implications)
            ),
        )
    )
    return "\n\n".join(sections)


class PersonalityFacetSelector(_StrictModel):
    display_name: str = Field(min_length=1)
    specification: PersonalityTraitSpecification
    band_descriptions: PersonalityBandDescriptions
    section_start: str = Field(min_length=1)
    section_end: str = Field(min_length=1)


class PersonalityDimensionSelector(_StrictModel):
    display_name: str = Field(min_length=1)
    specification: PersonalityTraitSpecification
    band_descriptions: PersonalityBandDescriptions
    section_start: str = Field(min_length=1)
    section_end: str = Field(min_length=1)
    facets: dict[str, PersonalityFacetSelector]


class PersonalityCombinationCondition(_StrictModel):
    trait_id: str = Field(min_length=1)
    band_ids: list[PersonalityBandId] = Field(min_length=1)


class PersonalityCombinationRule(_StrictModel):
    id: str = Field(min_length=1)
    all_of: list[PersonalityCombinationCondition] = Field(default_factory=list)
    any_of: list[PersonalityCombinationCondition] = Field(default_factory=list)
    guidance: str = Field(min_length=1)


class PersonalityBehaviorMap(_StrictModel):
    version: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)
    score_bands: list[PersonalityScoreBand] = Field(min_length=7, max_length=7)
    dimensions: dict[str, PersonalityDimensionSelector]
    combination_rules: list[PersonalityCombinationRule] = Field(default_factory=list)
    source_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_contract(self) -> "PersonalityBehaviorMap":
        digest = hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
        if self.source_sha256 != VERBATIM_PERSONALITY_SOURCE_SHA256:
            raise ValueError("personality behavior source_sha256 changed unexpectedly")
        if digest != self.source_sha256:
            raise ValueError(
                "personality behavior source_text does not match its immutable source hash"
            )

        bands = sorted(self.score_bands, key=lambda item: item.min_score)
        if len({item.id for item in bands}) != 7:
            raise ValueError("personality score band ids must be unique")
        if tuple(item.id for item in bands) != PERSONALITY_BAND_IDS:
            raise ValueError(
                "personality score band ids must match description fields exactly"
            )
        expected_directions: tuple[PersonalityBandDirection, ...] = (
            "low",
            "low",
            "low",
            "neutral",
            "high",
            "high",
            "high",
        )
        if tuple(item.direction for item in bands) != expected_directions:
            raise ValueError(
                "personality score band directions must be low/neutral/high "
                "in canonical score order"
            )
        expected_min = 0
        for band in bands:
            if band.min_score != expected_min or band.max_score < band.min_score:
                raise ValueError("personality score bands must cover 0..100 without gaps")
            expected_min = band.max_score + 1
        if expected_min != 101:
            raise ValueError("personality score bands must cover 0..100 exactly")

        expected_dimensions = tuple(BigFivePersonality.model_fields)
        if tuple(self.dimensions) != expected_dimensions:
            raise ValueError(
                "personality dimensions must match BigFivePersonality order exactly"
            )

        valid_trait_ids = set(expected_dimensions)
        for dimension_id, selector in self.dimensions.items():
            expected_facets = tuple(
                PERSONALITY_FACET_MODELS[dimension_id].model_fields
            )
            if tuple(selector.facets) != expected_facets:
                raise ValueError(
                    f"personality facets for {dimension_id} must match schema order exactly"
                )
            valid_trait_ids.update(
                f"{dimension_id}.{facet_id}" for facet_id in expected_facets
            )
            self._validate_dimension_markers(dimension_id, selector)
            self._validate_trait_specification(dimension_id, selector)
            self._validate_band_descriptions(dimension_id, selector, bands)

        rule_ids: set[str] = set()
        for rule in self.combination_rules:
            if rule.id in rule_ids:
                raise ValueError(f"duplicate personality combination rule: {rule.id}")
            rule_ids.add(rule.id)
            if not rule.all_of:
                raise ValueError(
                    f"personality combination rule {rule.id} requires all_of"
                )
            for condition in (*rule.all_of, *rule.any_of):
                if condition.trait_id not in valid_trait_ids:
                    raise ValueError(
                        f"unknown personality trait in {rule.id}: {condition.trait_id}"
                    )
            if "【" in rule.guidance or "】" in rule.guidance:
                raise ValueError(
                    f"personality combination guidance for {rule.id} must "
                    "contain plain conditional text"
                )
        return self

    def _validate_dimension_markers(
        self,
        dimension_id: str,
        selector: PersonalityDimensionSelector,
    ) -> None:
        section_start, section_end = self._ordered_range(
            self.source_text,
            selector.section_start,
            selector.section_end,
            f"dimension {dimension_id}",
        )
        section = self.source_text[section_start:section_end]
        for facet_id, facet in selector.facets.items():
            self._ordered_range(
                section,
                facet.section_start,
                facet.section_end,
                f"facet {dimension_id}.{facet_id}",
            )
            self._validate_trait_specification(
                f"{dimension_id}.{facet_id}",
                facet,
            )
            self._validate_band_descriptions(
                f"{dimension_id}.{facet_id}",
                facet,
                sorted(self.score_bands, key=lambda item: item.min_score),
            )

    @staticmethod
    def _validate_trait_specification(
        trait_id: str,
        selector: PersonalityDimensionSelector | PersonalityFacetSelector,
    ) -> None:
        specification = selector.specification
        fields = [
            specification.construct_definition,
            specification.activation_context,
            specification.neutral_tendency,
            specification.contextual_modifiers.more_visible_when,
            specification.contextual_modifiers.less_visible_when,
            *specification.non_implications,
        ]
        fields.extend(
            specification.band_calibrations.model_dump(mode="python").values()
        )
        for behavior in (
            specification.low_profile,
            specification.high_profile,
        ):
            fields.extend(
                (
                    behavior.internal_tendency,
                    behavior.attention_and_judgment,
                    behavior.observable_expression,
                    behavior.decision_and_interaction,
                    *behavior.workplace_manifestations,
                )
            )
        if any("【" in value or "】" in value for value in fields):
            raise ValueError(
                f"personality specification for {trait_id} must contain plain "
                "construct text, not rendered section markers"
            )

    @staticmethod
    def _validate_band_descriptions(
        trait_id: str,
        selector: PersonalityDimensionSelector | PersonalityFacetSelector,
        bands: list[PersonalityScoreBand],
    ) -> None:
        descriptions = selector.band_descriptions.model_dump(mode="python")
        if tuple(descriptions) != PERSONALITY_BAND_IDS:
            raise ValueError(
                f"personality band descriptions for {trait_id} must contain "
                "all seven bands in score order"
            )

        for band in bands:
            expected = render_personality_band_description(
                selector.specification,
                band,
            )
            if descriptions[band.id] != expected:
                raise ValueError(
                    f"personality band {trait_id}.{band.id} is stale or "
                    "manually edited; rebuild the generated seven-band map"
                )

    @staticmethod
    def _ordered_range(
        source: str,
        start_marker: str,
        end_marker: str,
        label: str,
    ) -> tuple[int, int]:
        start = source.find(start_marker)
        if start < 0:
            raise ValueError(f"missing start marker for {label}: {start_marker!r}")
        end = source.find(end_marker, start + len(start_marker))
        if end < 0:
            raise ValueError(f"missing end marker for {label}: {end_marker!r}")
        return start, end


class PersonalityBehaviorScore(_StrictModel):
    trait_id: str
    score: int = Field(ge=0, le=100)
    band_id: PersonalityBandId
    label: str
    display_name: str
    detailed_description: str


class PersonalityBehaviorProfile(_StrictModel):
    map_version: str
    map_hash: str = Field(min_length=64, max_length=64)
    scores: list[PersonalityBehaviorScore]
    matched_combination_ids: list[str] = Field(default_factory=list)
    matched_combination_rules: list[str] = Field(default_factory=list)
    general_rules: str

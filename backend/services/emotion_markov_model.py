from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import math
from typing import Any, Literal

from backend.schemas.simulation import (
    BigFivePersonality,
    EmotionAnchor,
    EmotionTransitionModelConfig,
    VADVector,
)

TransitionStrategy = Literal["expected_value", "maximum_probability", "sampling"]
_AXES = ("valence", "arousal", "dominance")
_BIG_FIVE_DIMENSIONS = (
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
)


@dataclass(frozen=True)
class EmotionTransitionDecision:
    next_vad: VADVector
    actual_delta: VADVector
    current_anchor_id: str
    target_anchor_id: str
    probabilities: dict[str, float]
    strategy: TransitionStrategy
    intensity: float
    transition_rate: float


class EmotionMarkovModel:
    """Deterministic 3D VAD transition model adapted from MECoT."""

    def __init__(
        self,
        anchors: Mapping[str, EmotionAnchor],
        config: EmotionTransitionModelConfig,
        personality_config: Mapping[str, Any] | None = None,
    ) -> None:
        if not anchors:
            raise ValueError("EmotionMarkovModel requires at least one anchor")
        self.anchors = tuple(anchors.values())
        self.anchor_by_id = {anchor.id: anchor for anchor in self.anchors}
        if len(self.anchor_by_id) != len(self.anchors):
            raise ValueError("Emotion anchor IDs must be unique")

        self.config = config
        self._axis_weights = tuple(
            float(getattr(config.axis_weights, axis)) for axis in _AXES
        )
        self._personality_weights, self._max_personality_bias = (
            self._parse_personality_config(personality_config or {})
        )
        self._base_logits = {
            source.id: tuple(self._base_logit(source, target) for target in self.anchors)
            for source in self.anchors
        }

    def base_transition_probabilities(self, current_anchor_id: str) -> dict[str, float]:
        source = self._require_anchor(current_anchor_id)
        return self._probability_mapping(self._base_logits[source.id])

    def transition_probabilities(
        self,
        current_vad: VADVector,
        current_anchor_id: str | None,
        vad_delta: VADVector,
        personality: BigFivePersonality | None,
        eligible_anchor_ids: Collection[str] | None = None,
    ) -> dict[str, float]:
        source = self._source_anchor(current_vad, current_anchor_id)
        eligible_ids = self._validated_eligible_anchor_ids(eligible_anchor_ids)
        personality_bias = self.personality_vad_bias(personality)
        logits = []
        for target in self.anchors:
            if eligible_ids is not None and target.id not in eligible_ids:
                logits.append(None)
                continue
            base_logit = self._base_logit_from_vad(
                current_vad,
                source.id,
                target,
            )
            slow_process = self.config.slow_process_weight * sum(
                getattr(vad_delta, axis)
                * (getattr(target.vad, axis) - getattr(current_vad, axis))
                for axis in _AXES
            )
            personality_process = self.config.personality_strength * self._dot(
                personality_bias,
                target.vad,
            )
            logits.append(base_logit + slow_process + personality_process)
        return self._probability_mapping(tuple(logits))

    def transition(
        self,
        current_vad: VADVector,
        current_anchor_id: str | None,
        vad_delta: VADVector,
        personality: BigFivePersonality | None,
        requested_strategy: TransitionStrategy,
        eligible_anchor_ids: Collection[str] | None = None,
    ) -> EmotionTransitionDecision:
        source = self._source_anchor(current_vad, current_anchor_id)
        eligible_ids = self._validated_eligible_anchor_ids(eligible_anchor_ids)
        intensity = self.stimulus_intensity(vad_delta)
        probabilities = self.transition_probabilities(
            current_vad,
            source.id,
            vad_delta,
            personality,
            eligible_ids,
        )

        if intensity <= self.config.neutral_delta_epsilon:
            zero = VADVector()
            result_anchor_id = self.nearest_anchor_id(current_vad, eligible_ids)
            return EmotionTransitionDecision(
                next_vad=current_vad.model_copy(deep=True),
                actual_delta=zero,
                current_anchor_id=result_anchor_id,
                target_anchor_id=result_anchor_id,
                probabilities=probabilities,
                strategy=self._resolve_strategy(requested_strategy, intensity),
                intensity=intensity,
                transition_rate=0.0,
            )

        strategy = self._resolve_strategy(requested_strategy, intensity)
        expected_vad = self._expected_vad(probabilities)
        if strategy == "maximum_probability":
            maximum_anchor_id = max(
                probabilities,
                key=probabilities.__getitem__,
            )
            decisive_vad = self._expected_vad(
                self._sharpened_probabilities(probabilities)
            )
            blend = self._decisive_target_blend(intensity)
            target_vad = VADVector(
                **{
                    axis: getattr(expected_vad, axis)
                    + (
                        getattr(decisive_vad, axis)
                        - getattr(expected_vad, axis)
                    )
                    * blend
                    for axis in _AXES
                }
            )
            # Keep the categorical label for observability, but derive the numeric
            # target from a sharpened continuous distribution. Directly mixing in
            # the argmax anchor would reintroduce cliffs when the argmax changes.
            target_anchor_id = maximum_anchor_id
        else:
            target_vad = expected_vad
            target_anchor_id = self.nearest_anchor_id(target_vad, eligible_ids)

        transition_rate = self._transition_rate(intensity)
        next_vad = VADVector(
            **{
                axis: self._clamp(
                    getattr(current_vad, axis)
                    + self._align_step_with_stimulus(
                        self._clamp(
                            (getattr(target_vad, axis) - getattr(current_vad, axis))
                            * transition_rate,
                            -self.config.max_axis_step,
                            self.config.max_axis_step,
                        ),
                        getattr(vad_delta, axis),
                    )
                )
                for axis in _AXES
            }
        )
        actual_delta = self._subtract(next_vad, current_vad)
        result_anchor_id = self.nearest_anchor_id(next_vad, eligible_ids)
        return EmotionTransitionDecision(
            next_vad=next_vad,
            actual_delta=actual_delta,
            current_anchor_id=result_anchor_id,
            target_anchor_id=target_anchor_id,
            probabilities=probabilities,
            strategy=strategy,
            intensity=intensity,
            transition_rate=transition_rate,
        )

    def personality_vad_bias(
        self,
        personality: BigFivePersonality | None,
    ) -> VADVector:
        scores = personality or BigFivePersonality()
        values = {axis: 0.0 for axis in _AXES}
        for dimension in _BIG_FIVE_DIMENSIONS:
            normalized = (float(getattr(scores, dimension)) - 50.0) / 50.0
            weights = self._personality_weights[dimension]
            for axis_index, axis in enumerate(_AXES):
                values[axis] += normalized * weights[axis_index]
        return VADVector(
            **{
                axis: self._clamp(
                    values[axis],
                    -self._max_personality_bias,
                    self._max_personality_bias,
                )
                for axis in _AXES
            }
        )

    def personality_adjusted_vad(
        self,
        base_vad: VADVector,
        personality: BigFivePersonality | None,
    ) -> VADVector:
        bias = self.personality_vad_bias(personality)
        return VADVector(
            **{
                axis: self._clamp(getattr(base_vad, axis) + getattr(bias, axis))
                for axis in _AXES
            }
        )

    def stimulus_intensity(self, vad_delta: VADVector) -> float:
        # Thresholds and prompt calibration are defined by the dominant VAD axis.
        # Averaging three axes diluted clear single-axis emotional stimuli.
        return max(abs(getattr(vad_delta, axis)) for axis in _AXES)

    def nearest_anchor_id(
        self,
        vad: VADVector,
        eligible_anchor_ids: Collection[str] | None = None,
    ) -> str:
        eligible_ids = self._validated_eligible_anchor_ids(eligible_anchor_ids)
        candidates = (
            self.anchors
            if eligible_ids is None
            else tuple(anchor for anchor in self.anchors if anchor.id in eligible_ids)
        )
        return min(
            candidates,
            key=lambda anchor: self._weighted_distance_squared(vad, anchor.vad),
        ).id

    def _validated_eligible_anchor_ids(
        self,
        eligible_anchor_ids: Collection[str] | None,
    ) -> frozenset[str] | None:
        if eligible_anchor_ids is None:
            return None
        eligible_ids = frozenset(str(anchor_id) for anchor_id in eligible_anchor_ids)
        if not eligible_ids:
            raise ValueError("Emotion transition requires at least one eligible anchor")
        unknown = sorted(eligible_ids - set(self.anchor_by_id))
        if unknown:
            raise ValueError(f"Unknown eligible emotion anchors: {unknown}")
        return eligible_ids

    def _base_logit(self, source: EmotionAnchor, target: EmotionAnchor) -> float:
        distance_term = -(
            self._weighted_distance_squared(source.vad, target.vad)
            / self.config.distance_temperature
        )
        return distance_term + (self.config.stay_bias if source.id == target.id else 0.0)

    def _base_logit_from_vad(
        self,
        current_vad: VADVector,
        current_anchor_id: str,
        target: EmotionAnchor,
    ) -> float:
        distance_term = -(
            self._weighted_distance_squared(current_vad, target.vad)
            / self.config.distance_temperature
        )
        return distance_term + (
            self.config.stay_bias if current_anchor_id == target.id else 0.0
        )

    def _source_anchor(
        self,
        current_vad: VADVector,
        current_anchor_id: str | None,
    ) -> EmotionAnchor:
        if current_anchor_id in self.anchor_by_id:
            return self.anchor_by_id[current_anchor_id]
        return self.anchor_by_id[self.nearest_anchor_id(current_vad)]

    def _require_anchor(self, anchor_id: str) -> EmotionAnchor:
        try:
            return self.anchor_by_id[anchor_id]
        except KeyError as exc:
            raise ValueError(f"Unknown emotion anchor: {anchor_id}") from exc

    def _weighted_distance_squared(self, left: VADVector, right: VADVector) -> float:
        return sum(
            weight * (getattr(left, axis) - getattr(right, axis)) ** 2
            for axis, weight in zip(_AXES, self._axis_weights, strict=True)
        )

    def _probability_mapping(
        self,
        logits: tuple[float | None, ...],
    ) -> dict[str, float]:
        finite_logits = [logit for logit in logits if logit is not None]
        if (
            not logits
            or not finite_logits
            or any(not math.isfinite(logit) for logit in finite_logits)
        ):
            raise ValueError("Emotion transition logits must be finite and non-empty")
        maximum = max(finite_logits)
        weights = [
            0.0 if logit is None else math.exp(logit - maximum)
            for logit in logits
        ]
        total = sum(weights)
        if not math.isfinite(total) or total <= 0.0:
            raise ValueError("Emotion transition softmax normalization failed")
        return {
            anchor.id: weight / total
            for anchor, weight in zip(self.anchors, weights, strict=True)
        }

    def _expected_vad(self, probabilities: Mapping[str, float]) -> VADVector:
        return VADVector(
            **{
                axis: sum(
                    probabilities[anchor.id] * getattr(anchor.vad, axis)
                    for anchor in self.anchors
                )
                for axis in _AXES
            }
        )

    def _resolve_strategy(
        self,
        requested_strategy: TransitionStrategy,
        intensity: float,
    ) -> TransitionStrategy:
        if (
            requested_strategy == "maximum_probability"
            and intensity >= self.config.decisive_intensity_threshold
        ):
            return "maximum_probability"
        if requested_strategy == "sampling":
            # Production defaults to deterministic expectation. Enabling stochastic
            # selection requires an explicit seeded sampler, which this pure model
            # intentionally does not hide.
            return "expected_value"
        return "expected_value"

    def _transition_rate(self, intensity: float) -> float:
        progress = min(1.0, max(0.0, intensity))
        return self.config.min_transition_rate + (
            self.config.max_transition_rate - self.config.min_transition_rate
        ) * progress

    def _decisive_target_blend(self, intensity: float) -> float:
        threshold = self.config.decisive_intensity_threshold
        if threshold >= 1.0:
            return 1.0 if intensity >= threshold else 0.0
        return self._clamp(
            (intensity - threshold) / (1.0 - threshold),
            0.0,
            1.0,
        )

    def _sharpened_probabilities(
        self,
        probabilities: Mapping[str, float],
    ) -> dict[str, float]:
        sharpness = self.config.decisive_probability_sharpness
        weights = {
            anchor_id: probability**sharpness
            for anchor_id, probability in probabilities.items()
        }
        total = sum(weights.values())
        if not math.isfinite(total) or total <= 0.0:
            raise ValueError("Sharpened emotion probabilities must have positive mass")
        return {
            anchor_id: weight / total
            for anchor_id, weight in weights.items()
        }

    @staticmethod
    def _dot(left: VADVector, right: VADVector) -> float:
        return sum(getattr(left, axis) * getattr(right, axis) for axis in _AXES)

    @staticmethod
    def _subtract(left: VADVector, right: VADVector) -> VADVector:
        return VADVector(
            **{axis: getattr(left, axis) - getattr(right, axis) for axis in _AXES}
        )

    def _align_step_with_stimulus(self, step: float, stimulus: float) -> float:
        """Keep the applied axis step directional and within its stimulus budget."""

        budget = min(
            self.config.max_axis_step,
            max(0.0, abs(stimulus) - self.config.neutral_delta_epsilon),
        )
        if budget == 0.0 or step * stimulus < 0.0:
            return 0.0
        return self._clamp(step, -budget, budget)

    @staticmethod
    def _clamp(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
        return max(lower, min(upper, float(value)))

    @staticmethod
    def _parse_personality_config(
        config: Mapping[str, Any],
    ) -> tuple[dict[str, tuple[float, float, float]], float]:
        raw_dimensions = config.get("dimensions") or {}
        if not isinstance(raw_dimensions, Mapping):
            raise ValueError("personality transition dimensions must be a mapping")
        max_axis_bias = float(config.get("max_axis_bias", 0.35))
        if not math.isfinite(max_axis_bias) or not 0.0 <= max_axis_bias <= 1.0:
            raise ValueError("personality max_axis_bias must be within [0, 1]")

        dimensions: dict[str, tuple[float, float, float]] = {}
        for dimension in _BIG_FIVE_DIMENSIONS:
            raw_weights = raw_dimensions.get(dimension) or {}
            if not isinstance(raw_weights, Mapping):
                raise ValueError(f"personality weights for {dimension} must be a mapping")
            weights = tuple(float(raw_weights.get(axis, 0.0)) for axis in _AXES)
            if any(not math.isfinite(weight) for weight in weights):
                raise ValueError(f"personality weights for {dimension} must be finite")
            dimensions[dimension] = weights
        return dimensions, max_axis_bias

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any

from backend.business_config.loader import get_config_loader
from backend.config.settings import get_settings
from backend.exceptions.llm_errors import StructuredOutputError
from backend.observability.metrics import record_emotion_transition
from backend.schemas.conversation import ConversationTurn
from backend.schemas.simulation import (
    BigFivePersonality,
    EmotionState,
    EmotionTransitionStructuredOutput,
    VADVector,
)
from backend.schemas.state import SessionState
from backend.services.emotion_markov_model import (
    EmotionMarkovModel,
    EmotionTransitionDecision,
)
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.personality_behavior_service import (
    get_personality_behavior_resolver,
)

logger = logging.getLogger(__name__)


class EmotionTransitionService:
    """Updates VAD emotion only; it never reads motive satisfaction scores."""

    def __init__(self, markov_model: EmotionMarkovModel | None = None) -> None:
        self._loader = get_config_loader()
        self._expression_policy = self._loader.emotion_expression_policy()
        self._anchor_selection_policy = (
            self._loader.emotion_anchor_selection_policy()
        )
        self._markov_model = markov_model or EmotionMarkovModel(
            self._loader.emotion_anchors(),
            self._loader.emotion_transition_model_config(),
            self._loader.personality_transition_vad_weights(),
        )

    def initial_state(
        self,
        intent_id: str | None,
        personality: BigFivePersonality | None = None,
    ) -> EmotionState:
        anchors = self._loader.emotion_anchors()
        base_anchor_id = self._loader.default_emotion_anchor_id(intent_id)
        base_anchor = anchors[base_anchor_id]
        initial_vad = self._markov_model.personality_adjusted_vad(
            base_anchor.vad,
            personality or self._loader.default_big_five(),
        )
        initial_eligible_anchor_ids = self._eligible_anchor_ids(None, [])
        anchor = anchors[
            self._markov_model.nearest_anchor_id(
                initial_vad,
                initial_eligible_anchor_ids,
            )
        ]
        return EmotionState(
            previous_anchor_id=anchor.id,
            current_vad=initial_vad,
            current_anchor_id=anchor.id,
            transition_strategy="expected_value",
            last_reason_summary="初始 VAD 由 neutral 基线和大五人格偏置共同确定。",
            reply_emotion_guidance=self._expression_guidance(
                anchor_id=anchor.id,
                previous_anchor_id=anchor.id,
                current_vad=initial_vad,
                actual_delta=VADVector(),
            ),
        )

    async def update_after_manager_message(self, state: SessionState, manager_message: str) -> SessionState:
        if not state.emotion_state:
            state.emotion_state = self.initial_state(
                state.intent.intent_id if state.intent else None,
                state.personality,
            )
        else:
            state.emotion_state.current_anchor_id = self._loader.resolve_emotion_anchor_id(
                state.emotion_state.current_anchor_id
            )
        task_name = "emotion_transition"
        settings = get_settings()
        try:
            output = await LangChainLLMService().ainvoke_structured_single(
                prompt=self._build_prompt(state, manager_message),
                schema=EmotionTransitionStructuredOutput,
                task_name=task_name,
                model=settings.model_for_task(task_name),
                temperature=settings.temperature_for_task(task_name),
                max_tokens=settings.max_tokens_for_task(task_name),
                timeout_seconds=settings.timeout_for_task(task_name),
                enable_thinking=settings.enable_thinking_for_task(task_name),
                payload_normalizer=self._normalize_structured_payload,
                structured_transport="json_schema",
                json_schema_strict=False,
            )
        except Exception as exc:  # noqa: BLE001
            error_code = getattr(exc, "code", type(exc).__name__)
            logger.warning("Emotion transition LLM failed, using fallback: error_type=%s", error_code)
            output = self._fallback_transition(state, manager_message)
            warning = f"情绪转移使用规则兜底：{error_code}"
            if warning not in state.warnings:
                state.warnings.append(warning)
        state.emotion_state = self._apply_transition(state, output, manager_message)
        return state

    def _build_prompt(self, state: SessionState, manager_message: str) -> str:
        history = state.conversation
        if (
            history
            and history[-1].speaker == "manager"
            and history[-1].text.strip() == manager_message.strip()
        ):
            history = history[:-1]
        personality_behavior_profile = (
            get_personality_behavior_resolver().resolve(
                state.personality,
                state.personality_facets,
            )
        )
        context = {
            "intent": state.intent.model_dump(exclude_none=True) if state.intent else {},
            "personality_behavior_profile": (
                personality_behavior_profile.model_dump(
                    mode="json",
                    exclude={
                        "matched_combination_ids",
                        "matched_combination_rules",
                    },
                )
                if personality_behavior_profile
                else {}
            ),
            "emotion_state": (
                state.emotion_state.model_dump(
                    mode="json",
                    exclude={"reply_emotion_guidance", "updated_at"},
                )
                if state.emotion_state
                else {}
            ),
            "emotion_anchors": [
                {
                    "id": anchor.id,
                    "name": anchor.name,
                    "description": " ".join(anchor.description.split()),
                    "vad": anchor.vad.model_dump(mode="json"),
                }
                for anchor in self._loader.emotion_anchors().values()
            ],
            "emotion_appraisal_policy": {
                "max_output_tags": self._anchor_selection_policy["max_output_tags"],
                "appraisal_tags": self._anchor_selection_policy["appraisal_tags"],
            },
            "conversation": self._emotion_prompt_conversation(history[-8:]),
            "latest_manager_message": manager_message,
        }
        return (
            "你只负责三维 VAD 情绪转移，不要读取、推断或修改动机满足度分数。"
            "基于对话历史、当前 VAD、大五人格、面谈目的和经理最新话术，判断员工下一轮情绪方向。"
            "personality_behavior_profile是按最终人格分数选择的稳定内部人格；每项detailed_description已将对应人格定义按唯一命中的档位展开为本档行为表现、具体线索、本档特有校准、情境调节和禁止推论；"
            "本档特有校准用于区分相邻档位，具体线索只是概率性例子；只在真实刺激触发对应构念时优先使用最具体facet，总维度只作背景，不得重复放大同向信号，并严格执行情境调节和禁止推论；"
            "只能执行当前档位要求，不得补回其他档位；人格只影响员工对刺激的敏感通道、心理权重和恢复方式；"
            "不得创造刺激，也不得让实际情绪变化方向违背经理本轮话语产生的刺激方向。"
            "dominance表示员工感到的掌控感和话语权变化。"
            "vad_delta是必填对象，表示本轮刺激建议的变化方向与幅度预算，而不是下一状态坐标，必须同时包含valence、arousal、dominance，"
            "三个变化值都在[-1,1]。按刺激的真实力度校准绝对值："
            "没有明显情绪刺激时各轴通常不超过0.04；"
            "轻微刺激的主轴通常为0.10到0.25；"
            "语义方向校准：不公平评价或直接冲突通常降低valence并提高arousal，若员工主动抗辩则dominance上升；"
            "对后果不确定通常降低valence、提高arousal并降低dominance；"
            "受挫退缩通常降低valence、arousal和dominance；"
            "获得具体支持通常提高valence和dominance并降低arousal；"
            "获得认可且看到推进机会通常提高valence、arousal和dominance。"
            "明确反馈、支持或施压的主轴通常为0.30到0.55；"
            "强烈对抗、威胁或重大认可的主轴通常为0.60到0.90。"
            "职业化只约束员工如何表达，不得把明确刺激压缩为接近零的变化。"
            "transition_strategy必填且只能是expected_value、maximum_probability、sampling之一；"
            "只有刺激方向明确且主轴达到0.35以上时才可以选择maximum_probability，"
            "轻微或方向混合的变化选择expected_value。"
            "appraisal_tags必填且必须是数组，可以为空；只能从context.emotion_appraisal_policy.appraisal_tags中选择，"
            "最多选择context中规定的max_output_tags个。只有对话中存在明确证据时才选择；"
            "这些标签只表示员工主观事件评估的证据条件，不认定客观事实、责任、公平性或风险必然成立。"
            "对与本轮刺激相关的候选标签逐项核对；多个标签分别有明确证据时应同时返回，不因语义相近而合并。"
            "context.emotion_anchors保留每种情绪的一行情绪语义和VAD代表坐标，只用于校准情绪区域与vad_delta；"
            "锚点坐标是区域代表点，不是固定边界或下一状态坐标。"
            "最终可用锚点仍由后端门控和Markov模型决定，不要输出锚点ID。"
            "不能仅因VAD坐标接近某锚点就补标签，也不能仅因Manager指责员工就选择possible_employee_contribution。"
            "不要使用二维 VA，不要假设固定情绪数量。reason_summary保持简短。"
            "只返回一个匹配响应Schema的对象，不要附加解释文字。"
            f"context={json.dumps(context, ensure_ascii=False)}"
        )

    @staticmethod
    def _emotion_prompt_conversation(
        turns: list[ConversationTurn],
    ) -> list[dict[str, object]]:
        """Expose dialogue semantics without timestamps or operational metadata."""

        return [
            {
                "turn_index": turn.turn_index,
                "speaker": turn.speaker,
                "text": turn.text,
            }
            for turn in turns
        ]

    def _normalize_structured_payload(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        aliases = {
            "vad_delta": ("vadDelta", "vad", "delta", "emotion_delta", "next_vad_delta"),
            "transition_strategy": ("strategy", "transitionStrategy", "selection_strategy"),
            "appraisal_tags": (
                "appraisalTags",
                "appraisals",
                "semantic_tags",
                "emotion_evidence_tags",
            ),
            "reason_summary": ("reason", "summary", "reasonSummary"),
        }
        normalized = dict(payload)
        repair_steps: list[str] = []
        for canonical, candidates in aliases.items():
            if canonical in normalized:
                continue
            for alias in candidates:
                if alias in normalized:
                    normalized[canonical] = normalized[alias]
                    repair_steps.append(f"mapped_{alias}_to_{canonical}")
                    break

        if "vad_delta" not in normalized or normalized.get("vad_delta") is None:
            raise StructuredOutputError(
                "missing_core_field",
                "Emotion transition output is missing vad_delta.",
            )
        vad_raw = normalized.get("vad_delta")
        if isinstance(vad_raw, (list, tuple)):
            if len(vad_raw) != 3:
                raise StructuredOutputError(
                    "schema_validation",
                    "vad_delta array must contain valence, arousal, and dominance.",
                )
            vad_values: dict[str, Any] = dict(zip(("valence", "arousal", "dominance"), vad_raw, strict=True))
            repair_steps.append("converted_vad_array_to_object")
        elif isinstance(vad_raw, dict):
            vad_values = vad_raw
        else:
            raise StructuredOutputError("schema_validation", "vad_delta must be an object or three-item array.")

        axis_aliases = {
            "valence": ("valence", "v", "pleasure", "valence_delta"),
            "arousal": ("arousal", "a", "activation", "arousal_delta"),
            "dominance": ("dominance", "d", "control", "dominance_delta"),
        }
        normalized_vad_keys = {self._normalized_key(str(key)): value for key, value in vad_values.items()}
        vad_delta: dict[str, float] = {}
        found_axes = 0
        for axis, candidates in axis_aliases.items():
            raw_value: Any = None
            matched_alias: str | None = None
            for candidate in candidates:
                key = self._normalized_key(candidate)
                if key in normalized_vad_keys:
                    raw_value = normalized_vad_keys[key]
                    matched_alias = candidate
                    break
            if matched_alias is None:
                vad_delta[axis] = 0.0
                repair_steps.append(f"filled_missing_{axis}_with_zero")
                continue
            found_axes += 1
            if matched_alias != axis:
                repair_steps.append(f"mapped_{matched_alias}_to_{axis}")
            value, value_steps = self._bounded_vad_number(raw_value, field_name=f"vad_delta.{axis}")
            vad_delta[axis] = value
            repair_steps.extend(value_steps)
        if found_axes == 0:
            raise StructuredOutputError(
                "missing_core_field",
                "vad_delta does not contain any recognizable VAD axis.",
            )

        if "transition_strategy" not in normalized or normalized.get("transition_strategy") is None:
            raise StructuredOutputError(
                "missing_core_field",
                "Emotion transition output is missing transition_strategy.",
            )
        strategy, strategy_steps = self._normalized_strategy(normalized.get("transition_strategy"))
        repair_steps.extend(strategy_steps)
        appraisal_tags, appraisal_steps = self._normalized_appraisal_tags(
            normalized.get("appraisal_tags")
        )
        repair_steps.extend(appraisal_steps)

        result = {
            "vad_delta": vad_delta,
            "transition_strategy": strategy,
            "appraisal_tags": appraisal_tags,
            "reason_summary": self._text_value(normalized.get("reason_summary"), repair_steps),
        }
        return result, list(dict.fromkeys(repair_steps))

    @staticmethod
    def _bounded_vad_number(value: Any, *, field_name: str) -> tuple[float, list[str]]:
        repair_steps: list[str] = []
        percentage = False
        if isinstance(value, bool):
            raise StructuredOutputError("schema_validation", f"{field_name} must be numeric.")
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            if cleaned.endswith("%"):
                cleaned = cleaned[:-1].strip()
                percentage = True
                repair_steps.append(f"parsed_percent_{field_name}")
            try:
                number = float(cleaned)
            except ValueError as exc:
                raise StructuredOutputError("schema_validation", f"{field_name} must be numeric.") from exc
            repair_steps.append(f"parsed_numeric_string_{field_name}")
        elif isinstance(value, (int, float)):
            number = float(value)
        else:
            raise StructuredOutputError("schema_validation", f"{field_name} must be numeric.")
        if percentage:
            number /= 100.0
        if not math.isfinite(number):
            raise StructuredOutputError("schema_validation", f"{field_name} must be finite.")
        bounded = max(-1.0, min(1.0, number))
        if bounded != number:
            repair_steps.append(f"clamped_{field_name}")
        return bounded, repair_steps

    @classmethod
    def _normalized_strategy(cls, value: Any) -> tuple[str, list[str]]:
        if not isinstance(value, str) or not value.strip():
            raise StructuredOutputError("schema_validation", "transition_strategy must be a non-empty string.")
        token = cls._normalized_key(value)
        mappings = {
            "expected_value": {
                "expectedvalue",
                "expected",
                "mean",
                "average",
                "weightedaverage",
                "期望值",
                "加权平均",
            },
            "maximum_probability": {
                "maximumprobability",
                "maximum",
                "max",
                "argmax",
                "最大概率",
            },
            "sampling": {
                "sampling",
                "sample",
                "probabilisticsampling",
                "probabilistic",
                "概率采样",
            },
        }
        for canonical, aliases in mappings.items():
            if token in aliases:
                return canonical, ([] if value == canonical else ["normalized_transition_strategy"])
        raise StructuredOutputError("schema_validation", "transition_strategy is not recognized.")

    def _normalized_appraisal_tags(self, value: Any) -> tuple[list[str], list[str]]:
        repair_steps: list[str] = []
        if value is None:
            return [], ["filled_missing_appraisal_tags_with_empty"]
        if isinstance(value, str):
            raw_tags = [
                item
                for item in re.split(r"[,;，；\s]+", value.strip())
                if item
            ]
            repair_steps.append("split_appraisal_tags_string")
        elif isinstance(value, (list, tuple, set)):
            raw_tags = list(value)
        elif isinstance(value, dict):
            raw_tags = [key for key, enabled in value.items() if enabled is True]
            repair_steps.append("converted_appraisal_tags_mapping")
            if any(enabled is not True for enabled in value.values()):
                repair_steps.append("discarded_non_true_appraisal_tag_flags")
        else:
            return [], ["discarded_invalid_appraisal_tags"]

        allowed_tags = self._anchor_selection_policy["appraisal_tags"]
        normalized_allowed = {
            self._normalized_key(tag_id): tag_id for tag_id in allowed_tags
        }
        tags: list[str] = []
        for raw_tag in raw_tags:
            if not isinstance(raw_tag, str):
                repair_steps.append("discarded_invalid_appraisal_tag")
                continue
            tag_id = normalized_allowed.get(self._normalized_key(raw_tag))
            if tag_id is None:
                repair_steps.append("discarded_unknown_appraisal_tag")
                continue
            if tag_id in tags:
                repair_steps.append("deduplicated_appraisal_tag")
                continue
            tags.append(tag_id)

        max_output_tags = self._anchor_selection_policy["max_output_tags"]
        if len(tags) > max_output_tags:
            tags = tags[:max_output_tags]
            repair_steps.append("truncated_appraisal_tags")
        return tags, repair_steps

    @staticmethod
    def _text_value(value: Any, repair_steps: list[str]) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            repair_steps.append("joined_text_list")
            return "; ".join(str(item).strip() for item in value if item is not None and str(item).strip())
        repair_steps.append("converted_value_to_text")
        return str(value).strip()

    @staticmethod
    def _normalized_key(value: str) -> str:
        return re.sub(r"[\s_\-]+", "", value.casefold())

    @staticmethod
    def _contains_unnegated_phrase(
        text: str,
        phrases: tuple[str, ...],
    ) -> bool:
        negation_before_phrase = re.compile(
            r"(?:不|未|没|没有|无|非|无法|不能|不会|不是|并非).{0,3}$"
        )
        for phrase in phrases:
            search_from = 0
            while True:
                index = text.find(phrase, search_from)
                if index < 0:
                    break
                prefix = text[max(0, index - 10) : index]
                if not negation_before_phrase.search(prefix):
                    return True
                search_from = index + len(phrase)
        return False

    def _fallback_transition(self, state: SessionState, manager_message: str) -> EmotionTransitionStructuredOutput:
        text = manager_message or ""
        positive = self._contains_unnegated_phrase(
            text,
            (
                "理解你的",
                "支持你",
                "提供支持",
                "提供资源",
                "给你资源",
                "一起讨论",
                "一起解决",
                "谢谢",
                "认可你的",
                "承认你的",
                "可以讨论",
            ),
        )
        negative = self._contains_unnegated_phrase(
            text,
            (
                "威胁",
                "辞退",
                "解雇",
                "态度不好",
                "别找理由",
                "不接受你的解释",
                "就是你的问题",
                "你必须接受",
                "必须服从",
                "没有商量余地",
            ),
        )
        if positive and not negative:
            delta = VADVector(valence=0.35, arousal=-0.20, dominance=0.25)
            reason_summary = "support_or_empathy"
        elif negative and not positive:
            delta = VADVector(valence=-0.40, arousal=0.40, dominance=-0.25)
            reason_summary = "pressure_or_denial"
        else:
            delta = VADVector(valence=-0.03, arousal=0.04, dominance=0.0)
            reason_summary = "unclear_or_neutral"
        return EmotionTransitionStructuredOutput(
            vad_delta=delta,
            transition_strategy="expected_value",
            # A rules fallback can estimate broad VAD direction, but it cannot
            # establish the contextual facts required by semantic anchors.
            appraisal_tags=[],
            reason_summary=reason_summary,
        )

    def _eligible_anchor_ids(
        self,
        current_anchor_id: str | None,
        appraisal_tags: list[str],
    ) -> set[str]:
        anchors = self._loader.emotion_anchors()
        policy = self._anchor_selection_policy
        if policy["mode"] == "observe":
            return set(anchors)

        active_tags = set(appraisal_tags)
        eligible = set(policy["direct_vad_anchor_ids"])
        for anchor_id, gate in policy["contextual_anchor_gates"].items():
            required_any = set(gate["requires_any"])
            required_all = set(gate["requires_all"])
            if required_all.issubset(active_tags) and (
                not required_any or bool(required_any & active_tags)
            ):
                eligible.add(anchor_id)

        if policy["preserve_current_anchor"] and current_anchor_id in anchors:
            eligible.add(current_anchor_id)
        eligible.add(self._loader.default_emotion_anchor_id(None))
        return eligible

    def _apply_transition(
        self,
        state: SessionState,
        output: EmotionTransitionStructuredOutput,
        manager_message: str,
    ) -> EmotionState:
        current_state = state.emotion_state or self.initial_state(
            state.intent.intent_id if state.intent else None,
            state.personality,
        )
        resolved_anchor_id = self._loader.resolve_emotion_anchor_id(
            current_state.current_anchor_id
        )
        if resolved_anchor_id in self._markov_model.anchor_by_id:
            current_anchor_id = resolved_anchor_id
        else:
            repair_eligible_anchor_ids = self._eligible_anchor_ids(
                None,
                output.appraisal_tags,
            )
            current_anchor_id = self._markov_model.nearest_anchor_id(
                current_state.current_vad,
                repair_eligible_anchor_ids,
            )
        eligible_anchor_ids = self._eligible_anchor_ids(
            current_anchor_id,
            output.appraisal_tags,
        )
        decision = self._markov_model.transition(
            current_vad=current_state.current_vad,
            current_anchor_id=current_anchor_id,
            vad_delta=output.vad_delta,
            personality=state.personality,
            requested_strategy=output.transition_strategy,
            eligible_anchor_ids=eligible_anchor_ids,
        )
        logger.info(
            "Emotion transition applied: source=%s target=%s result=%s "
            "strategy=%s intensity=%.3f rate=%.3f input_delta=%s "
            "before_vad=%s after_vad=%s actual_delta=%s "
            "appraisal_tags=%s eligible_anchors=%d "
            "top_probabilities=%s manager_message_chars=%d",
            current_anchor_id,
            decision.target_anchor_id,
            decision.current_anchor_id,
            decision.strategy,
            decision.intensity,
            decision.transition_rate,
            self._rounded_vad(output.vad_delta),
            self._rounded_vad(current_state.current_vad),
            self._rounded_vad(decision.next_vad),
            self._rounded_vad(decision.actual_delta),
            output.appraisal_tags,
            len(eligible_anchor_ids),
            self._top_probabilities(decision),
            len(manager_message),
        )
        actual_change = max(
            abs(decision.actual_delta.valence),
            abs(decision.actual_delta.arousal),
            abs(decision.actual_delta.dominance),
        )
        record_emotion_transition(
            source_anchor=current_anchor_id,
            target_anchor=decision.target_anchor_id,
            result_anchor=decision.current_anchor_id,
            strategy=decision.strategy,
            stimulus_intensity=decision.intensity,
            actual_change=actual_change,
            anchor_changed=current_anchor_id != decision.current_anchor_id,
            strength=self._transition_strength(actual_change),
        )
        return EmotionState(
            current_vad=decision.next_vad,
            current_anchor_id=decision.current_anchor_id,
            previous_anchor_id=current_anchor_id,
            last_vad_delta=decision.actual_delta,
            transition_intensity=decision.intensity,
            transition_strategy=decision.strategy,
            last_reason_summary=output.reason_summary or "情绪状态已按本轮刺激更新。",
            reply_emotion_guidance=self._reply_emotion_guidance(
                decision,
                previous_anchor_id=current_anchor_id,
            ),
            has_manager_response=True,
            updated_at=datetime.now(timezone.utc),
        )

    def _reply_emotion_guidance(
        self,
        decision: EmotionTransitionDecision,
        *,
        previous_anchor_id: str | None = None,
    ) -> str:
        return self._expression_guidance(
            anchor_id=decision.current_anchor_id,
            current_vad=decision.next_vad,
            actual_delta=decision.actual_delta,
            previous_anchor_id=previous_anchor_id,
        )

    def _expression_guidance(
        self,
        *,
        anchor_id: str,
        current_vad: VADVector,
        actual_delta: VADVector,
        previous_anchor_id: str | None = None,
    ) -> str:
        anchor = self._loader.emotion_anchors()[anchor_id]
        profile = self._loader.emotion_expression_profiles()[anchor_id]
        policy = self._expression_policy
        field_semantics = policy["field_semantics"]
        actual_intensity = max(
            abs(actual_delta.valence),
            abs(actual_delta.arousal),
            abs(actual_delta.dominance),
        )
        profile_field_by_axis = {
            "valence": "stance_cue",
            "arousal": "rhythm_cue",
            "dominance": "agency_cue",
        }
        rank_by_delta = actual_intensity >= 0.04
        ranked_axes = sorted(
            ("valence", "arousal", "dominance"),
            key=lambda axis: (
                abs(getattr(actual_delta if rank_by_delta else current_vad, axis)),
                abs(getattr(current_vad if rank_by_delta else actual_delta, axis)),
            ),
            reverse=True,
        )
        ranked_profile_fields = [
            profile_field_by_axis[axis] for axis in ranked_axes
        ]
        primary_field = ranked_profile_fields[0]
        secondary_field = ranked_profile_fields[1]
        primary_cue = (
            f"{field_semantics[primary_field].rstrip('。；')}；"
            f"具体风格：{profile[primary_field].rstrip('。；')}"
        )
        secondary_cue = (
            f"{field_semantics[secondary_field].rstrip('。；')}；"
            f"具体风格：{profile[secondary_field].rstrip('。；')}"
        )
        direction_signal = self._direction_signal(actual_delta)

        if actual_intensity >= 0.25:
            strength = (
                "本轮内部情绪变化强；若 Human Turn Selection 选择外显，"
                "应让一项主风格信号清楚可感知；若与已选内容自然兼容，可同时带出次要信号，"
                "但不显式命名情绪，也不要求固定信号数量"
            )
            optional_cues = (
                f"优先表达提示：{primary_cue}；可选次要提示：{secondary_cue}"
            )
        elif actual_intensity >= 0.10:
            strength = (
                "本轮内部情绪变化明确；若选择外显，落实一项清楚但不过度的风格变化即可，"
                "不要求多个信号"
            )
            optional_cues = f"主要表达提示：{primary_cue}"
        elif actual_intensity >= 0.04:
            strength = "本轮内部情绪变化轻微；仅作细微风格调节，不为展示情绪新增内容"
            optional_cues = f"若自然外显，可参考：{primary_cue}"
        else:
            strength = "本轮内部情绪基本稳定；保持状态连续，不要求专门外显"
            optional_cues = f"如本轮内容自然涉及情绪，可参考：{primary_cue}"

        parts = [
            f"本轮内部情绪基调：{anchor.name}",
            strength,
            optional_cues,
        ]
        if direction_signal:
            parts.append(f"若本轮选择外显，相较上一轮可作的风格调节：{direction_signal}")
        if previous_anchor_id == anchor_id and actual_intensity >= 0.04:
            parts.append(
                "锚点未变但内部情绪已移动；仅在本轮选择外显时调整相应强度"
            )
        elif previous_anchor_id and previous_anchor_id != anchor_id:
            parts.append(
                "内部锚点已经转换；是否让变化可感知仍由 Human Turn Selection 决定"
            )
        parts.extend(
            [
                f"禁止表现：{profile['avoid']}",
                f"情绪表达边界：{policy['role']}",
                f"全局表达不变量：{'；'.join(policy['invariants'])}",
                (
                    "情绪锚点和表达提示不是事实、立场或行动证据；"
                    "情绪只能调节本轮已选内容的情绪色彩、确定度、节奏、信息密度、停顿感和主控感；"
                    "不得新增事实、立场、提问、行动、诉求或承诺；"
                    "不要复述本合同，也不要输出系统情绪标签、数值、舞台动作或分析文字"
                ),
            ]
        )
        return "。".join(part.rstrip("。") for part in parts) + "。"

    @staticmethod
    def _direction_signal(delta: VADVector) -> str:
        changes: list[tuple[float, str]] = [
            (
                abs(delta.valence),
                "正向情绪色彩可略增强，但不得自动形成接受、感谢、附和或配合"
                if delta.valence > 0
                else "正向缓冲可略减少，但不得自动生成分歧、责备、失望结论或保留立场",
            ),
            (
                abs(delta.arousal),
                "节奏和激活感可略提高，信息组织保持清楚"
                if delta.arousal > 0
                else "节奏可放缓、信息密度可降低，但不自动改变任何立场",
            ),
            (
                abs(delta.dominance),
                "已有内容可表达得更确定直接，但不得新增判断、条件或边界"
                if delta.dominance > 0
                else "已有内容的断言强度可降低，但不得自动新增提问、求助或让步",
            ),
        ]
        ranked = sorted(changes, key=lambda item: item[0], reverse=True)
        return "；".join(message for magnitude, message in ranked[:2] if magnitude >= 0.04)

    @staticmethod
    def _transition_strength(actual_change: float) -> str:
        if actual_change >= 0.25:
            return "strong"
        if actual_change >= 0.10:
            return "clear"
        if actual_change >= 0.04:
            return "subtle"
        return "stable"

    @staticmethod
    def _top_probabilities(
        decision: EmotionTransitionDecision,
    ) -> list[tuple[str, float]]:
        ranked = sorted(
            decision.probabilities.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        return [(anchor_id, round(probability, 4)) for anchor_id, probability in ranked[:3]]

    @staticmethod
    def _rounded_vad(vad: VADVector) -> dict[str, float]:
        return {
            "valence": round(vad.valence, 4),
            "arousal": round(vad.arousal, 4),
            "dominance": round(vad.dominance, 4),
        }

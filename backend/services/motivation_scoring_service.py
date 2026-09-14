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
from backend.schemas.conversation import ConversationTurn
from backend.schemas.simulation import MotivationScoringStructuredOutput, MotivationState
from backend.schemas.state import SessionState
from backend.services.langchain_llm_service import LangChainLLMService

logger = logging.getLogger(__name__)


# A cue is only affected by operators in its own local clause.  This keeps a
# negation in one part of a compound sentence from reversing unrelated cues
# after punctuation or a contrast/addition boundary.
_CUE_SCOPE_BOUNDARY_RE = re.compile(
    r"(?:[，。！？；,.!?;：:\n]+|但是|不过|然而|可是|同时|另外|并且|而且|却|但|也)"
)
_NEGATING_OPERATOR_RE = re.compile(
    r"(?:"
    r"并不是|并非|并不|不是|不会|不能|不愿意|不愿|不想|不肯|不再|不曾|"
    r"没有|没能|无法|无意|未能|未曾|从未|绝不|难以|很难|不要|"
    r"看不到|缺乏|缺少|欠缺|拒绝|否认|否定|忽视|无视|停止|取消|撤回|"
    r"削减|减少|别|不|没|未|无"
    r")"
)


class MotivationScoringService:
    """Updates motive satisfaction scores only; it never reads or writes VAD."""

    async def update_after_manager_message(self, state: SessionState, manager_message: str) -> SessionState:
        if not state.motivation:
            return state
        task_name = "motivation_scoring"
        settings = get_settings()
        try:
            output = await LangChainLLMService().ainvoke_structured_single(
                prompt=self._build_prompt(state, manager_message),
                schema=MotivationScoringStructuredOutput,
                task_name=task_name,
                model=settings.model_for_task(task_name),
                temperature=settings.temperature_for_task(task_name),
                max_tokens=settings.max_tokens_for_task(task_name),
                timeout_seconds=settings.timeout_for_task(task_name),
                enable_thinking=settings.enable_thinking_for_task(task_name),
                payload_normalizer=lambda payload: self._normalize_structured_payload(state, payload),
            )
        except Exception as exc:  # noqa: BLE001
            error_code = getattr(exc, "code", type(exc).__name__)
            logger.warning("Motivation scoring LLM failed, using fallback: error_type=%s", error_code)
            output = self._fallback_score(state, manager_message)
            warning = f"动机满足度评分使用规则兜底：{error_code}"
            if warning not in state.warnings:
                state.warnings.append(warning)
        state.motivation = self._apply_score_change(state.motivation, output)
        return state

    def _build_prompt(self, state: SessionState, manager_message: str) -> str:
        motive = state.motivation
        assert motive is not None
        selected_motive_ids = list(
            dict.fromkeys(
                [motive.primary_motive_id, *motive.secondary_motive_ids]
            )
        )
        motive_options = get_config_loader().motives()
        motives = {
            motive_id: motive_options[motive_id].model_dump()
            for motive_id in selected_motive_ids
            if motive_id in motive_options
        }
        context = {
            "intent": (
                {"intent_id": state.intent.intent_id}
                if state.intent
                else {}
            ),
            "motivation": {
                "primary_motive_id": motive.primary_motive_id,
                "secondary_motive_ids": list(motive.secondary_motive_ids),
                "primary_score": motive.primary_score,
                "secondary_scores": dict(motive.secondary_scores),
                "has_manager_response": motive.has_manager_response,
            },
            "motives": motives,
            "current_runtime_context": {
                "runtime_notes": list(state.rehearsal_context.runtime_notes),
            },
            "conversation": self._motivation_prompt_conversation(
                state.conversation,
                manager_message,
            ),
            "latest_manager_message": manager_message,
        }
        selected_secondary_ids = motive.secondary_motive_ids
        scoring_weights = (
            "主诉求权重70%，辅助诉求合计权重30%；选择一个辅助诉求时占30%，选择两个时各占15%。"
            if selected_secondary_ids
            else "未选择辅助诉求；主诉求权重为100%，secondary_score_deltas必须为空对象。"
        )
        return (
            "你只负责评估员工主/辅诉求满足度，不要输出或推断情绪 VAD。"
            "primary_score_delta 是必填的本轮变化值，范围[-100,100]。"
            "secondary_score_deltas 只能使用选中的辅诉求ID作为键："
            f"{json.dumps(selected_secondary_ids, ensure_ascii=False)}。"
            f"{scoring_weights}"
            "current_runtime_context只帮助理解员工当前背景，不能作为经理本轮已经共情、否定、提供路径或触发红线的证据。"
            "根据经理最新话术是否共情、否定、给出落地路径、触发红线，输出各诉求分数变化。"
            "退出或改进+退出场景中，若出现“态度不好”且没有具体行为证据，命中红线。"
            "reason_summary简要说明本轮变化的主要依据。"
            "必须调用指定工具一次，不要附加解释文字。"
            f"context={json.dumps(context, ensure_ascii=False)}"
        )

    @staticmethod
    def _motivation_prompt_conversation(
        turns: list[ConversationTurn],
        latest_manager_message: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        """Project only prior dialogue semantics into Motivation scoring."""

        dialogue_turns = [
            turn
            for turn in turns
            if turn.speaker in {"manager", "employee"}
        ]
        if dialogue_turns:
            latest_turn = dialogue_turns[-1]
            if (
                latest_turn.speaker == "manager"
                and latest_turn.text.strip() == latest_manager_message.strip()
            ):
                dialogue_turns.pop()

        return [
            {
                "turn_index": turn.turn_index,
                "speaker": turn.speaker,
                "text": turn.text,
            }
            for turn in dialogue_turns[-limit:]
        ]

    def _normalize_structured_payload(
        self,
        state: SessionState,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        aliases = {
            "primary_score_delta": ("primary_delta", "primaryScoreDelta", "primary_motive_delta", "main_score_delta"),
            "secondary_score_deltas": (
                "secondary_deltas",
                "secondaryScoreDeltas",
                "secondary_motive_deltas",
                "secondary_scores",
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

        if "primary_score_delta" not in normalized or normalized.get("primary_score_delta") is None:
            raise StructuredOutputError(
                "missing_core_field",
                "Motivation scoring output is missing primary_score_delta.",
            )
        primary_delta, primary_steps = self._bounded_number(
            normalized.get("primary_score_delta"),
            minimum=-100.0,
            maximum=100.0,
            field_name="primary_score_delta",
        )
        repair_steps.extend(primary_steps)

        secondary_raw = normalized.get("secondary_score_deltas")
        secondary_items: list[tuple[Any, Any]] = []
        if secondary_raw is None:
            secondary_items = []
        elif isinstance(secondary_raw, dict):
            secondary_items = list(secondary_raw.items())
        elif isinstance(secondary_raw, list):
            for item in secondary_raw:
                if not isinstance(item, dict):
                    repair_steps.append("discarded_invalid_secondary_entry")
                    continue
                motive_key = item.get("motive_id") or item.get("id") or item.get("name") or item.get("dimension")
                delta_value = item.get("score_delta")
                if delta_value is None:
                    delta_value = item.get("delta") if item.get("delta") is not None else item.get("value")
                secondary_items.append((motive_key, delta_value))
            repair_steps.append("converted_secondary_list_to_object")
        else:
            repair_steps.append("discarded_invalid_secondary_container")

        selected_ids = list(state.motivation.secondary_motive_ids if state.motivation else [])
        motive_options = get_config_loader().motives()
        alias_targets: dict[str, set[str]] = {}
        for motive_id, option in motive_options.items():
            for candidate in (motive_id, option.name, option.dimension):
                alias_targets.setdefault(self._normalized_motive_key(candidate), set()).add(motive_id)

        secondary_deltas: dict[str, float] = {}
        for raw_key, raw_value in secondary_items:
            motive_id: str | None = None
            if isinstance(raw_key, str):
                if raw_key in selected_ids:
                    motive_id = raw_key
                else:
                    candidates = alias_targets.get(self._normalized_motive_key(raw_key), set())
                    selected_candidates = candidates.intersection(selected_ids)
                    if len(selected_candidates) == 1:
                        motive_id = next(iter(selected_candidates))
                        repair_steps.append("normalized_secondary_motive_id")
            if motive_id is None:
                repair_steps.append("discarded_unknown_secondary_motive")
                continue
            try:
                delta, delta_steps = self._bounded_number(
                    raw_value,
                    minimum=-100.0,
                    maximum=100.0,
                    field_name=f"secondary_score_deltas.{motive_id}",
                )
            except StructuredOutputError:
                repair_steps.append("discarded_invalid_secondary_delta")
                continue
            secondary_deltas[motive_id] = delta
            repair_steps.extend(delta_steps)

        result = {
            "primary_score_delta": primary_delta,
            "secondary_score_deltas": secondary_deltas,
            "reason_summary": self._text_value(normalized.get("reason_summary"), repair_steps),
        }
        return result, list(dict.fromkeys(repair_steps))

    @staticmethod
    def _bounded_number(
        value: Any,
        *,
        minimum: float,
        maximum: float,
        field_name: str,
    ) -> tuple[float, list[str]]:
        repair_steps: list[str] = []
        if isinstance(value, bool):
            raise StructuredOutputError("schema_validation", f"{field_name} must be numeric.")
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            if cleaned.endswith("%"):
                cleaned = cleaned[:-1].strip()
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
        if not math.isfinite(number):
            raise StructuredOutputError("schema_validation", f"{field_name} must be finite.")
        bounded = max(minimum, min(maximum, number))
        if bounded != number:
            repair_steps.append(f"clamped_{field_name}")
        return bounded, repair_steps

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
    def _normalized_motive_key(value: str) -> str:
        return re.sub(r"[\s_\-]+", "", value.casefold())

    def _fallback_score(self, state: SessionState, manager_message: str) -> MotivationScoringStructuredOutput:
        text = manager_message or ""
        intent_id = state.intent.intent_id if state.intent else ""
        empathy, negated_empathy = self._cue_polarities(
            text,
            ["理解", "辛苦", "压力", "感受", "落差", "担心", "确实不容易"],
        )
        plan, negated_plan = self._cue_polarities(
            text,
            ["计划", "目标", "路径", "资源", "支持", "安排", "阶段", "时间", "过渡", "补偿", "方案"],
        )
        explicit_denial = any(
            token in text
            for token in ["别找理由", "不是问题", "没什么可说", "你必须", "公司已经决定", "不接受", "就是你的问题"]
        )
        # A negated positive cue is itself negative evidence only when that cue
        # has no affirmative occurrence in the same message.  This preserves
        # mixed statements such as “暂时不能承诺资源，但会安排支持”.
        denied_empathy = negated_empathy and not empathy
        withheld_action_path = negated_plan and not plan
        denial = explicit_denial or denied_empathy or withheld_action_path
        behavior_evidence = any(token in text for token in ["行为", "例子", "事实", "记录", "具体", "数据", "证据"])
        redline = "态度不好" in text and intent_id in {"exit", "improvement_exit"} and not behavior_evidence
        primary_delta = 0.0
        secondary_delta = 0.0
        behaviors: list[str] = []
        redlines: list[str] = []
        if empathy:
            primary_delta += 8
            secondary_delta += 5
            behaviors.append("empathy")
        if plan:
            primary_delta += 15
            secondary_delta += 8
            behaviors.append("action_path")
        if denial:
            primary_delta -= 18
            secondary_delta -= 12
            if denied_empathy:
                behaviors.append("negated_empathy")
            if withheld_action_path:
                behaviors.append("withheld_action_path")
            behaviors.append("denial_or_pressure")
        if redline:
            primary_delta -= 35
            secondary_delta -= 20
            redlines.append("attitude_without_behavior_evidence")
        if not behaviors and not redlines:
            behaviors.append("neutral_or_unclear")
        motive = state.motivation
        secondary = {motive_id: secondary_delta for motive_id in (motive.secondary_motive_ids if motive else [])}
        return MotivationScoringStructuredOutput(
            primary_score_delta=primary_delta,
            secondary_score_deltas=secondary,
            reason_summary=";".join(behaviors + redlines),
        )

    @classmethod
    def _cue_polarities(cls, text: str, cues: list[str]) -> tuple[bool, bool]:
        """Return whether a cue occurs affirmatively and under negation.

        Chinese negation commonly scopes over an object several characters
        later (for example, “不会提供资源支持”), so checking only the character
        immediately before a keyword is insufficient.  We inspect the prefix
        inside the cue's local clause and use operator parity so common double
        negations such as “不是不理解” remain affirmative.
        """

        affirmative = False
        negated = False
        for cue in cues:
            for match in re.finditer(re.escape(cue), text):
                if cls._cue_is_negated(text, match.start(), cue):
                    negated = True
                else:
                    affirmative = True
        return affirmative, negated

    @staticmethod
    def _cue_is_negated(text: str, cue_start: int, cue: str) -> bool:
        prefix = text[:cue_start]
        boundaries = list(_CUE_SCOPE_BOUNDARY_RE.finditer(prefix))
        if boundaries:
            prefix = prefix[boundaries[-1].end() :]
        # Keep the scope local even in punctuation-free ASR transcripts.
        prefix = prefix[-32:]

        # “别/不用/无需/不必担心” is reassurance rather than a denial of the
        # employee's concern, so it must not create negative motivation evidence.
        if cue == "担心" and re.search(r"(?:别|不用|无需|不必)\s*$", prefix):
            return False

        operators = list(_NEGATING_OPERATOR_RE.finditer(prefix))
        return len(operators) % 2 == 1

    @staticmethod
    def _apply_score_change(motivation: MotivationState, output: MotivationScoringStructuredOutput) -> MotivationState:
        motivation.primary_score = max(-100.0, min(100.0, motivation.primary_score + output.primary_score_delta))
        for motive_id in motivation.secondary_motive_ids:
            delta = float(output.secondary_score_deltas.get(motive_id, 0.0))
            current = motivation.secondary_scores.get(motive_id, 0.0)
            motivation.secondary_scores[motive_id] = max(-100.0, min(100.0, current + delta))
        motivation.has_manager_response = True
        motivation.last_change_reason = output.reason_summary or "本轮诉求满足度已更新。"
        motivation.updated_at = datetime.now(timezone.utc)
        return MotivationState.model_validate(motivation.model_dump(mode="json"))

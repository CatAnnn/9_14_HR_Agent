from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
import time
from typing import Any

from backend.agents.employee_agent import EmployeeAgent
from backend.config.settings import Settings, get_settings
from backend.exceptions.llm_errors import StructuredOutputError
from backend.observability.metrics import log_metric
from backend.schemas.conversation_summary import ConversationPromptContext
from backend.schemas.simulation import (
    PatternResponseGuidance,
    PsychologicalPatternActivation,
    PsychologicalPatternDynamicsStructuredOutput,
    PsychologicalPatternInteraction,
    PsychologicalPatternState,
)
from backend.schemas.state import SessionState
from backend.services.conversation_summary_service import (
    ConversationSummaryService,
    get_conversation_summary_service,
)
from backend.services.emotion_transition_service import EmotionTransitionService
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.model_scheduler import ModelScheduler
from backend.services.psychological_pattern_catalog import (
    PsychologicalPatternCatalogLoader,
    PsychologicalPatternCatalogSnapshot,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmployeeStateTransitionResult:
    """Merged concurrent transition result plus ephemeral reply guidance."""

    state: SessionState
    pattern_response_guidance: str = ""
    pattern_dynamics_applied: bool = False
    emotion_transition_ms: int = 0
    pattern_dynamics_ms: int = 0
    emotion_model_queue_ms: float = 0.0
    pattern_model_queue_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class _EmotionBranchResult:
    state: SessionState
    duration_ms: int
    queue_ms: float


@dataclass(frozen=True, slots=True)
class _PatternBranchResult:
    state: SessionState
    guidance: str
    applied: bool
    duration_ms: int
    queue_ms: float


class EmployeeStateTransitionService:
    """Updates emotion and Pattern Dynamics with two concurrent model calls."""

    _STRENGTHS = ("weak", "medium", "medium_high", "strong")
    _PATTERN_EMOTION_SNAPSHOT_FIELDS = (
        "valence",
        "arousal",
        "dominance",
        "anchor_id",
        "previous_anchor_id",
        "vad_delta",
        "transition_intensity",
        "reason_summary",
    )
    # Keep the full, immutable Pattern contract ahead of every request-specific
    # value. Providers with prefix caching can then reuse the catalog and rules
    # while employee data, conversation history, and the current Manager message
    # remain outside the shared prefix.
    _PATTERN_DYNAMICS_INSTRUCTIONS = (
        "你只负责 Psychological Pattern Dynamics，不计算或修改三维 VAD 情绪。"
        "不得生成员工最终话语。"
        "当前 Manager/PARTNER 发言只读取后方 pattern_context 的"
        "latest_manager_message，其正文在整个输入中只出现这一次；"
        "recent_egocentric_conversation 已移除该条。"
        "system 记录只能作为上下文事件，绝不能解释成 Manager 原话。"
        "current_effective_dynamic_context 是动态设定的唯一权威来源；"
        "已清除的旧设定即使出现在历史中也不得恢复。"
        "RAG 未提供给此模块；任何背景参考、Manager 隐藏意图或未向员工明确提供的信息，"
        "都不得成为员工已知事实或 Pattern Trigger。"
        "\n[PATTERN_USAGE_SAFETY]"
        "目录同时包含人格倾向、情境状态、认知偏差、个体行为、关系或团队过程、"
        "工作设计、职业构念和理论框架；必须按每个条目的定义理解其层级，不能相互代换。"
        "稳定人格只以 stable_personality 和 applicable_personality_facets 为依据；"
        "不得从单轮话语反推、新增或修改人格、能力、动机、道德品质或心理健康状态。"
        "Pattern 只是内部概率性解释假设，不是员工事实、确定因果、心理诊断、"
        "绩效结论或人才决策；core_mechanisms 只表示可能机制，证据不足时选择0个。"
        "情境状态或偏差必须有对应 Trigger 和上下文证据；团队构念必须有团队层证据；"
        "工作设计问题不得归咎个人；关系、健康和职业构念必须有相应历史、自述或资料依据。"
        "负向英文 pattern_id 只是内部目录标识，不得作为称呼、输出标签或道德结论。"
        "同一证据对应父级、子维度、近义或反向条目时不得重复激活或叠加强度；"
        "优先选择最具体且证据最充分者，只有独立心理力量才可同时保留。"
        "\n按以下顺序内部判断：事件实际心理意义；从 psychological_patterns 的"
        "pattern_id、construct_name、description、core_mechanisms、"
        "real_world_manifestation 综合选择真正相关的 0至5个 Pattern；"
        "强度变化；最多4条 reinforce/conflict/modulate 关系；"
        "统一涌现心理状态；一个主导心理活动；一个主要回应倾向；紧凑回应指导。"
        "不得按名称或关键词表面匹配，不得创造目录外 ID，不得为了数量凑选。"
        "\n强度只能是 weak、medium、medium_high、strong。首次激活可直接是任意档。"
        "既有 Pattern 默认单轮最多变化一档；只有明确、可信、与核心机制高度相关且"
        "重大影响的新信息，才同时设置 major_new_information=true 与该 Pattern 的"
        "major_change_justified=true，并最多跨两档。"
        "没有新 Trigger 不等于减弱；相关争议、关系张力或心理后果仍存在时保持原 strength。"
        "仅在实质回应、可信反向证据、澄清、议题明显离开、关系实质变化或自然淡化等"
        "有实际心理依据时才可降低 strength。不得为了推进对话自动降低防御。"
        "曾存在但跨轮未激活的 Pattern 再次被有效触发时重新列入 patterns；"
        "reactivated 由后端根据轮次自动计算。"
        "\ninteraction 只连接本轮 active Pattern，最多4条，不生成全量两两组合。"
        "Pattern 不修改 Big Five、facets、core motivation、客观事实；"
        "不创建或删除 dispute，不直接修改 acceptance。"
        "事实与 Manager 原话是不可违反边界；其后依次受 dispute/acceptance、"
        "稳定人格/动机/自我认知、Pattern Dynamics、emotion_t_minus_1/表达约束。"
        "本轮 VAD 情绪变化由另一并发任务独立计算，不得猜测、等待或替代该结果。"
        "情绪缓和不等于认知接受，愿意行动不等于接受评价，停止争论不等于内部认同。"
        "\nlatest_manager_message 的具体问题和当前沟通目的先决定员工说什么。"
        "Pattern 不得更换话题、增加回复事项、重启旧立场或设计完整话术结构；"
        "只能对已经被当前问题选中的内容调节回应倾向、语气、直接程度、"
        "信息密度和承诺边界。main_psychological_activity 只供内部汇总，"
        "不得作为员工回复的内容议程。checks 只能是防止越界的禁止性检查，"
        "不得是待办清单或话术步骤。"
        "不得要求或暗示同时加入感谢/认可、重述旧立场、条件或风险、"
        "完整行动计划与后续追问；这些言语动作只有在当前问题或其他更高优先级"
        "规则独立要求时才能出现。当 Pattern 对本轮没有必要的可见影响时，"
        "response_guidance 的各字段应留空，不得为了输出而凑指导。"
        "\nresponse_guidance 只包含一个不超过200字的 main_psychological_activity、一个"
        "不超过80字的 response_tendency、一条不超过300字的 expression_guidance 与"
        "0至2条不超过120字的当前相关 checks。"
        "其中禁止出现 Pattern 名称、ID、强度、Trigger、interaction、"
        "behavioral_tendency 或内部推演；它只指导员工生成器自然表达。"
        "事件意义、Trigger、行为倾向只在内部判断，不得输出 event_interpretation、"
        "trigger、behavioral_tendency、change 或 trigger_turn 字段。"
        "只返回 PsychologicalPatternDynamicsStructuredOutput 对象。"
    )
    _PATTERN_CACHE_PREFIX_END = "\n\n[END_PSYCHOLOGICAL_PATTERN_CACHE_PREFIX]\n\n"
    _INTERNAL_GUIDANCE_MARKER = re.compile(
        r"(?i)(?:"
        r"\bpattern(?:s|_id)?\b|"
        r"\btrigger(?:_type|_turn)?\b|"
        r"\binteraction(?:s|_type)?\b|"
        r"\bbehavioral[\s_-]*tendency\b|"
        r"\b(?:relative[\s_-]*)?strength\b|"
        r"\b(?:construct[\s_-]*name|core[\s_-]*mechanisms|"
        r"real[\s_-]*world[\s_-]*manifestation)\b|"
        r"\b(?:from|to)[\s_-]*pattern[\s_-]*id\b|"
        r"\bresponse[\s_-]*guidance\b|"
        r"\binternal[\s_-]*(?:checklist|reasoning|analysis)\b|"
        r"\bevent[\s_-]*interpretation\b|"
        r"\bmajor[\s_-]*(?:new[\s_-]*information|change[\s_-]*justified)\b|"
        r"模式(?:名称|ID|标识)|触发(?:类型|轮次)|相互作用|行为倾向|"
        r"相对强度|内部(?:检查清单|推演|分析)"
        r")"
    )

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        emotion_transition: EmotionTransitionService | None = None,
        catalog: PsychologicalPatternCatalogLoader | None = None,
        summary_service: ConversationSummaryService | None = None,
        model_scheduler: ModelScheduler | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.emotion_transition = emotion_transition or EmotionTransitionService()
        self.catalog = catalog or PsychologicalPatternCatalogLoader()
        self.summary_service = summary_service or get_conversation_summary_service()
        self.model_scheduler = model_scheduler or ModelScheduler(self.settings)

    async def update_after_manager_message(
        self,
        state: SessionState,
        manager_message: str,
    ) -> EmployeeStateTransitionResult:
        if not getattr(
            self.settings,
            "psychological_pattern_dynamics_enabled",
            False,
        ):
            emotion = await self._run_emotion_branch(
                state,
                manager_message,
            )
            return EmployeeStateTransitionResult(
                state=emotion.state,
                emotion_transition_ms=emotion.duration_ms,
                emotion_model_queue_ms=emotion.queue_ms,
            )

        try:
            catalog = await self.catalog.asnapshot()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Psychological pattern catalog access failed; using the "
                "existing emotion transition flow",
                exc_info=True,
            )
            emotion = await self._run_emotion_branch(
                state,
                manager_message,
            )
            return EmployeeStateTransitionResult(
                state=emotion.state,
                emotion_transition_ms=emotion.duration_ms,
                emotion_model_queue_ms=emotion.queue_ms,
            )
        if not catalog.available or not catalog.valid_pattern_ids:
            emotion = await self._run_emotion_branch(
                state,
                manager_message,
            )
            return EmployeeStateTransitionResult(
                state=emotion.state,
                emotion_transition_ms=emotion.duration_ms,
                emotion_model_queue_ms=emotion.queue_ms,
            )

        previous_pattern_state = (
            state.psychological_pattern_state.model_copy(deep=True)
            if state.psychological_pattern_state
            else None
        )
        try:
            self._prune_removed_catalog_patterns(
                state,
                catalog.valid_pattern_ids,
            )
        except Exception as exc:  # noqa: BLE001
            emotion = await self._run_emotion_branch(state, manager_message)
            pattern = self._pattern_failure_branch(
                state=state.model_copy(deep=True),
                previous_pattern_state=previous_pattern_state,
                error=exc,
                catalog=catalog,
                prompt="",
                started=time.perf_counter(),
            )
            self._merge_branch_state(state, emotion.state, pattern.state)
            return EmployeeStateTransitionResult(
                state=state,
                emotion_transition_ms=emotion.duration_ms,
                pattern_dynamics_ms=pattern.duration_ms,
                emotion_model_queue_ms=emotion.queue_ms,
                pattern_model_queue_ms=pattern.queue_ms,
            )
        previous_pattern_state = (
            state.psychological_pattern_state.model_copy(deep=True)
            if state.psychological_pattern_state
            else None
        )

        emotion_task = asyncio.create_task(
            self._run_emotion_branch(
                state.model_copy(deep=True),
                manager_message,
            )
        )
        pattern_task = asyncio.create_task(
            self._run_pattern_branch(
                state.model_copy(deep=True),
                manager_message,
                catalog,
                previous_pattern_state=previous_pattern_state,
            )
        )
        try:
            emotion, pattern = await asyncio.gather(emotion_task, pattern_task)
        except BaseException:
            for task in (emotion_task, pattern_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                emotion_task,
                pattern_task,
                return_exceptions=True,
            )
            raise

        self._merge_branch_state(state, emotion.state, pattern.state)
        return EmployeeStateTransitionResult(
            state=state,
            pattern_response_guidance=pattern.guidance,
            pattern_dynamics_applied=pattern.applied,
            emotion_transition_ms=emotion.duration_ms,
            pattern_dynamics_ms=pattern.duration_ms,
            emotion_model_queue_ms=emotion.queue_ms,
            pattern_model_queue_ms=pattern.queue_ms,
        )

    async def _run_emotion_branch(
        self,
        state: SessionState,
        manager_message: str,
    ) -> _EmotionBranchResult:
        task_name = "emotion_transition"
        started = time.perf_counter()
        queue_ms = 0.0
        try:
            async with self.model_scheduler.slot(
                session_id=state.session_id,
                category="interactive",
                endpoint=getattr(self.settings, "chat_url", ""),
                model=self.settings.model_for_task(task_name),
            ) as queue_slot:
                queue_ms = float(getattr(queue_slot, "queue_ms", 0.0) or 0.0)
                state = await self.emotion_transition.update_after_manager_message(
                    state,
                    manager_message,
                )
        except Exception as exc:  # noqa: BLE001
            error_code = getattr(exc, "code", type(exc).__name__)
            logger.warning(
                "Emotion transition branch failed before its normal fallback; "
                "using the deterministic fallback: error_type=%s",
                error_code,
            )
            try:
                fallback = self.emotion_transition._fallback_transition(  # noqa: SLF001
                    state,
                    manager_message,
                )
                state.emotion_state = self.emotion_transition._apply_transition(  # noqa: SLF001
                    state,
                    fallback,
                    manager_message,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Rule-based emotion fallback failed; preserving prior emotion"
                )
            warning = f"情绪转移使用规则兜底：{error_code}"
            if warning not in state.warnings:
                state.warnings.append(warning)
        return _EmotionBranchResult(
            state=state,
            duration_ms=self._elapsed_ms(started),
            queue_ms=queue_ms,
        )

    async def _run_pattern_branch(
        self,
        state: SessionState,
        manager_message: str,
        catalog: PsychologicalPatternCatalogSnapshot,
        *,
        previous_pattern_state: PsychologicalPatternState | None,
    ) -> _PatternBranchResult:
        task_name = "psychological_pattern_dynamics"
        started = time.perf_counter()
        prompt = ""
        queue_ms = 0.0
        try:
            manager_turn = self._manager_turn_index(state)
            pattern_context = await self._build_pattern_context(
                state,
                manager_message,
            )
            prompt = self._build_pattern_prompt(pattern_context, catalog)
            async with self.model_scheduler.slot(
                session_id=state.session_id,
                category="interactive",
                endpoint=getattr(self.settings, "chat_url", ""),
                model=self.settings.model_for_task(task_name),
            ) as queue_slot:
                queue_ms = float(getattr(queue_slot, "queue_ms", 0.0) or 0.0)
                output = await LangChainLLMService().ainvoke_structured_single(
                    prompt=prompt,
                    schema=PsychologicalPatternDynamicsStructuredOutput,
                    task_name=task_name,
                    model=self.settings.model_for_task(task_name),
                    temperature=self.settings.temperature_for_task(task_name),
                    max_tokens=self.settings.max_tokens_for_task(task_name),
                    timeout_seconds=self.settings.timeout_for_task(task_name),
                    enable_thinking=self.settings.enable_thinking_for_task(task_name),
                    payload_normalizer=lambda payload: (
                        self._normalize_pattern_payload(
                            payload,
                            manager_turn=manager_turn,
                        )
                    ),
                    structured_transport="json_schema",
                    json_schema_strict=False,
                )
            guidance, active_count, interaction_count = self._apply_pattern_dynamics(
                state,
                output,
                catalog,
                manager_turn=manager_turn,
            )
        except Exception as exc:  # noqa: BLE001
            return self._pattern_failure_branch(
                state=state,
                previous_pattern_state=previous_pattern_state,
                error=exc,
                catalog=catalog,
                prompt=prompt,
                started=started,
                queue_ms=queue_ms,
            )

        try:
            self._record_metrics(
                state=state,
                catalog=catalog,
                prompt=prompt,
                started=started,
                outcome="success",
                active_count=active_count,
                interaction_count=interaction_count,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Pattern Dynamics metrics failed; preserving the valid result",
                exc_info=True,
            )
        return _PatternBranchResult(
            state=state,
            guidance=guidance,
            applied=True,
            duration_ms=self._elapsed_ms(started),
            queue_ms=queue_ms,
        )

    def _pattern_failure_branch(
        self,
        *,
        state: SessionState,
        previous_pattern_state: PsychologicalPatternState | None,
        error: Exception,
        catalog: PsychologicalPatternCatalogSnapshot,
        prompt: str,
        started: float,
        queue_ms: float = 0.0,
    ) -> _PatternBranchResult:
        """Preserve prior Pattern state without affecting the emotion branch."""

        error_code = getattr(error, "code", type(error).__name__)
        logger.warning(
            "Pattern Dynamics transition failed; preserving Pattern state: "
            "error_type=%s",
            error_code,
        )
        state.psychological_pattern_state = (
            previous_pattern_state.model_copy(deep=True)
            if previous_pattern_state is not None
            else None
        )
        warning = f"心理状态转移保留上一状态：{error_code}"
        if warning not in state.warnings:
            state.warnings.append(warning)
        try:
            self._record_metrics(
                state=state,
                catalog=catalog,
                prompt=prompt,
                started=started,
                outcome="fallback",
                active_count=0,
                interaction_count=0,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Pattern Dynamics fallback metrics failed; reply processing continues",
                exc_info=True,
            )
        return _PatternBranchResult(
            state=state,
            guidance="",
            applied=False,
            duration_ms=self._elapsed_ms(started),
            queue_ms=queue_ms,
        )

    @staticmethod
    def _merge_branch_state(
        target: SessionState,
        emotion_state: SessionState,
        pattern_state: SessionState,
    ) -> None:
        target.emotion_state = (
            emotion_state.emotion_state.model_copy(deep=True)
            if emotion_state.emotion_state is not None
            else None
        )
        target.psychological_pattern_state = (
            pattern_state.psychological_pattern_state.model_copy(deep=True)
            if pattern_state.psychological_pattern_state is not None
            else None
        )
        merged_warnings = list(target.warnings)
        for warning in (*emotion_state.warnings, *pattern_state.warnings):
            if warning not in merged_warnings:
                merged_warnings.append(warning)
        target.warnings = merged_warnings

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int(round((time.perf_counter() - started) * 1000)))

    async def _build_pattern_context(
        self,
        state: SessionState,
        manager_message: str,
    ) -> dict[str, Any]:
        try:
            conversation_context = await self.summary_service.context_for_reply(
                state
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Conversation summary context was unavailable for state transition; "
                "using the recent visible turns only",
                exc_info=True,
            )
            conversation_context = ConversationPromptContext(
                output_locale=state.locale,
                raw_turns=list(state.conversation)
            )

        history_mode = self.settings.employee_dialogue_history_mode
        visible_history_turns = self._pattern_history_turns(
            conversation_context.raw_turns,
            latest_manager_message=manager_message,
            limit=self.settings.psychological_pattern_history_turns,
        )
        employee_visible_profile = EmployeeAgent._employee_visible_profile_payload(  # noqa: SLF001
            state
        )
        profile_supplemental_info = employee_visible_profile.pop(
            "supplemental_info",
            "",
        )
        employee_visible_profile.pop("source_profile_text", None)
        supplemental_info = self._pattern_supplemental_info(
            state.supplemental_info,
            profile_supplemental_info,
            limit=self.settings.psychological_pattern_supplemental_max_chars,
        )
        personality_facets = (
            state.personality_facets.model_context()
            if state.personality_facets
            else {}
        )
        current_runtime_context = EmployeeAgent._employee_visible_value(  # noqa: SLF001
            {"runtime_notes": list(state.rehearsal_context.runtime_notes)}
        )
        return {
            "output_locale": state.locale,
            "previous_psychological_pattern_state": (
                state.psychological_pattern_state.model_dump(mode="json")
                if state.psychological_pattern_state
                else {}
            ),
            "stable_personality": (
                state.personality.model_dump(mode="json")
                if state.personality
                else {}
            ),
            "applicable_personality_facets": personality_facets,
            "motivation_t_minus_1": (
                state.motivation.model_dump(
                    mode="json",
                    exclude_none=True,
                    exclude={"updated_at"},
                )
                if state.motivation
                else {}
            ),
            "emotion_t_minus_1": (
                state.emotion_state.model_dump(
                    mode="json",
                    exclude_none=True,
                    # This instruction was generated for the preceding reply and
                    # must not influence the next Pattern decision.
                    exclude={"reply_emotion_guidance", "updated_at"},
                )
                if state.emotion_state
                else {}
            ),
            "employee_visible_profile": employee_visible_profile,
            "employee_visible_supplemental_info": supplemental_info,
            "employee_visible_performance_context": EmployeeAgent._employee_visible_text(  # noqa: SLF001
                state.intent.performance_context if state.intent else ""
            ),
            "current_effective_dynamic_context": current_runtime_context,
            "conversation_summary": conversation_context.summary_text,
            "dialogue_role_mapping": EmployeeAgent._employee_dialogue_role_mapping(  # noqa: SLF001
                history_mode
            ),
            "recent_egocentric_conversation": self._pattern_prompt_conversation(
                visible_history_turns,
                history_mode=history_mode,
            ),
            "latest_manager_message": manager_message,
            "cognitive_state_instruction": (
                "dispute 与 acceptance 只从员工已知事实、有效历史、摘要和当前"
                "PARTNER 发言中识别；它们保持独立，Pattern 不得创建、删除或改写。"
            ),
        }

    @staticmethod
    def _pattern_history_turns(
        turns: list[Any],
        *,
        latest_manager_message: str = "",
        limit: int | None = None,
    ) -> list[Any]:
        filtered = [
            turn
            for turn in turns
            if not (
                getattr(turn, "speaker", None) == "system"
                and isinstance(getattr(turn, "metadata", None), dict)
                and turn.metadata.get("type")
                == "rehearsal_context_update"
            )
        ]
        if filtered and latest_manager_message:
            latest_turn = filtered[-1]
            if (
                getattr(latest_turn, "speaker", None) == "manager"
                and str(getattr(latest_turn, "text", "")).strip()
                == latest_manager_message.strip()
            ):
                filtered.pop()
        if limit is not None:
            return filtered[-limit:]
        return filtered

    @classmethod
    def _pattern_prompt_conversation(
        cls,
        turns: list[Any],
        *,
        history_mode: str,
    ) -> list[dict[str, Any]]:
        """Project Pattern history without serializing operational turn metadata."""

        if history_mode not in {"ecp_labels", "legacy_labels"}:
            raise ValueError(
                "employee dialogue history mode must be ecp_labels or legacy_labels"
            )

        projected: list[dict[str, Any]] = []
        for turn in turns:
            speaker = str(getattr(turn, "speaker", ""))
            text = str(getattr(turn, "text", ""))
            if speaker == "system":
                text = EmployeeAgent._employee_visible_text(text)  # noqa: SLF001
            prompt_turn: dict[str, Any] = {
                "turn_index": getattr(turn, "turn_index", None),
                "speaker": (
                    {"manager": "PARTNER", "employee": "SELF"}.get(
                        speaker,
                        speaker,
                    )
                    if history_mode == "ecp_labels"
                    else speaker
                ),
                "text": text,
            }

            metadata = getattr(turn, "metadata", None)
            raw_snapshot = (
                metadata.get("emotion_snapshot")
                if isinstance(metadata, dict)
                else None
            )
            if isinstance(raw_snapshot, dict):
                snapshot = {
                    field: raw_snapshot[field]
                    for field in cls._PATTERN_EMOTION_SNAPSHOT_FIELDS
                    if field in raw_snapshot
                }
                sanitized_snapshot = EmployeeAgent._employee_visible_value(  # noqa: SLF001
                    snapshot
                )
                if isinstance(sanitized_snapshot, dict) and sanitized_snapshot:
                    prompt_turn["emotion_snapshot"] = sanitized_snapshot
            projected.append(prompt_turn)
        return projected

    @classmethod
    def _pattern_supplemental_info(
        cls,
        session_value: Any,
        profile_value: Any,
        *,
        limit: int,
    ) -> str:
        retained: list[str] = []
        for value in (session_value, profile_value):
            text = EmployeeAgent._employee_visible_text(  # noqa: SLF001
                str(value or "")
            ).strip()
            if not text or any(text in existing for existing in retained):
                continue
            retained = [existing for existing in retained if existing not in text]
            retained.append(text)
        return cls._compact_text("\n\n".join(retained), limit)

    def _build_pattern_prompt(
        self,
        pattern_context: dict[str, Any],
        catalog: PsychologicalPatternCatalogSnapshot,
    ) -> str:
        return (
            self._pattern_cache_prefix(catalog)
            + "\npattern_context="
            + json.dumps(
                pattern_context,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )

    @classmethod
    def _pattern_cache_prefix(
        cls,
        catalog: PsychologicalPatternCatalogSnapshot,
    ) -> str:
        """Return the exact static prefix shared by requests for one catalog."""

        return (
            cls._PATTERN_DYNAMICS_INSTRUCTIONS
            + "\npsychological_pattern_catalog_version="
            + str(catalog.catalog_version or "")
            + "\npsychological_patterns="
            + catalog.render_patterns
            + cls._PATTERN_CACHE_PREFIX_END
        )

    def _normalize_pattern_payload(
        self,
        payload: dict[str, Any],
        *,
        manager_turn: int,
    ) -> tuple[dict[str, Any], list[str]]:
        raw_dynamics = (
            payload.get("pattern_dynamics")
            or payload.get("patternDynamics")
            or payload.get("psychological_pattern_dynamics")
            or payload
        )
        if not isinstance(raw_dynamics, dict):
            raise StructuredOutputError(
                "missing_core_field",
                "Employee state transition output is missing pattern_dynamics.",
            )
        dynamics = dict(raw_dynamics)
        raw_patterns = (
            dynamics.get("patterns")
            or dynamics.get("active_patterns")
            or dynamics.get("activations")
            or []
        )
        if not isinstance(raw_patterns, list):
            raise StructuredOutputError(
                "schema_validation",
                "pattern_dynamics.patterns must be an array.",
            )
        normalized_patterns: list[dict[str, Any]] = []
        for raw_pattern in raw_patterns[:5]:
            if not isinstance(raw_pattern, dict):
                continue
            item = dict(raw_pattern)
            pattern_id = item.get("pattern_id") or item.get("patternId") or item.get("id")
            strength = self._normalize_strength(
                item.get("strength") or item.get("relative_strength")
            )
            trigger_type = self._normalize_trigger_type(
                item.get("trigger_type") or item.get("triggerType")
            )
            normalized_patterns.append(
                {
                    "pattern_id": str(pattern_id or "").strip(),
                    "strength": strength,
                    "trigger_type": trigger_type,
                    "major_change_justified": self._as_bool(
                        item.get("major_change_justified")
                        or item.get("majorChangeJustified")
                    ),
                }
            )

        raw_interactions = dynamics.get("interactions") or []
        if not isinstance(raw_interactions, list):
            raw_interactions = []
        normalized_interactions: list[dict[str, Any]] = []
        for raw_interaction in raw_interactions[:4]:
            if not isinstance(raw_interaction, dict):
                continue
            relation_type = self._normalize_interaction_type(
                raw_interaction.get("type")
                or raw_interaction.get("interaction_type")
            )
            if relation_type is None:
                continue
            normalized_interactions.append(
                {
                    "from_pattern_id": str(
                        raw_interaction.get("from_pattern_id")
                        or raw_interaction.get("fromPatternId")
                        or raw_interaction.get("from")
                        or ""
                    ).strip(),
                    "to_pattern_id": str(
                        raw_interaction.get("to_pattern_id")
                        or raw_interaction.get("toPatternId")
                        or raw_interaction.get("to")
                        or ""
                    ).strip(),
                    "type": relation_type,
                }
            )

        guidance = (
            dynamics.get("response_guidance")
            or dynamics.get("responseGuidance")
            or {}
        )
        if not isinstance(guidance, dict):
            guidance = {}
        checks = guidance.get("checks") or []
        if isinstance(checks, str):
            checks = [checks]
        if not isinstance(checks, list):
            checks = []
        normalized = {
            "major_new_information": self._as_bool(
                dynamics.get("major_new_information")
                or dynamics.get("majorNewInformation")
            ),
            "patterns": normalized_patterns,
            "interactions": normalized_interactions,
            "response_guidance": {
                "main_psychological_activity": self._compact_text(
                    guidance.get("main_psychological_activity")
                    or guidance.get("mainPsychologicalActivity"),
                    200,
                ),
                "response_tendency": self._compact_text(
                    guidance.get("response_tendency")
                    or guidance.get("responseTendency"),
                    80,
                ),
                "expression_guidance": self._compact_text(
                    guidance.get("expression_guidance")
                    or guidance.get("expressionGuidance"),
                    300,
                ),
                "checks": [
                    self._compact_text(item, 120)
                    for item in checks[:2]
                    if self._compact_text(item, 120)
                ],
            },
        }
        return normalized, ["normalized_pattern_dynamics"]

    def _normalize_joint_payload(
        self,
        payload: dict[str, Any],
        *,
        manager_turn: int,
    ) -> tuple[dict[str, Any], list[str]]:
        """Normalize historical joint payloads used by stored tests and fixtures."""

        normalized, repair_steps = (
            self.emotion_transition._normalize_structured_payload(payload)  # noqa: SLF001
        )
        pattern, pattern_repairs = self._normalize_pattern_payload(
            payload,
            manager_turn=manager_turn,
        )
        normalized["pattern_dynamics"] = pattern
        return normalized, [*repair_steps, *pattern_repairs]

    def _apply_pattern_dynamics(
        self,
        state: SessionState,
        dynamics: PsychologicalPatternDynamicsStructuredOutput,
        catalog: PsychologicalPatternCatalogSnapshot,
        *,
        manager_turn: int,
    ) -> tuple[str, int, int]:
        previous = state.psychological_pattern_state or PsychologicalPatternState()
        previous_by_id = {
            item.pattern_id: item
            for item in previous.patterns
            if item.pattern_id in catalog.valid_pattern_ids
        }

        active: list[PsychologicalPatternActivation] = []
        active_ids: set[str] = set()
        for decision in dynamics.patterns:
            if (
                decision.pattern_id not in catalog.valid_pattern_ids
                or decision.pattern_id in active_ids
            ):
                continue
            old = previous_by_id.get(decision.pattern_id)
            if old is None:
                strength = decision.strength
                change = "first_activated"
            else:
                strength = self._bounded_existing_strength(
                    old.strength,
                    decision.strength,
                    allow_two_levels=(
                        dynamics.major_new_information
                        and decision.major_change_justified
                    ),
                )
                old_index = self._STRENGTHS.index(old.strength)
                new_index = self._STRENGTHS.index(strength)
                was_inactive = bool(
                    previous.last_processed_manager_turn
                    and old.trigger_turn < previous.last_processed_manager_turn
                )
                if was_inactive:
                    change = "reactivated"
                elif new_index > old_index:
                    change = "strengthened"
                elif new_index < old_index:
                    change = "weakened"
                else:
                    change = "maintained"

            active.append(
                PsychologicalPatternActivation(
                    pattern_id=decision.pattern_id,
                    strength=strength,
                    change=change,
                    trigger_type=decision.trigger_type,
                    trigger_turn=manager_turn,
                )
            )
            active_ids.add(decision.pattern_id)

        retained: list[PsychologicalPatternActivation] = []
        for item in previous.patterns:
            if (
                item.pattern_id not in catalog.valid_pattern_ids
                or item.pattern_id in active_ids
            ):
                continue
            retained.append(
                PsychologicalPatternActivation(
                    pattern_id=item.pattern_id,
                    strength=item.strength,
                    change="maintained",
                    trigger_type=item.trigger_type,
                    trigger_turn=item.trigger_turn,
                )
            )

        interactions: list[PsychologicalPatternInteraction] = []
        seen_interactions: set[tuple[str, str, str]] = set()
        for interaction in dynamics.interactions:
            if (
                interaction.from_pattern_id not in active_ids
                or interaction.to_pattern_id not in active_ids
                or interaction.from_pattern_id == interaction.to_pattern_id
            ):
                continue
            key = (
                interaction.from_pattern_id,
                interaction.to_pattern_id,
                interaction.type,
            )
            if key in seen_interactions:
                continue
            seen_interactions.add(key)
            interactions.append(interaction)
            if len(interactions) >= 4:
                break

        state.psychological_pattern_state = PsychologicalPatternState(
            last_processed_manager_turn=manager_turn,
            patterns=[*active, *retained][:5],
            interactions=interactions,
        )
        guidance = ""
        if active:
            guidance_model = self._sanitized_guidance(
                dynamics.response_guidance,
                catalog,
                active_ids,
            )
            if guidance_model is not None:
                # The main activity remains an internal Pattern summary. Reply
                # receives only externally actionable modulation, never an
                # agenda that it could translate into additional content.
                reply_guidance = guidance_model.model_dump(
                    mode="json",
                    exclude={"main_psychological_activity"},
                    exclude_defaults=True,
                )
                if any(
                    (
                        bool(value)
                        if not isinstance(value, str)
                        else bool(value.strip())
                    )
                    for value in reply_guidance.values()
                ):
                    guidance = json.dumps(
                        reply_guidance,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
        return guidance, len(active), len(interactions)

    @classmethod
    def _bounded_existing_strength(
        cls,
        previous: str,
        requested: str,
        *,
        allow_two_levels: bool,
    ) -> str:
        old_index = cls._STRENGTHS.index(previous)
        requested_index = cls._STRENGTHS.index(requested)
        max_step = 2 if allow_two_levels else 1
        bounded_index = max(
            old_index - max_step,
            min(old_index + max_step, requested_index),
        )
        return cls._STRENGTHS[bounded_index]

    @staticmethod
    def _prune_removed_catalog_patterns(
        state: SessionState,
        valid_ids: frozenset[str],
    ) -> None:
        previous = state.psychological_pattern_state
        if previous is None:
            return
        patterns = [
            pattern
            for pattern in previous.patterns
            if pattern.pattern_id in valid_ids
        ]
        retained_ids = {pattern.pattern_id for pattern in patterns}
        interactions = [
            interaction
            for interaction in previous.interactions
            if interaction.from_pattern_id in retained_ids
            and interaction.to_pattern_id in retained_ids
        ]
        if (
            len(patterns) != len(previous.patterns)
            or len(interactions) != len(previous.interactions)
        ):
            state.psychological_pattern_state = PsychologicalPatternState(
                last_processed_manager_turn=previous.last_processed_manager_turn,
                patterns=patterns,
                interactions=interactions,
            )

    def _sanitized_guidance(
        self,
        guidance: PatternResponseGuidance,
        catalog: PsychologicalPatternCatalogSnapshot,
        active_ids: set[str],
    ) -> PatternResponseGuidance | None:
        sensitive_terms = set(active_ids)
        for pattern in catalog.render_payload():
            pattern_id = pattern.get("pattern_id")
            if isinstance(pattern_id, str) and pattern_id.strip():
                sensitive_terms.add(pattern_id.strip())
            name = pattern.get("construct_name")
            if isinstance(name, str) and name.strip():
                sensitive_terms.add(name.strip())

        def sanitize(value: str, limit: int) -> str:
            cleaned = str(value or "").strip()
            for term in sorted(sensitive_terms, key=len, reverse=True):
                cleaned = re.sub(
                    re.escape(term),
                    "当前心理机制",
                    cleaned,
                    flags=re.IGNORECASE,
                )
            cleaned = re.sub(
                r"(?i)\b(?:weak|medium_high|medium|strong)\b",
                "",
                cleaned,
            )
            return self._compact_text(
                re.sub(r"\s{2,}", " ", cleaned).strip(),
                limit,
            )

        sanitized_checks: list[str] = []
        for item in guidance.checks[:2]:
            cleaned = sanitize(item, 120)
            if cleaned:
                sanitized_checks.append(cleaned)

        sanitized = PatternResponseGuidance(
            main_psychological_activity=sanitize(
                guidance.main_psychological_activity,
                200,
            ),
            response_tendency=sanitize(guidance.response_tendency, 80),
            expression_guidance=sanitize(guidance.expression_guidance, 300),
            checks=sanitized_checks,
        )
        values = [
            sanitized.main_psychological_activity,
            sanitized.response_tendency,
            sanitized.expression_guidance,
            *sanitized.checks,
        ]
        combined = "\n".join(values)
        lowered = combined.casefold()
        leaked_identity = any(
            term.casefold() in lowered
            for term in sensitive_terms
            if term.strip()
        )
        if leaked_identity or self._INTERNAL_GUIDANCE_MARKER.search(combined):
            logger.warning(
                "Pattern response guidance failed internal-data screening; "
                "dropping the entire guidance payload"
            )
            return None
        return sanitized

    @staticmethod
    def _manager_turn_index(state: SessionState) -> int:
        for turn in reversed(state.conversation):
            if turn.speaker == "manager":
                return max(1, int(turn.turn_index))
        return max(1, int(state.user_turn_count))

    @classmethod
    def _normalize_strength(cls, value: Any) -> str:
        token = str(value or "").strip().casefold().replace("-", "_")
        mappings = {
            "weak": {"weak", "low", "弱"},
            "medium": {"medium", "moderate", "中"},
            "medium_high": {
                "medium_high",
                "mediumhigh",
                "moderately_high",
                "中高",
            },
            "strong": {"strong", "high", "强"},
        }
        for canonical, aliases in mappings.items():
            if token in aliases:
                return canonical
        return "medium"

    @staticmethod
    def _normalize_interaction_type(value: Any) -> str | None:
        token = str(value or "").strip().casefold()
        mappings = {
            "reinforce": {"reinforce", "reinforcement", "强化"},
            "conflict": {"conflict", "冲突"},
            "modulate": {"modulate", "modulation", "调节"},
        }
        for canonical, aliases in mappings.items():
            if token in aliases:
                return canonical
        return None

    @staticmethod
    def _normalize_trigger_type(value: Any) -> str:
        token = re.sub(
            r"[^a-z0-9]+",
            "_",
            str(value or "").strip().casefold(),
        ).strip("_")
        if not token or not token[0].isalpha():
            return "context_event"
        return token[:96]

    @staticmethod
    def _compact_text(value: Any, limit: int) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            text = "；".join(
                str(item).strip()
                for item in value
                if str(item).strip()
            )
        else:
            text = str(value).strip()
        return text[:limit]

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    @staticmethod
    def _record_metrics(
        *,
        state: SessionState,
        catalog: PsychologicalPatternCatalogSnapshot,
        prompt: str,
        started: float,
        outcome: str,
        active_count: int,
        interaction_count: int,
    ) -> None:
        log_metric(
            "employee_state_transition.pattern_dynamics",
            session_id=state.session_id,
            outcome=outcome,
            pattern_catalog_chars=len(catalog.render_patterns),
            state_transition_input_chars=len(prompt),
            state_transition_latency_ms=round(
                (time.perf_counter() - started) * 1000
            ),
            active_pattern_count=active_count,
            interaction_count=interaction_count,
        )

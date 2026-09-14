from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import inspect
import json
import logging
import re

from backend.config.settings import get_settings
from backend.exceptions.llm_errors import LLMError
from backend.observability.metrics import log_metric
from backend.schemas.conversation import (
    EMOTION_TURN_SNAPSHOT_FIELDS,
    project_emotion_snapshot,
)
from backend.schemas.conversation_summary import ConversationPromptContext
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.state import SessionState
from backend.services.conversation_summary_service import (
    ConversationSummaryService,
    get_conversation_summary_service,
)
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.executor_utils import run_db_with_context
from backend.services.model_history_fallback_service import (
    HistoricalEmployeeReplyMatch,
    ModelHistoryFallbackService,
)
from backend.services.personality_behavior_service import (
    get_personality_behavior_resolver,
    personality_behavior_prompt_json,
)
from backend.services.prompt_service import PromptService
from backend.services.retrieval_service import RetrievalService
from backend.workflows.guards import ensure_current_performance_locale


logger = logging.getLogger(__name__)


class EmployeeAgent:
    """Employee role-play agent driven by profile, big five, motivation, and VAD emotion state."""

    _ECP_SPEAKER_LABELS = {
        "employee": "SELF",
        "manager": "PARTNER",
    }
    _ECP_DIALOGUE_ROLE_MAPPING = {
        "SELF": "当前扮演的员工",
        "PARTNER": "经理",
    }
    _REPLY_HISTORY_EMOTION_SNAPSHOT_FIELDS = EMOTION_TURN_SNAPSHOT_FIELDS
    _LEADING_REPLY_BUFFER_CHARS = 8
    _STREAM_HOLDBACK_CHARS = 1
    _EMPLOYEE_HIDDEN_PROFILE_KEYS = frozenset(
        {
            "performancerating",
            "performanceratingagroup",
            "tcl",
            "tclslx",
            "speechvoice",
            "speechseed",
            "当前绩效评级",
            "绩效评级",
        }
    )
    _EMPLOYEE_HIDDEN_PERFORMANCE_LINE = re.compile(
        r"^\s*[|{,]?\s*[\"']?(?:Performance[\s_]*Rating"
        r"(?:\s*\(\s*A\s*Group\s*\))?|当前绩效评级|绩效评级|"
        r"TCL(?:\s*\(\s*SLx\s*\))?)[\"']?"
        r"\s*(?::|：|=|为|是|\t|\|)\s*.*$",
        re.IGNORECASE,
    )
    _EMPLOYEE_HIDDEN_PERFORMANCE_INLINE = re.compile(
        r"[\"']?(?:Performance[\s_]*Rating"
        r"(?:\s*\(\s*A\s*Group\s*\))?|当前绩效评级|绩效评级|"
        r"TCL(?:\s*\(\s*SLx\s*\))?)[\"']?"
        r"\s*(?::|：|=|为|是|\|)\s*[^,，;；。\r\n|]+",
        re.IGNORECASE,
    )

    def __init__(
        self,
        retrieval: RetrievalService | None = None,
        summary_service: ConversationSummaryService | None = None,
        history_fallback: ModelHistoryFallbackService | None = None,
    ):
        self.retrieval = retrieval or RetrievalService()
        self.summary_service = summary_service or get_conversation_summary_service()
        self.history_fallback = history_fallback or ModelHistoryFallbackService()

    @staticmethod
    def _accepts_keyword_argument(
        callable_obj: object,
        keyword: str,
    ) -> bool:
        try:
            parameters = inspect.signature(callable_obj).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(
            parameter.name == keyword
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    @classmethod
    def _accepts_pattern_response_guidance(cls, callable_obj: object) -> bool:
        return cls._accepts_keyword_argument(
            callable_obj,
            "pattern_response_guidance",
        )

    async def reply(
        self,
        state: SessionState,
        latest_manager_message: str,
        pattern_response_guidance: str = "",
        *,
        retrieved_chunks: list[RetrievedChunk] | None = None,
    ) -> str:
        ensure_current_performance_locale(state)
        return await self._reply_with_llm(
            state,
            latest_manager_message,
            pattern_response_guidance=pattern_response_guidance,
            retrieved_chunks=retrieved_chunks,
        )

    async def stream_reply(
        self,
        state: SessionState,
        latest_manager_message: str,
        retrieved_chunks: list[RetrievedChunk] | None = None,
        *,
        pattern_response_guidance: str = "",
    ) -> AsyncIterator[str]:
        ensure_current_performance_locale(state)
        if retrieved_chunks is None:
            retrieved_chunks = await self.aretrieve_reply_context(
                state,
                latest_manager_message,
            )
        conversation_context = await self.summary_service.context_for_reply(state)
        if pattern_response_guidance:
            prompt = self._build_reply_prompt(
                state,
                latest_manager_message,
                retrieved_chunks,
                conversation_context=conversation_context,
                pattern_response_guidance=pattern_response_guidance,
            )
        else:
            prompt = self._build_reply_prompt(
                state,
                latest_manager_message,
                retrieved_chunks,
                conversation_context=conversation_context,
            )
        prefixes = self._reply_prefixes(state)
        emitted = False
        started = False
        pending = ""
        try:
            async for delta in LangChainLLMService().astream_text(prompt=prompt, task_name="employee_reply"):
                if not delta:
                    continue
                pending += delta
                if not started:
                    cleaned = self._strip_leading_reply_artifacts(pending, prefixes)
                    if not cleaned:
                        pending = ""
                        continue
                    if cleaned == pending.lstrip() and not self._has_stable_reply_start(cleaned):
                        pending = cleaned
                        continue
                    pending = cleaned
                    started = True
                if len(pending) <= self._STREAM_HOLDBACK_CHARS:
                    continue
                emitted = True
                emit_text, pending = pending[:-self._STREAM_HOLDBACK_CHARS], pending[-self._STREAM_HOLDBACK_CHARS:]
                async for piece in self._visible_stream_chunks(emit_text):
                    yield piece
        except LLMError:
            # Once a visible delta has reached the browser, replaying a complete
            # historical answer would concatenate two unrelated responses.
            if emitted:
                raise
            historical_match = await self._historical_employee_reply(
                state,
                latest_manager_message,
            )
            if historical_match is None:
                raise
            self._record_employee_reply_history_fallback(
                state,
                latest_manager_message,
                historical_match,
            )
            yield historical_match.reply
            return

        if not started:
            pending = self._strip_leading_reply_artifacts(pending, prefixes)
        pending = self._strip_trailing_reply_artifacts(pending)
        if pending:
            emitted = True
            async for piece in self._visible_stream_chunks(pending):
                yield piece
        if not emitted:
            historical_match = await self._historical_employee_reply(
                state,
                latest_manager_message,
            )
            if historical_match is not None:
                self._record_employee_reply_history_fallback(
                    state,
                    latest_manager_message,
                    historical_match,
                )
                yield historical_match.reply
                return
            raise LLMError("Employee Agent returned empty streamed reply.")

    @staticmethod
    async def _visible_stream_chunks(text: str) -> AsyncIterator[str]:
        if text:
            yield text

    @classmethod
    def _clean_reply_text(cls, text: str, prefixes: list[str] | None = None) -> str:
        cleaned = cls._strip_leading_reply_artifacts(text, prefixes)
        cleaned = cls._strip_trailing_reply_artifacts(cleaned)
        pairs = [("“", "”"), ('"', '"'), ("'", "'"), ("「", "」"), ("『", "』")]
        changed = True
        while changed and len(cleaned) >= 2:
            changed = False
            for left, right in pairs:
                if cleaned.startswith(left) and cleaned.endswith(right):
                    cleaned = cleaned[1:-1].strip()
                    changed = True
        return cleaned

    @staticmethod
    def _strip_leading_reply_artifacts(text: str, prefixes: list[str] | None = None) -> str:
        cleaned = text.lstrip()
        cleaned = re.sub(r"^(?:员工|employee|assistant|回复|答复)[：:]\s*", "", cleaned, flags=re.IGNORECASE)
        for prefix in prefixes or []:
            safe_prefix = re.escape(prefix.strip())
            if safe_prefix:
                cleaned = re.sub(rf"^{safe_prefix}[：:]\s*", "", cleaned)
        cleaned = re.sub(r"^(?:[\u4e00-\u9fff]{1,3}经理|经理)(?:[，,、：:\s]+|(?=[\u4e00-\u9fff])|$)", "", cleaned)
        return cleaned.lstrip(" \t\r\n\"'“”‘’「」『』")

    @classmethod
    def _has_stable_reply_start(cls, text: str) -> bool:
        stripped = text.lstrip()
        return len(stripped) >= cls._LEADING_REPLY_BUFFER_CHARS or any(char in stripped for char in "，,。！？!?；;：:\n")

    @staticmethod
    def _strip_trailing_reply_artifacts(text: str) -> str:
        return text.rstrip().rstrip("\"'“”‘’「」『』").rstrip()

    async def _reply_with_llm(
        self,
        state: SessionState,
        latest_manager_message: str,
        *,
        pattern_response_guidance: str = "",
        retrieved_chunks: list[RetrievedChunk] | None = None,
    ) -> str:
        if retrieved_chunks is None:
            retrieved_chunks = await self.aretrieve_reply_context(
                state,
                latest_manager_message,
            )
        conversation_context = await self.summary_service.context_for_reply(state)
        if pattern_response_guidance:
            prompt = self._build_reply_prompt(
                state,
                latest_manager_message,
                retrieved_chunks,
                conversation_context=conversation_context,
                pattern_response_guidance=pattern_response_guidance,
            )
        else:
            prompt = self._build_reply_prompt(
                state,
                latest_manager_message,
                retrieved_chunks,
                conversation_context=conversation_context,
            )
        try:
            reply = await LangChainLLMService().ainvoke_text(prompt=prompt, task_name="employee_reply")
            cleaned = self._clean_reply_text(reply, self._reply_prefixes(state))
            if not cleaned:
                raise LLMError("Employee Agent returned empty reply.")
            return cleaned
        except LLMError:
            historical_match = await self._historical_employee_reply(
                state,
                latest_manager_message,
            )
            if historical_match is None:
                raise
            self._record_employee_reply_history_fallback(
                state,
                latest_manager_message,
                historical_match,
            )
            return historical_match.reply

    async def _historical_employee_reply(
        self,
        state: SessionState,
        manager_message: str,
    ) -> HistoricalEmployeeReplyMatch | None:
        history_fallback = getattr(self, "history_fallback", None)
        if history_fallback is None or not history_fallback.enabled:
            return None
        try:
            return await run_db_with_context(
                None,
                "employee_reply.history_fallback.read",
                history_fallback.find_employee_reply,
                state,
                manager_message,
            )
        except Exception:  # history lookup must never replace the model error
            logger.exception(
                "Employee reply history fallback lookup failed: session_id=%s",
                state.session_id,
            )
            return None

    @staticmethod
    def _record_employee_reply_history_fallback(
        state: SessionState,
        manager_message: str,
        match: HistoricalEmployeeReplyMatch,
    ) -> None:
        ModelHistoryFallbackService.mark_employee_reply_fallback(
            state,
            manager_message,
            match,
        )
        log_metric(
            "employee_reply.history_fallback",
            session_id=state.session_id,
            source_session_id=match.source_session_id,
            history_fallback_score=round(match.score, 4),
            history_fallback_message_similarity=round(
                match.manager_message_similarity,
                4,
            ),
            complete=True,
        )

    def retrieve_general_context(
        self,
        state: SessionState,
        latest_manager_message: str,
        *,
        raise_errors: bool = False,
    ) -> list[RetrievedChunk]:
        return self._retrieve_general_context(state, latest_manager_message, raise_errors=raise_errors)

    def _retrieve_general_context(
        self,
        state: SessionState,
        latest_manager_message: str,
        *,
        raise_errors: bool = False,
    ) -> list[RetrievedChunk]:
        context = self._build_retrieval_context(state, latest_manager_message)
        try:
            return self.retrieval.retrieve("employee_response", context, top_k=8)
        except Exception as exc:  # noqa: BLE001
            if raise_errors:
                raise
            logger.warning("Employee general KB retrieval failed: %s", exc)
            return []

    async def aretrieve_reply_context(
        self,
        state: SessionState,
        latest_manager_message: str,
        *,
        raise_errors: bool = False,
    ) -> list[RetrievedChunk]:
        context = self._build_retrieval_context(state, latest_manager_message)
        results = await asyncio.gather(
            self.retrieval.aretrieve("employee_response", context, top_k=8),
            self.retrieval.aretrieve(
                "employee_rating_basis",
                context,
                top_k=4,
            ),
            return_exceptions=True,
        )

        successful_groups: list[list[RetrievedChunk]] = []
        failures: list[BaseException] = []
        for query_name, result in zip(
            ("employee_response", "employee_rating_basis"),
            results,
            strict=True,
        ):
            if isinstance(result, BaseException):
                failures.append(result)
                logger.warning(
                    "Employee reply KB retrieval failed for %s: %s",
                    query_name,
                    result,
                )
                continue
            successful_groups.append(list(result or []))

        chunks = self._merge_retrieved_chunks(*successful_groups)
        if failures and not chunks and raise_errors:
            raise failures[0]
        return chunks

    @classmethod
    def _build_retrieval_context(
        cls,
        state: SessionState,
        latest_manager_message: str,
    ) -> dict:
        ensure_current_performance_locale(state)
        return {
            "profile": cls._employee_visible_profile_payload(state),
            "supplemental_info": cls._employee_visible_text(
                state.supplemental_info_excerpt()
            ),
            "performance_context": cls._employee_visible_text(
                state.intent.performance_context
                if state.intent
                else ""
            ),
            "personality": state.personality.model_dump(exclude_none=True) if state.personality else {},
            "motivation": state.motivation.model_dump(mode="json", exclude_none=True) if state.motivation else {},
            "emotion_state": state.emotion_state.model_dump(mode="json", exclude_none=True) if state.emotion_state else {},
            "rehearsal_context": cls._employee_visible_value(
                state.rehearsal_context.model_dump(mode="json", exclude_none=True)
            ),
            "conversation": cls._reply_history_conversation(
                state.conversation[-8:]
            ),
            "latest_manager_message": latest_manager_message,
        }

    @classmethod
    def _employee_visible_profile_payload(cls, state: SessionState) -> dict:
        if state.employee_profile is None:
            return {}
        payload = state.employee_profile.model_dump(mode="json", exclude_none=True)
        sanitized = cls._employee_visible_value(payload)
        return sanitized if isinstance(sanitized, dict) else {}

    @classmethod
    def _employee_visible_value(cls, value):
        if isinstance(value, dict):
            return {
                key: cls._employee_visible_value(item)
                for key, item in value.items()
                if not cls._is_hidden_profile_key(key)
            }
        if isinstance(value, list):
            return [cls._employee_visible_value(item) for item in value]
        if isinstance(value, tuple):
            return [cls._employee_visible_value(item) for item in value]
        if isinstance(value, str):
            return cls._employee_visible_text(value)
        return value

    @classmethod
    def _is_hidden_profile_key(cls, key: object) -> bool:
        normalized = re.sub(r"[\W_]+", "", str(key).casefold())
        return normalized in cls._EMPLOYEE_HIDDEN_PROFILE_KEYS

    @classmethod
    def _employee_visible_text(cls, value: str | None) -> str:
        if not value:
            return ""
        retained_lines = [
            line
            for line in str(value).splitlines(keepends=True)
            if not cls._EMPLOYEE_HIDDEN_PERFORMANCE_LINE.match(
                line.rstrip("\r\n")
            )
        ]
        return cls._EMPLOYEE_HIDDEN_PERFORMANCE_INLINE.sub(
            "[当前绩效字段未向员工提供]",
            "".join(retained_lines),
        )

    @classmethod
    def _employee_visible_conversation(cls, turns: list) -> list[dict]:
        payloads: list[dict] = []
        for turn in turns:
            payload = turn.model_dump(mode="json", exclude_none=True)
            if str(payload.get("speaker") or "").casefold() == "system":
                sanitized = cls._employee_visible_value(payload)
                payload = sanitized if isinstance(sanitized, dict) else {}
            payloads.append(payload)
        return payloads

    @classmethod
    def _reply_history_conversation(cls, turns: list) -> list[dict]:
        """Project only semantic turn fields into the Employee Reply prompt."""

        projected: list[dict] = []
        for turn in turns:
            speaker = str(getattr(turn, "speaker", ""))
            text = str(getattr(turn, "text", ""))
            if speaker == "system":
                text = cls._employee_visible_text(text)
            prompt_turn = {
                "turn_index": getattr(turn, "turn_index", None),
                "speaker": speaker,
                "text": text,
            }

            metadata = getattr(turn, "metadata", None)
            raw_snapshot = (
                metadata.get("emotion_snapshot")
                if isinstance(metadata, dict)
                else None
            )
            if isinstance(raw_snapshot, dict):
                snapshot = project_emotion_snapshot(raw_snapshot)
                sanitized = cls._employee_visible_value(snapshot)
                if isinstance(sanitized, dict) and sanitized:
                    prompt_turn["emotion_snapshot"] = sanitized
            projected.append(prompt_turn)
        return projected

    @classmethod
    def _employee_egocentric_conversation(
        cls,
        turns: list,
        latest_partner_message: str,
        *,
        history_mode: str,
    ) -> list[dict]:
        prompt_turns = list(turns)
        if prompt_turns:
            last_turn = prompt_turns[-1]
            if (
                last_turn.speaker == "manager"
                and last_turn.text == latest_partner_message
            ):
                prompt_turns.pop()

        payloads = cls._reply_history_conversation(prompt_turns)
        if history_mode == "legacy_labels":
            return payloads
        if history_mode != "ecp_labels":
            raise ValueError(
                "employee dialogue history mode must be ecp_labels or legacy_labels"
            )

        for payload in payloads:
            speaker = str(payload.get("speaker") or "")
            payload["speaker"] = cls._ECP_SPEAKER_LABELS.get(speaker, speaker)
        return payloads

    @classmethod
    def _employee_dialogue_role_mapping(cls, history_mode: str) -> dict[str, str]:
        mapping = dict(cls._ECP_DIALOGUE_ROLE_MAPPING)
        if history_mode == "legacy_labels":
            mapping.update(
                {
                    "employee": "当前扮演的员工（legacy_labels 历史标签）",
                    "manager": "经理（legacy_labels 历史标签）",
                }
            )
        return mapping

    @staticmethod
    def _merge_retrieved_chunks(
        *groups: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        merged: list[RetrievedChunk] = []
        seen: set[str] = set()
        for group in groups:
            for chunk in group:
                if chunk.chunk_id in seen:
                    continue
                seen.add(chunk.chunk_id)
                merged.append(chunk)
        return merged

    @staticmethod
    def _serialize_retrieved_chunks(chunks: list[RetrievedChunk] | None) -> list[dict]:
        return [chunk.model_dump(mode="json", exclude_none=True) for chunk in chunks or []]

    @staticmethod
    def _reply_prefixes(state: SessionState) -> list[str]:
        profile = state.employee_profile
        if not profile:
            return []
        values = [getattr(profile, "employee_alias", None), getattr(profile, "name", None)]
        return [str(value).strip() for value in values if str(value or "").strip()]

    @staticmethod
    def _build_reply_prompt(
        state: SessionState,
        latest_manager_message: str,
        retrieved_chunks: list[RetrievedChunk] | None = None,
        *,
        conversation_context: ConversationPromptContext | None = None,
        test_prompt_enabled: bool | None = None,
        dialogue_history_mode: str | None = None,
        pattern_response_guidance: str = "",
    ) -> str:
        ensure_current_performance_locale(state)
        prompt_context = conversation_context or ConversationPromptContext(
            output_locale=state.locale,
            raw_turns=list(state.conversation)
        )
        profile_payload = EmployeeAgent._employee_visible_profile_payload(state)
        personality_behavior_profile = (
            get_personality_behavior_resolver().resolve(
                state.personality,
                state.personality_facets,
            )
        )
        settings = get_settings()
        if test_prompt_enabled is None:
            test_prompt_enabled = settings.employee_reply_test_prompt_enabled
        if dialogue_history_mode is None:
            dialogue_history_mode = settings.employee_dialogue_history_mode
        template_name = (
            "employee/reply_test.jinja2"
            if test_prompt_enabled
            else "employee/reply.jinja2"
        )
        return PromptService().render(
            template_name,
            profile=profile_payload,
            profile_json=json.dumps(
                profile_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            supplemental_info=EmployeeAgent._employee_visible_text(
                state.supplemental_info
            ),
            performance_context=EmployeeAgent._employee_visible_text(
                state.intent.performance_context
                if state.intent
                else ""
            ),
            personality_behavior_profile_json=personality_behavior_prompt_json(
                personality_behavior_profile
            ),
            motivation=state.motivation.model_dump(mode="json", exclude_none=True) if state.motivation else {},
            emotion_state=state.emotion_state.model_dump(mode="json", exclude_none=True) if state.emotion_state else {},
            rehearsal_context=EmployeeAgent._employee_visible_value(
                state.rehearsal_context.model_dump(mode="json", exclude_none=True)
            ),
            retrieved_chunks=EmployeeAgent._serialize_retrieved_chunks(retrieved_chunks),
            conversation_summary=prompt_context.summary_text,
            summary_emotion_continuity_json=json.dumps(
                EmployeeAgent._employee_visible_value(
                    prompt_context.summary_emotion_continuity
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            dialogue_role_mapping=json.dumps(
                EmployeeAgent._employee_dialogue_role_mapping(
                    dialogue_history_mode
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            egocentric_conversation=json.dumps(
                EmployeeAgent._employee_egocentric_conversation(
                    prompt_context.raw_turns,
                    latest_manager_message,
                    history_mode=dialogue_history_mode,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            latest_partner_message=latest_manager_message,
            pattern_response_guidance=pattern_response_guidance,
            output_locale=state.locale,
        )

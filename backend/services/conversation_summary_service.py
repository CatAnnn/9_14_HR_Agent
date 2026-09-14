from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from functools import lru_cache
import json
import logging

from backend.config.settings import Settings, get_settings
from backend.observability.metrics import (
    elapsed_ms,
    log_metric,
    now_ms,
    record_conversation_summary,
)
from backend.repositories.conversation_summary_repository import ConversationSummaryRepository
from backend.schemas.conversation import ConversationTurn, EmotionTurnSnapshot
from backend.schemas.conversation_summary import ConversationPromptContext, ConversationSummaryRecord
from backend.schemas.locale import localized_value
from backend.schemas.state import SessionState
from backend.services.executor_utils import run_db_with_context
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.model_scheduler import ModelScheduler


logger = logging.getLogger(__name__)

_SUMMARY_PROMPT_TEXT = {
    "zh-CN": (
        "你只负责更新绩效面谈的滚动对话摘要。将已有摘要与新增对话合并成一份可直接替代旧摘要的新摘要。"
        "保留已经确认的事实、数字、时间、人名、员工诉求、双方异议、经理承诺、员工立场变化和仍未解决的问题。"
        "不要推断满足度、VAD、大五人格或面谈目的，不要编造事实，不要评价经理表现。使用简洁中文纯文本，"
        "不使用Markdown，不超过800个汉字。姓名、数字和直接对话证据保持原样。只输出更新后的摘要。"
    ),
    "en": (
        "You only update the rolling summary of a performance conversation. Merge the existing summary "
        "and the new turns into a replacement summary. Preserve confirmed facts, figures, dates, names, "
        "employee needs, disagreements, manager commitments, changes in the employee's position, and "
        "unresolved issues. Do not infer satisfaction, VAD, Big Five traits, or the conversation intent. "
        "Do not invent facts or evaluate the manager. Use concise English plain text, no Markdown, and no "
        "more than 800 words. Preserve names, figures, and direct dialogue evidence in their original form. "
        "Output only the updated summary."
    ),
    "de": (
        "Du aktualisierst ausschließlich die fortlaufende Zusammenfassung eines Leistungsdialogs. Führe die "
        "bestehende Zusammenfassung und die neuen Gesprächsbeiträge zu einer vollständigen Ersatzfassung "
        "zusammen. Bewahre bestätigte Fakten, Zahlen, Daten, Namen, Anliegen der beschäftigten Person, "
        "Meinungsverschiedenheiten, Zusagen der Führungskraft, Veränderungen der Position der beschäftigten "
        "Person und ungelöste Punkte. Leite weder Zufriedenheit, VAD, Big-Five-Merkmale noch die Gesprächsabsicht "
        "ab. Erfinde keine Fakten und bewerte die Führungskraft nicht. Verwende knappen deutschen Klartext ohne "
        "Markdown und höchstens 800 Wörter. Namen, Zahlen und direkte Gesprächsbelege bleiben in ihrer "
        "ursprünglichen Form. Gib nur die aktualisierte Zusammenfassung aus."
    ),
    "ja": (
        "あなたは人事評価面談のローリング要約のみを更新します。既存の要約と新しい発言を統合し、"
        "旧要約を置き換えられる新しい要約を作成してください。確認済みの事実、数値、日付、氏名、"
        "従業員の要望、双方の相違点、管理職の約束、従業員の立場の変化、未解決事項を保持してください。"
        "満足度、VAD、ビッグファイブ特性、面談意図を推測せず、事実を創作せず、管理職を評価しないで"
        "ください。簡潔な日本語のプレーンテキストを使用し、Markdownは使わず、800文字以内にしてください。"
        "氏名、数値、直接の対話証拠は原文のまま保持してください。更新後の要約だけを出力してください。"
    ),
}
_SUMMARY_PROMPT_LABELS = {
    "zh-CN": ("已有摘要：", "无", "新增对话："),
    "en": ("Existing summary: ", "None", "New turns: "),
    "de": ("Bestehende Zusammenfassung: ", "Keine", "Neue Gesprächsbeiträge: "),
    "ja": ("既存の要約：", "なし", "新しい発言："),
}


class ConversationSummaryService:
    """Build and asynchronously advance rolling summaries used by employee replies."""

    executor: Executor | None = None

    PROMPT_VERSION = "v2"

    def __init__(
        self,
        settings: Settings | None = None,
        repository: ConversationSummaryRepository | None = None,
        llm_service: LangChainLLMService | None = None,
        model_scheduler: ModelScheduler | None = None,
        executor: Executor | None = None,
    ):
        self.settings = settings or get_settings()
        self.repository = repository or ConversationSummaryRepository()
        self.llm_service = llm_service or LangChainLLMService()
        self.model_scheduler = model_scheduler or ModelScheduler(self.settings)
        self.executor = executor
        self._tasks: dict[tuple[str, int], asyncio.Task[None]] = {}

    async def context_for_reply(self, state: SessionState) -> ConversationPromptContext:
        if not self._can_have_summary(state):
            return ConversationPromptContext(
                output_locale=state.locale,
                raw_turns=self._visible_raw_turns(state),
            )

        try:
            record = await run_db_with_context(
                self.executor,
                "conversation_summary.read",
                self.repository.get,
                state.session_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_context_failure(state, exc)
            return ConversationPromptContext(
                output_locale=state.locale,
                raw_turns=self._visible_raw_turns(state),
            )

        record = self._record_for_locale(state, record)
        context = self._build_context(state, record)
        self._schedule_if_due(state, record, trigger="before_employee_reply")
        return context

    @staticmethod
    def _visible_raw_turns(
        state: SessionState,
        *,
        covered_through_turn_index: int = 0,
    ) -> list[ConversationTurn]:
        return [
            turn
            for turn in state.conversation
            if turn.turn_index > covered_through_turn_index
            and not (
                turn.speaker == "system"
                and turn.metadata.get("type") == "rehearsal_context_update"
            )
        ]

    async def ensure_scheduled(self, state: SessionState) -> bool:
        if not self._can_have_summary(state):
            return False
        try:
            record = await run_db_with_context(
                self.executor,
                "conversation_summary.read",
                self.repository.get,
                state.session_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_context_failure(state, exc)
            return False
        return self._schedule_if_due(
            state,
            self._record_for_locale(state, record),
            trigger="after_session_save",
        )

    def reset_generation(self, session_id: str) -> ConversationSummaryRecord:
        return self.repository.reset_generation(session_id)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def _can_have_summary(self, state: SessionState) -> bool:
        return len(state.conversation) >= (
            self.settings.conversation_raw_turn_window
            + self.settings.conversation_summary_batch_turns
        )

    @classmethod
    def _prompt_version(cls, locale: str) -> str:
        return f"{cls.PROMPT_VERSION}:{locale}"

    @classmethod
    def _record_for_locale(
        cls,
        state: SessionState,
        record: ConversationSummaryRecord | None,
    ) -> ConversationSummaryRecord:
        expected_version = cls._prompt_version(state.locale)
        if record is None:
            return ConversationSummaryRecord(
                session_id=state.session_id,
                prompt_version=expected_version,
            )
        if record.prompt_version == expected_version or (
            state.locale == "zh-CN" and record.prompt_version == "v1"
        ):
            return record
        # Start a new monotonic generation without reusing prose from another locale.
        return ConversationSummaryRecord(
            session_id=state.session_id,
            generation=record.generation + 1,
            prompt_version=expected_version,
        )

    @staticmethod
    def _build_context(
        state: SessionState,
        record: ConversationSummaryRecord,
    ) -> ConversationPromptContext:
        raw_turns = [
            turn
            for turn in state.conversation
            if turn.turn_index > record.covered_through_turn_index
            and not (
                turn.speaker == "system"
                and turn.metadata.get("type") == "rehearsal_context_update"
            )
        ]
        return ConversationPromptContext(
            output_locale=state.locale,
            summary_text=record.summary_text,
            covered_through_turn_index=record.covered_through_turn_index,
            summary_emotion_continuity=(
                ConversationSummaryService._summary_emotion_continuity(
                    state,
                    record.covered_through_turn_index,
                )
            ),
            raw_turns=raw_turns,
        )

    @staticmethod
    def _summary_emotion_continuity(
        state: SessionState,
        covered_through_turn_index: int,
    ) -> dict[str, object]:
        """Return the last valid internal emotion state at the summary boundary.

        The free-text reason is deliberately excluded: it is an upstream internal
        inference, not dialogue evidence.  The boundary state is derived from the
        persisted turns so old summary rows need no schema migration.
        """

        if covered_through_turn_index <= 0:
            return {}
        for turn in reversed(state.conversation):
            if turn.turn_index > covered_through_turn_index:
                continue
            metadata = turn.metadata if isinstance(turn.metadata, dict) else {}
            raw_snapshot = metadata.get("emotion_snapshot")
            if not isinstance(raw_snapshot, dict):
                continue
            try:
                snapshot = EmotionTurnSnapshot.model_validate(raw_snapshot)
            except (TypeError, ValueError):
                continue
            return {
                "covered_through_turn_index": covered_through_turn_index,
                "snapshot_turn_index": turn.turn_index,
                "emotion_snapshot": snapshot.model_dump(
                    mode="json",
                    exclude_none=True,
                    exclude={"reason_summary"},
                ),
            }
        return {}

    def _schedule_if_due(
        self,
        state: SessionState,
        record: ConversationSummaryRecord,
        *,
        trigger: str,
    ) -> bool:
        due_turns = self._due_turns(state, record)
        if not due_turns:
            return False

        target_turn_index = due_turns[-1].turn_index
        is_compensation = trigger == "before_employee_reply"

        key = (state.session_id, record.generation)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            log_metric(
                "conversation.summary",
                task_name="conversation_summary",
                session_id=state.session_id,
                generation=record.generation,
                status="duplicate_skipped",
                trigger=trigger,
                is_compensation=is_compensation,
                target_turn_index=target_turn_index,
            )
            return False

        snapshot = state.model_copy(deep=True)
        task = asyncio.create_task(
            self._update_summary(snapshot, record.generation, trigger=trigger),
            name=f"conversation-summary:{state.session_id}:{record.generation}",
        )
        self._tasks[key] = task
        task.add_done_callback(lambda completed, task_key=key: self._forget_task(task_key, completed))
        log_metric(
            "conversation.summary",
            task_name="conversation_summary",
            session_id=state.session_id,
            generation=record.generation,
            status="scheduled",
            trigger=trigger,
            is_compensation=is_compensation,
            target_turn_index=target_turn_index,
        )
        return True

    def _forget_task(self, key: tuple[str, int], task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:  # noqa: BLE001
            logger.exception("Unable to consume conversation summary task result")

    async def _update_summary(
        self,
        state: SessionState,
        generation: int,
        *,
        trigger: str,
    ) -> None:
        started = now_ms()
        try:
            current = await run_db_with_context(
                self.executor,
                "conversation_summary.read",
                self.repository.get,
                state.session_id,
            )
            current = self._record_for_locale(state, current)
            if current.generation != generation:
                self._log_result(
                    state,
                    generation,
                    status="stale_generation",
                    started=started,
                    trigger=trigger,
                )
                return

            due_turns = self._due_turns(state, current)
            if not due_turns:
                self._log_result(
                    state,
                    generation,
                    status="not_due",
                    started=started,
                    trigger=trigger,
                )
                return

            target_turn_index = due_turns[-1].turn_index
            prompt = self._build_prompt(
                current.summary_text,
                due_turns,
                output_locale=state.locale,
            )
            timeout_seconds = self.settings.timeout_for_task("conversation_summary")
            model_name = self.settings.model_for_task("conversation_summary")
            async with self.model_scheduler.slot(
                session_id=state.session_id,
                category="background",
                endpoint=self.settings.chat_url,
                model=model_name,
            ):
                summary_text = await asyncio.wait_for(
                    self.llm_service.ainvoke_text(
                        prompt=prompt,
                        task_name="conversation_summary",
                        model=model_name,
                        temperature=self.settings.temperature_for_task("conversation_summary"),
                        max_tokens=self.settings.max_tokens_for_task("conversation_summary"),
                        enable_thinking=self.settings.enable_thinking_for_task("conversation_summary"),
                    ),
                    timeout=timeout_seconds,
                )
            summary_text = summary_text.strip()
            if not summary_text:
                raise ValueError("Conversation summary model returned empty content.")

            saved = await run_db_with_context(
                self.executor,
                "conversation_summary.save",
                self.repository.save_if_newer,
                session_id=state.session_id,
                generation=generation,
                summary_text=summary_text,
                covered_through_turn_index=target_turn_index,
                model_name=self.settings.model_for_task("conversation_summary"),
                prompt_version=self._prompt_version(state.locale),
            )
            self._log_result(
                state,
                generation,
                status="success" if saved is not None else "stale_write_rejected",
                started=started,
                target_turn_index=target_turn_index,
                summarized_turn_count=len(due_turns),
                trigger=trigger,
            )
        except asyncio.CancelledError:
            self._log_result(
                state,
                generation,
                status="cancelled",
                started=started,
                trigger=trigger,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Conversation summary update failed for session_id=%s: %s",
                state.session_id,
                type(exc).__name__,
            )
            self._log_result(
                state,
                generation,
                status="failed",
                started=started,
                trigger=trigger,
                error_type=type(exc).__name__,
            )

    def _due_turns(
        self,
        state: SessionState,
        record: ConversationSummaryRecord,
    ) -> list[ConversationTurn]:
        window = self.settings.conversation_raw_turn_window
        overflow = state.conversation[:-window] if len(state.conversation) > window else []
        eligible = [
            turn
            for turn in overflow
            if turn.turn_index > record.covered_through_turn_index
            and not (
                turn.speaker == "system"
                and turn.metadata.get("type") == "rehearsal_context_update"
            )
        ]
        batch_size = self.settings.conversation_summary_batch_turns
        summarized_count = (len(eligible) // batch_size) * batch_size
        return eligible[:summarized_count]

    @staticmethod
    def _summary_turn_payloads(
        turns: list[ConversationTurn],
    ) -> list[dict[str, object]]:
        """Expose dialogue semantics, never persisted operational turn metadata."""

        return [
            {
                "turn_index": turn.turn_index,
                "speaker": turn.speaker,
                "text": turn.text,
            }
            for turn in turns
        ]

    @classmethod
    def _build_prompt(
        cls,
        existing_summary: str,
        turns: list[ConversationTurn],
        *,
        output_locale: str = "zh-CN",
    ) -> str:
        payload = cls._summary_turn_payloads(turns)
        instruction = localized_value(output_locale, _SUMMARY_PROMPT_TEXT)
        existing_label, no_summary, turns_label = localized_value(
            output_locale,
            _SUMMARY_PROMPT_LABELS,
        )
        return (
            f"{instruction}\n"
            f"{existing_label}{existing_summary.strip() or no_summary}\n"
            f"{turns_label}{json.dumps(payload, ensure_ascii=False)}"
        )

    @staticmethod
    def _log_context_failure(state: SessionState, exc: Exception) -> None:
        logger.warning(
            "Conversation summary context load failed for session_id=%s: %s",
            state.session_id,
            type(exc).__name__,
        )
        log_metric(
            "conversation.summary",
            task_name="conversation_summary",
            session_id=state.session_id,
            status="context_load_failed",
            error_type=type(exc).__name__,
        )
        record_conversation_summary(
            status="context_load_failed",
            duration_ms=0,
            trigger="context_load",
            is_compensation=False,
        )

    @staticmethod
    def _log_result(
        state: SessionState,
        generation: int,
        *,
        status: str,
        started: int,
        trigger: str,
        **fields: object,
    ) -> None:
        duration_ms = elapsed_ms(started)
        is_compensation = trigger == "before_employee_reply"
        log_metric(
            "conversation.summary",
            task_name="conversation_summary",
            session_id=state.session_id,
            generation=generation,
            status=status,
            duration_ms=duration_ms,
            trigger=trigger,
            is_compensation=is_compensation,
            **fields,
        )
        record_conversation_summary(
            status=status,
            duration_ms=duration_ms,
            trigger=trigger,
            is_compensation=is_compensation,
            target_turn_index=int(fields.get("target_turn_index") or 0),
            summarized_turn_count=int(fields.get("summarized_turn_count") or 0),
        )


@lru_cache(maxsize=1)
def get_conversation_summary_service() -> ConversationSummaryService:
    return ConversationSummaryService()

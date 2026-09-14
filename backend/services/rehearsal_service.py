from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import Executor
from contextlib import aclosing, nullcontext
from datetime import datetime, timezone
import hashlib
import logging
from typing import TYPE_CHECKING
import uuid

from backend.agents.employee_agent import EmployeeAgent
from backend.config.settings import Settings, get_settings
from backend.exceptions.workflow_errors import WorkflowError
from backend.observability.rehearsal_timing import RehearsalTurnTiming
from backend.services.conversation_summary_service import (
    ConversationSummaryService,
    get_conversation_summary_service,
)
from backend.services.emotion_transition_service import EmotionTransitionService
from backend.services.employee_state_transition_service import (
    EmployeeStateTransitionResult,
    EmployeeStateTransitionService,
)
from backend.services.executor_utils import run_db_with_context
from backend.services.fish_tts_service import (
    FishTtsService,
    TtsConfigurationError,
    TtsError,
)
from backend.services.local_model_runtime import LocalModelRuntimeRecycler, get_local_model_runtime_recycler
from backend.services.model_scheduler import ModelScheduler
from backend.services.motivation_scoring_service import MotivationScoringService
from backend.services.personality_facet_service import PersonalityFacetService
from backend.services.rehearsal_dimension_evaluation_service import RehearsalDimensionEvaluationService
from backend.services.retrieval_service import RetrievalService
from backend.redis.distributed_coordination import (
    RedisDistributedCoordinator,
    get_distributed_coordinator,
)
from backend.services.session_service import SessionService
from backend.workflows.graph import RehearsalWorkflow
from backend.workflows.guards import ensure_rehearsal_allowed
from backend.schemas.conversation import (
    CONVERSATION_TURN_LOCALE_METADATA_KEY,
    ConversationTurn,
    attach_emotion_snapshot,
)
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.simulation import MotivationState
from backend.schemas.state import (
    REHEARSAL_DIMENSION_COVERAGE_VERSION,
    RehearsalRuntimeContext,
    SessionState,
)


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.services.speech_playback_service import SpeechPlaybackBuffer


class _CriticalDbCancelled(asyncio.CancelledError):
    def __init__(self, operation: str, result: object) -> None:
        super().__init__(
            f"Cancelled after critical DB operation completed: {operation}"
        )
        self.operation = operation
        self.result = result


class RehearsalService:
    """Business entry for rehearsal."""

    executor: Executor | None = None
    _REQUEST_ID_METADATA_KEY = "rehearsal_request_id"

    def __init__(
        self,
        settings: Settings | None = None,
        runtime_recycler: LocalModelRuntimeRecycler | None = None,
        summary_service: ConversationSummaryService | None = None,
        tts_service: FishTtsService | None = None,
        model_scheduler: ModelScheduler | None = None,
        session_service: SessionService | None = None,
        retrieval: RetrievalService | None = None,
        motivation_scoring: MotivationScoringService | None = None,
        emotion_transition: EmotionTransitionService | None = None,
        employee_state_transition: EmployeeStateTransitionService | None = None,
        personality_facets: PersonalityFacetService | None = None,
        dimension_evaluation: RehearsalDimensionEvaluationService | None = None,
        executor: Executor | None = None,
        coordinator: RedisDistributedCoordinator | None = None,
    ):
        self.settings = settings or get_settings()
        self.runtime_recycler = runtime_recycler or get_local_model_runtime_recycler()
        self.model_scheduler = model_scheduler or ModelScheduler(self.settings)
        self.session_service = session_service or SessionService(
            runtime_recycler=self.runtime_recycler
        )
        self.summary_service = summary_service or get_conversation_summary_service()
        self.motivation_scoring = motivation_scoring or MotivationScoringService()
        self.emotion_transition = emotion_transition or EmotionTransitionService()
        self.employee_state_transition = (
            employee_state_transition
            or EmployeeStateTransitionService(
                settings=self.settings,
                emotion_transition=self.emotion_transition,
                summary_service=self.summary_service,
                model_scheduler=self.model_scheduler,
            )
        )
        self.personality_facets = personality_facets or PersonalityFacetService(
            settings=self.settings,
            model_scheduler=self.model_scheduler,
            executor=executor,
        )
        self.dimension_evaluation = dimension_evaluation
        self.workflow = RehearsalWorkflow(
            summary_service=self.summary_service,
            retrieval=retrieval,
            motivation_scoring=self.motivation_scoring,
            employee_state_transition=self.employee_state_transition,
            emotion_transition=self.emotion_transition,
        )
        self.executor = executor
        self.tts_service = tts_service or FishTtsService(self.settings)
        self.coordinator = coordinator or get_distributed_coordinator(self.settings)

    def _session_speech_voice(
        self,
        state: SessionState,
        requested_voice: str | None,
    ) -> str:
        candidate = (
            state.rehearsal_context.speech_voice
            or requested_voice
            or self.settings.tts_default_voice
        )
        persisted_voice = state.rehearsal_context.speech_voice
        resolver = getattr(self.tts_service, "resolve_voice", None)
        if callable(resolver):
            try:
                resolved = resolver(candidate)
            except TtsConfigurationError:
                if (
                    not persisted_voice
                    or persisted_voice.casefold()
                    == self.settings.tts_default_voice.strip().casefold()
                ):
                    raise
                resolved = resolver(self.settings.tts_default_voice)
        else:
            resolved = candidate.strip()
        if not resolved:
            raise TtsError("未配置可用的员工语音音色。")
        if state.rehearsal_context.speech_voice != resolved:
            state.rehearsal_context.speech_voice = resolved
            state.rehearsal_context.touch()
        return resolved

    def _session_speech_seed(self, state: SessionState) -> int | None:
        context = state.rehearsal_context
        if context.speech_seed is not None:
            return context.speech_seed
        base_seed = (
            self.settings.tts_seed if self.settings.tts_seed is not None else 42
        )
        material = f"hr-agent-tts-session-v1:{base_seed}:{state.session_id}".encode(
            "utf-8"
        )
        seed = int.from_bytes(
            hashlib.blake2b(material, digest_size=8).digest(),
            "big",
        ) & (2**63 - 1)
        context.speech_seed = seed
        context.touch()
        return seed

    def _start_dimension_evaluation(
        self,
        state: SessionState,
        manager_message: str,
        *,
        timing: RehearsalTurnTiming | None = None,
    ) -> asyncio.Task | None:
        if self.dimension_evaluation is None:
            if timing is not None:
                timing.record_skipped("dimension_evaluation")
            return None
        snapshot = state.model_copy(deep=True)

        async def evaluate():
            if timing is None:
                return await self.dimension_evaluation.evaluate_manager_turn(
                    snapshot,
                    manager_message,
                )
            with timing.stage(
                "dimension_evaluation",
                parallel_group="analysis",
            ):
                return await self.dimension_evaluation.evaluate_manager_turn(
                    snapshot,
                    manager_message,
                )

        return asyncio.create_task(
            evaluate(),
            name=f"rehearsal-dimensions:{state.session_id}",
        )

    async def _finish_dimension_evaluation(
        self,
        state: SessionState,
        task: asyncio.Task | None,
    ) -> bool:
        if task is None:
            return False
        try:
            result = await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Rehearsal dimension evaluation task failed: error_type=%s",
                type(exc).__name__,
            )
            result = None
        if result is None:
            context = state.rehearsal_context
            changed = False
            if (
                context.dimension_coverage_version
                < REHEARSAL_DIMENSION_COVERAGE_VERSION
            ):
                if context.covered_dimensions:
                    context.covered_dimensions = []
                    context.touch()
                    changed = True
                warning = (
                    "对话维度模型评估暂不可用，"
                    "已隐藏旧口径统计，后续成功评估后恢复。"
                )
            else:
                warning = "对话维度模型评估暂不可用，本轮保留已有状态。"
            if warning not in state.warnings:
                state.warnings.append(warning)
                changed = True
            return changed
        context = state.rehearsal_context
        changed = False
        if result != context.covered_dimensions:
            context.covered_dimensions = result
            changed = True
        if (
            context.dimension_coverage_version
            < REHEARSAL_DIMENSION_COVERAGE_VERSION
        ):
            context.dimension_coverage_version = (
                REHEARSAL_DIMENSION_COVERAGE_VERSION
            )
            changed = True
        if changed:
            context.touch()
        return changed

    async def send_manager_message(
        self,
        session_id: str,
        message: str,
        *,
        request_id: str | None = None,
    ) -> SessionState:
        timing = RehearsalTurnTiming(
            session_id=session_id,
            transport="nonstream",
            speech_enabled=False,
            explicit_thinking_enabled=bool(
                self.settings.enable_thinking_for_task("employee_reply")
            ),
        )
        lock_stage = timing.start_stage("session_lock_wait")
        with timing.trace():
            try:
                async with self.coordinator.session_lock("rehearsal", session_id):
                    timing.finish_stage(lock_stage)
                    state, replayed = await self._send_manager_message(
                        session_id,
                        message,
                        request_id=request_id,
                        timing=timing,
                    )
                    payload = timing.finalize(
                        "replayed" if replayed else "success"
                    )
                    if replayed:
                        return state
                    self._attach_rehearsal_timing(state, payload)
                    state = await self._persist_final_timing_metadata(state)
                    return state
            except asyncio.CancelledError:
                timing.finish_stage(lock_stage, outcome="cancelled")
                timing.finalize("cancelled")
                raise
            except BaseException:
                timing.finish_stage(lock_stage, outcome="error")
                timing.finalize("error")
                raise

    async def _send_manager_message(
        self,
        session_id: str,
        message: str,
        *,
        request_id: str | None = None,
        timing: RehearsalTurnTiming | None = None,
    ) -> tuple[SessionState, bool]:
        if timing is None:
            timing = RehearsalTurnTiming(
                session_id=session_id,
                transport="nonstream",
                speech_enabled=False,
                explicit_thinking_enabled=bool(
                    self.settings.enable_thinking_for_task("employee_reply")
                ),
            )
        with timing.stage("session_read"):
            state = await run_db_with_context(
                self.executor,
                "rehearsal.session.read",
                self.session_service.get_session,
                session_id,
            )
        completed_turns = self._completed_request_turns(
            state,
            request_id=request_id,
            manager_message=message,
        )
        if completed_turns is not None:
            timing.set_turn_indexes(
                manager_turn_index=completed_turns[0],
                employee_turn_index=completed_turns[1],
            )
            timing.record_skipped("idempotent_replay")
            return state, True
        ensure_rehearsal_allowed(state)
        if state.personality is not None:
            with timing.stage("personality_context"):
                await self.personality_facets.ensure_for_state(
                    state,
                    allow_model=False,
                )
        else:
            timing.record_skipped("personality_context")
        # Embedding and reranker protect each real model call with a request-scoped
        # runtime lease. A rehearsal-scoped lease would let an abandoned session
        # keep both models resident forever and defeat idle recycling.
        timing.record_skipped("model_lease")
        next_index = len(state.conversation) + 1
        timing.set_turn_indexes(
            manager_turn_index=next_index,
            employee_turn_index=next_index + 1,
        )
        dimension_task = self._start_dimension_evaluation(
            state,
            message,
            timing=timing,
        )
        try:
            timing.mark_milestone("generating")
            with timing.stage("workflow_nonstream", parallel_group="response"):
                async with self.model_scheduler.slot(
                    session_id=session_id,
                    category="interactive",
                    endpoint=self.settings.chat_url,
                    model=self.settings.model_for_task("employee_reply"),
                ) as queue_slot:
                    timing.record_duration(
                        "employee_model_queue",
                        getattr(queue_slot, "queue_ms", None),
                        parallel_group="response",
                    )
                    new_state = await self.workflow.invoke(
                        state,
                        {"manager_message": message},
                    )
            self._attach_request_id(
                new_state,
                request_id=request_id,
                manager_turn_index=next_index,
                employee_turn_index=next_index + 1,
            )
            timing.mark_milestone("text_complete")
            with timing.stage("dimension_apply"):
                await self._finish_dimension_evaluation(
                    new_state,
                    dimension_task,
                )
        finally:
            if dimension_task is not None and not dimension_task.done():
                dimension_task.cancel()
                await asyncio.gather(dimension_task, return_exceptions=True)
        business_saved = False
        saved = new_state
        try:
            self._attach_rehearsal_timing(
                new_state,
                {**timing.snapshot(), "outcome": "in_progress"},
            )
            with timing.stage("session_save"):
                try:
                    saved = await self._run_critical_db(
                        "rehearsal.session.save",
                        self.session_service.save_session,
                        new_state,
                    )
                except _CriticalDbCancelled as exc:
                    if isinstance(exc.result, SessionState):
                        saved = exc.result
                    business_saved = True
                    raise
                else:
                    business_saved = True
            with timing.stage("summary_schedule"):
                await self.summary_service.ensure_scheduled(saved)
        except asyncio.CancelledError:
            timing_payload = timing.finalize("cancelled")
            if business_saved and self._attach_rehearsal_timing(
                saved,
                timing_payload,
            ):
                await self._persist_final_timing_metadata(saved)
            raise
        except Exception:
            timing_payload = timing.finalize("error")
            if business_saved and self._attach_rehearsal_timing(
                saved,
                timing_payload,
            ):
                await self._persist_final_timing_metadata(saved)
            raise
        return saved, False

    async def stream_manager_message(
        self,
        session_id: str,
        message: str,
        *,
        request_id: str | None = None,
        speech_enabled: bool = False,
        speech_voice: str | None = None,
        speech_stream_id: str | None = None,
        speech_cancel_event: asyncio.Event | None = None,
        speech_playback_buffer: SpeechPlaybackBuffer | None = None,
    ) -> AsyncIterator[dict]:
        timing = RehearsalTurnTiming(
            session_id=session_id,
            transport="stream",
            speech_enabled=speech_enabled,
            explicit_thinking_enabled=bool(
                self.settings.enable_thinking_for_task("employee_reply")
            ),
        )
        lock_stage = timing.start_stage("session_lock_wait")
        with timing.trace():
            try:
                async with self.coordinator.session_lock("rehearsal", session_id):
                    timing.finish_stage(lock_stage)
                    async with aclosing(
                        self._stream_manager_message(
                            session_id,
                            message,
                            request_id=request_id,
                            speech_enabled=speech_enabled,
                            speech_voice=speech_voice,
                            speech_stream_id=speech_stream_id,
                            speech_cancel_event=speech_cancel_event,
                            speech_playback_buffer=speech_playback_buffer,
                            timing=timing,
                        )
                    ) as event_stream:
                        async for event in event_stream:
                            yield event
            except (asyncio.CancelledError, GeneratorExit):
                timing.finish_stage(lock_stage, outcome="cancelled")
                if not timing.finalized:
                    timing.finalize("cancelled")
                raise
            except BaseException:
                timing.finish_stage(lock_stage, outcome="error")
                if not timing.finalized:
                    timing.finalize("error")
                raise
            finally:
                if not timing.finalized:
                    timing.finalize("cancelled")

    async def _stream_manager_message(
        self,
        session_id: str,
        message: str,
        *,
        request_id: str | None = None,
        speech_enabled: bool = False,
        speech_voice: str | None = None,
        speech_stream_id: str | None = None,
        speech_cancel_event: asyncio.Event | None = None,
        speech_playback_buffer: SpeechPlaybackBuffer | None = None,
        timing: RehearsalTurnTiming | None = None,
    ) -> AsyncIterator[dict]:
        if timing is None:
            timing = RehearsalTurnTiming(
                session_id=session_id,
                transport="stream",
                speech_enabled=speech_enabled,
                explicit_thinking_enabled=bool(
                    self.settings.enable_thinking_for_task("employee_reply")
                ),
            )
        parallel_tasks: list[asyncio.Task] = []
        analysis_stage = None
        state: SessionState | None = None
        saved: SessionState | None = None
        reply_persisted = False
        try:
            with timing.stage("session_read"):
                state = await run_db_with_context(
                    self.executor,
                    "rehearsal.session.read",
                    self.session_service.get_session,
                    session_id,
                )
            completed_turns = self._completed_request_turns(
                state,
                request_id=request_id,
                manager_message=message,
            )
            if completed_turns is not None:
                timing.set_turn_indexes(
                    manager_turn_index=completed_turns[0],
                    employee_turn_index=completed_turns[1],
                )
                timing.record_skipped("idempotent_replay")
                timing.mark_milestone("start_event")
                yield {"event": "start", "idempotent_replay": True}
                timing.mark_milestone("text_complete")
                if speech_enabled and speech_stream_id:
                    yield {
                        "event": "speech_done",
                        "session_id": session_id,
                        "speech_stream_id": speech_stream_id,
                        "idempotent_replay": True,
                    }
                timing_payload = timing.finalize("replayed")
                yield {
                    "event": "done",
                    "state": state.model_dump(mode="json"),
                    "timing": timing_payload,
                    "idempotent_replay": True,
                }
                return
            ensure_rehearsal_allowed(state)
            if state.personality is not None:
                with timing.stage("personality_context"):
                    await self.personality_facets.ensure_for_state(
                        state,
                        allow_model=False,
                    )
            else:
                timing.record_skipped("personality_context")
            # Request-scoped leases in EmbeddingService/RerankService provide the
            # required in-flight protection without pinning models for the session.
            timing.record_skipped("model_lease")
            next_index = len(state.conversation) + 1
            timing.set_turn_indexes(
                manager_turn_index=next_index,
                employee_turn_index=next_index + 1,
            )
            dimension_task = self._start_dimension_evaluation(
                state,
                message,
                timing=timing,
            )
            with timing.stage("manager_turn_prepare"):
                manager_turn = ConversationTurn(
                    turn_index=next_index,
                    speaker="manager",
                    text=message,
                    metadata={
                        **self._request_metadata(request_id),
                        CONVERSATION_TURN_LOCALE_METADATA_KEY: state.locale,
                    },
                )
                state.conversation.append(manager_turn)
                state.user_turn_count += 1
                state.rehearsal_ended_at = None

            employee_agent = self.workflow.nodes.employee_agent
            async def update_motivation() -> SessionState:
                with timing.stage(
                    "motivation_scoring",
                    parallel_group="analysis",
                ):
                    async with self.model_scheduler.slot(
                        session_id=session_id,
                        category="interactive",
                        endpoint=self.settings.chat_url,
                        model=self.settings.model_for_task("motivation_scoring"),
                    ) as queue_slot:
                        timing.record_duration(
                            "motivation_model_queue",
                            getattr(queue_slot, "queue_ms", None),
                            parallel_group="analysis",
                        )
                        return await self.motivation_scoring.update_after_manager_message(
                            state.model_copy(deep=True),
                            message,
                        )

            async def update_state_transition() -> EmployeeStateTransitionResult:
                with timing.stage(
                    "emotion_transition",
                    parallel_group="analysis",
                ):
                    result = await (
                        self.employee_state_transition.update_after_manager_message(
                            state.model_copy(deep=True),
                            message,
                        )
                    )
                timing.record_duration(
                    "emotion_transition_model",
                    result.emotion_transition_ms,
                    parallel_group="state_transition",
                )
                timing.record_duration(
                    "psychological_pattern_transition",
                    result.pattern_dynamics_ms,
                    parallel_group="state_transition",
                )
                timing.record_duration(
                    "emotion_model_queue",
                    result.emotion_model_queue_ms,
                    parallel_group="state_transition",
                )
                timing.record_duration(
                    "pattern_model_queue",
                    result.pattern_model_queue_ms,
                    parallel_group="state_transition",
                )
                return result

            async def retrieve_context() -> list[RetrievedChunk]:
                with timing.stage(
                    "knowledge_retrieval",
                    parallel_group="analysis",
                ):
                    return await employee_agent.aretrieve_reply_context(
                        state,
                        message,
                        raise_errors=True,
                    )

            analysis_stage = timing.start_stage(
                "analysis_parallel",
                parallel_group="analysis",
            )
            motivation_task = asyncio.create_task(update_motivation())
            emotion_task = asyncio.create_task(update_state_transition())
            retrieval_task = asyncio.create_task(retrieve_context())
            parallel_tasks = [motivation_task, emotion_task, retrieval_task]
            if dimension_task is not None:
                parallel_tasks.append(dimension_task)

            timing.mark_milestone("start_event")
            yield {"event": "start"}
            yield {"event": "progress", "stage": "scoring"}

            motivation_result, transition_result, retrieval_result = await asyncio.gather(
                motivation_task,
                emotion_task,
                retrieval_task,
                return_exceptions=True,
            )
            timing.finish_stage(analysis_stage)
            self._merge_parallel_state(state, motivation_result, field_name="motivation", label="动机满足度评分")
            pattern_response_guidance = self._merge_state_transition(
                state, transition_result
            )
            attach_emotion_snapshot(manager_turn, state.emotion_state)
            retrieved_chunks = self._resolve_retrieved_chunks(state, retrieval_result)

            timing.mark_milestone("generating")
            yield {"event": "progress", "stage": "generating"}
            dimension_finished = dimension_task is None
            async with aclosing(
                self._stream_employee_reply(
                    employee_agent,
                    state,
                    message,
                    retrieved_chunks,
                    speech_enabled=speech_enabled,
                    speech_voice=speech_voice,
                    session_id=session_id,
                    speech_stream_id=speech_stream_id,
                    speech_cancel_event=speech_cancel_event,
                    speech_playback_buffer=speech_playback_buffer,
                    timing=timing,
                    pattern_response_guidance=pattern_response_guidance,
                )
            ) as reply_stream:
                async for event in reply_stream:
                    if event.get("event") == "_reply_complete":
                        reply = str(event.get("reply") or "").strip()
                        if not reply:
                            raise ValueError(
                                "Employee Agent returned empty streamed reply."
                            )
                        timing.mark_milestone("text_complete")
                        employee_turn = ConversationTurn(
                            turn_index=next_index + 1,
                            speaker="employee",
                            text=reply,
                            metadata={
                                **self._request_metadata(request_id),
                                CONVERSATION_TURN_LOCALE_METADATA_KEY: state.locale,
                                "rehearsal_timing": {
                                    **timing.snapshot(),
                                    "outcome": "in_progress",
                                }
                            },
                        )
                        state.conversation.append(employee_turn)
                        state.stage = "rehearsal"
                        if dimension_task is not None and dimension_task.done():
                            with timing.stage("dimension_apply"):
                                await self._finish_dimension_evaluation(
                                    state,
                                    dimension_task,
                                )
                            dimension_finished = True

                        with timing.stage("session_save"):
                            try:
                                saved = await self._run_critical_db(
                                    "rehearsal.session.save",
                                    self.session_service.save_session,
                                    state,
                                )
                            except _CriticalDbCancelled as exc:
                                if isinstance(exc.result, SessionState):
                                    saved = exc.result
                                reply_persisted = True
                                raise
                            else:
                                reply_persisted = True
                        continue
                    yield event

            if saved is None:
                raise ValueError("Employee Agent stream ended before the reply was saved.")
            if not dimension_finished:
                with timing.stage("dimension_tail_wait"):
                    persist_dimensions = await self._finish_dimension_evaluation(
                        state,
                        dimension_task,
                    )
                if persist_dimensions:
                    with timing.stage("dimension_save"):
                        try:
                            saved = await self._run_critical_db(
                                "rehearsal.session.save_dimensions",
                                self.session_service.save_session,
                                state,
                            )
                        except _CriticalDbCancelled as exc:
                            if isinstance(exc.result, SessionState):
                                saved = exc.result
                            raise
            with timing.stage("summary_schedule"):
                await self.summary_service.ensure_scheduled(saved)
            timing_payload = timing.finalize("success")
            self._attach_rehearsal_timing(saved, timing_payload)
            saved = await self._persist_final_timing_metadata(saved)
            yield {
                "event": "done",
                "state": saved.model_dump(mode="json"),
                "timing": timing_payload,
            }
        except (asyncio.CancelledError, GeneratorExit):
            for task in parallel_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*parallel_tasks, return_exceptions=True)
            if analysis_stage is not None:
                timing.finish_stage(analysis_stage, outcome="cancelled")
            timing_payload = timing.finalize("cancelled")
            persisted_state = saved if saved is not None else state
            if (
                reply_persisted
                and persisted_state is not None
                and self._attach_rehearsal_timing(
                    persisted_state,
                    timing_payload,
                )
            ):
                await self._persist_final_timing_metadata(persisted_state)
            raise
        except WorkflowError as exc:
            logger.info("Rehearsal stream rejected for session_id=%s: %s", session_id, exc)
            for task in parallel_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*parallel_tasks, return_exceptions=True)
            if analysis_stage is not None:
                timing.finish_stage(analysis_stage, outcome="error")
            timing_payload = timing.finalize("rejected")
            persisted_state = saved if saved is not None else state
            if (
                reply_persisted
                and persisted_state is not None
                and self._attach_rehearsal_timing(
                    persisted_state,
                    timing_payload,
                )
            ):
                await self._persist_final_timing_metadata(persisted_state)
            yield {
                "event": "error",
                "message": str(exc) or type(exc).__name__,
                "timing": timing_payload,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Rehearsal stream failed for session_id=%s", session_id)
            for task in parallel_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*parallel_tasks, return_exceptions=True)
            if analysis_stage is not None:
                timing.finish_stage(analysis_stage, outcome="error")
            timing_payload = timing.finalize("error")
            persisted_state = saved if saved is not None else state
            if (
                reply_persisted
                and persisted_state is not None
                and self._attach_rehearsal_timing(
                    persisted_state,
                    timing_payload,
                )
            ):
                await self._persist_final_timing_metadata(persisted_state)
            yield {
                "event": "error",
                "message": str(exc) or type(exc).__name__,
                "timing": timing_payload,
            }
        finally:
            for task in parallel_tasks:
                if not task.done():
                    task.cancel()
            if parallel_tasks:
                await asyncio.gather(*parallel_tasks, return_exceptions=True)
            if analysis_stage is not None and not timing.finalized:
                timing.finish_stage(analysis_stage, outcome="cancelled")

    async def _stream_employee_reply(
        self,
        employee_agent,
        state: SessionState,
        message: str,
        retrieved_chunks: list[RetrievedChunk],
        *,
        speech_enabled: bool,
        speech_voice: str | None,
        session_id: str | None = None,
        speech_stream_id: str | None = None,
        speech_cancel_event: asyncio.Event | None = None,
        speech_playback_buffer: SpeechPlaybackBuffer | None = None,
        timing: RehearsalTurnTiming | None = None,
        pattern_response_guidance: str = "",
    ) -> AsyncIterator[dict]:
        event_queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=256)
        tts_text_queue: asyncio.Queue[str | None] | None = None
        if (
            speech_enabled
            and self.settings.tts_enabled
            and not (speech_cancel_event and speech_cancel_event.is_set())
        ):
            tts_text_queue = asyncio.Queue()
        reply_chunks: list[str] = []
        resolved_session_id = (session_id or state.session_id).strip()
        resolved_stream_id = (speech_stream_id or uuid.uuid4().hex).strip()
        response_pipeline_stage = (
            timing.start_stage(
                "response_pipeline",
                parallel_group="response",
            )
            if timing is not None
            else None
        )

        async def produce_text() -> None:
            failure: BaseException | None = None
            cancelled = False
            stage_context = (
                timing.stage(
                    "employee_reply",
                    parallel_group="response",
                )
                if timing is not None
                else nullcontext()
            )
            try:
                with stage_context:
                    async with self.model_scheduler.slot(
                        session_id=resolved_session_id,
                        category="interactive",
                        endpoint=self.settings.chat_url,
                        model=self.settings.model_for_task("employee_reply"),
                    ) as queue_slot:
                        if timing is not None:
                            timing.record_duration(
                                "employee_model_queue",
                                getattr(queue_slot, "queue_ms", None),
                                parallel_group="response",
                            )
                        if (
                            getattr(
                                self.settings,
                                "psychological_pattern_dynamics_enabled",
                                True,
                            )
                            and pattern_response_guidance
                            and EmployeeAgent._accepts_pattern_response_guidance(
                                employee_agent.stream_reply
                            )
                        ):
                            text_stream = employee_agent.stream_reply(
                                state,
                                message,
                                retrieved_chunks=retrieved_chunks,
                                pattern_response_guidance=(
                                    pattern_response_guidance
                                ),
                            )
                        else:
                            text_stream = employee_agent.stream_reply(
                                state,
                                message,
                                retrieved_chunks=retrieved_chunks,
                            )
                        async with aclosing(text_stream):
                            async for delta in text_stream:
                                if timing is not None:
                                    timing.mark_milestone("first_visible_delta")
                                reply_chunks.append(delta)
                                if (
                                    tts_text_queue is not None
                                    and speech_task is not None
                                    and not speech_task.done()
                                    and not (
                                        speech_cancel_event and speech_cancel_event.is_set()
                                    )
                                ):
                                    tts_text_queue.put_nowait(delta)
                                await event_queue.put(
                                    (
                                        "event",
                                        {"event": "delta", "text": delta},
                                    )
                                )
            except asyncio.CancelledError as exc:
                cancelled = True
                failure = exc
            except BaseException as exc:  # noqa: BLE001
                failure = exc
            finally:
                if (
                    tts_text_queue is not None
                    and speech_task is not None
                    and not speech_task.done()
                ):
                    tts_text_queue.put_nowait(None)
                await self._signal_pipeline_completion(
                    event_queue,
                    ("text_done", failure),
                    nonblocking=cancelled,
                )

        async def produce_speech() -> None:
            assert tts_text_queue is not None
            cancelled = False
            stage_context = (
                timing.stage(
                    "speech_synthesis",
                    parallel_group="response",
                )
                if timing is not None
                else nullcontext()
            )
            try:
                with stage_context:
                    readiness_gate = getattr(self.tts_service, "ensure_ready", None)
                    if callable(readiness_gate):
                        await readiness_gate()
                    session_voice = self._session_speech_voice(state, speech_voice)
                    session_seed = self._session_speech_seed(state)
                    await event_queue.put(
                        (
                            "event",
                            {
                                "event": "speech_stream_ready",
                                "session_id": resolved_session_id,
                                "speech_stream_id": resolved_stream_id,
                            },
                        )
                    )
                    playback_options = (
                        {"playback_buffer": speech_playback_buffer}
                        if speech_playback_buffer is not None
                        else {}
                    )
                    async with aclosing(self.tts_service.stream(
                        tts_text_queue,
                        emotion_state=state.emotion_state,
                        voice=session_voice,
                        seed=session_seed,
                        **playback_options,
                    )) as speech_stream:
                        async for speech_event in speech_stream:
                            if (
                                timing is not None
                                and speech_event.get("event")
                                in {"speech_start", "speech_audio"}
                            ):
                                timing.mark_milestone("first_audio")
                            await event_queue.put(
                                (
                                    "event",
                                    {
                                        **speech_event,
                                        "session_id": resolved_session_id,
                                        "speech_stream_id": resolved_stream_id,
                                    },
                                )
                            )
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Employee speech synthesis failed: %s", exc)
                message_text = (
                    str(exc).strip()
                    if isinstance(exc, TtsError) and str(exc).strip()
                    else "语音服务暂不可用，本轮文字回复不受影响。"
                )
                try:
                    await event_queue.put(
                        (
                            "event",
                            {
                                "event": "speech_error",
                                "message": message_text,
                                "error_type": type(exc).__name__,
                                "session_id": resolved_session_id,
                                "speech_stream_id": resolved_stream_id,
                            },
                        )
                    )
                except asyncio.CancelledError:
                    cancelled = True
                    raise
            finally:
                await self._signal_pipeline_completion(
                    event_queue,
                    ("speech_done", None),
                    nonblocking=cancelled,
                )

        text_task = asyncio.create_task(produce_text())
        speech_task = asyncio.create_task(produce_speech()) if tts_text_queue is not None else None
        pipeline_tasks = [text_task, *([speech_task] if speech_task is not None else [])]

        async def watch_speech_cancellation() -> None:
            assert speech_cancel_event is not None and speech_task is not None
            await speech_cancel_event.wait()
            if not speech_task.done() and not speech_task.cancelling():
                speech_task.cancel()
            # Cancelling the watcher during SSE teardown must not interrupt the
            # speech task again while it is releasing HTTP/global admission leases.
            await asyncio.shield(asyncio.gather(speech_task, return_exceptions=True))
            if tts_text_queue is not None:
                while not tts_text_queue.empty():
                    tts_text_queue.get_nowait()
            # Also covers cancellation before produce_speech entered its finally.
            await event_queue.put(("speech_done", None))

        if speech_task is not None and speech_cancel_event is not None:
            pipeline_tasks.append(asyncio.create_task(watch_speech_cancellation()))
        text_done = False
        speech_done = speech_task is None
        reply_emitted = False
        pipeline_completed_naturally = False
        producer_error: BaseException | None = None

        try:
            while not (text_done and speech_done):
                kind, payload = await event_queue.get()
                if kind == "event":
                    if isinstance(payload, dict):
                        if (
                            speech_cancel_event is not None
                            and speech_cancel_event.is_set()
                            and str(payload.get("event", "")).startswith("speech_")
                        ):
                            continue
                        yield payload
                    continue
                if kind == "speech_done":
                    speech_done = True
                    continue
                if kind != "text_done":
                    continue

                text_done = True
                if isinstance(payload, BaseException):
                    producer_error = payload
                    if speech_task is not None and not speech_task.done() and not speech_task.cancelling():
                        speech_task.cancel()
                    speech_done = True
                    continue

                reply = "".join(reply_chunks).strip()
                if not reply:
                    producer_error = ValueError("Employee Agent returned empty streamed reply.")
                    if speech_task is not None and not speech_task.done() and not speech_task.cancelling():
                        speech_task.cancel()
                    speech_done = True
                    continue
                if timing is not None:
                    timing.mark_milestone("text_complete")
                reply_emitted = True
                yield {"event": "_reply_complete", "reply": reply}
            pipeline_completed_naturally = True
        finally:
            for task in pipeline_tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*pipeline_tasks, return_exceptions=True)
            if timing is not None and response_pipeline_stage is not None:
                timing.finish_stage(
                    response_pipeline_stage,
                    outcome=(
                        "error"
                        if producer_error is not None
                        else (
                            "success"
                            if reply_emitted and pipeline_completed_naturally
                            else "cancelled"
                        )
                    ),
                )

        if producer_error is not None:
            raise producer_error
        if not reply_emitted:
            raise ValueError("Employee Agent stream ended without a complete reply.")

    @classmethod
    def _request_metadata(cls, request_id: str | None) -> dict[str, str]:
        normalized = str(request_id or "").strip()
        return {cls._REQUEST_ID_METADATA_KEY: normalized} if normalized else {}

    @classmethod
    def _completed_request_turns(
        cls,
        state: SessionState,
        *,
        request_id: str | None,
        manager_message: str,
    ) -> tuple[int, int] | None:
        normalized = str(request_id or "").strip()
        if not normalized:
            return None

        for manager_turn in reversed(state.conversation):
            if manager_turn.speaker != "manager":
                continue
            metadata = manager_turn.metadata or {}
            if metadata.get(cls._REQUEST_ID_METADATA_KEY) != normalized:
                continue
            if manager_turn.text != manager_message:
                raise WorkflowError(
                    "request_id is already associated with a different Manager message."
                )
            employee_turn_index = manager_turn.turn_index + 1
            employee_turn = next(
                (
                    turn
                    for turn in state.conversation
                    if turn.turn_index == employee_turn_index
                    and turn.speaker == "employee"
                    and (turn.metadata or {}).get(cls._REQUEST_ID_METADATA_KEY)
                    == normalized
                ),
                None,
            )
            if employee_turn is None:
                raise WorkflowError(
                    "request_id is already associated with an incomplete rehearsal turn."
                )
            return manager_turn.turn_index, employee_turn.turn_index
        return None

    @classmethod
    def _attach_request_id(
        cls,
        state: SessionState,
        *,
        request_id: str | None,
        manager_turn_index: int,
        employee_turn_index: int,
    ) -> None:
        metadata = cls._request_metadata(request_id)
        if not metadata:
            return

        expected = {
            manager_turn_index: "manager",
            employee_turn_index: "employee",
        }
        matched: set[int] = set()
        for turn in state.conversation:
            expected_speaker = expected.get(turn.turn_index)
            if expected_speaker is None or turn.speaker != expected_speaker:
                continue
            turn.metadata = {**(turn.metadata or {}), **metadata}
            matched.add(turn.turn_index)
        if matched != set(expected):
            raise ValueError(
                "Rehearsal workflow did not produce the expected Manager/Employee turn pair."
            )

    @staticmethod
    def _attach_rehearsal_timing(
        state: SessionState,
        timing_payload: dict,
    ) -> bool:
        employee_turn_index = timing_payload.get("employee_turn_index")
        for turn in reversed(state.conversation):
            if str(turn.speaker) != "employee":
                continue
            if (
                employee_turn_index is not None
                and turn.turn_index != employee_turn_index
            ):
                continue
            turn.metadata = {
                **(turn.metadata or {}),
                "rehearsal_timing": timing_payload,
            }
            return True
        return False

    async def _run_critical_db(
        self,
        operation: str,
        function,
        *args,
    ):
        """Keep a mutating DB operation alive and locked through cancellation."""

        task = asyncio.create_task(
            run_db_with_context(
                self.executor,
                operation,
                function,
                *args,
            ),
            name=f"critical-db:{operation}",
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            try:
                result = task.result()
            except BaseException:  # preserve cancellation, but expose DB failure
                logger.exception(
                    "Critical DB operation failed after cancellation: operation=%s",
                    operation,
                )
                raise cancellation
            raise _CriticalDbCancelled(operation, result) from cancellation

    async def _persist_final_timing_metadata(
        self,
        state: SessionState,
    ) -> SessionState:
        """Persist the finalized waterfall outside its measured business boundary."""

        try:
            return await self._run_critical_db(
                "rehearsal.session.save_timing",
                self.session_service.save_session,
                state,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # observability persistence must not lose the reply
            logger.exception(
                "Failed to persist finalized rehearsal timing: session_id=%s",
                state.session_id,
            )
            return state

    @staticmethod
    async def _signal_pipeline_completion(
        queue: asyncio.Queue[tuple[str, object]],
        item: tuple[str, object],
        *,
        nonblocking: bool,
    ) -> None:
        if not nonblocking:
            await queue.put(item)
            return
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # The consumer is already closing; a terminal signal is no longer needed.
            return

    def _merge_state_transition(
        self,
        state: SessionState,
        result: EmployeeStateTransitionResult | BaseException,
    ) -> str:
        if isinstance(result, BaseException):
            logger.warning(
                "Employee state transition parallel task failed: error_type=%s",
                type(result).__name__,
            )
            self._append_warning(
                state,
                f"情绪转移并发任务失败：{type(result).__name__}",
            )
            return ""
        state.emotion_state = result.state.emotion_state
        state.psychological_pattern_state = (
            result.state.psychological_pattern_state
        )
        self._merge_warnings(state, result.state)
        return result.pattern_response_guidance

    def _merge_parallel_state(
        self,
        state: SessionState,
        result: SessionState | BaseException,
        *,
        field_name: str,
        label: str,
    ) -> None:
        if isinstance(result, BaseException):
            logger.warning("%s parallel task failed: %s", label, result)
            self._append_warning(state, f"{label}并发任务失败：{type(result).__name__}")
            return
        value = getattr(result, field_name, None)
        if value is not None:
            setattr(state, field_name, value)
        self._merge_warnings(state, result)

    def _resolve_retrieved_chunks(
        self,
        state: SessionState,
        result: list[RetrievedChunk] | BaseException,
    ) -> list[RetrievedChunk]:
        if isinstance(result, BaseException):
            logger.warning("Employee reply RAG parallel task failed: %s", result)
            self._append_warning(state, f"员工回复 RAG 检索失败：{type(result).__name__}")
            return []
        return list(result or [])

    @staticmethod
    def _merge_warnings(target: SessionState, source: SessionState) -> None:
        for warning in source.warnings:
            if warning not in target.warnings:
                target.warnings.append(warning)

    @staticmethod
    def _append_warning(state: SessionState, warning: str) -> None:
        if warning not in state.warnings:
            state.warnings.append(warning)

    def update_runtime_context(
        self,
        session_id: str,
        *,
        runtime_note: str | None = None,
        runtime_notes: str | list[str] | None = None,
        clear_context: bool = False,
    ) -> SessionState:
        state = self.session_service.get_session(session_id)
        ensure_rehearsal_allowed(state)

        notes = self._clean_notes(runtime_note, runtime_notes)
        if not any([clear_context, notes]):
            raise ValueError("请先输入要应用到预演的员工信息或模拟提示。")

        if clear_context:
            # Invalidate the rolling summary before clearing the authoritative
            # runtime context. This also rejects any older in-flight summary.
            self.summary_service.reset_generation(session_id)

        lines = ["已更新本轮动态模拟设定。"]
        context = state.rehearsal_context
        if clear_context:
            lines = ["已清空本轮动态模拟设定。"]
            context = RehearsalRuntimeContext(
                speech_voice=context.speech_voice,
                speech_seed=context.speech_seed,
                covered_dimensions=list(context.covered_dimensions),
                dimension_coverage_version=(
                    context.dimension_coverage_version
                ),
            )
            state.psychological_pattern_state = None

        for note in notes:
            context.runtime_notes.append(note)
            lines.append(f"新增信息：{note}")

        context.touch()
        state.rehearsal_context = context
        state.stage = "rehearsal"
        state.coach_report_id = None
        state.rehearsal_ended_at = None

        state.conversation.append(
            ConversationTurn(
                turn_index=len(state.conversation) + 1,
                speaker="system",
                text="\n".join(lines),
                metadata={
                    "type": "rehearsal_context_update",
                    "clear_context": clear_context,
                    "has_runtime_note": bool(notes),
                    "runtime_note_count": len(notes),
                },
            )
        )
        return self.session_service.save_session(state)

    def end_rehearsal(self, session_id: str) -> SessionState:
        state = self.session_service.get_session(session_id)
        state.stage = "rehearsal"
        state.rehearsal_ended_at = datetime.now(timezone.utc)
        saved = self.session_service.save_session(state)
        self.runtime_recycler.release_session_lease(session_id)
        return saved

    def retry_rehearsal(self, session_id: str) -> SessionState:
        state = self.session_service.get_session(session_id)
        if state.run_mode == "guidance_only":
            raise ValueError("run_mode=guidance_only，不允许再练一轮。")
        self.summary_service.reset_generation(session_id)
        state.conversation = []
        state.rehearsal_context = RehearsalRuntimeContext()
        state.psychological_pattern_state = None
        if state.motivation is not None:
            primary_motive_id = state.motivation.primary_motive_id
            secondary_motive_ids = list(state.motivation.secondary_motive_ids)
            state.motivation = MotivationState(
                primary_motive_id=primary_motive_id,
                secondary_motive_ids=secondary_motive_ids,
                primary_score=50.0,
                secondary_scores={
                    motive_id: 50.0 for motive_id in secondary_motive_ids
                },
            )
        state.emotion_state = self.emotion_transition.initial_state(
            intent_id=state.intent.intent_id if state.intent else None,
            personality=state.personality,
        )
        state.user_turn_count = 0
        state.stage = "setup_ready"
        state.coach_report_id = None
        state.rehearsal_ended_at = None
        saved = self.session_service.save_session(state)
        self.runtime_recycler.release_session_lease(session_id)
        return saved

    @classmethod
    def _clean_notes(cls, runtime_note: str | None, runtime_notes: str | list[str] | None) -> list[str]:
        candidates: list[object] = []
        if runtime_note is not None:
            candidates.append(runtime_note)
        if isinstance(runtime_notes, list):
            candidates.extend(runtime_notes)
        elif runtime_notes is not None:
            candidates.append(runtime_notes)

        cleaned_notes: list[str] = []
        seen: set[str] = set()
        for value in candidates:
            cleaned = cls._clean_optional(value)
            if cleaned and cleaned not in seen:
                cleaned_notes.append(cleaned)
                seen.add(cleaned)
        return cleaned_notes

    @staticmethod
    def _clean_optional(value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

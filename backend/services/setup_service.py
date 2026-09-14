from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Executor
from dataclasses import asdict

from backend.agents.intent_performance import (
    IntentPerformanceAgent,
    PreparedIntentPerformance,
)
from backend.agents.intent_recognition import IntentRecognitionAgent
from backend.business_config.loader import get_config_loader
from backend.config.settings import Settings, get_settings
from backend.exceptions.llm_errors import (
    ModelInvocationError,
    StructuredOutputError,
    is_raceable_model_error,
    model_error_code,
)
from backend.exceptions.workflow_errors import WorkflowError
from backend.observability.metrics import elapsed_ms, log_metric, now_ms
from backend.schemas.api import IntentPerformanceDraftResponse
from backend.schemas.intent import (
    PERFORMANCE_SECTION_TITLES,
    IntentConfig,
    IntentGoalPerformanceItem,
    IntentPerformanceDraft,
    build_performance_context,
    expected_performance_goals,
    performance_section_titles,
)
from backend.schemas.locale import SessionLocale
from backend.schemas.profile import EmployeeProfile
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.simulation import BigFivePersonality, MotivationState
from backend.schemas.state import SessionState
from backend.services.emotion_transition_service import EmotionTransitionService
from backend.services.executor_utils import run_db_with_context
from backend.services.intent_eligibility import evaluate_intent_eligibility
from backend.services.model_scheduler import ModelScheduler
from backend.services.model_retry_race import (
    MODEL_RETRY_RACE_WIDTH,
    MODEL_TRANSIENT_RETRY_DELAYS_SECONDS,
    ModelRetryRaceExhausted,
    first_valid_model_result,
)
from backend.services.retrieval_service import RetrievalService
from backend.services.session_service import SessionService


IntentPerformanceEventSink = Callable[[dict[str, object]], Awaitable[None]]


def _complete_json_string_field(payload: str, key: str) -> str | None:
    encoded_key = json.dumps(key)
    key_index = payload.find(encoded_key)
    if key_index < 0:
        return None
    colon_index = payload.find(":", key_index + len(encoded_key))
    if colon_index < 0:
        return None

    value_start = colon_index + 1
    while value_start < len(payload) and payload[value_start].isspace():
        value_start += 1
    try:
        value, _ = json.JSONDecoder().raw_decode(payload[value_start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


class _IntentPerformanceStreamTracker:
    _field_names = ("goal_overview", "positive_performance", "performance_gaps")

    def __init__(
        self,
        emit: IntentPerformanceEventSink,
        section_titles: tuple[str, str, str] = PERFORMANCE_SECTION_TITLES,
    ):
        self._emit = emit
        self._section_titles = section_titles
        self._lock = asyncio.Lock()
        self._emitted: dict[int, str] = {}

    async def update(self, payload: str) -> None:
        performances = [
            _complete_json_string_field(payload, field_name)
            for field_name in self._field_names
        ]
        if not any(performances):
            return
        async with self._lock:
            for index, performance in enumerate(performances):
                if performance is None:
                    continue
                current_performance = performance.strip()
                if not current_performance:
                    continue
                if self._emitted.get(index) == current_performance:
                    continue
                self._emitted[index] = current_performance
                await self._emit(
                    {
                        "event": "section",
                        "section_index": index,
                        "item": {
                            "goal": self._section_titles[index],
                            "current_performance": current_performance,
                            "generation_reason": None,
                        },
                    }
                )

    async def reset(self) -> None:
        async with self._lock:
            if not self._emitted:
                return
            self._emitted.clear()
            await self._emit({"event": "reset"})


class SetupService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model_scheduler: ModelScheduler | None = None,
        intent_performance_agent: IntentPerformanceAgent | None = None,
        retrieval: RetrievalService | None = None,
        session_service: SessionService | None = None,
        executor: Executor | None = None,
    ):
        self.settings = settings or get_settings()
        self.session_service = session_service or SessionService()
        self.executor = executor
        self.intent_agent = IntentRecognitionAgent()
        self.intent_performance_agent = (
            intent_performance_agent or IntentPerformanceAgent()
        )
        self.loader = get_config_loader()
        self.emotion_transition = EmotionTransitionService()
        self.model_scheduler = model_scheduler or ModelScheduler(self.settings)
        self.retrieval = retrieval

    def confirm_profile(self, session_id: str, profile: EmployeeProfile) -> SessionState:
        state = self.session_service.get_session(session_id)
        existing = state.employee_profile
        profile_changed = existing != profile
        if existing and existing.supplemental_info:
            state.supplemental_info = self._merge_text(state.supplemental_info, existing.supplemental_info)
        if profile.supplemental_info:
            state.supplemental_info = self._merge_text(state.supplemental_info, profile.supplemental_info)
            profile.supplemental_info = None
        if profile_changed:
            state.psychological_pattern_state = None
            state.guidance_report_id = None
            state.coach_report_id = None
            state.rehearsal_ended_at = None
        state.employee_profile = profile
        self._backfill_profile_setup_fields(state)
        if profile.is_ready_for_setup():
            state.stage = "profile_ready"
        else:
            state.warnings.append(f"缺少必填字段: {', '.join(profile.missing_required_fields())}")
        return self.session_service.save_session(state)

    async def confirm_intent(
        self,
        session_id: str,
        intent_id: str,
        performance_items: list[IntentGoalPerformanceItem],
    ) -> SessionState:
        state = await run_db_with_context(
            getattr(self, "executor", None),
            "setup.confirm_intent.load",
            self.session_service.get_session,
            session_id,
        )
        previous_intent = state.intent
        result = await self.intent_agent.recognize(
            text=None,
            profile=state.employee_profile,
            intent_id=intent_id,
        )
        intent_config = self.loader.intents().get(result.intent_id)
        if intent_config is None:
            raise WorkflowError(f"未知沟通意图: {result.intent_id}")
        eligibility = evaluate_intent_eligibility(state.employee_profile, intent_config)
        if not eligibility.allowed:
            raise WorkflowError(
                eligibility.message or "当前员工不符合所选沟通意图的适用规则。"
            )
        result.config = intent_config
        if not performance_items:
            raise WorkflowError("当前表现不能为空。")
        actual_labels = [item.goal for item in performance_items]
        expected_labels = list(performance_section_titles(state.locale))
        if actual_labels != expected_labels:
            raise WorkflowError("当前表现必须严格使用固定的三个综合维度及其顺序。")
        result.performance_items = performance_items
        result.performance_locale = state.locale
        result.performance_context = build_performance_context(
            performance_items,
            locale=state.locale,
        )
        setup_changed = previous_intent is not None and (
            previous_intent.intent_id != result.intent_id
            or previous_intent.performance_context != result.performance_context
        )
        state.intent = result
        if setup_changed:
            state.guidance_report_id = None
            state.coach_report_id = None
            state.rehearsal_ended_at = None
            state.psychological_pattern_state = None
        self._backfill_profile_setup_fields(state)
        return await run_db_with_context(
            getattr(self, "executor", None),
            "setup.confirm_intent.save",
            self.session_service.save_session,
            state,
        )


    async def generate_intent_performance_draft(
        self,
        session_id: str,
        *,
        intent_id: str,
    ) -> IntentPerformanceDraftResponse:
        return await self._generate_intent_performance_draft(
            session_id,
            intent_id=intent_id,
            event_sink=None,
        )

    async def stream_intent_performance_draft(
        self,
        session_id: str,
        *,
        intent_id: str,
    ) -> AsyncIterator[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

        async def emit(event: dict[str, object]) -> None:
            await queue.put(event)

        async def produce() -> None:
            try:
                response = await self._generate_intent_performance_draft(
                    session_id,
                    intent_id=intent_id,
                    event_sink=emit,
                )
                await queue.put(
                    {
                        "event": "complete",
                        **response.model_dump(mode="json"),
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                detail = (
                    str(exc)
                    if isinstance(exc, WorkflowError)
                    else "当前表现初稿生成失败，请稍后重试或手动填写。"
                )
                await queue.put({"event": "error", "detail": detail})
            finally:
                await queue.put(None)

        producer = asyncio.create_task(
            produce(),
            name=f"intent-performance-stream-{session_id}",
        )
        try:
            yield {
                "event": "started",
                "intent_id": intent_id,
                "candidate_concurrency": 1,
                "retry_candidate_concurrency": MODEL_RETRY_RACE_WIDTH,
                "section_count": len(PERFORMANCE_SECTION_TITLES),
            }
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def _generate_intent_performance_draft(
        self,
        session_id: str,
        *,
        intent_id: str,
        event_sink: IntentPerformanceEventSink | None,
    ) -> IntentPerformanceDraftResponse:
        started = now_ms()
        state = await run_db_with_context(
            getattr(self, "executor", None),
            "setup.intent_performance.load",
            self.session_service.get_session,
            session_id,
        )
        if state.employee_profile is None:
            raise WorkflowError("请先确认员工信息，再生成当前表现初稿。")
        intent_config = self.loader.intents().get(intent_id)
        if intent_config is None:
            raise WorkflowError(f"未知沟通意图: {intent_id}")
        if intent_config.performance_inference is None:
            raise WorkflowError("所选沟通意图缺少当前表现反推规则。")

        eligibility = evaluate_intent_eligibility(
            state.employee_profile,
            intent_config,
        )
        model = self.settings.model_for_task("intent_performance")
        completed_model = model
        try:
            (
                retrieved_chunks,
                organization_unit_chunks,
            ) = await self._retrieve_intent_performance_chunks(
                session_id=session_id,
                profile=state.employee_profile,
                supplemental_info=state.supplemental_info,
                intent_id=intent_config.id,
                intent_name=intent_config.name,
            )
            if event_sink is not None:
                await event_sink(
                    {
                        "event": "retrieval_complete",
                        "retrieved_chunk_count": (
                            len(retrieved_chunks)
                            + len(organization_unit_chunks)
                        ),
                    }
                )

            eligibility_payload = asdict(eligibility)
            prepare_generation = getattr(
                self.intent_performance_agent,
                "prepare_generation",
                None,
            )
            prepared = (
                prepare_generation(
                    profile=state.employee_profile,
                    supplemental_info=state.supplemental_info,
                    intent=intent_config,
                    eligibility=eligibility_payload,
                    retrieved_chunks=retrieved_chunks,
                    organization_unit_chunks=organization_unit_chunks,
                    output_locale=state.locale,
                )
                if callable(prepare_generation)
                else None
            )
            tracker = (
                _IntentPerformanceStreamTracker(
                    event_sink,
                    performance_section_titles(state.locale),
                )
                if event_sink is not None
                else None
            )

            async def invoke_candidate(
                _candidate_index: int,
                *,
                retry: bool,
            ) -> IntentPerformanceDraft:
                on_content_update = (
                    tracker.update
                    if tracker is not None and not retry
                    else None
                )
                return await self._invoke_intent_performance_attempt(
                    session_id=session_id,
                    profile=state.employee_profile,
                    supplemental_info=state.supplemental_info,
                    intent=intent_config,
                    eligibility=eligibility_payload,
                    retrieved_chunks=retrieved_chunks,
                    organization_unit_chunks=organization_unit_chunks,
                    output_locale=state.locale,
                    retry=retry,
                    prepared=prepared,
                    on_content_update=on_content_update,
                )

            log_metric(
                "intent.performance_draft.model_primary",
                session_id=session_id,
                intent_id=intent_id,
                model=model,
                model_race_stage="primary",
                model_race_width=1,
                model_race_outcome="started",
            )
            try:
                output = await invoke_candidate(
                    0,
                    retry=False,
                )
                log_metric(
                    "intent.performance_draft.model_primary",
                    session_id=session_id,
                    intent_id=intent_id,
                    model=model,
                    model_race_stage="primary",
                    model_race_width=1,
                    model_race_outcome="winner",
                    model_race_winner_index=0,
                    model_race_failed_count=0,
                    model_race_canceled_count=0,
                )
            except Exception as first_error:
                if not is_raceable_model_error(first_error):
                    raise
                if tracker is not None:
                    await tracker.reset()
                configured_chat_urls = tuple(
                    getattr(self.settings, "chat_urls", ()) or ()
                )
                if not configured_chat_urls:
                    configured_chat_url = str(
                        getattr(self.settings, "chat_url", "") or ""
                    ).strip()
                    configured_chat_urls = (
                        (configured_chat_url,) if configured_chat_url else ()
                    )
                stagger_transient_retry = (
                    isinstance(first_error, ModelInvocationError)
                    and len(set(configured_chat_urls)) <= 1
                )
                retry_start_delays = (
                    MODEL_TRANSIENT_RETRY_DELAYS_SECONDS
                    if stagger_transient_retry
                    else None
                )
                log_metric(
                    "intent.performance_draft.model_retry_race",
                    session_id=session_id,
                    intent_id=intent_id,
                    model_retry_race_width=MODEL_RETRY_RACE_WIDTH,
                    model_retry_model=self.settings.model_retry_race_model,
                    model_retry_race_outcome="started",
                    model_retry_trigger=model_error_code(first_error),
                    model_retry_schedule=(
                        "staggered" if stagger_transient_retry else "parallel"
                    ),
                )
                try:
                    retry_result = await first_valid_model_result(
                        lambda candidate_index: invoke_candidate(
                            candidate_index,
                            retry=True,
                        ),
                        start_delays_seconds=retry_start_delays,
                    )
                except ModelRetryRaceExhausted as retry_race_error:
                    log_metric(
                        "intent.performance_draft.model_retry_race",
                        session_id=session_id,
                        intent_id=intent_id,
                        model_retry_race_width=MODEL_RETRY_RACE_WIDTH,
                        model_retry_model=self.settings.model_retry_race_model,
                        model_retry_race_outcome="exhausted",
                        model_retry_trigger=model_error_code(first_error),
                        model_retry_failed_count=len(retry_race_error.errors),
                        model_retry_error_codes=",".join(
                            model_error_code(error)
                            for error in retry_race_error.errors
                        ),
                    )
                    raise first_error from retry_race_error
                output = retry_result.value
                completed_model = self.settings.model_retry_race_model
                log_metric(
                    "intent.performance_draft.model_retry_race",
                    session_id=session_id,
                    intent_id=intent_id,
                    model_retry_race_width=MODEL_RETRY_RACE_WIDTH,
                    model_retry_model=self.settings.model_retry_race_model,
                    model_retry_race_outcome="winner",
                    model_retry_trigger=model_error_code(first_error),
                    model_retry_winner_index=retry_result.winner_index,
                    model_retry_failed_count=retry_result.failed_count,
                    model_retry_canceled_count=retry_result.canceled_count,
                )
        except Exception as exc:  # noqa: BLE001
            log_metric(
                "intent.performance_draft.error",
                session_id=session_id,
                intent_id=intent_id,
                model=model,
                intent_performance_total_ms=elapsed_ms(started),
                expected_goal_count=len(
                    expected_performance_goals(state.employee_profile.key_goals)
                ),
                error_code=str(getattr(exc, "code", "") or ""),
                error_type=type(exc).__name__,
                structured_error_detail=(
                    str(exc)[:500]
                    if isinstance(exc, StructuredOutputError)
                    else ""
                ),
            )
            raise WorkflowError(
                "当前表现初稿生成失败，请稍后重试或手动填写。"
            ) from exc

        log_metric(
            "intent.performance_draft.complete",
            session_id=session_id,
            intent_id=intent_id,
            model=completed_model,
            primary_model=model,
            intent_performance_total_ms=elapsed_ms(started),
            input_profile_chars=len(
                state.employee_profile.model_dump_json(exclude_none=True)
            ),
            input_supplemental_chars=len(state.supplemental_info or ""),
            retrieved_chunk_count=(
                len(retrieved_chunks) + len(organization_unit_chunks)
            ),
            organization_unit_chunk_count=len(organization_unit_chunks),
            output_chars=len(output.performance_context),
        )
        return IntentPerformanceDraftResponse(
            intent_id=intent_id,
            locale=state.locale,
            performance_context=output.performance_context,
            performance_items=output.goal_performance_items,
        )

    async def _invoke_intent_performance_attempt(
        self,
        *,
        session_id: str,
        profile: EmployeeProfile,
        supplemental_info: str | None,
        intent: IntentConfig,
        eligibility: dict[str, object],
        retrieved_chunks: list[RetrievedChunk],
        organization_unit_chunks: list[RetrievedChunk],
        output_locale: SessionLocale,
        retry: bool,
        prepared: PreparedIntentPerformance | None,
        on_content_update: Callable[[str], Awaitable[None]] | None,
    ) -> IntentPerformanceDraft:
        attempt_model = (
            self.settings.model_retry_race_model
            if retry
            else self.settings.model_for_task("intent_performance")
        )
        async with self.model_scheduler.slot(
            session_id=session_id,
            category="interactive",
            endpoint=self.settings.chat_url,
            model=attempt_model,
        ):
            generation_arguments = {
                "profile": profile,
                "supplemental_info": supplemental_info,
                "intent": intent,
                "eligibility": eligibility,
                "retrieved_chunks": retrieved_chunks,
                "organization_unit_chunks": organization_unit_chunks,
                "output_locale": output_locale,
                "retry": retry,
                "retry_model": attempt_model if retry else None,
                "prepared": prepared,
            }
            if on_content_update is not None:
                generation_arguments["on_content_update"] = on_content_update
            return await self.intent_performance_agent.generate(
                **generation_arguments,
            )

    async def _retrieve_intent_performance_chunks(
        self,
        *,
        session_id: str,
        profile: EmployeeProfile,
        supplemental_info: str | None,
        intent_id: str,
        intent_name: str,
    ) -> tuple[list[RetrievedChunk], list[RetrievedChunk]]:
        if self.retrieval is None:
            raise RuntimeError("Intent performance retrieval service is unavailable.")

        started = now_ms()
        context = {
            "profile": profile.model_dump(mode="json", exclude_none=True),
            "supplemental_info": str(supplemental_info or ""),
            "intent": {"id": intent_id, "name": intent_name},
        }
        try:
            chunks, organization_unit_chunks = await asyncio.gather(
                self.retrieval.aretrieve(
                    "intent_performance",
                    context,
                    top_k=8,
                ),
                self.retrieval.aretrieve(
                    "intent_performance_organization_unit",
                    context,
                    top_k=3,
                ),
            )
        except Exception as exc:
            log_metric(
                "intent.performance_draft.retrieval",
                session_id=session_id,
                intent_id=intent_id,
                intent_performance_retrieval_ms=elapsed_ms(started),
                retrieved_chunk_count=0,
                organization_unit_chunk_count=0,
                complete=False,
                error_type=type(exc).__name__,
            )
            raise

        log_metric(
            "intent.performance_draft.retrieval",
            session_id=session_id,
            intent_id=intent_id,
            intent_performance_retrieval_ms=elapsed_ms(started),
            retrieved_chunk_count=len(chunks) + len(organization_unit_chunks),
            organization_unit_chunk_count=len(organization_unit_chunks),
            complete=True,
        )
        return chunks, organization_unit_chunks

    def confirm_simulation(
        self,
        session_id: str,
        *,
        personality: BigFivePersonality,
        primary_motive_id: str,
        secondary_motive_ids: list[str],
        run_mode: str = "guidance_then_rehearsal",
    ) -> SessionState:
        state = self.session_service.get_session(session_id)
        self._validate_motives(primary_motive_id, secondary_motive_ids)
        previous_personality = state.personality
        previous_motivation = state.motivation
        previous_run_mode = state.run_mode
        state.psychological_pattern_state = None
        if state.personality != personality:
            state.personality_facets = None
        state.personality = personality
        state.motivation = MotivationState(
            primary_motive_id=primary_motive_id,
            secondary_motive_ids=secondary_motive_ids,
            primary_score=50.0,
            secondary_scores={motive_id: 50.0 for motive_id in secondary_motive_ids},
        )
        state.emotion_state = self.emotion_transition.initial_state(
            intent_id=state.intent.intent_id if state.intent else None,
            personality=personality,
        )
        state.run_mode = run_mode
        simulation_changed = (
            previous_personality != state.personality
            or previous_motivation is None
            or previous_motivation.primary_motive_id
            != state.motivation.primary_motive_id
            or previous_motivation.secondary_motive_ids
            != state.motivation.secondary_motive_ids
            or previous_run_mode != state.run_mode
        )
        if simulation_changed:
            state.guidance_report_id = None
            state.coach_report_id = None
            state.rehearsal_ended_at = None
        return self.session_service.save_session(state)

    def complete_setup(self, session_id: str) -> SessionState:
        state = self.session_service.get_session(session_id)
        self._backfill_profile_setup_fields(state)
        missing = []
        if not state.employee_profile or not state.employee_profile.is_ready_for_setup():
            missing.append("employee_profile_required_fields")
        if not state.intent:
            missing.append("intent")
        else:
            intent_config = self.loader.intents().get(state.intent.intent_id)
            if intent_config is None:
                raise WorkflowError(f"未知沟通意图: {state.intent.intent_id}")
            eligibility = evaluate_intent_eligibility(state.employee_profile, intent_config)
            if not eligibility.allowed:
                message = eligibility.message or "当前员工不符合所选沟通意图的适用规则。"
                if message not in state.warnings:
                    state.warnings.append(message)
                self.session_service.save_session(state)
                raise WorkflowError(message)
        if not state.personality:
            missing.append("personality")
        if not state.motivation:
            missing.append("motivation")
        if not state.emotion_state:
            missing.append("emotion_state")
        if missing:
            state.warnings.append(f"setup 未完成: {', '.join(missing)}")
            self.session_service.save_session(state)
            raise ValueError(f"setup 未完成: {', '.join(missing)}")
        state.setup_ready = True
        state.stage = "setup_ready"
        return self.session_service.save_session(state)

    def _validate_motives(self, primary_motive_id: str, secondary_motive_ids: list[str]) -> None:
        motives = self.loader.motives()
        if primary_motive_id not in motives:
            raise ValueError(f"Unknown primary_motive_id: {primary_motive_id}")
        if len(secondary_motive_ids) > 2:
            raise ValueError("secondary_motive_ids must contain at most two motives")
        if len(set(secondary_motive_ids)) != len(secondary_motive_ids):
            raise ValueError("secondary_motive_ids must not contain duplicates")
        if primary_motive_id in secondary_motive_ids:
            raise ValueError("primary motive and secondary motives must be different")
        unknown = [motive_id for motive_id in secondary_motive_ids if motive_id not in motives]
        if unknown:
            raise ValueError(f"Unknown secondary_motive_ids: {', '.join(unknown)}")

    def _backfill_profile_setup_fields(self, state: SessionState) -> None:
        profile = state.employee_profile
        if not profile:
            return
        if not profile.review_cycle:
            profile.review_cycle = "当前绩效周期"
        if not profile.conversation_topic and state.intent:
            profile.conversation_topic = self._intent_display_name(state)

    @staticmethod
    def _merge_text(existing: str | None, addition: str | None) -> str:
        existing_text = str(existing or "").strip()
        addition_text = str(addition or "").strip()
        if not existing_text:
            return addition_text
        if not addition_text or addition_text in existing_text:
            return existing_text
        if existing_text in addition_text:
            return addition_text
        return f"{existing_text}\n\n{addition_text}"

    @staticmethod
    def _intent_display_name(state: SessionState) -> str | None:
        if not state.intent:
            return None
        if state.intent.config and state.intent.config.name:
            return state.intent.config.name
        return state.intent.intent_id

    def list_options(self) -> dict:
        settings = getattr(self, "settings", None) or get_settings()
        intents = [cfg.model_dump() for cfg in self.loader.intents().values()]
        anchors = [cfg.model_dump() for cfg in self.loader.emotion_anchors().values()]
        return {
            "intents": intents,
            "default_intent": self.loader.default_intent_id(),
            "motives": [cfg.model_dump() for cfg in self.loader.motives().values()],
            "emotion_anchors": anchors,
            "default_big_five": self.loader.default_big_five().model_dump(),
            "motive_recommendations": {
                item["id"]: self.loader.motive_recommendation(item["id"])
                for item in intents
                if item.get("id")
            },
            "default_motive_recommendation": self.loader.motive_recommendation(None),
            "speech": {
                "enabled": settings.tts_enabled,
                "default_voice": settings.tts_default_voice,
                "voices": list(settings.tts_voice_options),
                "sample_rate": settings.tts_sample_rate,
            },
        }

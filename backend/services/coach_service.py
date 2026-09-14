from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import Executor
from dataclasses import dataclass
import hashlib
import json
import logging

from backend.agents.coach_agent.coach_orchestrator import COACH_TASK_SPECS, CoachOrchestrator
from backend.agents.coach_agent.dimension_evaluator import PreparedDimensionEvaluation
from backend.agents.coach_agent.dimension_evaluator import coach_task_name
from backend.business_config.loader import get_config_loader
from backend.config.settings import get_settings
from backend.exceptions.llm_errors import is_raceable_model_error, model_error_code
from backend.exceptions.workflow_errors import WorkflowError
from backend.observability.metrics import elapsed_ms, log_metric, now_ms
from backend.repositories.report_repository import ReportRepository
from backend.redis.distributed_coordination import (
    RedisDistributedCoordinator,
    get_distributed_coordinator,
)
from backend.services.agentic_evidence_service import AgenticEvidencePlanner
from backend.services.career_elements import (
    career_elements_applicable,
    current_career_elements,
)
from backend.services.executor_utils import (
    run_db_with_context,
    run_sync_with_context,
)
from backend.services.knowledge_skill_service import build_knowledge_skill_context
from backend.services.retrieval_service import RetrievalService
from backend.services.model_retry_race import (
    MODEL_RETRY_RACE_WIDTH,
    ModelRetryRaceExhausted,
    first_valid_model_result,
)
from backend.services.model_scheduler import ModelScheduler
from backend.services.model_history_fallback_service import (
    COACH_HISTORY_WARNING,
    HISTORY_FALLBACK_METADATA_KEY,
    HistoricalCoachTaskMatch,
    ModelHistoryFallbackService,
)
from backend.services.personality_behavior_service import (
    get_personality_behavior_resolver,
)
from backend.services.session_service import SessionService
from backend.schemas.coach import CoachReport
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.state import SessionState
from backend.schemas.task import CoachTaskResult
from backend.workflows.guards import ensure_current_performance_locale


logger = logging.getLogger(__name__)


_COACH_TASK_METRIC_FIELDS = {
    "opening_evaluation": "coach_opening_ms",
    "emotion_evaluation": "coach_emotion_ms",
    "output_expectations_evaluation": "coach_output_expectations_ms",
    "development_plan_evaluation": "coach_development_plan_ms",
}


def _coach_task_timing_fields(task_timings: dict[str, int] | None = None) -> dict[str, int | None]:
    task_timings = task_timings or {}
    return {field: task_timings.get(task_id) for task_id, field in _COACH_TASK_METRIC_FIELDS.items()}


@dataclass(frozen=True)
class _CoachTaskPipelineResult:
    task_id: str
    task_name: str
    result: CoachTaskResult
    error: str | None
    warning: str | None
    retrieval_ms: int
    task_ms: int
    pipeline_ms: int


class CoachService:
    executor: Executor | None = None

    def __init__(
        self,
        *,
        session_service: SessionService | None = None,
        report_repo: ReportRepository | None = None,
        orchestrator: CoachOrchestrator | None = None,
        retrieval: RetrievalService | None = None,
        model_scheduler: ModelScheduler | None = None,
        agentic_evidence: AgenticEvidencePlanner | None = None,
        history_fallback: ModelHistoryFallbackService | None = None,
        executor: Executor | None = None,
        coordinator: RedisDistributedCoordinator | None = None,
    ):
        self.session_service = session_service or SessionService()
        self.report_repo = report_repo or ReportRepository()
        self.orchestrator = orchestrator or CoachOrchestrator()
        self.retrieval = retrieval or RetrievalService()
        self.model_scheduler = model_scheduler or ModelScheduler()
        self.agentic_evidence = agentic_evidence
        self.executor = executor
        self.settings = getattr(self.retrieval, "settings", get_settings())
        self.history_fallback = history_fallback or ModelHistoryFallbackService(
            settings=self.settings,
        )
        self.config_loader = get_config_loader()
        self.coordinator = coordinator or get_distributed_coordinator(self.settings)

    async def generate(self, session_id: str) -> CoachReport:
        total_started = now_ms()
        state = await run_db_with_context(
            self.executor,
            "coach.session.read",
            self.session_service.get_session,
            session_id,
        )
        ensure_current_performance_locale(state)
        cached = await run_db_with_context(
            self.executor,
            "coach.report.read",
            self._cached_report,
            session_id,
            state,
        )
        if cached is not None:
            log_metric(
                "coach.generate",
                session_id=session_id,
                coach_cache_hit=True,
                coach_retrieval_ms=0,
                coach_report_ms=0,
                coach_total_ms=elapsed_ms(total_started),
                complete=True,
                **{field: 0 for field in _COACH_TASK_METRIC_FIELDS.values()},
            )
            return cached

        async with self.coordinator.session_lock("coach", session_id):
            state = await run_db_with_context(
                self.executor,
                "coach.session.read",
                self.session_service.get_session,
                session_id,
            )
            ensure_current_performance_locale(state)
            cached = await run_db_with_context(
                self.executor,
                "coach.report.read",
                self._cached_report,
                session_id,
                state,
            )
            if cached is not None:
                log_metric(
                    "coach.generate",
                    session_id=session_id,
                    coach_cache_hit=True,
                    coach_retrieval_ms=0,
                    coach_report_ms=0,
                    coach_total_ms=elapsed_ms(total_started),
                    complete=True,
                    **{field: 0 for field in _COACH_TASK_METRIC_FIELDS.values()},
                )
                return cached
            return await self._generate_uncached(session_id, state, total_started=total_started)

    async def stream_generate(self, session_id: str) -> AsyncIterator[dict]:
        total_started = now_ms()
        retrieval_ms: int | None = None
        report_ms: int | None = None
        task_timings: dict[str, int] = {}
        try:
            state = await run_db_with_context(
                self.executor,
                "coach.session.read",
                self.session_service.get_session,
                session_id,
            )
            ensure_current_performance_locale(state)
            cached = await run_db_with_context(
                self.executor,
                "coach.report.read",
                self._cached_report,
                session_id,
                state,
            )
            yield {"event": "start", "cached": cached is not None}
            if cached is not None:
                log_metric(
                    "coach.generate",
                    session_id=session_id,
                    stream=True,
                    coach_cache_hit=True,
                    coach_retrieval_ms=0,
                    coach_report_ms=0,
                    coach_total_ms=elapsed_ms(total_started),
                    complete=True,
                    **{field: 0 for field in _COACH_TASK_METRIC_FIELDS.values()},
                )
                yield {
                    "event": "done",
                    "complete": True,
                    "cached": True,
                    "report": cached.model_dump(mode="json"),
                    "state": state.model_dump(mode="json"),
                }
                return

            async with self.coordinator.session_lock("coach", session_id):
                state = await run_db_with_context(
                    self.executor,
                    "coach.session.read",
                    self.session_service.get_session,
                    session_id,
                )
                ensure_current_performance_locale(state)
                cached = await run_db_with_context(
                    self.executor,
                    "coach.report.read",
                    self._cached_report,
                    session_id,
                    state,
                )
                if cached is not None:
                    log_metric(
                        "coach.generate",
                        session_id=session_id,
                        stream=True,
                        coach_cache_hit=True,
                        coach_retrieval_ms=0,
                        coach_report_ms=0,
                        coach_total_ms=elapsed_ms(total_started),
                        complete=True,
                        **{field: 0 for field in _COACH_TASK_METRIC_FIELDS.values()},
                    )
                    yield {
                        "event": "done",
                        "complete": True,
                        "cached": True,
                        "report": cached.model_dump(mode="json"),
                        "state": state.model_dump(mode="json"),
                    }
                    return

                context = self._generation_context(state)
                input_fingerprint = self._input_fingerprint(state)
                checkpoint_results = await run_db_with_context(
                    self.executor,
                    "coach.checkpoint.read",
                    self._checkpoint_results,
                    session_id,
                    input_fingerprint,
                )
                task_results: list[CoachTaskResult] = list(
                    checkpoint_results.values()
                )
                task_errors: list[dict[str, str]] = []
                kb_warnings: list[str] = []
                tasks = []
                try:
                    for task_id, task_name in COACH_TASK_SPECS:
                        task_name = coach_task_name(
                            task_id,
                            state.locale,
                            task_name,
                        )
                        yield {"event": "task_start", "task_id": task_id, "task_name": task_name}
                        checkpoint = checkpoint_results.get(task_id)
                        if checkpoint is not None:
                            task_timings[task_id] = 0
                            yield {
                                "event": "task_done",
                                "task_id": task_id,
                                "task_name": task_name,
                                "result": checkpoint.model_dump(mode="json"),
                                "retrieval_ms": 0,
                                "task_ms": 0,
                                "pipeline_ms": 0,
                                "cached": True,
                            }
                            continue
                        tasks.append(
                            asyncio.create_task(
                                self._run_task_pipeline(
                                    session_id,
                                    state,
                                    context,
                                    task_id,
                                    task_name,
                                )
                            )
                        )

                    for completed in asyncio.as_completed(tasks):
                        outcome = await completed
                        await run_db_with_context(
                            self.executor,
                            "coach.checkpoint.save",
                            self._save_task_checkpoint,
                            session_id,
                            input_fingerprint,
                            outcome,
                        )
                        retrieval_ms = max(retrieval_ms or 0, outcome.retrieval_ms)
                        task_timings[outcome.task_id] = outcome.task_ms
                        log_metric(
                            "coach.task",
                            session_id=session_id,
                            task_id=outcome.task_id,
                            task_name=outcome.task_name,
                            coach_task_retrieval_ms=outcome.retrieval_ms,
                            coach_task_ms=outcome.task_ms,
                            coach_pipeline_ms=outcome.pipeline_ms,
                            complete=outcome.error is None,
                        )
                        task_results.append(outcome.result)
                        if outcome.warning is not None:
                            kb_warnings.append(outcome.warning)
                        timing_payload = {
                            "retrieval_ms": outcome.retrieval_ms,
                            "task_ms": outcome.task_ms,
                            "pipeline_ms": outcome.pipeline_ms,
                        }
                        if outcome.error is not None:
                            task_errors.append(
                                {
                                    "task_id": outcome.task_id,
                                    "task_name": outcome.task_name,
                                    "message": outcome.error,
                                }
                            )
                            yield {
                                "event": "task_error",
                                "task_id": outcome.task_id,
                                "task_name": outcome.task_name,
                                "message": outcome.error,
                                "result": outcome.result.model_dump(mode="json"),
                                **timing_payload,
                            }
                            continue
                        yield {
                            "event": "task_done",
                            "task_id": outcome.task_id,
                            "task_name": outcome.task_name,
                            "result": outcome.result.model_dump(mode="json"),
                            **timing_payload,
                        }
                finally:
                    pending_tasks = [task for task in tasks if not task.done()]
                    for task in pending_tasks:
                        task.cancel()
                    if pending_tasks:
                        await asyncio.gather(*pending_tasks, return_exceptions=True)

                report_started = now_ms()
                report = self.orchestrator.build_report(
                    state.session_id,
                    task_results,
                    locale=state.locale,
                )
                report_ms = elapsed_ms(report_started)
                complete = self._report_complete(report)
                log_metric(
                    "coach.report_assembly",
                    session_id=session_id,
                    coach_report_dimension_count=len(report.task_results),
                    coach_report_assembly_ms=report_ms,
                    complete=complete,
                )
                report = self._with_runtime_versions(report)
                saved_state = state
                if complete:
                    saved_state = await run_db_with_context(
                        self.executor,
                        "coach.report.save",
                        self._save_report_state,
                        state,
                        report,
                        kb_warnings,
                    )
                log_metric(
                    "coach.generate",
                    session_id=session_id,
                    stream=True,
                    coach_cache_hit=False,
                    coach_retrieval_ms=retrieval_ms,
                    coach_report_ms=report_ms,
                    coach_total_ms=elapsed_ms(total_started),
                    complete=complete,
                    error_count=len(task_errors),
                    **_coach_task_timing_fields(task_timings),
                )
                yield {
                    "event": "done",
                    "complete": complete,
                    "errors": task_errors,
                    "report": report.model_dump(mode="json"),
                    "state": saved_state.model_dump(mode="json"),
                }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Coach report stream failed for session_id=%s", session_id)
            log_metric(
                "coach.generate.error",
                session_id=session_id,
                stream=True,
                coach_cache_hit=False,
                coach_retrieval_ms=retrieval_ms,
                coach_report_ms=report_ms,
                coach_total_ms=elapsed_ms(total_started),
                complete=False,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
                **_coach_task_timing_fields(task_timings),
            )
            yield {"event": "error", "message": str(exc) or type(exc).__name__}

    def _cached_report(self, session_id: str, state) -> CoachReport | None:
        if state.coach_report_id:
            source_loader = getattr(self.report_repo, "get_coach_source", None)
            if callable(source_loader):
                try:
                    source = source_loader(session_id)
                except KeyError:
                    return None
                if source == "historical_fallback":
                    logger.info(
                        "Cached Coach report contains historical fallback tasks; "
                        "retrying model generation session_id=%s",
                        session_id,
                    )
                    return None
            try:
                report = self.report_repo.get_coach(session_id)
            except KeyError:
                logger.warning("Coach report id exists but report is missing; regenerating session_id=%s", session_id)
            else:
                if report.coach_version != self._runtime_coach_version():
                    logger.info("Coach dimension version changed; regenerating session_id=%s", session_id)
                    return None
                if report.locale != state.locale:
                    logger.info("Coach locale changed; regenerating session_id=%s", session_id)
                    return None
                return report if self._report_complete(report) else None
        return None

    async def _generate_uncached(self, session_id: str, state, *, total_started: float | None = None) -> CoachReport:
        total_started = total_started if total_started is not None else now_ms()
        retrieval_ms: int | None = None
        report_ms: int | None = None
        task_timings: dict[str, int] = {}
        try:
            context = self._generation_context(state)
            input_fingerprint = self._input_fingerprint(state)
            checkpoint_results = await run_db_with_context(
                self.executor,
                "coach.checkpoint.read",
                self._checkpoint_results,
                session_id,
                input_fingerprint,
            )
            pending_specs = [
                (task_id, task_name)
                for task_id, task_name in COACH_TASK_SPECS
                if task_id not in checkpoint_results
            ]
            outcomes = await asyncio.gather(
                *(
                    self._run_task_pipeline(session_id, state, context, task_id, task_name)
                    for task_id, task_name in pending_specs
                )
            )
            retrieval_ms = max((outcome.retrieval_ms for outcome in outcomes), default=0)
            task_results: list[CoachTaskResult] = list(
                checkpoint_results.values()
            )
            kb_warnings: list[str] = []
            task_errors: list[str] = []
            for outcome in outcomes:
                await run_db_with_context(
                    self.executor,
                    "coach.checkpoint.save",
                    self._save_task_checkpoint,
                    session_id,
                    input_fingerprint,
                    outcome,
                )
                task_results.append(outcome.result)
                task_timings[outcome.task_id] = outcome.task_ms
                if outcome.warning is not None:
                    kb_warnings.append(outcome.warning)
                if outcome.error is not None:
                    task_errors.append(outcome.error)
                log_metric(
                    "coach.task",
                    session_id=session_id,
                    task_id=outcome.task_id,
                    task_name=outcome.task_name,
                    coach_task_retrieval_ms=outcome.retrieval_ms,
                    coach_task_ms=outcome.task_ms,
                    coach_pipeline_ms=outcome.pipeline_ms,
                    complete=outcome.error is None,
                )

            report_started = now_ms()
            report = self.orchestrator.build_report(
                state.session_id,
                task_results,
                locale=state.locale,
            )
            report_ms = elapsed_ms(report_started)
            complete = self._report_complete(report)
            log_metric(
                "coach.report_assembly",
                session_id=session_id,
                coach_report_dimension_count=len(report.task_results),
                coach_report_assembly_ms=report_ms,
                complete=complete,
            )
            report = self._with_runtime_versions(report)
            if complete:
                await run_db_with_context(
                    self.executor,
                    "coach.report.save",
                    self._save_report_state,
                    state,
                    report,
                    kb_warnings,
                )
            log_metric(
                "coach.generate",
                session_id=session_id,
                coach_cache_hit=False,
                coach_retrieval_ms=retrieval_ms,
                coach_report_ms=report_ms,
                coach_total_ms=elapsed_ms(total_started),
                complete=complete,
                error_count=len(task_errors),
                **_coach_task_timing_fields(task_timings),
            )
            return report
        except Exception as exc:
            log_metric(
                "coach.generate.error",
                session_id=session_id,
                coach_cache_hit=False,
                coach_retrieval_ms=retrieval_ms,
                coach_report_ms=report_ms,
                coach_total_ms=elapsed_ms(total_started),
                complete=False,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
                **_coach_task_timing_fields(task_timings),
            )
            raise

    def _generation_context(self, state: SessionState) -> dict:
        ensure_current_performance_locale(state)
        if len([t for t in state.conversation if t.speaker == "manager"]) == 0:
            raise WorkflowError("缺少预演对话，无法生成 CoachReport。")
        intent_id = state.intent.intent_id if state.intent else ""
        return {
            "output_locale": state.locale,
            "intent": state.intent.config if state.intent else {},
            "profile": state.employee_profile,
            "supplemental_info": state.supplemental_info_excerpt(),
            "performance_context": (
                state.intent.performance_context if state.intent else ""
            )
            or "",
            "personality": state.personality,
            "personality_behavior_profile": (
                get_personality_behavior_resolver().resolve(
                    state.personality,
                    state.personality_facets,
                )
            ),
            "motivation": state.motivation,
            "emotion_state": state.emotion_state,
            "conversation": state.conversation,
            "run_mode": state.run_mode,
            "career_elements_applicable": career_elements_applicable(
                state.employee_profile,
                intent_id,
            ),
            "current_career_elements": current_career_elements(
                state.employee_profile
            ),
            "knowledge_skill_context": build_knowledge_skill_context(
                state,
                include_conversation=True,
            ),
        }

    def _with_runtime_versions(self, report: CoachReport) -> CoachReport:
        return report.model_copy(
            update={
                "coach_version": self._runtime_coach_version(),
            }
        )

    def _runtime_coach_version(self) -> str:
        suffix = (
            getattr(agentic_evidence, "report_version_suffix", "")
            if (
                agentic_evidence := getattr(
                    self,
                    "agentic_evidence",
                    None,
                )
            )
            is not None
            else ""
        )
        return f"{self.config_loader.coach_version()}{suffix}"

    def _retrieve_task_chunks(
        self,
        session_id: str,
        task_id: str,
        context: dict,
    ) -> tuple[str, list[RetrievedChunk], str | None]:
        try:
            chunks = self.retrieval.retrieve(task_id, context)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Coach retrieval failed for session_id=%s task_id=%s", session_id, task_id)
            return task_id, [], f"Coach KB 检索 {task_id} 失败，已基于对话和本地规则继续生成：{exc}"
        if not chunks:
            return task_id, [], f"Coach KB 未检索到 {task_id} 所需知识片段，已基于对话和本地规则继续生成。"
        return task_id, chunks, None

    async def _invoke_task_attempt(
        self,
        state: SessionState,
        chunks: list[RetrievedChunk],
        task_id: str,
        *,
        retry: bool,
        prepared: PreparedDimensionEvaluation | None = None,
    ) -> CoachTaskResult:
        attempt_model = (
            self.settings.model_retry_race_model
            if retry
            else self.settings.model_for_task("coach_evaluator")
        )
        async with self.model_scheduler.slot(
            session_id=state.session_id,
            category="coach",
            endpoint=self.settings.chat_url,
            model=attempt_model,
        ):
            arguments = {}
            if retry:
                arguments.update(
                    {
                        "retry": True,
                        "retry_model": attempt_model,
                    }
                )
            if prepared is not None:
                arguments["prepared"] = prepared
            return await self.orchestrator.run_task(
                task_id,
                state,
                chunks,
                **arguments,
            )

    async def _run_task(
        self,
        state: SessionState,
        chunks: list[RetrievedChunk],
        task_id: str,
        task_name: str,
    ) -> tuple[str, str, CoachTaskResult, str | None, int]:
        started = now_ms()
        task_name = coach_task_name(task_id, state.locale, task_name)
        try:
            prepare_task = getattr(self.orchestrator, "prepare_task", None)
            prepared = (
                prepare_task(task_id, state, chunks)
                if callable(prepare_task)
                else None
            )
            try:
                result = await self._invoke_task_attempt(
                    state,
                    chunks,
                    task_id,
                    retry=False,
                    prepared=prepared,
                )
            except Exception as first_error:
                if not is_raceable_model_error(first_error):
                    raise
                log_metric(
                    "coach.model_retry_race",
                    session_id=state.session_id,
                    coach_task_id=task_id,
                    model_retry_race_width=MODEL_RETRY_RACE_WIDTH,
                    model_retry_model=self.settings.model_retry_race_model,
                    model_retry_race_outcome="started",
                    model_retry_trigger=model_error_code(first_error),
                )
                try:
                    race_result = await first_valid_model_result(
                        lambda _candidate_index: self._invoke_task_attempt(
                            state,
                            chunks,
                            task_id,
                            retry=True,
                            prepared=prepared,
                        )
                    )
                except ModelRetryRaceExhausted as race_error:
                    log_metric(
                        "coach.model_retry_race",
                        session_id=state.session_id,
                        coach_task_id=task_id,
                        model_retry_race_width=MODEL_RETRY_RACE_WIDTH,
                        model_retry_model=self.settings.model_retry_race_model,
                        model_retry_race_outcome="exhausted",
                        model_retry_trigger=model_error_code(first_error),
                        model_retry_failed_count=len(race_error.errors),
                        model_retry_error_codes=",".join(
                            model_error_code(error) for error in race_error.errors
                        ),
                    )
                    historical_match = await self._historical_coach_task_match(
                        state,
                        task_id,
                    )
                    if historical_match is None:
                        raise first_error from race_error
                    result = historical_match.result.model_copy(
                        update={
                            "extra": {
                                **historical_match.result.extra,
                                HISTORY_FALLBACK_METADATA_KEY: {
                                    "artifact": "coach_task",
                                    "score": round(historical_match.score, 4),
                                    "conversation_similarity": round(
                                        historical_match.conversation_similarity,
                                        4,
                                    ),
                                },
                            }
                        }
                    )
                    if not self._task_result_complete(result):
                        # Historical reports may predate the current business
                        # contract. Never turn an invalid legacy task into a
                        # successful response after the live-model retries fail.
                        raise first_error from race_error
                    if COACH_HISTORY_WARNING not in state.warnings:
                        state.warnings.append(COACH_HISTORY_WARNING)
                    log_metric(
                        "coach.history_fallback",
                        session_id=state.session_id,
                        coach_task_id=task_id,
                        source_session_id=historical_match.source_session_id,
                        history_fallback_score=round(historical_match.score, 4),
                        history_fallback_conversation_similarity=round(
                            historical_match.conversation_similarity,
                            4,
                        ),
                        complete=True,
                    )
                    return task_id, task_name, result, None, elapsed_ms(started)
                result = race_result.value
                log_metric(
                    "coach.model_retry_race",
                    session_id=state.session_id,
                    coach_task_id=task_id,
                    model_retry_race_width=MODEL_RETRY_RACE_WIDTH,
                    model_retry_model=self.settings.model_retry_race_model,
                    model_retry_race_outcome="winner",
                    model_retry_trigger=model_error_code(first_error),
                    model_retry_winner_index=race_result.winner_index,
                    model_retry_failed_count=race_result.failed_count,
                    model_retry_canceled_count=race_result.canceled_count,
                )
            if not self._task_result_complete(result):
                message = self._brief_error(
                    RuntimeError(result.summary or f"Coach task status={result.status}")
                )
                return task_id, task_name, result, message, elapsed_ms(started)
            return task_id, task_name, result, None, elapsed_ms(started)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Coach task failed: session_id=%s task_id=%s", state.session_id, task_id)
            message = self._brief_error(exc)
            result = self.orchestrator.failed_task_result(
                task_id,
                task_name,
                message,
                locale=state.locale,
            )
            return task_id, task_name, result, message, elapsed_ms(started)

    async def _run_task_pipeline(
        self,
        session_id: str,
        state: SessionState,
        context: dict,
        task_id: str,
        task_name: str,
    ) -> _CoachTaskPipelineResult:
        pipeline_started = now_ms()
        retrieval_started = now_ms()
        if hasattr(self.retrieval, "aretrieve"):
            _, chunks, warning = await self._aretrieve_task_chunks(
                session_id,
                task_id,
                context,
            )
        else:
            _, chunks, warning = await run_sync_with_context(
                getattr(self.retrieval, "_executor", None),
                self._retrieve_task_chunks,
                session_id,
                task_id,
                context,
            )
        agentic_evidence = getattr(self, "agentic_evidence", None)
        if agentic_evidence is not None:
            chunks = await agentic_evidence.enhance(
                flow="coach",
                session_id=session_id,
                dimension=task_id,
                retrieval_name=task_id,
                objective=task_name,
                context=context,
                initial_chunks=chunks,
            )
        retrieval_ms = elapsed_ms(retrieval_started)
        task_id, task_name, result, error, task_ms = await self._run_task(
            state,
            chunks,
            task_id,
            task_name,
        )
        return _CoachTaskPipelineResult(
            task_id=task_id,
            task_name=task_name,
            result=result,
            error=error,
            warning=warning,
            retrieval_ms=retrieval_ms,
            task_ms=task_ms,
            pipeline_ms=elapsed_ms(pipeline_started),
        )

    async def _aretrieve_task_chunks(
        self,
        session_id: str,
        task_id: str,
        context: dict,
    ) -> tuple[str, list[RetrievedChunk], str | None]:
        try:
            chunks = await self.retrieval.aretrieve(task_id, context)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Coach retrieval failed for session_id=%s task_id=%s",
                session_id,
                task_id,
            )
            return (
                task_id,
                [],
                f"Coach KB 检索 {task_id} 失败，已基于对话和本地规则继续生成：{exc}",
            )
        if not chunks:
            return (
                task_id,
                [],
                f"Coach KB 未检索到 {task_id} 所需知识片段，已基于对话和本地规则继续生成。",
            )
        return task_id, chunks, None

    def _input_fingerprint(self, state: SessionState) -> str:
        payload = {
            "output_locale": state.locale,
            "coach_version": self._runtime_coach_version(),
            "kb_index_version": self.settings.kb_index_version,
            "coach_model": self.settings.coach_evaluator_model,
            "intent": state.intent,
            "profile": state.employee_profile,
            "supplemental_info": state.supplemental_info,
            "personality": state.personality,
            "personality_facets": state.personality_facets,
            "personality_behavior_map": (
                self.config_loader.personality_behavior_map_hash()
            ),
            "motivation": state.motivation,
            "emotion_state": state.emotion_state,
            "conversation": state.conversation,
            "run_mode": state.run_mode,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: (
                value.model_dump(mode="json")
                if hasattr(value, "model_dump")
                else str(value)
            ),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _checkpoint_results(
        self,
        session_id: str,
        input_fingerprint: str,
    ) -> dict[str, CoachTaskResult]:
        loader = getattr(self.report_repo, "get_coach_task_results", None)
        if loader is None:
            return {}
        results = loader(session_id, input_fingerprint)
        return {
            task_id: result
            for task_id, result in results.items()
            if self._task_result_complete(result)
            and not result.extra.get(HISTORY_FALLBACK_METADATA_KEY)
        }

    def _save_task_checkpoint(
        self,
        session_id: str,
        input_fingerprint: str,
        outcome: _CoachTaskPipelineResult,
    ) -> None:
        saver = getattr(self.report_repo, "save_coach_task_result", None)
        if saver is None:
            return
        saver(
            session_id=session_id,
            input_fingerprint=input_fingerprint,
            result=outcome.result,
            warning=outcome.warning,
            error=outcome.error,
            retrieval_ms=outcome.retrieval_ms,
            task_ms=outcome.task_ms,
            pipeline_ms=outcome.pipeline_ms,
        )

    @staticmethod
    def _task_result_complete(result: CoachTaskResult) -> bool:
        if result.status == "insufficient_information":
            return True
        if result.status != "success":
            return False
        if result.score not in {1, 2, 3, 4, 5}:
            return False
        # Scores 1-3 describe an observable deficiency. Treat a persisted
        # result without its Manager-backed issue as incomplete so an older or
        # race-winning invalid checkpoint cannot remain cached indefinitely.
        return not (
            result.score in {1, 2, 3}
            and not result.improvement_points
        )

    @classmethod
    def _report_complete(cls, report: CoachReport) -> bool:
        expected = {task_id for task_id, _ in COACH_TASK_SPECS}
        results = {result.task_id: result for result in report.task_results}
        return (
            set(results) == expected
            and all(cls._task_result_complete(results[task_id]) for task_id in expected)
        )

    def _save_report_state(self, state: SessionState, report: CoachReport, kb_warnings: list[str] | None = None) -> SessionState:
        for warning in kb_warnings or []:
            if warning not in state.warnings:
                state.warnings.append(warning)
        state.coach_report_id = state.session_id
        state.stage = "report_ready"
        source = (
            "historical_fallback"
            if any(
                bool(result.extra.get(HISTORY_FALLBACK_METADATA_KEY))
                for result in report.task_results
            )
            else "generation"
        )
        atomic_saver = getattr(self.report_repo, "save_coach_with_state", None)
        if callable(atomic_saver):
            if source == "generation":
                return atomic_saver(report, state)
            return atomic_saver(report, state, source=source)
        if source == "generation":
            self.report_repo.save_coach(report)
        else:
            self.report_repo.save_coach(report, source=source)
        return self.session_service.save_session(state)

    async def _historical_coach_task_match(
        self,
        state: SessionState,
        task_id: str,
    ) -> HistoricalCoachTaskMatch | None:
        history_fallback = getattr(self, "history_fallback", None)
        if history_fallback is None or not history_fallback.enabled:
            return None
        try:
            return await run_db_with_context(
                self.executor,
                "coach.history_fallback.read",
                history_fallback.find_coach_task,
                state,
                task_id=task_id,
                coach_version=self._runtime_coach_version(),
            )
        except Exception:
            logger.exception(
                "Coach history fallback lookup failed: session_id=%s task_id=%s",
                state.session_id,
                task_id,
            )
            return None

    @staticmethod
    def _brief_error(exc: Exception, limit: int = 320) -> str:
        message = str(exc).strip() or type(exc).__name__
        return message[:limit]

    def get(self, session_id: str) -> CoachReport:
        state = self.session_service.get_session(session_id)
        report = self.report_repo.get_coach(session_id)
        if (
            report.coach_version != self._runtime_coach_version()
            or report.locale != state.locale
            or not self._report_complete(report)
        ):
            # The stored artifact is not a usable report for the current
            # runtime. Keep GET read-only and let the POST generation path
            # rebuild it with the current prompt and validation contract.
            raise KeyError(f"Current Coach report is unavailable: {session_id}")
        return report

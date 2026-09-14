from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from concurrent.futures import Executor
from dataclasses import dataclass
import logging

from pydantic import ValidationError

from backend.agents.guidance_agent import (
    GUIDANCE_DIMENSION_SPECS,
    GUIDANCE_INTERNAL_RESULT_KEYS,
    GUIDANCE_REPORT_VERSION,
    GUIDANCE_RESULT_KEYS,
    GUIDANCE_SECTION_KEYS,
    GUIDANCE_SECTION_TITLES,
    GuidanceAgent,
    GuidanceDimensionSpec,
    PreparedGuidanceDimension,
    GuidanceResultKey,
    GuidanceResultValue,
    GuidanceSectionValue,
    guidance_motivation_context,
    guidance_section_titles,
    guidance_supplemental_info,
)
from backend.business_config.loader import get_config_loader
from backend.exceptions.llm_errors import is_raceable_model_error, model_error_code
from backend.exceptions.workflow_errors import SetupNotReadyError
from backend.observability.metrics import elapsed_ms, log_metric, now_ms
from backend.repositories.report_repository import ReportRepository
from backend.redis.distributed_coordination import (
    RedisDistributedCoordinator,
    get_distributed_coordinator,
)
from backend.services.executor_utils import (
    run_db_with_context,
    run_sync_with_context,
)
from backend.services.agentic_evidence_service import AgenticEvidencePlanner
from backend.services.career_elements import (
    career_elements_applicable,
    current_career_elements,
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
    GUIDANCE_HISTORY_WARNING,
    HistoricalGuidanceMatch,
    ModelHistoryFallbackService,
)
from backend.services.personality_facet_service import PersonalityFacetService
from backend.services.session_service import SessionService
from backend.schemas.guidance import GuidanceReport, guidance_quality_issues
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.state import SessionState
from backend.workflows.guards import ensure_current_performance_locale


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _GuidanceDimensionRun:
    spec: GuidanceDimensionSpec
    sections: dict[GuidanceResultKey, GuidanceResultValue]
    chunks: list[RetrievedChunk]
    retrieval_ms: int
    llm_ms: int
    historical_match: HistoricalGuidanceMatch | None = None


class GuidanceService:
    executor: Executor | None = None

    def __init__(
        self,
        *,
        session_service: SessionService | None = None,
        report_repo: ReportRepository | None = None,
        retrieval: RetrievalService | None = None,
        agent: GuidanceAgent | None = None,
        model_scheduler: ModelScheduler | None = None,
        agentic_evidence: AgenticEvidencePlanner | None = None,
        personality_facets: PersonalityFacetService | None = None,
        history_fallback: ModelHistoryFallbackService | None = None,
        executor: Executor | None = None,
        coordinator: RedisDistributedCoordinator | None = None,
    ):
        self.session_service = session_service or SessionService()
        self.report_repo = report_repo or ReportRepository()
        self.retrieval = retrieval or RetrievalService()
        self.settings = self.retrieval.settings
        self.agent = agent or GuidanceAgent()
        self.model_scheduler = model_scheduler or ModelScheduler(self.settings)
        self.agentic_evidence = agentic_evidence
        self.executor = executor
        self.personality_facets = personality_facets or PersonalityFacetService(
            settings=self.settings,
            model_scheduler=self.model_scheduler,
            executor=executor,
        )
        self.history_fallback = history_fallback or ModelHistoryFallbackService(
            settings=self.settings,
        )
        self.config_loader = get_config_loader()
        self.coordinator = coordinator or get_distributed_coordinator(self.settings)

    @asynccontextmanager
    async def _session_generation_lock(
        self,
        session_id: str,
    ) -> AsyncIterator[None]:
        coordinator = getattr(self, "coordinator", None)
        if coordinator is None:
            yield
            return
        async with coordinator.session_lock("guidance", session_id):
            yield

    def _start_personality_facets(
        self,
        state: SessionState,
    ) -> asyncio.Task | None:
        if not state.setup_ready or state.personality is None:
            return None
        task = asyncio.create_task(
            self.personality_facets.ensure_for_state(state, allow_model=True),
            name=f"guidance-personality-facets-{state.session_id}",
        )
        task.add_done_callback(self._observe_background_task)
        return task

    @staticmethod
    def _observe_background_task(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:  # noqa: BLE001
            return

    @staticmethod
    async def _finish_personality_facets(
        state: SessionState,
        task: asyncio.Task | None,
    ) -> None:
        if task is None:
            return
        state.personality_facets = await asyncio.shield(task)

    async def _invoke_dimension_attempt(
        self,
        session_id: str,
        state: SessionState,
        chunks: list[RetrievedChunk],
        spec: GuidanceDimensionSpec,
        *,
        retry: bool,
        prepared: PreparedGuidanceDimension | None = None,
    ) -> dict[GuidanceResultKey, GuidanceResultValue]:
        attempt_model = (
            self.settings.model_retry_race_model
            if retry
            else self.settings.model_for_task("guidance")
        )
        async with self.model_scheduler.slot(
            session_id=session_id,
            category="guidance",
            endpoint=self.settings.chat_url,
            model=attempt_model,
        ):
            if retry:
                arguments = {
                    "retry": True,
                    "retry_model": attempt_model,
                }
            else:
                arguments = {}
            if prepared is not None:
                arguments["prepared"] = prepared
            return await self.agent.generate_dimension(
                state,
                chunks,
                spec,
                **arguments,
            )

    async def _generate_dimension(
        self,
        session_id: str,
        state: SessionState,
        chunks: list[RetrievedChunk],
        spec: GuidanceDimensionSpec,
    ) -> tuple[
        GuidanceDimensionSpec,
        dict[GuidanceResultKey, GuidanceResultValue],
        int,
        HistoricalGuidanceMatch | None,
    ]:
        started = now_ms()
        historical_match: HistoricalGuidanceMatch | None = None
        try:
            prepare_dimension = getattr(self.agent, "prepare_dimension", None)
            prepared = (
                prepare_dimension(state, chunks, spec)
                if callable(prepare_dimension)
                else None
            )
            try:
                sections = await self._invoke_dimension_attempt(
                    session_id,
                    state,
                    chunks,
                    spec,
                    retry=False,
                    prepared=prepared,
                )
            except Exception as first_error:
                if not is_raceable_model_error(first_error):
                    raise
                log_metric(
                    "guidance.model_retry_race",
                    session_id=session_id,
                    guidance_dimension=spec.key,
                    model_retry_race_width=MODEL_RETRY_RACE_WIDTH,
                    model_retry_model=self.settings.model_retry_race_model,
                    model_retry_race_outcome="started",
                    model_retry_trigger=model_error_code(first_error),
                )
                try:
                    race_result = await first_valid_model_result(
                        lambda _candidate_index: self._invoke_dimension_attempt(
                            session_id,
                            state,
                            chunks,
                            spec,
                            retry=True,
                            prepared=prepared,
                        )
                    )
                except ModelRetryRaceExhausted as race_error:
                    log_metric(
                        "guidance.model_retry_race",
                        session_id=session_id,
                        guidance_dimension=spec.key,
                        model_retry_race_width=MODEL_RETRY_RACE_WIDTH,
                        model_retry_model=self.settings.model_retry_race_model,
                        model_retry_race_outcome="exhausted",
                        model_retry_trigger=model_error_code(first_error),
                        model_retry_failed_count=len(race_error.errors),
                        model_retry_error_codes=",".join(
                            model_error_code(error) for error in race_error.errors
                        ),
                    )
                    historical_match = await self._historical_guidance_match(
                        state,
                        dimension_key=spec.key,
                    )
                    sections = (
                        self._guidance_dimension_sections(
                            historical_match.report,
                            spec,
                            state,
                        )
                        if historical_match is not None
                        else None
                    )
                    if historical_match is None or sections is None:
                        raise first_error from race_error
                    self._append_warning(state, GUIDANCE_HISTORY_WARNING)
                    log_metric(
                        "guidance.history_fallback",
                        session_id=session_id,
                        guidance_dimension=spec.key,
                        source_session_id=historical_match.source_session_id,
                        history_fallback_score=round(historical_match.score, 4),
                        history_fallback_personality_similarity=round(
                            historical_match.personality_similarity,
                            4,
                        ),
                        history_fallback_motive_similarity=round(
                            historical_match.motive_similarity,
                            4,
                        ),
                        history_fallback_context_similarity=round(
                            historical_match.context_similarity,
                            4,
                        ),
                        complete=True,
                    )
                else:
                    sections = race_result.value
                    log_metric(
                        "guidance.model_retry_race",
                        session_id=session_id,
                        guidance_dimension=spec.key,
                        model_retry_race_width=MODEL_RETRY_RACE_WIDTH,
                        model_retry_model=self.settings.model_retry_race_model,
                        model_retry_race_outcome="winner",
                        model_retry_trigger=model_error_code(first_error),
                        model_retry_winner_index=race_result.winner_index,
                        model_retry_failed_count=race_result.failed_count,
                        model_retry_canceled_count=race_result.canceled_count,
                    )
        except Exception as exc:
            log_metric(
                "guidance.dimension",
                session_id=session_id,
                guidance_dimension=spec.key,
                guidance_dimension_ms=elapsed_ms(started),
                guidance_section_keys=list(spec.section_keys),
                complete=False,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            raise

        dimension_ms = elapsed_ms(started)
        log_metric(
            "guidance.dimension",
            session_id=session_id,
            guidance_dimension=spec.key,
            guidance_dimension_ms=dimension_ms,
            guidance_section_keys=list(spec.section_keys),
            historical_fallback=historical_match is not None,
            complete=True,
        )
        return spec, sections, dimension_ms, historical_match

    async def _run_dimension_pipeline(
        self,
        session_id: str,
        state: SessionState,
        spec: GuidanceDimensionSpec,
        facet_task: asyncio.Task | None = None,
    ) -> _GuidanceDimensionRun:
        retrieval_started = now_ms()
        retrieval_context = self._retrieval_context(state)
        chunks = await self._retrieve_dimension_chunks(
            state,
            spec,
            context=retrieval_context,
        )
        agentic_evidence = getattr(self, "agentic_evidence", None)
        if agentic_evidence is not None:
            chunks = await agentic_evidence.enhance(
                flow="guidance",
                session_id=session_id,
                dimension=spec.key,
                retrieval_name=spec.retrieval_name,
                objective="；".join(
                    GUIDANCE_SECTION_TITLES[key]
                    for key in spec.section_keys
                ),
                context=retrieval_context,
                initial_chunks=chunks,
            )
        retrieval_ms = elapsed_ms(retrieval_started)
        await self._finish_personality_facets(state, facet_task)
        _spec, sections, llm_ms, historical_match = await self._generate_dimension(
            session_id,
            state,
            chunks,
            spec,
        )
        return _GuidanceDimensionRun(
            spec=spec,
            sections=sections,
            chunks=chunks,
            retrieval_ms=retrieval_ms,
            llm_ms=llm_ms,
            historical_match=historical_match,
        )

    async def _generate_sections_parallel(
        self,
        session_id: str,
        state: SessionState,
        facet_task: asyncio.Task | None = None,
    ) -> tuple[
        dict[GuidanceResultKey, GuidanceResultValue],
        list[RetrievedChunk],
        int,
        int,
        list[str],
    ]:
        tasks = [
            asyncio.create_task(
                self._run_dimension_pipeline(
                    session_id,
                    state,
                    spec,
                    facet_task,
                ),
                name=f"guidance-service-{spec.key}",
            )
            for spec in GUIDANCE_DIMENSION_SPECS
        ]
        try:
            dimension_runs = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        sections: dict[GuidanceResultKey, GuidanceResultValue] = {}
        for run in dimension_runs:
            sections.update(run.sections)
        chunks = self._merge_chunks(*(run.chunks for run in dimension_runs))
        retrieval_ms = max((run.retrieval_ms for run in dimension_runs), default=0)
        llm_ms = max((run.llm_ms for run in dimension_runs), default=0)
        ordered_sections = {key: sections[key] for key in GUIDANCE_RESULT_KEYS}
        ordered_sections.update(
            {
                key: sections[key]
                for key in GUIDANCE_INTERNAL_RESULT_KEYS
                if key in sections
            }
        )
        fallback_dimensions = [
            run.spec.key
            for run in dimension_runs
            if run.historical_match is not None
        ]
        return (
            ordered_sections,
            chunks,
            retrieval_ms,
            llm_ms,
            fallback_dimensions,
        )

    async def generate(self, session_id: str) -> GuidanceReport:
        total_started = now_ms()
        retrieval_ms: int | None = None
        llm_ms: int | None = None
        facet_task: asyncio.Task | None = None
        try:
            state = await run_db_with_context(
                self.executor,
                "guidance.session.read",
                self.session_service.get_session,
                session_id,
            )
            ensure_current_performance_locale(state)
            facet_task = self._start_personality_facets(state)
            cached = (
                await run_db_with_context(
                    self.executor,
                    "guidance.report.read",
                    self._cached_report,
                    session_id,
                    state.locale,
                )
                if state.guidance_report_id
                else None
            )
            if cached is not None:
                await self._finish_personality_facets(state, facet_task)
                await run_db_with_context(
                    self.executor,
                    "guidance.session.mark_report",
                    self._mark_report_state,
                    state,
                )
                log_metric(
                    "guidance.generate",
                    session_id=session_id,
                    guidance_cache_hit=True,
                    guidance_retrieval_ms=0,
                    guidance_llm_ms=0,
                    guidance_total_ms=elapsed_ms(total_started),
                    complete=True,
                )
                return cached

            async with self._session_generation_lock(session_id):
                state = await run_db_with_context(
                    self.executor,
                    "guidance.session.read",
                    self.session_service.get_session,
                    session_id,
                )
                ensure_current_performance_locale(state)
                if facet_task is None:
                    facet_task = self._start_personality_facets(state)
                cached = (
                    await run_db_with_context(
                        self.executor,
                        "guidance.report.read",
                        self._cached_report,
                        session_id,
                        state.locale,
                    )
                    if state.guidance_report_id
                    else None
                )
                if cached is not None:
                    await self._finish_personality_facets(state, facet_task)
                    await run_db_with_context(
                        self.executor,
                        "guidance.session.mark_report",
                        self._mark_report_state,
                        state,
                    )
                    log_metric(
                        "guidance.generate",
                        session_id=session_id,
                        guidance_cache_hit=True,
                        guidance_retrieval_ms=0,
                        guidance_llm_ms=0,
                        guidance_total_ms=elapsed_ms(total_started),
                        complete=True,
                    )
                    return cached

                if not state.setup_ready:
                    raise SetupNotReadyError("请先完成员工信息、面谈目的、人格与诉求设置。")
                (
                    sections,
                    chunks,
                    retrieval_ms,
                    llm_ms,
                    fallback_dimensions,
                ) = await self._generate_sections_parallel(
                    session_id,
                    state,
                    facet_task,
                )
                await self._finish_personality_facets(state, facet_task)
                report = self._with_runtime_versions(
                    self.agent.report_from_sections(state, chunks, sections)
                )
                report_source = (
                    "historical_fallback"
                    if fallback_dimensions
                    else "generation"
                )
                if report_source == "generation":
                    await run_db_with_context(
                        self.executor,
                        "guidance.report.save",
                        self._save_report_state,
                        state,
                        report,
                    )
                else:
                    await run_db_with_context(
                        self.executor,
                        "guidance.report.save_history_fallback",
                        self._save_report_state,
                        state,
                        report,
                        report_source,
                    )
                log_metric(
                    "guidance.generate",
                    session_id=session_id,
                    guidance_cache_hit=False,
                    guidance_retrieval_ms=retrieval_ms,
                    guidance_llm_ms=llm_ms,
                    guidance_total_ms=elapsed_ms(total_started),
                    guidance_model_call_count=len(GUIDANCE_DIMENSION_SPECS),
                    historical_fallback_dimensions=fallback_dimensions,
                    complete=True,
                )
                return report
        except Exception as exc:
            log_metric(
                "guidance.generate.error",
                session_id=session_id,
                guidance_cache_hit=False,
                guidance_retrieval_ms=retrieval_ms,
                guidance_llm_ms=llm_ms,
                guidance_total_ms=elapsed_ms(total_started),
                complete=False,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            raise

    async def stream_generate(self, session_id: str) -> AsyncIterator[dict]:
        total_started = now_ms()
        retrieval_ms: int | None = None
        llm_ms: int | None = None
        generation_lock: AbstractAsyncContextManager[None] | None = None
        lock_acquired = False
        facet_task: asyncio.Task | None = None
        try:
            state = await run_db_with_context(
                self.executor,
                "guidance.session.read",
                self.session_service.get_session,
                session_id,
            )
            ensure_current_performance_locale(state)
            facet_task = self._start_personality_facets(state)
            cached = (
                await run_db_with_context(
                    self.executor,
                    "guidance.report.read",
                    self._cached_report,
                    session_id,
                    state.locale,
                )
                if state.guidance_report_id
                else None
            )
            yield {"event": "start", "cached": cached is not None}
            if cached is not None:
                for key, title, value in self._stream_sections(cached):
                    yield {"event": "section_start", "key": key, "title": title, "cached": True}
                    if isinstance(value, str):
                        for delta in self._chunk_text(value):
                            yield {"event": "delta", "key": key, "text": delta, "cached": True}
                    event = {
                        "event": "section_done",
                        "key": key,
                        "title": title,
                        "value": value,
                        "cached": True,
                    }
                    point_groups = self._point_groups_for_section(cached, key)
                    if point_groups is not None:
                        event["point_groups"] = point_groups
                    yield event
                await self._finish_personality_facets(state, facet_task)
                saved_state = await run_db_with_context(
                    self.executor,
                    "guidance.session.mark_report",
                    self._mark_report_state,
                    state,
                )
                log_metric(
                    "guidance.generate",
                    session_id=session_id,
                    stream=True,
                    guidance_cache_hit=True,
                    guidance_retrieval_ms=0,
                    guidance_llm_ms=0,
                    guidance_total_ms=elapsed_ms(total_started),
                    complete=True,
                )
                yield {
                    "event": "done",
                    "complete": True,
                    "cached": True,
                    "report": cached.model_dump(mode="json"),
                    "state": saved_state.model_dump(mode="json"),
                }
                return

            if not state.setup_ready:
                raise SetupNotReadyError("请先完成员工信息、面谈目的、人格与诉求设置。")
            localized_titles = guidance_section_titles(state.locale)
            for key in GUIDANCE_SECTION_KEYS:
                yield {"event": "section_start", "key": key, "title": localized_titles[key]}

            generation_lock = self._session_generation_lock(session_id)
            await generation_lock.__aenter__()
            lock_acquired = True

            state = await run_db_with_context(
                self.executor,
                "guidance.session.read",
                self.session_service.get_session,
                session_id,
            )
            ensure_current_performance_locale(state)
            localized_titles = guidance_section_titles(state.locale)
            if facet_task is None:
                facet_task = self._start_personality_facets(state)
            cached = (
                await run_db_with_context(
                    self.executor,
                    "guidance.report.read",
                    self._cached_report,
                    session_id,
                    state.locale,
                )
                if state.guidance_report_id
                else None
            )
            if cached is not None:
                retrieval_ms = 0
                for key, title, value in self._stream_sections(cached):
                    if isinstance(value, str):
                        for delta in self._chunk_text(value):
                            yield {"event": "delta", "key": key, "text": delta, "cached": True}
                    event = {
                        "event": "section_done",
                        "key": key,
                        "title": title,
                        "value": value,
                        "cached": True,
                    }
                    point_groups = self._point_groups_for_section(cached, key)
                    if point_groups is not None:
                        event["point_groups"] = point_groups
                    yield event
                await self._finish_personality_facets(state, facet_task)
                saved_state = await run_db_with_context(
                    self.executor,
                    "guidance.session.mark_report",
                    self._mark_report_state,
                    state,
                )
                log_metric(
                    "guidance.generate",
                    session_id=session_id,
                    stream=True,
                    guidance_cache_hit=True,
                    guidance_retrieval_ms=retrieval_ms,
                    guidance_llm_ms=0,
                    guidance_total_ms=elapsed_ms(total_started),
                    complete=True,
                )
                yield {
                    "event": "done",
                    "complete": True,
                    "cached": True,
                    "report": cached.model_dump(mode="json"),
                    "state": saved_state.model_dump(mode="json"),
                }
                return

            pipeline_started = now_ms()
            results: dict[GuidanceResultKey, GuidanceResultValue] = {}
            errors: list[dict[str, str]] = []
            failed_dimensions: set[str] = set()
            completed_runs: list[_GuidanceDimensionRun] = []

            async def capture_dimension(spec: GuidanceDimensionSpec):
                try:
                    run = await self._run_dimension_pipeline(
                        session_id,
                        state,
                        spec,
                        facet_task,
                    )
                    return spec, run, None
                except Exception as exc:  # noqa: BLE001
                    return spec, None, exc

            dimension_tasks = [
                asyncio.create_task(
                    capture_dimension(spec),
                    name=f"guidance-stream-{spec.key}",
                )
                for spec in GUIDANCE_DIMENSION_SPECS
            ]
            try:
                for completed in asyncio.as_completed(dimension_tasks):
                    spec, run, error = await completed
                    if error is not None:
                        failed_dimensions.add(spec.key)
                        message = str(error) or type(error).__name__
                        logger.error(
                            "Guidance dimension failed for session_id=%s dimension=%s: %s",
                            session_id,
                            spec.key,
                            message,
                        )
                        for key in spec.section_keys:
                            item = {
                                "key": key,
                                "title": localized_titles[key],
                                "message": message,
                            }
                            errors.append(item)
                            yield {"event": "section_error", **item}
                        continue

                    assert run is not None
                    completed_runs.append(run)
                    dimension_sections = run.sections
                    results.update(dimension_sections)
                    point_groups = dimension_sections[spec.point_group_key]
                    for key in spec.section_keys:
                        title = localized_titles[key]
                        value = dimension_sections[key]
                        if isinstance(value, str):
                            for delta in self._chunk_text(value):
                                yield {"event": "delta", "key": key, "text": delta}
                        yield {
                            "event": "section_done",
                            "key": key,
                            "title": title,
                            "value": value,
                            "point_groups": point_groups,
                            "historical_fallback": (
                                run.historical_match is not None
                            ),
                        }
            finally:
                for task in dimension_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*dimension_tasks, return_exceptions=True)

            pipeline_ms = elapsed_ms(pipeline_started)
            retrieval_ms = max(
                (run.retrieval_ms for run in completed_runs),
                default=0,
            )
            llm_ms = max((run.llm_ms for run in completed_runs), default=0)
            all_chunks = self._merge_chunks(*(run.chunks for run in completed_runs))
            log_metric(
                "guidance.section_batch",
                session_id=session_id,
                guidance_dimension_count=len(GUIDANCE_DIMENSION_SPECS),
                guidance_section_count=len(results),
                guidance_section_batch_ms=pipeline_ms,
                guidance_model_call_count=len(GUIDANCE_DIMENSION_SPECS),
                successful_dimension_count=(
                    len(GUIDANCE_DIMENSION_SPECS) - len(failed_dimensions)
                ),
                failed_dimension_count=len(failed_dimensions),
                complete=not errors,
            )
            if errors:
                log_metric(
                    "guidance.generate",
                    session_id=session_id,
                    stream=True,
                    guidance_cache_hit=False,
                    guidance_retrieval_ms=retrieval_ms,
                    guidance_llm_ms=llm_ms,
                    guidance_total_ms=elapsed_ms(total_started),
                    guidance_model_call_count=len(GUIDANCE_DIMENSION_SPECS),
                    complete=False,
                    error_count=len(errors),
                )
                yield {"event": "done", "complete": False, "errors": errors}
                return

            await self._finish_personality_facets(state, facet_task)
            report = self._with_runtime_versions(
                self.agent.report_from_sections(state, all_chunks, results)
            )
            fallback_dimensions = [
                run.spec.key
                for run in completed_runs
                if run.historical_match is not None
            ]
            report_source = (
                "historical_fallback"
                if fallback_dimensions
                else "generation"
            )
            if report_source == "generation":
                saved_state = await run_db_with_context(
                    self.executor,
                    "guidance.report.save",
                    self._save_report_state,
                    state,
                    report,
                )
            else:
                saved_state = await run_db_with_context(
                    self.executor,
                    "guidance.report.save_history_fallback",
                    self._save_report_state,
                    state,
                    report,
                    report_source,
                )
            log_metric(
                "guidance.generate",
                session_id=session_id,
                stream=True,
                guidance_cache_hit=False,
                guidance_retrieval_ms=retrieval_ms,
                guidance_llm_ms=llm_ms,
                guidance_total_ms=elapsed_ms(total_started),
                guidance_model_call_count=len(GUIDANCE_DIMENSION_SPECS),
                historical_fallback_dimensions=fallback_dimensions,
                complete=True,
            )
            yield {
                "event": "done",
                "complete": True,
                "historical_fallback": bool(fallback_dimensions),
                "historical_fallback_dimensions": fallback_dimensions,
                "report": report.model_dump(mode="json"),
                "state": saved_state.model_dump(mode="json"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Guidance stream failed for session_id=%s", session_id)
            log_metric(
                "guidance.generate.error",
                session_id=session_id,
                stream=True,
                guidance_cache_hit=False,
                guidance_retrieval_ms=retrieval_ms,
                guidance_llm_ms=llm_ms,
                guidance_total_ms=elapsed_ms(total_started),
                complete=False,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            yield {"event": "error", "message": str(exc) or type(exc).__name__}
        finally:
            if lock_acquired and generation_lock is not None:
                await generation_lock.__aexit__(None, None, None)

    def _retrieval_context(self, state: SessionState) -> dict:
        ensure_current_performance_locale(state)
        intent_id = state.intent.intent_id if state.intent else ""
        return {
            "output_locale": state.locale,
            "intent": state.intent.config if state.intent else {},
            "profile": state.employee_profile,
            "supplemental_info": guidance_supplemental_info(state),
            "performance_context": (
                state.intent.performance_context if state.intent else ""
            )
            or "",
            "personality": state.personality,
            "motivation": guidance_motivation_context(state),
            "company_value_terms": self.config_loader.company_value_terms(),
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
                include_conversation=False,
            ),
        }

    async def _retrieve_dimension_chunks(
        self,
        state: SessionState,
        spec: GuidanceDimensionSpec,
        *,
        context: dict | None = None,
    ) -> list[RetrievedChunk]:
        if not state.setup_ready:
            raise SetupNotReadyError("请先完成员工信息、面谈目的、人格与诉求设置。")

        started = now_ms()
        context = context or self._retrieval_context(state)
        dimension_title = GUIDANCE_SECTION_TITLES[spec.section_keys[-1]]
        try:
            if hasattr(self.retrieval, "aretrieve"):
                chunks = await self.retrieval.aretrieve(
                    spec.retrieval_name,
                    context,
                    top_k=spec.retrieval_top_k,
                )
            else:
                chunks = await run_sync_with_context(
                    getattr(self.retrieval, "_executor", None),
                    self.retrieval.retrieve,
                    spec.retrieval_name,
                    context,
                    spec.retrieval_top_k,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Guidance dimension retrieval failed for session_id=%s dimension=%s query=%s: %s",
                state.session_id,
                spec.key,
                spec.retrieval_name,
                exc,
            )
            self._append_warning(
                state,
                f"谈前指导“{dimension_title}”KB 检索失败，已基于员工信息和本地配置继续生成：{exc}",
            )
            log_metric(
                "guidance.dimension_retrieval",
                session_id=state.session_id,
                guidance_dimension=spec.key,
                retrieval_name=spec.retrieval_name,
                guidance_retrieval_ms=elapsed_ms(started),
                guidance_retrieved_chunk_count=0,
                complete=False,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            return []

        if not chunks:
            self._append_warning(
                state,
                f"谈前指导“{dimension_title}”KB 未检索到知识片段，已基于员工信息和本地配置继续生成。",
            )
        log_metric(
            "guidance.dimension_retrieval",
            session_id=state.session_id,
            guidance_dimension=spec.key,
            retrieval_name=spec.retrieval_name,
            guidance_retrieval_ms=elapsed_ms(started),
            guidance_retrieved_chunk_count=len(chunks),
            complete=True,
        )
        return chunks

    def _cached_report(
        self,
        session_id: str,
        locale: str = "zh-CN",
    ) -> GuidanceReport | None:
        source_loader = getattr(self.report_repo, "get_guidance_source", None)
        if callable(source_loader):
            try:
                source = source_loader(session_id)
            except KeyError:
                return None
            if source == "historical_fallback":
                logger.info(
                    "Cached Guidance contains historical fallback dimensions; "
                    "retrying model generation session_id=%s",
                    session_id,
                )
                return None
        try:
            report = self.report_repo.get_guidance(session_id)
        except KeyError:
            return None
        except ValidationError as exc:
            logger.warning(
                "Cached Guidance report failed structural validation; regenerating "
                "session_id=%s error_count=%s",
                session_id,
                exc.error_count(),
            )
            return None
        current_version = self._runtime_guidance_version()
        if report.locale != locale:
            logger.info("Guidance locale changed; regenerating session_id=%s", session_id)
            return None
        if report.guidance_version != current_version:
            logger.info("Guidance prompt version changed; regenerating session_id=%s", session_id)
            return None
        if report.culture_version != self.config_loader.culture_version():
            logger.info("Guidance culture version changed; regenerating session_id=%s", session_id)
            return None
        quality_issues = guidance_quality_issues(report.model_dump(mode="json"))
        if quality_issues:
            logger.warning(
                "Cached Guidance report has prose quality warnings; preserving "
                "session_id=%s issues=%s",
                session_id,
                ",".join(quality_issues),
            )
        return report

    def _runtime_guidance_version(self) -> str:
        version_factory = getattr(self.config_loader, "guidance_version", None)
        base_version = (
            version_factory()
            if callable(version_factory)
            else GUIDANCE_REPORT_VERSION
        )
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
        return f"{base_version}{suffix}"

    def _with_runtime_versions(
        self,
        report: GuidanceReport,
    ) -> GuidanceReport:
        return report.model_copy(
            update={"guidance_version": self._runtime_guidance_version()}
        )

    @staticmethod
    def _append_warning(state: SessionState, warning: str) -> None:
        if warning not in state.warnings:
            state.warnings.append(warning)

    @staticmethod
    def _merge_chunks(*groups: list[RetrievedChunk]) -> list[RetrievedChunk]:
        merged: dict[str, RetrievedChunk] = {}
        for group in groups:
            for chunk in group:
                current = merged.get(chunk.chunk_id)
                if current is None or (chunk.score or 0.0) > (current.score or 0.0):
                    merged[chunk.chunk_id] = chunk
        return list(merged.values())

    def _mark_report_state(self, state: SessionState) -> SessionState:
        if state.guidance_report_id != state.session_id:
            state.guidance_report_id = state.session_id
        if state.stage in {"created", "profile_ready", "setup_ready"}:
            state.stage = "guidance_ready"
        return self.session_service.save_session(state)

    def _save_report_state(
        self,
        state,
        report: GuidanceReport,
        source: str = "generation",
    ):
        if state.guidance_report_id != state.session_id:
            state.guidance_report_id = state.session_id
        if state.stage in {"created", "profile_ready", "setup_ready"}:
            state.stage = "guidance_ready"
        atomic_saver = getattr(self.report_repo, "save_guidance_with_state", None)
        if callable(atomic_saver):
            if source == "generation":
                return atomic_saver(report, state)
            return atomic_saver(report, state, source=source)
        if source == "generation":
            self.report_repo.save_guidance(report)
        else:
            self.report_repo.save_guidance(report, source=source)
        return self.session_service.save_session(state)

    async def _historical_guidance_match(
        self,
        state: SessionState,
        *,
        dimension_key: str,
    ) -> HistoricalGuidanceMatch | None:
        history_fallback = getattr(self, "history_fallback", None)
        if history_fallback is None or not history_fallback.enabled:
            return None
        try:
            return await run_db_with_context(
                self.executor,
                "guidance.history_fallback.read",
                history_fallback.find_guidance,
                state,
                guidance_version=self._runtime_guidance_version(),
                culture_version=self.config_loader.culture_version(),
                dimension_key=dimension_key,
            )
        except Exception:
            logger.exception(
                "Guidance history fallback lookup failed: session_id=%s",
                state.session_id,
            )
            return None

    @staticmethod
    def _guidance_dimension_sections(
        report: GuidanceReport,
        spec: GuidanceDimensionSpec,
        state: SessionState,
    ) -> dict[GuidanceResultKey, GuidanceResultValue] | None:
        if report.dimension_points is None:
            return None
        point_groups = getattr(report.dimension_points, spec.key, None)
        if not point_groups:
            return None
        expected_titles = GuidanceAgent._fixed_point_titles(state, spec)
        if [point.title for point in point_groups] != expected_titles:
            return None
        if any(
            not spec.detail_min_items <= len(point.details) <= spec.detail_max_items
            for point in point_groups
        ):
            return None
        sections: dict[GuidanceResultKey, GuidanceResultValue] = {
            key: getattr(report, key)
            for key in spec.section_keys
        }
        sections[spec.point_group_key] = [
            point.model_dump(mode="json")
            for point in point_groups
        ]
        return sections

    @staticmethod
    def _stream_sections(
        report: GuidanceReport,
    ) -> list[tuple[str, str, GuidanceSectionValue]]:
        titles = guidance_section_titles(report.locale)
        return [
            ("purpose", titles["purpose"], report.purpose),
            ("opening_suggestion", titles["opening_suggestion"], report.opening_suggestion),
            ("risk_preview", titles["risk_preview"], report.risk_preview),
            ("response_strategies", titles["response_strategies"], report.response_strategies),
            ("safer_phrases", titles["safer_phrases"], report.safer_phrases),
        ]

    @staticmethod
    def _point_groups_for_section(
        report: GuidanceReport,
        key: str,
    ) -> list[dict[str, object]] | None:
        if report.dimension_points is None:
            return None
        dimension_by_section = {
            "purpose": "start",
            "opening_suggestion": "start",
            "risk_preview": "emotion",
            "response_strategies": "requirement",
            "safer_phrases": "plan",
        }
        dimension = dimension_by_section.get(key)
        if dimension is None:
            return None
        return [
            point.model_dump(mode="json")
            for point in getattr(report.dimension_points, dimension)
        ]

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 80) -> list[str]:
        if not text:
            return []
        return [text[index:index + chunk_size] for index in range(0, len(text), chunk_size)]

    def get(self, session_id: str) -> GuidanceReport:
        state = self.session_service.get_session(session_id)
        report = self._cached_report(session_id, state.locale)
        if report is None:
            raise KeyError(
                f"Guidance report is missing, invalid, or stale: {session_id}"
            )
        return report

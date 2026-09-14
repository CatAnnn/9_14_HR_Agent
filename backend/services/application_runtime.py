from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

from backend.config.settings import Settings, get_settings
from backend.observability.metrics import (
    log_metric,
    record_runtime_event_loop_lag,
)
from backend.redis.retrieval_cache import RetrievalResultCache
from backend.redis.distributed_coordination import get_distributed_coordinator
from backend.repositories.postgres_repository import (
    EmbeddingProfileState,
    PostgresRepository,
    RetrievalScopeCapability,
)
from backend.services.agentic_evidence_service import AgenticEvidencePlanner
from backend.services.coach_service import CoachService
from backend.services.conversation_summary_service import ConversationSummaryService
from backend.services.employee_database_service import EmployeeDatabaseService
from backend.services.fish_tts_service import FishTtsService
from backend.services.guidance_service import GuidanceService
from backend.services.model_scheduler import ModelScheduler
from backend.services.personality_facet_service import PersonalityFacetService
from backend.services.rehearsal_dimension_evaluation_service import RehearsalDimensionEvaluationService
from backend.services.rehearsal_service import RehearsalService
from backend.services.resource_chat_service import ResourceChatService
from backend.services.speech_websocket_service import SpeechWebSocketHub
from backend.services.retrieval_service import RetrievalService
from backend.services.session_service import SessionService
from backend.services.setup_service import SetupService
from backend.services.upload_document import UploadDocumentService
from backend.vectorstore.embedding_profile import resolve_embedding_dimensions


class ApplicationRuntime:
    """Process-wide ownership boundary for reusable services and executors."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.distributed_coordinator = get_distributed_coordinator(self.settings)
        self.rag_executor = ThreadPoolExecutor(
            max_workers=self.settings.rag_thread_pool_max_workers,
            thread_name_prefix="rag-db",
        )
        # Compatibility alias for maintenance code that still reads db_executor.
        self.db_executor = self.rag_executor
        self.workflow_db_executor = ThreadPoolExecutor(
            max_workers=self.settings.workflow_db_thread_pool_max_workers,
            thread_name_prefix="workflow-db",
        )
        self._event_loop_monitor_task: asyncio.Task[None] | None = None
        self._embedding_profile_monitor_task: asyncio.Task[None] | None = None
        self._embedding_profile_refresh_lock = threading.Lock()
        self._embedding_capability_refresh_pending = False
        repository = PostgresRepository(
            database_url=self.settings.database_url,
            initialize=False,
        )
        self.repository = repository
        self.embedding_profile = repository.require_active_embedding_profile(
            self.settings.effective_embedding_model,
            resolve_embedding_dimensions(self.settings),
        )
        self.retrieval_capabilities = (
            repository.retrieval_capability_snapshot(self.embedding_profile)
        )
        self._embedding_capability_refresh_pending = not (
            self._retrieval_indexes_ready(
                self.embedding_profile,
                self.retrieval_capabilities,
            )
        )
        self.model_scheduler = ModelScheduler(self.settings)
        self.summary_service = ConversationSummaryService(
            settings=self.settings,
            model_scheduler=self.model_scheduler,
            executor=self.workflow_db_executor,
        )
        self.retrieval_cache = RetrievalResultCache(
            self.settings,
            coordinator=self.distributed_coordinator,
        )
        self.retrieval_service = RetrievalService(
            executor=self.rag_executor,
            capabilities=self.retrieval_capabilities,
            result_cache=self.retrieval_cache,
            coordinator=self.distributed_coordinator,
            repository=repository,
            embedding_profile=self.embedding_profile,
        )
        self.resource_chat_service = ResourceChatService(
            settings=self.settings,
            retrieval=self.retrieval_service,
            model_scheduler=self.model_scheduler,
        )
        self.agentic_evidence_planner = AgenticEvidencePlanner(
            settings=self.settings,
            retrieval=self.retrieval_service,
            model_scheduler=self.model_scheduler,
            result_cache=self.retrieval_cache,
        )

        self.session_service = SessionService()
        self.personality_facet_service = PersonalityFacetService(
            settings=self.settings,
            model_scheduler=self.model_scheduler,
            executor=self.workflow_db_executor,
        )
        self.rehearsal_dimension_evaluation = RehearsalDimensionEvaluationService(
            settings=self.settings,
            model_scheduler=self.model_scheduler,
        )
        self.setup_service = SetupService(
            settings=self.settings,
            model_scheduler=self.model_scheduler,
            retrieval=self.retrieval_service,
            session_service=self.session_service,
            executor=self.workflow_db_executor,
        )
        self.document_service = UploadDocumentService()
        self.employee_database_service = EmployeeDatabaseService()
        self.tts_service = FishTtsService(
            self.settings,
            coordinator=self.distributed_coordinator,
        )
        self.speech_websocket_hub = SpeechWebSocketHub(
            send_timeout_seconds=self.settings.tts_send_timeout_seconds,
            max_queue_bytes=self.settings.tts_audio_queue_max_bytes,
            max_queue_events=self.settings.tts_audio_queue_max_events,
            playback_high_water_seconds=self.settings.tts_playback_high_water_seconds,
            playback_low_water_seconds=self.settings.tts_playback_low_water_seconds,
            playback_feedback_timeout_seconds=self.settings.tts_playback_feedback_timeout_seconds,
        )
        self.guidance_service = GuidanceService(
            session_service=self.session_service,
            retrieval=self.retrieval_service,
            model_scheduler=self.model_scheduler,
            agentic_evidence=self.agentic_evidence_planner,
            personality_facets=self.personality_facet_service,
            executor=self.workflow_db_executor,
            coordinator=self.distributed_coordinator,
        )
        self.rehearsal_service = RehearsalService(
            settings=self.settings,
            summary_service=self.summary_service,
            tts_service=self.tts_service,
            model_scheduler=self.model_scheduler,
            session_service=self.session_service,
            retrieval=self.retrieval_service,
            personality_facets=self.personality_facet_service,
            dimension_evaluation=self.rehearsal_dimension_evaluation,
            executor=self.workflow_db_executor,
            coordinator=self.distributed_coordinator,
        )
        self.coach_service = CoachService(
            session_service=self.session_service,
            retrieval=self.retrieval_service,
            model_scheduler=self.model_scheduler,
            agentic_evidence=self.agentic_evidence_planner,
            executor=self.workflow_db_executor,
            coordinator=self.distributed_coordinator,
        )
        self._log_capability_snapshot()

    def start(self) -> None:
        if self._event_loop_monitor_task is None or self._event_loop_monitor_task.done():
            self._event_loop_monitor_task = asyncio.create_task(
                self._monitor_event_loop_lag(),
                name="runtime-event-loop-lag",
            )
        if (
            self._embedding_profile_monitor_task is None
            or self._embedding_profile_monitor_task.done()
        ):
            self._embedding_profile_monitor_task = asyncio.create_task(
                self._monitor_embedding_profile(),
                name="runtime-embedding-profile",
            )

    async def _monitor_embedding_profile(self) -> None:
        interval_seconds = float(
            self.settings.kb_active_profile_refresh_interval_seconds
        )
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await asyncio.to_thread(self._refresh_embedding_profile_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Keep the last fully verified generation online. The next
                # interval retries rather than interrupting user retrieval.
                log_metric(
                    "rag.embedding_profile.refresh_error",
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )

    def _refresh_embedding_profile_once(self) -> bool:
        """Publish a newly activated, fully verified retrieval generation."""

        with self._embedding_profile_refresh_lock:
            current_profile = self.embedding_profile
            active_build_id = self.repository.active_embedding_build_id(
                self.settings.effective_embedding_model,
                resolve_embedding_dimensions(self.settings),
            )
            build_changed = bool(
                active_build_id
                and active_build_id != current_profile.active_build_id
            )
            capability_refresh_pending = bool(
                getattr(self, "_embedding_capability_refresh_pending", False)
            )
            if not build_changed and not capability_refresh_pending:
                return False

            candidate_profile = current_profile
            if build_changed:
                candidate_profile = (
                    self.repository.require_active_embedding_profile(
                        self.settings.effective_embedding_model,
                        resolve_embedding_dimensions(self.settings),
                    )
                )
                if (
                    candidate_profile.profile_id != current_profile.profile_id
                    or int(candidate_profile.dimensions)
                    != int(current_profile.dimensions)
                ):
                    log_metric(
                        "rag.embedding_profile.refresh_rejected",
                        current_profile_id=current_profile.profile_id,
                        candidate_profile_id=candidate_profile.profile_id,
                        current_dimensions=current_profile.dimensions,
                        candidate_dimensions=candidate_profile.dimensions,
                    )
                    return False

            capabilities = self.repository.retrieval_capability_snapshot(
                candidate_profile
            )
            previous_capabilities = self.retrieval_capabilities
            capabilities_changed = capabilities != previous_capabilities
            service_build_changed = False
            if build_changed or capabilities_changed:
                service_build_changed = (
                    self.retrieval_service.set_embedding_profile(
                        candidate_profile,
                        capabilities,
                    )
                )
                self.embedding_profile = candidate_profile
                self.retrieval_capabilities = capabilities
            self._embedding_capability_refresh_pending = not (
                self._retrieval_indexes_ready(
                    candidate_profile,
                    capabilities,
                )
            )
            changed = bool(
                build_changed
                or service_build_changed
                or capabilities_changed
            )
            if changed:
                log_metric(
                    "rag.embedding_profile.refreshed",
                    embedding_profile_id=candidate_profile.profile_id,
                    previous_build_id=current_profile.active_build_id,
                    embedding_build_id=candidate_profile.active_build_id,
                    embedding_profile_vector_count=(
                        candidate_profile.vector_count
                    ),
                    rag_scope_count=len(capabilities),
                    rag_bm25_ready_count=sum(
                        item.bm25_ready for item in capabilities.values()
                    ),
                    rag_ann_ready_count=sum(
                        bool(item.ann_dimensions)
                        for item in capabilities.values()
                    ),
                    capability_refresh_pending=(
                        self._embedding_capability_refresh_pending
                    ),
                )
            return changed

    def _retrieval_indexes_ready(
        self,
        profile: EmbeddingProfileState,
        capabilities: dict[
            tuple[str, str],
            RetrievalScopeCapability,
        ],
    ) -> bool:
        """Return whether the active generation's expected indexes are ready."""

        if not capabilities:
            return False
        require_bm25 = bool(
            getattr(self.settings, "rag_hybrid_search_enabled", True)
        )
        require_ann = bool(
            getattr(self.settings, "postgres_create_hnsw_index", True)
        )
        ann_min_rows = int(
            getattr(self.settings, "postgres_ann_min_rows", 10_000)
        )
        for capability in capabilities.values():
            if require_bm25 and not capability.bm25_ready:
                return False
            if (
                require_ann
                and (
                    capability.row_count >= ann_min_rows
                    or capability.ann_required(profile.dimensions)
                )
                and not capability.ann_ready(profile.dimensions)
            ):
                return False
        return True

    async def _monitor_event_loop_lag(self) -> None:
        interval_seconds = 1.0
        loop = asyncio.get_running_loop()
        expected = loop.time() + interval_seconds
        while True:
            await asyncio.sleep(max(0.0, expected - loop.time()))
            observed = loop.time()
            lag_ms = max(0.0, (observed - expected) * 1000)
            record_runtime_event_loop_lag(lag_ms)
            if lag_ms >= 100:
                log_metric(
                    "runtime.event_loop_lag",
                    event_loop_lag_ms=round(lag_ms, 2),
                )
            expected = observed + interval_seconds

    async def shutdown(self) -> None:
        if self._embedding_profile_monitor_task is not None:
            self._embedding_profile_monitor_task.cancel()
            await asyncio.gather(
                self._embedding_profile_monitor_task,
                return_exceptions=True,
            )
            self._embedding_profile_monitor_task = None
        if self._event_loop_monitor_task is not None:
            self._event_loop_monitor_task.cancel()
            await asyncio.gather(
                self._event_loop_monitor_task,
                return_exceptions=True,
            )
            self._event_loop_monitor_task = None
        await self.speech_websocket_hub.shutdown()
        await self.summary_service.shutdown()
        await self.tts_service.aclose()
        await self.agentic_evidence_planner.shutdown()
        await self.retrieval_service.shutdown()
        await self.distributed_coordinator.aclose()
        await asyncio.to_thread(
            self.rag_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        await asyncio.to_thread(
            self.workflow_db_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )

    def _log_capability_snapshot(self) -> None:
        capabilities = list(self.retrieval_capabilities.values())
        log_metric(
            "rag.capability_snapshot",
            rag_scope_count=len(capabilities),
            rag_bm25_ready_count=sum(item.bm25_ready for item in capabilities),
            rag_ann_ready_count=sum(bool(item.ann_dimensions) for item in capabilities),
            rag_exact_scope_count=sum(not item.ann_dimensions for item in capabilities),
            rag_bm25_missing_scopes=[
                item.scope for item in capabilities if not item.bm25_ready
            ],
            postgres_ann_min_rows=self.settings.postgres_ann_min_rows,
            embedding_provider=self.settings.effective_embedding_provider,
            embedding_model=self.settings.effective_embedding_model,
            embedding_dimensions=resolve_embedding_dimensions(self.settings),
            embedding_profile_id=self.embedding_profile.profile_id,
            embedding_build_id=self.embedding_profile.active_build_id,
            embedding_profile_status=self.embedding_profile.status,
            embedding_profile_ready=self.embedding_profile.ready,
            embedding_profile_vector_count=self.embedding_profile.vector_count,
            rag_executor_workers=self.settings.rag_thread_pool_max_workers,
            workflow_db_executor_workers=(
                self.settings.workflow_db_thread_pool_max_workers
            ),
            postgres_pool_max_size=self.settings.postgres_pool_max_size,
            rerank_max_concurrency=self.settings.rag_rerank_max_concurrency,
            rag_agentic_search_mode=self.agentic_evidence_planner.mode,
            rag_agentic_search_config_hash=(
                self.agentic_evidence_planner.report_version_suffix
            ),
            rag_agentic_search_timeout_seconds=(
                self.settings.rag_agentic_search_timeout_seconds
            ),
            model_total_max_concurrency=self.settings.model_total_max_concurrency,
        )
        log_metric(
            "tts.runtime_config",
            enabled=self.settings.tts_enabled,
            provider=self.settings.tts_provider,
            model=self.settings.tts_model,
            model_revision=self.settings.tts_model_revision,
            stage_0_max_num_seqs=self.settings.tts_stage_0_max_num_seqs,
            stage_1_max_num_seqs=self.settings.tts_stage_1_max_num_seqs,
            application_max_concurrency=self.settings.tts_max_concurrency,
        )
        for capability in capabilities:
            if capability.bm25_ready:
                continue
            log_metric(
                "rag.bm25.degraded",
                collection_name=capability.collection_name,
                scope=capability.scope,
                row_count=capability.row_count,
                reason="capability_snapshot_missing_or_incomplete_index",
            )


_runtime: ApplicationRuntime | None = None
_runtime_lock = threading.Lock()


def get_application_runtime() -> ApplicationRuntime:
    global _runtime
    if _runtime is not None:
        return _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = ApplicationRuntime()
        return _runtime


async def shutdown_application_runtime() -> None:
    global _runtime
    with _runtime_lock:
        runtime = _runtime
        _runtime = None
    if runtime is not None:
        await runtime.shutdown()

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.agents.coach_agent.dimension_evaluator import validate_coach_runtime
from backend.agents.guidance_agent import validate_guidance_runtime
from backend.api.error_handlers import register_error_handlers
from backend.api.routes import admin_exports, admin_test_workflows
from backend.api.routes import (
    asr,
    auth,
    documents,
    ebooks,
    employees,
    guidance,
    health,
    rehearsal,
    reports,
    resource_chat,
    sessions,
    setup,
)
from backend.config.settings import get_settings
from backend.core.auth_dependency import get_current_user
from backend.observability.metrics import (
    initialize_user_observability,
    shutdown_metrics_logger,
)
from backend.observability.user_activity_middleware import UserActivityMiddleware
from backend.repositories.postgres_repository import (
    EmbeddingProfileState,
    PostgresRepository,
)
from backend.services.http_client import (
    close_shared_async_client,
    close_shared_sync_client,
    get_shared_async_client,
)
from backend.services.local_model_runtime import get_local_model_runtime_recycler
from backend.services.model_warmup_service import ModelWarmupService
from backend.services.recording_asr_service import (
    AsrStorageError,
    prepare_recording_storage,
    recording_cleanup_loop,
)
from backend.services.application_runtime import (
    get_application_runtime,
    shutdown_application_runtime,
)
from backend.vectorstore.index_manager import IndexManager


logger = logging.getLogger(__name__)


async def _wait_for_active_embedding_profile(
    repository: PostgresRepository,
    *,
    model_name: str,
    dimensions: int,
    timeout_seconds: float,
    poll_interval_seconds: float = 5.0,
) -> EmbeddingProfileState:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_seconds)
    while True:
        state = await asyncio.to_thread(
            repository.embedding_profile_state,
            model_name,
            dimensions,
        )
        if state.ready:
            return state
        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            raise RuntimeError(
                "Timed out waiting for the knowledge base startup sync to "
                "activate the configured embedding profile: "
                f"model={model_name}, dimensions={dimensions}, "
                f"profile_id={state.profile_id}, status={state.status}, "
                f"reason={state.stale_reason or 'not_ready'}"
            )
        await asyncio.sleep(min(poll_interval_seconds, remaining_seconds))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    initialize_user_observability()
    app.state.coach_version = validate_coach_runtime()
    app.state.guidance_version = validate_guidance_runtime()
    logger.info(
        "Prompt bundles loaded coach_version=%s guidance_version=%s auto_reload=%s",
        app.state.coach_version,
        app.state.guidance_version,
        settings.prompt_auto_reload_enabled,
    )
    repository = PostgresRepository()
    repository.ensure_schema_initialized()
    if settings.kb_sync_on_startup:
        kb_sync_summary = await asyncio.to_thread(
            IndexManager(settings=settings).try_sync_changed
        )
        app.state.kb_sync_summary = kb_sync_summary
        if kb_sync_summary is None:
            logger.info(
                "Knowledge base startup sync is running in another backend; "
                "waiting for the configured embedding profile"
            )
            embedding_profile = await _wait_for_active_embedding_profile(
                repository,
                model_name=settings.effective_embedding_model,
                dimensions=settings.effective_embedding_dimensions,
                timeout_seconds=settings.kb_startup_sync_wait_timeout_seconds,
            )
            logger.info(
                "Knowledge base startup sync became available "
                "profile_id=%s build_id=%s vectors=%s",
                embedding_profile.profile_id,
                embedding_profile.active_build_id,
                embedding_profile.vector_count,
            )
        else:
            logger.info(
                "Knowledge base startup sync complete changed=%s adopted=%s "
                "unchanged=%s removed=%s vectors=%s elapsed_seconds=%s",
                kb_sync_summary["changed_document_count"],
                kb_sync_summary["adopted_document_count"],
                kb_sync_summary["unchanged_document_count"],
                kb_sync_summary["removed_document_count"],
                kb_sync_summary["vector_count"],
                kb_sync_summary["elapsed_seconds"],
            )
    app.state.runtime = get_application_runtime()
    runtime_starter = getattr(app.state.runtime, "start", None)
    if callable(runtime_starter):
        runtime_starter()
    app.state.http_client = get_shared_async_client()
    runtime_recycler = get_local_model_runtime_recycler()
    runtime_recycler.start()
    app.state.conversation_summary_service = app.state.runtime.summary_service
    warmup_service = ModelWarmupService(
        settings,
        tts_service=app.state.runtime.tts_service,
    )
    app.state.model_warmup = warmup_service
    warmup_task: asyncio.Task[dict] | None = None
    if settings.model_warmup_enabled:
        if settings.model_warmup_block_startup:
            await warmup_service.warmup()
        else:
            warmup_task = asyncio.create_task(
                warmup_service.warmup(),
                name="model-startup-warmup",
            )
            app.state.model_warmup_task = warmup_task
    asr_keepwarm_task: asyncio.Task[None] | None = None
    if (
        settings.asr_enabled
        and settings.asr_provider_mode == "bosch"
        and settings.asr_bosch_keepwarm_enabled
    ):
        asr_keepwarm_task = asyncio.create_task(
            warmup_service.keep_http_asr_warm(),
            name="bosch-asr-keepwarm",
        )
        app.state.asr_keepwarm_task = asr_keepwarm_task
    recording_cleanup_task: asyncio.Task[None] | None = None
    if settings.asr_enabled:
        try:
            await asyncio.to_thread(prepare_recording_storage, settings)
        except (AsrStorageError, OSError):
            logger.warning("ASR recording storage preflight failed", exc_info=True)
        recording_cleanup_task = asyncio.create_task(
            recording_cleanup_loop(settings),
            name="asr-recording-cleanup",
        )
        app.state.asr_recording_cleanup_task = recording_cleanup_task
    try:
        yield
    finally:
        if warmup_task is not None and not warmup_task.done():
            warmup_task.cancel()
        if warmup_task is not None:
            await asyncio.gather(warmup_task, return_exceptions=True)
        if asr_keepwarm_task is not None and not asr_keepwarm_task.done():
            asr_keepwarm_task.cancel()
        if asr_keepwarm_task is not None:
            await asyncio.gather(asr_keepwarm_task, return_exceptions=True)
        if recording_cleanup_task is not None and not recording_cleanup_task.done():
            recording_cleanup_task.cancel()
        if recording_cleanup_task is not None:
            await asyncio.gather(recording_cleanup_task, return_exceptions=True)
        await shutdown_application_runtime()
        await close_shared_async_client()
        close_shared_sync_client()
        PostgresRepository.close_connection_pools()
        runtime_recycler.shutdown()
        shutdown_metrics_logger()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(UserActivityMiddleware)
    register_error_handlers(app)
    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(admin_exports.router, prefix=settings.api_prefix)
    app.include_router(admin_test_workflows.router, prefix=settings.api_prefix)
    protected_routers = [
        sessions.router,
        documents.router,
        ebooks.router,
        employees.router,
        setup.router,
        guidance.router,
        rehearsal.router,
        reports.router,
        resource_chat.router,
        asr.router,
    ]
    for router in protected_routers:
        app.include_router(
            router,
            prefix=settings.api_prefix,
            dependencies=[Depends(get_current_user)],
        )
    return app


app = create_app()

from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.api.dependencies as dependencies
import backend.services.application_runtime as runtime_module


@pytest.mark.asyncio
async def test_application_runtime_singleton_is_reused_and_shutdown_once(monkeypatch):
    created: list[object] = []
    shutdown: list[object] = []

    class Runtime:
        def __init__(self):
            created.append(self)

        async def shutdown(self):
            shutdown.append(self)

    monkeypatch.setattr(runtime_module, "ApplicationRuntime", Runtime)
    monkeypatch.setattr(runtime_module, "_runtime", None)

    first = runtime_module.get_application_runtime()
    second = runtime_module.get_application_runtime()
    await runtime_module.shutdown_application_runtime()

    assert first is second
    assert created == [first]
    assert shutdown == [first]
    assert runtime_module._runtime is None


def test_application_runtime_injects_one_retrieval_and_two_executors(monkeypatch):
    created: dict[str, list[object]] = {}

    class Component:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            created.setdefault(type(self).__name__, []).append(self)

    class Repository:
        def __init__(self, *args, **kwargs):
            pass

        def require_active_embedding_profile(self, model, dimensions):
            return SimpleNamespace(
                profile_id="profile-a",
                active_build_id="build-a",
                model_name=model,
                dimensions=dimensions,
            )

        def retrieval_capability_snapshot(self, profile):
            return {}

    class Retrieval(Component):
        pass

    class AgenticPlanner(Component):
        pass

    class Summary(Component):
        pass

    class Session(Component):
        pass

    class Guidance(Component):
        pass

    class Rehearsal(Component):
        pass

    class Coach(Component):
        pass

    monkeypatch.setattr(runtime_module, "PostgresRepository", Repository)
    monkeypatch.setattr(runtime_module, "RetrievalResultCache", Component)
    monkeypatch.setattr(runtime_module, "ModelScheduler", Component)
    monkeypatch.setattr(runtime_module, "ResourceChatService", Component)
    monkeypatch.setattr(runtime_module, "ConversationSummaryService", Summary)
    monkeypatch.setattr(runtime_module, "RetrievalService", Retrieval)
    monkeypatch.setattr(
        runtime_module,
        "AgenticEvidencePlanner",
        AgenticPlanner,
    )
    monkeypatch.setattr(runtime_module, "SessionService", Session)
    monkeypatch.setattr(runtime_module, "SetupService", Component)
    monkeypatch.setattr(runtime_module, "UploadDocumentService", Component)
    monkeypatch.setattr(runtime_module, "EmployeeDatabaseService", Component)
    monkeypatch.setattr(runtime_module, "FishTtsService", Component)
    monkeypatch.setattr(runtime_module, "SpeechWebSocketHub", Component)
    monkeypatch.setattr(runtime_module, "GuidanceService", Guidance)
    monkeypatch.setattr(runtime_module, "RehearsalService", Rehearsal)
    monkeypatch.setattr(runtime_module, "CoachService", Coach)
    monkeypatch.setattr(
        runtime_module.ApplicationRuntime,
        "_log_capability_snapshot",
        lambda self: None,
    )

    settings = SimpleNamespace(
        rag_thread_pool_max_workers=3,
        workflow_db_thread_pool_max_workers=2,
        database_url="postgresql://test",
        effective_embedding_model="embedding",
        embedding_dimensions=2,
        tts_send_timeout_seconds=3.0,
        tts_audio_queue_max_bytes=1048576,
        tts_audio_queue_max_events=16,
        tts_playback_high_water_seconds=8.0,
        tts_playback_low_water_seconds=4.0,
        tts_playback_feedback_timeout_seconds=3.0,
    )
    runtime = runtime_module.ApplicationRuntime(settings)
    try:
        retrieval = created["Retrieval"][0]
        resource_chat = runtime.resource_chat_service
        planner = created["AgenticPlanner"][0]
        session = created["Session"][0]
        summary = created["Summary"][0]
        rehearsal = created["Rehearsal"][0]
        guidance = created["Guidance"][0]
        coach = created["Coach"][0]

        assert len(created["Retrieval"]) == 1
        assert len(created["AgenticPlanner"]) == 1
        assert planner.kwargs["retrieval"] is retrieval
        assert retrieval.kwargs["executor"] is runtime.rag_executor
        assert isinstance(retrieval.kwargs["repository"], Repository)
        assert retrieval.kwargs["embedding_profile"].profile_id == "profile-a"
        assert resource_chat.kwargs["retrieval"] is retrieval
        assert runtime.db_executor is runtime.rag_executor
        assert runtime.workflow_db_executor is not runtime.rag_executor
        assert summary.kwargs["executor"] is runtime.workflow_db_executor
        assert rehearsal.kwargs["retrieval"] is retrieval
        assert rehearsal.kwargs["session_service"] is session
        assert rehearsal.kwargs["executor"] is runtime.workflow_db_executor
        assert guidance.kwargs["retrieval"] is retrieval
        assert guidance.kwargs["agentic_evidence"] is planner
        assert guidance.kwargs["session_service"] is session
        assert guidance.kwargs["executor"] is runtime.workflow_db_executor
        assert coach.kwargs["retrieval"] is retrieval
        assert coach.kwargs["agentic_evidence"] is planner
        assert coach.kwargs["session_service"] is session
        assert coach.kwargs["executor"] is runtime.workflow_db_executor
        assert runtime.speech_websocket_hub.kwargs == {
            "send_timeout_seconds": 3.0,
            "max_queue_bytes": 1048576,
            "max_queue_events": 16,
            "playback_high_water_seconds": 8.0,
            "playback_low_water_seconds": 4.0,
            "playback_feedback_timeout_seconds": 3.0,
        }
    finally:
        runtime.rag_executor.shutdown(wait=True)
        runtime.workflow_db_executor.shutdown(wait=True)


def test_api_dependencies_return_services_from_shared_runtime(monkeypatch):
    runtime = SimpleNamespace(
        session_service=object(),
        setup_service=object(),
        document_service=object(),
        employee_database_service=object(),
        guidance_service=object(),
        rehearsal_service=object(),
        speech_websocket_hub=object(),
        coach_service=object(),
        resource_chat_service=object(),
    )
    monkeypatch.setattr(dependencies, "get_application_runtime", lambda: runtime)

    assert dependencies.get_session_service() is runtime.session_service
    assert dependencies.get_setup_service() is runtime.setup_service
    assert dependencies.get_document_service() is runtime.document_service
    assert (
        dependencies.get_employee_database_service()
        is runtime.employee_database_service
    )
    assert dependencies.get_guidance_service() is runtime.guidance_service
    assert dependencies.get_rehearsal_service() is runtime.rehearsal_service
    assert (
        dependencies.get_speech_websocket_hub() is runtime.speech_websocket_hub
    )
    assert dependencies.get_coach_service() is runtime.coach_service
    assert (
        dependencies.get_resource_chat_service()
        is runtime.resource_chat_service
    )

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api.dependencies import get_session_service, get_setup_service
from backend.api.routes import admin_test_workflows
from backend.business_config.loader import get_config_loader
from backend.core.auth_dependency import get_current_session
from backend.schemas.admin_test_workflow import AdminTestWorkflowCreateRequest
from backend.schemas.auth import AuthUserResponse
from backend.schemas.simulation import BigFivePersonality, EmotionState, VADVector
from backend.schemas.state import SessionState
from backend.services.admin_test_workflow_service import AdminTestWorkflowService
from backend.services.auth_session_service import AuthSession
from backend.workflows.guards import ensure_rehearsal_allowed


class _EmotionTransition:
    def initial_state(self, **_kwargs) -> EmotionState:
        return EmotionState(
            current_vad=VADVector(valence=0.0, arousal=-0.1, dominance=0.0),
            current_anchor_id="neutral",
        )


class _SetupService:
    def __init__(self) -> None:
        self.loader = get_config_loader()
        self.emotion_transition = _EmotionTransition()


class _SessionService:
    def __init__(self) -> None:
        self.saved: SessionState | None = None
        self.save_count = 0

    def save_session(self, state: SessionState) -> SessionState:
        self.saved = state
        self.save_count += 1
        return state


def _payload(destination: str = "report") -> AdminTestWorkflowCreateRequest:
    return AdminTestWorkflowCreateRequest(
        destination=destination,
        intent_id="improvement",
        personality=BigFivePersonality(
            openness=60,
            conscientiousness=70,
            extraversion=40,
            agreeableness=55,
            neuroticism=65,
        ),
        primary_motive_id="commerce",
        secondary_motive_ids=[],
        conversation=(
            [
                {"speaker": "manager", "text": "我们先回顾本周期结果。"},
                {"speaker": "employee", "text": "我对结果有不同看法。"},
            ]
            if destination == "report"
            else []
        ),
    )


def _auth_session(role: str) -> AuthSession:
    return AuthSession(
        session_id="auth-session",
        user=AuthUserResponse(
            email=f"{role}@bosch.com",
            display_name=role,
            role=role,
        ),
        user_id=f"{role}-id",
        role=role,
        created_at=1.0,
        last_seen_at=2.0,
    )


def _test_app(role: str, session_service: _SessionService) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_test_workflows.router)
    app.dependency_overrides[get_current_session] = lambda: _auth_session(role)
    app.dependency_overrides[get_setup_service] = _SetupService
    app.dependency_overrides[get_session_service] = lambda: session_service
    return app


def test_report_request_requires_both_sides_of_the_conversation() -> None:
    with pytest.raises(ValidationError, match="at least one manager turn"):
        AdminTestWorkflowCreateRequest(
            destination="report",
            intent_id="development",
            personality=BigFivePersonality(),
            primary_motive_id="commerce",
            secondary_motive_ids=["recognition"],
            conversation=[{"speaker": "employee", "text": "我了解了。"}],
        )


def test_service_creates_a_complete_isolated_report_session() -> None:
    session_service = _SessionService()
    state = AdminTestWorkflowService(
        setup_service=_SetupService(),
        session_service=session_service,
    ).create(_payload())

    assert session_service.save_count == 1
    assert state.setup_ready is True
    assert state.run_mode == "rehearsal_report"
    assert state.stage == "rehearsal"
    assert state.intent and state.intent.intent_id == "improvement"
    assert state.employee_profile and state.employee_profile.is_ready_for_setup()
    assert state.employee_profile.performance_rating == "4"
    assert state.personality and state.personality.conscientiousness == 70
    assert state.motivation and state.motivation.primary_motive_id == "commerce"
    assert state.motivation.secondary_motive_ids == []
    assert state.emotion_state and state.emotion_state.current_anchor_id == "neutral"
    assert [turn.turn_index for turn in state.conversation] == [1, 2]
    assert [turn.speaker for turn in state.conversation] == ["manager", "employee"]
    assert all(turn.metadata["synthetic"] is True for turn in state.conversation)
    assert state.user_turn_count == 1
    assert AdminTestWorkflowService.TEST_SESSION_WARNING in state.warnings
    ensure_rehearsal_allowed(state)


def test_rehearsal_destination_starts_without_seeded_conversation() -> None:
    state = AdminTestWorkflowService(
        setup_service=_SetupService(),
        session_service=_SessionService(),
    ).create(_payload("rehearsal"))

    assert state.stage == "setup_ready"
    assert state.conversation == []
    assert state.user_turn_count == 0
    ensure_rehearsal_allowed(state)


def test_admin_test_workflow_starts_in_requested_english_locale() -> None:
    payload = _payload("rehearsal").model_copy(update={"locale": "en"})

    state = AdminTestWorkflowService(
        setup_service=_SetupService(),
        session_service=_SessionService(),
    ).create(payload)

    assert state.locale == "en"
    assert state.intent and state.intent.performance_locale == "en"
    assert state.intent.reason == "Explicitly selected in the administrator test workflow"
    assert state.employee_profile and state.employee_profile.employee_alias == "Test Employee"
    assert state.employee_profile.conversation_topic == "Improvement"
    assert "改进" not in state.employee_profile.source_profile_text
    assert state.warnings == [AdminTestWorkflowService.TEST_SESSION_WARNING_EN]


def test_route_rejects_non_admin_before_creating_a_session() -> None:
    session_service = _SessionService()
    with TestClient(_test_app("user", session_service)) as client:
        response = client.post(
            "/admin/test-workflows",
            json=_payload().model_dump(mode="json"),
        )

    assert response.status_code == 403
    assert session_service.save_count == 0


def test_route_allows_an_admin_to_create_the_test_session() -> None:
    session_service = _SessionService()
    with TestClient(_test_app("admin", session_service)) as client:
        response = client.post(
            "/admin/test-workflows",
            json=_payload().model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json()["run_mode"] == "rehearsal_report"
    assert session_service.save_count == 1

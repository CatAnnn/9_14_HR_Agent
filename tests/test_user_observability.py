from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.core import auth_dependency
from backend.core.session_context import (
    get_current_auth_user_id,
    get_current_observability_user_id,
    observability_user_id_from_email,
    reset_current_auth_user_id,
    reset_current_observability_user_id,
    set_current_auth_user_id,
    set_current_observability_user_email,
)
from backend.observability import metrics as metrics_module
from backend.observability import user_activity_middleware as middleware_module
from backend.schemas.auth import AuthUserResponse
from backend.services.auth_session_service import AuthSession


class FakeCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int | float, dict[str, object]]] = []

    def add(self, value: int | float, attributes: dict[str, object]) -> None:
        self.calls.append((value, attributes))


class FakeHistogram:
    def __init__(self) -> None:
        self.calls: list[tuple[int | float, dict[str, object]]] = []

    def record(self, value: int | float, attributes: dict[str, object]) -> None:
        self.calls.append((value, attributes))


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def is_recording(self) -> bool:
        return True

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


def test_observability_user_id_uses_normalized_email_local_part() -> None:
    user_identifier = observability_user_id_from_email(
        " Fixed.Term+team@CN.BOSCH.COM ",
    )

    assert user_identifier == "fixed.term+team"
    assert "@" not in user_identifier
    assert observability_user_id_from_email(None) == "system"
    assert observability_user_id_from_email("invalid-email") == "system"


def test_active_user_tracker_counts_unique_users_in_rolling_windows() -> None:
    now = [1000.0]
    tracker = metrics_module.ActiveUserTracker(clock=lambda: now[0])
    tracker.mark_seen("user-a")
    tracker.mark_seen("user-a")
    tracker.mark_seen("user-b")

    assert tracker.count(300) == 2
    now[0] += 301

    assert tracker.count(300) == 0
    assert tracker.count(900) == 2


def test_authenticated_request_metrics_use_email_local_part(monkeypatch) -> None:
    counter = FakeCounter()
    histogram = FakeHistogram()
    span = FakeSpan()
    user_identifier = "fixed-term.yiheng.lu"
    monkeypatch.setattr(metrics_module, "_USER_REQUEST_COUNTER", counter)
    monkeypatch.setattr(metrics_module, "_USER_REQUEST_DURATION", histogram)
    monkeypatch.setattr(metrics_module.otel_trace, "get_current_span", lambda: span)

    token = set_current_observability_user_email(f"{user_identifier}@cn.bosch.com")
    try:
        metrics_module.record_authenticated_user_request(
            method="get",
            route="/api/v1/sessions/{session_id}",
            status_code=200,
            duration_ms=35,
        )
    finally:
        reset_current_observability_user_id(token)

    attributes = counter.calls[0][1]
    assert counter.calls[0][0] == 1
    assert histogram.calls == [(35, attributes)]
    assert attributes["hr_agent.user.id"] == user_identifier
    assert "@bosch" not in str(attributes)
    assert attributes["http.route"] == "/api/v1/sessions/{session_id}"
    assert attributes["hr_agent.response.status_class"] == "2xx"
    assert span.attributes["hr_agent.user.id"] == attributes["hr_agent.user.id"]


def test_rehearsal_turn_metrics_use_user_and_low_cardinality_labels(
    monkeypatch,
) -> None:
    counter = FakeCounter()
    histogram = FakeHistogram()
    logged: dict[str, object] = {}
    monkeypatch.setattr(metrics_module, "_REHEARSAL_TURN_COUNTER", counter)
    monkeypatch.setattr(metrics_module, "_REHEARSAL_TURN_DURATION", histogram)
    monkeypatch.setattr(
        metrics_module,
        "log_metric",
        lambda event, **fields: logged.update({"event": event, **fields}),
    )
    token = set_current_observability_user_email("Timing.User@bosch.com")
    try:
        metrics_module.record_rehearsal_turn_timing(
            user_id="timing.user",
            attempt_id="attempt-1",
            session_hash="deadbeefdeadbeef",
            manager_turn_index=1,
            employee_turn_index=2,
            transport="stream",
            speech_enabled=False,
            outcome="success",
            stages=[
                {
                    "name": "knowledge_retrieval",
                    "duration_ms": 120,
                    "outcome": "success",
                    "parallel_group": "analysis",
                }
            ],
            summary_ms={"total_ms": 250},
        )
    finally:
        reset_current_observability_user_id(token)

    assert counter.calls[0][1]["hr_agent.user.id"] == "timing.user"
    assert {call[1]["hr_agent.rehearsal.operation"] for call in histogram.calls} == {
        "knowledge_retrieval",
        "total_ms",
    }
    assert all(
        "attempt" not in " ".join(call[1])
        and "session" not in " ".join(call[1])
        for call in histogram.calls
    )
    assert logged["event"] == "rehearsal.turn"
    assert logged["user_id"] == "timing.user"
    assert logged["rehearsal_session_hash"] == "deadbeefdeadbeef"


def test_user_request_metric_is_initialized_without_fake_usage(monkeypatch) -> None:
    counter = FakeCounter()
    monkeypatch.setattr(metrics_module, "_USER_REQUEST_COUNTER", counter)

    metrics_module.initialize_user_observability()

    assert counter.calls[0][0] == 0
    assert counter.calls[0][1]["hr_agent.user.id"] == "system"
    assert counter.calls[0][1]["http.route"] == "bootstrap"


@pytest.mark.asyncio
async def test_authenticated_session_sets_observability_id_from_email(monkeypatch) -> None:
    session = AuthSession(
        session_id="session-1",
        user=AuthUserResponse(
            email="Fixed.Term.Yiheng.Lu@cn.bosch.com",
            display_name="Test user",
            role="user",
        ),
        user_id="internal-user-uuid",
        role="user",
        created_at=1,
        last_seen_at=1,
    )
    connection = SimpleNamespace(
        cookies={"hr_agent_session": "session-1"},
        scope={"type": "http"},
    )
    settings = SimpleNamespace(auth_enabled=True, auth_cookie_name="hr_agent_session")
    session_service = SimpleNamespace(get_session=lambda _session_id: session)
    marked: list[str] = []
    monkeypatch.setattr(
        auth_dependency,
        "mark_authenticated_user_active",
        lambda: marked.append(get_current_observability_user_id()),
    )
    auth_token = set_current_auth_user_id(None)
    observability_token = set_current_observability_user_email(None)
    try:
        result = await auth_dependency.get_current_session(
            connection,
            settings,
            session_service,
        )

        assert result is session
        assert get_current_auth_user_id() == "internal-user-uuid"
        assert get_current_observability_user_id() == "fixed.term.yiheng.lu"
        assert marked == ["fixed.term.yiheng.lu"]
    finally:
        reset_current_observability_user_id(observability_token)
        reset_current_auth_user_id(auth_token)


def test_llm_metrics_inherit_email_local_part(monkeypatch) -> None:
    counter = FakeCounter()
    monkeypatch.setattr(metrics_module, "_LLM_TOKEN_USAGE_COUNTER", counter)
    auth_token = set_current_auth_user_id("internal-user-id")
    observability_token = set_current_observability_user_email(
        "Request.User@bosch.com",
    )
    try:
        metrics_module.record_llm_token_usage(
            input_tokens=12,
            output_tokens=3,
            task_name="guidance",
            model_name="glm-5.2",
            provider_name="bosch_openai_compatible",
            stream=True,
            request_outcome="success",
            input_estimated=False,
            output_estimated=False,
        )
    finally:
        reset_current_observability_user_id(observability_token)
        reset_current_auth_user_id(auth_token)

    assert counter.calls[0][1]["hr_agent.user.id"] == "request.user"
    assert "@bosch" not in str(counter.calls)


def test_user_activity_middleware_records_template_route_and_resets_context(monkeypatch) -> None:
    recorded: dict[str, object] = {}
    sent: list[dict[str, object]] = []
    outer_auth_token = set_current_auth_user_id("outer-user")
    outer_observability_token = set_current_observability_user_email(
        "outer.user@bosch.com",
    )

    async def downstream(scope, _receive, send) -> None:
        set_current_auth_user_id("request-user")
        set_current_observability_user_email("Request.User@bosch.com")
        scope["route"] = SimpleNamespace(path="/api/v1/rehearsal/{session_id}/message")
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    monkeypatch.setattr(
        middleware_module,
        "record_authenticated_user_request",
        lambda **fields: recorded.update(
            fields,
            user_identifier=get_current_observability_user_id(),
        ),
    )
    middleware = middleware_module.UserActivityMiddleware(downstream)
    scope = {"type": "http", "method": "POST", "path": "/actual/session-id"}

    asyncio.run(middleware(scope, None, lambda message: _capture(sent, message)))

    assert recorded["user_identifier"] == "request.user"
    assert recorded["route"] == "/api/v1/rehearsal/{session_id}/message"
    assert recorded["status_code"] == 201
    assert recorded["protocol"] == "http"
    assert get_current_auth_user_id() == "outer-user"
    assert get_current_observability_user_id() == "outer.user"
    reset_current_observability_user_id(outer_observability_token)
    reset_current_auth_user_id(outer_auth_token)


async def _capture(messages: list[dict[str, object]], message: dict[str, object]) -> None:
    messages.append(message)

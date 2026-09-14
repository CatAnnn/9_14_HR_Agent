from __future__ import annotations

import os
import time
from typing import Any

from locust import events

from loadtests.accounts import LoadTestAccount
from loadtests.coordination import RunCoordinator
from loadtests.cost import estimate_cost_cny, parse_rates, reserve_cost
from loadtests.payloads import (
    MANAGER_MESSAGES,
    intent_request,
    profile_payload,
    simulation_payload,
)
from loadtests.sse import SSEParser, SSEProtocolError, StreamObservation


class WorkflowFailure(RuntimeError):
    pass


def estimated_live_flow_cost_cny() -> float:
    return estimate_cost_cny(
        model=os.getenv("LOADTEST_LIVE_MODEL", "glm-5.2"),
        input_tokens=int(
            os.getenv("LOADTEST_ESTIMATED_INPUT_TOKENS_PER_FLOW", "120000")
        ),
        output_tokens=int(
            os.getenv("LOADTEST_ESTIMATED_OUTPUT_TOKENS_PER_FLOW", "16000")
        ),
        rates=parse_rates(os.getenv("LOADTEST_MODEL_PRICING")),
    )


def reserve_live_flow_cost(coordinator: RunCoordinator) -> float | None:
    if os.getenv("LOADTEST_MODEL_MODE", "stub").strip().lower() != "live":
        return None
    if os.getenv("LOADTEST_ALLOW_LIVE_MODELS", "false").strip().lower() != "true":
        raise WorkflowFailure(
            "live models require LOADTEST_ALLOW_LIVE_MODELS=true"
        )
    maximum = float(os.getenv("LOADTEST_MAX_COST_CNY", "0"))
    if maximum <= 0:
        raise WorkflowFailure(
            "live models require a positive LOADTEST_MAX_COST_CNY"
        )
    return reserve_cost(
        coordinator.redis,
        key=f"{coordinator.prefix}:estimated_cost_cny",
        estimated_cny=estimated_live_flow_cost_cny(),
        maximum_cny=maximum,
    )


class FullWorkflowRunner:
    def __init__(
        self,
        *,
        user: Any,
        account: LoadTestAccount,
        coordinator: RunCoordinator,
        run_id: str,
    ) -> None:
        self.user = user
        self.client = user.client
        self.account = account
        self.coordinator = coordinator
        self.run_id = run_id
        self.session_id: str | None = None
        self.marked_started = False

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Load-Test-Run-ID": self.run_id,
            "X-Load-Test-User": f"user-{self.account.index:04d}",
        }

    def run(self) -> str:
        reserve_live_flow_cost(self.coordinator)
        self._login()
        state = self._json_request(
            "POST",
            "/api/v1/sessions",
            name="01 sessions/create",
        )
        self.session_id = str(state.get("session_id") or "")
        if not self.session_id:
            raise WorkflowFailure("create session response omitted session_id")
        self.coordinator.mark_started(
            account=self.account,
            session_id=self.session_id,
        )
        self.marked_started = True
        self._heartbeat()

        self._json_request(
            "PATCH",
            f"/api/v1/setup/{self.session_id}/profile",
            name="02 setup/profile",
            json=profile_payload(self.account),
        )
        self._heartbeat()
        draft = self._json_request(
            "POST",
            f"/api/v1/setup/{self.session_id}/intent-performance-draft",
            name="03 setup/intent-performance-draft",
            json={"intent_id": "development"},
            timeout=(20, 240),
        )
        performance_items = draft.get("performance_items")
        if not isinstance(performance_items, list) or len(performance_items) != 3:
            raise WorkflowFailure(
                "intent draft must return exactly three performance items"
            )
        self._json_request(
            "PATCH",
            f"/api/v1/setup/{self.session_id}/intent",
            name="04 setup/intent",
            json=intent_request("development", performance_items),
            timeout=(20, 240),
        )
        self._json_request(
            "PATCH",
            f"/api/v1/setup/{self.session_id}/simulation",
            name="05 setup/simulation",
            json=simulation_payload(),
        )
        completed_setup = self._json_request(
            "POST",
            f"/api/v1/setup/{self.session_id}/complete",
            name="06 setup/complete",
        )
        if not completed_setup.get("setup_ready"):
            raise WorkflowFailure("setup did not become ready")

        self._heartbeat()
        self._sse_request(
            f"/api/v1/guidance/{self.session_id}/stream",
            flow="guidance",
            name="07 guidance/stream",
        )
        for index, message in enumerate(MANAGER_MESSAGES, start=1):
            self._heartbeat()
            self._sse_request(
                f"/api/v1/rehearsal/{self.session_id}/message/stream",
                flow="rehearsal",
                name=f"08 rehearsal/message/{index}",
                json={"message": message},
            )
        self._json_request(
            "POST",
            f"/api/v1/rehearsal/{self.session_id}/end",
            name="09 rehearsal/end",
        )
        self._heartbeat()
        self._sse_request(
            f"/api/v1/reports/{self.session_id}/coach/stream",
            flow="coach",
            name="10 coach/stream",
            timeout=(20, 420),
        )
        self._validate_isolation()
        return self.session_id

    def logout_best_effort(self) -> None:
        try:
            self.client.post(
                "/api/v1/auth/logout",
                name="11 auth/logout",
                headers=self.headers,
                timeout=(10, 30),
            )
        except Exception:
            pass

    def _login(self) -> None:
        result = self._json_request(
            "POST",
            "/api/v1/auth/login",
            name="00 auth/login",
            json={
                "email": self.account.email,
                "password": self.account.password,
            },
        )
        returned_email = str(
            (result.get("user") or {}).get("email") or ""
        ).lower()
        if returned_email != self.account.email:
            raise WorkflowFailure(
                "login response returned a different user"
            )

    def _validate_isolation(self) -> None:
        assert self.session_id is not None
        state = self._json_request(
            "GET",
            f"/api/v1/sessions/{self.session_id}",
            name="10 sessions/isolation-check",
        )
        marker = f"LT-{self.account.index:04d}"
        profile = state.get("employee_profile") or {}
        if profile.get("employee_id") != marker:
            raise WorkflowFailure(
                "cross-user profile isolation check failed"
            )
        conversation = state.get("conversation") or []
        if len(conversation) < len(MANAGER_MESSAGES) * 2:
            raise WorkflowFailure(
                "conversation is missing one or more rehearsal turns"
            )
        manager_text = "\n".join(
            str(turn.get("text") or "")
            for turn in conversation
            if str(turn.get("speaker") or "").lower() == "manager"
        )
        if any(message not in manager_text for message in MANAGER_MESSAGES):
            raise WorkflowFailure(
                "conversation contains missing or cross-session manager turns"
            )

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        name: str,
        json: dict[str, Any] | None = None,
        timeout: tuple[int, int] = (20, 120),
    ) -> dict[str, Any]:
        error: str | None = None
        payload: dict[str, Any] | None = None
        with self.client.request(
            method,
            path,
            name=name,
            headers=self.headers,
            json=json,
            timeout=timeout,
            catch_response=True,
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                error = f"{name} returned HTTP {response.status_code}"
                response.failure(error)
            else:
                try:
                    decoded = response.json()
                    if not isinstance(decoded, dict):
                        raise ValueError("JSON root is not an object")
                    payload = decoded
                    response.success()
                except (ValueError, TypeError) as exc:
                    error = (
                        f"{name} returned invalid JSON: "
                        f"{type(exc).__name__}"
                    )
                    response.failure(error)
        if error is not None or payload is None:
            raise WorkflowFailure(
                error or f"{name} returned no payload"
            )
        return payload

    def _sse_request(
        self,
        path: str,
        *,
        flow: str,
        name: str,
        json: dict[str, Any] | None = None,
        timeout: tuple[int, int] = (20, 300),
    ) -> StreamObservation:
        request_started = time.perf_counter()
        observation = StreamObservation(
            flow=flow,
            started_at=request_started,
        )
        parser = SSEParser()
        error: str | None = None
        with self.client.post(
            path,
            name=name,
            headers=self.headers,
            json=json,
            timeout=timeout,
            stream=True,
            catch_response=True,
        ) as response:
            connect_ms = (
                time.perf_counter() - request_started
            ) * 1000.0
            if response.status_code < 200 or response.status_code >= 300:
                error = f"{name} returned HTTP {response.status_code}"
            else:
                try:
                    for chunk in response.iter_content(chunk_size=4096):
                        if not chunk:
                            continue
                        observation.response_bytes += len(chunk)
                        for event in parser.feed(chunk):
                            observation.observe(event)
                    for event in parser.finish():
                        observation.observe(event)
                    observation.validate()
                except (SSEProtocolError, OSError, ValueError) as exc:
                    error = (
                        f"{name} failed stream validation: {exc}"
                    )
            if error is None:
                response.success()
            else:
                response.failure(error)
        self._record_phase(name, "connect", connect_ms)
        if observation.first_event_ms is not None:
            self._record_phase(
                name,
                "first_event",
                observation.first_event_ms,
            )
        if observation.first_content_ms is not None:
            self._record_phase(
                name,
                "first_content",
                observation.first_content_ms,
            )
        if observation.completed_ms is not None:
            self._record_phase(
                name,
                "complete",
                observation.completed_ms,
            )
        if error is not None:
            raise WorkflowFailure(error)
        return observation

    def _record_phase(
        self,
        request_name: str,
        phase: str,
        elapsed_ms: float,
    ) -> None:
        events.request.fire(
            request_type="SSE_PHASE",
            name=f"{request_name}/{phase}",
            response_time=elapsed_ms,
            response_length=0,
            exception=None,
            context={
                "run_id": self.run_id,
                "account_index": self.account.index,
            },
        )

    def _heartbeat(self) -> None:
        self.coordinator.heartbeat_account(self.account)

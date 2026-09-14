from __future__ import annotations

from types import SimpleNamespace

from locust import events

from loadtests.accounts import LoadTestAccount
from loadtests.workflow import FullWorkflowRunner


class _Coordinator:
    def __init__(self) -> None:
        self.started: tuple[str, str] | None = None
        self.heartbeats = 0

    def mark_started(self, *, account: LoadTestAccount, session_id: str) -> None:
        self.started = (account.email, session_id)

    def mark_attempted(self) -> None:
        pass

    def heartbeat_account(self, _account: LoadTestAccount) -> None:
        self.heartbeats += 1


class _Workflow(FullWorkflowRunner):
    def __init__(self) -> None:
        account = LoadTestAccount(1, "loadtest0001@loadtest.invalid", "password", "User")
        coordinator = _Coordinator()
        super().__init__(
            user=SimpleNamespace(client=object()),
            account=account,
            coordinator=coordinator,
            run_id="run-1",
        )
        self.calls: list[str] = []

    def _login(self) -> None:
        self.calls.append("login")

    def _json_request(self, _method: str, _path: str, *, name: str, **_kwargs):
        self.calls.append(name)
        if name == "01 sessions/create":
            return {"session_id": "session-1"}
        if name == "03 setup/intent-performance-draft":
            return {"performance_items": [{"item": index} for index in range(3)]}
        if name == "06 setup/complete":
            return {"setup_ready": True}
        return {}

    def _sse_request(self, _path: str, *, name: str, **_kwargs):
        self.calls.append(name)
        return SimpleNamespace()

    def _validate_isolation(self) -> None:
        self.calls.append("isolation")


def test_full_workflow_invokes_all_business_stages(monkeypatch) -> None:
    monkeypatch.setenv("LOADTEST_MODEL_MODE", "stub")
    workflow = _Workflow()
    assert workflow.run() == "session-1"
    assert workflow.calls[0:4] == [
        "login",
        "01 sessions/create",
        "02 setup/profile",
        "03 setup/intent-performance-draft",
    ]
    assert sum(name.startswith("08 rehearsal/message/") for name in workflow.calls) == 5
    assert "07 guidance/stream" in workflow.calls
    assert "09 rehearsal/end" in workflow.calls
    assert "10 coach/stream" in workflow.calls
    assert workflow.calls[-1] == "isolation"
    assert workflow.coordinator.started == (workflow.account.email, "session-1")


def test_sse_phase_is_recorded_as_locust_custom_request(monkeypatch) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(events.request, "fire", lambda **payload: observed.append(payload))
    workflow = _Workflow()
    workflow._record_phase("07 guidance/stream", "first_content", 123.4)
    assert observed == [
        {
            "request_type": "SSE_PHASE",
            "name": "07 guidance/stream/first_content",
            "response_time": 123.4,
            "response_length": 0,
            "exception": None,
            "context": {"run_id": "run-1", "account_index": 1},
        }
    ]

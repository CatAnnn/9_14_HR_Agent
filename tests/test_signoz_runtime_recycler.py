from __future__ import annotations

from typing import Any

from backend.services.signoz_runtime_recycler import (
    SignozRecycleConfig,
    SignozRuntimeRecycler,
)


class FakeRuntimeEngine:
    def __init__(self):
        self.states: dict[tuple[str, str], dict[str, Any]] = {}
        self.starts: list[tuple[str, str]] = []
        self.stops: list[tuple[str, str]] = []

    def add(
        self,
        project_name: str,
        service_name: str,
        *,
        running: bool,
        health: str | None = "healthy",
    ) -> None:
        self.states[(project_name, service_name)] = {
            "id": f"{project_name}-{service_name}",
            "exists": True,
            "running": running,
            "status": "running" if running else "exited",
            "health": health if running else None,
        }

    def state(self, project_name: str, service_name: str) -> dict[str, Any]:
        return dict(
            self.states.get(
                (project_name, service_name),
                {
                    "id": "",
                    "exists": False,
                    "running": False,
                    "status": "missing",
                    "health": None,
                },
            )
        )

    def start(self, project_name: str, service_name: str) -> bool:
        state = self.states.get((project_name, service_name))
        if state is None:
            return False
        self.starts.append((project_name, service_name))
        state.update(running=True, status="running", health="healthy")
        return True

    def stop(self, project_name: str, service_name: str) -> bool:
        state = self.states.get((project_name, service_name))
        if state is None:
            return False
        self.stops.append((project_name, service_name))
        state.update(running=False, status="exited", health=None)
        return True


def _runtime() -> tuple[SignozRuntimeRecycler, FakeRuntimeEngine, list[float]]:
    engine = FakeRuntimeEngine()
    clock = [0.0]
    config = SignozRecycleConfig(
        enabled=True,
        idle_ttl_seconds=10,
        check_interval_seconds=1,
        start_timeout_seconds=1,
        health_poll_interval_seconds=0.001,
    )
    recycler = SignozRuntimeRecycler(
        config,
        engine,
        clock=lambda: clock[0],
    )
    for service_name in recycler.APP_ACTIVITY_SERVICES:
        engine.add(config.app_project_name, service_name, running=False)
    engine.add(
        config.app_project_name,
        recycler.APP_COLLECTOR_SERVICE,
        running=True,
        health=None,
    )
    for service_name in recycler.core_services:
        engine.add(config.signoz_project_name, service_name, running=True)
    return recycler, engine, clock


def test_signoz_recycles_after_app_idle_and_recovers_when_app_returns():
    recycler, engine, clock = _runtime()

    recycler.run_once()
    clock[0] = 9
    recycler.run_once()
    assert engine.stops == []

    clock[0] = 11
    recycler.run_once()
    assert engine.stops == [
        ("06-emotion-main", "signoz_collection_agent"),
        *(("signoz", service_name) for service_name in recycler.CORE_STOP_ORDER),
    ]

    engine.states[("06-emotion-main", "backend")].update(
        running=True,
        status="running",
        health="healthy",
    )
    clock[0] = 12
    recycler.run_once()

    expected_core_starts = [
        ("signoz", service_name)
        for phase in recycler.CORE_START_PHASES
        for service_name in phase
    ]
    assert engine.starts == [
        *expected_core_starts,
        ("06-emotion-main", "signoz_collection_agent"),
    ]


def test_signoz_recycle_config_defaults_to_unique_app_project(monkeypatch):
    monkeypatch.delenv("SIGNOZ_APP_PROJECT_NAME", raising=False)

    assert SignozRecycleConfig().app_project_name == "06-emotion-main"
    assert SignozRecycleConfig.from_env().app_project_name == "06-emotion-main"

    monkeypatch.setenv("SIGNOZ_APP_PROJECT_NAME", "   ")
    assert SignozRecycleConfig.from_env().app_project_name == "06-emotion-main"


def test_signoz_recycle_can_be_disabled():
    recycler, engine, clock = _runtime()
    recycler.config = SignozRecycleConfig(enabled=False, idle_ttl_seconds=1)
    clock[0] = 100

    recycler.run_once()

    assert engine.starts == []
    assert engine.stops == []

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


class DockerRuntimeError(RuntimeError):
    """Raised when the restricted SigNoz Docker operation fails."""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SignozRecycleConfig:
    enabled: bool = True
    app_project_name: str = "06-emotion-main"
    signoz_project_name: str = "signoz"
    docker_socket_path: str = "/var/run/docker.sock"
    idle_ttl_seconds: float = 900.0
    check_interval_seconds: float = 10.0
    start_timeout_seconds: float = 180.0
    health_poll_interval_seconds: float = 2.0
    stop_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "SignozRecycleConfig":
        return cls(
            enabled=_env_bool("SIGNOZ_AUTO_RECYCLE_ENABLED", True),
            app_project_name=(
                os.getenv("SIGNOZ_APP_PROJECT_NAME", "06-emotion-main").strip()
                or "06-emotion-main"
            ),
            signoz_project_name=os.getenv("SIGNOZ_PROJECT_NAME", "signoz").strip() or "signoz",
            docker_socket_path=os.getenv(
                "SIGNOZ_DOCKER_SOCKET_PATH",
                "/var/run/docker.sock",
            ),
            idle_ttl_seconds=max(
                1.0,
                float(os.getenv("SIGNOZ_IDLE_TTL_SECONDS", "900")),
            ),
            check_interval_seconds=max(
                1.0,
                float(os.getenv("SIGNOZ_RECYCLE_CHECK_INTERVAL_SECONDS", "10")),
            ),
            start_timeout_seconds=max(
                1.0,
                float(os.getenv("SIGNOZ_START_TIMEOUT_SECONDS", "180")),
            ),
            health_poll_interval_seconds=max(
                0.1,
                float(os.getenv("SIGNOZ_HEALTH_POLL_INTERVAL_SECONDS", "2")),
            ),
            stop_timeout_seconds=max(
                1.0,
                float(os.getenv("SIGNOZ_STOP_TIMEOUT_SECONDS", "30")),
            ),
        )


class RuntimeEngine(Protocol):
    def state(self, project_name: str, service_name: str) -> dict[str, Any]: ...

    def start(self, project_name: str, service_name: str) -> bool: ...

    def stop(self, project_name: str, service_name: str) -> bool: ...


class DockerEngineServiceController:
    """Docker Engine adapter limited to Compose project/service lookups."""

    def __init__(self, socket_path: str, stop_timeout_seconds: float):
        self.socket_path = socket_path
        self.stop_timeout_seconds = stop_timeout_seconds

    def state(self, project_name: str, service_name: str) -> dict[str, Any]:
        container = self._find_container(project_name, service_name)
        if container is None:
            return {
                "id": "",
                "exists": False,
                "running": False,
                "status": "missing",
                "health": None,
            }
        container_id = str(container.get("Id") or "").strip()
        payload = self._request("GET", f"/containers/{container_id}/json").json()
        docker_state = payload.get("State") if isinstance(payload, dict) else None
        docker_state = docker_state if isinstance(docker_state, dict) else {}
        health = docker_state.get("Health")
        health = health if isinstance(health, dict) else {}
        return {
            "id": container_id,
            "exists": True,
            "running": bool(docker_state.get("Running")),
            "status": str(docker_state.get("Status") or "unknown"),
            "health": str(health.get("Status")) if health.get("Status") else None,
        }

    def start(self, project_name: str, service_name: str) -> bool:
        state = self.state(project_name, service_name)
        if not state["exists"]:
            return False
        if not state["running"]:
            self._request("POST", f"/containers/{state['id']}/start")
            logger.info("Started SigNoz-related service: %s/%s", project_name, service_name)
        return True

    def stop(self, project_name: str, service_name: str) -> bool:
        state = self.state(project_name, service_name)
        if not state["exists"]:
            return False
        if state["running"]:
            self._request(
                "POST",
                f"/containers/{state['id']}/stop",
                params={"t": int(self.stop_timeout_seconds)},
            )
            logger.info("Stopped idle SigNoz-related service: %s/%s", project_name, service_name)
        return True

    def _find_container(
        self,
        project_name: str,
        service_name: str,
    ) -> dict[str, Any] | None:
        filters = {
            "label": [
                f"com.docker.compose.project={project_name}",
                f"com.docker.compose.service={service_name}",
            ]
        }
        response = self._request(
            "GET",
            "/containers/json",
            params={
                "all": "true",
                "filters": json.dumps(filters, separators=(",", ":")),
            },
        )
        containers = response.json()
        if not isinstance(containers, list):
            raise DockerRuntimeError("Docker returned an invalid container list.")
        valid = [item for item in containers if isinstance(item, dict)]
        if not valid:
            return None
        return max(valid, key=lambda item: int(item.get("Created") or 0))

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not Path(self.socket_path).exists():
            raise DockerRuntimeError(
                f"Docker Engine socket is unavailable: {self.socket_path}"
            )
        transport = httpx.HTTPTransport(uds=self.socket_path)
        timeout = max(30.0, self.stop_timeout_seconds + 5.0)
        with httpx.Client(
            transport=transport,
            base_url="http://docker",
            timeout=timeout,
            trust_env=False,
        ) as client:
            response = client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise DockerRuntimeError(
                f"Docker Engine {method} {path} failed with HTTP "
                f"{response.status_code}: {response.text[:500]}"
            )
        return response


class SignozRuntimeRecycler:
    APP_ACTIVITY_SERVICES = ("backend", "frontend")
    APP_COLLECTOR_SERVICE = "signoz_collection_agent"
    CORE_START_PHASES = (
        (
            "signoz-metastore-postgres-0",
            "signoz-telemetrykeeper-clickhousekeeper-0",
        ),
        ("signoz-telemetrystore-clickhouse-0-0",),
        ("ingester", "signoz-signoz-0"),
    )
    CORE_STOP_ORDER = (
        "ingester",
        "signoz-signoz-0",
        "signoz-telemetrystore-clickhouse-0-0",
        "signoz-telemetrykeeper-clickhousekeeper-0",
        "signoz-metastore-postgres-0",
    )

    def __init__(
        self,
        config: SignozRecycleConfig,
        engine: RuntimeEngine | None = None,
        *,
        clock=time.monotonic,
    ):
        self.config = config
        self.engine = engine or DockerEngineServiceController(
            config.docker_socket_path,
            config.stop_timeout_seconds,
        )
        self._clock = clock
        self._last_app_active_at = clock()
        self._recycled = False

    @property
    def core_services(self) -> tuple[str, ...]:
        return tuple(
            service_name
            for phase in self.CORE_START_PHASES
            for service_name in phase
        )

    def run_once(self, *, now: float | None = None) -> None:
        if not self.config.enabled:
            return
        current_time = self._clock() if now is None else now
        app_active = self._app_is_active()
        core_running = self._any_core_service_running()

        if app_active:
            self._last_app_active_at = current_time
            if not self._all_core_services_running():
                self._start_observability()
            else:
                self._start_collector_if_available()
            self._recycled = False
            return

        if not core_running:
            self._recycled = True
            return

        if self._recycled:
            # A manual wake receives a fresh idle window before recycling again.
            self._recycled = False
            self._last_app_active_at = current_time
            return

        if current_time - self._last_app_active_at < self.config.idle_ttl_seconds:
            return

        self._stop_observability()
        self._recycled = True

    def run(self, stop_event: threading.Event) -> None:
        if not self.config.enabled:
            logger.info("SigNoz automatic recycle is disabled.")
            stop_event.wait()
            return
        logger.info(
            "SigNoz automatic recycle started (idle_ttl=%.0fs, app_project=%s).",
            self.config.idle_ttl_seconds,
            self.config.app_project_name,
        )
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("SigNoz automatic recycle check failed.")
            stop_event.wait(self.config.check_interval_seconds)

    def _app_is_active(self) -> bool:
        return any(
            self.engine.state(self.config.app_project_name, service_name)["running"]
            for service_name in self.APP_ACTIVITY_SERVICES
        )

    def _any_core_service_running(self) -> bool:
        return any(
            self.engine.state(self.config.signoz_project_name, service_name)["running"]
            for service_name in self.core_services
        )

    def _all_core_services_running(self) -> bool:
        return all(
            self.engine.state(self.config.signoz_project_name, service_name)["running"]
            for service_name in self.core_services
        )

    def _start_observability(self) -> None:
        for phase in self.CORE_START_PHASES:
            for service_name in phase:
                if not self.engine.start(self.config.signoz_project_name, service_name):
                    raise DockerRuntimeError(
                        f"SigNoz service container is missing: {service_name}"
                    )
            for service_name in phase:
                self._wait_until_ready(self.config.signoz_project_name, service_name)
        self._start_collector_if_available()

    def _start_collector_if_available(self) -> None:
        state = self.engine.state(
            self.config.app_project_name,
            self.APP_COLLECTOR_SERVICE,
        )
        if state["exists"] and not state["running"]:
            self.engine.start(
                self.config.app_project_name,
                self.APP_COLLECTOR_SERVICE,
            )

    def _stop_observability(self) -> None:
        collector = self.engine.state(
            self.config.app_project_name,
            self.APP_COLLECTOR_SERVICE,
        )
        if collector["exists"]:
            self.engine.stop(
                self.config.app_project_name,
                self.APP_COLLECTOR_SERVICE,
            )
        for service_name in self.CORE_STOP_ORDER:
            self.engine.stop(self.config.signoz_project_name, service_name)

    def _wait_until_ready(self, project_name: str, service_name: str) -> None:
        deadline = self._clock() + self.config.start_timeout_seconds
        last_state: dict[str, Any] = {}
        while self._clock() < deadline:
            last_state = self.engine.state(project_name, service_name)
            if last_state["running"] and last_state["health"] in {None, "healthy"}:
                return
            if last_state["health"] == "unhealthy":
                break
            time.sleep(self.config.health_poll_interval_seconds)
        raise DockerRuntimeError(
            f"Service {project_name}/{service_name} did not become ready "
            f"within {self.config.start_timeout_seconds:.0f}s (state={last_state})."
        )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    SignozRuntimeRecycler(SignozRecycleConfig.from_env()).run(stop_event)


if __name__ == "__main__":
    main()

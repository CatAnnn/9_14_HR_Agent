from __future__ import annotations

import json
import math
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

import httpx
from fastapi import FastAPI, HTTPException

from backend.observability.gpu_metrics import initialize_gpu_metrics

logger = logging.getLogger(__name__)

_ALLOWED_SERVICES = frozenset(
    {"qwen_embedding", "qwen_reranker", "qwen3_asr", "fish_tts"}
)


class DockerRuntimeControllerError(RuntimeError):
    """Raised when the restricted Docker lifecycle operation fails."""


class DockerRuntimeControllerBusyError(DockerRuntimeControllerError):
    """Raised when a model still has an active cross-process request lease."""


class DockerRuntimeActivityLeaseNotFoundError(DockerRuntimeControllerError):
    """Raised when an activity lease expired or belongs to another controller."""


class DockerModelRuntimeController:
    """Restricted Docker Engine adapter for managed local inference services."""

    def __init__(
        self,
        *,
        project_name: str | None = None,
        socket_path: str | None = None,
        stop_timeout_seconds: float | None = None,
        activity_lease_ttl_seconds: float | None = None,
    ):
        self.project_name = project_name or os.getenv("LOCAL_MODEL_DOCKER_PROJECT_NAME", "06")
        self.socket_path = socket_path or os.getenv(
            "LOCAL_MODEL_DOCKER_SOCKET_PATH",
            "/var/run/docker.sock",
        )
        raw_stop_timeout = stop_timeout_seconds
        if raw_stop_timeout is None:
            raw_stop_timeout = float(os.getenv("LOCAL_MODEL_STOP_TIMEOUT_SECONDS", "30"))
        self.stop_timeout_seconds = max(1.0, float(raw_stop_timeout))
        raw_activity_ttl = activity_lease_ttl_seconds
        if raw_activity_ttl is None:
            raw_activity_ttl = float(
                os.getenv("LOCAL_MODEL_ACTIVITY_LEASE_TTL_SECONDS", "90")
            )
        self.activity_lease_ttl_seconds = max(3.0, float(raw_activity_ttl))
        self._activity_leases: dict[str, dict[str, float]] = {
            service_name: {} for service_name in _ALLOWED_SERVICES
        }
        self._last_activity_at = {
            service_name: time.monotonic() for service_name in _ALLOWED_SERVICES
        }
        # Registration/renewal and stop must be ordered by the same lock. If a
        # stop wins, a later request will observe the stopped service and start
        # it; if a request wins, stop is rejected until its lease is released.
        self._service_locks = {
            service_name: threading.RLock() for service_name in _ALLOWED_SERVICES
        }

    def ping(self) -> bool:
        response = self._docker_request("GET", "/_ping")
        return response.text.strip().upper() == "OK"

    def state(self, service_name: str) -> dict[str, Any]:
        self._validate_service(service_name)
        container = self._find_container(service_name)
        if container is None:
            return {
                "service": service_name,
                "exists": False,
                "running": False,
                "status": "missing",
                "health": None,
                "error": "",
            }
        container_id = str(container.get("Id") or "").strip()
        data = self._docker_request("GET", f"/containers/{container_id}/json").json()
        docker_state = data.get("State") if isinstance(data, dict) else None
        docker_state = docker_state if isinstance(docker_state, dict) else {}
        health = docker_state.get("Health") if isinstance(docker_state.get("Health"), dict) else None
        return {
            "service": service_name,
            "exists": True,
            "running": bool(docker_state.get("Running")),
            "status": str(docker_state.get("Status") or "unknown"),
            "health": str(health.get("Status")) if health and health.get("Status") else None,
            "error": str(docker_state.get("Error") or "")[:300],
        }

    def start(self, service_name: str) -> dict[str, Any]:
        container_id, current = self._container_for_action(service_name)
        if not current["running"]:
            self._docker_request("POST", f"/containers/{container_id}/start")
            logger.info("Started managed local model service: %s", service_name)
        return self.state(service_name)

    def restart(self, service_name: str) -> dict[str, Any]:
        container_id, _ = self._container_for_action(service_name)
        self._docker_request(
            "POST",
            f"/containers/{container_id}/restart",
            params={"t": int(self.stop_timeout_seconds)},
        )
        logger.info("Restarted managed local model service: %s", service_name)
        return self.state(service_name)

    def stop(
        self, service_name: str, *, idle_ttl_seconds: float = 0.0
    ) -> dict[str, Any]:
        self._validate_service(service_name)
        with self._service_locks[service_name]:
            active_count = self._active_lease_count_locked(service_name)
            if active_count:
                raise DockerRuntimeControllerBusyError(
                    f"Local model service {service_name} has {active_count} active request "
                    "lease(s); refusing to stop it."
                )
            requested_idle_ttl = float(idle_ttl_seconds)
            if not math.isfinite(requested_idle_ttl) or requested_idle_ttl < 0:
                raise ValueError(
                    "idle_ttl_seconds must be a finite non-negative number."
                )
            idle_for = time.monotonic() - self._last_activity_at[service_name]
            if idle_for < requested_idle_ttl:
                raise DockerRuntimeControllerBusyError(
                    f"Local model service {service_name} was used by another backend "
                    f"{idle_for:.1f}s ago; refusing to stop it before the shared "
                    f"{requested_idle_ttl:.1f}s idle TTL."
                )
            container_id, current = self._container_for_action(service_name)
            if current["running"]:
                self._docker_request(
                    "POST",
                    f"/containers/{container_id}/stop",
                    params={"t": int(self.stop_timeout_seconds)},
                )
                logger.info("Stopped managed local model service: %s", service_name)
            return self.state(service_name)

    def acquire_activity_lease(self, service_name: str) -> dict[str, Any]:
        """Register one live model user before it checks/starts the container."""

        self._validate_service(service_name)
        token = uuid.uuid4().hex
        with self._service_locks[service_name]:
            self._purge_expired_activity_leases_locked(service_name)
            now = time.monotonic()
            self._last_activity_at[service_name] = now
            self._activity_leases[service_name][token] = (
                now + self.activity_lease_ttl_seconds
            )
            active_count = len(self._activity_leases[service_name])
        return self._activity_lease_response(token, active_count)

    def renew_activity_lease(self, service_name: str, token: str) -> dict[str, Any]:
        """Extend an activity lease while a request or logical session is live."""

        self._validate_service(service_name)
        normalized_token = self._validate_activity_token(token)
        with self._service_locks[service_name]:
            self._purge_expired_activity_leases_locked(service_name)
            leases = self._activity_leases[service_name]
            if normalized_token not in leases:
                raise DockerRuntimeActivityLeaseNotFoundError(
                    f"Activity lease for {service_name} was not found or has expired."
                )
            now = time.monotonic()
            leases[normalized_token] = now + self.activity_lease_ttl_seconds
            self._last_activity_at[service_name] = now
            active_count = len(leases)
        return self._activity_lease_response(normalized_token, active_count)

    def release_activity_lease(self, service_name: str, token: str) -> dict[str, Any]:
        """Release a request lease; deletion is idempotent for cleanup safety."""

        self._validate_service(service_name)
        normalized_token = self._validate_activity_token(token)
        with self._service_locks[service_name]:
            leases = self._activity_leases[service_name]
            released = leases.pop(normalized_token, None) is not None
            if released:
                self._last_activity_at[service_name] = time.monotonic()
            self._purge_expired_activity_leases_locked(service_name)
            active_count = len(leases)
        return {"released": released, "active_leases": active_count}

    def _activity_lease_response(self, token: str, active_count: int) -> dict[str, Any]:
        return {
            "token": token,
            "ttl_seconds": self.activity_lease_ttl_seconds,
            "renew_after_seconds": max(1.0, self.activity_lease_ttl_seconds / 3.0),
            "active_leases": active_count,
        }

    def _active_lease_count_locked(self, service_name: str) -> int:
        self._purge_expired_activity_leases_locked(service_name)
        return len(self._activity_leases[service_name])

    def _purge_expired_activity_leases_locked(self, service_name: str) -> None:
        now = time.monotonic()
        leases = self._activity_leases[service_name]
        expired_tokens = [
            token for token, expires_at in leases.items() if expires_at <= now
        ]
        for token in expired_tokens:
            leases.pop(token, None)

    @staticmethod
    def _validate_activity_token(token: str) -> str:
        normalized = str(token or "").strip()
        if len(normalized) != 32 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("Invalid local model activity lease token.")
        return normalized

    def _container_for_action(self, service_name: str) -> tuple[str, dict[str, Any]]:
        current = self.state(service_name)
        if not current["exists"]:
            raise DockerRuntimeControllerError(
                f"Compose service {service_name} has no container. "
                f"Create it once with `docker compose up --no-start {service_name}`."
            )
        container = self._find_container(service_name)
        container_id = str((container or {}).get("Id") or "").strip()
        if not container_id:
            raise DockerRuntimeControllerError(f"Docker returned an invalid container for {service_name}.")
        return container_id, current

    def _find_container(self, service_name: str) -> dict[str, Any] | None:
        filters = {
            "label": [
                f"com.docker.compose.project={self.project_name}",
                f"com.docker.compose.service={service_name}",
                "com.docker.compose.oneoff=False",
            ]
        }
        response = self._docker_request(
            "GET",
            "/containers/json",
            params={"all": "true", "filters": json.dumps(filters, separators=(",", ":"))},
        )
        containers = response.json()
        if not isinstance(containers, list):
            raise DockerRuntimeControllerError("Docker returned an invalid container list.")
        valid = []
        for item in containers:
            if not isinstance(item, dict):
                continue
            labels = item.get("Labels")
            if not isinstance(labels, dict):
                continue
            if str(labels.get("com.docker.compose.project") or "") != self.project_name:
                continue
            if str(labels.get("com.docker.compose.service") or "") != service_name:
                continue
            if str(labels.get("com.docker.compose.oneoff") or "").casefold() != "false":
                continue
            valid.append(item)
        if not valid:
            return None
        return max(valid, key=lambda item: int(item.get("Created") or 0))

    def _validate_service(self, service_name: str) -> None:
        if service_name not in _ALLOWED_SERVICES:
            raise ValueError(f"Unsupported local model service: {service_name}")

    def _docker_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not Path(self.socket_path).exists():
            raise DockerRuntimeControllerError(f"Docker Engine socket is unavailable: {self.socket_path}")
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
            raise DockerRuntimeControllerError(
                f"Docker Engine {method} {path} failed with HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        return response


initialize_gpu_metrics()
controller = DockerModelRuntimeController()
app = FastAPI(title="Local Model Runtime Controller", docs_url=None, redoc_url=None)


def _run(action):
    try:
        return action()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DockerRuntimeControllerBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DockerRuntimeActivityLeaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DockerRuntimeControllerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, str]:
    if not _run(controller.ping):
        raise HTTPException(status_code=503, detail="Docker Engine did not answer OK.")
    return {"status": "ok"}


@app.get("/services/{service_name}")
def service_state(service_name: str) -> dict[str, Any]:
    return _run(lambda: controller.state(service_name))


@app.post("/services/{service_name}/start")
def start_service(service_name: str) -> dict[str, Any]:
    return _run(lambda: controller.start(service_name))


@app.post("/services/{service_name}/restart")
def restart_service(service_name: str) -> dict[str, Any]:
    return _run(lambda: controller.restart(service_name))


@app.post("/services/{service_name}/stop")
def stop_service(
    service_name: str, idle_ttl_seconds: float = 0.0
) -> dict[str, Any]:
    return _run(lambda: controller.stop(service_name, idle_ttl_seconds=idle_ttl_seconds))


@app.post("/services/{service_name}/activity-leases")
def acquire_service_activity_lease(service_name: str) -> dict[str, Any]:
    return _run(lambda: controller.acquire_activity_lease(service_name))


@app.put("/services/{service_name}/activity-leases/{token}")
def renew_service_activity_lease(service_name: str, token: str) -> dict[str, Any]:
    return _run(lambda: controller.renew_activity_lease(service_name, token))


@app.delete("/services/{service_name}/activity-leases/{token}")
def release_service_activity_lease(service_name: str, token: str) -> dict[str, Any]:
    return _run(lambda: controller.release_activity_lease(service_name, token))

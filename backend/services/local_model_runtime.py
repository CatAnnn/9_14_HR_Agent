from __future__ import annotations

import asyncio
import json
import logging
import shlex
import subprocess
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx

from backend.config.settings import Settings, get_settings
from backend.services.http_client import get_shared_sync_client

logger = logging.getLogger(__name__)


_SERVICE_NAMES = frozenset(
    {
        "qwen_embedding",
        "qwen_reranker",
        "qwen3_asr",
    }
)
_CONTROLLED_SERVICE_NAMES = _SERVICE_NAMES | {"fish_tts"}


class LocalModelRuntimeError(RuntimeError):
    """Raised when a managed local model container cannot become available."""


class LocalModelRuntimeBusyError(LocalModelRuntimeError):
    """Raised when another backend still owns a model activity lease."""


@dataclass(frozen=True)
class _RecycleCommand:
    argv: list[str]
    cwd: str
    timeout_seconds: float


@dataclass
class _ControllerActivityLease:
    token: str
    renew_after_seconds: float
    stop_event: threading.Event


class LocalModelRuntimeRecycler:
    """Start local Qwen services on demand and stop them after an idle period.

    A lease protects an in-flight model request from idle recycling. Container
    control is restricted to the managed Compose services and resolved via
    Compose labels instead of accepting arbitrary container names.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.local_model_auto_recycle_enabled
        self.auto_start_enabled = settings.local_model_auto_start_enabled
        self.configured_services = settings.configured_local_model_services
        required_resident_services = {"qwen3_asr"} & self.configured_services
        self.required_resident_services = frozenset(required_resident_services)
        requested_pinned = set(settings.local_model_pinned_service_set)
        requested_pinned.update(self.required_resident_services)
        unsupported_pinned = requested_pinned - _SERVICE_NAMES
        if unsupported_pinned:
            raise ValueError(
                "Unsupported pinned local model service(s): "
                + ", ".join(sorted(unsupported_pinned))
            )
        disabled_pinned = requested_pinned - self.configured_services
        if disabled_pinned:
            logger.info(
                "Ignoring pinned local model service(s) disabled by provider configuration: %s",
                ", ".join(sorted(disabled_pinned)),
            )
            requested_pinned.difference_update(disabled_pinned)
        self.pinned_services = frozenset(requested_pinned)
        self.idle_ttl_seconds = max(1, int(settings.local_model_idle_ttl_seconds))
        self.check_interval_seconds = max(1, int(settings.local_model_recycle_check_interval_seconds))
        self.start_timeout_seconds = max(1.0, float(settings.local_model_start_timeout_seconds))
        self.health_poll_interval_seconds = max(0.1, float(settings.local_model_health_poll_interval_seconds))
        self._last_used_at: dict[str, float] = {}
        self._active_uses: dict[str, int] = {service_name: 0 for service_name in _SERVICE_NAMES}
        self._session_leases: dict[str, set[str]] = {}
        self._stopped_after_use: set[str] = set()
        self._warming_services: set[str] = set()
        self._controller_activity_leases: dict[
            str, _ControllerActivityLease
        ] = {}
        self._lock = threading.Lock()
        self._service_locks = {service_name: threading.Lock() for service_name in _SERVICE_NAMES}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @contextmanager
    def lease(self, service_name: str) -> Iterator[None]:
        """Ensure a service is ready and protect it for one model request."""

        self._validate_service_name(service_name)
        self._validate_service_enabled(service_name)
        if not self.enabled:
            yield
            return

        service_lock = self._service_locks[service_name]
        with service_lock:
            self._ensure_monitor_started()
            with self._lock:
                service_is_in_use = self._active_uses[service_name] > 0
            controller_lease_acquired = False
            try:
                if (
                    not service_is_in_use
                    and self.settings.local_model_controller_url.strip()
                ):
                    self._acquire_controller_activity_lease(service_name)
                    controller_lease_acquired = True
                if self.auto_start_enabled and not service_is_in_use:
                    self._ensure_service_ready(service_name)
                with self._lock:
                    self._active_uses[service_name] += 1
                    self._last_used_at[service_name] = time.monotonic()
                    self._stopped_after_use.discard(service_name)
            except Exception:
                if controller_lease_acquired:
                    self._release_controller_activity_lease(service_name)
                raise

        try:
            yield
        finally:
            with service_lock:
                with self._lock:
                    self._active_uses[service_name] = max(
                        0, self._active_uses[service_name] - 1
                    )
                    service_became_idle = self._active_uses[service_name] == 0
                    self._last_used_at[service_name] = time.monotonic()
                if service_became_idle:
                    self._release_controller_activity_lease(service_name)

    @asynccontextmanager
    async def async_lease(self, service_name: str):
        """Async wrapper that keeps Docker readiness checks off the event loop."""

        lease = self.lease(service_name)
        await asyncio.to_thread(lease.__enter__)
        try:
            yield
        finally:
            await asyncio.to_thread(lease.__exit__, None, None, None)

    def acquire_session_lease(self, session_id: str, service_names: Iterable[str]) -> None:
        """Prevent selected services from idle recycling for a logical session."""

        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            raise ValueError("session_id is required for a local model session lease.")
        normalized_services = tuple(
            dict.fromkeys(str(name).strip() for name in service_names if str(name).strip())
        )
        for service_name in normalized_services:
            self._validate_service_name(service_name)
            self._validate_service_enabled(service_name)
        if not self.enabled or not normalized_services:
            return

        self._ensure_monitor_started()
        now = time.monotonic()
        with self._lock:
            held_services = self._session_leases.setdefault(normalized_session_id, set())
            for service_name in normalized_services:
                if service_name not in held_services:
                    held_services.add(service_name)
                    self._active_uses[service_name] += 1
                self._last_used_at[service_name] = now
                self._stopped_after_use.discard(service_name)

    def release_session_lease(self, session_id: str) -> None:
        """Release a logical session and start the normal idle TTL from now."""

        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id or not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            held_services = self._session_leases.pop(normalized_session_id, set())
            for service_name in held_services:
                self._active_uses[service_name] = max(0, self._active_uses[service_name] - 1)
                self._last_used_at[service_name] = now

    def start(self) -> None:
        """Start recovery monitoring and adopt any models left running."""

        if not self.enabled and not self.required_resident_services:
            return
        if self.enabled:
            self._reconcile_disabled_services()
            self._ensure_monitor_started()
            now = time.monotonic()
            with self._lock:
                for service_name in self.configured_services:
                    self._last_used_at.setdefault(service_name, now)
                    self._stopped_after_use.discard(service_name)

        startup_services = (
            self.pinned_services
            if self.auto_start_enabled
            else self.required_resident_services
        )
        for service_name in sorted(startup_services):
            self.ensure_resident(service_name)

    def ensure_resident(self, service_name: str) -> bool:
        """Start a pinned service once in the background and protect it from idle recycling."""

        self._validate_service_name(service_name)
        self._validate_service_enabled(service_name)
        if service_name not in self.pinned_services:
            return False
        if not self.auto_start_enabled and service_name not in self.required_resident_services:
            return False
        with self._lock:
            if service_name in self._warming_services:
                return False
            self._warming_services.add(service_name)
        thread = threading.Thread(
            target=self._warm_pinned_service,
            args=(service_name,),
            name=f"local-model-pinned-{service_name}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._warming_services.discard(service_name)
            raise
        return True

    def shutdown(self) -> None:
        """Stop the monitor thread without changing container state."""

        self._stop_event.set()

    def _validate_service_name(self, service_name: str) -> None:
        if service_name not in _SERVICE_NAMES:
            raise ValueError(f"Unsupported local model service: {service_name}")

    def _validate_controlled_service_name(self, service_name: str) -> None:
        if service_name not in _CONTROLLED_SERVICE_NAMES:
            raise ValueError(f"Unsupported local model service: {service_name}")

    def _validate_service_enabled(self, service_name: str) -> None:
        if service_name not in self.configured_services:
            raise LocalModelRuntimeError(
                f"Local model service {service_name} is disabled by provider configuration."
            )

    def _ensure_monitor_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._monitor_loop,
                name="local-model-runtime-recycler",
                daemon=True,
            )
            self._thread.start()

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.check_interval_seconds):
            for service_name in self.configured_services:
                self._stop_if_idle(service_name)

    def _reconcile_disabled_services(self) -> None:
        """Stop stale containers excluded by the active provider configuration."""

        enabled_services = set(self.configured_services)
        if self.settings.tts_enabled:
            enabled_services.add("fish_tts")

        for service_name in sorted(_CONTROLLED_SERVICE_NAMES - enabled_services):
            try:
                if self.settings.local_model_controller_url.strip():
                    state = self._controller_state(service_name)
                    if not state.get("exists") or not state.get("running"):
                        continue
                elif self._docker_socket_exists():
                    container = self._find_container(service_name)
                    if container is None:
                        continue
                    container_id = str(container.get("Id") or "").strip()
                    if not container_id or not self._inspect_state(container_id)["running"]:
                        continue
                else:
                    return

                logger.info(
                    "Stopping local model service disabled by runtime configuration: %s",
                    service_name,
                )
                self._stop_service(service_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Unable to reconcile disabled local model service %s: %s",
                    service_name,
                    exc,
                )

    def _stop_if_idle(self, service_name: str) -> None:
        if service_name in self.pinned_services:
            return
        with self._service_locks[service_name]:
            with self._lock:
                last_used_at = self._last_used_at.get(service_name)
                if last_used_at is None:
                    return
                if self._active_uses[service_name] > 0 or service_name in self._stopped_after_use:
                    return
                if time.monotonic() - last_used_at < self.idle_ttl_seconds:
                    return

            stopped = self._stop_service(service_name)
            if stopped is None:
                return
            with self._lock:
                if stopped:
                    self._stopped_after_use.add(service_name)
                else:
                    # Avoid retrying a failed Docker operation on every monitor tick.
                    self._last_used_at[service_name] = time.monotonic()

    def _warm_pinned_service(self, service_name: str) -> None:
        """Start a pinned service in the background without blocking application startup."""

        try:
            with self._service_locks[service_name]:
                self._ensure_service_ready(service_name)
            logger.info("Pinned local model service is ready: %s", service_name)
        except Exception:
            logger.warning(
                "Pinned local model service is not ready yet: %s",
                service_name,
                exc_info=True,
            )
        finally:
            with self._lock:
                self._warming_services.discard(service_name)

    def service_state(self, service_name: str) -> dict[str, Any]:
        """Return current container state for a managed local model service."""

        self._validate_service_name(service_name)
        self._validate_service_enabled(service_name)
        if self.settings.local_model_controller_url.strip():
            return self._controller_state(service_name)
        container = self._find_container(service_name)
        if container is None:
            return {
                "exists": False,
                "running": False,
                "status": "missing",
                "health": None,
            }
        container_id = str(container.get("Id") or "").strip()
        state = self._inspect_state(container_id)
        return {"exists": True, **state}

    def _ensure_service_ready(self, service_name: str) -> None:
        self._validate_service_name(service_name)
        self._validate_service_enabled(service_name)
        if self.settings.local_model_controller_url.strip():
            self._ensure_service_ready_via_controller(service_name)
            return

        container = self._find_container(service_name)
        if container is None:
            raise LocalModelRuntimeError(
                f"Compose service {service_name} has no container. "
                "Create it once with `docker compose up --no-start " + service_name + "`."
            )

        container_id = str(container.get("Id") or "").strip()
        if not container_id:
            raise LocalModelRuntimeError(f"Docker returned an invalid container for {service_name}.")

        state = self._inspect_state(container_id)
        if state["running"] and state["health"] == "healthy":
            return
        if state["running"] and state["health"] is None:
            return
        if state["running"] and state["health"] == "unhealthy":
            logger.warning("Restarting unhealthy local model service: %s", service_name)
            self._docker_request(
                "POST",
                f"/containers/{container_id}/restart",
                params={"t": int(self.settings.local_model_stop_timeout_seconds)},
            )
        elif not state["running"]:
            logger.info("Starting local model service on demand: %s", service_name)
            self._docker_request("POST", f"/containers/{container_id}/start")

        self._wait_until_ready(service_name, container_id)

    def _ensure_service_ready_via_controller(self, service_name: str) -> None:
        state = self._controller_state(service_name)
        if not state.get("exists"):
            raise LocalModelRuntimeError(
                f"Compose service {service_name} has no container. "
                f"Create it once with `docker compose up --no-start {service_name}`."
            )
        if state.get("running") and state.get("health") in {None, "healthy"}:
            return
        if state.get("running") and state.get("health") == "unhealthy":
            logger.warning("Restarting unhealthy local model service: %s", service_name)
            self._controller_request("POST", f"/services/{service_name}/restart")
        elif not state.get("running"):
            logger.info("Starting local model service on demand: %s", service_name)
            self._controller_request("POST", f"/services/{service_name}/start")
        self._wait_until_controller_ready(service_name)

    def _wait_until_controller_ready(self, service_name: str) -> None:
        deadline = time.monotonic() + self.start_timeout_seconds
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_state = self._controller_state(service_name)
            if last_state.get("running") and last_state.get("health") in {None, "healthy"}:
                logger.info("Local model service is ready: %s", service_name)
                return
            if last_state.get("status") in {"dead", "removing", "missing"}:
                break
            time.sleep(self.health_poll_interval_seconds)
        raise LocalModelRuntimeError(
            f"Local model service {service_name} did not become ready within "
            f"{self.start_timeout_seconds:.0f}s (state={last_state})."
        )

    def _wait_until_ready(self, service_name: str, container_id: str) -> None:
        deadline = time.monotonic() + self.start_timeout_seconds
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_state = self._inspect_state(container_id)
            if last_state["running"] and last_state["health"] in {None, "healthy"}:
                logger.info("Local model service is ready: %s", service_name)
                return
            if last_state["status"] in {"dead", "removing"}:
                break
            time.sleep(self.health_poll_interval_seconds)
        raise LocalModelRuntimeError(
            f"Local model service {service_name} did not become ready within "
            f"{self.start_timeout_seconds:.0f}s (state={last_state})."
        )

    def _stop_service(self, service_name: str) -> bool | None:
        try:
            if self.settings.local_model_controller_url.strip():
                self._controller_request(
                    "POST",
                    f"/services/{service_name}/stop",
                    params={"idle_ttl_seconds": self.idle_ttl_seconds},
                )
                logger.info("Stopped local model service: %s", service_name)
                return True
            if self._docker_socket_exists():
                container = self._find_container(service_name)
                if container is None:
                    logger.info("Local model service has no container to stop: %s", service_name)
                    return True
                container_id = str(container.get("Id") or "").strip()
                state = self._inspect_state(container_id)
                if not state["running"]:
                    return True
                self._docker_request(
                    "POST",
                    f"/containers/{container_id}/stop",
                    params={"t": int(self.settings.local_model_stop_timeout_seconds)},
                )
                logger.info("Stopped idle local model service: %s", service_name)
                return True
            return self._stop_service_with_compose(service_name)
        except LocalModelRuntimeBusyError as exc:
            logger.debug(
                "Local model service is still globally active; skip recycle: %s", exc
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Local model recycle failed for %s: %s", service_name, exc)
            return False

    def _request_controller_activity_lease(
        self, service_name: str
    ) -> tuple[str, float]:
        response = self._controller_request(
            "POST", f"/services/{service_name}/activity-leases"
        )
        data = response.json()
        if not isinstance(data, dict):
            raise LocalModelRuntimeError(
                "Local model controller returned an invalid activity lease response."
            )
        token = str(data.get("token") or "").strip()
        if not token:
            raise LocalModelRuntimeError(
                "Local model controller returned an empty activity lease token."
            )
        renew_after_seconds = max(
            1.0, float(data.get("renew_after_seconds") or 30.0)
        )
        return token, renew_after_seconds

    def _acquire_controller_activity_lease(self, service_name: str) -> None:
        token, renew_after_seconds = self._request_controller_activity_lease(
            service_name
        )
        lease = _ControllerActivityLease(
            token=token,
            renew_after_seconds=renew_after_seconds,
            stop_event=threading.Event(),
        )
        with self._lock:
            self._controller_activity_leases[service_name] = lease
        thread = threading.Thread(
            target=self._heartbeat_controller_activity_lease,
            args=(service_name, lease),
            name=f"local-model-activity-{service_name}",
            daemon=True,
        )
        thread.start()

    def _heartbeat_controller_activity_lease(
        self,
        service_name: str,
        lease: _ControllerActivityLease,
    ) -> None:
        while not lease.stop_event.wait(lease.renew_after_seconds):
            with self._lock:
                if self._controller_activity_leases.get(service_name) is not lease:
                    return
                token = lease.token
            try:
                self._controller_request(
                    "PUT",
                    f"/services/{service_name}/activity-leases/{token}",
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Unable to renew local model activity lease for %s: %s",
                    service_name,
                    exc,
                )

            try:
                replacement_token, replacement_interval = (
                    self._request_controller_activity_lease(service_name)
                )
            except Exception:  # noqa: BLE001
                continue

            adopted = False
            with self._lock:
                if (
                    self._controller_activity_leases.get(service_name) is lease
                    and self._active_uses[service_name] > 0
                    and not lease.stop_event.is_set()
                ):
                    lease.token = replacement_token
                    lease.renew_after_seconds = replacement_interval
                    adopted = True
            if not adopted:
                self._best_effort_release_controller_activity_token(
                    service_name, replacement_token
                )

    def _release_controller_activity_lease(self, service_name: str) -> None:
        with self._lock:
            lease = self._controller_activity_leases.pop(service_name, None)
        if lease is None:
            return
        lease.stop_event.set()
        self._best_effort_release_controller_activity_token(
            service_name, lease.token
        )

    def _best_effort_release_controller_activity_token(
        self, service_name: str, token: str
    ) -> None:
        try:
            self._controller_request(
                "DELETE",
                f"/services/{service_name}/activity-leases/{token}",
            )
        except Exception as exc:  # noqa: BLE001
            # The controller expires orphaned tokens, so a failed cleanup delays
            # recycling but can never stop a model that is still being used.
            logger.warning(
                "Unable to release local model activity lease for %s: %s",
                service_name,
                exc,
            )

    def _controller_state(self, service_name: str) -> dict[str, Any]:
        self._validate_controlled_service_name(service_name)
        data = self._controller_request("GET", f"/services/{service_name}").json()
        if not isinstance(data, dict):
            raise LocalModelRuntimeError("Local model controller returned an invalid state response.")
        return data

    def _controller_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        base_url = self.settings.local_model_controller_url.strip().rstrip("/")
        if not base_url:
            raise LocalModelRuntimeError("Local model controller URL is not configured.")
        timeout = max(30.0, float(self.settings.local_model_stop_timeout_seconds) + 5.0)
        try:
            response = get_shared_sync_client("controller").request(
                method,
                f"{base_url}{path}",
                timeout=timeout,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            raise LocalModelRuntimeError(
                f"Local model controller request failed: {method} {path}: {exc}"
            ) from exc
        if response.status_code == 409:
            raise LocalModelRuntimeBusyError(
                f"Local model controller reports an active lease for {path}: "
                f"{response.text[:500]}"
            )
        if response.status_code >= 400:
            raise LocalModelRuntimeError(
                f"Local model controller {method} {path} failed with HTTP "
                f"{response.status_code}: {response.text[:500]}"
            )
        return response

    def _find_container(self, service_name: str) -> dict[str, Any] | None:
        self._validate_controlled_service_name(service_name)
        filters = {
            "label": [
                f"com.docker.compose.project={self.settings.local_model_docker_project_name}",
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
        if not isinstance(containers, list) or not containers:
            return None
        valid = []
        for item in containers:
            if not isinstance(item, dict):
                continue
            labels = item.get("Labels")
            if not isinstance(labels, dict):
                continue
            if (
                str(labels.get("com.docker.compose.project") or "")
                != self.settings.local_model_docker_project_name
            ):
                continue
            if str(labels.get("com.docker.compose.service") or "") != service_name:
                continue
            if str(labels.get("com.docker.compose.oneoff") or "").casefold() != "false":
                continue
            valid.append(item)
        if not valid:
            return None
        return max(valid, key=lambda item: int(item.get("Created") or 0))

    def _inspect_state(self, container_id: str) -> dict[str, Any]:
        data = self._docker_request("GET", f"/containers/{container_id}/json").json()
        state = data.get("State") if isinstance(data, dict) else None
        state = state if isinstance(state, dict) else {}
        health = state.get("Health") if isinstance(state.get("Health"), dict) else None
        return {
            "running": bool(state.get("Running")),
            "status": str(state.get("Status") or "unknown"),
            "health": str(health.get("Status")) if health and health.get("Status") else None,
            "error": str(state.get("Error") or "")[:300],
        }

    def _docker_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        socket_path = str(self.settings.local_model_docker_socket_path)
        if not self._docker_socket_exists():
            raise LocalModelRuntimeError(f"Docker Engine socket is unavailable: {socket_path}")
        transport = httpx.HTTPTransport(uds=socket_path)
        timeout = max(30.0, float(self.settings.local_model_stop_timeout_seconds) + 5.0)
        with httpx.Client(
            transport=transport,
            base_url="http://docker",
            timeout=timeout,
            trust_env=False,
        ) as client:
            response = client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise LocalModelRuntimeError(
                f"Docker Engine {method} {path} failed with HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        return response

    def _docker_socket_exists(self) -> bool:
        return Path(self.settings.local_model_docker_socket_path).exists()

    def _stop_service_with_compose(self, service_name: str) -> bool:
        command = self._build_stop_command(service_name)
        if command is None:
            return False
        result = subprocess.run(
            command.argv,
            cwd=command.cwd,
            timeout=command.timeout_seconds,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "Local model recycle command failed for %s with code %s: %s",
                service_name,
                result.returncode,
                (result.stderr or result.stdout).strip()[:1000],
            )
            return False
        logger.info("Stopped idle local model service: %s", service_name)
        return True

    def _build_stop_command(self, service_name: str) -> _RecycleCommand | None:
        self._validate_controlled_service_name(service_name)
        docker_command = shlex.split(self.settings.local_model_docker_command.strip())
        if not docker_command:
            logger.warning("LOCAL_MODEL_DOCKER_COMMAND is empty; skip local model recycle.")
            return None
        project_dir = self.settings.local_model_docker_project_dir or self.settings.project_root
        compose_file = self.settings.local_model_docker_compose_file
        argv = [
            *docker_command,
            "compose",
            "-f",
            str(compose_file),
            "stop",
            service_name,
        ]
        return _RecycleCommand(
            argv=argv,
            cwd=str(project_dir),
            timeout_seconds=float(self.settings.local_model_stop_timeout_seconds),
        )


_recycler: LocalModelRuntimeRecycler | None = None
_recycler_lock = threading.Lock()


def get_local_model_runtime_recycler() -> LocalModelRuntimeRecycler:
    global _recycler
    with _recycler_lock:
        if _recycler is None:
            _recycler = LocalModelRuntimeRecycler(get_settings())
        return _recycler

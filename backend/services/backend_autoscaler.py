from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, HTTPException
from redis import Redis
from redis.exceptions import RedisError


logger = logging.getLogger(__name__)

ACTIVE_SESSIONS_KEY = "hragent:auth:active_sessions"
AUTH_SESSION_KEY_PREFIX = "hragent:auth:session:"
BACKEND_SERVICE_NAME = "backend"
BACKEND_ACTIVITY_PATH = "/api/v1/health/autoscaling"
RECONCILE_LEASE_KEY_PREFIX = "hragent:backend-autoscaler:reconcile:"
_CONTAINER_ID_PATTERN = re.compile(r"^[a-f0-9]{12,64}$")
_PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{7,127}$")
_GENERIC_PROJECT_NAMES = frozenset(
    {
        "06",
        "default",
        "docker",
        "compose",
        "backend",
        "hragent",
        "hr-agent",
        "hragent-05",
    }
)


class BackendAutoscalerError(RuntimeError):
    """Raised when autoscaling cannot proceed without violating its safety boundary."""


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return default if raw is None else int(raw)
    except ValueError as exc:
        raise BackendAutoscalerError(f"{name} must be an integer.") from exc


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return default if raw is None else float(raw)
    except ValueError as exc:
        raise BackendAutoscalerError(f"{name} must be a number.") from exc


def validate_compose_project_name(value: str | None) -> str:
    project_name = str(value or "").strip().lower()
    if not project_name:
        raise BackendAutoscalerError(
            "COMPOSE_PROJECT_NAME is required; the autoscaler has no fallback project."
        )
    if (
        project_name in _GENERIC_PROJECT_NAMES
        or not _PROJECT_NAME_PATTERN.fullmatch(project_name)
        or project_name.count("-") < 2
    ):
        raise BackendAutoscalerError(
            "COMPOSE_PROJECT_NAME must be a specific instance name, not a generic project name."
        )
    return project_name


@dataclass(frozen=True)
class BackendAutoscalerConfig:
    project_name: str
    redis_url: str
    min_replicas: int = 2
    max_replicas: int = 8
    target_users_per_replica: int = 8
    spare_replicas: int = 1
    active_window_seconds: float = 300.0
    poll_interval_seconds: float = 10.0
    scale_down_stabilization_seconds: float = 900.0
    scale_down_cooldown_seconds: float = 300.0
    scale_down_min_idle_seconds: float = 30.0
    docker_socket_path: str = "/var/run/docker.sock"
    backend_port: int = 7111
    docker_stop_timeout_seconds: float = 300.0
    activity_probe_timeout_seconds: float = 1.5
    compose_working_dir: str | None = None
    compose_config_files: str | None = None
    compose_env_file: str | None = None
    docker_cli_path: str | None = None
    compose_command_timeout_seconds: float = 180.0
    start_health_timeout_seconds: float = 120.0
    start_health_poll_seconds: float = 1.0
    scale_down_probe_interval_seconds: float = 1.0
    reconcile_lease_seconds: float = 1500.0
    watchdog_stale_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_name", validate_compose_project_name(self.project_name))
        if not self.redis_url.strip():
            raise BackendAutoscalerError("REDIS_URL is required by the backend autoscaler.")
        if self.min_replicas < 2:
            raise BackendAutoscalerError("The backend autoscaler requires at least 2 replicas.")
        if self.max_replicas > 8 or self.max_replicas < self.min_replicas:
            raise BackendAutoscalerError("Backend replica bounds must satisfy min <= max <= 8.")
        if self.target_users_per_replica < 1:
            raise BackendAutoscalerError("Target users per backend replica must be positive.")
        if self.spare_replicas < 0:
            raise BackendAutoscalerError("BACKEND_AUTOSCALER_SPARE_REPLICAS cannot be negative.")
        required_paths = {
            "BACKEND_AUTOSCALER_COMPOSE_WORKING_DIR": self.compose_working_dir,
            "BACKEND_AUTOSCALER_COMPOSE_CONFIG_FILES": self.compose_config_files,
            "BACKEND_AUTOSCALER_COMPOSE_ENV_FILE": self.compose_env_file,
            "BACKEND_AUTOSCALER_DOCKER_CLI_PATH": self.docker_cli_path,
        }
        missing_paths = [name for name, value in required_paths.items() if not str(value or "").strip()]
        if missing_paths:
            raise BackendAutoscalerError(
                "Required backend autoscaler path settings are missing: " + ", ".join(missing_paths)
            )
        non_absolute = [
            name for name, value in required_paths.items() if not Path(str(value)).is_absolute()
        ]
        if non_absolute:
            raise BackendAutoscalerError(
                "Backend autoscaler paths must be absolute: " + ", ".join(non_absolute)
            )
        positive_values = {
            "active_window_seconds": self.active_window_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "scale_down_stabilization_seconds": self.scale_down_stabilization_seconds,
            "scale_down_cooldown_seconds": self.scale_down_cooldown_seconds,
            "scale_down_min_idle_seconds": self.scale_down_min_idle_seconds,
            "docker_stop_timeout_seconds": self.docker_stop_timeout_seconds,
            "activity_probe_timeout_seconds": self.activity_probe_timeout_seconds,
            "compose_command_timeout_seconds": self.compose_command_timeout_seconds,
            "start_health_timeout_seconds": self.start_health_timeout_seconds,
            "start_health_poll_seconds": self.start_health_poll_seconds,
            "scale_down_probe_interval_seconds": self.scale_down_probe_interval_seconds,
            "reconcile_lease_seconds": self.reconcile_lease_seconds,
            "watchdog_stale_seconds": self.watchdog_stale_seconds,
        }
        invalid = [name for name, value in positive_values.items() if value <= 0]
        if invalid:
            raise BackendAutoscalerError(f"Autoscaler timing values must be positive: {invalid}")
        if not (1 <= self.backend_port <= 65535):
            raise BackendAutoscalerError("BACKEND_AUTOSCALER_BACKEND_PORT is invalid.")
        longest_mutation_seconds = max(
            self.compose_command_timeout_seconds,
            self.start_health_timeout_seconds
            + max(30.0, self.docker_stop_timeout_seconds + 5.0),
            self.docker_stop_timeout_seconds + 2 * self.scale_down_probe_interval_seconds,
        )
        if self.reconcile_lease_seconds <= 2 * longest_mutation_seconds:
            raise BackendAutoscalerError(
                "BACKEND_AUTOSCALER_RECONCILE_LEASE_SECONDS must exceed twice the "
                "longest configured mutating operation."
            )
        if self.watchdog_stale_seconds <= 2 * self.poll_interval_seconds:
            raise BackendAutoscalerError(
                "BACKEND_AUTOSCALER_WATCHDOG_STALE_SECONDS must exceed two poll intervals."
            )

    @classmethod
    def from_env(cls) -> "BackendAutoscalerConfig":
        return cls(
            project_name=os.getenv("COMPOSE_PROJECT_NAME", ""),
            redis_url=os.getenv("REDIS_URL", ""),
            min_replicas=_read_int("BACKEND_AUTOSCALER_MIN_REPLICAS", 2),
            max_replicas=_read_int("BACKEND_AUTOSCALER_MAX_REPLICAS", 8),
            target_users_per_replica=_read_int(
                "BACKEND_AUTOSCALER_TARGET_USERS_PER_REPLICA", 8
            ),
            spare_replicas=_read_int("BACKEND_AUTOSCALER_SPARE_REPLICAS", 1),
            active_window_seconds=_read_float(
                "BACKEND_AUTOSCALER_ACTIVE_WINDOW_SECONDS", 300.0
            ),
            poll_interval_seconds=_read_float("BACKEND_AUTOSCALER_POLL_SECONDS", 10.0),
            scale_down_stabilization_seconds=_read_float(
                "BACKEND_AUTOSCALER_SCALE_DOWN_STABILIZATION_SECONDS", 900.0
            ),
            scale_down_cooldown_seconds=_read_float(
                "BACKEND_AUTOSCALER_SCALE_DOWN_COOLDOWN_SECONDS", 300.0
            ),
            scale_down_min_idle_seconds=_read_float(
                "BACKEND_AUTOSCALER_SCALE_DOWN_MIN_IDLE_SECONDS", 30.0
            ),
            docker_socket_path=os.getenv(
                "BACKEND_AUTOSCALER_DOCKER_SOCKET_PATH", "/var/run/docker.sock"
            ),
            backend_port=_read_int("BACKEND_AUTOSCALER_BACKEND_PORT", 7111),
            docker_stop_timeout_seconds=_read_float(
                "BACKEND_AUTOSCALER_STOP_TIMEOUT_SECONDS", 300.0
            ),
            activity_probe_timeout_seconds=_read_float(
                "BACKEND_AUTOSCALER_ACTIVITY_TIMEOUT_SECONDS", 1.5
            ),
            compose_working_dir=(
                os.getenv("BACKEND_AUTOSCALER_COMPOSE_WORKING_DIR", "").strip() or None
            ),
            compose_config_files=(
                os.getenv("BACKEND_AUTOSCALER_COMPOSE_CONFIG_FILES", "").strip() or None
            ),
            compose_env_file=(
                os.getenv("BACKEND_AUTOSCALER_COMPOSE_ENV_FILE", "").strip() or None
            ),
            docker_cli_path=(
                os.getenv("BACKEND_AUTOSCALER_DOCKER_CLI_PATH", "").strip() or None
            ),
            compose_command_timeout_seconds=_read_float(
                "BACKEND_AUTOSCALER_COMPOSE_TIMEOUT_SECONDS", 180.0
            ),
            start_health_timeout_seconds=_read_float(
                "BACKEND_AUTOSCALER_START_HEALTH_TIMEOUT_SECONDS", 120.0
            ),
            start_health_poll_seconds=_read_float(
                "BACKEND_AUTOSCALER_START_HEALTH_POLL_SECONDS", 1.0
            ),
            scale_down_probe_interval_seconds=_read_float(
                "BACKEND_AUTOSCALER_SCALE_DOWN_PROBE_INTERVAL_SECONDS", 1.0
            ),
            reconcile_lease_seconds=_read_float(
                "BACKEND_AUTOSCALER_RECONCILE_LEASE_SECONDS", 1500.0
            ),
            watchdog_stale_seconds=_read_float(
                "BACKEND_AUTOSCALER_WATCHDOG_STALE_SECONDS", 30.0
            ),
        )


@dataclass(frozen=True)
class BackendContainer:
    container_id: str
    name: str
    number: int
    running: bool
    status: str
    health: str | None = None
    ip_address: str | None = None

    @property
    def ready(self) -> bool:
        return self.running and self.health == "healthy"


@dataclass(frozen=True)
class BackendActivity:
    active_http_requests: int
    active_websockets: int
    idle_for_seconds: float | None = None

    def is_safe_to_stop(self, minimum_idle_seconds: float) -> bool:
        return (
            self.active_http_requests == 0
            and self.active_websockets == 0
            and self.idle_for_seconds is not None
            and self.idle_for_seconds >= minimum_idle_seconds
        )


@dataclass(frozen=True)
class AutoscalerStatus:
    updated_at: float
    healthy: bool
    active_users: int | None
    # Compatibility alias retained for existing dashboards and API clients.
    active_sessions: int | None
    desired_replicas: int | None
    running_replicas: int
    ready_replicas: int
    pool_size: int
    action: str
    detail: str
    scale_down_pending_since: float | None


class ActiveSessionSource(Protocol):
    def count(self, *, now: float | None = None) -> int: ...


class ReconcileLease(Protocol):
    def acquire(self) -> str | None: ...

    def renew(self, token: str) -> bool: ...

    def release(self, token: str) -> bool: ...


class BackendPool(Protocol):
    def ensure_capacity(self, desired: int) -> list[BackendContainer]: ...

    def list_containers(self) -> list[BackendContainer]: ...

    def start(self, container: BackendContainer) -> None: ...

    def start_many(self, containers: list[BackendContainer], *, count: int) -> None: ...

    def stop(self, container: BackendContainer) -> None: ...

    def activity(self, container: BackendContainer) -> BackendActivity: ...


class RedisActiveSessionSource:
    """Read active auth sessions and count distinct users without mutating auth keys."""

    def __init__(
        self,
        redis_url: str,
        *,
        window_seconds: float,
        client: Redis | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.window_seconds = window_seconds
        self._redis = client

    def count(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else float(now)
        cutoff = timestamp - self.window_seconds
        client = self._client()
        try:
            raw_session_ids = client.zrangebyscore(ACTIVE_SESSIONS_KEY, cutoff, "+inf")
            session_ids = list(dict.fromkeys(str(value) for value in raw_session_ids))
            if not session_ids:
                return 0
            payloads = client.mget(
                [f"{AUTH_SESSION_KEY_PREFIX}{session_id}" for session_id in session_ids]
            )
        except RedisError as exc:
            raise BackendAutoscalerError("Redis active-session query failed.") from exc

        identities: set[str] = set()
        for session_id, raw in zip(session_ids, payloads, strict=False):
            fallback = f"session:{session_id}"
            if not raw:
                identities.add(fallback)
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                identities.add(fallback)
                continue
            if not isinstance(payload, dict):
                identities.add(fallback)
                continue
            user_id = str(payload.get("user_id") or "").strip()
            identities.add(f"user:{user_id}" if user_id else fallback)
        # A short MGET response is treated conservatively as distinct sessions.
        for session_id in session_ids[len(payloads) :]:
            identities.add(f"session:{session_id}")
        return len(identities)

    def _client(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(
                self.redis_url,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
                decode_responses=True,
            )
        return self._redis


class RedisReconcileLease:
    """A single-reconciler lease released only by its current token owner."""

    _COMPARE_DELETE_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    _COMPARE_PEXPIRE_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('PEXPIRE', KEYS[1], ARGV[2])
    end
    return 0
    """

    def __init__(
        self,
        redis_url: str,
        *,
        project_name: str,
        ttl_seconds: float,
        client: Redis | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.key = f"{RECONCILE_LEASE_KEY_PREFIX}{project_name}"
        self.ttl_milliseconds = max(1, int(ttl_seconds * 1000))
        self._redis = client

    def acquire(self) -> str | None:
        token = secrets.token_urlsafe(32)
        try:
            acquired = self._client().set(
                self.key,
                token,
                nx=True,
                px=self.ttl_milliseconds,
            )
        except RedisError as exc:
            raise BackendAutoscalerError(
                "Redis reconciliation lease acquisition failed."
            ) from exc
        return token if bool(acquired) else None

    def renew(self, token: str) -> bool:
        try:
            result = self._client().eval(
                self._COMPARE_PEXPIRE_SCRIPT,
                1,
                self.key,
                token,
                str(self.ttl_milliseconds),
            )
        except RedisError as exc:
            raise BackendAutoscalerError(
                "Redis reconciliation lease renewal failed."
            ) from exc
        return int(result or 0) == 1

    def release(self, token: str) -> bool:
        try:
            result = self._client().eval(
                self._COMPARE_DELETE_SCRIPT,
                1,
                self.key,
                token,
            )
        except RedisError as exc:
            raise BackendAutoscalerError(
                "Redis reconciliation lease release failed."
            ) from exc
        return int(result or 0) == 1

    def _client(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(
                self.redis_url,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
                decode_responses=True,
            )
        return self._redis


class DockerBackendPool:
    """A restricted adapter for one exact Compose backend service.

    It starts/stops pre-created replicas and can provision a stopped pool with the
    configured Compose files.  If every backend is already stopped, it may rebuild
    that stopped pool so recovery does not depend on a healthy running template.
    """

    def __init__(
        self,
        config: BackendAutoscalerConfig,
        *,
        command_runner: Any | None = None,
        clock: Any | None = None,
        sleeper: Any | None = None,
    ) -> None:
        self.config = config
        self._command_runner = command_runner or subprocess.run
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._trusted_stopped_generation: tuple[str, str, frozenset[str]] | None = None

    def ensure_capacity(self, desired: int) -> list[BackendContainer]:
        if desired < self.config.min_replicas or desired > self.config.max_replicas:
            raise BackendAutoscalerError("Requested backend pool capacity is outside configured bounds.")
        # A trusted stopped generation is valid only for the immediately discovered
        # pool.  Never carry it across a later reconciliation or external deploy.
        self._trusted_stopped_generation = None
        before_summaries = self._list_project_container_summaries()
        before = self._backend_containers_from_summaries(before_summaries)
        before_ids = {container.container_id for container in before}
        before_states = [
            str(item.get("State") or "").lower()
            for item in before_summaries
            if str(item.get("Id") or "").lower() in before_ids
        ]
        all_stopped = len(before_states) == len(before) and all(
            state in {"created", "exited"} for state in before_states
        )
        any_running = any(container.running for container in before)

        if any_running and len(before) >= desired:
            return before
        if not any_running and before and not all_stopped:
            raise BackendAutoscalerError(
                "Refusing to rebuild the backend pool because a replica is neither "
                "running nor safely stopped/created."
            )

        # With no healthy running template, stopped replicas cannot prove that they
        # match the current image/config generation.  Recreate only when every exact
        # backend target is safely stopped (or the pool is empty); a running but
        # unhealthy generation must remain untouched and fail closed later.
        force_recreate = not any_running and (not before or all_stopped)

        cli_path, project_dir, config_paths, env_file = self._validated_compose_paths()
        argv = [
            str(cli_path),
            "compose",
            "--project-name",
            self.config.project_name,
            "--project-directory",
            str(project_dir),
            "--env-file",
            str(env_file),
        ]
        for config_path in config_paths:
            argv.extend(["-f", str(config_path)])
        argv.extend(
            [
                "up",
                "--no-start",
                "--no-deps",
                "--force-recreate" if force_recreate else "--no-recreate",
                "--no-build",
                "--scale",
                f"{BACKEND_SERVICE_NAME}={desired}",
                BACKEND_SERVICE_NAME,
            ]
        )
        try:
            completed = self._command_runner(
                argv,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=self.config.compose_command_timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendAutoscalerError("Docker Compose stopped-pool provisioning failed.") from exc
        if int(getattr(completed, "returncode", 1)) != 0:
            stderr = str(getattr(completed, "stderr", "") or "")[:500]
            raise BackendAutoscalerError(
                f"Docker Compose stopped-pool provisioning failed: {stderr}"
            )

        after_summaries = self._list_project_container_summaries()
        after = self._backend_containers_from_summaries(after_summaries)
        unexpected_running = [
            container.name
            for container in after
            if container.running
            and (force_recreate or container.container_id not in before_ids)
        ]
        if unexpected_running:
            raise BackendAutoscalerError(
                "Docker Compose unexpectedly started newly provisioned backend replicas: "
                + ", ".join(unexpected_running[:5])
            )
        if len(after) < desired:
            raise BackendAutoscalerError(
                f"Docker Compose provisioned {len(after)} backend containers; {desired} are required."
            )
        if force_recreate:
            if len(after) != desired:
                raise BackendAutoscalerError(
                    f"Docker Compose recreated {len(after)} backend containers; "
                    f"exactly {desired} are required."
                )
            retained_ids = before_ids.intersection(
                container.container_id for container in after
            )
            if retained_ids:
                raise BackendAutoscalerError(
                    "Docker Compose did not replace every stopped backend replica; "
                    "the generation was not trusted."
                )
            self._trusted_stopped_generation = self._validate_recreated_generation(after)
        logger.info(
            "%s stopped backend pool capacity from %d to %d containers",
            "Recreated" if force_recreate else "Provisioned",
            len(before),
            len(after),
        )
        return after

    def _validate_recreated_generation(
        self,
        containers: list[BackendContainer],
    ) -> tuple[str, str, frozenset[str]]:
        fingerprints: set[tuple[str, str]] = set()
        container_ids: set[str] = set()
        for container in containers:
            inspected = self._inspect_action_target(container.container_id)
            running, _, status = self._runtime_state(inspected)
            if running or status not in {"created", "exited"}:
                raise BackendAutoscalerError(
                    "A recreated backend replica was not left safely stopped/created."
                )
            image_id = str(inspected.get("Image") or "")
            config_hash = self._compose_config_hash(inspected)
            if not image_id or not config_hash:
                raise BackendAutoscalerError(
                    "A recreated backend replica lacks an image ID or Compose config hash."
                )
            fingerprints.add((image_id, config_hash))
            container_ids.add(container.container_id)
        if len(container_ids) != len(containers):
            raise BackendAutoscalerError(
                "Docker returned duplicate identities for recreated backend replicas."
            )
        if len(fingerprints) != 1:
            raise BackendAutoscalerError(
                "Recreated backend replicas do not share one image ID and Compose config hash."
            )
        image_id, config_hash = next(iter(fingerprints))
        return image_id, config_hash, frozenset(container_ids)

    def _list_project_container_summaries(self) -> list[dict[str, Any]]:
        filters = {
            "label": [f"com.docker.compose.project={self.config.project_name}"]
        }
        response = self._docker_request(
            "GET",
            "/containers/json",
            params={"all": "true", "filters": json.dumps(filters, separators=(",", ":"))},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise BackendAutoscalerError("Docker returned an invalid project container list.")
        summaries: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise BackendAutoscalerError("Docker returned a malformed project container.")
            self._validate_container_id(item.get("Id"))
            labels = item.get("Labels") if isinstance(item.get("Labels"), dict) else {}
            self._validate_project_scope_labels(labels)
            summaries.append(item)
        return summaries

    def _backend_containers_from_summaries(
        self,
        summaries: list[dict[str, Any]],
    ) -> list[BackendContainer]:
        containers = []
        for item in summaries:
            labels = item.get("Labels") if isinstance(item.get("Labels"), dict) else {}
            if str(labels.get("com.docker.compose.service") or "") != BACKEND_SERVICE_NAME:
                continue
            if str(labels.get("com.docker.compose.oneoff") or "").lower() != "false":
                continue
            containers.append(self._container_from_summary(item))
        return sorted(containers, key=lambda item: (item.number, item.name, item.container_id))

    def _validate_project_scope_labels(self, labels: dict[str, Any]) -> None:
        expected = {
            "com.docker.compose.project": self.config.project_name,
            "com.docker.compose.project.working_dir": str(self.config.compose_working_dir),
            "com.docker.compose.project.config_files": str(self.config.compose_config_files),
        }
        mismatches = [
            name
            for name, value in expected.items()
            if str(labels.get(name) or "") != value
        ]
        if mismatches:
            raise BackendAutoscalerError(
                "Refusing Docker Compose invocation because project containers have mismatched labels: "
                + ", ".join(mismatches)
            )

    def _validated_compose_paths(
        self,
    ) -> tuple[Path, Path, list[Path], Path]:
        cli_path = Path(str(self.config.docker_cli_path))
        project_dir = Path(str(self.config.compose_working_dir))
        env_file = Path(str(self.config.compose_env_file))
        config_paths = [
            Path(value.strip())
            for value in str(self.config.compose_config_files).split(",")
            if value.strip()
        ]
        if not cli_path.is_file() or not os.access(cli_path, os.X_OK):
            raise BackendAutoscalerError(
                f"Docker CLI is missing or not executable: {cli_path}"
            )
        if not project_dir.is_dir():
            raise BackendAutoscalerError(
                f"Compose project directory does not exist: {project_dir}"
            )
        missing_files = [
            str(path) for path in [*config_paths, env_file] if not path.is_file()
        ]
        if not config_paths or missing_files:
            raise BackendAutoscalerError(
                "Compose config or env file is missing: " + ", ".join(missing_files)
            )
        return cli_path, project_dir, config_paths, env_file

    def list_containers(self) -> list[BackendContainer]:
        filters = {
            "label": [
                f"com.docker.compose.project={self.config.project_name}",
                f"com.docker.compose.service={BACKEND_SERVICE_NAME}",
                "com.docker.compose.oneoff=False",
            ]
        }
        if self.config.compose_working_dir:
            filters["label"].append(
                "com.docker.compose.project.working_dir=" + self.config.compose_working_dir
            )
        if self.config.compose_config_files:
            filters["label"].append(
                "com.docker.compose.project.config_files=" + self.config.compose_config_files
            )
        response = self._docker_request(
            "GET",
            "/containers/json",
            params={"all": "true", "filters": json.dumps(filters, separators=(",", ":"))},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise BackendAutoscalerError("Docker returned an invalid backend container list.")
        containers = [self._container_from_summary(item) for item in payload]
        return sorted(containers, key=lambda item: (item.number, item.name, item.container_id))

    def start(self, container: BackendContainer) -> None:
        self.start_many([container], count=1)

    def start_many(self, containers: list[BackendContainer], *, count: int) -> None:
        if count <= 0:
            return
        selected: list[BackendContainer] = []
        validation_errors: list[str] = []
        for container in containers:
            try:
                target = self._inspect_action_target(container.container_id)
                self._validate_stopped_replica(target)
            except Exception as exc:
                validation_errors.append(f"{container.name}: {exc}")
                continue
            selected.append(container)
            if len(selected) == count:
                break
        if len(selected) < count:
            raise BackendAutoscalerError(
                f"Only {len(selected)} of {count} requested stopped replicas were valid: "
                + "; ".join(validation_errors[:5])
            )
        for container in selected:
            self._docker_request("POST", f"/containers/{container.container_id}/start")
        self._wait_until_healthy(selected)
        logger.info("Started %d healthy backend pool container(s)", len(selected))


    def _validate_stopped_replica(self, target: dict[str, Any]) -> None:
        target_running, _, target_status = self._runtime_state(target)
        if target_running:
            raise BackendAutoscalerError("Refusing to start a backend replica that is already running.")
        if target_status not in {"created", "exited"}:
            raise BackendAutoscalerError(
                "Refusing to start a backend replica that is not safely stopped/created."
            )
        target_id = self._validate_container_id(target.get("Id"))
        target_image = str(target.get("Image") or "")
        target_hash = self._compose_config_hash(target)
        if not target_image or not target_hash:
            raise BackendAutoscalerError(
                "Stopped backend replica lacks an image ID or Compose config hash."
            )

        healthy_templates = [item for item in self.list_containers() if item.ready]
        for template in healthy_templates:
            inspected = self._inspect_action_target(template.container_id)
            running, health, _ = self._runtime_state(inspected)
            if (
                running
                and health == "healthy"
                and str(inspected.get("Image") or "") == target_image
                and self._compose_config_hash(inspected) == target_hash
            ):
                return
        trusted = self._trusted_stopped_generation
        if trusted is not None:
            trusted_image, trusted_hash, trusted_ids = trusted
            if (
                target_id in trusted_ids
                and target_image == trusted_image
                and target_hash == trusted_hash
            ):
                return
        raise BackendAutoscalerError(
            "Stopped backend replica is stale or no healthy running template has "
            "the same image ID and Compose config hash; it was left stopped."
        )

    def _wait_until_healthy(self, containers: list[BackendContainer]) -> None:
        deadline = self._clock() + self.config.start_health_timeout_seconds
        pending = {item.container_id: item for item in containers}
        last_states: dict[str, str] = {}
        while pending:
            for container_id, container in list(pending.items()):
                inspected = self._inspect_action_target(container_id)
                running, health, status = self._runtime_state(inspected)
                state_label = f"state={status}, health={health or 'missing'}"
                last_states[container.name] = state_label
                if running and health == "healthy":
                    del pending[container_id]
                    continue
                if status in {"dead", "removing", "exited"}:
                    state = (
                        inspected.get("State")
                        if isinstance(inspected.get("State"), dict)
                        else {}
                    )
                    raise BackendAutoscalerError(
                        f"Backend container {container.name} failed to start "
                        f"({state_label}, exit_code={state.get('ExitCode')}, "
                        f"error={str(state.get('Error') or '')[:200]})."
                    )
            if not pending:
                return
            remaining = deadline - self._clock()
            if remaining <= 0:
                details = "; ".join(
                    f"{item.name}: {last_states.get(item.name, 'unknown')}"
                    for item in pending.values()
                )
                raise BackendAutoscalerError(
                    f"{len(pending)} backend container(s) did not become running and "
                    f"healthy within {self.config.start_health_timeout_seconds:.1f}s "
                    f"({details[:500]})."
                )
            self._sleep(min(self.config.start_health_poll_seconds, remaining))

    @staticmethod
    def _runtime_state(payload: dict[str, Any]) -> tuple[bool, str | None, str]:
        state = payload.get("State") if isinstance(payload.get("State"), dict) else {}
        status = str(state.get("Status") or "unknown").lower()
        running = bool(state.get("Running")) or status == "running"
        health_payload = state.get("Health") if isinstance(state.get("Health"), dict) else {}
        health = str(health_payload.get("Status") or "").lower() or None
        return running, health, status

    @staticmethod
    def _compose_config_hash(payload: dict[str, Any]) -> str:
        config = payload.get("Config") if isinstance(payload.get("Config"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        return str(labels.get("com.docker.compose.config-hash") or "")

    def stop(self, container: BackendContainer) -> None:
        self._inspect_action_target(container.container_id)
        self._docker_request(
            "POST",
            f"/containers/{container.container_id}/stop",
            params={"t": int(self.config.docker_stop_timeout_seconds)},
        )
        logger.info("Stopped idle backend pool container %s", container.name)

    def activity(self, container: BackendContainer) -> BackendActivity:
        inspected = self._inspect_action_target(container.container_id)
        ip_address = self._container_ip(inspected)
        if not ip_address:
            raise BackendAutoscalerError(f"Backend container {container.name} has no network IP.")
        try:
            parsed_ip = ipaddress.ip_address(ip_address)
        except ValueError as exc:
            raise BackendAutoscalerError(
                f"Backend container {container.name} returned an invalid network IP."
            ) from exc
        if not (parsed_ip.is_private or parsed_ip.is_loopback or parsed_ip.is_link_local):
            raise BackendAutoscalerError(
                f"Refusing activity probe to non-private address {ip_address}."
            )
        host = f"[{ip_address}]" if parsed_ip.version == 6 else ip_address
        response = self._activity_request(
            f"http://{host}:{self.config.backend_port}{BACKEND_ACTIVITY_PATH}"
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise BackendAutoscalerError("Backend activity probe returned invalid JSON.")
        try:
            raw_idle = payload.get("idle_for_seconds")
            activity = BackendActivity(
                active_http_requests=int(payload["active_http_requests"]),
                active_websockets=int(payload["active_websockets"]),
                idle_for_seconds=float(raw_idle) if raw_idle is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendAutoscalerError("Backend activity probe contract is incomplete.") from exc
        if (
            activity.active_http_requests < 0
            or activity.active_websockets < 0
            or (activity.idle_for_seconds is not None and activity.idle_for_seconds < 0)
        ):
            raise BackendAutoscalerError("Backend activity probe returned negative counters.")
        return activity

    def _container_from_summary(self, item: Any) -> BackendContainer:
        if not isinstance(item, dict):
            raise BackendAutoscalerError("Docker returned a malformed backend container.")
        container_id = self._validate_container_id(item.get("Id"))
        labels = item.get("Labels") if isinstance(item.get("Labels"), dict) else {}
        self._validate_labels(labels)
        raw_number = str(labels.get("com.docker.compose.container-number") or "")
        try:
            number = int(raw_number)
        except ValueError as exc:
            raise BackendAutoscalerError("Backend container is missing its Compose replica number.") from exc
        names = item.get("Names") if isinstance(item.get("Names"), list) else []
        name = str(names[0] if names else container_id[:12]).lstrip("/")
        state = str(item.get("State") or "unknown").lower()
        status = str(item.get("Status") or state)
        lowered_status = status.lower()
        health = None
        if "(healthy)" in lowered_status:
            health = "healthy"
        elif "(unhealthy)" in lowered_status:
            health = "unhealthy"
        elif "health: starting" in lowered_status:
            health = "starting"
        return BackendContainer(
            container_id=container_id,
            name=name,
            number=number,
            running=state == "running",
            status=status,
            health=health,
        )

    def _inspect_action_target(self, container_id: str) -> dict[str, Any]:
        safe_id = self._validate_container_id(container_id)
        payload = self._docker_request("GET", f"/containers/{safe_id}/json").json()
        if not isinstance(payload, dict):
            raise BackendAutoscalerError("Docker returned invalid container inspection data.")
        inspected_id = self._validate_container_id(payload.get("Id"))
        if inspected_id != safe_id:
            raise BackendAutoscalerError("Docker container identity changed during reconciliation.")
        config = payload.get("Config") if isinstance(payload.get("Config"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        self._validate_labels(labels)
        return payload

    def _validate_labels(self, labels: dict[str, Any]) -> None:
        expected = {
            "com.docker.compose.project": self.config.project_name,
            "com.docker.compose.service": BACKEND_SERVICE_NAME,
        }
        if any(str(labels.get(name) or "") != value for name, value in expected.items()):
            raise BackendAutoscalerError("Refusing Docker operation outside the exact backend project.")
        if str(labels.get("com.docker.compose.oneoff") or "").lower() != "false":
            raise BackendAutoscalerError("Refusing Docker operation on a Compose one-off container.")
        optional_expected = {
            "com.docker.compose.project.working_dir": self.config.compose_working_dir,
            "com.docker.compose.project.config_files": self.config.compose_config_files,
        }
        for name, value in optional_expected.items():
            if value is not None and str(labels.get(name) or "") != value:
                raise BackendAutoscalerError(f"Refusing Docker operation with mismatched {name}.")

    @staticmethod
    def _validate_container_id(value: Any) -> str:
        container_id = str(value or "").strip().lower()
        if not _CONTAINER_ID_PATTERN.fullmatch(container_id):
            raise BackendAutoscalerError("Docker returned an invalid container ID.")
        return container_id

    def _container_ip(self, payload: dict[str, Any]) -> str | None:
        network_settings = (
            payload.get("NetworkSettings")
            if isinstance(payload.get("NetworkSettings"), dict)
            else {}
        )
        networks = (
            network_settings.get("Networks")
            if isinstance(network_settings.get("Networks"), dict)
            else {}
        )
        preferred_name = f"{self.config.project_name}_default"
        candidates = []
        if preferred_name in networks:
            candidates.append(networks[preferred_name])
        candidates.extend(value for name, value in networks.items() if name != preferred_name)
        for network in candidates:
            if not isinstance(network, dict):
                continue
            address = str(network.get("IPAddress") or network.get("GlobalIPv6Address") or "").strip()
            if address:
                return address
        return None

    def _activity_request(self, url: str) -> httpx.Response:
        with httpx.Client(
            timeout=self.config.activity_probe_timeout_seconds,
            trust_env=False,
        ) as client:
            response = client.get(url, headers={"accept": "application/json"})
        if response.status_code >= 400:
            raise BackendAutoscalerError(
                f"Backend activity probe failed with HTTP {response.status_code}."
            )
        return response

    def _docker_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not Path(self.config.docker_socket_path).exists():
            raise BackendAutoscalerError(
                f"Docker Engine socket is unavailable: {self.config.docker_socket_path}"
            )
        transport = httpx.HTTPTransport(uds=self.config.docker_socket_path)
        with httpx.Client(
            transport=transport,
            base_url="http://docker",
            timeout=max(30.0, self.config.docker_stop_timeout_seconds + 5.0),
            trust_env=False,
        ) as client:
            response = client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise BackendAutoscalerError(
                f"Docker Engine {method} {path} failed with HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response


class BackendAutoscaler:
    def __init__(
        self,
        config: BackendAutoscalerConfig,
        active_sessions: ActiveSessionSource,
        pool: BackendPool,
        lease: ReconcileLease,
        *,
        sleeper: Any | None = None,
    ) -> None:
        self.config = config
        self.active_sessions = active_sessions
        self.pool = pool
        self.lease = lease
        self._sleep = sleeper or time.sleep
        self._current_lease_token: str | None = None
        self._lock = threading.Lock()
        self._scale_down_target: int | None = None
        self._scale_down_pending_since: float | None = None
        self._last_scale_at: float | None = None
        self._status = AutoscalerStatus(
            updated_at=0.0,
            healthy=False,
            active_users=None,
            active_sessions=None,
            desired_replicas=None,
            running_replicas=0,
            ready_replicas=0,
            pool_size=0,
            action="starting",
            detail="Autoscaler has not completed its first reconciliation.",
            scale_down_pending_since=None,
        )

    def desired_replicas(self, active_sessions: int) -> int:
        demand = (
            math.ceil(max(0, active_sessions) / self.config.target_users_per_replica)
            + self.config.spare_replicas
        )
        return max(self.config.min_replicas, min(self.config.max_replicas, demand))

    def snapshot(self) -> AutoscalerStatus:
        with self._lock:
            return self._status

    def reconcile(self, *, now: float | None = None) -> AutoscalerStatus:
        timestamp = time.time() if now is None else float(now)
        previous = self.snapshot()
        try:
            token = self.lease.acquire()
        except Exception as exc:
            return self._publish(
                timestamp,
                healthy=False,
                active_sessions=previous.active_users,
                desired=previous.desired_replicas,
                running=previous.running_replicas,
                ready=previous.ready_replicas,
                pool_size=previous.pool_size,
                action="lease_unavailable",
                detail=f"Reconciliation lease is unavailable; no Docker mutation occurred: {exc}",
            )
        if token is None:
            return self._publish(
                timestamp,
                healthy=False,
                active_sessions=previous.active_users,
                desired=previous.desired_replicas,
                running=previous.running_replicas,
                ready=previous.ready_replicas,
                pool_size=previous.pool_size,
                action="lease_contended",
                detail="Another reconciler owns the lease; no Docker mutation occurred.",
            )

        self._current_lease_token = token
        try:
            result = self._reconcile_locked(timestamp=timestamp)
        except Exception as exc:
            result = self._publish(
                timestamp,
                healthy=False,
                active_sessions=None,
                desired=None,
                running=previous.running_replicas,
                ready=previous.ready_replicas,
                pool_size=previous.pool_size,
                action="reconcile_failed",
                detail=f"Unexpected reconciliation failure: {exc}",
            )
        try:
            released = self.lease.release(token)
            release_error = None
        except Exception as exc:
            released = False
            release_error = str(exc)
        finally:
            self._current_lease_token = None
        if not released:
            return self._publish(
                timestamp,
                healthy=False,
                active_sessions=result.active_users,
                desired=result.desired_replicas,
                running=result.running_replicas,
                ready=result.ready_replicas,
                pool_size=result.pool_size,
                action="lease_release_failed",
                detail=(
                    "Reconciliation completed but lease ownership could not be safely released: "
                    + (release_error or "lease token was no longer owned")
                ),
            )
        return result

    def _reconcile_locked(self, *, timestamp: float) -> AutoscalerStatus:
        try:
            containers = self.pool.ensure_capacity(self.config.max_replicas)
        except Exception as exc:
            self._clear_scale_down_pending()
            return self._publish(
                timestamp,
                healthy=False,
                active_sessions=None,
                desired=None,
                running=0,
                ready=0,
                pool_size=0,
                action="blocked",
                detail=f"Backend pool discovery failed: {exc}",
            )

        running = sum(container.running for container in containers)
        ready = sum(container.ready for container in containers)
        action_parts: list[str] = []
        if running < self.config.min_replicas:
            previous_ready = ready
            started, error = self._start_replicas(
                containers,
                self.config.min_replicas - running,
            )
            try:
                containers = self.pool.list_containers()
                running = sum(container.running for container in containers)
                ready = sum(container.ready for container in containers)
            except Exception as exc:
                error = error or f"Backend pool refresh after start failed: {exc}"
            actual_started = max(started if error is None else 0, ready - previous_ready)
            if actual_started:
                self._last_scale_at = timestamp
                action_parts.append(f"started {actual_started} minimum replica(s)")
            if error:
                self._clear_scale_down_pending()
                return self._publish(
                    timestamp,
                    healthy=False,
                    active_sessions=None,
                    desired=self.config.min_replicas,
                    running=running,
                    ready=ready,
                    pool_size=len(containers),
                    action="scale_up_incomplete",
                    detail=error,
                )

        try:
            # Refresh the immutable snapshot so replicas just started to meet
            # the minimum cannot be selected a second time for demand scaling.
            containers = self.pool.list_containers()
            running = sum(container.running for container in containers)
            ready = sum(container.ready for container in containers)

            active_count = self.active_sessions.count(now=timestamp)
        except Exception as exc:
            self._clear_scale_down_pending()
            return self._publish(
                timestamp,
                healthy=ready >= self.config.min_replicas,
                active_sessions=None,
                desired=None,
                running=running,
                ready=ready,
                pool_size=len(containers),
                action="; ".join(action_parts) or "hold",
                detail=f"Active-session demand is unavailable; replicas were preserved: {exc}",
            )

        desired = self.desired_replicas(active_count)
        starting = sum(
            container.running and container.health in {None, "starting"}
            for container in containers
        )
        needed_ready_capacity = max(0, desired - (ready + starting))
        start_count = min(
            needed_ready_capacity,
            max(0, self.config.max_replicas - running),
        )
        if start_count > 0:
            self._clear_scale_down_pending()
            previous_ready = ready
            started, error = self._start_replicas(containers, start_count)
            try:
                containers = self.pool.list_containers()
                running = sum(container.running for container in containers)
                ready = sum(container.ready for container in containers)
            except Exception as exc:
                error = error or f"Backend pool refresh after demand start failed: {exc}"
            actual_started = max(started if error is None else 0, ready - previous_ready)
            if actual_started:
                self._last_scale_at = timestamp
                action_parts.append(
                    f"started {actual_started} demand replica(s)"
                )
            return self._publish(
                timestamp,
                healthy=ready >= desired and error is None,
                active_sessions=active_count,
                desired=desired,
                running=running,
                ready=ready,
                pool_size=len(containers),
                action="; ".join(action_parts) or "scale_up_incomplete",
                detail=error or "Demand capacity was started without a scale-up delay.",
            )

        if ready <= desired and ready < running:
            self._clear_scale_down_pending()
            return self._publish(
                timestamp,
                healthy=False,
                active_sessions=active_count,
                desired=desired,
                running=running,
                ready=ready,
                pool_size=len(containers),
                action="waiting_for_readiness",
                detail=(
                    "Running but unhealthy/starting replicas occupy capacity; no extra "
                    "replica was started and no healthy replica was stopped."
                ),
            )

        if desired == running:
            self._clear_scale_down_pending()
            return self._publish(
                timestamp,
                healthy=ready >= desired,
                active_sessions=active_count,
                desired=desired,
                running=running,
                ready=ready,
                pool_size=len(containers),
                action="; ".join(action_parts) or "hold",
                detail="Running replica count matches demand.",
            )

        if self._scale_down_target != desired or self._scale_down_pending_since is None:
            self._scale_down_target = desired
            self._scale_down_pending_since = timestamp
            return self._publish(
                timestamp,
                healthy=ready >= desired,
                active_sessions=active_count,
                desired=desired,
                running=running,
                ready=ready,
                pool_size=len(containers),
                action="scale_down_stabilizing",
                detail="Lower demand must remain stable before one replica may stop.",
            )

        stable_for = timestamp - self._scale_down_pending_since
        if stable_for < self.config.scale_down_stabilization_seconds:
            return self._publish(
                timestamp,
                healthy=ready >= desired,
                active_sessions=active_count,
                desired=desired,
                running=running,
                ready=ready,
                pool_size=len(containers),
                action="scale_down_stabilizing",
                detail="Lower demand is still inside the stabilization window.",
            )
        if (
            self._last_scale_at is not None
            and timestamp - self._last_scale_at < self.config.scale_down_cooldown_seconds
        ):
            return self._publish(
                timestamp,
                healthy=ready >= desired,
                active_sessions=active_count,
                desired=desired,
                running=running,
                ready=ready,
                pool_size=len(containers),
                action="scale_down_cooldown",
                detail="Scale-down is limited to one replica per cooldown interval.",
            )

        stopped, detail = self._stop_one_idle_replica(containers, desired=desired)
        if stopped:
            containers = self.pool.list_containers()
            running = sum(container.running for container in containers)
            ready = sum(container.ready for container in containers)
            self._last_scale_at = timestamp
            action = "stopped 1 idle replica"
            healthy = True
        else:
            action = "scale_down_blocked"
            healthy = ready >= desired
        return self._publish(
            timestamp,
            healthy=healthy,
            active_sessions=active_count,
            desired=desired,
            running=running,
            ready=ready,
            pool_size=len(containers),
            action=action,
            detail=detail,
        )

    def _start_replicas(
        self,
        containers: list[BackendContainer],
        count: int,
    ) -> tuple[int, str | None]:
        stopped = sorted(
            (container for container in containers if not container.running),
            key=lambda item: (item.number, item.name),
        )
        available = len(stopped)
        attempt = min(count, available)
        try:
            self._renew_lease()
            if attempt:
                self.pool.start_many(stopped, count=attempt)
        except Exception as exc:
            return 0, f"Failed to start requested backend pool capacity: {exc}"
        if attempt < count:
            required = sum(container.running for container in containers) + count
            return (
                attempt,
                f"Backend pool capacity {len(containers)} is below desired {required}; "
                "the autoscaler does not create, clone, or relabel containers. "
                "Pre-create the missing project-scoped backend replicas.",
            )
        return attempt, None

    def _renew_lease(self) -> None:
        token = self._current_lease_token
        if not token:
            raise BackendAutoscalerError("No reconciliation lease is held.")
        if not self.lease.renew(token):
            raise BackendAutoscalerError("Reconciliation lease ownership was lost.")

    def _stop_one_idle_replica(
        self,
        containers: list[BackendContainer],
        *,
        desired: int,
    ) -> tuple[bool, str]:
        candidates = sorted(
            (container for container in containers if container.running),
            key=lambda item: (item.health == "healthy", -item.number, item.name),
        )
        probe_errors: list[str] = []
        active_names: list[str] = []
        for container in candidates:
            try:
                activity = self.pool.activity(container)
            except Exception as exc:
                probe_errors.append(f"{container.name}: {exc}")
                continue
            if not activity.is_safe_to_stop(self.config.scale_down_min_idle_seconds):
                active_names.append(container.name)
                continue
            self._sleep(self.config.scale_down_probe_interval_seconds)
            try:
                second_activity = self.pool.activity(container)
            except Exception as exc:
                probe_errors.append(f"{container.name}: second probe failed: {exc}")
                continue
            if not second_activity.is_safe_to_stop(
                self.config.scale_down_min_idle_seconds
            ):
                active_names.append(container.name)
                continue
            try:
                latest = self.pool.list_containers()
                running_now = sum(item.running for item in latest)
                ready_now = sum(item.ready for item in latest)
                floor = max(self.config.min_replicas, desired)
                if running_now <= floor or ready_now <= desired:
                    return (
                        False,
                        "No replica was stopped because the final capacity recheck "
                        f"reported running={running_now}, ready={ready_now}, floor={floor}.",
                    )
                refreshed = next(
                    (
                        item
                        for item in latest
                        if item.container_id == container.container_id and item.running
                    ),
                    None,
                )
                if refreshed is None:
                    probe_errors.append(
                        f"{container.name}: state changed before the final stop"
                    )
                    continue
                # Capacity discovery can take long enough for a fresh HTTP/SSE/WS
                # request to land on the candidate. Probe once more immediately
                # before SIGTERM so the remaining race window is limited to the
                # Docker stop call itself.
                try:
                    final_activity = self.pool.activity(refreshed)
                except Exception as exc:
                    probe_errors.append(
                        f"{container.name}: final probe failed: {exc}"
                    )
                    continue
                if not final_activity.is_safe_to_stop(
                    self.config.scale_down_min_idle_seconds
                ):
                    active_names.append(container.name)
                    continue
                self._renew_lease()
                self.pool.stop(refreshed)
            except Exception as exc:
                probe_errors.append(f"{container.name}: stop failed: {exc}")
                continue
            return (
                True,
                f"Stopped {container.name} after three zero-work probes and "
                f"{float(final_activity.idle_for_seconds):.1f}s idle.",
            )
        if probe_errors:
            return (
                False,
                "No replica was stopped because activity could not be proven safe: "
                + "; ".join(probe_errors[:3]),
            )
        return (
            False,
            "No replica was stopped because all candidates still had active work or "
            f"were idle for less than {self.config.scale_down_min_idle_seconds:.1f}s: "
            + ", ".join(active_names[:5]),
        )

    def _clear_scale_down_pending(self) -> None:
        self._scale_down_target = None
        self._scale_down_pending_since = None

    def _publish(
        self,
        timestamp: float,
        *,
        healthy: bool,
        active_sessions: int | None,
        desired: int | None,
        running: int,
        pool_size: int,
        action: str,
        detail: str,
        ready: int | None = None,
    ) -> AutoscalerStatus:
        status = AutoscalerStatus(
            updated_at=timestamp,
            healthy=healthy,
            active_users=active_sessions,
            active_sessions=active_sessions,
            desired_replicas=desired,
            running_replicas=running,
            ready_replicas=running if ready is None else ready,
            pool_size=pool_size,
            action=action,
            detail=detail[:1000],
            scale_down_pending_since=self._scale_down_pending_since,
        )
        with self._lock:
            self._status = status
        return status


def build_autoscaler(config: BackendAutoscalerConfig | None = None) -> BackendAutoscaler:
    resolved = config or BackendAutoscalerConfig.from_env()
    return BackendAutoscaler(
        resolved,
        RedisActiveSessionSource(
            resolved.redis_url,
            window_seconds=resolved.active_window_seconds,
        ),
        DockerBackendPool(resolved),
        RedisReconcileLease(
            resolved.redis_url,
            project_name=resolved.project_name,
            ttl_seconds=resolved.reconcile_lease_seconds,
        ),
    )


async def _reconcile_loop(app: FastAPI, stopping: asyncio.Event) -> None:
    autoscaler: BackendAutoscaler = app.state.autoscaler
    try:
        while not stopping.is_set():
            app.state.reconcile_heartbeat = time.monotonic()
            worker = asyncio.create_task(asyncio.to_thread(autoscaler.reconcile))
            while not worker.done():
                app.state.reconcile_heartbeat = time.monotonic()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(worker),
                        timeout=autoscaler.config.poll_interval_seconds,
                    )
                except TimeoutError:
                    continue
            await worker
            app.state.reconcile_heartbeat = time.monotonic()
            try:
                await asyncio.wait_for(
                    stopping.wait(),
                    timeout=autoscaler.config.poll_interval_seconds,
                )
            except TimeoutError:
                continue
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        app.state.reconcile_failure = repr(exc)
        app.state.reconcile_heartbeat = time.monotonic()
        logger.exception("Backend autoscaler reconcile loop failed")
        raise


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Configuration is intentionally resolved at startup. Missing or generic
    # COMPOSE_PROJECT_NAME values prevent this privileged sidecar from starting.
    app.state.autoscaler = build_autoscaler()
    stopping = asyncio.Event()
    task = asyncio.create_task(_reconcile_loop(app, stopping))
    app.state.reconcile_task = task
    app.state.reconcile_failure = None
    app.state.reconcile_heartbeat = time.monotonic()
    try:
        yield
    finally:
        stopping.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


app = FastAPI(
    title="Backend Autoscaler",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
)


@app.get("/health")
def health() -> dict[str, Any]:
    autoscaler: BackendAutoscaler | None = getattr(app.state, "autoscaler", None)
    if autoscaler is None:
        raise HTTPException(status_code=503, detail="Autoscaler is not initialized.")
    task: asyncio.Task | None = getattr(app.state, "reconcile_task", None)
    failure = getattr(app.state, "reconcile_failure", None)
    heartbeat = float(getattr(app.state, "reconcile_heartbeat", 0.0) or 0.0)
    stale_for = time.monotonic() - heartbeat
    if failure or (task is not None and task.done()) or (
        heartbeat <= 0 or stale_for > autoscaler.config.watchdog_stale_seconds
    ):
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Autoscaler reconcile loop is failed or stale.",
                "failure": failure,
                "stale_for_seconds": stale_for,
            },
        )
    status = autoscaler.snapshot()
    if not status.healthy:
        raise HTTPException(status_code=503, detail=asdict(status))
    return {"status": "ok", **asdict(status)}


@app.get("/status")
def status() -> dict[str, Any]:
    autoscaler: BackendAutoscaler | None = getattr(app.state, "autoscaler", None)
    if autoscaler is None:
        raise HTTPException(status_code=503, detail="Autoscaler is not initialized.")
    return asdict(autoscaler.snapshot())

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError

from backend.services.backend_autoscaler import (
    ACTIVE_SESSIONS_KEY,
    BACKEND_ACTIVITY_PATH,
    BackendActivity,
    BackendAutoscaler,
    BackendAutoscalerConfig,
    BackendAutoscalerError,
    BackendContainer,
    DockerBackendPool,
    RedisActiveSessionSource,
    RedisReconcileLease,
    app,
    health,
)


def _config(**overrides) -> BackendAutoscalerConfig:
    values = {
        "project_name": "06-emotion-main",
        "redis_url": "redis://redis:6379/0",
        "scale_down_stabilization_seconds": 10.0,
        "scale_down_cooldown_seconds": 5.0,
        "scale_down_min_idle_seconds": 2.0,
        "compose_working_dir": str(Path(__file__).resolve().parents[1]),
        "compose_config_files": str(Path(__file__).resolve().parents[1] / "docker-compose.yml"),
        "compose_env_file": str(Path(__file__).resolve().parents[1] / "backend" / "config" / ".env"),
        "docker_cli_path": "/bin/true",
    }
    values.update(overrides)
    return BackendAutoscalerConfig(**values)


def _container(number: int, *, running: bool) -> BackendContainer:
    return BackendContainer(
        container_id=f"{number:064x}",
        name=f"06-emotion-main-backend-{number}",
        number=number,
        running=running,
        status="Up (healthy)" if running else "Exited (0)",
        health="healthy" if running else None,
    )


class _Sessions:
    def __init__(self, count: int = 0, error: Exception | None = None) -> None:
        self.value = count
        self.error = error

    def count(self, *, now=None) -> int:
        if self.error:
            raise self.error
        return self.value


class _Pool:
    def __init__(self, containers: list[BackendContainer]) -> None:
        self.containers = containers
        self.ensure_calls = 0
        self.started: list[int] = []
        self.stopped: list[int] = []
        self.activity_calls: list[int] = []
        self.activities: dict[int, BackendActivity | Exception] = {}

    def ensure_capacity(self, desired: int) -> list[BackendContainer]:
        self.ensure_calls += 1
        return list(self.containers)

    def list_containers(self) -> list[BackendContainer]:
        return list(self.containers)

    def start(self, container: BackendContainer) -> None:
        self.started.append(container.number)
        self._replace(container.number, running=True)

    def start_many(self, containers: list[BackendContainer], *, count: int) -> None:
        for container in containers[:count]:
            self.start(container)

    def stop(self, container: BackendContainer) -> None:
        self.stopped.append(container.number)
        self._replace(container.number, running=False)

    def activity(self, container: BackendContainer) -> BackendActivity:
        self.activity_calls.append(container.number)
        result = self.activities.get(container.number, BackendActivity(0, 0, 99.0))
        if isinstance(result, Exception):
            raise result
        return result

    def _replace(self, number: int, *, running: bool) -> None:
        self.containers = [
            replace(
                item,
                running=running,
                health="healthy" if running else None,
                status="Up (healthy)" if running else "Exited (0)",
            )
            if item.number == number
            else item
            for item in self.containers
        ]


class _Lease:
    def __init__(self, token: str | None = "lease-token", error: Exception | None = None):
        self.token = token
        self.error = error
        self.renewed = 0
        self.released = 0

    def acquire(self):
        if self.error:
            raise self.error
        return self.token

    def renew(self, token):
        self.renewed += 1
        return token == self.token

    def release(self, token):
        self.released += 1
        return token == self.token


def _autoscaler(config, sessions, pool, *, lease=None):
    return BackendAutoscaler(
        config,
        sessions,
        pool,
        lease or _Lease(),
        sleeper=lambda _seconds: None,
    )


@pytest.mark.parametrize("project_name", [None, "", "06", "hragent-05", "default"])
def test_config_refuses_missing_or_generic_compose_project(project_name):
    with pytest.raises(BackendAutoscalerError, match="COMPOSE_PROJECT_NAME"):
        BackendAutoscalerConfig(
            project_name=project_name or "",
            redis_url="redis://redis:6379/0",
        )


def test_config_accepts_specific_instance_and_enforces_two_to_eight_replicas():
    config = _config()
    assert config.project_name == "06-emotion-main"
    assert config.min_replicas == 2
    assert config.max_replicas == 8
    assert config.spare_replicas == 1

    with pytest.raises(BackendAutoscalerError, match="at least 2"):
        _config(min_replicas=1)
    with pytest.raises(BackendAutoscalerError, match="max <= 8"):
        _config(max_replicas=9)


@pytest.mark.parametrize(
    ("active_sessions", "expected"),
    [(0, 2), (1, 2), (8, 2), (16, 3), (17, 4), (30, 5), (60, 8), (500, 8)],
)
def test_desired_replicas_are_bounded_by_demand(active_sessions, expected):
    autoscaler = _autoscaler(_config(), _Sessions(), _Pool([]))
    assert autoscaler.desired_replicas(active_sessions) == expected


def test_scale_up_starts_all_needed_existing_pool_replicas_immediately():
    pool = _Pool([_container(number, running=number <= 2) for number in range(1, 9)])
    autoscaler = _autoscaler(_config(), _Sessions(30), pool)

    status = autoscaler.reconcile(now=100.0)

    assert pool.started == [3, 4, 5]
    assert status.running_replicas == 5
    assert status.desired_replicas == 5
    assert status.healthy is True
    assert status.action == "started 3 demand replica(s)"


def test_lease_contention_holds_without_any_pool_mutation():
    pool = _Pool([_container(1, running=True), _container(2, running=True)])
    status = _autoscaler(
        _config(),
        _Sessions(30),
        pool,
        lease=_Lease(token=None),
    ).reconcile(now=100.0)

    assert status.action == "lease_contended"
    assert status.healthy is False
    assert pool.ensure_calls == 0
    assert pool.started == []
    assert pool.stopped == []


def test_starting_replica_occupies_capacity_but_is_not_ready_or_healthy():
    containers = [
        _container(1, running=True),
        replace(_container(2, running=True), health="starting", status="Up (health: starting)"),
        _container(3, running=False),
    ]
    pool = _Pool(containers)

    status = _autoscaler(_config(), _Sessions(0), pool).reconcile(now=100.0)

    assert pool.started == []
    assert status.running_replicas == 2
    assert status.ready_replicas == 1
    assert status.healthy is False
    assert status.action == "waiting_for_readiness"


def test_pool_shortage_never_creates_or_clones_a_container():
    pool = _Pool([_container(number, running=number <= 2) for number in range(1, 4)])
    autoscaler = _autoscaler(_config(), _Sessions(64), pool)

    status = autoscaler.reconcile(now=100.0)

    assert pool.started == [3]
    assert status.running_replicas == 3
    assert status.desired_replicas == 8
    assert status.healthy is False
    assert "does not create, clone, or relabel" in status.detail


def test_scale_down_waits_for_stability_then_stops_only_one_idle_replica_per_cooldown():
    pool = _Pool([_container(number, running=True) for number in range(1, 9)])
    autoscaler = _autoscaler(_config(), _Sessions(0), pool)

    assert autoscaler.reconcile(now=0.0).action == "scale_down_stabilizing"
    assert autoscaler.reconcile(now=9.0).action == "scale_down_stabilizing"
    first = autoscaler.reconcile(now=10.0)
    assert first.action == "stopped 1 idle replica"
    assert pool.stopped == [8]
    assert pool.activity_calls[-3:] == [8, 8, 8]

    assert autoscaler.reconcile(now=14.0).action == "scale_down_cooldown"
    second = autoscaler.reconcile(now=15.0)
    assert second.action == "stopped 1 idle replica"
    assert pool.stopped == [8, 7]
    assert pool.activity_calls[-3:] == [7, 7, 7]


def test_scale_down_skips_candidate_when_work_arrives_after_capacity_recheck():
    class _LateActivityPool(_Pool):
        def activity(self, container: BackendContainer) -> BackendActivity:
            self.activity_calls.append(container.number)
            if container.number == 3 and self.activity_calls.count(3) == 3:
                return BackendActivity(
                    active_http_requests=1,
                    active_websockets=0,
                    idle_for_seconds=0.0,
                )
            return BackendActivity(
                active_http_requests=0,
                active_websockets=0,
                idle_for_seconds=99.0,
            )

    pool = _LateActivityPool(
        [_container(number, running=True) for number in range(1, 4)]
    )
    autoscaler = _autoscaler(_config(), _Sessions(0), pool)

    autoscaler.reconcile(now=0.0)
    status = autoscaler.reconcile(now=10.0)

    assert pool.stopped == [2]
    assert pool.activity_calls == [3, 3, 3, 2, 2, 2]
    assert status.action == "stopped 1 idle replica"


def test_health_fails_when_reconcile_task_has_died(monkeypatch):
    autoscaler = _autoscaler(_config(), _Sessions(), _Pool([]))

    class _DoneTask:
        @staticmethod
        def done():
            return True

    monkeypatch.setattr(app.state, "autoscaler", autoscaler, raising=False)
    monkeypatch.setattr(app.state, "reconcile_task", _DoneTask(), raising=False)
    monkeypatch.setattr(app.state, "reconcile_failure", "boom", raising=False)
    monkeypatch.setattr(app.state, "reconcile_heartbeat", 1.0, raising=False)

    with pytest.raises(HTTPException) as exc_info:
        health()
    assert exc_info.value.status_code == 503
    assert "failed or stale" in str(exc_info.value.detail)


def test_scale_down_refuses_replica_with_active_websocket_or_http_work():
    pool = _Pool([_container(number, running=True) for number in range(1, 4)])
    pool.activities = {
        1: BackendActivity(1, 0, 0.0),
        2: BackendActivity(0, 1, 0.0),
        3: BackendActivity(0, 0, 1.0),
    }
    autoscaler = _autoscaler(_config(), _Sessions(0), pool)

    autoscaler.reconcile(now=0.0)
    status = autoscaler.reconcile(now=10.0)

    assert pool.stopped == []
    assert status.action == "scale_down_blocked"
    assert "active work" in status.detail


def test_scale_down_fails_closed_when_activity_probe_is_unavailable():
    pool = _Pool([_container(number, running=True) for number in range(1, 4)])
    pool.activities = {
        1: BackendAutoscalerError("timeout"),
        2: BackendAutoscalerError("timeout"),
        3: BackendAutoscalerError("timeout"),
    }
    autoscaler = _autoscaler(_config(), _Sessions(0), pool)

    autoscaler.reconcile(now=0.0)
    status = autoscaler.reconcile(now=10.0)

    assert pool.stopped == []
    assert status.action == "scale_down_blocked"
    assert "could not be proven safe" in status.detail


def test_redis_failure_preserves_running_replicas_and_cancels_scale_down():
    pool = _Pool([_container(number, running=True) for number in range(1, 5)])
    sessions = _Sessions(0)
    autoscaler = _autoscaler(_config(), sessions, pool)
    autoscaler.reconcile(now=0.0)
    sessions.error = RedisError("unavailable")

    status = autoscaler.reconcile(now=20.0)

    assert pool.stopped == []
    assert status.running_replicas == 4
    assert status.desired_replicas is None
    assert status.scale_down_pending_since is None
    assert "replicas were preserved" in status.detail


def test_minimum_replicas_start_even_when_redis_is_unavailable():
    pool = _Pool([_container(number, running=False) for number in range(1, 5)])
    autoscaler = _autoscaler(
        _config(),
        _Sessions(error=RedisError("unavailable")),
        pool,
    )

    status = autoscaler.reconcile(now=100.0)

    assert pool.started == [1, 2]
    assert status.running_replicas == 2
    assert status.healthy is True
    assert "replicas were preserved" in status.detail


class _Redis:
    def __init__(self, session_ids, payloads):
        self.session_ids = session_ids
        self.payloads = payloads
        self.calls = []

    def zrangebyscore(self, *args):
        self.calls.append(("zrangebyscore", args))
        return self.session_ids

    def mget(self, keys):
        self.calls.append(("mget", tuple(keys)))
        return self.payloads


def test_redis_source_is_read_only_and_counts_distinct_users():
    redis = _Redis(
        ["s1", "s2", "s3"],
        [
            json.dumps({"user_id": "u1"}),
            json.dumps({"user_id": "u1"}),
            "{bad-json",
        ],
    )
    source = RedisActiveSessionSource(
        "redis://redis:6379/0",
        window_seconds=120.0,
        client=redis,
    )

    assert source.count(now=1000.0) == 2
    assert redis.calls == [
        ("zrangebyscore", (ACTIVE_SESSIONS_KEY, 880.0, "+inf")),
        (
            "mget",
            (
                "hragent:auth:session:s1",
                "hragent:auth:session:s2",
                "hragent:auth:session:s3",
            ),
        ),
    ]


class _Response:
    def __init__(self, payload, *, status_code=200, text="") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


def _docker_labels(**overrides):
    labels = {
        "com.docker.compose.project": "06-emotion-main",
        "com.docker.compose.service": "backend",
        "com.docker.compose.oneoff": "False",
        "com.docker.compose.container-number": "1",
        "com.docker.compose.project.working_dir": "/srv/hr-agent/06_emotion",
        "com.docker.compose.project.config_files": "/srv/hr-agent/06_emotion/docker-compose.yml",
    }
    labels.update(overrides)
    return labels


def test_docker_pool_lists_only_exact_project_service_and_non_oneoff(monkeypatch):
    config = _config(
        compose_working_dir="/srv/hr-agent/06_emotion",
        compose_config_files="/srv/hr-agent/06_emotion/docker-compose.yml",
    )
    pool = DockerBackendPool(config)
    captured = {}

    def request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return _Response(
            [
                {
                    "Id": "a" * 64,
                    "Names": ["/06-emotion-main-backend-1"],
                    "Labels": _docker_labels(),
                    "State": "exited",
                    "Status": "Exited (0)",
                }
            ]
        )

    monkeypatch.setattr(pool, "_docker_request", request)

    assert pool.list_containers()[0].name == "06-emotion-main-backend-1"
    assert json.loads(captured["params"]["filters"]) == {
        "label": [
            "com.docker.compose.project=06-emotion-main",
            "com.docker.compose.service=backend",
            "com.docker.compose.oneoff=False",
            "com.docker.compose.project.working_dir=/srv/hr-agent/06_emotion",
            "com.docker.compose.project.config_files=/srv/hr-agent/06_emotion/docker-compose.yml",
        ]
    }


def test_docker_pool_revalidates_labels_even_after_filtered_query(monkeypatch):
    pool = DockerBackendPool(_config())
    monkeypatch.setattr(
        pool,
        "_docker_request",
        lambda *args, **kwargs: _Response(
            [
                {
                    "Id": "a" * 64,
                    "Labels": _docker_labels(
                        **{"com.docker.compose.project": "someone-else-project"}
                    ),
                    "State": "exited",
                }
            ]
        ),
    )

    with pytest.raises(BackendAutoscalerError, match="exact backend project"):
        pool.list_containers()


def test_activity_probe_uses_fixed_internal_path_and_requires_complete_contract(monkeypatch):
    pool = DockerBackendPool(_config())
    captured = {}
    inspected = {
        "Id": "a" * 64,
        "Config": {"Labels": _docker_labels()},
        "NetworkSettings": {
            "Networks": {
                "06-emotion-main_default": {"IPAddress": "172.20.0.8"},
            }
        },
    }
    monkeypatch.setattr(pool, "_inspect_action_target", lambda _container_id: inspected)

    def activity_request(url):
        captured["url"] = url
        return _Response(
            {
                "active_http_requests": 0,
                "active_websockets": 0,
                "idle_for_seconds": 61.5,
            }
        )

    monkeypatch.setattr(pool, "_activity_request", activity_request)
    result = pool.activity(_container(1, running=True))

    assert captured["url"] == f"http://172.20.0.8:7111{BACKEND_ACTIVITY_PATH}"
    assert result.is_safe_to_stop(60.0) is True

    monkeypatch.setattr(
        pool,
        "_activity_request",
        lambda _url: _Response({"active_http_requests": 0, "active_websockets": 0}),
    )
    assert pool.activity(_container(1, running=True)).is_safe_to_stop(0.0) is False

    monkeypatch.setattr(pool, "_activity_request", lambda _url: _Response({}))
    with pytest.raises(BackendAutoscalerError, match="contract is incomplete"):
        pool.activity(_container(1, running=True))


def _pool_summary(
    number: int,
    *,
    running: bool,
    config: BackendAutoscalerConfig,
    service: str = "backend",
    working_dir: str | None = None,
    config_files: str | None = None,
):
    labels = {
        "com.docker.compose.project": config.project_name,
        "com.docker.compose.service": service,
        "com.docker.compose.oneoff": "False",
        "com.docker.compose.container-number": str(number),
        "com.docker.compose.project.working_dir": (
            working_dir if working_dir is not None else str(config.compose_working_dir)
        ),
        "com.docker.compose.project.config_files": (
            config_files if config_files is not None else str(config.compose_config_files)
        ),
    }
    return {
        "Id": f"{number:064x}",
        "Names": [f"/{config.project_name}-{service}-{number}"],
        "Labels": labels,
        "State": "running" if running else "exited",
        "Status": "Up (healthy)" if running else "Exited (0)",
    }


def _pool_inspection(
    summary: dict,
    *,
    image_id: str = "sha256:current-backend-image",
    config_hash: str = "current-compose-config",
    running: bool = False,
) -> dict:
    labels = dict(summary["Labels"])
    labels["com.docker.compose.config-hash"] = config_hash
    return {
        "Id": summary["Id"],
        "Image": image_id,
        "Config": {"Labels": labels},
        "State": {
            "Status": "running" if running else "created",
            "Running": running,
            "Health": {"Status": "healthy"} if running else {},
        },
    }


def _provisioning_config(tmp_path: Path, **overrides) -> BackendAutoscalerConfig:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config_file = project_dir / "docker-compose.yml"
    config_file.write_text("services: {}\n", encoding="utf-8")
    env_file = project_dir / ".env"
    env_file.write_text("COMPOSE_PROJECT_NAME=06-emotion-main\n", encoding="utf-8")
    docker_cli = tmp_path / "docker"
    docker_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker_cli.chmod(0o755)
    return _config(
        compose_working_dir=str(project_dir),
        compose_config_files=str(config_file),
        compose_env_file=str(env_file),
        docker_cli_path=str(docker_cli),
        **overrides,
    )


def test_pool_provisioning_uses_exact_argv_and_leaves_new_replicas_stopped(
    monkeypatch,
    tmp_path,
):
    config = _provisioning_config(tmp_path)
    before = [
        _pool_summary(number, running=True, config=config)
        for number in range(1, 3)
    ]
    after = [
        _pool_summary(number, running=number <= 2, config=config)
        for number in range(1, 9)
    ]
    docker_payloads = iter([before, after])
    invocations = []

    def command_runner(argv, **kwargs):
        invocations.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    pool = DockerBackendPool(config, command_runner=command_runner)
    monkeypatch.setattr(
        pool,
        "_docker_request",
        lambda *args, **kwargs: _Response(next(docker_payloads)),
    )

    containers = pool.ensure_capacity(8)

    assert len(containers) == 8
    assert sum(container.running for container in containers) == 2
    assert len(invocations) == 1
    argv, kwargs = invocations[0]
    assert argv == [
        str(config.docker_cli_path),
        "compose",
        "--project-name",
        "06-emotion-main",
        "--project-directory",
        str(config.compose_working_dir),
        "--env-file",
        str(config.compose_env_file),
        "-f",
        str(config.compose_config_files),
        "up",
        "--no-start",
        "--no-deps",
        "--no-recreate",
        "--no-build",
        "--scale",
        "backend=8",
        "backend",
    ]
    assert kwargs == {
        "cwd": str(config.compose_working_dir),
        "capture_output": True,
        "text": True,
        "timeout": 180.0,
        "check": False,
        "shell": False,
    }


def test_all_stopped_pool_is_recreated_as_one_trusted_generation_and_started(
    monkeypatch,
    tmp_path,
):
    config = _provisioning_config(tmp_path)
    before = [
        _pool_summary(number, running=False, config=config)
        for number in range(1, 9)
    ]
    after = []
    for number in range(1, 9):
        summary = _pool_summary(number, running=False, config=config)
        summary["Id"] = f"{number + 100:064x}"
        summary["State"] = "created"
        summary["Status"] = "Created"
        after.append(summary)

    invocations = []
    started: set[str] = set()
    list_calls = 0

    def command_runner(argv, **kwargs):
        invocations.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def docker_request(method, path, **kwargs):
        nonlocal list_calls
        if method == "GET" and path == "/containers/json":
            list_calls += 1
            return _Response(before if list_calls == 1 else after)
        if method == "GET" and path.endswith("/json"):
            container_id = path.split("/")[2]
            summary = next(item for item in after if item["Id"] == container_id)
            return _Response(
                _pool_inspection(summary, running=container_id in started)
            )
        if method == "POST" and path.endswith("/start"):
            started.add(path.split("/")[2])
            return _Response({})
        raise AssertionError(f"unexpected Docker request: {method} {path} {kwargs}")

    pool = DockerBackendPool(
        config,
        command_runner=command_runner,
        sleeper=lambda _seconds: None,
    )
    monkeypatch.setattr(pool, "_docker_request", docker_request)

    containers = pool.ensure_capacity(8)
    pool.start_many(containers, count=2)

    assert len(invocations) == 1
    argv, _ = invocations[0]
    assert "--force-recreate" in argv
    assert "--no-recreate" not in argv
    assert argv[-3:] == ["--scale", "backend=8", "backend"]
    assert started == {after[0]["Id"], after[1]["Id"]}


def test_all_stopped_recovery_refuses_mixed_image_or_config_generation(
    monkeypatch,
    tmp_path,
):
    config = _provisioning_config(tmp_path)
    before = [
        _pool_summary(number, running=False, config=config)
        for number in range(1, 9)
    ]
    after = []
    for number in range(1, 9):
        summary = _pool_summary(number, running=False, config=config)
        summary["Id"] = f"{number + 100:064x}"
        summary["State"] = "created"
        summary["Status"] = "Created"
        after.append(summary)

    list_calls = 0
    start_calls = 0

    def command_runner(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def docker_request(method, path, **kwargs):
        nonlocal list_calls, start_calls
        if method == "GET" and path == "/containers/json":
            list_calls += 1
            return _Response(before if list_calls == 1 else after)
        if method == "GET" and path.endswith("/json"):
            container_id = path.split("/")[2]
            summary = next(item for item in after if item["Id"] == container_id)
            image_id = (
                "sha256:stale-backend-image"
                if summary is after[-1]
                else "sha256:current-backend-image"
            )
            return _Response(_pool_inspection(summary, image_id=image_id))
        if method == "POST" and path.endswith("/start"):
            start_calls += 1
            return _Response({})
        raise AssertionError(f"unexpected Docker request: {method} {path} {kwargs}")

    pool = DockerBackendPool(config, command_runner=command_runner)
    monkeypatch.setattr(pool, "_docker_request", docker_request)

    with pytest.raises(BackendAutoscalerError, match="do not share one image ID"):
        pool.ensure_capacity(8)
    assert start_calls == 0


def test_running_unhealthy_generation_is_not_force_recreated_or_trusted(
    monkeypatch,
    tmp_path,
):
    config = _provisioning_config(tmp_path)
    containers = [
        _pool_summary(number, running=number == 1, config=config)
        for number in range(1, 9)
    ]
    containers[0]["Status"] = "Up (unhealthy)"
    invoked = False

    def command_runner(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("must not force-recreate a running generation")

    def docker_request(method, path, **kwargs):
        if method == "GET" and path == "/containers/json":
            return _Response(containers)
        if method == "GET" and path.endswith("/json"):
            container_id = path.split("/")[2]
            summary = next(item for item in containers if item["Id"] == container_id)
            return _Response(_pool_inspection(summary, running=summary["State"] == "running"))
        raise AssertionError(f"unexpected Docker request: {method} {path} {kwargs}")

    pool = DockerBackendPool(config, command_runner=command_runner)
    monkeypatch.setattr(pool, "_docker_request", docker_request)

    discovered = pool.ensure_capacity(8)
    with pytest.raises(BackendAutoscalerError, match="no healthy running template"):
        pool.start_many(discovered[1:], count=1)
    assert invoked is False


def test_pool_provisioning_refuses_foreign_working_directory_before_cli(
    monkeypatch,
    tmp_path,
):
    config = _provisioning_config(tmp_path)
    payload = [
        _pool_summary(1, running=True, config=config),
        _pool_summary(
            2,
            running=True,
            config=config,
            service="frontend",
            working_dir="/home/another-user/hr_agent/06_emotion",
        ),
    ]
    invoked = False

    def command_runner(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("must not invoke Docker Compose")

    pool = DockerBackendPool(config, command_runner=command_runner)
    monkeypatch.setattr(
        pool,
        "_docker_request",
        lambda *args, **kwargs: _Response(payload),
    )

    with pytest.raises(BackendAutoscalerError, match="mismatched labels"):
        pool.ensure_capacity(8)
    assert invoked is False


@pytest.mark.parametrize("missing", ["docker_cli", "config_file"])
def test_pool_provisioning_refuses_missing_cli_or_path_before_invocation(
    monkeypatch,
    tmp_path,
    missing,
):
    config = _provisioning_config(tmp_path)
    values = {
        "docker_cli_path": (
            str(tmp_path / "missing-docker")
            if missing == "docker_cli"
            else config.docker_cli_path
        ),
        "compose_config_files": (
            str(tmp_path / "missing-compose.yml")
            if missing == "config_file"
            else config.compose_config_files
        ),
    }
    config = replace(config, **values)
    payload = [
        _pool_summary(number, running=True, config=config)
        for number in range(1, 3)
    ]
    invoked = False

    def command_runner(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("must not invoke Docker Compose")

    pool = DockerBackendPool(config, command_runner=command_runner)
    monkeypatch.setattr(
        pool,
        "_docker_request",
        lambda *args, **kwargs: _Response(payload),
    )

    with pytest.raises(BackendAutoscalerError, match="missing|executable"):
        pool.ensure_capacity(8)
    assert invoked is False


def test_pool_provisioning_skips_cli_when_pool_is_already_full(monkeypatch, tmp_path):
    config = _provisioning_config(tmp_path)
    payload = [
        _pool_summary(number, running=number <= 2, config=config)
        for number in range(1, 9)
    ]

    def command_runner(*args, **kwargs):
        raise AssertionError("must not invoke Docker Compose")

    pool = DockerBackendPool(config, command_runner=command_runner)
    monkeypatch.setattr(
        pool,
        "_docker_request",
        lambda *args, **kwargs: _Response(payload),
    )

    containers = pool.ensure_capacity(8)

    assert len(containers) == 8
    assert sum(container.running for container in containers) == 2

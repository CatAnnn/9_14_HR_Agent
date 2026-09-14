from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

import pytest


SOURCE_WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "compose.sh"


def test_wrapper_resolves_the_host_docker_socket_group() -> None:
    wrapper = SOURCE_WRAPPER.read_text(encoding="utf-8")

    assert "stat -c '%g' /var/run/docker.sock" in wrapper
    assert 'export DOCKER_SOCKET_GID="${docker_socket_gid}"' in wrapper


def _container(
    root: Path,
    *,
    service: str = "backend",
    running: bool = True,
    working_dir: str | None = None,
    config_files: str | None = None,
) -> dict[str, object]:
    return {
        "project": "06-emotion-main",
        "working_dir": working_dir or str(root),
        "config_files": config_files or str(root / "docker-compose.yml"),
        "service": service,
        "running": running,
    }


def _run_wrapper(
    tmp_path: Path,
    arguments: list[str],
    *,
    containers: dict[str, dict[str, object]] | None = None,
    containers_after_lock: dict[str, dict[str, object]] | None = None,
    containers_after_compose: dict[str, dict[str, object]] | None = None,
    model_mode: str = "local",
    asr_mode: str = "local",
    tts_enabled: str = "true",
    environment_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, list[list[str]]]:
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    (root / "backend" / "config").mkdir(parents=True)
    wrapper = root / "scripts" / "compose.sh"
    wrapper.write_text(SOURCE_WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
    wrapper.chmod(0o755)
    (root / "docker-compose.yml").write_text(
        'name: "06-emotion-main"\nservices: {}\n',
        encoding="utf-8",
    )
    (root / "backend" / "config" / ".env").write_text(
        "\n".join(
            (
                f"MODEL_PROVIDER_MODE={model_mode}",
                f"ASR_PROVIDER_MODE={asr_mode}",
                f"TTS_ENABLED={tts_enabled}",
                "",
            )
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

arguments = sys.argv[1:]
with Path(os.environ["FAKE_DOCKER_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(arguments) + "\\n")

redis_state_path = Path(os.environ["FAKE_REDIS_STATE"])
compose_state_path = Path(os.environ["FAKE_COMPOSE_STATE"])
stopped_state_path = Path(os.environ["FAKE_STOPPED_STATE"])
activity_state_path = Path(os.environ["FAKE_ACTIVITY_STATE"])
if redis_state_path.exists():
    redis_state = json.loads(redis_state_path.read_text(encoding="utf-8"))
else:
    redis_state = {}
if stopped_state_path.exists():
    stopped_ids = set(json.loads(stopped_state_path.read_text(encoding="utf-8")))
else:
    stopped_ids = set()
container_source = "FAKE_DOCKER_CONTAINERS"
if compose_state_path.exists() and os.environ.get("FAKE_DOCKER_CONTAINERS_AFTER_COMPOSE"):
    container_source = "FAKE_DOCKER_CONTAINERS_AFTER_COMPOSE"
elif redis_state.get("owner") and os.environ.get("FAKE_DOCKER_CONTAINERS_AFTER_LOCK"):
    container_source = "FAKE_DOCKER_CONTAINERS_AFTER_LOCK"
containers = json.loads(os.environ.get(container_source, "{}"))
if arguments[:2] == ["ps", "-aq"]:
    print("\\n".join(containers))
elif arguments and arguments[0] == "inspect":
    container = containers[arguments[-1]]
    running = bool(container["running"]) and arguments[-1] not in stopped_ids
    health = str(container.get("health") or ("healthy" if running else "none"))
    container_number = str(
        container.get("number") or arguments[-1].rsplit("-", 1)[-1]
    )
    print(
        "\\x1f".join(
            (
                str(container["project"]),
                str(container["working_dir"]),
                str(container["config_files"]),
                str(container["service"]),
                str(running).lower(),
                container_number,
                str(container.get("oneoff", False)).lower(),
                health,
            )
        )
    )
elif arguments and arguments[0] == "stop":
    container_id = arguments[-1]
    stopped_ids.add(container_id)
    stopped_state_path.write_text(json.dumps(sorted(stopped_ids)), encoding="utf-8")
    print(container_id)
elif arguments and arguments[0] == "exec":
    if len(arguments) > 2 and arguments[2] == "python3":
        time.sleep(float(os.environ.get("FAKE_BACKEND_ACTIVITY_DELAY_SECONDS", "0")))
        activity_probes = (
            int(activity_state_path.read_text(encoding="utf-8"))
            if activity_state_path.exists()
            else 0
        ) + 1
        activity_state_path.write_text(str(activity_probes), encoding="utf-8")
        busy_probes = int(os.environ.get("FAKE_BACKEND_ACTIVITY_BUSY_PROBES", "0"))
        print(
            "busy"
            if activity_probes <= busy_probes
            else os.environ.get("FAKE_BACKEND_ACTIVITY", "safe")
        )
    else:
        state_path = redis_state_path
        state = redis_state
        operation = arguments[4]
        if operation == "SET":
            state["set_attempts"] = int(state.get("set_attempts", 0)) + 1
            contention_count = int(os.environ.get("FAKE_REDIS_CONTENTION_COUNT", "0"))
            if state["set_attempts"] <= contention_count:
                print("")
            elif "owner" in state:
                print("")
            else:
                state["key"] = arguments[5]
                state["owner"] = arguments[6]
                state["ttl_ms"] = arguments[9]
                print("OK")
        elif operation == "EVAL":
            script = arguments[5]
            key = arguments[7]
            token = arguments[8]
            if (
                "DEL" in script
                and os.environ.get("FAKE_REDIS_REPLACE_TOKEN_BEFORE_RELEASE") == "true"
                and not state.get("replacement_done")
            ):
                state["owner"] = "replacement-token"
                state["replacement_done"] = True
            if state.get("key") == key and state.get("owner") == token:
                if "PEXPIRE" in script:
                    state["pexpire_attempts"] = int(state.get("pexpire_attempts", 0)) + 1
                    lose_on_attempt = int(
                        os.environ.get("FAKE_REDIS_LOSE_OWNERSHIP_ON_PEXPIRE", "0")
                    )
                    if state["pexpire_attempts"] == lose_on_attempt:
                        state["owner"] = "replacement-token"
                        print("0")
                    else:
                        state["ttl_ms"] = arguments[9]
                        print("1")
                elif "DEL" in script:
                    state.pop("owner", None)
                    print("1")
            else:
                print("0")
        state_path.write_text(json.dumps(state), encoding="utf-8")
elif arguments and arguments[0] == "compose" and "up" in arguments:
    time.sleep(float(os.environ.get("FAKE_COMPOSE_DELAY_SECONDS", "0")))
    exit_code = int(os.environ.get("FAKE_COMPOSE_EXIT_CODE", "0"))
    if exit_code == 0:
        compose_state_path.write_text("completed", encoding="utf-8")
    raise SystemExit(exit_code)
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    fake_date = fake_bin / "date"
    fake_date.write_text(
        """#!/usr/bin/env python3
import os
import sys

if (
    os.environ.get("FAKE_DATE_FAIL_FRACTIONAL") == "true"
    and sys.argv[1:] == ["+%s.%N"]
):
    raise SystemExit(1)
os.execv("/usr/bin/date", ["/usr/bin/date", *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    fake_date.chmod(0o755)

    log = tmp_path / "docker.jsonl"
    environment = os.environ.copy()
    environment.pop("COMPOSE_PROJECT_NAME", None)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_DOCKER_CONTAINERS": json.dumps(containers or {}),
            "FAKE_REDIS_STATE": str(tmp_path / "redis-state.json"),
            "FAKE_COMPOSE_STATE": str(tmp_path / "compose-state"),
            "FAKE_STOPPED_STATE": str(tmp_path / "stopped-state.json"),
            "FAKE_ACTIVITY_STATE": str(tmp_path / "activity-state"),
            "HR_AGENT_COMPOSE_BACKEND_RESTORE_PROBE_SECONDS": "0.01",
            "HR_AGENT_COMPOSE_BACKEND_RESTORE_WAIT_SECONDS": "15",
        }
    )
    if containers_after_lock is not None:
        environment["FAKE_DOCKER_CONTAINERS_AFTER_LOCK"] = json.dumps(
            containers_after_lock
        )
    if containers_after_compose is not None:
        environment["FAKE_DOCKER_CONTAINERS_AFTER_COMPOSE"] = json.dumps(
            containers_after_compose
        )
    environment.update(environment_overrides or {})
    result = subprocess.run(
        [str(wrapper), *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    commands = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return result, root, commands


def _locking_containers(root: Path) -> dict[str, dict[str, object]]:
    return {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "redis-1": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }


def _final_compose_command(root: Path, *arguments: str) -> list[str]:
    return [
        "compose",
        "--project-name",
        "06-emotion-main",
        "--project-directory",
        str(root),
        "--env-file",
        str(root / "backend" / "config" / ".env"),
        "-f",
        str(root / "docker-compose.yml"),
        *arguments,
    ]


def test_full_up_preserves_complete_backend_pool(tmp_path: Path) -> None:
    root = tmp_path / "project"
    containers = {
        f"backend-{number}": _container(root)
        for number in range(1, 6)
    }
    containers["backend-6"] = _container(root, running=False)
    containers["frontend-1"] = _container(
        root, service="frontend", running=True
    )
    containers["redis-1"] = _container(root, service="redis")
    containers["autoscaler-1"] = _container(root, service="backend_autoscaler")
    after_compose = {
        container_id: {**container, "running": True}
        if container["service"] == "backend"
        else container
        for container_id, container in containers.items()
    }

    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d", "--build"],
        containers=containers,
        containers_after_compose=after_compose,
    )

    assert result.returncode == 0, result.stderr
    assert commands[0] == [
        "ps",
        "-aq",
        "--filter",
        "label=com.docker.compose.project=06-emotion-main",
    ]
    assert _final_compose_command(
        root, "up", "-d", "--build", "--scale", "backend=6"
    ) in commands
    assert ["stop", "--time", "300", "backend-6"] in commands


def test_first_full_up_starts_with_two_backends(tmp_path: Path) -> None:
    result, root, commands = _run_wrapper(tmp_path, ["up", "-d"])

    assert result.returncode == 0, result.stderr
    assert commands == [
        [
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=06-emotion-main",
        ],
        _final_compose_command(
            root, "up", "-d", "--scale", "backend=2"
        ),
    ]


def test_full_up_restores_minimum_running_count_for_stopped_pool(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = {
        f"backend-{number}": _container(root, running=False)
        for number in range(1, 9)
    }
    containers["redis-1"] = _container(root, service="redis")
    containers["autoscaler-1"] = _container(root, service="backend_autoscaler")
    after_compose = {
        container_id: {**container, "running": True}
        if container["service"] == "backend"
        else container
        for container_id, container in containers.items()
    }

    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
        containers_after_compose=after_compose,
    )

    assert result.returncode == 0, result.stderr
    assert _final_compose_command(
        root, "up", "-d", "--scale", "backend=8"
    ) in commands
    assert [
        command[-1]
        for command in commands
        if command[:3] == ["stop", "--time", "300"]
    ] == [f"backend-{number}" for number in range(8, 2, -1)]


def test_backend_restore_refuses_to_stop_a_busy_replica(tmp_path: Path) -> None:
    root = tmp_path / "project"
    containers = {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "backend-3": _container(root, running=False),
        "redis-1": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }
    after_compose = {
        container_id: {**container, "running": True}
        if container["service"] == "backend"
        else container
        for container_id, container in containers.items()
    }

    started_at = time.monotonic()
    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
        containers_after_compose=after_compose,
        environment_overrides={
            "FAKE_BACKEND_ACTIVITY": "busy",
            "FAKE_BACKEND_ACTIVITY_DELAY_SECONDS": "10",
            "HR_AGENT_COMPOSE_BACKEND_RESTORE_WAIT_SECONDS": "0.2",
        },
    )
    elapsed = time.monotonic() - started_at

    assert result.returncode == 3
    assert "zero-work state could not be proven" in result.stderr
    assert all(command[:1] != ["stop"] for command in commands)
    assert elapsed < 4


def test_backend_restore_fails_closed_when_deadline_time_is_unavailable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "backend-3": _container(root, running=False),
        "redis-1": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }
    after_compose = {
        container_id: {**container, "running": True}
        if container["service"] == "backend"
        else container
        for container_id, container in containers.items()
    }

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
        containers_after_compose=after_compose,
        environment_overrides={"FAKE_DATE_FAIL_FRACTIONAL": "true"},
    )

    assert result.returncode == 3
    assert "could not establish a bounded idle-wait deadline" in result.stderr
    assert all(command[:1] != ["stop"] for command in commands)


def test_backend_restore_waits_for_a_fresh_replica_to_become_idle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "backend-3": _container(root, running=False),
        "redis-1": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }
    after_compose = {
        container_id: {**container, "running": True}
        if container["service"] == "backend"
        else container
        for container_id, container in containers.items()
    }

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
        containers_after_compose=after_compose,
        environment_overrides={"FAKE_BACKEND_ACTIVITY_BUSY_PROBES": "2"},
    )

    assert result.returncode == 0, result.stderr
    assert ["stop", "--time", "300", "backend-3"] in commands
    assert len(
        [
            command
            for command in commands
            if command[:1] == ["exec"] and command[2:3] == ["python3"]
        ]
    ) >= 5


def test_backend_restore_preserves_healthy_capacity_floor(tmp_path: Path) -> None:
    root = tmp_path / "project"
    containers = {
        "backend-1": {**_container(root), "health": "starting"},
        "backend-2": _container(root),
        "backend-3": _container(root, running=False),
        "redis-1": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }
    after_compose = {
        container_id: {
            **container,
            "running": True,
            "health": "starting" if container_id == "backend-1" else "healthy",
        }
        if container["service"] == "backend"
        else container
        for container_id, container in containers.items()
    }

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
        containers_after_compose=after_compose,
    )

    assert result.returncode == 3
    assert "healthy capacity floor was reached" in result.stderr
    assert all(command[:1] != ["stop"] for command in commands)


def test_backend_restore_aborts_before_first_stop_if_lease_is_lost(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "backend-3": _container(root, running=False),
        "redis-1": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }
    after_compose = {
        container_id: {**container, "running": True}
        if container["service"] == "backend"
        else container
        for container_id, container in containers.items()
    }

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
        containers_after_compose=after_compose,
        environment_overrides={"FAKE_REDIS_LOSE_OWNERSHIP_ON_PEXPIRE": "3"},
    )

    assert result.returncode == 3
    assert "lease ownership or zero-work state could not be proven" in result.stderr
    assert all(command[:1] != ["stop"] for command in commands)


def test_backend_restore_stops_no_more_replicas_after_mid_restore_lease_loss(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "backend-3": _container(root, running=False),
        "backend-4": _container(root, running=False),
        "redis-1": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }
    after_compose = {
        container_id: {**container, "running": True}
        if container["service"] == "backend"
        else container
        for container_id, container in containers.items()
    }

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
        containers_after_compose=after_compose,
        environment_overrides={"FAKE_REDIS_LOSE_OWNERSHIP_ON_PEXPIRE": "6"},
    )

    assert result.returncode == 3
    assert "no further replicas will be changed" in result.stderr
    assert [
        command[-1]
        for command in commands
        if command[:3] == ["stop", "--time", "300"]
    ] == ["backend-4"]


def test_backend_oneoff_container_is_not_counted_as_pool_capacity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "backend-run": {
            **_container(root),
            "number": 2,
            "oneoff": True,
        },
    }

    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
    )

    assert result.returncode == 0, result.stderr
    assert commands[-1] == _final_compose_command(
        root, "up", "-d", "--scale", "backend=2"
    )


@pytest.mark.parametrize(
    "scale_arguments",
    (["--scale", "backend=7"], ["--scale=backend=7"]),
)
def test_explicit_backend_scale_is_not_overridden(
    tmp_path: Path,
    scale_arguments: list[str],
) -> None:
    containers = {
        f"backend-{number}": _container(tmp_path / "project")
        for number in range(1, 6)
    }

    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d", *scale_arguments],
        containers=containers,
    )

    assert result.returncode == 0, result.stderr
    assert commands[-1] == _final_compose_command(
        root, "up", "-d", *scale_arguments
    )


def test_dry_run_never_executes_real_profile_cleanup(tmp_path: Path) -> None:
    containers = {"backend-1": _container(tmp_path / "project")}

    result, root, commands = _run_wrapper(
        tmp_path,
        ["--dry-run", "up", "-d"],
        containers=containers,
        model_mode="platform",
        asr_mode="bosch",
        tts_enabled="false",
    )

    assert result.returncode == 0, result.stderr
    assert all("rm" not in command for command in commands)
    assert commands[-1] == _final_compose_command(
        root, "--dry-run", "up", "-d", "--scale", "backend=2"
    )


def test_platform_bosch_up_cleans_all_disabled_gpu_services(tmp_path: Path) -> None:
    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        model_mode="platform",
        asr_mode="bosch",
        tts_enabled="false",
    )

    assert result.returncode == 0, result.stderr
    assert _final_compose_command(
        root,
        "--profile",
        "local",
        "rm",
        "--stop",
        "--force",
        "qwen_embedding",
        "qwen_reranker",
    ) in commands
    assert _final_compose_command(
        root,
        "--profile",
        "asr-local",
        "rm",
        "--stop",
        "--force",
        "qwen3_asr",
        "qwen3_asr_secondary",
    ) in commands
    assert commands[-1] == _final_compose_command(
        root, "up", "-d", "--scale", "backend=2"
    )


def test_targeted_up_that_includes_backend_preserves_running_capacity(
    tmp_path: Path,
) -> None:
    containers = {
        f"backend-{number}": _container(tmp_path / "project")
        for number in range(1, 6)
    }

    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d", "backend", "frontend", "backend_autoscaler"],
        containers=containers,
    )

    assert result.returncode == 0, result.stderr
    assert commands[-1] == _final_compose_command(
        root,
        "up",
        "-d",
        "backend",
        "frontend",
        "backend_autoscaler",
        "--scale",
        "backend=5",
    )


def test_targeted_frontend_up_preserves_running_backend_capacity(
    tmp_path: Path,
) -> None:
    containers = {
        f"backend-{number}": _container(tmp_path / "project")
        for number in range(1, 6)
    }

    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d", "frontend"],
        containers=containers,
    )

    assert result.returncode == 0, result.stderr
    assert commands[-1] == _final_compose_command(
        root, "up", "-d", "frontend", "--scale", "backend=5"
    )


def test_targeted_autoscaler_up_preserves_running_backend_capacity(
    tmp_path: Path,
) -> None:
    containers = {
        f"backend-{number}": _container(tmp_path / "project")
        for number in range(1, 6)
    }

    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d", "backend_autoscaler"],
        containers=containers,
    )

    assert result.returncode == 0, result.stderr
    assert commands[-1] == _final_compose_command(
        root, "up", "-d", "backend_autoscaler", "--scale", "backend=5"
    )


def test_implicit_scale_uses_backend_count_from_locked_second_scan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    before = {
        **{
            f"backend-{number}": _container(root)
            for number in range(1, 6)
        },
        "redis-1": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }
    after = {
        **{
            f"backend-{number}": _container(root)
            for number in range(1, 4)
        },
        "backend-4": _container(root, running=False),
        "redis-1": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }
    after_compose = {
        container_id: {**container, "running": True}
        if container["service"] == "backend"
        else container
        for container_id, container in after.items()
    }

    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=before,
        containers_after_lock=after,
        containers_after_compose=after_compose,
    )

    assert result.returncode == 0, result.stderr
    assert _final_compose_command(
        root, "up", "-d", "--scale", "backend=4"
    ) in commands
    assert ["stop", "--time", "300", "backend-4"] in commands
    assert len(
        [command for command in commands if command[:2] == ["ps", "-aq"]]
    ) == 5


def test_post_lock_redis_identity_change_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    before = _locking_containers(root)
    after = {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "redis-2": _container(root, service="redis"),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=before,
        containers_after_lock=after,
    )

    assert result.returncode == 3
    assert "Redis topology changed after acquiring" in result.stderr
    assert all(command[:1] != ["compose"] for command in commands)
    assert any(
        command[4:5] == ["EVAL"] and "DEL" in command[5]
        for command in commands
        if command[:1] == ["exec"]
    )


def test_running_redis_acquires_lease_even_without_running_autoscaler(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "redis-1": _container(root, service="redis"),
    }

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
    )

    assert result.returncode == 0, result.stderr
    assert any(
        command[:1] == ["exec"] and command[4:5] == ["SET"]
        for command in commands
    )
    assert len(
        [command for command in commands if command[:2] == ["ps", "-aq"]]
    ) == 2


def test_reconcile_lease_waits_for_contention_and_renews_during_up(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    result, root, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=_locking_containers(root),
        environment_overrides={
            "FAKE_REDIS_CONTENTION_COUNT": "1",
            "FAKE_COMPOSE_DELAY_SECONDS": "0.06",
            "HR_AGENT_COMPOSE_LOCK_MAX_ATTEMPTS": "3",
            "HR_AGENT_COMPOSE_LOCK_POLL_SECONDS": "0.01",
            "HR_AGENT_COMPOSE_LOCK_HEARTBEAT_SECONDS": "0.01",
        },
    )

    assert result.returncode == 0, result.stderr
    redis_commands = [command for command in commands if command[:1] == ["exec"]]
    set_commands = [command for command in redis_commands if command[4:5] == ["SET"]]
    assert len(set_commands) == 2
    assert set_commands[-1][5] == "hragent:backend-autoscaler:reconcile:06-emotion-main"
    assert set_commands[-1][7:9] == ["NX", "PX"]
    assert any(
        command[4:5] == ["EVAL"] and "PEXPIRE" in command[5]
        for command in redis_commands
    )
    assert "owner" not in json.loads(
        (tmp_path / "redis-state.json").read_text(encoding="utf-8")
    )


def test_reconcile_lease_cleanup_never_deletes_a_replacement_token(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=_locking_containers(root),
        environment_overrides={
            "FAKE_REDIS_REPLACE_TOKEN_BEFORE_RELEASE": "true",
            "HR_AGENT_COMPOSE_LOCK_HEARTBEAT_SECONDS": "0.01",
        },
    )

    assert result.returncode == 0
    state = json.loads((tmp_path / "redis-state.json").read_text(encoding="utf-8"))
    assert state["owner"] == "replacement-token"
    assert "ownership changed" in result.stderr
    assert any(
        command[4:5] == ["EVAL"] and "DEL" in command[5]
        for command in commands
        if command[:1] == ["exec"]
    )


def test_dry_run_with_running_autoscaler_does_not_mutate_lease(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    result, _, commands = _run_wrapper(
        tmp_path,
        ["--dry-run", "up", "-d"],
        containers=_locking_containers(root),
    )

    assert result.returncode == 0, result.stderr
    assert all(command[:1] != ["exec"] for command in commands)
    assert not (tmp_path / "redis-state.json").exists()


def test_compose_failure_releases_lease_and_preserves_exit_code(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = _locking_containers(root)
    containers["backend-3"] = _container(root, running=False)
    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
        environment_overrides={
            "FAKE_COMPOSE_EXIT_CODE": "37",
            "HR_AGENT_COMPOSE_LOCK_HEARTBEAT_SECONDS": "0.01",
        },
    )

    assert result.returncode == 37
    assert "owner" not in json.loads(
        (tmp_path / "redis-state.json").read_text(encoding="utf-8")
    )
    assert any(
        command[4:5] == ["EVAL"] and "DEL" in command[5]
        for command in commands
        if command[:1] == ["exec"]
    )
    assert all(command[:1] != ["stop"] for command in commands)


def test_backend_pool_over_maximum_fails_without_reconciling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = {
        f"backend-{number}": _container(root, running=number <= 5)
        for number in range(1, 10)
    }
    containers["redis-1"] = _container(root, service="redis")
    containers["autoscaler-1"] = _container(root, service="backend_autoscaler")

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
    )

    assert result.returncode == 3
    assert "pool size 9 exceeds the configured maximum 8" in result.stderr
    assert all(command[:1] not in (["compose"], ["stop"]) for command in commands)


def test_successful_compose_fails_closed_if_final_lease_check_loses_ownership(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=_locking_containers(root),
        environment_overrides={
            # First PEXPIRE is the locked preflight. With an immediate Compose
            # completion, the second is the final synchronous ownership check.
            "FAKE_REDIS_LOSE_OWNERSHIP_ON_PEXPIRE": "2",
        },
    )

    assert result.returncode == 3
    assert "lease ownership was lost before success" in result.stderr
    state = json.loads((tmp_path / "redis-state.json").read_text(encoding="utf-8"))
    assert state["owner"] == "replacement-token"
    assert state["pexpire_attempts"] == 2
    assert any(command[:1] == ["compose"] for command in commands)
    assert any(
        command[4:5] == ["EVAL"] and "DEL" in command[5]
        for command in commands
        if command[:1] == ["exec"]
    )


def test_running_autoscaler_without_exact_running_redis_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    containers = {
        "backend-1": _container(root),
        "backend-2": _container(root),
        "autoscaler-1": _container(root, service="backend_autoscaler"),
    }

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
    )

    assert result.returncode == 3
    assert "requires exactly one running" in result.stderr
    assert all(command[:1] != ["compose"] for command in commands)


def test_exhausted_reconcile_lease_contention_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=_locking_containers(root),
        environment_overrides={
            "FAKE_REDIS_CONTENTION_COUNT": "99",
            "HR_AGENT_COMPOSE_LOCK_MAX_ATTEMPTS": "2",
            "HR_AGENT_COMPOSE_LOCK_POLL_SECONDS": "0.01",
        },
    )

    assert result.returncode == 3
    assert "remained contended" in result.stderr
    assert len(
        [
            command
            for command in commands
            if command[:1] == ["exec"] and command[4:5] == ["SET"]
        ]
    ) == 2
    assert all(command[:1] != ["compose"] for command in commands)


@pytest.mark.parametrize(
    "arguments",
    (
        ["-p", "06", "up", "-d"],
        ["-p06", "up", "-d"],
        ["--project-name", "06", "up", "-d"],
        ["--project-name=06", "up", "-d"],
        ["-f", "/tmp/foreign.yml", "up", "-d"],
        ["-f/tmp/foreign.yml", "up", "-d"],
        ["--file=/tmp/foreign.yml", "up", "-d"],
        ["--project-directory", "/tmp/foreign", "up", "-d"],
        ["--project-directory=/tmp/foreign", "up", "-d"],
        ["--env-file", "/tmp/foreign.env", "up", "-d"],
        ["--env-file=/tmp/foreign.env", "up", "-d"],
    ),
)
def test_scope_override_arguments_are_rejected_before_docker(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    result, _, commands = _run_wrapper(tmp_path, arguments)

    assert result.returncode == 2
    assert "overrides are not allowed" in result.stderr
    assert commands == []


def test_foreign_project_environment_is_rejected_before_docker(
    tmp_path: Path,
) -> None:
    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        environment_overrides={"COMPOSE_PROJECT_NAME": "06"},
    )

    assert result.returncode == 2
    assert "COMPOSE_PROJECT_NAME must be 06-emotion-main" in result.stderr
    assert commands == []


@pytest.mark.parametrize("foreign_label", ["working_dir", "config_files"])
def test_full_up_refuses_foreign_same_project_container(
    tmp_path: Path,
    foreign_label: str,
) -> None:
    root = tmp_path / "project"
    overrides = {foreign_label: "/home/another-user/hr_agent/06_emotion"}
    containers = {"foreign-1": _container(root, **overrides)}

    result, _, commands = _run_wrapper(
        tmp_path,
        ["up", "-d"],
        containers=containers,
    )

    assert result.returncode == 3
    assert "mismatched working_dir/config_files labels" in result.stderr
    assert [command[0] for command in commands] == ["ps", "inspect"]
    assert all(command[0] != "compose" for command in commands)

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loadtests.resource_observer import (
    ASR_SERVICES,
    COMPOSE_PROJECT,
    COMPOSE_WORKING_DIR,
    ObservationError,
    ResourceObserver,
    collect_jsonl,
)


def _inspect_item(identifier: str, service: str, *, working_dir: str | None = None) -> dict:
    return {
        "Id": identifier,
        "Name": f"/{COMPOSE_PROJECT}-{service}-1",
        "Image": f"sha256:{identifier}",
        "Config": {
            "Image": f"test/{service}:local",
            "Labels": {
                "com.docker.compose.project": COMPOSE_PROJECT,
                "com.docker.compose.project.working_dir": working_dir
                or str(COMPOSE_WORKING_DIR),
                "com.docker.compose.service": service,
            },
            "Healthcheck": {
                "Test": ["CMD-SHELL", "curl -f http://localhost:8001/health"]
            },
        },
        "State": {
            "Running": True,
            "Status": "running",
            "OOMKilled": False,
            "StartedAt": "2026-08-16T00:00:00Z",
            "Health": {
                "Status": "healthy",
                "FailingStreak": 0,
                "Log": [{"ExitCode": 0, "End": "2026-08-16T00:00:01Z"}],
            },
        },
        "RestartCount": 0,
        "NetworkSettings": {"Networks": {"default": {"IPAddress": "172.28.0.10"}}},
    }


class FakeCommands:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv) -> str:
        command = tuple(argv)
        self.calls.append(command)
        if command[:2] == ("docker", "ps"):
            return "\n".join(item["Id"] for item in self.items) + "\n"
        if command[:2] == ("docker", "inspect"):
            requested = set(command[2:])
            return json.dumps([item for item in self.items if item["Id"] in requested])
        if command[:2] == ("docker", "stats"):
            return "\n".join(
                json.dumps(
                    {
                        "Name": item["Name"].lstrip("/"),
                        "CPUPerc": "12.5%",
                        "MemPerc": "3.0%",
                        "MemUsage": "1GiB / 32GiB",
                        "NetIO": "1MB / 2MB",
                        "BlockIO": "0B / 0B",
                        "PIDs": "10",
                    }
                )
                for item in self.items
            )
        if command[:1] == ("nvidia-smi",):
            return (
                "0, GPU-zero, 91, 8300, 24564, 245.5\n"
                "1, GPU-one, 92, 10100, 24564, 250.0\n"
            )
        raise AssertionError(f"unexpected command: {command!r}")


def _items() -> list[dict]:
    return [
        _inspect_item("asr-primary", "qwen3_asr"),
        _inspect_item("asr-secondary", "qwen3_asr_secondary"),
        _inspect_item("backend-one", "backend"),
    ]


def test_snapshot_is_scoped_to_exact_project_and_both_asr_services() -> None:
    commands = FakeCommands(_items())
    health_calls: list[str] = []

    def health(url: str, timeout: float):
        health_calls.append(url)
        return 200, {"ready": True, "queue_depth": 2, "timeout": timeout}

    observer = ResourceObserver(command_runner=commands, http_get=health)
    result = observer.snapshot(run_id="live-60", sequence=4)

    ps = commands.calls[0]
    assert ps[:3] == ("docker", "ps", "-a")
    assert f"label=com.docker.compose.project={COMPOSE_PROJECT}" in ps
    assert result["sequence"] == 4
    assert [gpu["index"] for gpu in result["gpu"]] == [0, 1]
    assert {item["service"] for item in result["asr_health"]} == set(ASR_SERVICES)
    assert all(item["endpoint"]["payload"]["queue_depth"] == 2 for item in result["asr_health"])
    assert len(health_calls) == 2
    stats_call = next(call for call in commands.calls if call[:2] == ("docker", "stats"))
    assert set(stats_call[5:]) == {item["Id"] for item in _items()}


def test_inventory_rejects_container_from_another_working_directory() -> None:
    items = _items()
    items[0] = _inspect_item("asr-primary", "qwen3_asr", working_dir="/tmp/other")
    commands = FakeCommands(items)
    observer = ResourceObserver(command_runner=commands)

    with pytest.raises(ObservationError, match="isolation label mismatch"):
        observer.inventory()

    assert not any(call[:2] == ("docker", "stats") for call in commands.calls)


def test_collect_jsonl_flushes_start_sample_and_end_manifest(tmp_path: Path) -> None:
    commands = FakeCommands(_items())
    observer = ResourceObserver(
        command_runner=commands,
        http_get=lambda _url, _timeout: (200, {"ready": True}),
    )
    ticks = iter((10.0, 10.0, 10.0))
    output = tmp_path / "resources.jsonl"

    count = collect_jsonl(
        observer=observer,
        output=output,
        run_id="live-60",
        duration_seconds=0,
        interval_seconds=0.5,
        monotonic=lambda: next(ticks),
        sleeper=lambda _seconds: None,
    )

    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert count == 1
    assert [(record["record_type"], record.get("phase")) for record in records] == [
        ("manifest", "start"),
        ("sample", None),
        ("manifest", "end"),
    ]
    assert records[-1]["sample_count"] == 1
    assert records[0]["scope"]["compose_project"] == COMPOSE_PROJECT

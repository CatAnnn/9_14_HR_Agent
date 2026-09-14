"""Read-only resource observations for the isolated load-test Compose project.

The observer deliberately has no Docker lifecycle operations.  It only reads the
exact ``06-emotion-loadtest`` project via ``docker ps``, ``docker inspect`` and
``docker stats``; samples GPUs 0 and 1; and probes the two local ASR health
endpoints.  Every record is flushed as JSONL so a terminated load run still has
usable evidence.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SCHEMA_VERSION = "hr-agent.loadtest-resource-observation.v1"
COMPOSE_PROJECT = "06-emotion-loadtest"
COMPOSE_WORKING_DIR = Path(__file__).resolve().parent
GPU_INDICES = (0, 1)
ASR_SERVICES = ("qwen3_asr", "qwen3_asr_secondary")

_PROJECT_LABEL = "com.docker.compose.project"
_WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
_SERVICE_LABEL = "com.docker.compose.service"
_HEALTHCHECK_URL = re.compile(
    r"http://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):"
    r"(?P<port>[0-9]{1,5})(?P<path>/[^\s'\";)]*)"
)


class ObservationError(RuntimeError):
    """Raised when project isolation or command output cannot be verified."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _run_read_only(argv: Sequence[str], *, timeout: float = 10.0) -> str:
    command = tuple(str(part) for part in argv)
    allowed = (
        command[:2] in {("docker", "ps"), ("docker", "inspect"), ("docker", "stats")}
        or command[:1] == ("nvidia-smi",)
    )
    if not allowed:
        raise ObservationError(f"non-read-only command rejected: {command!r}")
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ObservationError(f"command failed: {command[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise ObservationError(f"command failed: {' '.join(command[:2])}: {detail}")
    return completed.stdout


def _safe_asr_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ObservationError("ASR health URL must be unauthenticated local HTTP")
    hostname = parsed.hostname
    if hostname in {"localhost", *ASR_SERVICES}:
        return url
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ObservationError("ASR health URL hostname must be local or a private IP") from exc
    if not (address.is_loopback or address.is_private or address.is_link_local):
        raise ObservationError("external ASR health URL rejected")
    return url


def _http_get_json(url: str, timeout: float) -> tuple[int, Any]:
    safe_url = _safe_asr_url(url)
    request = Request(safe_url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is constrained above
            body = response.read(1_048_576)
            status = int(response.status)
    except HTTPError as exc:
        body = exc.read(65_536)
        status = int(exc.code)
    except (OSError, TimeoutError, URLError) as exc:
        raise ObservationError(f"ASR health request failed: {type(exc).__name__}: {exc}") from exc
    try:
        payload: Any = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"body": body.decode("utf-8", errors="replace")[:512]}
    return status, payload


def _as_number(value: str) -> float | None:
    cleaned = value.strip().removesuffix("%").strip()
    if not cleaned or cleaned.upper() in {"N/A", "NA", "[NOT SUPPORTED]"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _container_id(item: Mapping[str, Any]) -> str:
    value = str(item.get("Id") or "")
    if not value:
        raise ObservationError("docker inspect returned a container without an ID")
    return value


@dataclass(frozen=True)
class ContainerInventory:
    by_id: dict[str, dict[str, Any]]
    asr_by_service: dict[str, dict[str, Any]]


class ResourceObserver:
    """Collect isolated resource snapshots without changing container state."""

    def __init__(
        self,
        *,
        command_runner: Callable[[Sequence[str]], str] = _run_read_only,
        http_get: Callable[[str, float], tuple[int, Any]] = _http_get_json,
        asr_health_urls: Mapping[str, str] | None = None,
        http_timeout: float = 2.0,
    ) -> None:
        self._command_runner = command_runner
        self._http_get = http_get
        self._http_timeout = http_timeout
        supplied = dict(asr_health_urls or {})
        unknown = set(supplied) - set(ASR_SERVICES)
        if unknown:
            raise ObservationError(f"unknown ASR service health mapping: {sorted(unknown)!r}")
        self._asr_health_urls = {
            service: _safe_asr_url(url) for service, url in supplied.items()
        }

    def inventory(self) -> ContainerInventory:
        ids_output = self._command_runner(
            (
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={_PROJECT_LABEL}={COMPOSE_PROJECT}",
                "--format",
                "{{.ID}}",
            )
        )
        ids = tuple(line.strip() for line in ids_output.splitlines() if line.strip())
        if not ids:
            raise ObservationError(f"no containers found for project {COMPOSE_PROJECT!r}")

        raw = self._command_runner(("docker", "inspect", *ids))
        try:
            inspected = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ObservationError("docker inspect did not return valid JSON") from exc
        if not isinstance(inspected, list) or len(inspected) != len(ids):
            raise ObservationError("docker inspect did not return every selected container")

        by_id: dict[str, dict[str, Any]] = {}
        asr_by_service: dict[str, dict[str, Any]] = {}
        expected_dir = str(COMPOSE_WORKING_DIR)
        for item in inspected:
            if not isinstance(item, dict):
                raise ObservationError("docker inspect contained a non-object entry")
            labels = item.get("Config", {}).get("Labels") or {}
            project = labels.get(_PROJECT_LABEL)
            working_dir = labels.get(_WORKING_DIR_LABEL)
            service = str(labels.get(_SERVICE_LABEL) or "")
            if project != COMPOSE_PROJECT or working_dir != expected_dir:
                raise ObservationError(
                    "container isolation label mismatch for "
                    f"{str(item.get('Name') or '').lstrip('/') or '<unknown>'}"
                )
            identifier = _container_id(item)
            if identifier in by_id:
                raise ObservationError(f"duplicate container ID in inspect output: {identifier}")
            by_id[identifier] = item
            if service in ASR_SERVICES:
                if service in asr_by_service:
                    raise ObservationError(f"multiple containers found for ASR service {service!r}")
                asr_by_service[service] = item

        missing = set(ASR_SERVICES) - set(asr_by_service)
        if missing:
            raise ObservationError(f"missing ASR service containers: {sorted(missing)!r}")
        return ContainerInventory(by_id=by_id, asr_by_service=asr_by_service)

    @staticmethod
    def _inventory_record(item: Mapping[str, Any]) -> dict[str, Any]:
        labels = item.get("Config", {}).get("Labels") or {}
        state = item.get("State") or {}
        health = state.get("Health") or {}
        return {
            "container_id": _container_id(item),
            "name": str(item.get("Name") or "").lstrip("/"),
            "service": labels.get(_SERVICE_LABEL),
            "image": item.get("Config", {}).get("Image"),
            "image_id": item.get("Image"),
            "running": bool(state.get("Running")),
            "status": state.get("Status"),
            "health": health.get("Status"),
            "restart_count": int(item.get("RestartCount") or 0),
            "oom_killed": bool(state.get("OOMKilled")),
            "started_at": state.get("StartedAt"),
        }

    def manifest(self, *, run_id: str, phase: str, inventory: ContainerInventory) -> dict[str, Any]:
        containers = [
            self._inventory_record(item)
            for _, item in sorted(inventory.by_id.items(), key=lambda pair: pair[0])
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "record_type": "manifest",
            "phase": phase,
            "run_id": run_id,
            "observed_at": _utc_now(),
            "host": socket.gethostname(),
            "scope": {
                "compose_project": COMPOSE_PROJECT,
                "compose_working_dir": str(COMPOSE_WORKING_DIR),
                "gpu_indices": list(GPU_INDICES),
                "asr_services": list(ASR_SERVICES),
                "read_only": True,
            },
            "containers": containers,
        }

    def _gpu_snapshot(self) -> list[dict[str, Any]]:
        raw = self._command_runner(
            (
                "nvidia-smi",
                "--id=0,1",
                "--query-gpu=index,uuid,utilization.gpu,memory.used,memory.total,power.draw",
                "--format=csv,noheader,nounits",
            )
        )
        gpus: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 6:
                raise ObservationError(f"unexpected nvidia-smi row: {line!r}")
            try:
                index = int(fields[0])
            except ValueError as exc:
                raise ObservationError(f"invalid GPU index: {fields[0]!r}") from exc
            if index not in GPU_INDICES:
                raise ObservationError(f"nvidia-smi returned out-of-scope GPU {index}")
            gpus.append(
                {
                    "index": index,
                    "uuid": fields[1],
                    "utilization_percent": _as_number(fields[2]),
                    "memory_used_mib": _as_number(fields[3]),
                    "memory_total_mib": _as_number(fields[4]),
                    "power_watts": _as_number(fields[5]),
                }
            )
        if {gpu["index"] for gpu in gpus} != set(GPU_INDICES):
            raise ObservationError("nvidia-smi did not return both requested GPUs")
        return sorted(gpus, key=lambda gpu: gpu["index"])

    def _container_snapshot(self, inventory: ContainerInventory) -> list[dict[str, Any]]:
        records = {
            identifier: self._inventory_record(item)
            for identifier, item in inventory.by_id.items()
        }
        running_ids = tuple(
            identifier
            for identifier, item in inventory.by_id.items()
            if bool((item.get("State") or {}).get("Running"))
        )
        stats_by_name: dict[str, dict[str, Any]] = {}
        if running_ids:
            raw = self._command_runner(
                (
                    "docker",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{json .}}",
                    *running_ids,
                )
            )
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    stat = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ObservationError("docker stats emitted invalid JSON") from exc
                name = str(stat.get("Name") or "")
                stats_by_name[name] = {
                    "cpu_percent": _as_number(str(stat.get("CPUPerc") or "")),
                    "memory_percent": _as_number(str(stat.get("MemPerc") or "")),
                    "memory_usage": stat.get("MemUsage"),
                    "network_io": stat.get("NetIO"),
                    "block_io": stat.get("BlockIO"),
                    "pids": int(stat["PIDs"]) if str(stat.get("PIDs") or "").isdigit() else None,
                }
        for record in records.values():
            record["stats"] = stats_by_name.get(str(record["name"]))
        return sorted(records.values(), key=lambda record: (str(record["service"]), record["name"]))

    @staticmethod
    def _derived_health_url(item: Mapping[str, Any]) -> str | None:
        test = ((item.get("Config") or {}).get("Healthcheck") or {}).get("Test") or []
        command = " ".join(str(part) for part in test)
        match = _HEALTHCHECK_URL.search(command)
        if not match:
            return None
        port = int(match.group("port"))
        if not (1 <= port <= 65_535):
            return None
        networks = ((item.get("NetworkSettings") or {}).get("Networks") or {}).values()
        addresses = sorted(
            str(network.get("IPAddress") or "")
            for network in networks
            if str(network.get("IPAddress") or "")
        )
        if not addresses:
            return None
        host = addresses[0]
        if ":" in host:
            host = f"[{host}]"
        return _safe_asr_url(f"http://{host}:{port}{match.group('path')}")

    def _asr_snapshot(self, inventory: ContainerInventory) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        for service in ASR_SERVICES:
            item = inventory.asr_by_service[service]
            state = item.get("State") or {}
            health = state.get("Health") or {}
            logs = health.get("Log") or []
            latest = logs[-1] if logs else {}
            observation: dict[str, Any] = {
                "service": service,
                "container_id": _container_id(item),
                "running": bool(state.get("Running")),
                "docker_health": health.get("Status"),
                "failing_streak": int(health.get("FailingStreak") or 0),
                "last_probe_exit_code": latest.get("ExitCode"),
                "last_probe_finished_at": latest.get("End"),
            }
            url = self._asr_health_urls.get(service) or self._derived_health_url(item)
            if url is None:
                observation["endpoint"] = {"status": "not_discoverable"}
            else:
                try:
                    status, payload = self._http_get(url, self._http_timeout)
                    observation["endpoint"] = {
                        "status": "ok" if 200 <= status < 300 else "http_error",
                        "http_status": status,
                        "payload": payload,
                    }
                except ObservationError as exc:
                    observation["endpoint"] = {
                        "status": "request_error",
                        "error": str(exc),
                    }
            observations.append(observation)
        return observations

    def snapshot(self, *, run_id: str, sequence: int) -> dict[str, Any]:
        observed_at = _utc_now()
        inventory = self.inventory()
        errors: list[dict[str, str]] = []
        try:
            gpus = self._gpu_snapshot()
        except ObservationError as exc:
            gpus = []
            errors.append({"component": "gpu", "error": str(exc)})
        try:
            containers = self._container_snapshot(inventory)
        except ObservationError as exc:
            containers = [
                self._inventory_record(item) for item in inventory.by_id.values()
            ]
            errors.append({"component": "container_stats", "error": str(exc)})
        asr = self._asr_snapshot(inventory)
        return {
            "schema_version": SCHEMA_VERSION,
            "record_type": "sample",
            "run_id": run_id,
            "sequence": sequence,
            "observed_at": observed_at,
            "gpu": gpus,
            "containers": containers,
            "asr_health": asr,
            "errors": errors,
        }


def collect_jsonl(
    *,
    observer: ResourceObserver,
    output: Path,
    run_id: str,
    duration_seconds: float,
    interval_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    if duration_seconds < 0:
        raise ObservationError("duration must be non-negative")
    if interval_seconds <= 0:
        raise ObservationError("interval must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    sequence = 0
    inventory = observer.inventory()
    with output.open("x", encoding="utf-8") as stream:
        def emit(record: Mapping[str, Any]) -> None:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()

        emit(observer.manifest(run_id=run_id, phase="start", inventory=inventory))
        try:
            while True:
                emit(observer.snapshot(run_id=run_id, sequence=sequence))
                sequence += 1
                elapsed = monotonic() - started
                if elapsed >= duration_seconds:
                    break
                sleeper(min(interval_seconds, max(0.0, duration_seconds - elapsed)))
        finally:
            final_inventory = observer.inventory()
            final = observer.manifest(run_id=run_id, phase="end", inventory=final_inventory)
            final["sample_count"] = sequence
            final["elapsed_seconds"] = round(max(0.0, monotonic() - started), 3)
            emit(final)
    return sequence


def _parse_health_mapping(value: str) -> tuple[str, str]:
    service, separator, url = value.partition("=")
    if not separator or service not in ASR_SERVICES or not url:
        expected = " or ".join(f"{service}=http://..." for service in ASR_SERVICES)
        raise argparse.ArgumentTypeError(f"expected {expected}")
    try:
        return service, _safe_asr_url(url)
    except ObservationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--interval-seconds", type=float, default=0.5)
    parser.add_argument(
        "--asr-health-url",
        action="append",
        default=[],
        type=_parse_health_mapping,
        metavar="SERVICE=LOCAL_HTTP_URL",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mappings = dict(args.asr_health_url)
    if len(mappings) != len(args.asr_health_url):
        raise SystemExit("each --asr-health-url service may be supplied only once")
    observer = ResourceObserver(asr_health_urls=mappings)
    try:
        collect_jsonl(
            observer=observer,
            output=args.output,
            run_id=args.run_id,
            duration_seconds=args.duration_seconds,
            interval_seconds=args.interval_seconds,
        )
    except (ObservationError, FileExistsError) as exc:
        raise SystemExit(f"resource observation failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

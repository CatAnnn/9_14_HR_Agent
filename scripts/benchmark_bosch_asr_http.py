#!/usr/bin/env python3
"""Run a guarded staircase benchmark against the configured Bosch HTTP ASR.

This benchmark intentionally calls the configured upstream endpoint directly.
It therefore measures upstream HTTP capacity without the application's Redis
admission limit or its automatic retry behavior.  A live run requires the
explicit ``--allow-live`` switch.

Authentication is built by :class:`ModelAPIAuth`.  Request headers, tokens,
keys, response bodies, and URL query parameters are never included in output.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import statistics
import sys
import threading
import time
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit
import wave

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import Settings  # noqa: E402
from backend.services.model_api_auth import ModelAPIAuth  # noqa: E402


DEFAULT_LEVELS = (16, 24, 32, 40, 48, 56, 64)


class BenchmarkConfigurationError(RuntimeError):
    """A safe, credential-free configuration error."""


@dataclass(frozen=True, slots=True)
class RequestObservation:
    request_index: int
    status: int | None
    latency_ms: float
    response_bytes: int
    error: str | None
    started_at: float
    completed_at: float

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("started_at", None)
        payload.pop("completed_at", None)
        return payload


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive finite number")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("value must be a non-negative finite number")
    return parsed


def _parse_levels(value: str) -> tuple[int, ...]:
    try:
        levels = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("levels must be comma-separated integers") from exc
    if not levels or any(level <= 0 for level in levels):
        raise argparse.ArgumentTypeError("levels must contain positive integers")
    if tuple(sorted(set(levels))) != levels:
        raise argparse.ArgumentTypeError("levels must be unique and strictly increasing")
    return levels


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _latency_statistics(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "median": None, "p95": None, "max": None}
    normalized = [float(value) for value in values]
    return {
        "min": round(min(normalized), 3),
        "median": round(statistics.median(normalized), 3),
        "p95": round(float(_percentile(normalized, 0.95)), 3),
        "max": round(max(normalized), 3),
    }


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))


def _pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    if not pcm:
        raise BenchmarkConfigurationError("PCM input is empty")
    if len(pcm) % 2:
        raise BenchmarkConfigurationError("PCM16 input must have an even byte length")
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


def _safe_exception_code(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "transport_error"
    if isinstance(exc, threading.BrokenBarrierError):
        return "start_barrier_broken"
    return f"exception_{type(exc).__name__}"


def _run_request(
    *,
    client: httpx.Client,
    barrier: threading.Barrier,
    barrier_timeout_seconds: float,
    request_index: int,
    level: int,
    round_index: int,
    url: str,
    headers: dict[str, str],
    model: str,
    language: str,
    prompt: str,
    wav_audio: bytes,
    timeout_seconds: float,
) -> RequestObservation:
    status: int | None = None
    response_bytes = 0
    error: str | None = None
    started_at = time.perf_counter()
    try:
        barrier.wait(timeout=barrier_timeout_seconds)
        started_at = time.perf_counter()
        data = {"model": model, "language": language}
        if prompt:
            data["prompt"] = prompt
        response = client.post(
            url,
            headers=headers,
            data=data,
            files={
                "file": (
                    f"bosch-load-l{level}-r{round_index}-q{request_index + 1}.wav",
                    wav_audio,
                    "audio/wav",
                )
            },
            timeout=httpx.Timeout(timeout_seconds),
        )
        status = response.status_code
        response_bytes = len(response.content)
        if not 200 <= status < 300:
            error = "http_status"
    except Exception as exc:  # every concurrent failure must become an observation
        error = _safe_exception_code(exc)
    completed_at = time.perf_counter()
    return RequestObservation(
        request_index=request_index,
        status=status,
        latency_ms=round((completed_at - started_at) * 1000.0, 3),
        response_bytes=response_bytes,
        error=error,
        started_at=started_at,
        completed_at=completed_at,
    )


def _summarize_round(
    observations: Sequence[RequestObservation],
    *,
    audio_seconds: float,
    maximum_p95_ms: float,
) -> dict[str, Any]:
    successful = [observation for observation in observations if observation.succeeded]
    wall_seconds = max(
        0.0,
        max(observation.completed_at for observation in observations)
        - min(observation.started_at for observation in observations),
    )
    status_counts: dict[str, int] = {}
    exception_counts: dict[str, int] = {}
    for observation in observations:
        status_key = "none" if observation.status is None else str(observation.status)
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        if observation.error:
            exception_counts[observation.error] = exception_counts.get(observation.error, 0) + 1

    latency_ms = _latency_statistics(
        [observation.latency_ms for observation in successful]
    )
    stop_reasons: list[str] = []
    if len(successful) != len(observations):
        stop_reasons.append("one_or_more_requests_failed")
    p95_ms = latency_ms["p95"]
    if p95_ms is not None and p95_ms > maximum_p95_ms:
        stop_reasons.append("p95_latency_exceeded")
    return {
        "requests": len(observations),
        "successful": len(successful),
        "failed": len(observations) - len(successful),
        "status_counts": status_counts,
        "exception_counts": exception_counts,
        "wall_seconds": round(wall_seconds, 6),
        "latency_ms": latency_ms,
        "throughput": {
            "attempted_requests_per_second": (
                round(len(observations) / wall_seconds, 6) if wall_seconds > 0 else None
            ),
            "successful_requests_per_second": (
                round(len(successful) / wall_seconds, 6) if wall_seconds > 0 else None
            ),
            "successful_audio_seconds_per_wall_second": (
                round(len(successful) * audio_seconds / wall_seconds, 6)
                if wall_seconds > 0
                else None
            ),
        },
        "passed": not stop_reasons,
        "stop_reasons": stop_reasons,
    }


def _summarize_level(rounds: Sequence[dict[str, Any]]) -> dict[str, Any]:
    observations = [
        observation
        for round_payload in rounds
        for observation in round_payload["observations"]
    ]
    successful = [observation for observation in observations if observation["error"] is None]
    active_wall_seconds = sum(
        float(round_payload["summary"]["wall_seconds"] or 0.0)
        for round_payload in rounds
    )
    return {
        "rounds_completed": len(rounds),
        "requests": len(observations),
        "successful": len(successful),
        "failed": len(observations) - len(successful),
        "latency_ms": _latency_statistics(
            [float(observation["latency_ms"]) for observation in successful]
        ),
        "active_wall_seconds": round(active_wall_seconds, 6),
        "successful_requests_per_second": (
            round(len(successful) / active_wall_seconds, 6)
            if active_wall_seconds > 0
            else None
        ),
        "passed": all(round_payload["summary"]["passed"] for round_payload in rounds),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a guarded staircase load test against configured Bosch HTTP ASR."
    )
    parser.add_argument("--pcm-file", type=Path, required=True)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / "backend/config/.env",
    )
    parser.add_argument(
        "--levels",
        type=_parse_levels,
        default=DEFAULT_LEVELS,
        help="strictly increasing comma-separated concurrency levels",
    )
    parser.add_argument("--rounds", type=_positive_int, default=1)
    parser.add_argument("--sample-rate", type=_positive_int)
    parser.add_argument("--timeout-seconds", type=_positive_float)
    parser.add_argument("--barrier-timeout-seconds", type=_positive_float, default=30.0)
    parser.add_argument("--max-p95-ms", type=_positive_float, default=3000.0)
    parser.add_argument("--pause-seconds", type=_nonnegative_float, default=0.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="required acknowledgement that this sends concurrent upstream requests",
    )
    return parser


def _load_settings(env_file: Path) -> Settings:
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except Exception as exc:
        raise BenchmarkConfigurationError(
            f"settings could not be loaded ({type(exc).__name__})"
        ) from None


def _build_auth_headers(settings: Settings) -> dict[str, str]:
    try:
        return ModelAPIAuth(settings).sync_headers(settings.effective_asr_api_key)
    except Exception as exc:
        raise BenchmarkConfigurationError(
            f"ASR authentication could not be initialized ({type(exc).__name__})"
        ) from None


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.allow_live:
        raise BenchmarkConfigurationError("live execution requires --allow-live")
    settings = _load_settings(args.env_file)
    if not settings.asr_enabled:
        raise BenchmarkConfigurationError("ASR is disabled in Settings")
    if settings.asr_provider_mode != "bosch":
        raise BenchmarkConfigurationError("ASR_PROVIDER_MODE must be bosch")
    endpoint = settings.asr_http_url.strip()
    parsed_endpoint = urlsplit(endpoint)
    if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
        raise BenchmarkConfigurationError("configured ASR HTTP endpoint is invalid")

    sample_rate = int(args.sample_rate or settings.asr_sample_rate)
    timeout_seconds = float(args.timeout_seconds or settings.asr_timeout_seconds)
    pcm = args.pcm_file.read_bytes()
    wav_audio = _pcm16_to_wav(pcm, sample_rate)
    audio_seconds = len(pcm) / float(sample_rate * 2)
    headers = _build_auth_headers(settings)
    levels: tuple[int, ...] = tuple(args.levels)
    generated_at = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "format_version": 1,
        "kind": "bosch-http-asr-staircase",
        "generated_at": generated_at,
        "configuration": {
            "endpoint": _redact_url(endpoint),
            "model": settings.asr_http_model,
            "language": settings.asr_language,
            "prompt_configured": bool(settings.asr_bosch_prompt.strip()),
            "levels": list(levels),
            "rounds_per_level": args.rounds,
            "timeout_seconds": timeout_seconds,
            "barrier_timeout_seconds": args.barrier_timeout_seconds,
            "maximum_p95_ms": args.max_p95_ms,
            "pause_seconds": args.pause_seconds,
            "audio": {
                "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                "pcm_bytes": len(pcm),
                "wav_bytes": len(wav_audio),
                "sample_rate": sample_rate,
                "channels": 1,
                "sample_width_bytes": 2,
                "duration_seconds": round(audio_seconds, 6),
            },
        },
        "levels": [],
        "passed": True,
        "stopped_early": False,
        "stop": None,
    }

    client_timeout = httpx.Timeout(timeout_seconds)
    limits = httpx.Limits(
        max_connections=max(levels),
        max_keepalive_connections=max(levels),
    )
    with httpx.Client(timeout=client_timeout, limits=limits) as client:
        for level_index, level in enumerate(levels):
            level_rounds: list[dict[str, Any]] = []
            level_failed = False
            for round_index in range(1, args.rounds + 1):
                barrier = threading.Barrier(level)
                with ThreadPoolExecutor(max_workers=level) as executor:
                    futures = [
                        executor.submit(
                            _run_request,
                            client=client,
                            barrier=barrier,
                            barrier_timeout_seconds=args.barrier_timeout_seconds,
                            request_index=request_index,
                            level=level,
                            round_index=round_index,
                            url=endpoint,
                            headers=headers,
                            model=settings.asr_http_model,
                            language=settings.asr_language,
                            prompt=settings.asr_bosch_prompt.strip(),
                            wav_audio=wav_audio,
                            timeout_seconds=timeout_seconds,
                        )
                        for request_index in range(level)
                    ]
                    observations = [future.result() for future in futures]
                observations.sort(key=lambda observation: observation.request_index)
                summary = _summarize_round(
                    observations,
                    audio_seconds=audio_seconds,
                    maximum_p95_ms=args.max_p95_ms,
                )
                level_rounds.append(
                    {
                        "round": round_index,
                        "summary": summary,
                        "observations": [observation.public() for observation in observations],
                    }
                )
                if not summary["passed"]:
                    level_failed = True
                    break

            level_payload = {
                "concurrency": level,
                "summary": _summarize_level(level_rounds),
                "rounds": level_rounds,
            }
            report["levels"].append(level_payload)
            if level_failed:
                failed_round = level_rounds[-1]
                report["passed"] = False
                report["stopped_early"] = True
                report["stop"] = {
                    "concurrency": level,
                    "round": failed_round["round"],
                    "reasons": failed_round["summary"]["stop_reasons"],
                }
                break
            if level_index < len(levels) - 1 and args.pause_seconds > 0:
                time.sleep(args.pause_seconds)

    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except (BenchmarkConfigurationError, OSError) as exc:
        parser.error(str(exc))
    if args.output is not None:
        _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "passed": report["passed"],
                    "stopped_early": report["stopped_early"],
                    "stop": report["stop"],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

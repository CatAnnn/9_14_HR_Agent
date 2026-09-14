#!/usr/bin/env python3
"""Benchmark simultaneous streaming Fish TTS requests without backend admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PlaybackSimulation:
    packet_count: int
    intervals: tuple[tuple[float, float], ...]
    gap_count: int
    total_gap_seconds: float
    max_gap_seconds: float

    @property
    def started_at(self) -> float | None:
        return self.intervals[0][0] if self.intervals else None

    @property
    def ended_at(self) -> float | None:
        return self.intervals[-1][1] if self.intervals else None


def _simulate_playback(
    packets: list[tuple[float, int]],
    *,
    sample_rate: int,
    playback_buffer_seconds: float = 1.4,
) -> PlaybackSimulation:
    """Mirror frontend scheduling using HTTP arrival times, without browser overhead."""
    if (
        not isinstance(sample_rate, int)
        or isinstance(sample_rate, bool)
        or sample_rate <= 0
        or not math.isfinite(playback_buffer_seconds)
        or playback_buffer_seconds < 0
    ):
        raise ValueError("Playback simulation requires a positive sample rate and non-negative buffer.")
    intervals: list[tuple[float, float]] = []
    gap_count = 0
    total_gap = 0.0
    max_gap = 0.0
    byte_count = 0
    packet_count = 0
    previous_arrival: float | None = None
    for arrived_at, packet_bytes in packets:
        if (
            not math.isfinite(arrived_at)
            or (previous_arrival is not None and arrived_at < previous_arrival)
            or not isinstance(packet_bytes, int)
            or isinstance(packet_bytes, bool)
            or packet_bytes < 0
        ):
            raise ValueError("PCM packets require ordered finite arrival times and non-negative sizes.")
        previous_arrival = arrived_at
        if packet_bytes == 0:
            continue
        packet_count += 1
        # HTTP packets may split a 16-bit PCM sample between two reads.
        previous_samples = byte_count // 2
        byte_count += packet_bytes
        duration = (byte_count // 2 - previous_samples) / float(sample_rate)
        if duration <= 0:
            continue
        if intervals and intervals[-1][1] > arrived_at:
            start, end = intervals[-1]
            intervals[-1] = (start, end + duration)
            continue
        start = arrived_at + playback_buffer_seconds
        if intervals:
            gap = start - intervals[-1][1]
            if gap > 0:
                gap_count += 1
                total_gap += gap
                max_gap = max(max_gap, gap)
            else:
                intervals[-1] = (intervals[-1][0], start + duration)
                continue
        intervals.append((start, start + duration))
    return PlaybackSimulation(packet_count, tuple(intervals), gap_count, total_gap, max_gap)


@dataclass(frozen=True)
class Sample:
    request_index: int
    seed: int
    status: int | None
    content_type: str
    first_audio_seconds: float | None
    completion_seconds: float
    audio_bytes: int
    audio_seconds: float
    realtime_factor: float | None
    sha256: str
    error: str | None
    started_at: float
    completed_at: float
    playback: PlaybackSimulation | None = None
    response_headers_seconds: float | None = None

    @property
    def headers_to_first_audio_seconds(self) -> float | None:
        if self.response_headers_seconds is None or self.first_audio_seconds is None:
            return None
        return self.first_audio_seconds - self.response_headers_seconds

    def public(self, max_playback_gap_seconds: float = 0.1) -> dict[str, Any]:
        playback = self.playback
        return {
            "request_index": self.request_index,
            "seed": self.seed,
            "status": self.status,
            "content_type": self.content_type,
            "first_audio_seconds": self.first_audio_seconds,
            "response_headers_seconds": self.response_headers_seconds,
            "headers_to_first_audio_seconds": self.headers_to_first_audio_seconds,
            "completion_seconds": self.completion_seconds,
            "audio_bytes": self.audio_bytes,
            "audio_seconds": self.audio_seconds,
            "realtime_factor": self.realtime_factor,
            "sha256": self.sha256,
            "error": self.error,
            "audio_packet_count": playback.packet_count if playback else 0,
            "simulated_playback_start_seconds": (
                playback.started_at - self.started_at
                if playback is not None and playback.started_at is not None else None
            ),
            "simulated_playback_end_seconds": (
                playback.ended_at - self.started_at
                if playback is not None and playback.ended_at is not None else None
            ),
            "simulated_playback_gap_count": playback.gap_count if playback else 0,
            "simulated_playback_total_gap_seconds": playback.total_gap_seconds if playback else 0.0,
            "simulated_playback_max_gap_seconds": playback.max_gap_seconds if playback else 0.0,
            "playback_continuity_passed": _playback_passed(self, max_playback_gap_seconds),
        }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _sample_succeeded(sample: Sample) -> bool:
    return (
        sample.error is None
        and sample.status is not None
        and 200 <= sample.status < 300
        and sample.content_type.partition(";")[0].strip().lower()
        in {"audio/pcm", "application/octet-stream"}
        and sample.audio_bytes > 0
        and sample.audio_bytes % 2 == 0
    )


def _playback_passed(sample: Sample, max_gap_seconds: float) -> bool:
    return (
        _sample_succeeded(sample)
        and sample.playback is not None
        and bool(sample.playback.intervals)
        and sample.playback.max_gap_seconds <= max_gap_seconds + 1e-9
    )


def _common_playback_intervals(samples: list[Sample]) -> list[tuple[float, float]]:
    if not samples or not all(
        _sample_succeeded(sample) and sample.playback is not None for sample in samples
    ):
        return []
    common = list(samples[0].playback.intervals)
    for sample in samples[1:]:
        intersection: list[tuple[float, float]] = []
        other = sample.playback.intervals
        left = right = 0
        while left < len(common) and right < len(other):
            start = max(common[left][0], other[right][0])
            end = min(common[left][1], other[right][1])
            if end > start:
                intersection.append((start, end))
            if common[left][1] <= other[right][1]:
                left += 1
            else:
                right += 1
        common = intersection
    return common


def _run_request(
    *,
    request_index: int,
    barrier: threading.Barrier,
    url: str,
    model: str,
    voice: str,
    text: str,
    seed: int,
    timeout_seconds: float,
    sample_rate: int,
    max_new_tokens: int,
    stagger_ms: float,
    playback_buffer_seconds: float = 1.4,
) -> Sample:
    payload = json.dumps(
        {
            "model": model,
            "input": f"{text}（并发请求{request_index + 1}）",
            "voice": voice,
            "stream": True,
            "stream_format": "audio",
            "response_format": "pcm",
            "max_new_tokens": max_new_tokens,
            "seed": seed,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "audio/pcm"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    barrier.wait()
    if stagger_ms > 0:
        time.sleep((request_index * stagger_ms) / 1000.0)

    started_at = time.perf_counter()
    response_headers_at: float | None = None
    first_audio_at: float | None = None
    status: int | None = None
    content_type = ""
    audio_bytes = 0
    packets: list[tuple[float, int]] = []
    digest = hashlib.sha256()
    error: str | None = None
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            response_headers_at = time.perf_counter()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            if not 200 <= status < 300:
                raise ValueError(f"unexpected HTTP status {status}")
            if content_type.partition(";")[0].strip().lower() not in {
                "audio/pcm", "application/octet-stream"
            }:
                raise ValueError(f"expected PCM content type, received {content_type!r}")
            read_chunk = getattr(response, "read1", response.read)
            while True:
                chunk = read_chunk(64 * 1024)
                if not chunk:
                    break
                arrived_at = time.perf_counter()
                packets.append((arrived_at, len(chunk)))
                if first_audio_at is None:
                    first_audio_at = arrived_at
                audio_bytes += len(chunk)
                digest.update(chunk)
    except urllib.error.HTTPError as exc:
        # urllib raises for error responses after receiving their headers.
        if response_headers_at is None:
            response_headers_at = time.perf_counter()
        status = exc.code
        try:
            body = exc.read(512).decode("utf-8", errors="replace")
        except Exception as body_error:
            body = f"error body unavailable ({type(body_error).__name__}: {body_error})"
        error = f"HTTP {exc.code}: {body}"
    except Exception as exc:  # benchmark must report every concurrent failure
        error = f"{type(exc).__name__}: {exc}"

    completed_at = time.perf_counter()
    completion_seconds = completed_at - started_at
    audio_seconds = audio_bytes / float(sample_rate * 2)
    if error is None and audio_bytes <= 0:
        error = "response contained no PCM audio"
    elif error is None and audio_bytes % 2:
        error = "PCM response has an odd byte length"
    return Sample(
        request_index=request_index,
        seed=seed,
        status=status,
        content_type=content_type,
        first_audio_seconds=(
            None if first_audio_at is None else first_audio_at - started_at
        ),
        response_headers_seconds=(
            None if response_headers_at is None else response_headers_at - started_at
        ),
        completion_seconds=completion_seconds,
        audio_bytes=audio_bytes,
        audio_seconds=audio_seconds,
        realtime_factor=(
            None if audio_seconds <= 0 else completion_seconds / audio_seconds
        ),
        sha256=digest.hexdigest() if audio_bytes else "",
        error=error,
        started_at=started_at,
        completed_at=completed_at,
        playback=_simulate_playback(
            packets, sample_rate=sample_rate, playback_buffer_seconds=playback_buffer_seconds
        ),
    )


def _summarize(
    samples: list[Sample], max_playback_gap_seconds: float = 0.1,
    min_common_playback_seconds: float = 0.0,
) -> dict[str, Any]:
    successful = [sample for sample in samples if _sample_succeeded(sample)]
    first_audio = [
        sample.first_audio_seconds
        for sample in successful
        if sample.first_audio_seconds is not None
    ]
    response_headers = [
        sample.response_headers_seconds for sample in successful
        if sample.response_headers_seconds is not None
    ]
    headers_to_first_audio = [
        sample.headers_to_first_audio_seconds for sample in successful
        if sample.headers_to_first_audio_seconds is not None
    ]
    completions = [sample.completion_seconds for sample in successful]
    realtime_factors = [
        sample.realtime_factor
        for sample in successful
        if sample.realtime_factor is not None
    ]
    wall_seconds = (
        max(sample.completed_at for sample in samples)
        - min(sample.started_at for sample in samples)
    ) if samples else 0.0
    aggregate_audio_seconds = sum(sample.audio_seconds for sample in successful)
    continuity_passed = sum(_playback_passed(sample, max_playback_gap_seconds) for sample in samples)
    complete_windows = [
        sample.playback for sample in successful
        if sample.playback is not None and sample.playback.intervals
    ]
    nominal_overlap = (
        max(0.0, min(playback.ended_at for playback in complete_windows)
            - max(playback.started_at for playback in complete_windows))
        if len(complete_windows) == len(samples) and samples else 0.0
    )
    common_intervals = _common_playback_intervals(samples)
    common_seconds = sum(end - start for start, end in common_intervals)
    longest_common_seconds = max(
        (end - start for start, end in common_intervals), default=0.0
    )
    return {
        "requests": len(samples),
        "successful": len(successful),
        "failed": len(samples) - len(successful),
        "wall_seconds": wall_seconds,
        "aggregate_audio_seconds": aggregate_audio_seconds,
        "aggregate_audio_per_wall_second": (
            aggregate_audio_seconds / wall_seconds if wall_seconds > 0 else None
        ),
        "first_audio_p50_seconds": _percentile(first_audio, 0.50),
        "first_audio_p95_seconds": _percentile(first_audio, 0.95),
        "response_headers_p50_seconds": _percentile(response_headers, 0.50),
        "response_headers_p95_seconds": _percentile(response_headers, 0.95),
        "headers_to_first_audio_p50_seconds": _percentile(headers_to_first_audio, 0.50),
        "headers_to_first_audio_p95_seconds": _percentile(headers_to_first_audio, 0.95),
        "completion_p50_seconds": _percentile(completions, 0.50),
        "completion_p95_seconds": _percentile(completions, 0.95),
        "realtime_factor_p50": _percentile(realtime_factors, 0.50),
        "realtime_factor_p95": _percentile(realtime_factors, 0.95),
        "playback_continuity_passed": continuity_passed,
        "playback_continuity_failed": len(samples) - continuity_passed,
        "simulated_playback_max_gap_seconds": max(
            (sample.playback.max_gap_seconds for sample in samples if sample.playback),
            default=0.0,
        ),
        "nominal_playback_overlap_seconds": nominal_overlap,
        "simulated_all_streams_playing_seconds": common_seconds,
        "simulated_max_all_streams_playing_seconds": longest_common_seconds,
        "continuous_playback_passed": (
            continuity_passed == len(samples)
            and common_seconds > 0
            and longest_common_seconds >= min_common_playback_seconds
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url", default="http://127.0.0.1:7119/v1/audio/speech"
    )
    parser.add_argument("--model", default="/app/checkpoints/s2-pro")
    parser.add_argument("--voice", default="employee-natural")
    text_source = parser.add_mutually_exclusive_group()
    text_source.add_argument(
        "--text",
        default=(
            "我理解你对这次反馈有顾虑。我们先把已经完成的工作和当前标准逐项核对，"
            "再一起确定接下来的改进重点、支持方式和检查时间。"
        ),
    )
    text_source.add_argument("--text-file", type=Path, help="Read synthesis text from a UTF-8 file.")
    parser.add_argument("--output", type=Path, help="Also save the JSON report to this path.")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--seed-base", type=int, default=41_000)
    parser.add_argument("--stagger-ms", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--sample-rate", type=int, default=44_100)
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--playback-buffer-seconds", type=float, default=1.4)
    parser.add_argument("--max-playback-gap-seconds", type=float, default=0.1)
    parser.add_argument(
        "--min-common-playback-seconds", type=float, default=0.0,
        help="Minimum uninterrupted simulated overlap required of all users.",
    )
    parser.add_argument(
        "--require-continuous-playback",
        action="store_true",
        help="Fail unless all streams meet the gap limit and have common simulated playback time.",
    )
    args = parser.parse_args()
    if args.concurrency < 1 or args.rounds < 1:
        parser.error("concurrency and rounds must be positive")
    if not math.isfinite(args.stagger_ms) or args.stagger_ms < 0:
        parser.error("stagger-ms must be non-negative")
    if args.sample_rate <= 0:
        parser.error("sample-rate must be positive")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be finite and positive")
    if args.max_new_tokens <= 0:
        parser.error("max-new-tokens must be positive")
    for name in (
        "playback_buffer_seconds", "max_playback_gap_seconds", "min_common_playback_seconds"
    ):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0:
            parser.error(f"{name.replace('_', '-')} must be finite and non-negative")
    synthesis_text = args.text
    if args.text_file is not None:
        try:
            synthesis_text = args.text_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            parser.error(f"cannot read text-file: {exc}")
    if not synthesis_text.strip():
        parser.error("synthesis text must not be empty")

    rounds: list[dict[str, Any]] = []
    any_failure = False
    started_at_utc = datetime.now(timezone.utc).isoformat()
    for round_index in range(args.rounds):
        barrier = threading.Barrier(args.concurrency)
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [
                executor.submit(
                    _run_request,
                    request_index=request_index,
                    barrier=barrier,
                    url=args.url,
                    model=args.model,
                    voice=args.voice,
                    text=synthesis_text,
                    seed=args.seed_base + request_index,
                    timeout_seconds=args.timeout_seconds,
                    sample_rate=args.sample_rate,
                    max_new_tokens=args.max_new_tokens,
                    stagger_ms=args.stagger_ms,
                    playback_buffer_seconds=args.playback_buffer_seconds,
                )
                for request_index in range(args.concurrency)
            ]
            samples = [future.result() for future in futures]
        samples.sort(key=lambda sample: sample.request_index)
        summary = _summarize(
            samples, args.max_playback_gap_seconds, args.min_common_playback_seconds
        )
        any_failure = any_failure or summary["failed"] > 0
        if args.require_continuous_playback:
            any_failure = any_failure or not summary["continuous_playback_passed"]
        rounds.append(
            {
                "round": round_index + 1,
                "summary": summary,
                "samples": [sample.public(args.max_playback_gap_seconds) for sample in samples],
            }
        )

    report = json.dumps(
        {
            "started_at_utc": started_at_utc,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "validation_scope": "direct_model_http_no_backend_admission",
            "timing_measurement_notes": {
                "response_headers_seconds": (
                    "Client request start to response headers: roughly server request preparation "
                    "plus network time; not an exact server-stage duration."
                ),
                "headers_to_first_audio_seconds": (
                    "Per-request response headers to first non-empty PCM read: roughly queueing, "
                    "model generation, audio encoding and network time."
                ),
                "limitations": (
                    "These are client-observed intervals. Server/proxy header flushing determines "
                    "the boundary; queueing or preprocessing can occur on either side. "
                    "They do not isolate precise internal service stages or browser playback latency."
                ),
                "percentiles": (
                    "Each stage percentile uses known per-request intervals from successful requests; "
                    "headers-to-audio percentiles are not differences of independent percentiles."
                ),
            },
            "playback_simulation_notes": (
                "Schedules HTTP read chunks using PCM duration and the frontend start/rebuffer rule. "
                "Sample playback start/end seconds are relative to that request. Nominal overlap "
                "includes gaps; simulated all-streams-playing durations exclude gaps. "
                "This excludes backend admission, WebSocket transport, browser scheduling costs, "
                "device output, and subjective listening quality."
            ),
            "configuration": {
                "url": args.url,
                "model": args.model,
                "voice": args.voice,
                "concurrency": args.concurrency,
                "rounds": args.rounds,
                "stagger_ms": args.stagger_ms,
                "sample_rate": args.sample_rate,
                "max_new_tokens": args.max_new_tokens,
                "seed_base": args.seed_base,
                "playback_buffer_seconds": args.playback_buffer_seconds,
                "max_playback_gap_seconds": args.max_playback_gap_seconds,
                "min_common_playback_seconds": args.min_common_playback_seconds,
                "require_continuous_playback": args.require_continuous_playback,
                "text_file": str(args.text_file) if args.text_file is not None else None,
                "text_sha256": hashlib.sha256(synthesis_text.encode("utf-8")).hexdigest(),
                "text_char_count": len(synthesis_text),
            },
            "rounds": rounds,
        },
        ensure_ascii=False,
        indent=2,
    )
    print(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report + "\n", encoding="utf-8")
        except OSError as exc:
            parser.exit(2, f"cannot write output report: {exc}\n")
    return 1 if any_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())

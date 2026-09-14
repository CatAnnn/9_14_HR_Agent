#!/usr/bin/env python3
"""Exercise real Fish PCM through service admission and the in-process speech hub.

Uses synthetic identities and in-memory socket receivers; never creates database
sessions or invokes an LLM. Run from the repository root with ``python -m``.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
import hashlib
import json
import math
from pathlib import Path
import time
import uuid
from typing import Any

import httpx

from backend.config.settings import Settings
from backend.core.session_context import (
    get_current_auth_user_id,
    reset_current_auth_user_id,
    set_current_auth_user_id,
)
from backend.services.fish_tts_service import FishTtsService
from backend.services.speech_websocket_service import SpeechWebSocketHub
from scripts.benchmark_fish_tts import _simulate_playback


class MeasuredFish(FishTtsService):
    """Observe real requests without changing their payload or PCM."""

    def __init__(self, settings: Settings, client: Any) -> None:
        super().__init__(settings, client=client)
        self.active = 0
        self.peak = 0
        self.requests: list[str | None] = []

    async def _stream_sentence(self, *args, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.requests.append(get_current_auth_user_id())
        try:
            async with aclosing(super()._stream_sentence(*args, **kwargs)) as stream:
                async for event in stream:
                    yield event
        finally:
            self.active -= 1


class PcmReceiver:
    def __init__(self, hub, index, namespace, sample_rate, start_buffer_seconds):
        self.hub = hub
        self.index = index
        self.user_id = f"synthetic-fish-user-{namespace}-{index}"
        self.session_id = f"synthetic-fish-session-{namespace}-{index}"
        self.stream_id = f"synthetic-fish-stream-{namespace}-{index}"
        self.sample_rate = sample_rate
        self.start_buffer_seconds = start_buffer_seconds
        self.connection_id = None
        self.delivery = None
        self.ready = asyncio.Event()
        self.first_audio = asyncio.Event()
        self.finished = asyncio.Event()
        self.sequence = 0
        self.scheduled_end = 0.0
        self.packets: list[tuple[float, int]] = []
        self.expected_hashes: list[str] = []
        self.expected_bytes = 0
        self.received_hash = hashlib.sha256()
        self.expected_hash = hashlib.sha256()
        self.pending_audio = None
        self.completed = False
        self.cancel_requested = False
        self.cancelled = False
        self.error: str | None = None
        self.isolation_errors: list[str] = []
        self.completed_at: float | None = None

    def validate(self, condition: bool, message: str) -> None:
        if not condition:
            self.isolation_errors.append(message)
            raise RuntimeError(message)

    async def send_json(self, event):
        self.validate(event.get("session_id") == self.session_id, "wrong PCM session")
        self.validate(event.get("speech_stream_id") == self.stream_id, "wrong PCM stream")
        if event["event"] == "speech_audio":
            self.validate(self.pending_audio is None, "unpaired PCM metadata")
            index = event.get("audit_chunk_index")
            self.validate(
                isinstance(index, int) and 0 <= index < len(self.expected_hashes),
                "PCM metadata does not belong to this receiver",
            )
            self.validate(index == len(self.packets), "reordered or missing PCM frame")
            self.pending_audio = event
        elif event["event"] == "speech_error":
            self.error = str(event.get("message", "speech delivery failed"))

    async def send_bytes(self, pcm):
        event = self.pending_audio
        self.validate(event is not None, "PCM frame arrived without metadata")
        self.validate(len(pcm) > 0 and len(pcm) % 2 == 0, "invalid PCM byte length")
        self.validate(len(pcm) == event["byte_length"], "PCM length differs from metadata")
        self.validate(
            hashlib.sha256(pcm).hexdigest() == self.expected_hashes[event["audit_chunk_index"]],
            "PCM frame differs from this user's generated audio",
        )
        self.validate(event["sample_rate"] == self.sample_rate, "unexpected PCM sample rate")
        self.pending_audio = None
        now = time.perf_counter()
        self.packets.append((now, len(pcm)))
        self.received_hash.update(pcm)
        start = self.scheduled_end if self.scheduled_end > now else now + self.start_buffer_seconds
        self.scheduled_end = start + len(pcm) / (2 * self.sample_rate)
        self.first_audio.set()

    async def close(self, **_kwargs):
        pass

    async def report(self, buffered_seconds):
        self.sequence += 1
        return await self.hub.report_playback(
            self.session_id, self.connection_id, self.stream_id,
            buffered_seconds=min(120.0, max(0.0, buffered_seconds)),
            sequence=self.sequence,
        )

    def bind(self, event):
        event = {**event, "session_id": self.session_id, "speech_stream_id": self.stream_id}
        if event["event"] == "speech_audio":
            pcm = event["_pcm"]
            event["audit_chunk_index"] = len(self.expected_hashes)
            self.expected_hashes.append(hashlib.sha256(pcm).hexdigest())
            self.expected_hash.update(pcm)
            self.expected_bytes += len(pcm)
        return event


async def _run_user(player, service, text, seed, release_probe, feedback_interval):
    token = set_current_auth_user_id(player.user_id)
    producer = watcher = reporter = None

    async def produce():
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        queue.put_nowait(f"这是第{player.index + 1}位用户的测试。{text}")
        queue.put_nowait(None)
        async with aclosing(service.stream(
            queue, emotion_state=None, seed=seed, playback_buffer=player.delivery.playback,
        )) as stream:
            async for event in stream:
                if not player.delivery.enqueue(player.bind(event)):
                    if player.delivery.cancelled.is_set():
                        raise asyncio.CancelledError
                    raise RuntimeError("speech hub rejected an event")
                await asyncio.sleep(0)

    async def watch_cancel():
        await player.delivery.cancelled.wait()
        if not producer.done() and not producer.cancelling():
            producer.cancel()
        await asyncio.shield(asyncio.gather(producer, return_exceptions=True))

    async def feedback():
        while not player.finished.is_set():
            await player.report(player.scheduled_end - time.perf_counter())
            try:
                async with asyncio.timeout(feedback_interval):
                    await player.finished.wait()
            except TimeoutError:
                pass

    try:
        player.connection_id = await player.hub.register(player.session_id, player)
        player.delivery = await player.hub.open_stream(player.session_id, player.stream_id)
        if player.delivery is None:
            raise RuntimeError("could not open synthetic speech stream")
        if not await player.report(min(120.0, player.hub.playback_high_water_seconds + 1)):
            raise RuntimeError("initial playback feedback was rejected")
        producer = asyncio.create_task(produce())
        watcher = asyncio.create_task(watch_cancel())
        player.ready.set()
        await release_probe.wait()
        if not await player.report(0):
            raise RuntimeError("playback resume feedback was rejected")
        reporter = asyncio.create_task(feedback())
        await producer
        player.completed = True
    except asyncio.CancelledError:
        player.cancelled = True
        if not player.cancel_requested:
            player.error = player.error or "unexpected cancellation or scenario timeout"
    except Exception as exc:
        player.error = f"{type(exc).__name__}: {exc}"
    finally:
        player.ready.set()
        player.finished.set()
        for task in (producer, watcher, reporter):
            if task is not None and not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(
            *(task for task in (producer, watcher, reporter) if task is not None),
            return_exceptions=True,
        )
        if player.delivery is not None:
            await player.hub.close_stream(player.delivery, drain=player.completed)
        if player.connection_id is not None:
            await player.hub.unregister(player.session_id, player.connection_id)
        player.completed_at = time.perf_counter()
        reset_current_auth_user_id(token)


async def _first_audio_or_finished(player, worker):
    waiting = asyncio.create_task(player.first_audio.wait())
    try:
        await asyncio.wait((waiting, worker), return_when=asyncio.FIRST_COMPLETED)
        return player.first_audio.is_set()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


def _common_intervals(playbacks):
    common = list(playbacks[0].intervals) if playbacks else []
    for playback in playbacks[1:]:
        common = [
            (max(start, other_start), min(end, other_end))
            for start, end in common
            for other_start, other_end in playback.intervals
            if min(end, other_end) > max(start, other_start)
        ]
    return common


async def run_scenario(service, *, users, text, seed_base, timeout_seconds,
                       probe_hold_seconds=0.5, feedback_interval=0.5,
                       start_buffer_seconds=1.4, max_gap_seconds=0.1,
                       cancel_one=False, require_continuous=False,
                       min_common_playback_seconds=0.0):
    settings = service.settings
    hub = SpeechWebSocketHub(
        send_timeout_seconds=settings.tts_send_timeout_seconds,
        max_queue_bytes=settings.tts_audio_queue_max_bytes,
        max_queue_events=settings.tts_audio_queue_max_events,
        playback_high_water_seconds=settings.tts_playback_high_water_seconds,
        playback_low_water_seconds=settings.tts_playback_low_water_seconds,
        playback_feedback_timeout_seconds=settings.tts_playback_feedback_timeout_seconds,
    )
    namespace = uuid.uuid4().hex[:12]
    players = [PcmReceiver(hub, i, namespace, settings.tts_sample_rate, start_buffer_seconds)
               for i in range(users)]
    release_probe = asyncio.Event()
    service.peak = 0
    initial_requests = len(service.requests)
    workers = [asyncio.create_task(_run_user(
        player, service, text, seed_base + player.index, release_probe, feedback_interval,
    )) for player in players]
    probe_passed = False
    cancellation_accepted = False
    cancellation_snapshot = None
    scenario_error = None
    started_at = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(*(player.ready.wait() for player in players))
            await asyncio.sleep(probe_hold_seconds)
            probe_passed = (
                all(player.delivery is not None and not player.error for player in players)
                and len(service.requests) == initial_requests
                and service._slots._value == settings.tts_max_concurrency
            )
            started_at = time.perf_counter()
            release_probe.set()
            if cancel_one:
                ready = await asyncio.gather(*(
                    _first_audio_or_finished(player, worker)
                    for player, worker in zip(players, workers)
                ))
                if all(ready) and not workers[0].done():
                    player = players[0]
                    token = set_current_auth_user_id(player.user_id)
                    try:
                        player.cancel_requested = True
                        cancellation_accepted = await hub.cancel_stream(
                            player.session_id, player.connection_id, player.stream_id
                        )
                        cancellation_snapshot = [
                            {
                                "user_index": peer.index,
                                "in_progress": not worker.done() and not peer.finished.is_set(),
                                "audio_bytes": sum(size for _arrival, size in peer.packets),
                            }
                            for peer, worker in zip(players[1:], workers[1:])
                        ]
                    finally:
                        reset_current_auth_user_id(token)
            await asyncio.gather(*workers)
    except TimeoutError:
        scenario_error = "scenario deadline expired"
    finally:
        release_probe.set()
        for worker in workers:
            if not worker.done() and not worker.cancelling():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await hub.shutdown()

    samples = []
    playbacks = []
    bytes_at_cancellation = {
        item["user_index"]: item["audio_bytes"] for item in cancellation_snapshot or []
    }
    for player in players:
        playback = _simulate_playback(
            player.packets, sample_rate=player.sample_rate,
            playback_buffer_seconds=start_buffer_seconds,
        )
        playbacks.append(playback)
        byte_count = sum(size for _arrival, size in player.packets)
        complete_pcm = (
            byte_count == player.expected_bytes
            and player.received_hash.digest() == player.expected_hash.digest()
        )
        isolated = (
            not player.isolation_errors and player.pending_audio is None
            and (complete_pcm if player.completed else True)
        )
        samples.append({
            "user_index": player.index, "seed": seed_base + player.index,
            "session_id": player.session_id, "stream_id": player.stream_id,
            "completed": player.completed, "cancelled": player.cancelled,
            "error": player.error, "pcm_isolation_passed": isolated,
            "isolation_errors": player.isolation_errors,
            "audio_bytes": byte_count, "audio_seconds": byte_count / (2 * player.sample_rate),
            "audio_bytes_at_peer_cancellation": bytes_at_cancellation.get(player.index),
            "audio_bytes_after_peer_cancellation": (
                byte_count - bytes_at_cancellation[player.index]
                if player.index in bytes_at_cancellation else None
            ),
            "sha256": player.received_hash.hexdigest() if byte_count else None,
            "first_audio_seconds": player.packets[0][0] - started_at if player.packets else None,
            "completion_seconds": player.completed_at - started_at if player.completed_at else None,
            "feedback_reports": player.sequence,
            "simulated_gap_count": playback.gap_count,
            "simulated_max_gap_seconds": playback.max_gap_seconds,
        })
    common = _common_intervals(playbacks)
    common_seconds = sum(end - start for start, end in common)
    longest_common = max((end - start for start, end in common), default=0.0)
    complete = all(item["completed"] and not item["error"] and item["audio_bytes"] > 0
                   for item in samples)
    continuity = (
        complete and all(p.max_gap_seconds <= max_gap_seconds for p in playbacks)
        and longest_common > 0 and longest_common >= min_common_playback_seconds
    )
    capacity_released = service.active == 0 and service._slots._value == settings.tts_max_concurrency
    cancellation_passed = (
        cancellation_accepted and samples[0]["cancelled"] and not samples[0]["error"]
        and bool(cancellation_snapshot)
        and all(item["in_progress"] for item in cancellation_snapshot)
        and all(
            item["completed"] and not item["error"]
            and item["audio_bytes_after_peer_cancellation"] > 0
            for item in samples[1:]
        )
    ) if cancel_one else None
    cancellation_failure_reason = None
    if cancel_one and not cancellation_passed:
        if not cancellation_accepted:
            cancellation_failure_reason = "target finished or failed before cancellation could be exercised"
        elif not cancellation_snapshot or not all(item["in_progress"] for item in cancellation_snapshot):
            cancellation_failure_reason = "other users were not all still running at cancellation"
        elif not all(item["audio_bytes_after_peer_cancellation"] > 0 for item in samples[1:]):
            cancellation_failure_reason = "other users did not all receive further PCM after cancellation"
        else:
            cancellation_failure_reason = "target cancellation or remaining-user completion failed"
    passed = (
        not scenario_error and probe_passed and capacity_released
        and all(item["pcm_isolation_passed"] for item in samples)
        and (cancellation_passed if cancel_one else complete)
        and (not require_continuous or cancel_one or continuity)
    )
    return {
        "scenario": "cancel_one" if cancel_one else "multi_user_playback",
        "passed": bool(passed), "error": scenario_error,
        "feedback_admission_probe_passed": probe_passed,
        "probe_hold_seconds_excluded_from_first_audio": probe_hold_seconds,
        "capacity_released": capacity_released,
        "peak_simultaneous_http_inference_requests": service.peak,
        "inference_requests": len(service.requests) - initial_requests,
        "cancellation_passed": cancellation_passed,
        "cancellation_snapshot": cancellation_snapshot,
        "cancellation_failure_reason": cancellation_failure_reason,
        "simulated_continuous_playback_passed": None if cancel_one else continuity,
        "simulated_all_users_playing_seconds": common_seconds,
        "simulated_max_all_users_playing_seconds": longest_common,
        "min_common_playback_seconds": min_common_playback_seconds,
        "samples": samples,
    }


async def _run(args):
    settings = Settings(
        tts_enabled=True, distributed_coordination_enabled=False,
        tts_provider="fish_vllm_omni", tts_http_url=args.url, tts_model=args.model,
        tts_default_voice=args.voice, tts_available_voices=args.voice,
        tts_max_concurrency=args.slots, tts_global_max_concurrency=0,
        tts_sample_rate=args.sample_rate, tts_max_new_tokens=args.max_new_tokens,
    )
    results = []
    async with httpx.AsyncClient(trust_env=False) as client:
        service = MeasuredFish(settings, client)
        await service.ensure_ready()
        for cancel_one in ([False, True] if args.scenario == "both" else [args.scenario == "cancel"]):
            results.append(await run_scenario(
                service, users=args.users, text=args.text, seed_base=args.seed_base,
                timeout_seconds=args.timeout_seconds,
                start_buffer_seconds=args.playback_buffer_seconds,
                max_gap_seconds=args.max_playback_gap_seconds,
                min_common_playback_seconds=args.min_common_playback_seconds,
                cancel_one=cancel_one, require_continuous=args.require_continuous_playback,
            ))
    return {
        "validation_scope": "real_model_http_fish_service_in_process_hub_synthetic_receivers",
        "excluded": ["HTTP rehearsal routes", "database sessions", "Redis coordination",
                     "LLM generation", "real WebSocket network", "browser and audio device"],
        "pcm_validation_notes": (
            "Completion and PCM hashes verify transport completeness and user isolation only. "
            "They do not verify that speech contains all requested words, is non-silent, "
            "or is semantically correct; no ASR or listening assessment is performed."
        ),
        "configuration": {"concurrency": args.users, "local_slots": args.slots,
                          "url": args.url, "model": args.model,
                          "voice": args.voice, "scenario": args.scenario,
                          "sample_rate": args.sample_rate,
                          "max_new_tokens": args.max_new_tokens,
                          "seed_base": args.seed_base,
                          "timeout_seconds": args.timeout_seconds,
                          "playback_buffer_seconds": args.playback_buffer_seconds,
                          "max_playback_gap_seconds": args.max_playback_gap_seconds,
                          "min_common_playback_seconds": args.min_common_playback_seconds,
                          "require_continuous_playback": args.require_continuous_playback,
                          "playback_high_water_seconds": settings.tts_playback_high_water_seconds,
                          "playback_low_water_seconds": settings.tts_playback_low_water_seconds,
                          "playback_feedback_timeout_seconds": settings.tts_playback_feedback_timeout_seconds,
                          "settings_override": "in-memory TTS_ENABLED=true; environment unchanged"},
        "passed": all(result["passed"] for result in results),
        "scenarios": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", default="/app/checkpoints/s2-pro")
    parser.add_argument("--voice", default="employee-natural")
    parser.add_argument("--concurrency", "--users", dest="users", type=int, default=4)
    parser.add_argument("--slots", type=int, default=4, help="Local Fish inference capacity, independent of user count.")
    parser.add_argument("--scenario", choices=("playback", "cancel", "both"), default="both")
    parser.add_argument("--timeout-seconds", type=float, default=240)
    parser.add_argument("--sample-rate", type=int, default=44_100)
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--seed-base", type=int, default=51_000)
    parser.add_argument("--playback-buffer-seconds", type=float, default=1.4)
    parser.add_argument("--max-playback-gap-seconds", type=float, default=0.1)
    parser.add_argument("--min-common-playback-seconds", type=float, default=0.0)
    parser.add_argument("--require-continuous-playback", action="store_true")
    parser.add_argument("--output", type=Path, help="Write the complete JSON report to this file.")
    parser.add_argument("--text", default=(
        "我理解你对这次反馈有顾虑。我们先把已经完成的工作和当前标准逐项核对，"
        "再一起确定接下来的改进重点、支持方式和检查时间。"
        "需要额外资源的部分也可以单独列出来，我们会逐项讨论具体可执行的安排。"
    ))
    args = parser.parse_args()
    if not 1 <= args.users <= 12:
        parser.error("users must be between 1 and 12")
    if not 1 <= args.slots <= 12:
        parser.error("slots must be between 1 and 12")
    if not args.text.strip():
        parser.error("text must not be empty")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be finite and positive")
    for name in ("playback_buffer_seconds", "max_playback_gap_seconds", "min_common_playback_seconds"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) < 0:
            parser.error(f"{name} must be finite and non-negative")
    report = asyncio.run(_run(args))
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

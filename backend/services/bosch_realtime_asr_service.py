from __future__ import annotations

import asyncio
import io
import math
import sys
import time
import unicodedata
import wave
from array import array
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx

from backend.config.settings import Settings
from backend.observability.metrics import (
    record_asr_duration,
    record_asr_preview_window,
    record_asr_silence_skip,
)
from backend.services.recording_asr_service import (
    AsrFinalTranscriptionError,
    RemoteAsrTranscriber,
)
from backend.services.realtime_asr_service import AsrConfigurationError

if TYPE_CHECKING:
    from backend.redis.distributed_coordination import RedisDistributedCoordinator


SendFrontend = Callable[[dict[str, Any]], Awaitable[None]]

_preview_slots: asyncio.Semaphore | None = None
_preview_slots_size = 0


def _get_preview_slots(max_concurrency: int) -> asyncio.Semaphore:
    global _preview_slots, _preview_slots_size
    size = max(1, int(max_concurrency))
    if _preview_slots is None or _preview_slots_size != size:
        _preview_slots = asyncio.Semaphore(size)
        _preview_slots_size = size
    return _preview_slots


def _pcm_rms(pcm: bytes) -> float:
    aligned = pcm[: len(pcm) - (len(pcm) % 2)]
    if not aligned:
        return 0.0
    samples = array("h")
    samples.frombytes(aligned)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768.0


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm[: len(pcm) - (len(pcm) % 2)])
    return output.getvalue()


def _merge_transcript(existing: str, incoming: str) -> str:
    current = unicodedata.normalize("NFKC", str(existing or "").strip())
    addition = unicodedata.normalize("NFKC", str(incoming or "").strip())
    if not current:
        return addition
    if not addition or current.endswith(addition):
        return current
    if addition.startswith(current):
        return addition

    overlap_limit = min(len(current), len(addition), 120)
    for size in range(overlap_limit, 1, -1):
        if current[-size:].casefold() == addition[:size].casefold():
            return f"{current}{addition[size:]}".strip()

    separator = ""
    if (
        current[-1].isascii()
        and current[-1].isalnum()
        and addition[0].isascii()
        and addition[0].isalnum()
    ):
        separator = " "
    return f"{current}{separator}{addition}".strip()


@dataclass(frozen=True, slots=True)
class _PreviewSnapshot:
    index: int
    segment_id: int
    pcm: bytes
    final: bool
    first: bool
    early: bool


@dataclass(slots=True)
class _PreviewSegment:
    segment_id: int
    closed: bool = False
    queued_snapshot_count: int = 0
    last_snapshot_index: int = 0
    last_snapshot_coverage_bytes: int = 0
    final_snapshot_index: int = 0
    final_coverage_bytes: int = 0
    latest_applied_index: int = 0
    final_applied: bool = False
    final_succeeded: bool = False
    text: str = ""


class BoschChunkedAsrProxy:
    """Build corrected previews from cumulative, speech-bounded Bosch snapshots."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient,
        language: str | None = None,
        coordinator: RedisDistributedCoordinator | None = None,
    ) -> None:
        self.settings = settings
        self._language = str(
            language
            if language is not None
            else getattr(settings, "asr_language", "")
        ).strip()
        self._client = client
        self._transcriber = RemoteAsrTranscriber(
            settings,
            client=client,
            coordinator=coordinator,
        )
        self._closed = False
        self._connected = False
        self._failed = False
        self._finishing = False
        self._authoritative_final = False
        self._final_payload: dict[str, Any] | None = None
        self._speech_detected = False
        self._buffer = bytearray()
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._result_lock = asyncio.Lock()
        self._pending_first_snapshots: dict[int, _PreviewSnapshot] = {}
        self._pending_snapshots: dict[int, _PreviewSnapshot] = {}
        self._pending_event = asyncio.Event()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._inflight_snapshots = 0
        self._ready_lanes = 0
        self._lanes_ready = asyncio.Event()
        self._scheduler_stopping = False
        self._segments: dict[int, _PreviewSegment] = {}
        self._active_segment_id = 0
        self._next_segment_id = 0
        self._snapshot_index = 0
        self._consecutive_failures = 0
        self._event_stream_closed = False
        self._last_text = ""
        self._stable_text = ""
        self._unstable_text = ""
        self._revision = 0
        self._first_nonempty_received = False
        self._segment_has_speech = False
        self._segment_voiced_ms = 0.0
        self._silence_ms = 0.0
        self._last_snapshot_bytes = 0
        self._recording_id = f"bosch-preview-{uuid4().hex}"
        self._request_headers: dict[str, str] | None = None

        self._sample_rate = max(1, int(settings.asr_sample_rate))
        self._bytes_per_second = self._sample_rate * 2
        self._refresh_bytes = self._aligned_bytes(
            float(settings.asr_bosch_preview_chunk_seconds) * self._bytes_per_second
        )
        self._first_chunk_bytes = min(
            self._refresh_bytes,
            self._aligned_bytes(
                float(settings.asr_bosch_first_preview_chunk_seconds)
                * self._bytes_per_second
            ),
        )
        self._second_chunk_bytes = max(
            self._first_chunk_bytes,
            self._refresh_bytes,
        )
        self._max_segment_bytes = max(
            self._refresh_bytes,
            self._aligned_bytes(
                float(settings.asr_bosch_accumulation_max_seconds)
                * self._bytes_per_second
            ),
        )
        self._overlap_bytes = min(
            self._max_segment_bytes // 4,
            self._aligned_bytes(
                float(settings.asr_bosch_preview_overlap_ms)
                * self._bytes_per_second
                / 1000
            ),
        )
        self._pre_roll_bytes = max(
            self._overlap_bytes,
            self._aligned_bytes(0.3 * self._bytes_per_second),
        )
        self._analysis_frame_bytes = self._aligned_bytes(
            min(0.05 * self._bytes_per_second, self._refresh_bytes)
        )
        self._silence_commit_ms = float(settings.asr_bosch_silence_commit_ms)
        self._min_speech_ms = float(settings.asr_bosch_min_speech_ms)
        self._rms_threshold = max(
            0.0,
            float(settings.asr_local_silence_rms_threshold),
        )
        self._per_session_concurrency = max(
            1,
            min(
                int(settings.asr_bosch_preview_per_session_concurrency),
                int(settings.asr_bosch_preview_max_concurrency),
            ),
        )

    @staticmethod
    def _aligned_bytes(value: float) -> int:
        result = max(2, int(value))
        return result - (result % 2)

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._closed

    @property
    def last_text(self) -> str:
        return self._last_text

    @property
    def authoritative_final(self) -> bool:
        return self._authoritative_final

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def speech_detected(self) -> bool:
        return self._speech_detected

    def validate(self) -> None:
        if not self.settings.asr_enabled or not self.settings.asr_realtime_enabled:
            raise AsrConfigurationError("实时语音预览当前未启用。")
        if self.settings.asr_provider_mode != "bosch":
            raise AsrConfigurationError("当前 ASR 模式不是 Bosch 平台。")
        if not self.settings.asr_http_url.strip():
            raise AsrConfigurationError("ASR_HTTP_URL 未配置。")
        if not self.settings.asr_http_model.strip():
            raise AsrConfigurationError("ASR_HTTP_MODEL 未配置。")
        if not self.settings.effective_asr_api_key:
            raise AsrConfigurationError("Bosch ASR 凭据未配置。")

    async def connect(self) -> None:
        self.validate()
        if self._closed:
            raise AsrConfigurationError("Bosch ASR 预览会话已关闭。")
        try:
            self._request_headers = await self._transcriber.auth.async_headers(
                self.settings.effective_asr_api_key
            )
        except Exception as exc:
            raise AsrConfigurationError("Bosch ASR 凭据不可用。") from exc
        self._connected = True
        self._worker = asyncio.create_task(
            self._preview_worker(),
            name=f"bosch-asr-preview-{self._recording_id[-8:]}",
        )
        await self._lanes_ready.wait()

    def _start_segment(self) -> None:
        if self._active_segment_id:
            return
        self._next_segment_id += 1
        self._active_segment_id = self._next_segment_id
        self._segments[self._active_segment_id] = _PreviewSegment(
            segment_id=self._active_segment_id
        )
        self._last_snapshot_bytes = 0

    def _queue_current_snapshot(self, *, final: bool) -> None:
        segment_id = self._active_segment_id
        if not segment_id or not self._segment_has_speech:
            return
        pcm = bytes(self._buffer)
        if not pcm:
            return
        self._snapshot_index += 1
        segment = self._segments[segment_id]
        segment.queued_snapshot_count += 1
        snapshot = _PreviewSnapshot(
            index=self._snapshot_index,
            segment_id=segment_id,
            pcm=pcm,
            final=final,
            first=self._last_snapshot_bytes == 0,
            early=segment.queued_snapshot_count <= 2,
        )
        segment.last_snapshot_index = snapshot.index
        segment.last_snapshot_coverage_bytes = len(pcm)
        if final:
            segment.closed = True
            segment.final_snapshot_index = snapshot.index
            segment.final_coverage_bytes = len(pcm)
        # Keep the first speech snapshot until a lane starts it. During Bosch
        # authentication, newer cumulative snapshots may already arrive; they
        # may coalesce with each other but must not erase the TTFT request.
        if snapshot.first:
            self._pending_first_snapshots.setdefault(segment_id, snapshot)
        else:
            # At most one later not-yet-started snapshot is retained per
            # segment. In-flight stale results are ignored by snapshot index.
            self._pending_snapshots[segment_id] = snapshot
        self._last_snapshot_bytes = len(pcm)
        self._idle_event.clear()
        self._pending_event.set()

    def _preview_prompt(self) -> str:
        base = str(self.settings.asr_bosch_prompt or "").strip()
        context_limit = int(self.settings.asr_bosch_prompt_context_chars)
        recent = self._stable_text[-context_limit:].strip() if context_limit else ""
        if not recent:
            return base
        context = f"上一段已确认文本：{recent}"
        return f"{base}；{context}" if base else context

    def _close_current_segment(
        self,
        *,
        forced: bool,
        allow_snapshot_promotion: bool = False,
    ) -> None:
        if not self._segment_has_speech or self._segment_voiced_ms < self._min_speech_ms:
            if len(self._buffer) > self._pre_roll_bytes:
                del self._buffer[:-self._pre_roll_bytes]
                record_asr_silence_skip()
            return

        pcm = bytes(self._buffer)
        segment = self._segments[self._active_segment_id]
        if not (
            allow_snapshot_promotion
            and self._promote_latest_snapshot_if_complete(segment)
        ):
            self._queue_current_snapshot(final=True)
        if forced and self._overlap_bytes:
            self._buffer = bytearray(pcm[-self._overlap_bytes :])
        else:
            self._buffer.clear()
        self._active_segment_id = 0
        self._segment_has_speech = False
        self._segment_voiced_ms = 0.0
        self._silence_ms = 0.0
        self._last_snapshot_bytes = 0

    def _promote_latest_snapshot_if_complete(
        self,
        segment: _PreviewSegment,
    ) -> bool:
        """Reuse the latest cumulative request when only silence follows it."""

        if (
            segment.last_snapshot_index <= 0
            or segment.last_snapshot_coverage_bytes <= 0
        ):
            return False
        tail_bytes = max(
            0,
            len(self._buffer) - segment.last_snapshot_coverage_bytes,
        )
        silent_tail_bytes = int(
            self._silence_ms * self._bytes_per_second / 1000
        )
        silent_tail_bytes -= silent_tail_bytes % 2
        if tail_bytes > silent_tail_bytes:
            return False

        segment.closed = True
        segment.final_snapshot_index = segment.last_snapshot_index
        segment.final_coverage_bytes = segment.last_snapshot_coverage_bytes
        if (
            segment.latest_applied_index >= segment.final_snapshot_index
            and bool(segment.text.strip())
        ):
            segment.final_applied = True
            segment.final_succeeded = True
            self._stable_text, self._unstable_text, self._last_text = (
                self._compose_preview()
            )
        return True

    def _snapshot_is_final(self, snapshot: _PreviewSnapshot) -> bool:
        segment = self._segments.get(snapshot.segment_id)
        return bool(
            snapshot.final
            or (
                segment is not None
                and segment.closed
                and segment.final_snapshot_index == snapshot.index
            )
        )

    async def append_audio(self, pcm_bytes: bytes) -> None:
        if self._closed or self._finishing or self._failed or not pcm_bytes:
            return
        aligned = pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % 2)]
        if not aligned:
            return
        offset = 0
        while offset < len(aligned):
            frame = aligned[offset : offset + self._analysis_frame_bytes]
            offset += len(frame)
            previous_snapshot_index = self._snapshot_index
            self._append_audio_frame(frame)
            if self._snapshot_index != previous_snapshot_index:
                # Dispatch early and cumulative snapshots even when one browser
                # frame contains enough PCM to cross both boundaries.
                await asyncio.sleep(0)

    def _append_audio_frame(self, frame: bytes) -> None:
        self._buffer.extend(frame)
        duration_ms = len(frame) * 1000 / self._bytes_per_second
        if _pcm_rms(frame) >= self._rms_threshold:
            self._start_segment()
            self._speech_detected = True
            self._segment_has_speech = True
            self._segment_voiced_ms += duration_ms
            self._silence_ms = 0.0
        elif self._segment_has_speech:
            self._silence_ms += duration_ms
        elif len(self._buffer) > self._pre_roll_bytes:
            del self._buffer[:-self._pre_roll_bytes]

        if not self._active_segment_id:
            return

        forced = len(self._buffer) >= self._max_segment_bytes
        silence_boundary = (
            self._segment_has_speech
            and self._silence_ms >= self._silence_commit_ms
        )
        if forced or silence_boundary:
            self._close_current_segment(
                forced=forced,
                allow_snapshot_promotion=silence_boundary,
            )
            return

        first_snapshot = (
            self._last_snapshot_bytes == 0
            and self._segment_voiced_ms >= self._min_speech_ms
            and len(self._buffer) >= self._first_chunk_bytes
        )
        segment = self._segments[self._active_segment_id]
        if segment.queued_snapshot_count == 1:
            refresh_snapshot = len(self._buffer) >= self._second_chunk_bytes
        else:
            refresh_snapshot = (
                self._last_snapshot_bytes > 0
                and len(self._buffer) - self._last_snapshot_bytes
                >= self._refresh_bytes
            )
        if first_snapshot or refresh_snapshot:
            self._queue_current_snapshot(final=False)

    async def _next_snapshot(self) -> _PreviewSnapshot | None:
        while True:
            if self._pending_first_snapshots or self._pending_snapshots:
                snapshot = min(
                    (
                        *self._pending_first_snapshots.values(),
                        *self._pending_snapshots.values(),
                    ),
                    key=lambda item: item.index,
                )
                if self._pending_first_snapshots.get(snapshot.segment_id) is snapshot:
                    self._pending_first_snapshots.pop(snapshot.segment_id)
                else:
                    self._pending_snapshots.pop(snapshot.segment_id)
                self._inflight_snapshots += 1
                return snapshot
            if self._scheduler_stopping:
                return None
            self._pending_event.clear()
            if (
                self._pending_first_snapshots
                or self._pending_snapshots
                or self._scheduler_stopping
            ):
                continue
            await self._pending_event.wait()

    def _snapshot_finished(self) -> None:
        self._inflight_snapshots = max(0, self._inflight_snapshots - 1)
        if (
            not self._pending_first_snapshots
            and not self._pending_snapshots
            and self._inflight_snapshots == 0
        ):
            self._idle_event.set()

    async def _wait_for_idle(self) -> None:
        await self._idle_event.wait()

    async def _transcribe_preview_chunk(
        self,
        slots: asyncio.Semaphore,
        *,
        index: int,
        pcm: bytes,
        priority: int,
    ) -> str:
        wav_audio = _pcm_to_wav(pcm, self._sample_rate)
        roundtrip_started = time.perf_counter()
        queue_started = time.perf_counter()
        try:
            async with slots:
                record_asr_duration(
                    "preview_batch_wait",
                    (time.perf_counter() - queue_started) * 1000,
                    outcome="success",
                )
                inference_started = time.perf_counter()
                text = await self._transcriber.transcribe_wav(
                    wav_audio,
                    recording_id=self._recording_id,
                    index=index - 1,
                    language=self._language,
                    headers=self._request_headers,
                    prompt=self._preview_prompt(),
                    admission_priority=priority,
                    admission_stage="bosch_preview_global_queue",
                )
                record_asr_duration(
                    "preview_inference",
                    (time.perf_counter() - inference_started) * 1000,
                    outcome="success",
                )
        except asyncio.CancelledError:
            record_asr_duration(
                "preview_roundtrip",
                (time.perf_counter() - roundtrip_started) * 1000,
                outcome="cancelled",
            )
            raise
        except Exception:
            record_asr_duration(
                "preview_roundtrip",
                (time.perf_counter() - roundtrip_started) * 1000,
                outcome="failed",
            )
            raise
        record_asr_duration(
            "preview_roundtrip",
            (time.perf_counter() - roundtrip_started) * 1000,
            outcome="success",
        )
        record_asr_preview_window(len(pcm) / self._bytes_per_second)
        return text

    def _compose_preview(self) -> tuple[str, str, str]:
        stable = ""
        unstable = ""
        stable_prefix_open = True
        for segment_id in sorted(self._segments):
            segment = self._segments[segment_id]
            if stable_prefix_open and segment.closed and segment.final_applied:
                stable = _merge_transcript(stable, segment.text)
                continue
            stable_prefix_open = False
            unstable = _merge_transcript(unstable, segment.text)
        return stable, unstable, _merge_transcript(stable, unstable)

    async def _store_preview_result(
        self,
        *,
        snapshot: _PreviewSnapshot,
        text: str,
        succeeded: bool = True,
    ) -> None:
        async with self._result_lock:
            segment = self._segments.get(snapshot.segment_id)
            if segment is None or snapshot.index < segment.latest_applied_index:
                return
            segment.latest_applied_index = snapshot.index
            snapshot_is_final = self._snapshot_is_final(snapshot)
            normalized = unicodedata.normalize("NFKC", str(text or "").strip())
            if normalized:
                # Every snapshot covers the complete active speech segment.
                # Replace its provisional text instead of appending another
                # short-window hypothesis.
                segment.text = normalized
                self._first_nonempty_received = True
            if (
                snapshot_is_final
                and snapshot.index >= segment.final_snapshot_index
            ):
                segment.final_applied = True
                segment.final_succeeded = bool(succeeded and normalized)

            stable, unstable, preview = self._compose_preview()
            state = (stable, unstable, preview)
            if state == (self._stable_text, self._unstable_text, self._last_text):
                return
            self._stable_text = stable
            self._unstable_text = unstable
            self._last_text = preview
            self._revision += 1
            await self._events.put(
                {
                    "type": "partial",
                    "text": preview,
                    "preview": preview,
                    "stable_text": stable,
                    "unstable_text": unstable,
                    "revision": self._revision,
                    "segment_id": snapshot.segment_id,
                    "correction_state": (
                        "committed" if snapshot_is_final else "correcting"
                    ),
                    "window_seconds": round(
                        len(snapshot.pcm) / self._bytes_per_second,
                        3,
                    ),
                    "speech_detected": True,
                }
            )

    async def _preview_lane(self, slots: asyncio.Semaphore) -> None:
        self._ready_lanes += 1
        if self._ready_lanes >= self._per_session_concurrency:
            self._lanes_ready.set()
        while True:
            snapshot = await self._next_snapshot()
            if snapshot is None:
                return
            try:
                try:
                    text = await self._transcribe_preview_chunk(
                        slots,
                        index=snapshot.index,
                        pcm=snapshot.pcm,
                        priority=(
                            0
                            if self._snapshot_is_final(snapshot)
                            else 5
                            if snapshot.early and not self._first_nonempty_received
                            else 20
                        ),
                    )
                except (
                    AsrFinalTranscriptionError,
                    httpx.HTTPError,
                    ValueError,
                ):
                    self._consecutive_failures += 1
                    if self._snapshot_is_final(snapshot):
                        await self._store_preview_result(
                            snapshot=snapshot,
                            text="",
                            succeeded=False,
                        )
                    if self._consecutive_failures >= 3:
                        raise
                    continue
                self._consecutive_failures = 0
                await self._store_preview_result(
                    snapshot=snapshot,
                    text=text,
                )
            finally:
                self._snapshot_finished()

    async def _preview_worker(self) -> None:
        slots = _get_preview_slots(self.settings.asr_bosch_preview_max_concurrency)
        workers = [
            asyncio.create_task(
                self._preview_lane(slots),
                name=f"bosch-asr-lane-{self._recording_id[-8:]}-{index}",
            )
            for index in range(self._per_session_concurrency)
        ]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            raise
        except (AsrFinalTranscriptionError, httpx.HTTPError, ValueError):
            self._failed = True
            self._connected = False
            await self._events.put(
                {
                    "type": "preview_unavailable",
                    "code": "bosch_preview_failed",
                    "message": "Bosch 实时预览暂不可用，完整录音仍在继续。",
                    "recoverable": True,
                }
            )
        finally:
            self._scheduler_stopping = True
            self._pending_event.set()
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if (
                not self._pending_first_snapshots
                and not self._pending_snapshots
                and self._inflight_snapshots == 0
            ):
                self._idle_event.set()

    def _verified_final_payload(self, *, timed_out: bool) -> dict[str, Any]:
        segments = [self._segments[key] for key in sorted(self._segments)]
        complete = bool(segments) and all(
            segment.closed
            and segment.final_applied
            and segment.final_succeeded
            and segment.final_coverage_bytes > 0
            and bool(segment.text.strip())
            for segment in segments
        )
        authoritative = bool(
            self.settings.asr_bosch_verified_stream_final_enabled
            and self._speech_detected
            and not self._failed
            and not timed_out
            and not self._active_segment_id
            and complete
            and self._last_text.strip()
        )
        self._authoritative_final = authoritative
        return {
            "text": self._last_text,
            "stable_text": self._stable_text,
            "unstable_text": self._unstable_text,
            "speech_detected": self._speech_detected,
            "revision": self._revision,
            "authoritative": authoritative,
            "segments_total": len(segments),
            "segments_complete": sum(
                1
                for segment in segments
                if segment.final_applied and segment.final_succeeded
            ),
            "timed_out": timed_out,
        }

    async def finish(self) -> dict[str, Any]:
        if self._closed:
            raise AsrConfigurationError("Bosch ASR 预览会话已关闭。")
        if self._final_payload is not None:
            return dict(self._final_payload)

        self._finishing = True
        if self._active_segment_id:
            self._close_current_segment(forced=False)

        timed_out = False
        try:
            await asyncio.wait_for(
                self._wait_for_idle(),
                timeout=float(self.settings.asr_bosch_tail_final_timeout_seconds),
            )
        except TimeoutError:
            timed_out = True

        self._scheduler_stopping = True
        if timed_out:
            self._pending_first_snapshots.clear()
            self._pending_snapshots.clear()
        self._pending_event.set()
        if self._worker is not None and not self._worker.done():
            if timed_out:
                self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        self._connected = False
        self._final_payload = self._verified_final_payload(timed_out=timed_out)
        return dict(self._final_payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        self._scheduler_stopping = True
        self._pending_first_snapshots.clear()
        self._pending_snapshots.clear()
        self._pending_event.set()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        if not self._event_stream_closed:
            self._event_stream_closed = True
            await self._events.put(None)

    async def receive_loop(self, send_frontend: SendFrontend) -> None:
        while True:
            event = await self._events.get()
            if event is None:
                return
            await send_frontend(event)

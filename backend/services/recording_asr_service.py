from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import shutil
import re
import sys
import time
import unicodedata
import uuid
import wave
from array import array
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, BinaryIO

import httpx

from backend.config.settings import Settings
from backend.observability.metrics import (
    record_asr_disk_free,
    record_asr_duration,
    record_asr_final_segments,
)
from backend.services.asr_activity import mark_bosch_http_asr_activity
from backend.services.model_api_auth import ModelAPIAuth

if TYPE_CHECKING:
    from backend.redis.distributed_coordination import RedisDistributedCoordinator


logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Awaitable[None]]

_final_slots: asyncio.Semaphore | None = None
_final_slots_size = 0
_SPEECH_RMS_WINDOW_FRAMES = 50
_NORMAL_SPEECH_MIN_FRAMES = 6
_STRONG_SPEECH_MIN_FRAMES = 4


def _longest_rms_run(
    values: Sequence[float],
    threshold: float,
) -> tuple[int, float]:
    longest_run = 0
    current_run = 0
    current_min = 0.0
    current_max = 0.0
    longest_range = 0.0
    for value in values:
        if value > threshold:
            current_run += 1
            if current_run == 1:
                current_min = value
                current_max = value
            else:
                current_min = min(current_min, value)
                current_max = max(current_max, value)
            current_range = current_max - current_min
            if current_run > longest_run:
                longest_run = current_run
                longest_range = current_range
            elif current_run == longest_run:
                longest_range = max(longest_range, current_range)
        else:
            current_run = 0
    return longest_run, longest_range


def _has_sustained_speech(
    frame_rms: Sequence[float],
    *,
    rms_threshold: float,
) -> bool:
    if not frame_rms:
        return False
    base_threshold = max(0.0, float(rms_threshold))
    ordered = sorted(frame_rms)
    floor_index = max(0, int(round((len(ordered) - 1) * 0.2)))
    noise_floor = ordered[floor_index]
    dynamic_ceiling = max(base_threshold * 1.75, base_threshold + 0.0045)
    dynamic_threshold = max(
        base_threshold,
        min(noise_floor * 1.6, dynamic_ceiling),
    )
    strong_threshold = max(base_threshold * 3.0, 0.018)
    normal_run, normal_range = _longest_rms_run(frame_rms, dynamic_threshold)
    strong_run, _ = _longest_rms_run(frame_rms, strong_threshold)
    return (
        (
            normal_run >= _NORMAL_SPEECH_MIN_FRAMES
            and normal_range >= max(0.0015, base_threshold * 0.25)
        )
        or strong_run >= _STRONG_SPEECH_MIN_FRAMES
    )


class AsrStorageError(RuntimeError):
    """Raised when a complete recording cannot be stored safely."""


class AsrFinalTranscriptionError(RuntimeError):
    """Raised when any segment of the authoritative transcription fails."""


def extract_transcript(value: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "transcript", "transcription"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key in ("output", "data", "result", "choices", "message", "content"):
            if key in value:
                candidate = extract_transcript(value[key], depth + 1)
                if candidate:
                    return candidate
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            candidate = extract_transcript(item, depth + 1)
            if candidate:
                return candidate
    return ""


def _get_final_slots(max_concurrency: int) -> asyncio.Semaphore:
    global _final_slots, _final_slots_size
    size = max(1, int(max_concurrency))
    if _final_slots is None or _final_slots_size != size:
        _final_slots = asyncio.Semaphore(size)
        _final_slots_size = size
    return _final_slots


def _isolation_digest(value: str) -> str:
    normalized = value.strip().casefold() or "unbound"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        logger.debug("Unable to tighten ASR temporary directory mode", exc_info=True)


def prepare_recording_storage(settings: Settings) -> Path:
    root = Path(settings.asr_recording_tmp_dir)
    _ensure_private_directory(root)
    free_bytes = shutil.disk_usage(root).free
    record_asr_disk_free(free_bytes)
    if free_bytes < int(settings.asr_recording_min_free_bytes):
        raise AsrStorageError("服务器录音空间不足，录音已停止，请稍后重试。")
    return root


def _minimum_rms_boundary(
    pcm: bytes,
    *,
    absolute_start: int,
    frame_bytes: int,
) -> int:
    if not pcm:
        return absolute_start
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    frame_samples = max(1, frame_bytes // 2)
    best_offset = len(samples)
    best_energy: int | None = None
    for offset in range(0, len(samples), frame_samples):
        frame = samples[offset : offset + frame_samples]
        if not frame:
            continue
        energy = sum(sample * sample for sample in frame) // len(frame)
        if best_energy is None or energy < best_energy:
            best_energy = energy
            best_offset = min(len(samples), offset + len(frame))
    return absolute_start + best_offset * 2


def segment_pcm_ranges(
    path: Path,
    *,
    sample_rate: int,
    max_segment_seconds: int,
    silence_search_seconds: int = 5,
) -> list[tuple[int, int]]:
    size = path.stat().st_size
    size -= size % 2
    if size <= 0:
        return []
    bytes_per_second = sample_rate * 2
    max_segment_bytes = max(2, max_segment_seconds * bytes_per_second)
    search_bytes = max(0, silence_search_seconds * bytes_per_second)
    frame_bytes = max(2, int(round(sample_rate * 0.02)) * 2)
    ranges: list[tuple[int, int]] = []
    start = 0
    with path.open("rb") as source:
        while size - start > max_segment_bytes:
            target = start + max_segment_bytes
            search_start = max(start + frame_bytes, target - search_bytes)
            search_start -= search_start % 2
            source.seek(search_start)
            search_audio = source.read(target - search_start)
            boundary = _minimum_rms_boundary(
                search_audio,
                absolute_start=search_start,
                frame_bytes=frame_bytes,
            )
            boundary = min(target, max(start + frame_bytes, boundary))
            boundary -= boundary % 2
            ranges.append((start, boundary))
            start = boundary
    if start < size:
        ranges.append((start, size))
    return ranges


def overlap_pcm_ranges(
    ranges: Sequence[tuple[int, int]],
    *,
    sample_rate: int,
    overlap_seconds: float,
) -> list[tuple[int, int]]:
    overlap_bytes = max(
        0,
        int(round(float(overlap_seconds) * sample_rate)) * 2,
    )
    overlap_bytes -= overlap_bytes % 2
    if overlap_bytes <= 0:
        return list(ranges)

    expanded: list[tuple[int, int]] = []
    for index, (start, end) in enumerate(ranges):
        adjusted_start = start if index == 0 else max(0, start - overlap_bytes)
        adjusted_start -= adjusted_start % 2
        expanded.append((adjusted_start, end))
    return expanded


def pcm_range_to_wav(
    path: Path,
    *,
    start: int,
    end: int,
    sample_rate: int,
) -> bytes:
    with path.open("rb") as source:
        source.seek(start)
        pcm = source.read(max(0, end - start))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


def join_segment_transcripts(parts: Sequence[str]) -> str:
    merged = ""
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        if (
            merged
            and merged[-1].isascii()
            and merged[-1].isalnum()
            and text[0].isascii()
            and text[0].isalnum()
        ):
            merged = f"{merged} {text}"
        else:
            merged = f"{merged}{text}"
    return merged.strip()


def _reliable_overlap_length(existing: str, incoming: str) -> int:
    max_size = min(len(existing), len(incoming), 240)
    for size in range(max_size, 5, -1):
        if existing[-size:].casefold() != incoming[:size].casefold():
            continue
        candidate = incoming[:size]
        cjk_count = sum(1 for char in candidate if "\u4e00" <= char <= "\u9fff")
        if cjk_count >= 6:
            return size
        ascii_words = re.findall(r"[A-Za-z0-9]+", candidate)
        if len(ascii_words) >= 2 and sum(map(len, ascii_words)) >= 8:
            return size
    return 0


def join_overlapping_segment_transcripts(parts: Sequence[str]) -> str:
    merged = ""
    for part in parts:
        text = unicodedata.normalize("NFKC", str(part or "").strip())
        if not text:
            continue
        overlap = _reliable_overlap_length(merged, text)
        if overlap:
            text = text[overlap:].lstrip()
        if not text:
            continue
        merged = join_segment_transcripts((merged, text))
    return merged


class PcmRecording:
    """Disk-backed PCM capture with no application-level duration limit."""

    def __init__(
        self,
        settings: Settings,
        *,
        owner_key: str = "unbound",
        session_id: str = "unbound",
    ):
        self.settings = settings
        self.recording_id = uuid.uuid4().hex
        self.storage_root = Path(settings.asr_recording_tmp_dir)
        self.owner_scope = _isolation_digest(owner_key)
        self.session_scope = _isolation_digest(session_id)
        self.root = (
            self.storage_root
            / f"user-{self.owner_scope}"
            / f"session-{self.session_scope}"
        )
        self.path = self.root / f"{self.recording_id}.pcm"
        self._file: BinaryIO | None = None
        self._bytes_written = 0
        self._next_disk_check = 0
        self._closed = False
        self._speech_buffer = bytearray()
        self._speech_frame_rms: deque[float] = deque(
            maxlen=_SPEECH_RMS_WINDOW_FRAMES
        )
        self._speech_detected = False
        self._speech_peak_rms = 0.0

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def duration_seconds(self) -> float:
        bytes_per_second = max(1, int(self.settings.asr_sample_rate) * 2)
        return self._bytes_written / bytes_per_second

    @property
    def speech_detected(self) -> bool:
        return self._speech_detected

    @property
    def speech_peak_rms(self) -> float:
        return self._speech_peak_rms

    def has_speech(self) -> bool:
        return self._speech_detected

    def _track_speech_activity(self, pcm_bytes: bytes) -> None:
        if self._speech_detected or not pcm_bytes:
            return
        sample_rate = max(1, int(self.settings.asr_sample_rate))
        frame_bytes = max(2, int(round(sample_rate * 0.02)) * 2)
        threshold = max(
            0.0,
            float(getattr(self.settings, "asr_local_silence_rms_threshold", 0.006)),
        )
        self._speech_buffer.extend(pcm_bytes)
        consumed = 0
        while len(self._speech_buffer) - consumed >= frame_bytes:
            frame = self._speech_buffer[consumed : consumed + frame_bytes]
            samples = array("h")
            samples.frombytes(frame)
            if sys.byteorder != "little":
                samples.byteswap()
            mean_square = sum(sample * sample for sample in samples) / len(samples)
            rms = (mean_square ** 0.5) / 32768.0
            self._speech_peak_rms = max(self._speech_peak_rms, rms)
            self._speech_frame_rms.append(rms)
            if _has_sustained_speech(
                self._speech_frame_rms,
                rms_threshold=threshold,
            ):
                self._speech_detected = True
                self._speech_buffer.clear()
                return
            consumed += frame_bytes
        if consumed:
            del self._speech_buffer[:consumed]

    def _check_disk_space(self) -> None:
        free_bytes = shutil.disk_usage(self.root).free
        record_asr_disk_free(free_bytes)
        if free_bytes < int(self.settings.asr_recording_min_free_bytes):
            raise AsrStorageError("服务器录音空间不足，录音已停止，请稍后重试。")
        self._next_disk_check = self._bytes_written + 1_048_576

    def open(self) -> None:
        if self._file is not None:
            return
        _ensure_private_directory(self.storage_root)
        _ensure_private_directory(self.root.parent)
        _ensure_private_directory(self.root)
        self._check_disk_space()
        descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        self._file = os.fdopen(descriptor, "wb", buffering=0)

    def append(self, pcm_bytes: bytes) -> None:
        if self._closed or self._file is None:
            raise AsrStorageError("录音文件未就绪。")
        if not pcm_bytes:
            return
        if len(pcm_bytes) % 2:
            raise AsrStorageError("收到的录音数据格式无效。")
        if self._bytes_written >= self._next_disk_check:
            self._check_disk_space()
        try:
            self._file.write(pcm_bytes)
        except OSError as exc:
            raise AsrStorageError("录音写入失败，请检查服务器存储空间。") from exc
        self._bytes_written += len(pcm_bytes)
        self._track_speech_activity(pcm_bytes)

    def _close_input_sync(self) -> None:
        if self._closed:
            return
        self._closed = True
        file_handle = self._file
        self._file = None
        if file_handle is not None:
            file_handle.flush()
            file_handle.close()

    async def close_input(self) -> None:
        await asyncio.to_thread(self._close_input_sync)

    def _discard_sync(self) -> None:
        try:
            self._close_input_sync()
        finally:
            try:
                self.path.unlink(missing_ok=True)
                for directory in (self.root, self.root.parent):
                    try:
                        directory.rmdir()
                    except OSError:
                        break
            except OSError:
                logger.warning("Unable to remove temporary ASR recording", exc_info=True)

    async def discard(self) -> None:
        await asyncio.to_thread(self._discard_sync)


def _read_pcm_range(path: Path, *, start: int, end: int) -> bytes:
    with path.open("rb") as source:
        source.seek(start)
        return source.read(max(0, end - start))


def _pcm_has_speech(pcm: bytes, *, rms_threshold: float) -> bool:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return False
    if sys.byteorder != "little":
        samples.byteswap()
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return (mean_square ** 0.5) / 32768.0 >= rms_threshold


def _transcript_signal_length(value: str) -> int:
    normalized = unicodedata.normalize("NFKC", value)
    return sum(1 for character in normalized if character.isalnum())


class LocalAsrTranscriber:
    # Fast full-recording transcription through the resident Qwen3-ASR model.

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.client = client
        self.client_factory = client_factory

    @property
    def endpoint(self) -> str:
        base_url = str(self.settings.asr_local_streaming_url or "").rstrip("/")
        return f"{base_url}/api/transcribe" if base_url else ""

    async def transcribe(
        self,
        recording: PcmRecording,
        *,
        language: str | None = None,
        progress: ProgressCallback | None = None,
        expected_text: str = "",
    ) -> str:
        if not self.settings.asr_realtime_enabled or not self.endpoint:
            raise AsrFinalTranscriptionError("本地完整语音转写服务当前不可用。")
        await recording.close_input()
        max_segment_seconds = min(
            int(self.settings.asr_local_final_segment_seconds),
            max(3, int(float(self.settings.asr_local_stream_window_seconds))),
        )
        ranges = await asyncio.to_thread(
            segment_pcm_ranges,
            recording.path,
            sample_rate=int(self.settings.asr_sample_rate),
            max_segment_seconds=max_segment_seconds,
        )
        if not ranges:
            raise AsrFinalTranscriptionError("没有检测到可转写的语音。")
        record_asr_final_segments(len(ranges))

        worker_count = min(
            len(ranges),
            max(1, int(self.settings.asr_local_final_per_recording_concurrency)),
        )
        completed = 0
        progress_lock = asyncio.Lock()
        timeout = httpx.Timeout(float(self.settings.asr_local_final_timeout_seconds))
        client_context = (
            nullcontext(self.client)
            if self.client is not None
            else self.client_factory(timeout=timeout)
        )

        async with client_context as client:
            work_items = iter(enumerate(ranges))
            parts = [""] * len(ranges)

            async def run_segment(index: int, byte_range: tuple[int, int]) -> str:
                nonlocal completed
                pcm = await asyncio.to_thread(
                    _read_pcm_range,
                    recording.path,
                    start=byte_range[0],
                    end=byte_range[1],
                )
                started_at = time.perf_counter()
                text = await self._transcribe_segment(
                    client,
                    pcm=pcm,
                    index=index,
                    language=(
                        self.settings.asr_language
                        if language is None
                        else language
                    ).strip(),
                )
                record_asr_duration(
                    "local_final_segment",
                    (time.perf_counter() - started_at) * 1000,
                    outcome="success",
                )
                if not text and _pcm_has_speech(
                    pcm,
                    rms_threshold=float(self.settings.asr_local_silence_rms_threshold),
                ):
                    raise AsrFinalTranscriptionError(
                        f"本地完整语音第 {index + 1} 段没有可用文本。"
                    )
                async with progress_lock:
                    completed += 1
                    if progress is not None:
                        await progress(completed, len(ranges))
                return text

            async def worker() -> None:
                while True:
                    try:
                        index, byte_range = next(work_items)
                    except StopIteration:
                        return
                    parts[index] = await run_segment(index, byte_range)

            tasks = [
                asyncio.create_task(
                    worker(),
                    name=f"asr-local-final-{recording.recording_id[:8]}-{index}",
                )
                for index in range(worker_count)
            ]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        transcript = join_segment_transcripts(parts)
        if not transcript:
            raise AsrFinalTranscriptionError("本地完整语音转写未返回可用文本。")
        expected_length = _transcript_signal_length(expected_text)
        actual_length = _transcript_signal_length(transcript)
        minimum_ratio = float(self.settings.asr_local_final_min_preview_ratio)
        if expected_length >= 8 and actual_length < int(expected_length * minimum_ratio):
            raise AsrFinalTranscriptionError(
                "本地完整语音转写疑似缺失内容，改用远程高质量转写。"
            )
        return transcript

    async def _transcribe_segment(
        self,
        client: httpx.AsyncClient,
        *,
        pcm: bytes,
        index: int,
        language: str,
    ) -> str:
        response: httpx.Response | None = None
        for attempt in range(2):
            try:
                request_options: dict[str, Any] = {}
                if language:
                    request_options["params"] = {"language": language}
                response = await client.post(
                    self.endpoint,
                    content=pcm,
                    headers={"Content-Type": "audio/pcm"},
                    timeout=httpx.Timeout(
                        float(self.settings.asr_local_final_timeout_seconds)
                    ),
                    **request_options,
                )
            except httpx.HTTPError as exc:
                if attempt == 0:
                    await asyncio.sleep(0.1)
                    continue
                raise AsrFinalTranscriptionError(
                    f"本地完整语音第 {index + 1} 段请求失败。"
                ) from exc
            if response.status_code >= 500 and attempt == 0:
                await asyncio.sleep(0.1)
                continue
            break

        if response is None or response.status_code >= 400:
            status_code = response.status_code if response is not None else "unknown"
            raise AsrFinalTranscriptionError(
                f"本地完整语音第 {index + 1} 段返回错误 {status_code}。"
            )
        try:
            return extract_transcript(response.json())
        except ValueError as exc:
            raise AsrFinalTranscriptionError(
                f"本地完整语音第 {index + 1} 段返回了无效响应。"
            ) from exc


class RemoteAsrTranscriber:
    def __init__(
        self,
        settings: Settings,
        *,
        auth: ModelAPIAuth | None = None,
        client: httpx.AsyncClient | None = None,
        client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
        coordinator: RedisDistributedCoordinator | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth or ModelAPIAuth()
        self.client = client
        self.client_factory = client_factory
        self.coordinator = coordinator

    @asynccontextmanager
    async def _platform_lease(
        self,
        *,
        priority: int,
        stage: str,
    ) -> AsyncIterator[None]:
        capacity = int(
            getattr(self.settings, "asr_bosch_global_max_concurrency", 0)
        )
        if self.coordinator is None or capacity <= 0:
            yield
            return

        async with self.coordinator.lease(
            "asr:bosch:http-inference",
            capacity=capacity,
            timeout_seconds=0.0,
            priority=priority,
        ) as slot:
            record_asr_duration(
                stage,
                slot.wait_ms,
                outcome="success",
            )
            yield

    async def transcribe_wav(
        self,
        audio: bytes,
        *,
        recording_id: str,
        index: int = 0,
        language: str | None = None,
        headers: dict[str, str] | None = None,
        prompt: str = "",
        admission_priority: int = 20,
        admission_stage: str = "bosch_preview_global_queue",
    ) -> str:
        """Transcribe one in-memory WAV while reusing the normal Bosch adapter."""

        if not audio:
            raise AsrFinalTranscriptionError("没有收到可转写的语音。")
        request_headers = headers
        if request_headers is None:
            try:
                request_headers = await self.auth.async_headers(
                    self.settings.effective_asr_api_key
                )
            except Exception as exc:
                raise AsrFinalTranscriptionError("完整语音转写凭据未配置。") from exc
        timeout = httpx.Timeout(float(self.settings.asr_timeout_seconds))
        client_context = (
            nullcontext(self.client)
            if self.client is not None
            else self.client_factory(timeout=timeout)
        )
        async with client_context as client:
            async with self._platform_lease(
                priority=admission_priority,
                stage=admission_stage,
            ):
                return await self._transcribe_segment(
                    client,
                    headers=request_headers,
                    audio=audio,
                    index=index,
                    recording_id=recording_id,
                    language=(
                        self.settings.asr_language
                        if language is None
                        else language
                    ).strip(),
                    prompt=prompt,
                )

    async def transcribe(
        self,
        recording: PcmRecording,
        *,
        language: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> str:
        if not self.settings.asr_enabled or not self.settings.asr_http_url:
            raise AsrFinalTranscriptionError("完整语音转写服务当前不可用。")
        await recording.close_input()
        ranges = await asyncio.to_thread(
            segment_pcm_ranges,
            recording.path,
            sample_rate=int(self.settings.asr_sample_rate),
            max_segment_seconds=int(self.settings.asr_final_segment_seconds),
        )
        ranges = overlap_pcm_ranges(
            ranges,
            sample_rate=int(self.settings.asr_sample_rate),
            overlap_seconds=float(
                getattr(self.settings, "asr_final_segment_overlap_seconds", 0.0)
            ),
        )
        if not ranges:
            raise AsrFinalTranscriptionError("没有检测到可转写的语音。")
        record_asr_final_segments(len(ranges))
        try:
            headers = await self.auth.async_headers(self.settings.effective_asr_api_key)
        except Exception as exc:
            raise AsrFinalTranscriptionError("完整语音转写凭据未配置。") from exc

        worker_count = min(
            len(ranges),
            max(1, int(self.settings.asr_final_per_recording_concurrency)),
        )
        final_slots = _get_final_slots(self.settings.asr_final_max_concurrency)
        completed = 0
        progress_lock = asyncio.Lock()
        timeout = httpx.Timeout(float(self.settings.asr_timeout_seconds))
        client_context = (
            nullcontext(self.client)
            if self.client is not None
            else self.client_factory(timeout=timeout)
        )

        async with client_context as client:
            async def run_segment(index: int, byte_range: tuple[int, int]) -> str:
                nonlocal completed
                queue_started = time.perf_counter()
                async with final_slots:
                    record_asr_duration(
                        "final_queue",
                        (time.perf_counter() - queue_started) * 1000,
                        outcome="success",
                    )
                    wav_audio = await asyncio.to_thread(
                        pcm_range_to_wav,
                        recording.path,
                        start=byte_range[0],
                        end=byte_range[1],
                        sample_rate=int(self.settings.asr_sample_rate),
                    )
                    async with self._platform_lease(
                        priority=0,
                        stage="bosch_final_global_queue",
                    ):
                        text = await self._transcribe_segment(
                            client,
                            headers=headers,
                            audio=wav_audio,
                            index=index,
                            recording_id=recording.recording_id,
                            language=(
                                self.settings.asr_language
                                if language is None
                                else language
                            ).strip(),
                            prompt=str(getattr(self.settings, "asr_bosch_prompt", "")),
                        )
                async with progress_lock:
                    completed += 1
                    if progress is not None:
                        await progress(completed, len(ranges))
                return text

            work_items = iter(enumerate(ranges))
            parts = [""] * len(ranges)

            async def worker() -> None:
                while True:
                    try:
                        index, byte_range = next(work_items)
                    except StopIteration:
                        return
                    parts[index] = await run_segment(index, byte_range)

            tasks = [
                asyncio.create_task(
                    worker(),
                    name=f"asr-final-worker-{recording.recording_id[:8]}-{index}",
                )
                for index in range(worker_count)
            ]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        transcript = join_overlapping_segment_transcripts(parts)
        if not transcript:
            raise AsrFinalTranscriptionError("完整语音转写未返回可用文本。")
        return transcript

    async def _transcribe_segment(
        self,
        client: httpx.AsyncClient,
        *,
        headers: dict[str, str],
        audio: bytes,
        index: int,
        recording_id: str,
        language: str,
        prompt: str = "",
    ) -> str:
        data = {"model": self.settings.asr_http_model}
        if language:
            data["language"] = language
        normalized_prompt = str(prompt or "").strip()
        if normalized_prompt:
            data["prompt"] = normalized_prompt
        files = {
            "file": (
                f"{recording_id}_{index + 1:04d}.wav",
                audio,
                "audio/wav",
            )
        }
        response: httpx.Response | None = None
        for attempt in range(2):
            mark_bosch_http_asr_activity()
            try:
                response = await client.post(
                    self.settings.asr_http_url,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=httpx.Timeout(float(self.settings.asr_timeout_seconds)),
                )
            except httpx.HTTPError as exc:
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise AsrFinalTranscriptionError(
                    f"完整语音第 {index + 1} 段请求失败。"
                ) from exc
            finally:
                mark_bosch_http_asr_activity()

            retryable = (
                response.status_code in {408, 429}
                or response.status_code >= 500
            )
            if retryable and attempt == 0:
                await asyncio.sleep(0.5)
                continue
            break

        if response is None:
            raise AsrFinalTranscriptionError(f"完整语音第 {index + 1} 段请求失败。")
        if response.status_code in {401, 403}:
            raise AsrFinalTranscriptionError("完整语音转写服务未接受当前凭据。")
        if response.status_code >= 400:
            raise AsrFinalTranscriptionError(
                f"完整语音第 {index + 1} 段返回错误 {response.status_code}。"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AsrFinalTranscriptionError(
                f"完整语音第 {index + 1} 段返回了无效响应。"
            ) from exc
        transcript = extract_transcript(payload)
        if not transcript:
            raise AsrFinalTranscriptionError(
                f"完整语音第 {index + 1} 段没有可用文本。"
            )
        return transcript


def cleanup_stale_recordings(settings: Settings) -> int:
    root = Path(settings.asr_recording_tmp_dir)
    _ensure_private_directory(root)
    cutoff = time.time() - int(settings.asr_recording_orphan_ttl_seconds)
    removed = 0
    for path in root.rglob("*.pcm"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Unable to clean stale ASR recording", exc_info=True)
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            continue
    return removed


async def recording_cleanup_loop(settings: Settings) -> None:
    interval = min(
        300,
        max(60, int(settings.asr_recording_orphan_ttl_seconds) // 2),
    )
    while True:
        removed = await asyncio.to_thread(cleanup_stale_recordings, settings)
        if removed:
            logger.info("Removed %s stale ASR recording files", removed)
        await asyncio.sleep(interval)

#!/usr/bin/env python3
"""Run a batched, native-streaming Qwen3-ASR preview service."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import queue
import threading
import time
import unicodedata
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from huggingface_hub import snapshot_download
from qwen_asr import Qwen3ASRModel

SAMPLE_RATE = 16_000
TERMINAL_PUNCTUATION = frozenset("。！？!?；;")
_SPEECH_FRAME_SAMPLES = int(round(SAMPLE_RATE * 0.02))
_PRE_SPEECH_ROLL_SAMPLES = int(round(SAMPLE_RATE * 0.75))
_NORMAL_SPEECH_MIN_FRAMES = 6
_STRONG_SPEECH_MIN_FRAMES = 4


def _longest_run_above(
    values: list[float],
    threshold: float,
) -> tuple[int, int, float]:
    longest_run = 0
    current_run = 0
    active_count = 0
    current_min = 0.0
    current_max = 0.0
    longest_range = 0.0
    for value in values:
        if value > threshold:
            current_run += 1
            active_count += 1
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
    return longest_run, active_count, longest_range


def _speech_frame_metrics(
    audio: np.ndarray,
    *,
    rms_threshold: float,
) -> tuple[bool, float, float, int]:
    """Detect sustained speech energy without averaging short utterances away."""

    if not audio.size:
        return False, 0.0, 0.0, 0
    normalized = audio.astype(np.float32, copy=False).reshape(-1)
    chunk_rms = float(
        np.sqrt(np.mean(np.square(normalized), dtype=np.float64))
    )
    frame_rms: list[float] = []
    for start in range(0, normalized.size, _SPEECH_FRAME_SAMPLES):
        frame = normalized[start : start + _SPEECH_FRAME_SAMPLES]
        if frame.size < _SPEECH_FRAME_SAMPLES // 2:
            continue
        frame_rms.append(
            float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
        )
    if not frame_rms:
        frame_rms.append(chunk_rms)

    base_threshold = max(0.0, float(rms_threshold))
    noise_floor = float(np.percentile(np.asarray(frame_rms), 20))
    dynamic_ceiling = max(base_threshold * 1.75, base_threshold + 0.0045)
    dynamic_threshold = max(
        base_threshold,
        min(noise_floor * 1.6, dynamic_ceiling),
    )
    strong_threshold = max(base_threshold * 3.0, 0.018)
    normal_run, active_frame_count, normal_range = _longest_run_above(
        frame_rms,
        dynamic_threshold,
    )
    strong_run, _, _ = _longest_run_above(frame_rms, strong_threshold)
    normal_variation = max(0.0015, base_threshold * 0.25)
    very_short_strong_clip = (
        normalized.size < _SPEECH_FRAME_SAMPLES // 2
        and max(frame_rms) > strong_threshold
    )
    return (
        (
            normal_run >= _NORMAL_SPEECH_MIN_FRAMES
            and normal_range >= normal_variation
        )
        or strong_run >= _STRONG_SPEECH_MIN_FRAMES
        or very_short_strong_clip,
        chunk_rms,
        max(frame_rms),
        active_frame_count,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asr-model-path", default="Qwen/Qwen3-ASR-1.7B")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7118)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.21)
    parser.add_argument("--max-model-len", type=int, default=2048)
    parser.add_argument("--max-num-seqs", type=int, default=11)
    parser.add_argument("--max-sessions", type=int, default=16)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--soft-window-seconds", type=float, default=12.0)
    parser.add_argument("--window-seconds", type=float, default=20.0)
    parser.add_argument("--overlap-seconds", type=float, default=1.5)
    parser.add_argument("--silence-commit-ms", type=float, default=750.0)
    parser.add_argument("--force-refresh-ms", type=float, default=1000.0)
    parser.add_argument("--silence-rms-threshold", type=float, default=0.006)
    parser.add_argument("--batch-wait-ms", type=float, default=10.0)
    parser.add_argument("--session-ttl-seconds", type=float, default=600.0)
    parser.add_argument("--max-request-audio-seconds", type=float, default=20.0)
    parser.add_argument("--unfixed-chunk-num", type=int, default=2)
    parser.add_argument("--unfixed-token-num", type=int, default=64)
    parser.add_argument("--chunk-size-sec", type=float, default=1.0)
    parser.add_argument("--stream-context-chars", type=int, default=160)
    args = parser.parse_args()
    if args.max_model_len <= 0:
        parser.error("--max-model-len must be positive")
    if not 1 <= args.max_num_seqs <= 11:
        parser.error("--max-num-seqs must be between 1 and 11")
    if args.max_sessions < 0:
        parser.error("--max-sessions must be zero (unbounded) or positive")
    if args.max_new_tokens <= 0:
        parser.error("--max-new-tokens must be positive")
    if args.window_seconds < 5:
        parser.error("--window-seconds must be at least 5")
    if args.soft_window_seconds < 5 or args.soft_window_seconds > args.window_seconds:
        parser.error(
            "--soft-window-seconds must be at least 5 and no greater than --window-seconds"
        )
    if args.overlap_seconds < 0 or args.overlap_seconds >= args.window_seconds:
        parser.error("--overlap-seconds must be non-negative and smaller than --window-seconds")
    if args.silence_commit_ms <= 0 or args.force_refresh_ms <= 0:
        parser.error("silence and refresh intervals must be positive")
    if not 0 <= args.silence_rms_threshold <= 1:
        parser.error("--silence-rms-threshold must be between 0 and 1")
    if args.batch_wait_ms < 0:
        parser.error("--batch-wait-ms must be non-negative")
    if args.max_request_audio_seconds <= 0:
        parser.error("--max-request-audio-seconds must be positive")
    if args.unfixed_chunk_num < 0 or args.unfixed_token_num < 0:
        parser.error("stream rollback values must be non-negative")
    if not 0 <= args.stream_context_chars <= 2_000:
        parser.error("--stream-context-chars must be between 0 and 2000")
    if not 0.25 <= args.chunk_size_sec <= args.window_seconds:
        parser.error("--chunk-size-sec must be between 0.25 and --window-seconds")
    return args


def resolve_model_path(model_path: str) -> str:
    local_path = Path(model_path).expanduser()
    if local_path.exists():
        return str(local_path)
    # A Hugging Face snapshot directory may exist even when it only contains
    # metadata from an interrupted download.  Resolving it with
    # local_files_only=True therefore accepted partial snapshots and made vLLM
    # fail later with missing tokenizer/weight files.  An online snapshot
    # resolution verifies the repository manifest and completes any missing
    # files while still reusing a complete local cache.
    snapshot_path = snapshot_download(model_path)
    print(f"Using complete ASR model snapshot: {snapshot_path}", flush=True)
    return snapshot_path


def _join_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
        return f"{left} {right}"
    return f"{left}{right}"


def _fold_with_positions(value: str) -> tuple[str, list[int], str]:
    normalized = unicodedata.normalize("NFKC", value).strip()
    folded: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(normalized):
        for item in character.casefold():
            if item.isalnum():
                folded.append(item)
                positions.append(index + 1)
    return "".join(folded), positions, normalized


def _strip_repeated_history_prefix(
    stable: str,
    live: str,
    *,
    minimum_overlap: int = 4,
) -> str:
    stable_key, _, stable_text = _fold_with_positions(stable)
    live_key, live_positions, live_text = _fold_with_positions(live)
    if not stable_text or not live_text:
        return live_text
    consumed_key = 0
    while consumed_key < len(live_key):
        remaining = live_key[consumed_key:]
        matched = 0
        max_overlap = min(120, len(stable_key), len(remaining))
        for size in range(max_overlap, minimum_overlap - 1, -1):
            left = stable_key[-size:]
            right = remaining[:size]
            is_reliable_fuzzy_match = (
                size >= 6
                and SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.92
            )
            if left == right or is_reliable_fuzzy_match:
                matched = size
                break
        if matched <= 0:
            break
        consumed_key += matched

    if consumed_key <= 0:
        return live_text
    consumed_characters = live_positions[consumed_key - 1]
    return live_text[consumed_characters:]


def merge_transcripts(stable: str, live: str, *, minimum_overlap: int = 4) -> str:
    """Append a rolling transcript while removing repeated history prefixes."""

    _, _, stable_text = _fold_with_positions(stable)
    return _join_text(
        stable_text,
        _strip_repeated_history_prefix(
            stable_text,
            live,
            minimum_overlap=minimum_overlap,
        ),
    )


@dataclass(frozen=True)
class PreparedInference:
    audio: np.ndarray
    sequence: int
    commit_after: bool
    finish_stream: bool = False
    requires_decode: bool = True
    commit_reason: str | None = None
    segment_id: int = 1


@dataclass(frozen=True)
class InferenceResult:
    language: str
    text: str
    queue_ms: float
    inference_ms: float
    decoded_chunks: int = 1


@dataclass
class PreviewSession:
    window_samples: int
    overlap_samples: int
    silence_commit_ms: float
    force_refresh_ms: float
    silence_rms_threshold: float
    created_at: float
    last_seen: float
    requested_language: str | None = None
    streaming_state_factory: Callable[[str | None, str], Any] | None = None
    stream_context_chars: int = 160
    soft_window_samples: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    inference_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    audio_window: np.ndarray = field(default_factory=lambda: np.zeros((0,), dtype=np.float32))
    stable_text: str = ""
    unstable_text: str = ""
    language: str = ""
    sequence: int = 0
    revision: int = 0
    trailing_silence_ms: float = 0.0
    last_inference_at: float = 0.0
    silence_skip_count: int = 0
    segment_id: int = 1
    speech_detected: bool = False
    segment_speech_detected: bool = False
    speech_frame_count: int = 0
    last_chunk_rms: float = 0.0
    last_peak_rms: float = 0.0
    pre_speech_audio: np.ndarray = field(
        default_factory=lambda: np.zeros((0,), dtype=np.float32)
    )
    streaming_state: Any | None = field(default=None, init=False, repr=False)
    _stream_needs_priming: bool = field(default=False, init=False, repr=False)
    _stream_has_overlap_prefix: bool = field(default=False, init=False, repr=False)
    _last_decoded_speech_frame_count: int = field(default=0, init=False, repr=False)
    _last_decoded_sequence: int = field(default=0, init=False, repr=False)
    _last_applied_sequence: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.soft_window_samples is None:
            self.soft_window_samples = self.window_samples
        self.soft_window_samples = max(
            1,
            min(int(self.soft_window_samples), self.window_samples),
        )
        self._reset_streaming_state(prime_with_overlap=False)

    def _reset_streaming_state(self, *, prime_with_overlap: bool) -> None:
        if self.streaming_state_factory is None:
            self.streaming_state = None
            self._stream_needs_priming = False
            self._stream_has_overlap_prefix = False
            return
        context = (
            self.stable_text[-self.stream_context_chars :].strip()
            if prime_with_overlap and self.stream_context_chars
            else ""
        )
        self.streaming_state = self.streaming_state_factory(
            self.requested_language,
            context,
        )
        self._stream_needs_priming = prime_with_overlap and bool(self.audio_window.size)
        self._stream_has_overlap_prefix = self._stream_needs_priming

    def _retain_overlap(self) -> None:
        if self.overlap_samples:
            self.audio_window = self.audio_window[-self.overlap_samples :].copy()
        else:
            self.audio_window = np.zeros((0,), dtype=np.float32)

    def _commit_window(self, *, retain_overlap: bool) -> None:
        if self._stream_has_overlap_prefix:
            self.stable_text = merge_transcripts(self.stable_text, self.unstable_text)
        else:
            self.stable_text = _join_text(self.stable_text, self.unstable_text)
        self.unstable_text = ""
        if retain_overlap:
            self._retain_overlap()
        else:
            self.audio_window = np.zeros((0,), dtype=np.float32)
        self.trailing_silence_ms = 0.0
        self.segment_id += 1
        self.segment_speech_detected = retain_overlap and bool(self.audio_window.size)
        self.pre_speech_audio = np.zeros((0,), dtype=np.float32)
        self._reset_streaming_state(prime_with_overlap=retain_overlap)

    def _active_text_ends_at_boundary(self) -> bool:
        text = self.unstable_text.rstrip()
        return bool(text) and text[-1] in TERMINAL_PUNCTUATION

    def _claim_prepared(self, prepared: PreparedInference) -> bool:
        """Consume an inference sequence exactly once."""

        if (
            prepared.sequence != self.sequence
            or prepared.sequence <= self._last_applied_sequence
        ):
            return False
        self._last_applied_sequence = prepared.sequence
        return True

    @staticmethod
    def _valid_audio_buffer(value: Any) -> np.ndarray | None:
        """Return a valid one-dimensional PCM buffer, or fail open."""

        try:
            audio = np.asarray(value, dtype=np.float32)
        except (TypeError, ValueError):
            return None
        if audio.ndim != 1 or not np.isfinite(audio).all():
            return None
        return audio

    def _can_seal_without_decode(
        self,
        additional_audio: np.ndarray | None = None,
    ) -> bool:
        """Whether every speech sample is already owned by an applied decode.

        A seal is deliberately conservative: overlap, an unknown streaming-state
        contract, or even a short active tail forces a real model decode. Only
        buffers that contain no active audio frames may be committed directly.
        """

        if (
            self._stream_has_overlap_prefix
            or self._last_decoded_sequence <= 0
            or self.speech_frame_count != self._last_decoded_speech_frame_count
        ):
            return False

        pending_parts: list[np.ndarray] = []
        if self.streaming_state is not None:
            if not hasattr(self.streaming_state, "buffer"):
                return False
            stream_buffer = self._valid_audio_buffer(self.streaming_state.buffer)
            if stream_buffer is None:
                return False
            if stream_buffer.size:
                pending_parts.append(stream_buffer)

        if additional_audio is not None:
            extra = self._valid_audio_buffer(additional_audio)
            if extra is None:
                return False
            if extra.size:
                pending_parts.append(extra)

        if not pending_parts:
            return True
        pending_audio = (
            pending_parts[0]
            if len(pending_parts) == 1
            else np.concatenate(pending_parts)
        )
        _, _, _, active_frames = _speech_frame_metrics(
            pending_audio,
            rms_threshold=self.silence_rms_threshold,
        )
        return active_frames == 0

    def _append_stream_buffer(self, audio: np.ndarray) -> None:
        state = self.streaming_state
        if state is None or not audio.size:
            return
        state.buffer = np.concatenate(
            (state.buffer, audio.astype(np.float32, copy=False))
        )

    def _buffer_pre_speech(self, audio: np.ndarray) -> None:
        if not audio.size:
            return
        self.pre_speech_audio = np.concatenate(
            (self.pre_speech_audio, audio.astype(np.float32, copy=False))
        )
        limit = min(self.window_samples, _PRE_SPEECH_ROLL_SAMPLES)
        if self.pre_speech_audio.size > limit:
            self.pre_speech_audio = self.pre_speech_audio[-limit:].copy()

    def prepare_inference(
        self,
        audio: np.ndarray,
        *,
        now: float | None = None,
    ) -> PreparedInference | None:
        now = time.monotonic() if now is None else now
        self.last_seen = time.time()
        duration_ms = audio.size * 1000.0 / SAMPLE_RATE
        detection_audio = audio
        if not self.segment_speech_detected and self.pre_speech_audio.size:
            detection_audio = np.concatenate((self.pre_speech_audio, audio))
        has_speech, chunk_rms, peak_rms, active_frames = _speech_frame_metrics(
            detection_audio,
            rms_threshold=self.silence_rms_threshold,
        )
        self.last_chunk_rms = chunk_rms
        self.last_peak_rms = peak_rms
        if self.unstable_text and not self.segment_speech_detected:
            self.segment_speech_detected = True
        if not has_speech and not self.segment_speech_detected:
            self._buffer_pre_speech(audio)
            self.trailing_silence_ms += duration_ms
            self.silence_skip_count += 1
            return None
        if has_speech and not self.segment_speech_detected:
            audio = detection_audio
            self.pre_speech_audio = np.zeros((0,), dtype=np.float32)

        if (
            self.audio_window.size
            and self.audio_window.size + audio.size > self.window_samples
        ):
            self._commit_window(retain_overlap=True)
        self.audio_window = np.concatenate((self.audio_window, audio))
        if self.audio_window.size > self.window_samples:
            self.audio_window = self.audio_window[-self.window_samples :]

        if self.streaming_state is not None:
            if self._stream_needs_priming:
                inference_audio = self.audio_window.copy()
                self._stream_needs_priming = False
            else:
                inference_audio = audio
        else:
            # Preserve the full-window contract for non-streaming test doubles.
            inference_audio = self.audio_window

        silent = not has_speech
        if has_speech:
            self.speech_detected = True
            self.segment_speech_detected = True
            self.speech_frame_count += active_frames
            self.trailing_silence_ms = 0.0
        else:
            self.trailing_silence_ms += duration_ms
        silence_commit_due = (
            silent
            and self.segment_speech_detected
            and self.trailing_silence_ms >= self.silence_commit_ms
        )
        soft_boundary_due = (
            self.audio_window.size >= int(self.soft_window_samples or self.window_samples)
            and bool(self.unstable_text)
            and (silent or self._active_text_ends_at_boundary())
        )
        commit_after = silence_commit_due or soft_boundary_due
        commit_reason = (
            "silence"
            if silence_commit_due
            else "soft_boundary"
            if soft_boundary_due
            else None
        )
        if silent and not commit_after:
            self.silence_skip_count += 1
            self._append_stream_buffer(inference_audio)
            return None

        requires_decode = not (
            commit_after and self._can_seal_without_decode(audio)
        )
        self.sequence += 1
        self.last_inference_at = now
        return PreparedInference(
            audio=inference_audio,
            sequence=self.sequence,
            commit_after=commit_after,
            finish_stream=(
                commit_after
                and requires_decode
                and self.streaming_state is not None
            ),
            requires_decode=requires_decode,
            commit_reason=commit_reason,
            segment_id=self.segment_id,
        )

    def prepare_finish(self) -> PreparedInference:
        requires_decode = not self._can_seal_without_decode()
        self.sequence += 1
        return PreparedInference(
            audio=np.zeros((0,), dtype=np.float32),
            sequence=self.sequence,
            commit_after=True,
            finish_stream=requires_decode and self.streaming_state is not None,
            requires_decode=requires_decode,
            commit_reason="stop",
            segment_id=self.segment_id,
        )

    def seal(self, prepared: PreparedInference) -> dict[str, Any]:
        """Finalize already-decoded text without replaying audio through the model."""

        correction_state = "finalized" if prepared.commit_after else "live"
        response_segment_id = prepared.segment_id
        if self._claim_prepared(prepared) and prepared.commit_after:
            self.revision += 1
            self._commit_window(retain_overlap=False)
        return self.snapshot(
            queue_ms=0.0,
            inference_ms=0.0,
            correction_state=correction_state,
            segment_id=response_segment_id,
        )

    def apply_result(
        self,
        prepared: PreparedInference,
        result: InferenceResult,
    ) -> dict[str, Any]:
        if not prepared.requires_decode:
            return self.seal(prepared)
        correction_state = "live"
        response_segment_id = prepared.segment_id
        if self._claim_prepared(prepared):
            decoded_text = result.decoded_chunks > 0
            if result.decoded_chunks > 0:
                self.language = result.language.strip()
                candidate = unicodedata.normalize("NFKC", result.text).strip()
                candidate = (
                    _strip_repeated_history_prefix(self.stable_text, candidate)
                    if self._stream_has_overlap_prefix
                    else candidate
                )
                self.unstable_text = candidate
                self.revision += 1
            if prepared.commit_after:
                if result.decoded_chunks <= 0:
                    self.revision += 1
                self._commit_window(retain_overlap=False)
                correction_state = "finalized"
            if decoded_text:
                self._last_decoded_speech_frame_count = self.speech_frame_count
                self._last_decoded_sequence = prepared.sequence
        return self.snapshot(
            queue_ms=result.queue_ms,
            inference_ms=result.inference_ms,
            correction_state=correction_state,
            segment_id=response_segment_id,
        )

    def snapshot(
        self,
        *,
        silence_skipped: bool = False,
        queue_ms: float | None = None,
        inference_ms: float | None = None,
        correction_state: str = "live",
        segment_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "language": self.language,
            "text": _join_text(self.stable_text, self.unstable_text),
            "stable_text": self.stable_text,
            "unstable_text": self.unstable_text,
            "revision": self.revision,
            "segment_id": self.segment_id if segment_id is None else segment_id,
            "correction_state": correction_state,
            "window_seconds": round(self.audio_window.size / SAMPLE_RATE, 3),
            "silence_skipped": silence_skipped,
            "silence_skip_count": self.silence_skip_count,
            "speech_detected": self.speech_detected,
            "speech_frame_count": self.speech_frame_count,
            "chunk_rms": round(self.last_chunk_rms, 6),
            "peak_rms": round(self.last_peak_rms, 6),
        }
        if queue_ms is not None:
            payload["queue_ms"] = round(queue_ms, 3)
        if inference_ms is not None:
            payload["inference_ms"] = round(inference_ms, 3)
        return payload


@dataclass
class InferenceRequest:
    audio: np.ndarray
    future: Future[InferenceResult]
    queued_at: float
    language: str | None = None
    streaming_state: Any | None = None
    finish_stream: bool = False


def _percentile(values: deque[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


class BatchInferenceScheduler:
    """Share one model between native streaming previews and final batches."""

    _LOW_PRIORITY_GRACE_SECONDS = 0.5

    def __init__(
        self,
        model: Any,
        *,
        max_batch_size: int,
        batch_wait_ms: float,
        stream_unfixed_chunk_num: int = 2,
        stream_unfixed_token_num: int = 5,
        stream_chunk_size_sec: float = 1.0,
        parse_stream_output: Callable[[str, str | None], tuple[str, str]] | None = None,
    ) -> None:
        self.model = model
        self.max_batch_size = max_batch_size
        self.batch_wait_seconds = batch_wait_ms / 1000.0
        self.stream_unfixed_chunk_num = stream_unfixed_chunk_num
        self.stream_unfixed_token_num = stream_unfixed_token_num
        self.stream_chunk_size_sec = stream_chunk_size_sec
        self._parse_stream_output = parse_stream_output
        self.requests: queue.PriorityQueue[
            tuple[float, int, InferenceRequest | None]
        ] = queue.PriorityQueue()
        self._request_sequence = itertools.count()
        self.queue_latencies_ms: deque[float] = deque(maxlen=512)
        self.inference_latencies_ms: deque[float] = deque(maxlen=512)
        self.total_batches = 0
        self.last_batch_size = 0
        self.last_stream_batch_size = 0
        self.stream_decode_batches = 0
        self.stream_decoded_chunks = 0
        self.stream_buffered_requests = 0
        self._metrics_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run,
            name="qwen3-asr-unified-inference",
            daemon=True,
        )
        self._worker.start()

    def create_streaming_state(
        self,
        language: str | None = None,
        context: str = "",
    ) -> Any:
        return self.model.init_streaming_state(
            context=context,
            language=language,
            unfixed_chunk_num=self.stream_unfixed_chunk_num,
            unfixed_token_num=self.stream_unfixed_token_num,
            chunk_size_sec=self.stream_chunk_size_sec,
        )

    def _queue_request(self, request: InferenceRequest, *, priority: int) -> None:
        priority_delay = max(0, int(priority)) * self._LOW_PRIORITY_GRACE_SECONDS
        self.requests.put(
            (
                request.queued_at + priority_delay,
                next(self._request_sequence),
                request,
            )
        )

    async def submit(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        priority: int = 0,
    ) -> InferenceResult:
        future: Future[InferenceResult] = Future()
        request = InferenceRequest(
            audio=audio,
            future=future,
            queued_at=time.monotonic(),
            language=language,
        )
        self._queue_request(request, priority=priority)
        return await asyncio.wrap_future(future)

    async def submit_stream(
        self,
        state: Any,
        audio: np.ndarray,
        *,
        finish: bool = False,
        priority: int = 0,
    ) -> InferenceResult:
        future: Future[InferenceResult] = Future()
        request = InferenceRequest(
            audio=audio,
            future=future,
            queued_at=time.monotonic(),
            streaming_state=state,
            finish_stream=finish,
        )
        self._queue_request(request, priority=priority)
        return await asyncio.wrap_future(future)

    def close(self) -> None:
        self.requests.put((float("-inf"), next(self._request_sequence), None))

    def _collect_batch(self, first: InferenceRequest) -> list[InferenceRequest]:
        batch = [first]
        deadline = time.monotonic() + self.batch_wait_seconds
        while len(batch) < self.max_batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                _, _, item = self.requests.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                self.close()
                break
            batch.append(item)
        return batch

    def _parse_native_output(
        self,
        raw: str,
        language: str | None,
    ) -> tuple[str, str]:
        parser = self._parse_stream_output
        if parser is None:
            from qwen_asr.inference.qwen3_asr import parse_asr_output

            parser = parse_asr_output
            self._parse_stream_output = parser
        return parser(raw, language)

    def _stream_prefix(self, state: Any) -> str:
        if state.chunk_id < state.unfixed_chunk_num:
            return ""
        token_ids = self.model.processor.tokenizer.encode(state._raw_decoded)
        end_index = max(1, len(token_ids) - int(state.unfixed_token_num))
        return self.model.processor.tokenizer.decode(token_ids[:end_index])

    def _prepare_stream_step(
        self,
        item: InferenceRequest,
    ) -> tuple[Any, dict[str, Any], str] | None:
        state = item.streaming_state
        if state is None:
            raise RuntimeError("streaming request is missing its state")
        if state.buffer.shape[0] >= state.chunk_size_samples:
            chunk = state.buffer[: state.chunk_size_samples]
            state.buffer = state.buffer[state.chunk_size_samples :]
        elif item.finish_stream and state.buffer.shape[0] > 0:
            chunk = state.buffer
            state.buffer = np.zeros((0,), dtype=np.float32)
        else:
            return None

        if state.audio_accum.shape[0] == 0:
            state.audio_accum = chunk
        else:
            state.audio_accum = np.concatenate((state.audio_accum, chunk))
        prefix = self._stream_prefix(state)
        prompt = state.prompt_raw + prefix
        model_input = {
            "prompt": prompt,
            "multi_modal_data": {"audio": [state.audio_accum]},
        }
        return state, model_input, prefix

    def _process_stream_batch(self, items: list[InferenceRequest]) -> float:
        started_at = time.monotonic()
        queue_times = [(started_at - item.queued_at) * 1000.0 for item in items]
        decoded_counts = {id(item): 0 for item in items}
        for item in items:
            state = item.streaming_state
            if state is None:
                raise RuntimeError("streaming request is missing its state")
            audio = np.asarray(item.audio)
            if audio.ndim != 1:
                audio = audio.reshape(-1)
            if audio.dtype == np.int16:
                audio = audio.astype(np.float32) / 32768.0
            else:
                audio = audio.astype(np.float32, copy=False)
            if audio.size:
                state.buffer = np.concatenate((state.buffer, audio))

        decode_batches = 0
        while True:
            ready: list[tuple[InferenceRequest, Any, str]] = []
            model_inputs: list[dict[str, Any]] = []
            for item in items:
                prepared = self._prepare_stream_step(item)
                if prepared is None:
                    continue
                state, model_input, prefix = prepared
                ready.append((item, state, prefix))
                model_inputs.append(model_input)
            if not ready:
                break

            outputs = self.model.model.generate(
                model_inputs,
                sampling_params=self.model.sampling_params,
                use_tqdm=False,
            )
            if len(outputs) != len(ready):
                raise RuntimeError("Qwen3-ASR returned an unexpected streaming batch size")
            for (item, state, prefix), output in zip(ready, outputs):
                generated = str(output.outputs[0].text or "")
                state._raw_decoded = prefix + generated
                language, transcript = self._parse_native_output(
                    state._raw_decoded,
                    state.force_language,
                )
                state.language = language
                state.text = transcript
                state.chunk_id += 1
                decoded_counts[id(item)] += 1
            decode_batches += 1

        inference_ms = (time.monotonic() - started_at) * 1000.0
        for item, queue_ms in zip(items, queue_times):
            if item.future.done():
                continue
            state = item.streaming_state
            item.future.set_result(
                InferenceResult(
                    language=str(state.language or ""),
                    text=str(state.text or ""),
                    queue_ms=queue_ms,
                    inference_ms=inference_ms,
                    decoded_chunks=decoded_counts[id(item)],
                )
            )
        with self._metrics_lock:
            self.stream_decode_batches += decode_batches
            self.stream_decoded_chunks += sum(decoded_counts.values())
            self.stream_buffered_requests += sum(
                count == 0 for count in decoded_counts.values()
            )
        return inference_ms

    def _process_full_batch(self, items: list[InferenceRequest]) -> float:
        started_at = time.monotonic()
        queue_times = [(started_at - item.queued_at) * 1000.0 for item in items]
        languages = [item.language for item in items]
        transcribe_kwargs: dict[str, Any] = {
            "audio": [(item.audio, SAMPLE_RATE) for item in items],
        }
        if any(languages):
            transcribe_kwargs["language"] = languages
        results = self.model.transcribe(**transcribe_kwargs)
        if len(results) != len(items):
            raise RuntimeError("Qwen3-ASR returned an unexpected batch size")
        inference_ms = (time.monotonic() - started_at) * 1000.0
        for item, result, queue_ms in zip(items, results, queue_times):
            if item.future.done():
                continue
            item.future.set_result(
                InferenceResult(
                    language=str(getattr(result, "language", "") or ""),
                    text=str(getattr(result, "text", "") or ""),
                    queue_ms=queue_ms,
                    inference_ms=inference_ms,
                )
            )
        return inference_ms

    @staticmethod
    def _fail(items: list[InferenceRequest], exc: BaseException) -> None:
        for item in items:
            if not item.future.done():
                item.future.set_exception(exc)

    def _run(self) -> None:
        while True:
            _, _, first = self.requests.get()
            if first is None:
                return
            batch = self._collect_batch(first)
            batch_started_at = time.monotonic()
            queue_times = [
                (batch_started_at - item.queued_at) * 1000.0 for item in batch
            ]
            stream_items = [item for item in batch if item.streaming_state is not None]
            full_items = [item for item in batch if item.streaming_state is None]
            inference_ms = 0.0
            if stream_items:
                try:
                    inference_ms += self._process_stream_batch(stream_items)
                except BaseException as exc:
                    self._fail(stream_items, exc)
            if full_items:
                try:
                    inference_ms += self._process_full_batch(full_items)
                except BaseException as exc:
                    self._fail(full_items, exc)
            with self._metrics_lock:
                self.total_batches += 1
                self.last_batch_size = len(batch)
                self.last_stream_batch_size = len(stream_items)
                self.queue_latencies_ms.extend(queue_times)
                self.inference_latencies_ms.append(inference_ms)
            print(
                "ASR batch complete "
                f"size={len(batch)} stream_size={len(stream_items)} "
                f"queue_p95_ms={_percentile(deque(queue_times), 0.95):.1f} "
                f"inference_ms={inference_ms:.1f}",
                flush=True,
            )

    def health(self) -> dict[str, Any]:
        with self._metrics_lock:
            return {
                "queue_depth": self.requests.qsize(),
                "last_batch_size": self.last_batch_size,
                "last_stream_batch_size": self.last_stream_batch_size,
                "total_batches": self.total_batches,
                "stream_decode_batches": self.stream_decode_batches,
                "stream_decoded_chunks": self.stream_decoded_chunks,
                "stream_buffered_requests": self.stream_buffered_requests,
                "queue_p50_ms": round(_percentile(self.queue_latencies_ms, 0.50), 1),
                "queue_p95_ms": round(_percentile(self.queue_latencies_ms, 0.95), 1),
                "inference_p50_ms": round(_percentile(self.inference_latencies_ms, 0.50), 1),
                "inference_p95_ms": round(_percentile(self.inference_latencies_ms, 0.95), 1),
            }


class SessionRegistry:
    def __init__(
        self,
        *,
        max_sessions: int,
        window_seconds: float,
        overlap_seconds: float,
        silence_commit_ms: float,
        force_refresh_ms: float,
        silence_rms_threshold: float,
        ttl_seconds: float,
        streaming_state_factory: Callable[[str | None, str], Any] | None = None,
        soft_window_seconds: float | None = None,
        stream_context_chars: int = 160,
    ) -> None:
        self.max_sessions = max_sessions
        self.window_samples = int(round(window_seconds * SAMPLE_RATE))
        self.soft_window_samples = int(
            round((soft_window_seconds or window_seconds) * SAMPLE_RATE)
        )
        self.overlap_samples = int(round(overlap_seconds * SAMPLE_RATE))
        self.silence_commit_ms = silence_commit_ms
        self.force_refresh_ms = force_refresh_ms
        self.silence_rms_threshold = silence_rms_threshold
        self.ttl_seconds = ttl_seconds
        self.streaming_state_factory = streaming_state_factory
        self.stream_context_chars = max(0, int(stream_context_chars))
        self.sessions: dict[str, PreviewSession] = {}
        self.lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        expired = [
            session_id
            for session_id, session in self.sessions.items()
            if (
                now - session.last_seen > self.ttl_seconds
                and not session.inference_lock.locked()
            )
        ]
        for session_id in expired:
            self.sessions.pop(session_id, None)

    def create(self, *, language: str | None = None) -> tuple[str, PreviewSession] | None:
        now = time.time()
        with self.lock:
            self._prune_locked(now)
            if self.max_sessions > 0 and len(self.sessions) >= self.max_sessions:
                return None
            session_id = uuid.uuid4().hex
            session = PreviewSession(
                window_samples=self.window_samples,
                overlap_samples=self.overlap_samples,
                silence_commit_ms=self.silence_commit_ms,
                force_refresh_ms=self.force_refresh_ms,
                silence_rms_threshold=self.silence_rms_threshold,
                created_at=now,
                last_seen=now,
                requested_language=normalize_language(language),
                streaming_state_factory=self.streaming_state_factory,
                stream_context_chars=self.stream_context_chars,
                soft_window_samples=self.soft_window_samples,
            )
            self.sessions[session_id] = session
            return session_id, session

    def get(self, session_id: str) -> PreviewSession | None:
        now = time.time()
        with self.lock:
            self._prune_locked(now)
            session = self.sessions.get(session_id)
            if session is not None:
                session.last_seen = now
            return session

    def remove(self, session_id: str) -> PreviewSession | None:
        with self.lock:
            return self.sessions.pop(session_id, None)

    def count(self) -> int:
        with self.lock:
            self._prune_locked(time.time())
            return len(self.sessions)

    def silence_skips(self) -> int:
        with self.lock:
            return sum(session.silence_skip_count for session in self.sessions.values())


def normalize_language(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    normalized_key = normalized.casefold().replace("_", "-")
    if normalized_key in {"auto", "automatic", "none"}:
        return None
    aliases = {
        "zh": "Chinese",
        "zh-cn": "Chinese",
        "zh-hans": "Chinese",
        "en": "English",
        "en-us": "English",
        "en-gb": "English",
        "de": "German",
        "de-de": "German",
        "de-at": "German",
        "de-ch": "German",
        "ja": "Japanese",
        "ja-jp": "Japanese",
    }
    return aliases.get(normalized_key, normalized)

def create_app(
    registry: SessionRegistry,
    scheduler: BatchInferenceScheduler,
    *,
    max_request_audio_seconds: float,
) -> FastAPI:
    app = FastAPI(title="Qwen3-ASR realtime preview", docs_url=None, redoc_url=None)
    max_request_samples = int(round(max_request_audio_seconds * SAMPLE_RATE))

    @app.get("/")
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "active_sessions": registry.count(),
            "max_sessions": registry.max_sessions,
            "max_batch_size": getattr(scheduler, "max_batch_size", 1),
            "streaming_mode": "native_incremental_batch",
            "stream_chunk_seconds": float(
                getattr(scheduler, "stream_chunk_size_sec", 0.0)
            ),
            "admission_mode": "bounded" if registry.max_sessions > 0 else "unbounded",
            "window_seconds": registry.window_samples / SAMPLE_RATE,
            "soft_window_seconds": registry.soft_window_samples / SAMPLE_RATE,
            "overlap_seconds": registry.overlap_samples / SAMPLE_RATE,
            "stream_context_chars": registry.stream_context_chars,
            "silence_skip_count": registry.silence_skips(),
            **scheduler.health(),
        }

    @app.post("/api/start")
    async def api_start(language: str | None = None) -> dict[str, Any]:
        created = registry.create(language=language)
        if created is None:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "preview capacity exceeded",
                    "code": "capacity_exceeded",
                    "recoverable": True,
                },
            )
        session_id, session = created
        return {
            "session_id": session_id,
            "audio_format": "pcm_s16le",
            "language": session.requested_language or "",
            "streaming_mode": "native_incremental_batch",
            "chunk_size_seconds": float(
                getattr(scheduler, "stream_chunk_size_sec", 0.0)
            ),
        }

    @app.post("/api/chunk")
    async def api_chunk(session_id: str, request: Request) -> dict[str, Any]:
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=400, detail="invalid session_id")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"audio/pcm", "audio/l16", "application/octet-stream"}:
            raise HTTPException(
                status_code=400,
                detail="expect PCM16 little-endian or legacy float32 audio",
            )
        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="audio chunk is empty")
        if content_type in {"audio/pcm", "audio/l16"}:
            if len(raw) % 2 != 0:
                raise HTTPException(
                    status_code=400,
                    detail="PCM16 bytes length not multiple of 2",
                )
            audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        else:
            if len(raw) % 4 != 0:
                raise HTTPException(
                    status_code=400,
                    detail="float32 bytes length not multiple of 4",
                )
            audio = np.frombuffer(raw, dtype=np.float32).reshape(-1)
        if audio.size > max_request_samples:
            raise HTTPException(status_code=413, detail="audio chunk is too large")
        if not np.isfinite(audio).all():
            raise HTTPException(status_code=400, detail="audio chunk contains non-finite samples")

        async with session.inference_lock:
            with session.lock:
                prepared = session.prepare_inference(audio)
                if prepared is None:
                    return session.snapshot(silence_skipped=True)
                streaming_state = session.streaming_state
                if not prepared.requires_decode:
                    return session.seal(prepared)
            try:
                if streaming_state is not None and hasattr(scheduler, "submit_stream"):
                    result = await scheduler.submit_stream(
                        streaming_state,
                        prepared.audio,
                        finish=prepared.finish_stream,
                        priority=0,
                    )
                else:
                    result = await scheduler.submit(
                        prepared.audio,
                        language=session.requested_language,
                        priority=0,
                    )
            except Exception as exc:
                raise HTTPException(status_code=500, detail="local ASR inference failed") from exc
            with session.lock:
                return session.apply_result(prepared, result)

    @app.post("/api/transcribe")
    async def api_transcribe(
        request: Request,
        language: str | None = None,
    ) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"audio/pcm", "audio/l16"}:
            raise HTTPException(status_code=400, detail="expect PCM16 little-endian audio")
        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="audio is empty")
        if len(raw) % 2 != 0:
            raise HTTPException(status_code=400, detail="PCM16 bytes length not multiple of 2")
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size > max_request_samples:
            raise HTTPException(status_code=413, detail="audio is too long")
        try:
            result = await scheduler.submit(
                audio,
                language=normalize_language(language),
                priority=1,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail="local ASR inference failed") from exc
        return {
            "language": result.language,
            "text": result.text,
            "queue_ms": round(result.queue_ms, 3),
            "inference_ms": round(result.inference_ms, 3),
            "audio_seconds": round(audio.size / SAMPLE_RATE, 3),
        }

    @app.post("/api/finish")
    async def api_finish(session_id: str) -> dict[str, Any]:
        session = registry.remove(session_id)
        if session is None:
            raise HTTPException(status_code=400, detail="invalid session_id")
        async with session.inference_lock:
            with session.lock:
                if (
                    not session.speech_detected
                    and not session.stable_text
                    and not session.unstable_text
                ):
                    return session.snapshot(correction_state="finalized")
                prepared = session.prepare_finish()
                if not prepared.requires_decode:
                    return session.seal(prepared)
                streaming_state = session.streaming_state
                if streaming_state is None or not hasattr(scheduler, "submit_stream"):
                    return session.snapshot()
            try:
                result = await scheduler.submit_stream(
                    streaming_state,
                    prepared.audio,
                    finish=prepared.finish_stream,
                    priority=0,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail="local ASR stream finalization failed",
                ) from exc
            with session.lock:
                return session.apply_result(prepared, result)

    @app.post("/api/cancel")
    async def api_cancel(session_id: str) -> dict[str, bool]:
        return {"cancelled": registry.remove(session_id) is not None}

    return app


def warmup_model(model: Any, *, audio_seconds: float = 1.0) -> float:
    sample_count = max(1, int(round(audio_seconds * SAMPLE_RATE)))
    audio = np.zeros((sample_count,), dtype=np.float32)
    started_at = time.perf_counter()
    results = model.transcribe(audio=[(audio, SAMPLE_RATE)])
    if len(results) != 1:
        raise RuntimeError("Qwen3-ASR warmup returned an unexpected result count")
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    print(f"Qwen3-ASR inference warmup complete in {elapsed_ms:.1f}ms", flush=True)
    return elapsed_ms

def main() -> None:
    args = parse_args()
    model = Qwen3ASRModel.LLM(
        model=resolve_model_path(args.asr_model_path),
        dtype=torch.bfloat16,
        hf_overrides={"dtype": torch.bfloat16},
        gpu_memory_utilization=args.gpu_memory_utilization,
        max_model_len=args.max_model_len,
        max_num_seqs=args.max_num_seqs,
        max_inference_batch_size=args.max_num_seqs,
        max_new_tokens=args.max_new_tokens,
    )
    warmup_model(model)
    scheduler = BatchInferenceScheduler(
        model,
        max_batch_size=args.max_num_seqs,
        batch_wait_ms=args.batch_wait_ms,
        stream_unfixed_chunk_num=args.unfixed_chunk_num,
        stream_unfixed_token_num=args.unfixed_token_num,
        stream_chunk_size_sec=args.chunk_size_sec,
    )
    scheduler.create_streaming_state(None)
    registry = SessionRegistry(
        max_sessions=args.max_sessions,
        window_seconds=args.window_seconds,
        soft_window_seconds=args.soft_window_seconds,
        overlap_seconds=args.overlap_seconds,
        silence_commit_ms=args.silence_commit_ms,
        force_refresh_ms=args.force_refresh_ms,
        silence_rms_threshold=args.silence_rms_threshold,
        ttl_seconds=args.session_ttl_seconds,
        streaming_state_factory=scheduler.create_streaming_state,
        stream_context_chars=args.stream_context_chars,
    )
    app = create_app(
        registry,
        scheduler,
        max_request_audio_seconds=args.max_request_audio_seconds,
    )
    print("Native-streaming batched Qwen3-ASR model loaded.", flush=True)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()

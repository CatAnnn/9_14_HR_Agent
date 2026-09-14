from __future__ import annotations

import math
from pathlib import Path
import struct
import wave

import pytest

from scripts.register_fish_voice import inspect_reference_wav, voices_url


def _write_wav(path: Path, *, duration_seconds: int = 5) -> None:
    sample_rate = 24_000
    frames = bytearray()
    for index in range(sample_rate * duration_seconds):
        # A deterministic speech-like test signal (not silence): two modest
        # harmonics avoid making the quality gate depend on a single sample.
        phase = 2 * math.pi * index / sample_rate
        sample = int(8_000 * math.sin(2 * phase) + 2_000 * math.sin(5 * phase))
        frames.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frames)


def test_reference_voice_wav_quality_gate_and_endpoint_resolution(tmp_path: Path) -> None:
    audio_path = tmp_path / "employee-natural.wav"
    _write_wav(audio_path)

    info = inspect_reference_wav(audio_path)
    assert info["bytes"] == audio_path.stat().st_size
    assert info["sample_rate"] == 24_000
    assert info["duration_seconds"] == 5.0
    assert info["rms_dbfs"] < 0
    assert info["clipping_ratio"] == 0
    # The energy proxy can mark a few harmonic frames inactive when the two
    # deterministic test tones briefly cancel; it is still clearly active
    # speech-like material and comfortably above the registration threshold.
    assert info["active_voice_ratio"] >= 0.8
    assert voices_url("http://fish:8091") == "http://fish:8091/v1/audio/voices"
    assert (
        voices_url("http://fish:8091/v1/audio/speech")
        == "http://fish:8091/v1/audio/voices"
    )


def test_reference_voice_rejects_short_or_lossy_input(tmp_path: Path) -> None:
    short_path = tmp_path / "short.wav"
    lossy_path = tmp_path / "voice.mp3"
    _write_wav(short_path, duration_seconds=1)
    lossy_path.write_bytes(b"not-a-wave")

    with pytest.raises(ValueError, match="between 5 and 30 seconds"):
        inspect_reference_wav(short_path)
    with pytest.raises(ValueError, match="PCM WAV"):
        inspect_reference_wav(lossy_path)


def test_reference_voice_rejects_all_zero_audio(tmp_path: Path) -> None:
    silent_path = tmp_path / "silent.wav"
    sample_rate = 24_000
    with wave.open(str(silent_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * sample_rate * 5)

    with pytest.raises(ValueError, match="silent or too quiet"):
        inspect_reference_wav(silent_path)


def test_reference_voice_rejects_excessive_clipping(tmp_path: Path) -> None:
    clipped_path = tmp_path / "clipped.wav"
    sample_rate = 24_000
    samples = [32767 if index % 2 else -32768 for index in range(sample_rate * 5)]
    with wave.open(str(clipped_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    with pytest.raises(ValueError, match="excessively clipped"):
        inspect_reference_wav(clipped_path)


def test_reference_voice_rejects_mostly_silent_audio(tmp_path: Path) -> None:
    mostly_silent_path = tmp_path / "mostly-silent.wav"
    sample_rate = 24_000
    samples = [0] * (sample_rate * 5)
    # Keep only 250 ms of audible signal and leave the remainder silent.
    for index in range(sample_rate // 4):
        samples[index] = int(8_000 * math.sin(2 * math.pi * 220 * index / sample_rate))
    with wave.open(str(mostly_silent_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    with pytest.raises(ValueError, match="too little active speech"):
        inspect_reference_wav(mostly_silent_path)

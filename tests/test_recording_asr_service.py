from __future__ import annotations

import os
import stat
import time
from collections import defaultdict
from types import SimpleNamespace

import pytest

from backend.services.recording_asr_service import (
    AsrFinalTranscriptionError,
    LocalAsrTranscriber,
    PcmRecording,
    RemoteAsrTranscriber,
    cleanup_stale_recordings,
    prepare_recording_storage,
    segment_pcm_ranges,
)


def _settings(tmp_path, **overrides):
    values = {
        "asr_enabled": True,
        "asr_recording_tmp_dir": tmp_path / "asr",
        "asr_recording_min_free_bytes": 0,
        "asr_recording_orphan_ttl_seconds": 3600,
        "asr_sample_rate": 16_000,
        "asr_realtime_enabled": True,
        "asr_local_streaming_url": "http://qwen3-asr:7118",
        "asr_local_stream_window_seconds": 12,
        "asr_local_final_segment_seconds": 10,
        "asr_local_final_per_recording_concurrency": 4,
        "asr_local_final_timeout_seconds": 20,
        "asr_local_final_min_preview_ratio": 0.45,
        "asr_local_silence_rms_threshold": 0.006,
        "asr_final_segment_seconds": 300,
        "asr_final_per_recording_concurrency": 2,
        "asr_final_max_concurrency": 8,
        "asr_http_url": "https://model.example/audio/transcriptions",
        "asr_http_model": "qwen3-asr-flash",
        "asr_language": "zh",
        "asr_timeout_seconds": 120,
        "effective_asr_api_key": "secret",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_pcm_recording_is_private_and_exceeds_legacy_size_and_duration_limits(tmp_path):
    settings = _settings(tmp_path)
    recording = PcmRecording(settings)
    recording.open()
    one_second = b"\x00\x00" * settings.asr_sample_rate

    for _ in range(626):
        recording.append(one_second)
    await recording.close_input()

    assert recording.bytes_written > 20_000_000
    assert recording.duration_seconds == pytest.approx(626)
    assert stat.S_IMODE(recording.path.stat().st_mode) == 0o600

    await recording.discard()
    assert not recording.path.exists()


def test_pcm_recordings_use_private_user_and_session_namespaces(tmp_path):
    settings = _settings(tmp_path)
    first = PcmRecording(
        settings,
        owner_key="first.user@bosch.com",
        session_id="session-a",
    )
    second = PcmRecording(
        settings,
        owner_key="second.user@bosch.com",
        session_id="session-b",
    )
    first.open()
    second.open()

    assert first.root != second.root
    assert first.path.parent == first.root
    assert "first.user" not in str(first.path)
    assert "session-a" not in str(first.path)
    assert stat.S_IMODE(first.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(first.root.parent.stat().st_mode) == 0o700

    first._discard_sync()
    second._discard_sync()


def test_pcm_recording_requires_sustained_energy_for_speech(tmp_path):
    settings = _settings(tmp_path)
    recording = PcmRecording(settings)
    recording.open()

    recording.append(b"\x00\x00" * settings.asr_sample_rate)
    assert recording.has_speech() is False

    recording.append((1024).to_bytes(2, "little", signed=True) * 1600)
    assert recording.has_speech() is True
    assert recording.speech_peak_rms >= settings.asr_local_silence_rms_threshold

    recording._discard_sync()


def test_pcm_recording_rejects_startup_impulse_and_steady_noise(tmp_path):
    settings = _settings(tmp_path)
    recording = PcmRecording(settings)
    recording.open()

    impulse = (8192).to_bytes(2, "little", signed=True) * int(
        settings.asr_sample_rate * 0.06
    )
    silence = b"\x00\x00" * int(settings.asr_sample_rate * 0.5)
    recording.append(impulse + silence)
    assert recording.has_speech() is False

    background = (328).to_bytes(2, "little", signed=True) * int(
        settings.asr_sample_rate * 0.75
    )
    recording.append(background)
    assert recording.has_speech() is False

    recording._discard_sync()


def test_pcm_segmentation_covers_file_and_never_exceeds_maximum(tmp_path):
    path = tmp_path / "audio.pcm"
    sample_rate = 100
    path.write_bytes((b"\x01\x00" * sample_rate) * 650)

    ranges = segment_pcm_ranges(
        path,
        sample_rate=sample_rate,
        max_segment_seconds=300,
    )

    assert ranges[0][0] == 0
    assert ranges[-1][1] == path.stat().st_size
    assert all(left_end == right_start for (_, left_end), (right_start, _) in zip(ranges, ranges[1:]))
    assert all(end - start <= sample_rate * 2 * 300 for start, end in ranges)


@pytest.mark.asyncio
async def test_remote_transcription_retries_retryable_segment_and_preserves_order(tmp_path):
    settings = _settings(
        tmp_path,
        asr_sample_rate=100,
        asr_final_segment_seconds=300,
    )
    recording = PcmRecording(settings)
    recording.open()
    recording.append((b"\x01\x00" * settings.asr_sample_rate) * 650)
    attempts: defaultdict[str, int] = defaultdict(int)
    progress: list[tuple[int, int]] = []

    class _Auth:
        async def async_headers(self, api_key):
            assert api_key == "secret"
            return {"Authorization": "Bearer secret"}

    class _Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **kwargs):
            filename = kwargs["files"]["file"][0]
            attempts[filename] += 1
            index = int(filename.rsplit("_", 1)[1].split(".", 1)[0])
            if index == 2 and attempts[filename] == 1:
                return _Response(500)
            return _Response(200, {"text": ["一", "二", "三"][index - 1]})

    async def record_progress(completed, total):
        progress.append((completed, total))

    result = await RemoteAsrTranscriber(
        settings,
        auth=_Auth(),
        client_factory=_Client,
    ).transcribe(recording, progress=record_progress)

    assert result == "一二三"
    second_filename = next(name for name in attempts if name.endswith("_0002.wav"))
    assert attempts[second_filename] == 2
    assert sorted(progress) == [(1, 3), (2, 3), (3, 3)]
    await recording.discard()


def test_cleanup_removes_only_expired_pcm_files(tmp_path):
    settings = _settings(tmp_path, asr_recording_orphan_ttl_seconds=60)
    settings.asr_recording_tmp_dir.mkdir(parents=True)
    expired_dir = settings.asr_recording_tmp_dir / "user-a" / "session-a"
    fresh_dir = settings.asr_recording_tmp_dir / "user-b" / "session-b"
    expired_dir.mkdir(parents=True)
    fresh_dir.mkdir(parents=True)
    expired = expired_dir / "expired.pcm"
    fresh = fresh_dir / "fresh.pcm"
    expired.write_bytes(b"old")
    fresh.write_bytes(b"new")
    old = time.time() - 120
    os.utime(expired, (old, old))

    assert cleanup_stale_recordings(settings) == 1
    assert not expired.exists()
    assert fresh.exists()

@pytest.mark.asyncio
async def test_remote_transcription_borrows_shared_client_without_closing(tmp_path):
    settings = _settings(tmp_path, asr_sample_rate=100)
    recording = PcmRecording(settings)
    recording.open()
    recording.append(b"\x01\x00" * settings.asr_sample_rate)
    calls: list[dict] = []

    class _Auth:
        async def async_headers(self, _api_key):
            return {"Authorization": "Bearer secret"}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "共享连接结果"}

    class _SharedClient:
        closed = False

        async def post(self, _url, **kwargs):
            calls.append(kwargs)
            return _Response()

        async def aclose(self):
            self.closed = True

    client = _SharedClient()
    result = await RemoteAsrTranscriber(
        settings,
        auth=_Auth(),
        client=client,
    ).transcribe(recording)

    assert result == "共享连接结果"
    assert client.closed is False
    assert len(calls) == 1
    assert calls[0]["timeout"] is not None
    await recording.discard()


@pytest.mark.asyncio
async def test_remote_fallback_can_omit_language_for_auto_detection(tmp_path):
    settings = _settings(tmp_path, asr_sample_rate=100)
    recording = PcmRecording(settings)
    recording.open()
    recording.append(b"\x01\x00" * settings.asr_sample_rate)
    calls: list[dict] = []

    class _Auth:
        async def async_headers(self, _api_key):
            return {"Authorization": "Bearer secret"}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "Bonjour 世界"}

    class _Client:
        async def post(self, _url, **kwargs):
            calls.append(kwargs)
            return _Response()

    result = await RemoteAsrTranscriber(
        settings,
        auth=_Auth(),
        client=_Client(),
    ).transcribe(recording, language="")

    assert result == "Bonjour 世界"
    assert "language" not in calls[0]["data"]
    await recording.discard()


@pytest.mark.asyncio
async def test_remote_in_memory_transcription_forwards_prompt(tmp_path):
    settings = _settings(tmp_path, asr_sample_rate=100)
    calls: list[dict] = []

    class _Auth:
        async def async_headers(self, _api_key):
            return {"Authorization": "Bearer secret"}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "TCL 识别结果"}

    class _Client:
        async def post(self, _url, **kwargs):
            calls.append(kwargs)
            return _Response()

    result = await RemoteAsrTranscriber(
        settings,
        auth=_Auth(),
        client=_Client(),
    ).transcribe_wav(
        b"RIFF-valid-wav",
        recording_id="preview",
        prompt="TCL, G9；上一段已确认文本：绩效反馈",
    )

    assert result == "TCL 识别结果"
    assert calls[0]["data"]["prompt"] == "TCL, G9；上一段已确认文本：绩效反馈"


def test_prepare_recording_storage_creates_private_directory(tmp_path):
    settings = _settings(tmp_path)

    root = prepare_recording_storage(settings)

    assert root == settings.asr_recording_tmp_dir
    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_local_final_transcription_uses_resident_model_and_preserves_progress(tmp_path):
    settings = _settings(tmp_path, asr_sample_rate=100)
    recording = PcmRecording(settings)
    recording.open()
    recording.append(b"\x10\x00" * settings.asr_sample_rate)
    calls: list[dict] = []
    progress: list[tuple[int, int]] = []

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "完整高质量文本"}

    class _Client:
        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return _Response()

    async def record_progress(completed, total):
        progress.append((completed, total))

    result = await LocalAsrTranscriber(
        settings,
        client=_Client(),
    ).transcribe(
        recording,
        language="zh",
        progress=record_progress,
        expected_text="完整高质量文本",
    )

    assert result == "完整高质量文本"
    assert progress == [(1, 1)]
    assert len(calls) == 1
    assert calls[0]["url"] == "http://qwen3-asr:7118/api/transcribe"
    assert calls[0]["headers"]["Content-Type"] == "audio/pcm"
    assert calls[0]["params"] == {"language": "zh"}
    await recording.discard()


@pytest.mark.asyncio
async def test_local_final_transcription_can_omit_language_for_auto_detection(tmp_path):
    settings = _settings(tmp_path, asr_sample_rate=100)
    recording = PcmRecording(settings)
    recording.open()
    recording.append(b"\x10\x00" * settings.asr_sample_rate)
    calls: list[dict] = []

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"language": "Chinese,English", "text": "这个 KPI 已 achieved"}

    class _Client:
        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return _Response()

    result = await LocalAsrTranscriber(
        settings,
        client=_Client(),
    ).transcribe(
        recording,
        language="",
        expected_text="这个 KPI 已 achieved",
    )

    assert result == "这个 KPI 已 achieved"
    assert "params" not in calls[0]
    await recording.discard()


@pytest.mark.asyncio
async def test_local_final_transcription_rejects_suspiciously_short_result(tmp_path):
    settings = _settings(tmp_path, asr_sample_rate=100)
    recording = PcmRecording(settings)
    recording.open()
    recording.append(b"\x10\x00" * settings.asr_sample_rate)

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "短"}

    class _Client:
        async def post(self, _url, **_kwargs):
            return _Response()

    with pytest.raises(AsrFinalTranscriptionError, match="疑似缺失内容"):
        await LocalAsrTranscriber(
            settings,
            client=_Client(),
        ).transcribe(
            recording,
            expected_text="这是用于校验完整性的较长实时预览文字内容",
        )
    await recording.discard()

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.config.settings import Settings
from backend.services.bosch_realtime_asr_service import BoschChunkedAsrProxy
from backend.services.recording_asr_service import (
    RemoteAsrTranscriber,
    join_overlapping_segment_transcripts,
    overlap_pcm_ranges,
)


class _Slot:
    wait_ms = 12.5


class _Coordinator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @asynccontextmanager
    async def lease(self, name: str, **kwargs):
        self.calls.append({"name": name, **kwargs})
        yield _Slot()


def test_final_segment_overlap_preserves_audio_context_at_boundaries():
    assert overlap_pcm_ranges(
        [(0, 200), (200, 400), (400, 500)],
        sample_rate=100,
        overlap_seconds=0.1,
    ) == [(0, 200), (180, 400), (380, 500)]


def test_final_transcript_join_removes_only_reliable_boundary_overlap():
    assert join_overlapping_segment_transcripts(
        [
            "第一段相同边界上下文",
            "相同边界上下文第二段公共边界内容",
            "第二段公共边界内容最后结束",
        ]
    ) == "第一段相同边界上下文第二段公共边界内容最后结束"
    assert join_overlapping_segment_transcripts(["嗯嗯", "嗯嗯继续"]) == (
        "嗯嗯嗯嗯继续"
    )


@pytest.mark.asyncio
async def test_remote_bosch_request_uses_cross_replica_lease_and_priority():
    settings = SimpleNamespace(
        asr_bosch_global_max_concurrency=16,
        asr_timeout_seconds=30,
        asr_language="zh",
        asr_http_model="qwen3-asr-flash",
        asr_http_url="https://model.example/audio/transcriptions",
        effective_asr_api_key="test-key",
    )
    coordinator = _Coordinator()
    calls: list[dict] = []

    class _Auth:
        async def async_headers(self, _api_key):
            return {"Authorization": "Bearer test-key"}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "识别成功"}

    class _Client:
        async def post(self, _url, **kwargs):
            calls.append(kwargs)
            return _Response()

    text = await RemoteAsrTranscriber(
        settings,
        auth=_Auth(),
        client=_Client(),
        coordinator=coordinator,
    ).transcribe_wav(
        b"RIFF-valid-wav",
        recording_id="preview",
        prompt="TCL, G9",
        admission_priority=5,
    )

    assert text == "识别成功"
    assert coordinator.calls == [
        {
            "name": "asr:bosch:http-inference",
            "capacity": 16,
            "timeout_seconds": 0.0,
            "priority": 5,
        }
    ]
    assert calls[0]["data"]["prompt"] == "TCL, G9"


@pytest.mark.asyncio
async def test_chunked_bosch_prioritizes_first_preview_request():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.5,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    priorities: list[int] = []

    async def transcribe(_audio: bytes, **kwargs) -> str:
        priorities.append(kwargs["admission_priority"])
        return "首段预览"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio((4000).to_bytes(2, "little", signed=True) * 8000)
    await proxy._wait_for_idle()

    assert priorities == [5]
    await proxy.close()

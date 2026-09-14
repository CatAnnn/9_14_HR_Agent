from __future__ import annotations

import asyncio
from array import array

import pytest

from backend.config.settings import Settings
from backend.services.bosch_realtime_asr_service import (
    BoschChunkedAsrProxy,
    _merge_transcript,
)
from backend.services.realtime_asr_service import (
    AsrConfigurationError,
    RealtimeAsrProxy,
    create_realtime_asr_provider,
)
from backend.services.recording_asr_service import AsrFinalTranscriptionError


def _speech_pcm(seconds: float = 1.0, *, sample_rate: int = 16_000) -> bytes:
    samples = array("h", [4_000] * int(sample_rate * seconds))
    return samples.tobytes()


def test_bosch_preview_merge_keeps_overlap_without_repeating_text():
    assert _merge_transcript("绩效目标需要", "目标需要进一步明确") == "绩效目标需要进一步明确"
    assert _merge_transcript("目标 A", "目标 B") == "目标 A目标 B"


def test_realtime_provider_factory_selects_all_three_modes():
    local_client = object()
    speech_client = object()

    local = create_realtime_asr_provider(
        Settings(asr_provider_mode="local"),
        local_client=local_client,
        language="en",
    )
    bosch = create_realtime_asr_provider(
        Settings(asr_provider_mode="bosch"),
        speech_client=speech_client,
        language="en",
    )

    assert isinstance(local, RealtimeAsrProxy)
    assert local._local_client is local_client
    assert local._language == "en"
    assert isinstance(bosch, BoschChunkedAsrProxy)
    assert bosch._client is speech_client
    assert bosch._language == "en"
    with pytest.raises(AsrConfigurationError):
        create_realtime_asr_provider(Settings(asr_provider_mode="browser"))


@pytest.mark.asyncio
async def test_bosch_preview_seals_active_tail_as_authoritative_final():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object(), language="en")
    calls: list[bytes] = []
    languages: list[str] = []

    async def transcribe(audio: bytes, **kwargs) -> str:
        calls.append(audio)
        languages.append(kwargs["language"])
        return "这是实时预览"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm())
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)

    assert calls and calls[0].startswith(b"RIFF")
    assert languages and set(languages) == {"en"}
    assert proxy.last_text == "这是实时预览"
    assert proxy.speech_detected is True
    assert proxy.authoritative_final is False
    result = await proxy.finish()

    assert result["authoritative"] is True
    assert result["text"] == "这是实时预览"
    assert result["segments_total"] == 1
    assert result["segments_complete"] == 1
    assert proxy.authoritative_final is True
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_preview_second_snapshot_runs_at_one_second_total():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.5,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=1,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    windows: list[float] = []

    async def transcribe(audio: bytes, **_kwargs) -> str:
        windows.append((len(audio) - 44) / (settings.asr_sample_rate * 2))
        return f"结果{len(windows)}"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.5))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    await proxy.append_audio(_speech_pcm(0.4))
    await asyncio.sleep(0)

    assert windows == pytest.approx([0.5], abs=0.01)

    await proxy.append_audio(_speech_pcm(0.1))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)

    assert windows == pytest.approx([0.5, 1.0], abs=0.01)
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_default_fast_first_snapshot_starts_at_point_four_seconds():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_preview_overlap_ms=0,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    assert settings.asr_bosch_preview_chunk_seconds == 0.75
    windows: list[float] = []

    async def transcribe(audio: bytes, **_kwargs) -> str:
        windows.append((len(audio) - 44) / (settings.asr_sample_rate * 2))
        return "首个结果"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.3))
    await asyncio.sleep(0)
    assert windows == []

    await proxy.append_audio(_speech_pcm(0.1))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    assert windows == pytest.approx([0.4], abs=0.01)

    await proxy.append_audio(_speech_pcm(0.3))
    await asyncio.sleep(0)
    assert windows == pytest.approx([0.4], abs=0.01)

    await proxy.append_audio(_speech_pcm(0.05))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    assert windows == pytest.approx([0.4, 0.75], abs=0.01)
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_preserves_first_snapshot_while_connection_prepares():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.4,
        asr_bosch_preview_chunk_seconds=0.5,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=2,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    windows: list[float] = []

    async def transcribe(audio: bytes, **_kwargs) -> str:
        windows.append((len(audio) - 44) / (settings.asr_sample_rate * 2))
        return f"结果{len(windows)}"

    proxy._transcriber.transcribe_wav = transcribe

    # Audio may reach the proxy while Bosch authentication is still running.
    # The first 400 ms request must survive later cumulative coalescing.
    await proxy.append_audio(_speech_pcm(1.0))
    await proxy.connect()
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)

    assert windows == pytest.approx([0.4, 1.0], abs=0.01)
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_finish_reuses_latest_snapshot_when_tail_is_silence():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.4,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_silence_commit_ms=400,
        asr_bosch_preview_per_session_concurrency=1,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    calls = 0

    async def transcribe(_audio: bytes, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "完整累计结果"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.4))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    await proxy.append_audio(b"\x00\x00" * 6_400)
    result = await proxy.finish()

    assert calls == 1
    assert result["authoritative"] is True
    assert result["text"] == "完整累计结果"
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_finish_transcribes_again_when_new_speech_follows_snapshot():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.4,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=1,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    calls = 0

    async def transcribe(_audio: bytes, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "最终完整结果" if calls == 2 else "中间结果"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.4))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    await proxy.append_audio(_speech_pcm(0.2))
    result = await proxy.finish()

    assert calls == 2
    assert result["authoritative"] is True
    assert result["text"] == "最终完整结果"
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_preview_rejects_empty_tail_final():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.5,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=1,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())

    async def transcribe(_audio: bytes, **kwargs) -> str:
        return "有效临时文字" if kwargs["admission_priority"] != 0 else ""

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.5))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    result = await proxy.finish()

    assert result["authoritative"] is False
    assert result["segments_complete"] == 0
    assert result["text"] == "有效临时文字"
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_preview_tail_timeout_is_not_authoritative():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.5,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_tail_final_timeout_seconds=0.01,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=1,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    cancelled = asyncio.Event()

    async def transcribe(_audio: bytes, **_kwargs) -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.2))
    result = await proxy.finish()

    assert result["authoritative"] is False
    assert result["timed_out"] is True
    assert cancelled.is_set()
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_cumulative_preview_replaces_the_unstable_hypothesis():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.5,
        asr_bosch_preview_chunk_seconds=0.5,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=1,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    calls = 0

    async def transcribe(_audio: bytes, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "今天谈" if calls == 1 else "今天讨论绩效目标"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.5))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    assert proxy.last_text == "今天谈"

    await proxy.append_audio(_speech_pcm(0.5))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    assert proxy.last_text == "今天讨论绩效目标"
    assert proxy.last_text != "今天谈今天讨论绩效目标"

    events = []
    while not proxy._events.empty():
        events.append(proxy._events.get_nowait())
    assert events[-1]["stable_text"] == ""
    assert events[-1]["unstable_text"] == "今天讨论绩效目标"
    assert events[-1]["correction_state"] == "correcting"
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_preview_uses_a_short_first_chunk_then_normal_windows():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.75,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=1,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    calls: list[bytes] = []

    async def transcribe(audio: bytes, **_kwargs) -> str:
        calls.append(audio)
        return "不完整首片" if len(calls) == 1 else "完整的一秒识别结果"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.7))
    await asyncio.sleep(0)
    assert calls == []

    await proxy.append_audio(_speech_pcm(0.1))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    assert len(calls) == 1

    await proxy.append_audio(_speech_pcm(1.0))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    assert len(calls) == 2
    assert proxy.last_text == "完整的一秒识别结果"
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_preview_uses_increasing_cumulative_snapshots():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.8,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_accumulation_max_seconds=3,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=2,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    windows: list[float] = []

    async def transcribe(audio: bytes, **_kwargs) -> str:
        # PCM payload size is WAV length minus its 44-byte header.
        windows.append((len(audio) - 44) / (settings.asr_sample_rate * 2))
        return f"第{len(windows)}段"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.8))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    await proxy.append_audio(_speech_pcm(1.0))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    await proxy.append_audio(_speech_pcm(1.0))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)

    assert windows == pytest.approx([0.8, 1.0, 2.0], abs=0.01)
    assert windows == sorted(windows)
    assert max(windows) <= 3.0
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_preview_supplies_hotwords_and_recent_stable_text():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_prompt="TCL, Career Elements",
        asr_bosch_prompt_context_chars=20,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    prompts: list[str] = []

    async def transcribe(_audio: bytes, **kwargs) -> str:
        prompts.append(kwargs["prompt"])
        return "第一段稳定文本" if len(prompts) <= 3 else "第二段"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm())
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    await proxy.append_audio(b"\x00\x00" * 12_000)
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    await proxy.append_audio(_speech_pcm())
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)

    assert prompts[0] == "TCL, Career Elements"
    assert "上一段已确认文本：第一段稳定文本" in prompts[-1]
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_fast_first_failure_does_not_disable_normal_preview():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.5,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=2,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    first_started = asyncio.Event()

    async def transcribe(_audio: bytes, **kwargs) -> str:
        if kwargs["index"] == 0:
            first_started.set()
            raise AsrFinalTranscriptionError("short preview rejected")
        return "正常窗口识别成功"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.5))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await proxy.append_audio(_speech_pcm(1.0))
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)

    assert proxy.failed is False
    assert proxy.last_text == "正常窗口识别成功"
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_normal_window_can_supersede_a_slow_fast_first_request():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_first_preview_chunk_seconds=0.5,
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=2,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    release_first = asyncio.Event()
    first_started = asyncio.Event()

    async def transcribe(_audio: bytes, **kwargs) -> str:
        if kwargs["index"] == 0:
            first_started.set()
            await release_first.wait()
            return "迟到的短首片"
        return "完整窗口优先显示"

    async def wait_for_complete_window() -> None:
        while proxy.last_text != "完整窗口优先显示":
            await asyncio.sleep(0)

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm(0.5))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await proxy.append_audio(_speech_pcm(1.0))
    await asyncio.wait_for(wait_for_complete_window(), timeout=1)
    assert proxy.last_text == "完整窗口优先显示"

    release_first.set()
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)
    assert proxy.last_text == "完整窗口优先显示"
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_preview_rejects_a_stale_result_after_newer_correction():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_preview_chunk_seconds=1,
        asr_bosch_preview_overlap_ms=0,
        asr_bosch_min_speech_ms=100,
        asr_bosch_preview_per_session_concurrency=2,
        asr_local_silence_rms_threshold=0.001,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    first_release = asyncio.Event()
    first_started = asyncio.Event()
    second_completed = asyncio.Event()
    active = 0
    max_active = 0

    async def transcribe(_audio: bytes, **kwargs) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            if kwargs["index"] == 0:
                first_started.set()
                await first_release.wait()
                return "第一段"
            second_completed.set()
            return "第二段"
        finally:
            active -= 1

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(_speech_pcm())
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await proxy.append_audio(_speech_pcm())

    await asyncio.wait_for(second_completed.wait(), timeout=1)
    assert proxy.last_text == "第二段"
    first_release.set()
    await asyncio.wait_for(proxy._wait_for_idle(), timeout=1)

    assert max_active == 2
    assert proxy.last_text == "第二段"
    await proxy.close()


@pytest.mark.asyncio
async def test_bosch_preview_does_not_send_silent_chunks():
    settings = Settings(
        asr_provider_mode="bosch",
        asr_http_url="https://model.example/audio/transcriptions",
        asr_http_model="qwen3-asr-flash",
        asr_api_key="test-key",
        asr_bosch_preview_chunk_seconds=1,
        asr_local_silence_rms_threshold=0.01,
    )
    proxy = BoschChunkedAsrProxy(settings, client=object())
    calls = 0

    async def transcribe(_audio: bytes, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "不应出现"

    proxy._transcriber.transcribe_wav = transcribe
    await proxy.connect()
    await proxy.append_audio(b"\x00\x00" * 32_000)
    await asyncio.sleep(0)

    assert calls == 0
    assert proxy.speech_detected is False
    await proxy.close()

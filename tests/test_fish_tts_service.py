from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
import json
from pathlib import Path
from typing import Any
import wave

import httpx
import pytest

from backend.business_config.loader import get_config_loader
from backend.config.settings import Settings
from backend.observability.rehearsal_timing import RehearsalTurnTiming
from backend.schemas.simulation import EmotionState, VADVector
from backend.schemas.state import SessionState
from backend.services.fish_tts_service import (
    FishTtsService,
    TtsConfigurationError,
    TtsError,
    TtsProtocolError,
    TtsQueueTimeoutError,
)
from backend.services.qwen_tts_service import QwenTtsService
from backend.services.rehearsal_service import RehearsalService


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "tts_enabled": True,
        "tts_provider": "fish_vllm_omni",
        "tts_http_url": "http://fish-tts.test/v1/audio/speech",
        "tts_model": "/app/checkpoints/s2-pro",
        "tts_model_revision": "test-revision",
        "tts_stage_0_max_num_seqs": 8,
        "tts_stage_1_max_num_seqs": 4,
        "tts_default_voice": "Fish Audio 默认音色",
        "tts_available_voices": "Fish Audio 默认音色,employee",
        "tts_random_voice_name": "Fish Audio 默认音色",
        "tts_require_registered_voice": False,
        "tts_max_concurrency": 2,
        "tts_global_max_concurrency": 8,
        "tts_queue_timeout_seconds": 0.5,
        "tts_connect_timeout_seconds": 0.5,
        "tts_generation_timeout_seconds": 2.0,
        "tts_sample_rate": 44_100,
        "tts_chunk_length": 200,
        "tts_sentence_max_chars": 180,
        "tts_max_new_tokens": 1_024,
        "tts_seed": 42,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_fish_tts_readiness_uses_health_endpoint_when_voice_gate_disabled() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(
            _settings(tts_require_registered_voice=False),
            client=client,
        )
        assert await service.is_ready() is True
        assert await service.is_ready() is True

    assert len(requests) == 1
    assert requests[0].url.path == "/health"
    assert requests[0].method == "GET"


def test_fish_tts_requires_a_registered_voice_by_default() -> None:
    assert Settings.model_fields["tts_require_registered_voice"].default is True


@pytest.mark.asyncio
async def test_fish_tts_readiness_accepts_registered_default_voice() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(
            200,
            json={"uploaded_voices": [{"name": "Employee-Natural"}]},
        )

    settings = _settings(
        tts_default_voice="employee-natural",
        tts_available_voices="employee-natural",
        tts_require_registered_voice=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(settings, client=client)
        assert await service.is_ready() is True
        assert service._readiness_checked_at is not None
        service._readiness_checked_at -= 2.0
        assert await service.is_ready() is True

    assert [request.url.path for request in requests] == [
        "/health",
        "/v1/audio/voices",
    ]


@pytest.mark.asyncio
async def test_fish_tts_rejects_unconditioned_default_voice_without_request() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(
            _settings(tts_require_registered_voice=True),
            client=client,
        )
        assert await service.is_ready(force=True) is False
        with pytest.raises(
            TtsConfigurationError,
            match="没有固定声纹的默认随机音色",
        ):
            await service.ensure_ready()

    assert requests == []


@pytest.mark.asyncio
async def test_fish_tts_rejects_missing_registered_default_voice() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"uploaded_voices": []})

    settings = _settings(
        tts_default_voice="employee-natural",
        tts_available_voices="employee-natural",
        tts_require_registered_voice=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(settings, client=client)
        assert await service.is_ready(force=True) is False
        with pytest.raises(
            TtsConfigurationError,
            match="employee-natural.*尚未.*注册",
        ):
            await service.ensure_ready()
        assert service._readiness_checked_at is not None
        service._readiness_checked_at -= 2.0
        assert await service.is_ready() is False

    assert [request.url.path for request in requests] == [
        "/health",
        "/v1/audio/voices",
        "/health",
        "/v1/audio/voices",
    ]


@pytest.mark.parametrize(
    ("available_voices", "requested_voice"),
    [
        ("employee-natural,Fish Audio 默认音色", "Fish Audio 默认音色"),
        ("employee-natural,default", "default"),
        ("employee-natural,Fish Audio 默认音色", "Vivian"),
    ],
)
def test_registered_voice_gate_rejects_every_unconditioned_voice_path(
    available_voices: str,
    requested_voice: str,
) -> None:
    service = FishTtsService(
        _settings(
            tts_default_voice="employee-natural",
            tts_available_voices=available_voices,
            tts_require_registered_voice=True,
        ),
        client=object(),
    )

    with pytest.raises(TtsConfigurationError, match="没有固定声纹"):
        service.resolve_voice(requested_voice)


def test_registered_voice_gate_keeps_named_voice_resolution() -> None:
    service = FishTtsService(
        _settings(
            tts_default_voice="employee-natural",
            tts_available_voices="employee-natural,employee-calm",
            tts_require_registered_voice=True,
        ),
        client=object(),
    )

    assert service.resolve_voice("Employee-Calm") == "employee-calm"


@pytest.mark.asyncio
async def test_disabled_tts_never_contacts_fish_runtime() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(_settings(tts_enabled=False), client=client)
        assert await service.is_ready(force=True) is False
        with pytest.raises(TtsConfigurationError, match="TTS_ENABLED"):
            await service.ensure_ready()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put("这段文字不应发送给语音模型。")
        await queue.put(None)
        with pytest.raises(TtsConfigurationError, match="TTS_ENABLED"):
            async for _event in service.stream(queue, emotion_state=None):
                pass

    assert requests == []


def _wav_bytes(pcm: bytes, sample_rate: int = 44_100) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


@pytest.mark.asyncio
async def test_fish_tts_streams_sentences_and_preserves_pcm_contract() -> None:
    pcm = b"\x00\x00\xff\x7f"
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "audio/pcm"},
            stream=_ChunkedStream([pcm[:1], pcm[1:3], pcm[3:]]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(_settings(), client=client)
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        text_queue.put_nowait("我需要先了解")
        text_queue.put_nowait("具体依据。我们再讨论。")
        text_queue.put_nowait(None)
        emotion = EmotionState(
            current_anchor_id="guarded",
            previous_anchor_id="neutral",
            current_vad=VADVector(valence=-0.3, arousal=0.5, dominance=0.25),
            last_vad_delta=VADVector(arousal=0.14),
            transition_intensity=0.2,
            has_manager_response=True,
        )

        events = [
            event
            async for event in service.stream(
                text_queue,
                emotion_state=emotion,
                voice="fish audio 默认音色",
            )
        ]

    assert [event["event"] for event in events] == [
        "speech_start",
        "speech_audio",
        "speech_sentence_done",
        "speech_start",
        "speech_audio",
        "speech_sentence_done",
        "speech_done",
    ]
    assert [
        event["_pcm"]
        for event in events
        if event["event"] == "speech_audio"
    ] == [pcm, pcm]
    assert all(event.get("sample_rate") == 44_100 for event in events if "sample_rate" in event)
    assert len(requests) == 2
    assert requests[0] == {
        "model": "/app/checkpoints/s2-pro",
        "input": "[guarded] [excited tone] 我需要先了解具体依据。",
        "voice": "default",
        "stream": True,
        "stream_format": "audio",
        "response_format": "pcm",
        "max_new_tokens": 1_024,
        "seed": 42,
    }
    assert requests[1] == {
        **requests[0],
        # Every sentence is an independent Fish request, so retain the short
        # primary cue to prevent the second sentence from drifting neutral,
        # while the optional delivery cue is only used on the first request.
        "input": "[guarded] 我们再讨论。",
    }
    legacy_fields = {
        "text",
        "chunk_length",
        "format",
        "use_memory_cache",
        "normalize",
        "streaming",
        "top_p",
        "repetition_penalty",
        "temperature",
        "reference_id",
    }
    assert legacy_fields.isdisjoint(requests[0])



@pytest.mark.asyncio
async def test_fish_tts_emits_first_sentence_before_text_queue_closes() -> None:
    pcm = b"\x00\x00\x10\x00"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "audio/pcm"},
            content=pcm,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(_settings(), client=client)
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        text_queue.put_nowait("第一句。")
        stream = service.stream(
            text_queue,
            emotion_state=None,
            voice="fish audio 默认音色",
        )
        try:
            first_event = await asyncio.wait_for(anext(stream), timeout=0.5)
            assert first_event["event"] == "speech_start"
            assert first_event["sentence_index"] == 0
        finally:
            await stream.aclose()


@pytest.mark.asyncio
async def test_fish_tts_accepts_headerless_pcm_from_official_streaming_server() -> None:
    pcm = b"\x00\x00\x10\x00\x20\x00"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            stream=_ChunkedStream([pcm[:1], pcm[1:4], pcm[4:]]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(_settings(), client=client)
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        text_queue.put_nowait("测试。")
        text_queue.put_nowait(None)
        events = [
            event
            async for event in service.stream(text_queue, emotion_state=None)
        ]

    audio = b"".join(
        event["_pcm"]
        for event in events
        if event["event"] == "speech_audio"
    )
    assert audio == pcm
    assert next(event for event in events if event["event"] == "speech_start")[
        "sample_rate"
    ] == 44_100


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [None, "audio/pcm", "application/octet-stream"])
async def test_vllm_omni_accepts_supported_pcm_content_types(
    content_type: str | None,
) -> None:
    headers = {"content-type": content_type} if content_type else {}

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=headers,
            stream=_ChunkedStream([b"\x00", b"\x00\x01", b"\x00"]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(_settings(), client=client)
        events = [
            event
            async for event in service.stream(
                _sentence_queue("测试。"),
                emotion_state=None,
            )
        ]

    assert b"".join(
        event["_pcm"]
        for event in events
        if event["event"] == "speech_audio"
    ) == b"\x00\x00\x01\x00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json", b'{"error":"model failed"}'),
        (None, b'{"error":"model failed"}'),
        ("text/plain", b"model failed"),
    ],
)
async def test_vllm_omni_rejects_error_documents_as_audio(
    content_type: str | None,
    body: bytes,
) -> None:
    headers = {"content-type": content_type} if content_type else {}

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=headers,
            stream=_ChunkedStream([body]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(_settings(), client=client)
        with pytest.raises(TtsProtocolError):
            async for _event in service.stream(
                _sentence_queue("测试。"),
                emotion_state=None,
            ):
                pass


@pytest.mark.asyncio
async def test_vllm_omni_rejects_unaligned_pcm() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "audio/pcm"},
            stream=_ChunkedStream([b"\x00\x00\x01\x00\xff"]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(_settings(), client=client)
        with pytest.raises(TtsProtocolError, match="未对齐"):
            async for _event in service.stream(
                _sentence_queue("测试。"),
                emotion_state=None,
            ):
                pass


def test_fish_tts_custom_voice_uses_cached_reference_id() -> None:
    service = FishTtsService(
        _settings(
            tts_provider="fish_legacy",
            tts_http_url="http://fish-tts.test/v1/tts",
            tts_model="fishaudio/s2-pro",
        ),
        client=object(),
    )

    payload = service._request_payload(
        "请继续。",
        emotion_state=None,
        voice=service.resolve_voice("EMPLOYEE"),
    )

    assert payload["reference_id"] == "employee"
    assert payload["use_memory_cache"] == "on"


def test_fish_legacy_s2_endpoint_uses_square_emotion_controls_by_default() -> None:
    service = FishTtsService(
        _settings(
            tts_provider="fish_legacy",
            tts_http_url="http://fish-tts.test/v1/tts",
            tts_model="fishaudio/s2-pro",
        ),
        client=object(),
    )
    state = EmotionState(
        current_anchor_id="angry",
        previous_anchor_id="neutral",
        current_vad=VADVector(valence=-0.8, arousal=0.8, dominance=0.8),
        transition_intensity=0.8,
        has_manager_response=True,
    )

    payload = service._request_payload(
        "请继续。",
        emotion_state=state,
        voice="employee",
    )

    # The historical /v1/tts deployment still loads S2-Pro.  Its endpoint
    # name is not enough to infer S1's parenthesized syntax.
    assert payload["text"].startswith("[angry] ")
    assert "(angry)" not in payload["text"]


def test_fish_legacy_parentheses_are_an_explicit_s1_compatibility_opt_in() -> None:
    service = FishTtsService(
        _settings(
            tts_provider="fish_legacy",
            tts_http_url="http://fish-tts.test/v1/tts",
            tts_model="fishaudio/s1",
            tts_legacy_emotion_tag_delimiter="parentheses",
        ),
        client=object(),
    )
    state = EmotionState(
        current_anchor_id="angry",
        previous_anchor_id="neutral",
        current_vad=VADVector(valence=-0.8, arousal=0.8, dominance=0.8),
        transition_intensity=0.8,
        has_manager_response=True,
    )

    payload = service._request_payload(
        "请继续。",
        emotion_state=state,
        voice="employee",
    )

    assert payload["text"].startswith("(angry) ")
    assert "[angry]" not in payload["text"]


@pytest.mark.asyncio
async def test_legacy_provider_retains_wav_streaming_contract() -> None:
    requests: list[dict[str, Any]] = []
    pcm = b"\x00\x00\x01\x00"

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            stream=_ChunkedStream([_wav_bytes(pcm)]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(
            _settings(
                tts_provider="fish_legacy",
                tts_http_url="http://fish-tts.test/v1/tts",
                tts_model="fishaudio/s2-pro",
            ),
            client=client,
        )
        events = [
            event
            async for event in service.stream(
                _sentence_queue("测试。"),
                emotion_state=None,
            )
        ]

    assert requests[0]["format"] == "wav"
    assert requests[0]["streaming"] is True
    assert b"".join(
        event["_pcm"]
        for event in events
        if event["event"] == "speech_audio"
    ) == pcm


def test_vllm_omni_uses_named_uploaded_reference_voice() -> None:
    service = FishTtsService(_settings(), client=object())

    voice = service.resolve_voice("EMPLOYEE")
    payload = service._request_payload(
        "请继续。",
        emotion_state=None,
        voice=voice,
    )

    assert voice == "employee"
    assert payload["voice"] == "employee"
    # Neutral/no-state speech is left untouched.  Adding a generic
    # ``natural and clear`` cue changed the acoustic baseline even though it
    # carried no useful emotion information.
    assert payload["input"] == "请继续。"
    assert payload["seed"] == 42


def test_fish_tts_emotion_tag_uses_anchor_and_vad_axes() -> None:
    tag = FishTtsService.emotion_tag(
        EmotionState(
            current_anchor_id="angry",
            previous_anchor_id="neutral",
            current_vad=VADVector(valence=-0.8, arousal=0.8, dominance=0.8),
            transition_intensity=0.8,
            has_manager_response=True,
        )
    )

    assert tag == "[angry]"


def test_fish_tts_does_not_voice_personality_baseline_before_manager_turn() -> None:
    state = EmotionState(
        current_anchor_id="angry",
        current_vad=VADVector(valence=-0.8, arousal=0.8, dominance=0.8),
        has_manager_response=False,
    )

    assert FishTtsService.emotion_tag(state) == ""
    assert FishTtsService.style_text("先从事实开始。", state) == "先从事实开始。"
    assert FishTtsService.style_text("先从事实开始。", state, tag_mode="legacy") == "先从事实开始。"


def test_fish_tts_does_not_voice_an_unchanged_anchor_after_a_neutral_manager_turn() -> None:
    state = EmotionState(
        current_anchor_id="anxious",
        previous_anchor_id="anxious",
        current_vad=VADVector(valence=-0.2, arousal=0.3, dominance=-0.1),
        transition_intensity=0.04,
        has_manager_response=True,
    )

    # The anchor remains available to the text/emotion pipeline, but a stable
    # state after a neutral turn must not tint the audio on its own.
    assert FishTtsService.emotion_tag(state) == ""
    assert FishTtsService.emotion_tag(state, mode="legacy") == ""


def test_fish_tts_can_voice_a_new_anchor_when_the_stimulus_is_material() -> None:
    state = EmotionState(
        current_anchor_id="anxious",
        previous_anchor_id="neutral",
        current_vad=VADVector(valence=-0.2, arousal=0.3, dominance=-0.1),
        transition_intensity=0.2,
        has_manager_response=True,
    )

    assert FishTtsService.emotion_tag(state) == "[anxious]"


def test_fish_tts_has_an_explicit_tag_for_every_emotion_anchor() -> None:
    assert set(FishTtsService._ANCHOR_TAGS) == set(  # noqa: SLF001
        get_config_loader().emotion_anchors()
    )


def test_fish_tts_keeps_voice_and_seed_while_emotion_changes() -> None:
    service = FishTtsService(_settings(), client=object())
    calm = service._request_payload(
        "我们可以一步一步讨论。",
        emotion_state=EmotionState(
            current_anchor_id="calm",
            previous_anchor_id="neutral",
            current_vad=VADVector(valence=0.35, arousal=-0.7, dominance=0.3),
            transition_intensity=0.8,
            has_manager_response=True,
        ),
        voice="employee",
        seed=12345,
    )
    angry = service._request_payload(
        "我不能接受这个结论。",
        emotion_state=EmotionState(
            current_anchor_id="angry",
            previous_anchor_id="neutral",
            current_vad=VADVector(valence=-0.8, arousal=0.8, dominance=0.8),
            transition_intensity=0.8,
            has_manager_response=True,
        ),
        voice="employee",
        seed=12345,
    )

    assert calm["voice"] == angry["voice"] == "employee"
    assert calm["seed"] == angry["seed"] == 12345
    assert calm["input"].startswith("[calm]")
    assert angry["input"].startswith("[angry]")


def test_fish_tts_rejects_unknown_voice_before_request() -> None:
    service = FishTtsService(_settings(), client=object())

    assert service.resolve_voice("Vivian") == "Fish Audio 默认音色"
    with pytest.raises(TtsConfigurationError, match="不支持的语音声线"):
        service.resolve_voice("Unknown")


@pytest.mark.asyncio
async def test_fish_tts_fails_on_http_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "not ready"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(_settings(), client=client)
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        text_queue.put_nowait("测试。")
        text_queue.put_nowait(None)

        with pytest.raises(TtsProtocolError, match="HTTP 503：not ready"):
            async for _event in service.stream(text_queue, emotion_state=None):
                pass


def test_fish_tts_defaults_to_ten_dynamically_batched_streams() -> None:
    assert Settings.model_fields["tts_provider"].default == "fish_vllm_omni"
    assert Settings.model_fields["tts_max_concurrency"].default == 10
    assert Settings.model_fields["tts_global_max_concurrency"].default == 10
    assert Settings.model_fields["tts_queue_timeout_seconds"].default == 0
    assert Settings.model_fields["tts_stage_0_max_num_seqs"].default == 10
    assert Settings.model_fields["tts_stage_1_max_num_seqs"].default == 10
    assert Settings.model_fields["tts_emotion_tag_mode"].default == "inline"
    assert Settings.model_fields["tts_emotion_max_tags"].default == 2
    assert Settings.model_fields["tts_emotion_delta_threshold"].default == 0.10
    assert Settings.model_fields["tts_legacy_emotion_tag_delimiter"].default == "square"
    assert _settings(tts_stage_1_max_num_seqs=8).tts_stage_1_max_num_seqs == 8


def test_fish_tts_image_pins_vllm_omni_and_verified_fish_speech_wheel() -> None:
    dockerfile = Path("fish_tts/Dockerfile").read_text(encoding="utf-8")
    cudagraph_patch = Path(
        "fish_tts/patches/vllm_omni_0_24_fish_cudagraph_warmup.patch"
    ).read_text(encoding="utf-8")
    interleaved_kv_patch = Path(
        "fish_tts/patches/vllm_omni_0_24_fish_interleaved_kv.patch"
    ).read_text(encoding="utf-8")
    default_voice_patch = Path(
        "fish_tts/patches/vllm_omni_0_24_fish_default_voice.patch"
    ).read_text(encoding="utf-8")
    dynamic_batch_patch = Path(
        "fish_tts/patches/vllm_omni_0_24_fish_dynamic_batch.patch"
    ).read_text(encoding="utf-8")

    assert (
        "FROM vllm/vllm-omni:v0.24.0@"
        "sha256:ffb9a2a8e78a848e80ce40674ab7f36d7653358b30e616629f3ba9a235873b79"
        in dockerfile
    )
    assert "FISH_SPEECH_VERSION=0.1.0" in dockerfile
    assert (
        "FISH_SPEECH_WHEEL_SHA256="
        "1ea6123c7b9a441bc48f296439ae0e987621b780f9314ff3eb5ad072ebe9e7ac"
        in dockerfile
    )
    assert "pip install --no-deps" in dockerfile
    assert "import vllm_omni" in dockerfile
    assert "fish_speech.models.dac import modded_dac, rvq" in dockerfile
    assert "m.version('vllm') == '0.24.0'" in dockerfile
    assert "m.version('vllm-omni') == '0.24.0'" in dockerfile
    assert "numpy.__version__ == '2.2.6'" in dockerfile
    assert "pydantic.__version__ == '2.13.4'" in dockerfile
    assert "torch.__version__ == '2.11.0+cu130'" in dockerfile
    assert "91a1410d6780c385635f328a73a1999b022266b27fb9239212cb32f67d77a19a" in dockerfile
    assert "patch --batch --forward --fuzz=0" in dockerfile
    assert "num_scheduled_tokens_np=num_scheduled_tokens" in cudagraph_patch
    assert "is_graph_capturing or profile_seq_lens is not None" in cudagraph_patch
    assert "1b84f3efc36aef48b4b0380ff7be2c53b8e9890e54ffc52cee6b4a57f22056ab" in dockerfile
    assert "14da390c8a51eef38a8ec419127f074bdb272d617a6a8001fd08172cde6f8bf9" in dockerfile
    assert "c6477d4de799c5c48dc4deb713cb1eca85c073fc3e46289b6f79935e08d5a340" in dockerfile
    assert "K_BLOCK_STRIDE=key_cache.stride(0)" in interleaved_kv_patch
    assert "V_BLOCK_STRIDE=value_cache.stride(0)" in interleaved_kv_patch
    assert "-        and key_cache.is_contiguous()" in interleaved_kv_patch
    assert "-        and value_cache.is_contiguous()" in interleaved_kv_patch
    assert "+        and key_cache.is_contiguous()" not in interleaved_kv_patch
    assert "+        and value_cache.is_contiguous()" not in interleaved_kv_patch
    assert "not torch.cuda.is_current_stream_capturing()" in interleaved_kv_patch
    assert "3e290faf4766fc3cae77bbdfcd0f1de5bbde4898795db1fa798471f5d352e84b" in dockerfile
    assert '(request.voice or "").strip().casefold() != "default"' in default_voice_patch
    assert "_apply_uploaded_speaker(request)" in default_voice_patch
    assert "talker_mtp_accepts_per_row_generators = True" in dynamic_batch_patch
    assert "self.talker_mtp_graph_safe = False" in dynamic_batch_patch
    assert "generators=kwargs.get(\"generators\")" in dynamic_batch_patch
    assert "batch_sizes=(1, 4)" in dynamic_batch_patch
    assert "generator is None and generators is None" in dynamic_batch_patch
    assert "actual_batch_size" in dynamic_batch_patch
    assert "[FishDynamicBatch]" in dynamic_batch_patch
    assert "stage=fast_ar" in dynamic_batch_patch
    assert "stage=dac_model" in dynamic_batch_patch
    assert "21ac086ef99c15dc998ed875d5c3be2e744007622de4fcea151480812e1441d1" in dockerfile
    assert "3c03cfa3db4b789fd0a642bd3be77977e43c1542c65670585c9ed87e8a873e5c" in dockerfile
    assert "bdf15958749ca4621f109b64a2aca53da22fe772e559b6c60202ae2e10ff4155" in dockerfile
    assert "5c19fe26ea22e530f02611ced08df37767d89ef9a6c4e821b84f97444453ba43" in dockerfile


def test_fish_tts_compose_uses_vllm_omni_and_validates_checkpoint() -> None:
    compose = Path("deployment/compose/compose.services.yml").read_text(
        encoding="utf-8"
    )

    init_block_start = compose.index("  fish_tts_model_init:")
    runtime_block_start = compose.index("  fish_tts:", init_block_start)
    next_service_start = compose.index("  signoz_collection_agent:", runtime_block_start)
    init_block = compose[init_block_start:runtime_block_start]
    runtime_block = compose[runtime_block_start:next_service_start]

    assert 'profiles: ["model-init"]' in init_block
    assert "hf download" in init_block
    assert "uv run hf download" not in init_block
    assert "HF_HOME: /app/checkpoints/.hf_home" in init_block
    assert ".hr-agent-ready-$${FISH_TTS_MODEL_REVISION}" in init_block
    for filename in (
        "codec.pth",
        "config.json",
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
        "model.safetensors.index.json",
        "tokenizer.json",
    ):
        assert filename in init_block
        assert filename in runtime_block

    assert "exec vllm serve \"$${FISH_TTS_MODEL_PATH}\"" in runtime_block
    assert "--omni" in runtime_block
    assert "--port 8091" in runtime_block
    assert "--deploy-config \"$${FISH_TTS_DEPLOY_CONFIG}\"" in runtime_block
    assert "FISH_TTS_DEPLOY_CONFIG: /app/config/fish_s2_pro_dynamic_batch.yaml" in runtime_block
    assert "./fish_tts/config/fish_s2_pro_dynamic_batch.yaml:/app/config/fish_s2_pro_dynamic_batch.yaml:ro" in runtime_block
    assert "FISH_TTS_STAGE_OVERRIDES" not in runtime_block
    assert "enforce_eager" not in runtime_block
    assert "VLLM_OMNI_FISH_KVCACHE_ATTN: required" in runtime_block
    assert "fish_kvcache_attn as f; assert f.is_available(), f.load_error()" in runtime_block
    assert "SPEAKER_SAMPLES_DIR: /app/speakers" in runtime_block
    assert "fish_tts_speakers:/app/speakers" in runtime_block
    assert "fish_tts_speakers:" in compose
    assert "name: 06_fish_tts_speakers" not in compose
    assert (
        "TORCHINDUCTOR_CACHE_DIR: "
        "/var/cache/fish-tts/torch-2.11.0-cu130-vllm-omni-0.24.0-fast-ar-v1"
        in runtime_block
    )
    assert "fish_tts_compile_cache:/var/cache/fish-tts" in runtime_block
    assert "fish_tts_compile_cache:" in compose
    assert "name: 06_fish_tts_compile_cache" in compose
    assert 'device_ids: ["${FISH_TTS_GPU_DEVICE:-1}"]' in runtime_block
    assert 'NVIDIA_VISIBLE_DEVICES: "${FISH_TTS_GPU_DEVICE:-1}"' in runtime_block
    assert 'CUDA_VISIBLE_DEVICES: "0"' in runtime_block
    assert "depends_on:" not in runtime_block
    assert "stop_grace_period: 120s" in runtime_block
    assert "/app/references" not in init_block
    assert "fish_tts_references" not in compose
    assert "fishaudio/fish-speech:server-cuda" not in runtime_block
    assert "/app/tools/api_server.py" not in runtime_block
    assert "http://127.0.0.1:8091/health" in runtime_block


def test_fish_tts_compose_uses_one_model_worker_and_ten_batched_streams() -> None:
    compose = Path("deployment/compose/compose.services.yml").read_text(
        encoding="utf-8"
    )

    assert compose.count("  fish_tts:\n") == 1
    assert 'TTS_PROVIDER: "${TTS_PROVIDER:-fish_vllm_omni}"' in compose
    assert (
        'TTS_HTTP_URL: "${TTS_HTTP_URL:-http://fish_tts:8091/v1/audio/speech}"'
        in compose
    )
    assert 'TTS_MAX_CONCURRENCY: "${TTS_MAX_CONCURRENCY:-10}"' in compose
    assert (
        'TTS_DEFAULT_VOICE: "${TTS_DEFAULT_VOICE:-employee-natural}"'
        in compose
    )
    assert (
        'TTS_AVAILABLE_VOICES: "${TTS_AVAILABLE_VOICES:-employee-natural}"'
        in compose
    )
    assert (
        'TTS_REQUIRE_REGISTERED_VOICE: "${TTS_REQUIRE_REGISTERED_VOICE:-true}"'
        in compose
    )
    assert (
        'TTS_GLOBAL_MAX_CONCURRENCY: "${TTS_GLOBAL_MAX_CONCURRENCY:-10}"'
        in compose
    )
    assert 'TTS_QUEUE_TIMEOUT_SECONDS: "${TTS_QUEUE_TIMEOUT_SECONDS:-0}"' in compose
    assert 'TTS_STAGE_0_MAX_NUM_SEQS: "10"' in compose
    assert 'TTS_STAGE_1_MAX_NUM_SEQS: "10"' in compose
    assert 'TTS_EMOTION_TAG_MODE: "${TTS_EMOTION_TAG_MODE:-inline}"' in compose
    assert 'TTS_EMOTION_MAX_TAGS: "${TTS_EMOTION_MAX_TAGS:-2}"' in compose
    assert (
        'TTS_EMOTION_DELTA_THRESHOLD: "${TTS_EMOTION_DELTA_THRESHOLD:-0.10}"'
        in compose
    )
    assert (
        'TTS_LEGACY_EMOTION_TAG_DELIMITER: "${TTS_LEGACY_EMOTION_TAG_DELIMITER:-square}"'
        in compose
    )


def test_fish_tts_dynamic_batch_config_keeps_both_stages_batchable() -> None:
    config = Path("fish_tts/config/fish_s2_pro_dynamic_batch.yaml").read_text(
        encoding="utf-8"
    )

    assert config.count("max_num_seqs: 10") == 2
    assert "fish_speech_tensor_codes: true" in config
    assert "fish_speech_backlog_codec_chunk_frames: 25" in config
    assert "fish_speech_dac_dtype: fp16" in config
    assert "fish_speech_dac_max_padded_frames: 256" in config
    assert "fish_speech_dac_max_batch: 0" in config
    assert "connector_get_sleep_s: 0.001" in config


def test_tts_enabled_selects_the_default_fish_compose_topology() -> None:
    compose = Path("deployment/compose/compose.services.yml").read_text(
        encoding="utf-8"
    )
    mode = Path("deployment/compose/compose.mode.yml").read_text(encoding="utf-8")
    enabled = Path("deployment/compose/compose.tts.true.yml").read_text(
        encoding="utf-8"
    )
    disabled = Path("deployment/compose/compose.tts.false.yml").read_text(
        encoding="utf-8"
    )
    wrapper = Path("scripts/compose.sh").read_text(encoding="utf-8")
    env_example = Path("backend/config/.env.example").read_text(encoding="utf-8")

    runtime_block = compose.split("  fish_tts:", 1)[1].split(
        "  signoz_collection_agent:", 1
    )[0]
    assert 'profiles: ["tts"]' in runtime_block
    assert "compose.tts.${TTS_ENABLED:-true}.yml" in mode
    assert "profiles: !reset []" in enabled
    assert disabled.strip() == "services: {}"
    assert 'if [[ "${tts_enabled}" == "false" ]]' in wrapper
    assert "--profile tts rm --stop --force fish_tts" in wrapper
    assert "TTS_ENABLED=true" in env_example


@pytest.mark.asyncio
async def test_fish_tts_rejects_incomplete_wav_stream() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            stream=_ChunkedStream([b"RIFF"]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = FishTtsService(
            _settings(
                tts_provider="fish_legacy",
                tts_http_url="http://fish-tts.test/v1/tts",
                tts_model="fishaudio/s2-pro",
            ),
            client=client,
        )
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        text_queue.put_nowait("测试。")
        text_queue.put_nowait(None)

        with pytest.raises(TtsProtocolError, match="WAV 流缺少完整文件头"):
            async for _event in service.stream(text_queue, emotion_state=None):
                pass


class _Agent:
    async def stream_reply(self, *_args: object, **_kwargs: object):
        yield "我明白"
        yield "你的意思。"


class _SuccessfulTts:
    async def stream(self, text_queue, *_args: object, **_kwargs: object):
        while await text_queue.get() is not None:
            pass
        yield {"event": "speech_start", "sentence_index": 0}
        yield {"event": "speech_audio", "audio": "AAA=", "byte_length": 2}
        yield {"event": "speech_done"}


class _FailedTts:
    async def stream(self, *_args: object, **_kwargs: object):
        raise TtsError("语音服务测试失败。")
        yield  # pragma: no cover


class _FloodingAgent:
    async def stream_reply(self, *_args: object, **_kwargs: object):
        for _ in range(400):
            yield "字"


class _BlockingTts:
    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def ensure_ready(self) -> None:
        return None

    async def stream(self, *_args: object, **_kwargs: object):
        await self.gate.wait()
        yield {"event": "speech_done"}


class _UnavailableTts:
    def __init__(self) -> None:
        self.stream_called = False

    async def ensure_ready(self) -> None:
        raise TtsError("Fish Audio 尚未就绪，本轮仅显示文字。")

    async def stream(self, *_args: object, **_kwargs: object):
        self.stream_called = True
        yield  # pragma: no cover


class _VoiceTrackingTts:
    def __init__(self) -> None:
        self.voices: list[str | None] = []
        self.seeds: list[int | None] = []

    async def ensure_ready(self) -> None:
        return None

    def resolve_voice(self, voice: str | None) -> str:
        return str(voice or "employee-natural").strip().casefold()

    async def stream(self, text_queue, *_args: object, **kwargs: object):
        self.voices.append(kwargs.get("voice"))
        self.seeds.append(kwargs.get("seed"))
        while await text_queue.get() is not None:
            pass
        yield {"event": "speech_done"}


@pytest.mark.asyncio
async def test_rehearsal_speech_events_are_bound_to_session_and_stream() -> None:
    service = RehearsalService(settings=_settings(), tts_service=_SuccessfulTts())
    state = SessionState(session_id="session-output-a", setup_ready=True)

    events = [
        event
        async for event in service._stream_employee_reply(
            _Agent(),
            state,
            "请说明你的想法。",
            [],
            speech_enabled=True,
            speech_voice="Fish Audio 默认音色",
            session_id="session-output-a",
            speech_stream_id="speech-stream-output-a",
        )
    ]

    speech_events = [event for event in events if event["event"].startswith("speech_")]
    assert speech_events
    assert all(event["session_id"] == "session-output-a" for event in speech_events)
    assert all(
        event["speech_stream_id"] == "speech-stream-output-a"
        for event in speech_events
    )


@pytest.mark.asyncio
async def test_rehearsal_locks_one_voice_for_every_turn_in_the_session() -> None:
    tts = _VoiceTrackingTts()
    service = RehearsalService(settings=_settings(), tts_service=tts)
    state = SessionState(session_id="session-stable-voice", setup_ready=True)

    for requested_voice in ("Employee-Natural", "another-voice"):
        _ = [
            event
            async for event in service._stream_employee_reply(
                _Agent(),
                state,
                "请说明你的想法。",
                [],
                speech_enabled=True,
                speech_voice=requested_voice,
                session_id=state.session_id,
                speech_stream_id=f"speech-{requested_voice}",
            )
        ]

    assert state.rehearsal_context.speech_voice == "employee-natural"
    assert tts.voices == ["employee-natural", "employee-natural"]
    assert state.rehearsal_context.speech_seed is not None
    assert tts.seeds == [
        state.rehearsal_context.speech_seed,
        state.rehearsal_context.speech_seed,
    ]


def test_rehearsal_derives_stable_session_seed_when_setting_is_empty() -> None:
    service = RehearsalService(settings=_settings(tts_seed=None))
    first = SessionState(session_id="stable-seed-a", setup_ready=True)
    second = SessionState(session_id="stable-seed-b", setup_ready=True)

    first_seed = service._session_speech_seed(first)

    assert first_seed is not None
    assert service._session_speech_seed(first) == first_seed
    assert first.rehearsal_context.speech_seed == first_seed
    assert service._session_speech_seed(second) != first_seed


def test_rehearsal_migrates_removed_persisted_voice_to_fixed_default() -> None:
    settings = _settings(
        tts_default_voice="employee-natural",
        tts_available_voices="employee-natural",
        tts_random_voice_name="random-voice",
    )
    service = RehearsalService(
        settings=settings,
        tts_service=FishTtsService(settings, client=object()),
    )
    state = SessionState(session_id="session-legacy-voice", setup_ready=True)
    state.rehearsal_context.speech_voice = "random-voice"

    assert service._session_speech_voice(state, None) == "employee-natural"
    assert state.rehearsal_context.speech_voice == "employee-natural"


def test_rehearsal_does_not_hide_an_invalid_new_voice_request() -> None:
    settings = _settings(
        tts_default_voice="employee-natural",
        tts_available_voices="employee-natural",
        tts_random_voice_name="random-voice",
    )
    service = RehearsalService(
        settings=settings,
        tts_service=FishTtsService(settings, client=object()),
    )
    state = SessionState(session_id="session-invalid-new-voice", setup_ready=True)

    with pytest.raises(TtsConfigurationError):
        service._session_speech_voice(state, "missing-voice")


@pytest.mark.asyncio
async def test_rehearsal_speech_failure_keeps_complete_text_reply() -> None:
    service = RehearsalService(settings=_settings(), tts_service=_FailedTts())
    state = SessionState(session_id="tts-fallback", setup_ready=True)
    timing = RehearsalTurnTiming(
        session_id=state.session_id,
        transport="stream",
        speech_enabled=True,
        explicit_thinking_enabled=False,
    )

    events = [
        event
        async for event in service._stream_employee_reply(
            _Agent(),
            state,
            "请说明你的想法。",
            [],
            speech_enabled=True,
            speech_voice="Fish Audio 默认音色",
            session_id="tts-fallback",
            speech_stream_id="speech-stream-test-0001",
            timing=timing,
        )
    ]

    assert (
        "".join(event.get("text", "") for event in events if event["event"] == "delta")
        == "我明白你的意思。"
    )
    completed = next(event for event in events if event["event"] == "_reply_complete")
    assert completed["reply"] == "我明白你的意思。"
    speech_ready = next(event for event in events if event["event"] == "speech_stream_ready")
    assert speech_ready == {
        "event": "speech_stream_ready",
        "session_id": "tts-fallback",
        "speech_stream_id": "speech-stream-test-0001",
    }
    speech_error = next(event for event in events if event["event"] == "speech_error")
    assert speech_error["message"] == "语音服务测试失败。"
    assert speech_error["session_id"] == "tts-fallback"
    assert speech_error["speech_stream_id"] == "speech-stream-test-0001"
    stage_outcomes = {
        stage["name"]: stage["outcome"]
        for stage in timing.snapshot()["stages"]
    }
    assert stage_outcomes["employee_reply"] == "success"
    assert stage_outcomes["speech_synthesis"] == "error"


@pytest.mark.asyncio
async def test_rehearsal_rejects_unready_tts_before_opening_audio_stream() -> None:
    tts = _UnavailableTts()
    service = RehearsalService(settings=_settings(), tts_service=tts)
    state = SessionState(session_id="tts-unready", setup_ready=True)

    events = [
        event
        async for event in service._stream_employee_reply(
            _Agent(),
            state,
            "请说明你的想法。",
            [],
            speech_enabled=True,
            speech_voice="Fish Audio 默认音色",
            session_id="tts-unready",
            speech_stream_id="speech-stream-unready",
        )
    ]

    assert tts.stream_called is False
    assert any(event["event"] == "_reply_complete" for event in events)
    assert all(event["event"] != "speech_stream_ready" for event in events)
    speech_error = next(event for event in events if event["event"] == "speech_error")
    assert speech_error["message"] == "Fish Audio 尚未就绪，本轮仅显示文字。"


@pytest.mark.asyncio
async def test_rehearsal_stream_close_does_not_block_when_event_queue_is_full() -> None:
    service = RehearsalService(settings=_settings(tts_enabled=False))
    state = SessionState(session_id="queue-close", setup_ready=True)
    timing = RehearsalTurnTiming(
        session_id=state.session_id,
        transport="stream",
        speech_enabled=False,
        explicit_thinking_enabled=False,
    )
    stream = service._stream_employee_reply(
        _FloodingAgent(),
        state,
        "请继续。",
        [],
        speech_enabled=False,
        speech_voice=None,
        timing=timing,
    )

    assert (await anext(stream))["event"] == "delta"
    await asyncio.sleep(0.02)
    await asyncio.wait_for(stream.aclose(), timeout=0.5)

    outcomes = {
        stage["name"]: stage["outcome"]
        for stage in timing.snapshot()["stages"]
    }
    assert outcomes["employee_reply"] == "cancelled"
    assert outcomes["response_pipeline"] == "cancelled"


@pytest.mark.asyncio
async def test_rehearsal_close_after_text_cancels_unfinished_speech_pipeline() -> None:
    service = RehearsalService(settings=_settings(), tts_service=_BlockingTts())
    state = SessionState(session_id="speech-tail-close", setup_ready=True)
    timing = RehearsalTurnTiming(
        session_id=state.session_id,
        transport="stream",
        speech_enabled=True,
        explicit_thinking_enabled=False,
    )
    stream = service._stream_employee_reply(
        _Agent(),
        state,
        "请继续。",
        [],
        speech_enabled=True,
        speech_voice="Fish Audio 默认音色",
        timing=timing,
    )

    while (await anext(stream))["event"] != "_reply_complete":
        pass
    await asyncio.wait_for(stream.aclose(), timeout=0.5)

    outcomes = {
        stage["name"]: stage["outcome"]
        for stage in timing.snapshot()["stages"]
    }
    assert outcomes["employee_reply"] == "success"
    assert outcomes["speech_synthesis"] == "cancelled"
    assert outcomes["response_pipeline"] == "cancelled"


class _BlockingSentenceTtsService(FishTtsService):
    def __init__(
        self,
        settings: Settings,
        gate: asyncio.Event,
        *,
        coordinator: Any | None = None,
    ) -> None:
        super().__init__(settings, client=object(), coordinator=coordinator)
        self.gate = gate
        self.started = asyncio.Event()

    async def _stream_sentence(self, *_args: object, **_kwargs: object):
        self.started.set()
        await self.gate.wait()
        yield {
            "event": "speech_sentence_done",
            "sentence_index": int(_kwargs.get("sentence_index") or 0),
        }


async def _collect_speech(
    service: FishTtsService,
    text_queue: asyncio.Queue[str | None],
) -> list[dict[str, Any]]:
    return [event async for event in service.stream(text_queue, emotion_state=None)]


def _sentence_queue(text: str) -> asyncio.Queue[str | None]:
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    queue.put_nowait(text)
    queue.put_nowait(None)
    return queue


class _SharedGlobalTtsCoordinator:
    def __init__(self) -> None:
        self._slot = asyncio.Semaphore(1)
        self.calls: list[tuple[str, int, float]] = []
        self.active = 0
        self.max_active = 0

    @asynccontextmanager
    async def lease(
        self,
        resource: str,
        *,
        capacity: int,
        timeout_seconds: float,
    ):
        self.calls.append((resource, capacity, timeout_seconds))
        await self._slot.acquire()
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield None
        finally:
            self.active -= 1
            self._slot.release()


@pytest.mark.asyncio
async def test_fish_tts_global_admission_is_shared_across_service_instances() -> None:
    gate = asyncio.Event()
    coordinator = _SharedGlobalTtsCoordinator()
    settings = _settings(
        tts_max_concurrency=8,
        tts_global_max_concurrency=1,
        tts_queue_timeout_seconds=0,
    )
    first_service = _BlockingSentenceTtsService(
        settings,
        gate,
        coordinator=coordinator,
    )
    second_service = _BlockingSentenceTtsService(
        settings,
        gate,
        coordinator=coordinator,
    )

    first_task = asyncio.create_task(
        _collect_speech(first_service, _sentence_queue("first."))
    )
    await first_service.started.wait()
    second_task = asyncio.create_task(
        _collect_speech(second_service, _sentence_queue("second."))
    )
    await asyncio.sleep(0.03)

    assert not second_service.started.is_set()
    assert coordinator.max_active == 1
    assert coordinator.calls == [
        ("tts:fish:inference", 1, 0.0),
        ("tts:fish:inference", 1, 0.0),
    ]

    gate.set()
    first_events, second_events = await asyncio.gather(first_task, second_task)
    assert first_events[-1]["event"] == "speech_done"
    assert second_events[-1]["event"] == "speech_done"


@pytest.mark.asyncio
async def test_zero_disables_fish_tts_global_admission() -> None:
    gate = asyncio.Event()
    gate.set()
    coordinator = _SharedGlobalTtsCoordinator()
    service = _BlockingSentenceTtsService(
        _settings(tts_global_max_concurrency=0),
        gate,
        coordinator=coordinator,
    )

    events = await _collect_speech(service, _sentence_queue("no global queue."))

    assert events[-1]["event"] == "speech_done"
    assert coordinator.calls == []


@pytest.mark.asyncio
async def test_fish_tts_unbounded_admission_waits_for_a_compute_slot() -> None:
    gate = asyncio.Event()
    service = _BlockingSentenceTtsService(
        _settings(tts_max_concurrency=1, tts_queue_timeout_seconds=0),
        gate,
    )

    first_task = asyncio.create_task(_collect_speech(service, _sentence_queue("第一句。")))
    await service.started.wait()
    second_task = asyncio.create_task(_collect_speech(service, _sentence_queue("第二句。")))
    await asyncio.sleep(0.03)

    assert not second_task.done()
    gate.set()
    first_events, second_events = await asyncio.gather(first_task, second_task)
    assert first_events[-1]["event"] == "speech_done"
    assert second_events[-1]["event"] == "speech_done"


@pytest.mark.asyncio
async def test_fish_tts_retains_optional_queue_timeout() -> None:
    gate = asyncio.Event()
    service = _BlockingSentenceTtsService(
        _settings(tts_max_concurrency=1, tts_queue_timeout_seconds=0.02),
        gate,
    )

    first_task = asyncio.create_task(_collect_speech(service, _sentence_queue("第一句。")))
    await service.started.wait()
    with pytest.raises(TtsQueueTimeoutError, match="语音服务繁忙"):
        await _collect_speech(service, _sentence_queue("第二句。"))

    gate.set()
    assert (await first_task)[-1]["event"] == "speech_done"


def test_qwen_tts_compatibility_export_points_to_fish_service() -> None:
    assert QwenTtsService is FishTtsService


def test_fish_tts_uses_transition_direction_without_changing_voice_identity() -> None:
    service = FishTtsService(_settings(), client=object())
    softening = service._request_payload(
        "我愿意继续听具体安排。",
        emotion_state=EmotionState(
            current_anchor_id="guarded",
            current_vad=VADVector(valence=-0.3, arousal=0.5, dominance=0.25),
            last_vad_delta=VADVector(valence=0.12, arousal=-0.10),
            has_manager_response=True,
        ),
        voice="employee",
        seed=54321,
    )
    intensifying = service._request_payload(
        "我需要你解释这个判断。",
        emotion_state=EmotionState(
            current_anchor_id="guarded",
            current_vad=VADVector(valence=-0.3, arousal=0.5, dominance=0.25),
            last_vad_delta=VADVector(valence=-0.12, arousal=0.10),
            has_manager_response=True,
        ),
        voice="employee",
        seed=54321,
    )

    assert softening["voice"] == intensifying["voice"] == "employee"
    assert softening["seed"] == intensifying["seed"] == 54321
    # Valence transition words are internal state language, not useful Fish
    # delivery instructions.  A guarded primary cue remains stable instead of
    # being combined with a contradictory ``self-assured``/``animated`` cue.
    assert softening["input"] == "[guarded] 我愿意继续听具体安排。"
    assert intensifying["input"] == "[guarded] 我需要你解释这个判断。"


def test_fish_tts_inline_tags_are_bounded_and_can_add_one_compatible_delivery_cue() -> None:
    state = EmotionState(
        current_anchor_id="hopeful",
        previous_anchor_id="neutral",
        current_vad=VADVector(valence=0.3, arousal=0.2, dominance=0.1),
        last_vad_delta=VADVector(arousal=0.14),
        transition_intensity=0.2,
        has_manager_response=True,
    )

    tags = FishTtsService.emotion_tags(state)

    assert tags == ("hopeful", "excited tone")
    assert len(tags) <= 2
    assert "," not in FishTtsService.emotion_tag(state)


def test_fish_tts_emotion_modes_provide_neutral_baseline_and_legacy_rollback() -> None:
    state = EmotionState(
        current_anchor_id="angry",
        previous_anchor_id="neutral",
        current_vad=VADVector(valence=-0.8, arousal=0.8, dominance=0.8),
        transition_intensity=0.8,
        has_manager_response=True,
    )

    assert FishTtsService.style_text("测试。", None) == "测试。"
    assert FishTtsService.style_text("测试。", state, tag_mode="off") == "测试。"
    assert FishTtsService.style_text("测试。", state, tag_mode="legacy").startswith(
        "[angry, high energy, assertive]"
    )


@pytest.mark.asyncio
async def test_fish_tts_global_queue_timeout_releases_local_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "backend.services.fish_tts_service.log_metric",
        lambda name, **fields: metrics.append((name, fields)),
    )
    gate = asyncio.Event()
    gate.set()
    coordinator = _SharedGlobalTtsCoordinator()
    service = _BlockingSentenceTtsService(
        _settings(tts_max_concurrency=1, tts_queue_timeout_seconds=0.02),
        gate,
        coordinator=coordinator,
    )
    await coordinator._slot.acquire()
    try:
        with pytest.raises(TtsQueueTimeoutError, match="语音服务繁忙"):
            await asyncio.wait_for(
                _collect_speech(service, _sentence_queue("busy global queue.")),
                timeout=0.5,
            )
        assert not service._slots.locked()
        assert not service.started.is_set()
        assert coordinator.active == 0
        queue_metric = next(fields for name, fields in metrics if name == "tts.queue")
        assert queue_metric["status"] == "timeout"
        assert queue_metric["wait_phase"] == "global"
        assert queue_metric["global_queue_ms"] > queue_metric["local_queue_ms"]
        assert queue_metric["queue_ms"] == pytest.approx(
            queue_metric["global_queue_ms"] + queue_metric["local_queue_ms"],
            abs=0.002,
        )
    finally:
        coordinator._slot.release()

    events = await asyncio.wait_for(
        _collect_speech(service, _sentence_queue("capacity recovered.")),
        timeout=0.5,
    )
    assert events[-1]["event"] == "speech_done"
    assert coordinator.active == 0


@pytest.mark.asyncio
async def test_fish_tts_local_and_global_queues_share_one_deadline() -> None:
    gate = asyncio.Event()
    gate.set()
    coordinator = _SharedGlobalTtsCoordinator()
    service = _BlockingSentenceTtsService(
        _settings(tts_max_concurrency=1, tts_queue_timeout_seconds=0.3),
        gate,
        coordinator=coordinator,
    )
    await service._slots.acquire()
    await coordinator._slot.acquire()
    task = asyncio.create_task(
        _collect_speech(service, _sentence_queue("wait in both queues."))
    )
    local_released = False
    try:
        await asyncio.sleep(0.12)
        # Waiting for process capacity must not occupy a global lease or queue entry.
        assert coordinator.calls == []
        service._slots.release()
        local_released = True
        await asyncio.sleep(0)
        assert len(coordinator.calls) == 1
        assert 0 < coordinator.calls[0][2] < 0.22
        # The fake coordinator ignores its timeout; the outer deadline must still
        # expire within the remaining budget rather than restart after local wait.
        with pytest.raises(TtsQueueTimeoutError):
            await asyncio.wait_for(task, timeout=0.24)
        assert not service._slots.locked()
        assert not service.started.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not local_released:
            service._slots.release()
        coordinator._slot.release()

    events = await asyncio.wait_for(
        _collect_speech(service, _sentence_queue("next request.")),
        timeout=0.5,
    )
    assert events[-1]["event"] == "speech_done"


@pytest.mark.asyncio
async def test_fish_tts_coordinator_timeout_is_a_queue_timeout() -> None:
    class TimeoutCoordinator:
        @asynccontextmanager
        async def lease(self, *_args: object, **_kwargs: object):
            raise TimeoutError("distributed queue deadline expired")
            yield  # pragma: no cover

    gate = asyncio.Event()
    gate.set()
    service = _BlockingSentenceTtsService(
        _settings(tts_max_concurrency=1),
        gate,
        coordinator=TimeoutCoordinator(),
    )

    with pytest.raises(TtsQueueTimeoutError) as caught:
        await _collect_speech(service, _sentence_queue("distributed timeout."))

    assert isinstance(caught.value.__cause__, TimeoutError)
    assert not service._slots.locked()
    assert not service.started.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["local", "global", "generation"])
async def test_fish_tts_cancellation_releases_admission_capacity(phase: str) -> None:
    gate = asyncio.Event()
    coordinator = _SharedGlobalTtsCoordinator()
    service = _BlockingSentenceTtsService(
        _settings(tts_max_concurrency=1, tts_queue_timeout_seconds=0),
        gate,
        coordinator=coordinator,
    )
    if phase == "local":
        await service._slots.acquire()
    elif phase == "global":
        await coordinator._slot.acquire()
    task = asyncio.create_task(
        _collect_speech(service, _sentence_queue("cancel this request."))
    )
    try:
        if phase == "generation":
            await asyncio.wait_for(service.started.wait(), timeout=0.5)
            assert coordinator.active == 1
        else:
            await asyncio.sleep(0)
            assert not service.started.is_set()
            assert len(coordinator.calls) == (0 if phase == "local" else 1)
        assert service._slots.locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert coordinator.active == 0
        assert service._slots.locked() is (phase == "local")
        assert coordinator._slot.locked() is (phase == "global")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if phase == "local":
            service._slots.release()
        elif phase == "global":
            coordinator._slot.release()

    gate.set()
    events = await asyncio.wait_for(
        _collect_speech(service, _sentence_queue("capacity recovered.")),
        timeout=0.5,
    )
    assert events[-1]["event"] == "speech_done"
    assert not service._slots.locked()
    assert coordinator.active == 0


@pytest.mark.asyncio
async def test_fish_tts_queue_deadline_does_not_limit_generation() -> None:
    gate = asyncio.Event()
    coordinator = _SharedGlobalTtsCoordinator()
    service = _BlockingSentenceTtsService(
        _settings(tts_max_concurrency=1, tts_queue_timeout_seconds=0.02),
        gate,
        coordinator=coordinator,
    )
    task = asyncio.create_task(
        _collect_speech(service, _sentence_queue("longer synthesis."))
    )
    try:
        await asyncio.wait_for(service.started.wait(), timeout=0.5)
        await asyncio.sleep(0.04)
        assert not task.done()
        assert coordinator.active == 1
        gate.set()
        assert (await asyncio.wait_for(task, timeout=0.5))[-1]["event"] == "speech_done"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert not service._slots.locked()
    assert coordinator.active == 0


@pytest.mark.asyncio
async def test_fish_tts_stream_close_releases_both_admission_slots() -> None:
    gate = asyncio.Event()
    gate.set()
    coordinator = _SharedGlobalTtsCoordinator()
    service = _BlockingSentenceTtsService(
        _settings(tts_max_concurrency=1),
        gate,
        coordinator=coordinator,
    )
    stream = service.stream(_sentence_queue("close after an event."), emotion_state=None)
    try:
        assert (await anext(stream))["event"] == "speech_sentence_done"
        assert service._slots.locked()
        assert coordinator.active == 1
    finally:
        await stream.aclose()

    assert not service._slots.locked()
    assert coordinator.active == 0
    events = await asyncio.wait_for(
        _collect_speech(service, _sentence_queue("next request.")),
        timeout=0.5,
    )
    assert events[-1]["event"] == "speech_done"

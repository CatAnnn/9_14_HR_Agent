from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
import json
import logging
import struct
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from backend.config.settings import Settings, get_settings
from backend.observability.metrics import log_metric
from backend.redis.distributed_coordination import RedisDistributedCoordinator
from backend.schemas.simulation import EmotionState
from backend.services.http_client import get_shared_async_client

if TYPE_CHECKING:
    from backend.services.speech_playback_service import SpeechPlaybackBuffer


logger = logging.getLogger(__name__)

_MAX_WAV_HEADER_BYTES = 65_536
_MAX_UPSTREAM_ERROR_BYTES = 4_096
_MAX_UPSTREAM_ERROR_CHARS = 240
_READINESS_SUCCESS_CACHE_SECONDS = 30.0
_READINESS_FAILURE_CACHE_SECONDS = 1.0
_SENTENCE_ENDINGS = frozenset("。！？!?；;\n.")
_SOFT_BREAKS = frozenset("，,、：: ")
_VLLM_OMNI_PROVIDER = "fish_vllm_omni"
_VLLM_PCM_CONTENT_TYPES = frozenset({"audio/pcm", "application/octet-stream"})
_GLOBAL_INFERENCE_RESOURCE = "tts:fish:inference"
_LEGACY_QWEN_VOICES = frozenset(
    voice.casefold() for voice in ("Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric")
)


class TtsError(RuntimeError):
    """Base error for local speech synthesis."""


class TtsConfigurationError(TtsError):
    """The request cannot be represented by the configured TTS service."""


class TtsQueueTimeoutError(TtsError):
    """An explicitly configured TTS queue deadline expired."""


class TtsProtocolError(TtsError):
    """The local TTS server returned an invalid or incomplete stream."""


class _StreamingWavPcmDecoder:
    """Decode Fish streams that may contain a WAV header or bare PCM."""

    def __init__(self, fallback_sample_rate: int) -> None:
        self._buffer = bytearray()
        self._fallback_sample_rate = fallback_sample_rate
        self._format_decided = False
        self._header_complete = False
        self.sample_rate: int | None = None

    def feed(self, data: bytes) -> list[bytes]:
        if data:
            self._buffer.extend(data)
        if not self._format_decided:
            if len(self._buffer) < 4:
                return []
            if self._buffer[:4] != b"RIFF":
                self._format_decided = True
                self._header_complete = True
                self.sample_rate = self._fallback_sample_rate
            elif len(self._buffer) < 12:
                return []
            else:
                self._format_decided = True

        if not self._header_complete:
            header_end = self._find_header_end()
            if header_end is None:
                return []
            del self._buffer[:header_end]
            self._header_complete = True

        aligned_size = len(self._buffer) - (len(self._buffer) % 2)
        if aligned_size <= 0:
            return []
        pcm = bytes(self._buffer[:aligned_size])
        del self._buffer[:aligned_size]
        return [pcm]

    def finish(self) -> None:
        if not self._header_complete:
            raise TtsProtocolError("Fish Audio 返回的 WAV 流缺少完整文件头。")
        if self._buffer:
            raise TtsProtocolError("Fish Audio 返回了未对齐的 PCM 音频数据。")

    def _find_header_end(self) -> int | None:
        if len(self._buffer) < 12:
            return None
        if self._buffer[:4] != b"RIFF" or self._buffer[8:12] != b"WAVE":
            raise TtsProtocolError("Fish Audio 返回的流不是有效 WAV 音频。")

        offset = 12
        while True:
            if offset + 8 > len(self._buffer):
                return None
            chunk_id = bytes(self._buffer[offset : offset + 4])
            chunk_size = int.from_bytes(
                self._buffer[offset + 4 : offset + 8],
                "little",
            )
            body_start = offset + 8
            body_end = body_start + chunk_size

            if chunk_id == b"data":
                if self.sample_rate is None:
                    raise TtsProtocolError("Fish Audio WAV 流缺少 fmt 音频参数。")
                return body_start

            if body_end > _MAX_WAV_HEADER_BYTES:
                raise TtsProtocolError("Fish Audio WAV 文件头异常过长。")
            if body_end > len(self._buffer):
                return None

            if chunk_id == b"fmt ":
                if chunk_size < 16:
                    raise TtsProtocolError("Fish Audio WAV fmt 参数不完整。")
                audio_format, channels, sample_rate = struct.unpack_from(
                    "<HHI",
                    self._buffer,
                    body_start,
                )
                bits_per_sample = struct.unpack_from(
                    "<H",
                    self._buffer,
                    body_start + 14,
                )[0]
                if audio_format != 1 or channels != 1 or bits_per_sample != 16:
                    raise TtsProtocolError(
                        "Fish Audio 必须返回 16-bit 单声道 PCM WAV 音频。"
                    )
                if not 8_000 <= sample_rate <= 96_000:
                    raise TtsProtocolError("Fish Audio 返回了无效采样率。")
                self.sample_rate = sample_rate

            offset = body_end + (chunk_size % 2)
            if offset > _MAX_WAV_HEADER_BYTES:
                raise TtsProtocolError("Fish Audio WAV 文件头异常过长。")


class FishTtsService:
    """Fish Audio client with unbounded admission and bounded GPU inference."""

    _ANCHOR_TAGS = {
        "surprised": "surprised",
        "pleased": "happy and satisfied",
        "engaged": "engaged and energetic",
        "confident": "confident and firm",
        "relieved": "relieved",
        "calm": "calm",
        "neutral": "neutral and natural",
        "tired": "tired",
        "discouraged": "discouraged",
        "sad": "sad",
        "disappointed": "disappointed",
        "anxious": "anxious",
        "confused": "confused and hesitant",
        "guarded": "guarded and defensive",
        "angry": "angry",
        "resentful": "resentful",
        "hopeful": "hopeful and cautiously optimistic",
        "proud": "proud and assured",
        "appreciative": "appreciative and warm",
        "inspired": "inspired and energized",
        "curious": "curious and interested",
        "determined": "determined and resolute",
        "trusting": "trusting and open",
        "grateful": "grateful and sincere",
        "accomplished": "fulfilled and accomplished",
        "eager": "eager and upbeat",
        "empowered": "empowered and capable",
        "attentive": "attentive and focused",
        "reflective": "reflective and thoughtful",
        "receptive": "receptive and open-minded",
        "skeptical": "skeptical and measured",
        "cautious": "cautious and restrained",
        "uncertain": "uncertain and hesitant",
        "ambivalent": "conflicted and ambivalent",
        "evaluating": "evaluative and deliberate",
        "composed": "composed and steady",
        "alert": "alert and watchful",
        "reserved": "reserved and restrained",
        "detached": "detached and subdued",
        "frustrated": "frustrated and strained",
        "irritated": "irritated and terse",
        "aggrieved": "hurt and aggrieved",
        "embarrassed": "embarrassed and uneasy",
        "ashamed": "ashamed and subdued",
        "guilty": "guilty and remorseful",
        "overwhelmed": "overwhelmed and strained",
        "afraid": "afraid and tense",
        "helpless": "helpless and subdued",
    }

    # Fish S2-Pro accepts natural-language cues in square brackets.  Keep the
    # cue itself short and make it describe one primary affect.  The previous
    # implementation put the anchor, transition direction, arousal and
    # dominance into one comma-separated bracket.  That is ambiguous to the
    # model (and expensive to repeat for every sentence), especially when the
    # axes point in different directions.  These values intentionally stay
    # independent from the internal Chinese expression guidance: that
    # guidance is for the text model and must never be sent to Fish verbatim.
    _INLINE_ANCHOR_TAGS: dict[str, str | None] = {
        "surprised": "surprised",
        "pleased": "satisfied",
        "engaged": "engaged",
        "confident": "confident",
        "relieved": "relieved",
        "calm": "calm",
        "neutral": None,
        "tired": "tired",
        "discouraged": "discouraged",
        "sad": "sad",
        "disappointed": "disappointed",
        "anxious": "anxious",
        "confused": "confused",
        "guarded": "guarded",
        "angry": "angry",
        "resentful": "resentful",
        "hopeful": "hopeful",
        "proud": "proud",
        "appreciative": "appreciative",
        "inspired": "inspired",
        "curious": "curious",
        "determined": "determined",
        "trusting": "trusting",
        "grateful": "grateful",
        "accomplished": "accomplished",
        "eager": "eager",
        "empowered": "empowered",
        "attentive": "focused",
        "reflective": "thoughtful",
        "receptive": "receptive",
        "skeptical": "skeptical",
        "cautious": "cautious",
        "uncertain": "uncertain",
        "ambivalent": "ambivalent",
        "evaluating": "deliberate",
        "composed": "composed",
        "alert": "alert",
        "reserved": "reserved",
        "detached": "indifferent",
        "frustrated": "frustrated",
        "irritated": "irritated",
        "aggrieved": "aggrieved",
        "embarrassed": "embarrassed",
        "ashamed": "ashamed",
        "guilty": "guilty",
        "overwhelmed": "overwhelmed",
        "afraid": "nervous",
        "helpless": "helpless",
    }

    # A delivery cue is only added for a material VAD movement and only when
    # the primary anchor does not already express that axis.  This prevents
    # combinations such as "guarded + self-assured" or "sad + high energy".
    _HIGH_AROUSAL_ANCHORS = frozenset(
        {
            "surprised",
            "engaged",
            "inspired",
            "eager",
            "alert",
            "angry",
            "frustrated",
            "irritated",
            "anxious",
            "overwhelmed",
            "afraid",
        }
    )
    _LOW_AROUSAL_ANCHORS = frozenset(
        {
            "relieved",
            "calm",
            "tired",
            "discouraged",
            "sad",
            "detached",
            "reflective",
            "reserved",
            "ashamed",
            "guilty",
            "helpless",
        }
    )
    _HIGH_DOMINANCE_ANCHORS = frozenset(
        {"confident", "proud", "determined", "empowered", "angry", "resentful"}
    )
    _LOW_DOMINANCE_ANCHORS = frozenset(
        {
            "confused",
            "uncertain",
            "ambivalent",
            "discouraged",
            "sad",
            "anxious",
            "cautious",
            "embarrassed",
            "ashamed",
            "guilty",
            "overwhelmed",
            "afraid",
            "helpless",
            "tired",
        }
    )

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        coordinator: RedisDistributedCoordinator | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_client = False
        self._client = client or get_shared_async_client("speech")
        self.coordinator = coordinator
        self._slots = asyncio.BoundedSemaphore(self.settings.tts_max_concurrency)
        self._readiness_lock = asyncio.Lock()
        self._readiness_checked_at: float | None = None
        self._readiness_value = False
        self._readiness_configuration_error: str | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def provider(self) -> str:
        return self.settings.tts_provider

    @property
    def uses_vllm_omni(self) -> bool:
        return self.provider == _VLLM_OMNI_PROVIDER

    @property
    def health_url(self) -> str:
        if self.uses_vllm_omni:
            parsed = urlsplit(self.settings.tts_http_url)
            return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))
        return f"{self.settings.tts_http_url.rsplit('/', 1)[0]}/health"

    @property
    def voices_url(self) -> str:
        parsed = urlsplit(self.settings.tts_http_url)
        return urlunsplit(
            (parsed.scheme, parsed.netloc, "/v1/audio/voices", "", "")
        )

    @property
    def _readiness_cache_seconds(self) -> float:
        return (
            _READINESS_SUCCESS_CACHE_SECONDS
            if self._readiness_value
            else _READINESS_FAILURE_CACHE_SECONDS
        )

    def _required_registered_voice(self) -> tuple[str | None, str | None]:
        if not (
            self.uses_vllm_omni
            and self.settings.tts_require_registered_voice
        ):
            return None, None

        voice = self.settings.tts_default_voice.strip()
        random_alias = self.settings.tts_random_voice_name.strip()
        if not voice:
            return None, (
                "TTS_REQUIRE_REGISTERED_VOICE 已启用，但 TTS_DEFAULT_VOICE 为空；"
                "请先注册固定员工音色。"
            )
        if voice.casefold() in {"default", random_alias.casefold()}:
            return None, (
                f"TTS_DEFAULT_VOICE={voice!r} 是没有固定声纹的默认随机音色；"
                "请先注册固定员工音色并将 TTS_DEFAULT_VOICE 指向该名称。"
            )
        return voice, None

    @staticmethod
    def _uploaded_voice_names(payload: object) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        uploaded = payload.get("uploaded_voices")
        if not isinstance(uploaded, list):
            return set()
        return {
            str(item.get("name") or "").strip().casefold()
            for item in uploaded
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }

    async def is_ready(self, *, force: bool = False) -> bool:
        if not self.settings.tts_enabled:
            self._readiness_checked_at = time.monotonic()
            self._readiness_value = False
            return False
        now = time.monotonic()
        if (
            not force
            and self._readiness_checked_at is not None
            and now - self._readiness_checked_at < self._readiness_cache_seconds
        ):
            return self._readiness_value

        async with self._readiness_lock:
            now = time.monotonic()
            if (
                not force
                and self._readiness_checked_at is not None
                and now - self._readiness_checked_at < self._readiness_cache_seconds
            ):
                return self._readiness_value

            timeout = httpx.Timeout(
                max(1.0, float(self.settings.tts_connect_timeout_seconds)),
                connect=max(1.0, float(self.settings.tts_connect_timeout_seconds)),
            )
            self._readiness_configuration_error = None
            required_voice, configuration_error = self._required_registered_voice()
            if configuration_error is not None:
                self._readiness_checked_at = time.monotonic()
                self._readiness_value = False
                self._readiness_configuration_error = configuration_error
                return False
            try:
                response = await self._client.get(self.health_url, timeout=timeout)
                ready = 200 <= response.status_code < 300
                if ready and required_voice is not None:
                    voices_response = await self._client.get(
                        self.voices_url,
                        timeout=timeout,
                    )
                    ready = 200 <= voices_response.status_code < 300
                    if ready:
                        try:
                            voice_names = self._uploaded_voice_names(
                                voices_response.json()
                            )
                        except (TypeError, ValueError):
                            ready = False
                        else:
                            ready = required_voice.casefold() in voice_names
                            if not ready:
                                self._readiness_configuration_error = (
                                    f"配置的固定员工音色 {required_voice!r} 尚未在 "
                                    "Fish Audio 注册；请先通过 /v1/audio/voices "
                                    "注册该音色。"
                                )
            except (httpx.HTTPError, OSError):
                ready = False
            self._readiness_checked_at = time.monotonic()
            self._readiness_value = ready
            return ready

    async def ensure_ready(self) -> None:
        if not self.settings.tts_enabled:
            raise TtsConfigurationError("员工语音输出已通过 TTS_ENABLED 关闭。")
        if not await self.is_ready():
            if self._readiness_configuration_error is not None:
                raise TtsConfigurationError(self._readiness_configuration_error)
            raise TtsError("Fish Audio 尚未就绪，本轮仅显示文字。")

    def _validate_resolved_voice(self, voice: str) -> str:
        if not (
            self.uses_vllm_omni
            and self.settings.tts_require_registered_voice
        ):
            return voice

        normalized = voice.strip().casefold()
        random_alias = self.settings.tts_random_voice_name.strip().casefold()
        if normalized in {"default", random_alias}:
            raise TtsConfigurationError(
                f"语音声线 {voice!r} 没有固定声纹；"
                "当前配置只允许已注册的员工音色。"
            )
        return voice

    def resolve_voice(self, requested_voice: str | None) -> str:
        available = self.settings.tts_voice_options
        requested = (requested_voice or self.settings.tts_default_voice).strip()
        by_lower_name = {voice.casefold(): voice for voice in available}
        resolved = by_lower_name.get(requested.casefold())
        if resolved is not None:
            return self._validate_resolved_voice(resolved)

        random_voice = by_lower_name.get(
            self.settings.tts_random_voice_name.strip().casefold()
        )
        if requested.casefold() in _LEGACY_QWEN_VOICES and random_voice is not None:
            return self._validate_resolved_voice(random_voice)
        raise TtsConfigurationError(
            f"不支持的语音声线 {requested!r}，可选值：{', '.join(available)}"
        )

    def reference_id_for_voice(self, voice: str) -> str | None:
        if voice.casefold() == self.settings.tts_random_voice_name.strip().casefold():
            return None
        return voice

    def upstream_voice_for_voice(self, voice: str) -> str:
        if voice.casefold() == self.settings.tts_random_voice_name.strip().casefold():
            return "default"
        return voice

    @classmethod
    def _legacy_emotion_tag(cls, emotion_state: EmotionState | None) -> str:
        """Render the pre-inline strategy for a controlled rollback/A-B run."""

        if emotion_state is None:
            return "[natural and clear]"

        vad = emotion_state.current_vad
        anchor_id = (emotion_state.current_anchor_id or "neutral").strip().lower()
        descriptors = [cls._ANCHOR_TAGS.get(anchor_id, cls._ANCHOR_TAGS["neutral"])]

        delta = emotion_state.last_vad_delta
        direction_changes = sorted(
            (
                (
                    abs(delta.valence),
                    "softening" if delta.valence > 0 else "more upset",
                ),
                (
                    abs(delta.arousal),
                    "rising intensity" if delta.arousal > 0 else "calming down",
                ),
                (
                    abs(delta.dominance),
                    "growing firmer" if delta.dominance > 0 else "less certain",
                ),
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        descriptors.extend(
            label for magnitude, label in direction_changes[:1] if magnitude >= 0.08
        )
        if vad.arousal >= 0.55:
            descriptors.append("high energy")
        elif vad.arousal >= 0.2:
            descriptors.append("animated")
        elif vad.arousal <= -0.55:
            descriptors.append("slow and subdued")
        elif vad.arousal <= -0.2:
            descriptors.append("measured and restrained")
        if vad.dominance >= 0.55:
            descriptors.append("assertive")
        elif vad.dominance >= 0.2:
            descriptors.append("self-assured")
        elif vad.dominance <= -0.55:
            descriptors.append("uncertain")
        elif vad.dominance <= -0.2:
            descriptors.append("tentative")

        return f"[{', '.join(dict.fromkeys(descriptors))}]"

    @classmethod
    def _inline_delivery_tag(
        cls,
        emotion_state: EmotionState,
        *,
        anchor_id: str,
        delta_threshold: float,
    ) -> str | None:
        """Return at most one compatible delivery cue for a VAD movement.

        The cue describes how to deliver the already-selected text.  It never
        exposes the transition's internal direction words (for example,
        ``more upset``), which are not useful instructions to a TTS model.
        Valence is intentionally not mapped to a second cue: the primary
        anchor is the least ambiguous carrier of that information.
        """

        threshold = max(0.0, min(float(delta_threshold), 1.0))
        delta = emotion_state.last_vad_delta
        vad = emotion_state.current_vad
        candidates = (
            (abs(delta.arousal), "arousal"),
            (abs(delta.dominance), "dominance"),
        )
        magnitude, axis = max(candidates, key=lambda item: item[0])
        # Treat the configured value as a strict material-change boundary.
        # A delta exactly at the boundary is common quantisation noise from
        # the transition model; adding a second cue for it makes a guarded
        # reply sound artificially animated.  Only a change *above* the
        # boundary earns an extra delivery instruction.
        if magnitude <= threshold:
            return None

        if axis == "arousal":
            if delta.arousal > 0 and vad.arousal >= 0.15:
                if anchor_id in cls._HIGH_AROUSAL_ANCHORS or vad.arousal >= 0.55:
                    return None
                return "excited tone"
            if delta.arousal < 0 and vad.arousal <= -0.15:
                if anchor_id in cls._LOW_AROUSAL_ANCHORS or vad.arousal <= -0.55:
                    return None
                return "low voice"
            return None

        if delta.dominance > 0 and vad.dominance >= 0.15:
            if anchor_id in cls._HIGH_DOMINANCE_ANCHORS or vad.dominance >= 0.55:
                return None
            return "firm tone"
        if delta.dominance < 0 and vad.dominance <= -0.15:
            if anchor_id in cls._LOW_DOMINANCE_ANCHORS or vad.dominance <= -0.55:
                return None
            return "hesitant tone"
        return None

    @staticmethod
    def _material_emotion_transition(
        emotion_state: EmotionState,
        *,
        threshold: float,
    ) -> bool:
        """Whether this state represents a reaction worth voicing.

        ``EmotionState.current_anchor_id`` is also used as a continuous
        personality/continuity baseline.  It is therefore not safe to send
        the anchor to Fish merely because a manager turn has happened: a
        neutral greeting can otherwise make a high-neuroticism employee sound
        anxious before the employee has said anything emotional.  Require a
        material realised VAD movement, or a material stimulus that actually
        changed the categorical anchor.  This is deliberately a TTS-side
        guard; the internal emotion state remains intact for text generation.
        """

        boundary = max(0.05, min(float(threshold), 1.0))
        delta = emotion_state.last_vad_delta
        realised_change = max(
            abs(delta.valence),
            abs(delta.arousal),
            abs(delta.dominance),
        )
        if realised_change > boundary:
            return True

        previous = (emotion_state.previous_anchor_id or "").strip().casefold()
        current = (emotion_state.current_anchor_id or "").strip().casefold()
        if not previous:
            # Older persisted states did not store the previous anchor.  Treat
            # that metadata as unknown and fail closed: an unknown state must
            # not turn a personality-adjusted baseline into an audible cue.
            # New transition states always carry ``previous_anchor_id`` and
            # therefore can pass the strict material-change gate above.
            return False
        return bool(
            current
            and previous != current
            and emotion_state.transition_intensity > boundary
        )

    @classmethod
    def emotion_tags(
        cls,
        emotion_state: EmotionState | None,
        *,
        mode: str = "inline",
        max_tags: int = 2,
        delta_threshold: float = 0.10,
        include_delivery_cue: bool = True,
    ) -> tuple[str, ...]:
        """Return bounded Fish inline cues without leaking internal guidance.

        ``inline`` is the production strategy: one concise primary cue and,
        only for a material compatible VAD movement, one delivery cue.
        ``legacy`` is retained for an explicit A/B or rollback. ``off`` is a
        useful neutral acoustic baseline. Unknown values fail open to inline so
        a bad environment value cannot make a reply unusable.
        """

        normalized_mode = str(mode or "inline").strip().casefold()
        if normalized_mode == "off":
            return ()

        if emotion_state is None:
            if normalized_mode == "legacy":
                # Keep the old explicit rollback baseline available to callers
                # that deliberately request it; production ``inline`` remains
                # silent for a neutral/no-state request.
                legacy = cls._legacy_emotion_tag(None)
                return (legacy[1:-1],) if legacy.startswith("[") else (legacy,)
            return ()

        # Both production and rollback modes must respect the same boundary:
        # an internal personality baseline or an unchanged state after a
        # manager turn is not, by itself, an instruction to tint the audio.
        if (
            not emotion_state.has_manager_response
            or not cls._material_emotion_transition(
                emotion_state,
                threshold=delta_threshold,
            )
        ):
            return ()

        if normalized_mode == "legacy":
            legacy = cls._legacy_emotion_tag(emotion_state)
            return (legacy[1:-1],) if legacy.startswith("[") else (legacy,)

        # The initial state is a personality-adjusted baseline, not a reaction
        # to anything the manager has said.  Voicing that baseline would make
        # the first employee turn sound arbitrarily anxious/confident and
        # would leak an internal state that was never selected for expression.
        # ``has_manager_response`` is set by the transition service only after
        # a real manager turn has been processed.
        anchor_id = (emotion_state.current_anchor_id or "").strip().casefold()
        primary = cls._INLINE_ANCHOR_TAGS.get(anchor_id)
        if not primary:
            return ()

        # Keep the hard safety bound in this class even if a caller bypasses
        # Settings.  Two cues are enough for S2-Pro and avoid reintroducing the
        # former multi-axis comma list through a caller override.
        limit = max(1, min(int(max_tags), 2))
        tags: list[str] = [primary]
        if limit > 1 and include_delivery_cue:
            delivery = cls._inline_delivery_tag(
                emotion_state,
                anchor_id=anchor_id,
                delta_threshold=delta_threshold,
            )
            if delivery and delivery not in tags:
                tags.append(delivery)
        return tuple(tags[:limit])

    @classmethod
    def emotion_tag(
        cls,
        emotion_state: EmotionState | None,
        *,
        mode: str = "inline",
        max_tags: int = 2,
        delta_threshold: float = 0.10,
        include_delivery_cue: bool = True,
    ) -> str:
        """Render inline cues at the beginning of a Fish input string."""

        tags = cls.emotion_tags(
            emotion_state,
            mode=mode,
            max_tags=max_tags,
            delta_threshold=delta_threshold,
            include_delivery_cue=include_delivery_cue,
        )
        normalized_mode = str(mode or "inline").strip().casefold()
        if normalized_mode == "legacy":
            return cls._legacy_emotion_tag(emotion_state) if tags else ""
        return " ".join(f"[{tag}]" for tag in tags)

    @classmethod
    def style_text(
        cls,
        text: str,
        emotion_state: EmotionState | None,
        *,
        tag_mode: str = "inline",
        max_tags: int = 2,
        delta_threshold: float = 0.10,
        tag_delimiter: str = "square",
        include_delivery_cue: bool = True,
    ) -> str:
        clean_text = text.strip()
        tag = cls.emotion_tag(
            emotion_state,
            mode=tag_mode,
            max_tags=max_tags,
            delta_threshold=delta_threshold,
            include_delivery_cue=include_delivery_cue,
        )
        if tag and str(tag_delimiter).strip().casefold() == "parentheses":
            # Fish Speech S1/legacy parses natural-language controls in
            # parentheses, while S2-Pro/vLLM-Omni uses square brackets.  Only
            # rewrite the generated prefix; never touch user text.
            tag = tag.replace("[", "(").replace("]", ")")
        return f"{tag} {clean_text}".strip() if tag else clean_text

    def _emotion_metric_fields(
        self,
        emotion_state: EmotionState | None,
        *,
        include_delivery_cue: bool = True,
    ) -> dict[str, Any]:
        """Build low-cardinality diagnostics for the selected Fish cues.

        We intentionally record the anchor and cue count, not the employee
        text or the internal expression guidance.  This makes it possible to
        compare ``inline``/``legacy``/``off`` runs without putting sensitive
        conversation content into the metrics stream.
        """

        tags = self.emotion_tags(
            emotion_state,
            mode=self.settings.tts_emotion_tag_mode,
            max_tags=self.settings.tts_emotion_max_tags,
            delta_threshold=self.settings.tts_emotion_delta_threshold,
            include_delivery_cue=include_delivery_cue,
        )
        return {
            "emotion_tag_mode": self.settings.tts_emotion_tag_mode,
            "emotion_anchor": (
                (emotion_state.current_anchor_id or "neutral").strip().casefold()
                if emotion_state is not None
                else "none"
            ),
            "emotion_tag_count": len(tags),
            "emotion_primary_tag": tags[0] if tags else None,
        }

    @asynccontextmanager
    async def _global_inference_lease(
        self,
        *,
        timeout_seconds: float,
    ) -> AsyncIterator[Any | None]:
        capacity = int(self.settings.tts_global_max_concurrency)
        if self.coordinator is None or capacity <= 0:
            yield None
            return
        async with self.coordinator.lease(
            _GLOBAL_INFERENCE_RESOURCE,
            capacity=capacity,
            timeout_seconds=timeout_seconds,
        ) as slot:
            yield slot

    @asynccontextmanager
    async def _inference_slot(
        self,
        *,
        sentence_index: int,
        queue_stats: dict[str, float] | None,
    ) -> AsyncIterator[None]:
        wait_started = time.perf_counter()
        local_wait_finished: float | None = None
        distributed_slot: Any | None = None
        queue_status = "failed"
        wait_phase = "local"
        queue_timeout = float(self.settings.tts_queue_timeout_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + queue_timeout if queue_timeout > 0 else None

        # Register cleanup outside the deadline so cancellation cannot skip release.
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout_at(deadline):
                    await stack.enter_async_context(self._slots)
                    local_wait_finished = time.perf_counter()
                    wait_phase = "global"
                    remaining = 0.0 if deadline is None else deadline - loop.time()
                    if deadline is not None and remaining <= 0:
                        raise TimeoutError
                    distributed_slot = await stack.enter_async_context(
                        self._global_inference_lease(timeout_seconds=remaining)
                    )
                queue_status = "acquired"
            except TimeoutError as exc:
                queue_status = "timeout"
                raise TtsQueueTimeoutError(
                    "语音服务繁忙，本轮仅显示文字。"
                ) from exc
            except asyncio.CancelledError:
                queue_status = "cancelled"
                raise
            finally:
                wait_finished = time.perf_counter()
                queue_ms = (wait_finished - wait_started) * 1000
                local_queue_ms = (
                    (local_wait_finished or wait_finished) - wait_started
                ) * 1000
                global_queue_ms = (
                    (wait_finished - local_wait_finished) * 1000
                    if local_wait_finished is not None
                    else 0.0
                )
                if queue_stats is not None:
                    queue_stats["total_ms"] += queue_ms
                    queue_stats["max_ms"] = max(queue_stats["max_ms"], queue_ms)
                    queue_stats["wait_count"] += 1
                log_metric(
                    "tts.queue",
                    status=queue_status,
                    provider=self.provider,
                    model=self.settings.tts_model,
                    sentence_index=sentence_index,
                    queue_ms=round(queue_ms, 3),
                    local_queue_ms=round(local_queue_ms, 3),
                    global_queue_ms=round(global_queue_ms, 3),
                    wait_phase=wait_phase if queue_status != "acquired" else None,
                    coordination_distributed=bool(
                        getattr(distributed_slot, "distributed", False)
                    ),
                )
            # Generation has its own timeout; the admission deadline ends above.
            yield

    async def stream(
        self,
        text_queue: asyncio.Queue[str | None],
        *,
        emotion_state: EmotionState | None,
        voice: str | None = None,
        seed: int | None = None,
        playback_buffer: SpeechPlaybackBuffer | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.settings.tts_enabled:
            raise TtsConfigurationError("员工语音输出已通过 TTS_ENABLED 关闭。")
        resolved_voice = self.resolve_voice(voice)
        resolved_seed = self.settings.tts_seed if seed is None else seed
        status = "failed"
        audio_bytes = 0
        sentence_count = 0
        first_audio_ms: float | None = None
        queue_stats = {
            "total_ms": 0.0,
            "max_ms": 0.0,
            "wait_count": 0.0,
        }
        emotion_metric_fields = self._emotion_metric_fields(emotion_state)
        stream_started = time.perf_counter()

        try:
            try:
                async with aclosing(
                    self._stream_locked(
                        text_queue,
                        emotion_state=emotion_state,
                        voice=resolved_voice,
                        seed=resolved_seed,
                        queue_stats=queue_stats,
                        playback_buffer=playback_buffer,
                    )
                ) as events:
                    async for event in events:
                        if event["event"] == "speech_audio":
                            audio_bytes += int(event.get("byte_length") or 0)
                            if first_audio_ms is None:
                                first_audio_ms = (
                                    time.perf_counter() - stream_started
                                ) * 1000
                        elif event["event"] == "speech_sentence_done":
                            sentence_count += 1
                        yield event
                status = "success"
            except httpx.TimeoutException as exc:
                raise TtsError("Fish Audio 响应超时，本轮仅显示文字。") from exc
            except httpx.TransportError as exc:
                raise TtsError("无法连接 Fish Audio，本轮仅显示文字。") from exc
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            log_metric(
                "tts.stream",
                status=status,
                provider=self.provider,
                model=self.settings.tts_model,
                voice=resolved_voice,
                seed=resolved_seed,
                queue_ms=round(queue_stats["total_ms"], 3),
                max_queue_ms=round(queue_stats["max_ms"], 3),
                queue_wait_count=int(queue_stats["wait_count"]),
                first_audio_ms=(
                    round(first_audio_ms, 3) if first_audio_ms is not None else None
                ),
                audio_bytes=audio_bytes,
                sentence_count=sentence_count,
                **emotion_metric_fields,
            )

    async def _stream_locked(
        self,
        text_queue: asyncio.Queue[str | None],
        *,
        emotion_state: EmotionState | None,
        voice: str,
        seed: int | None,
        queue_stats: dict[str, float] | None = None,
        playback_buffer: SpeechPlaybackBuffer | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        sentence_index = 0
        # Each sentence is sent as an independent Fish request, so every
        # sentence needs the short primary cue to keep the acoustic state from
        # drifting back to neutral.  The optional delivery cue is limited to
        # the first sentence; repeating a second cue on every request adds
        # unnecessary prosody resets while the primary anchor remains stable.
        inline_mode = (
            str(self.settings.tts_emotion_tag_mode or "inline").strip().casefold()
            == "inline"
        )
        async with aclosing(self._iter_sentences(text_queue)) as sentences:
            async for sentence in sentences:
                if not sentence.strip():
                    continue
                if playback_buffer is not None:
                    await playback_buffer.wait_for_capacity()
                async with self._inference_slot(
                    sentence_index=sentence_index,
                    queue_stats=queue_stats,
                ):
                    async with aclosing(
                        self._stream_sentence(
                            sentence,
                            sentence_index=sentence_index,
                            emotion_state=emotion_state,
                            include_delivery_cue=(
                                not inline_mode or sentence_index == 0
                            ),
                            voice=voice,
                            seed=seed,
                        )
                    ) as events:
                        async for event in events:
                            yield event
                sentence_index += 1

        yield {
            "event": "speech_done",
            "voice": voice,
            "sample_rate": self.settings.tts_sample_rate,
        }

    async def _stream_sentence(
        self,
        sentence: str,
        *,
        sentence_index: int,
        emotion_state: EmotionState | None,
        include_delivery_cue: bool = True,
        voice: str,
        seed: int | None,
    ) -> AsyncIterator[dict[str, Any]]:
        decoder = _StreamingWavPcmDecoder(self.settings.tts_sample_rate)
        audio_received = False
        speech_started = False
        audio_bytes = 0
        first_audio_ms: float | None = None
        content_type = ""
        request_status = "failed"
        request_started = time.perf_counter()
        emotion_metric_fields = self._emotion_metric_fields(
            emotion_state,
            include_delivery_cue=include_delivery_cue,
        )
        payload = self._request_payload(
            sentence,
            emotion_state=emotion_state,
            include_delivery_cue=include_delivery_cue,
            voice=voice,
            seed=seed,
        )
        timeout = httpx.Timeout(
            timeout=self.settings.tts_generation_timeout_seconds,
            connect=self.settings.tts_connect_timeout_seconds,
        )

        try:
            async with self._client.stream(
                "POST",
                self.settings.tts_http_url,
                json=payload,
                timeout=timeout,
            ) as response:
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code >= 400:
                    request_status = "http_error"
                    body = await self._read_error_body(response)
                    upstream_error = self._upstream_error_detail(body)
                    logger.warning(
                        "Fish Audio request failed status=%s content_type=%s "
                        "response_bytes=%s upstream_error=%r",
                        response.status_code,
                        content_type,
                        len(body),
                        upstream_error,
                    )
                    detail = f"：{upstream_error}" if upstream_error else ""
                    raise TtsProtocolError(
                        f"Fish Audio 请求失败（HTTP {response.status_code}{detail}）。"
                    )
                self._validate_content_type(content_type)

                first_payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    if not audio_received and not speech_started:
                        first_payload.extend(chunk)
                        if len(first_payload) < 4:
                            continue
                        chunk = bytes(first_payload)
                        first_payload.clear()
                        self._reject_error_document(chunk)

                    for pcm in decoder.feed(chunk):
                        sample_rate = decoder.sample_rate or self.settings.tts_sample_rate
                        if not speech_started:
                            speech_started = True
                            first_audio_ms = (
                                time.perf_counter() - request_started
                            ) * 1000
                            yield {
                                "event": "speech_start",
                                "sentence_index": sentence_index,
                                "sample_rate": sample_rate,
                                "voice": voice,
                            }
                        audio_received = True
                        audio_bytes += len(pcm)
                        yield self._audio_event(
                            pcm,
                            sentence_index=sentence_index,
                            sample_rate=sample_rate,
                        )

                if first_payload:
                    payload_tail = bytes(first_payload)
                    self._reject_error_document(payload_tail)
                    for pcm in decoder.feed(payload_tail):
                        sample_rate = decoder.sample_rate or self.settings.tts_sample_rate
                        if not speech_started:
                            speech_started = True
                            first_audio_ms = (
                                time.perf_counter() - request_started
                            ) * 1000
                            yield {
                                "event": "speech_start",
                                "sentence_index": sentence_index,
                                "sample_rate": sample_rate,
                                "voice": voice,
                            }
                        audio_received = True
                        audio_bytes += len(pcm)
                        yield self._audio_event(
                            pcm,
                            sentence_index=sentence_index,
                            sample_rate=sample_rate,
                        )

            decoder.finish()
            if not audio_received:
                raise TtsProtocolError("Fish Audio 未返回可播放的音频内容。")
            request_status = "success"
            yield {
                "event": "speech_sentence_done",
                "sentence_index": sentence_index,
            }
        except asyncio.CancelledError:
            request_status = "cancelled"
            raise
        except httpx.TimeoutException:
            request_status = "timeout"
            raise
        except httpx.TransportError:
            request_status = "transport_error"
            raise
        except TtsProtocolError:
            if request_status == "failed":
                request_status = "protocol_error"
            raise
        finally:
            log_metric(
                "tts.request",
                status=request_status,
                provider=self.provider,
                model=self.settings.tts_model,
                content_type=content_type or None,
                sentence_index=sentence_index,
                first_audio_ms=(
                    round(first_audio_ms, 3) if first_audio_ms is not None else None
                ),
                duration_ms=round((time.perf_counter() - request_started) * 1000, 3),
                audio_bytes=audio_bytes,
                **emotion_metric_fields,
            )

    def _request_payload(
        self,
        sentence: str,
        *,
        emotion_state: EmotionState | None,
        include_delivery_cue: bool = True,
        voice: str,
        seed: int | None = None,
    ) -> dict[str, Any]:
        resolved_seed = self.settings.tts_seed if seed is None else seed
        if self.uses_vllm_omni:
            payload: dict[str, Any] = {
                "model": self.settings.tts_model,
                "input": self.style_text(
                    sentence,
                    emotion_state,
                    tag_mode=self.settings.tts_emotion_tag_mode,
                    max_tags=self.settings.tts_emotion_max_tags,
                    delta_threshold=self.settings.tts_emotion_delta_threshold,
                    tag_delimiter="square",
                    include_delivery_cue=include_delivery_cue,
                ),
                "voice": self.upstream_voice_for_voice(voice),
                "stream": True,
                "stream_format": "audio",
                "response_format": "pcm",
                "max_new_tokens": self.settings.tts_max_new_tokens,
            }
            if resolved_seed is not None:
                payload["seed"] = resolved_seed
            return payload

        reference_id = self.reference_id_for_voice(voice)
        payload: dict[str, Any] = {
            "text": self.style_text(
                sentence,
                emotion_state,
                tag_mode=self.settings.tts_emotion_tag_mode,
                max_tags=self.settings.tts_emotion_max_tags,
                delta_threshold=self.settings.tts_emotion_delta_threshold,
                tag_delimiter=self.settings.tts_legacy_emotion_tag_delimiter,
                include_delivery_cue=include_delivery_cue,
            ),
            "chunk_length": self.settings.tts_chunk_length,
            "format": "wav",
            "seed": resolved_seed,
            "use_memory_cache": (
                "on"
                if reference_id is not None and self.settings.tts_use_memory_cache
                else "off"
            ),
            "normalize": self.settings.tts_normalize,
            "streaming": True,
            "max_new_tokens": self.settings.tts_max_new_tokens,
            "top_p": self.settings.tts_top_p,
            "repetition_penalty": self.settings.tts_repetition_penalty,
            "temperature": self.settings.tts_temperature,
        }
        if reference_id is not None:
            payload["reference_id"] = reference_id
        return payload

    def _validate_content_type(self, content_type: str) -> None:
        media_type = content_type.partition(";")[0].strip()
        if not media_type:
            return
        if self.uses_vllm_omni:
            if media_type in _VLLM_PCM_CONTENT_TYPES:
                return
        elif media_type == "audio/wav":
            return
        raise TtsProtocolError(
            f"Fish Audio 返回了不支持的内容类型：{content_type}。"
        )

    @staticmethod
    def _reject_error_document(data: bytes) -> None:
        stripped = data.lstrip()
        if not stripped.startswith((b"{", b"[")):
            return
        try:
            stripped.decode("utf-8")
        except UnicodeDecodeError:
            return
        raise TtsProtocolError("Fish Audio 返回了 JSON 错误内容而不是 PCM 音频。")

    @staticmethod
    async def _read_error_body(response: httpx.Response) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            remaining = _MAX_UPSTREAM_ERROR_BYTES - len(body)
            if remaining <= 0:
                break
            body.extend(chunk[:remaining])
            if len(body) >= _MAX_UPSTREAM_ERROR_BYTES:
                break
        return bytes(body)

    @staticmethod
    def _upstream_error_detail(body: bytes) -> str | None:
        if not body:
            return None
        decoded = body.decode("utf-8", errors="replace")
        detail: object | None = None
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError:
            detail = decoded
        else:
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("detail")
                elif isinstance(error, str):
                    detail = error
                if detail is None:
                    detail = payload.get("message") or payload.get("detail")
            elif isinstance(payload, str):
                detail = payload

        if detail is None:
            return None
        normalized = " ".join(
            "".join(
                character if character.isprintable() else " "
                for character in str(detail)
            ).split()
        )
        return normalized[:_MAX_UPSTREAM_ERROR_CHARS] or None

    async def _iter_sentences(
        self,
        text_queue: asyncio.Queue[str | None],
    ) -> AsyncIterator[str]:
        buffer = ""
        leading_whitespace = ""
        final = False
        first_segment = True
        first_deadline: float | None = None
        first_wait_expired = False
        loop = asyncio.get_running_loop()
        merge_limit = min(
            self.settings.tts_sentence_merge_max_chars,
            self.settings.tts_sentence_max_chars,
        )

        while buffer or not final:
            if not first_segment:
                # Only consume text that has already arrived. A short complete
                # sentence must never wait for another LLM delta just to merge.
                while not final and len(buffer) < merge_limit:
                    try:
                        delta = text_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    final = delta is None
                    if delta:
                        buffer += delta

            split_at = self._next_sentence_end(buffer, final=final)
            if first_segment:
                if (
                    first_wait_expired
                    or len(buffer) >= self.settings.tts_first_segment_max_chars
                ):
                    soft_end = self._first_segment_soft_end(buffer)
                    if soft_end is not None:
                        split_at = min(split_at, soft_end) if split_at else soft_end
            elif split_at is not None:
                while split_at < min(len(buffer), merge_limit):
                    next_end = self._next_sentence_end(buffer[split_at:], final=final)
                    if next_end is None or split_at + next_end > merge_limit:
                        break
                    split_at += next_end

            if split_at:
                segment = buffer[:split_at]
                buffer = buffer[split_at:]
                if not segment.strip():
                    leading_whitespace += segment
                    continue
                first_segment = False
                yield leading_whitespace + segment
                leading_whitespace = ""
                continue
            if final:
                break

            deadline = (
                first_deadline
                if first_segment and not first_wait_expired
                else None
            )
            try:
                async with asyncio.timeout_at(deadline):
                    delta = await text_queue.get()
            except TimeoutError:
                # Time alone is not a word boundary. If there is no suitable
                # punctuation/space yet, continue waiting without a busy loop.
                first_wait_expired = True
                continue
            final = delta is None
            if delta:
                buffer += delta
                if first_segment and first_deadline is None and buffer.strip():
                    first_deadline = (
                        loop.time() + self.settings.tts_first_segment_wait_ms / 1000
                    )

    def _first_segment_soft_end(self, text: str) -> int | None:
        ceiling = min(
            self.settings.tts_first_segment_max_chars,
            self.settings.tts_sentence_max_chars,
            len(text),
        )
        floor = self.settings.tts_first_segment_min_chars
        if ceiling < floor:
            return None
        for index in range(ceiling - 1, floor - 2, -1):
            if text[index] in _SOFT_BREAKS and len(text[: index + 1].strip()) >= floor:
                return index + 1
        return None

    def _next_sentence_end(self, text: str, *, final: bool) -> int | None:
        max_chars = self.settings.tts_sentence_max_chars
        for index, character in enumerate(text[:max_chars]):
            if character not in _SENTENCE_ENDINGS:
                continue
            if (
                character == "."
                and index > 0
                and text[index - 1].isdigit()
                and (
                    (index + 1 < len(text) and text[index + 1].isdigit())
                    or (index + 1 == len(text) and not final)
                )
            ):
                continue
            return index + 1

        if len(text) >= max_chars:
            floor = max_chars // 2
            for index in range(max_chars - 1, floor - 1, -1):
                if text[index] in _SOFT_BREAKS:
                    return index + 1
            return max_chars
        return len(text) if final else None

    @staticmethod
    def _audio_event(
        audio: bytes,
        *,
        sentence_index: int,
        sample_rate: int,
    ) -> dict[str, Any]:
        if not audio:
            raise TtsProtocolError("Fish Audio 返回了空音频块。")
        return {
            "event": "speech_audio",
            "_pcm": audio,
            "byte_length": len(audio),
            "sentence_index": sentence_index,
            "sample_rate": sample_rate,
            "format": "pcm_s16le",
            "channels": 1,
        }

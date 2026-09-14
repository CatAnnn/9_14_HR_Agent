"""Compatibility exports for the former Qwen TTS integration.

New code should import from :mod:`backend.services.fish_tts_service`.
"""

from backend.services.fish_tts_service import (
    FishTtsService,
    TtsConfigurationError,
    TtsError,
    TtsProtocolError,
    TtsQueueTimeoutError,
)

QwenTtsService = FishTtsService

__all__ = [
    "FishTtsService",
    "QwenTtsService",
    "TtsConfigurationError",
    "TtsError",
    "TtsProtocolError",
    "TtsQueueTimeoutError",
]

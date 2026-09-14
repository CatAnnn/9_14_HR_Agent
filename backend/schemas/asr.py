from __future__ import annotations

from pydantic import BaseModel


class AsrTranscribeResponse(BaseModel):
    text: str
    audio_emotion: str | None = None
    duration_seconds: float | None = None
    provider: str

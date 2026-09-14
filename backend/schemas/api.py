from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.intent import IntentGoalPerformanceItem
from backend.schemas.profile import EmployeeProfile
from backend.schemas.simulation import BigFivePersonality
from backend.schemas.locale import SessionLocale


class SessionLocaleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locale: SessionLocale = "zh-CN"


class SessionLocaleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locale: SessionLocale


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


class TextDocumentRequest(BaseModel):
    session_id: str | None = None
    text: str
    filename: str | None = "pasted_text.txt"


class ConfirmProfileRequest(BaseModel):
    profile: EmployeeProfile


class ConfirmIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(min_length=1)
    performance_items: list[IntentGoalPerformanceItem] = Field(
        min_length=3,
        max_length=3,
    )


class IntentPerformanceDraftRequest(BaseModel):
    intent_id: str = Field(min_length=1)


class IntentPerformanceDraftResponse(BaseModel):
    intent_id: str
    locale: SessionLocale = "zh-CN"
    performance_context: str
    performance_items: list[IntentGoalPerformanceItem]


class EmployeeLatestSetupSettingsResponse(BaseModel):
    employee_id: str
    found: bool = False
    supplemental_info: str = ""
    personality: BigFivePersonality | None = None
    primary_motive_id: str | None = None
    secondary_motive_ids: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class ConfirmSimulationRequest(BaseModel):
    personality: BigFivePersonality
    primary_motive_id: str
    secondary_motive_ids: list[str] = Field(default_factory=list, max_length=2)
    run_mode: str = "guidance_then_rehearsal"


class RehearsalSpeechRequest(BaseModel):
    enabled: bool = True
    voice: str | None = None
    stream_id: str | None = Field(
        default=None,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class RehearsalMessageRequest(BaseModel):
    message: str = Field(min_length=1)
    request_id: str | None = Field(
        default=None,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]+$",
    )
    speech: RehearsalSpeechRequest | None = None


class RehearsalContextUpdateRequest(BaseModel):
    runtime_note: str | None = None
    runtime_notes: list[str] | str | None = None
    clear_context: bool = False


class GenericStatusResponse(BaseModel):
    ok: bool = True
    message: str = "ok"

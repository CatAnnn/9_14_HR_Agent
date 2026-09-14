from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from backend.schemas.simulation import EmotionState

Speaker = Literal["manager", "employee", "system"]
CONVERSATION_TURN_LOCALE_METADATA_KEY = "locale"


class EmotionTurnSnapshot(BaseModel):
    valence: float = Field(ge=-1.0, le=1.0)
    arousal: float = Field(ge=-1.0, le=1.0)
    dominance: float = Field(ge=-1.0, le=1.0)
    anchor_id: str | None = None
    previous_anchor_id: str | None = None
    vad_delta: dict[str, float] | None = None
    transition_intensity: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_summary: str | None = None


# Deliberately explicit: adding a snapshot field must trigger the projection
# contract test and a privacy/semantics review instead of silently reaching Reply.
EMOTION_TURN_SNAPSHOT_FIELDS = (
    "valence",
    "arousal",
    "dominance",
    "anchor_id",
    "previous_anchor_id",
    "vad_delta",
    "transition_intensity",
    "reason_summary",
)


def project_emotion_snapshot(value: object) -> dict[str, object]:
    """Keep only fields declared by the persisted emotion snapshot schema."""

    if not isinstance(value, dict):
        return {}
    return {
        field: value[field]
        for field in EMOTION_TURN_SNAPSHOT_FIELDS
        if field in value and value[field] is not None
    }


class ConversationTurn(BaseModel):
    turn_index: int
    speaker: Speaker
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = Field(default_factory=dict)


def attach_emotion_snapshot(
    turn: ConversationTurn,
    emotion_state: EmotionState | None,
) -> None:
    if emotion_state is None:
        return

    vad = emotion_state.current_vad
    transition_metadata: dict[str, object] = {}
    if emotion_state.has_manager_response:
        transition_metadata = {
            "previous_anchor_id": emotion_state.previous_anchor_id,
            "vad_delta": emotion_state.last_vad_delta.model_dump(),
            "transition_intensity": emotion_state.transition_intensity,
        }
    snapshot = EmotionTurnSnapshot(
        valence=vad.valence,
        arousal=vad.arousal,
        dominance=vad.dominance,
        anchor_id=emotion_state.current_anchor_id,
        reason_summary=emotion_state.last_reason_summary,
        **transition_metadata,
    )
    turn.metadata["emotion_snapshot"] = snapshot.model_dump(exclude_none=True)

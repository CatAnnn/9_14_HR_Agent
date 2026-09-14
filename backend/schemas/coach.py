from __future__ import annotations

from pydantic import BaseModel, Field

from backend.schemas.task import CoachTaskResult
from backend.schemas.locale import SessionLocale


class CoachReport(BaseModel):
    """Final rehearsal report containing the four configured Coach dimensions."""

    session_id: str
    locale: SessionLocale = "zh-CN"
    coach_version: str | None = None
    status: str = "success"
    task_results: list[CoachTaskResult] = Field(default_factory=list)
    disclaimer: str = "Coach 报告仅提供演练复盘和改进建议，最终判断由人工负责。"

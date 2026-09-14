from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from backend.schemas.locale import SessionLocale, localized_value


PerformanceRating = Literal[1, 2, 3, 4, 5]
TCLValue = Literal["#", "+", "++", "+++", "++++"]


class IntentEligibilityRule(BaseModel):
    performance_ratings: list[PerformanceRating] = Field(default_factory=list)
    tcl_values: list[TCLValue] = Field(default_factory=list)
    match_mode: Literal["any"] = "any"


class IntentPerformanceInferenceRule(BaseModel):
    objective: str = Field(min_length=1)
    profile_tone: str = Field(min_length=1)
    evidence_priority: list[str] = Field(min_length=1)
    inference_rules: list[str] = Field(min_length=1)


class IntentConfig(BaseModel):
    id: str
    name: str
    description: str | None = Field(default=None, min_length=1)
    eligibility: IntentEligibilityRule | None = None
    performance_inference: IntentPerformanceInferenceRule | None = Field(
        default=None,
        exclude=True,
    )


PERFORMANCE_OVERVIEW_TARGET_CHARS = 120
PERFORMANCE_DETAIL_TARGET_CHARS = 480

def _normalize_editable_text(value: str, *, field_name: str) -> str:
    """Validate non-empty text without rewriting its presentation."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    return normalized


class IntentGoalPerformanceItem(BaseModel):
    goal: str = Field(
        min_length=1,
        max_length=2000,
        description="固定综合表现维度。",
    )
    current_performance: str = Field(
        min_length=1,
        description="该综合表现维度对应的可编辑内容。",
    )
    generation_reason: str | None = Field(
        default=None,
        description="旧会话兼容字段；当前模型不生成此内容。",
    )

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("goal cannot be empty")
        return normalized

    @field_validator("current_performance")
    @classmethod
    def normalize_current_performance(cls, value: str) -> str:
        return _normalize_editable_text(
            value,
            field_name="current_performance",
        )

class IntentResult(BaseModel):
    intent_id: str
    confidence: float = 0.0
    reason: str | None = None
    config: IntentConfig | None = None
    performance_context: str | None = None
    # Legacy saved intent output was Chinese; a mismatch with SessionState.locale
    # means the editable draft should be regenerated, not silently reused.
    performance_locale: SessionLocale = "zh-CN"
    performance_items: list[IntentGoalPerformanceItem] = Field(
        default_factory=list,
    )


PERFORMANCE_CONTEXT_DISCLAIMER = (
    "本内容基于有限信息推演模拟，仅用于管理者绩效面谈预演，"
    "不可直接作为正式绩效评估文件"
)
PERFORMANCE_TARGET_FALLBACK = "当前资料未提供明确目标"
PERFORMANCE_SECTION_TITLES = (
    "目标达成总览",
    "正向表现/取得进展",
    "现存差距与行为实例",
)
PERFORMANCE_SECTION_TITLES_EN = (
    "Goal Achievement Overview",
    "Positive Performance / Progress",
    "Current Gaps and Behavioral Examples",
)
PERFORMANCE_CONTEXT_DISCLAIMER_EN = (
    "This content is a simulation based on limited information for manager "
    "performance-conversation rehearsal only and must not be used directly "
    "as a formal performance evaluation document."
)
PERFORMANCE_SECTION_TITLES_DE = (
    "Überblick über die Zielerreichung",
    "Positive Leistung / Fortschritt",
    "Aktuelle Lücken und Verhaltensbeispiele",
)
PERFORMANCE_CONTEXT_DISCLAIMER_DE = (
    "Dieser Inhalt ist eine Simulation auf Grundlage begrenzter Informationen "
    "und dient ausschließlich der Vorbereitung eines Leistungsdialogs durch "
    "Führungskräfte. Er darf nicht unmittelbar als formelle Leistungsbeurteilung "
    "verwendet werden."
)
PERFORMANCE_SECTION_TITLES_JA = (
    "目標達成の概要",
    "良好な成果／進捗",
    "現在の課題と行動事例",
)
PERFORMANCE_CONTEXT_DISCLAIMER_JA = (
    "本内容は限られた情報に基づくシミュレーションであり、管理職が人事評価面談を"
    "準備する目的にのみ使用できます。正式な人事評価文書として直接使用することは"
    "できません。"
)

_PERFORMANCE_SECTION_TITLES = {
    "zh-CN": PERFORMANCE_SECTION_TITLES,
    "en": PERFORMANCE_SECTION_TITLES_EN,
    "de": PERFORMANCE_SECTION_TITLES_DE,
    "ja": PERFORMANCE_SECTION_TITLES_JA,
}
_PERFORMANCE_CONTEXT_DISCLAIMERS = {
    "zh-CN": PERFORMANCE_CONTEXT_DISCLAIMER,
    "en": PERFORMANCE_CONTEXT_DISCLAIMER_EN,
    "de": PERFORMANCE_CONTEXT_DISCLAIMER_DE,
    "ja": PERFORMANCE_CONTEXT_DISCLAIMER_JA,
}


def performance_section_titles(locale: SessionLocale = "zh-CN") -> tuple[str, str, str]:
    return localized_value(locale, _PERFORMANCE_SECTION_TITLES)


def expected_performance_goals(profile_goals: list[str]) -> list[str]:
    goals = [str(goal).strip() for goal in profile_goals if str(goal).strip()]
    return goals or [PERFORMANCE_TARGET_FALLBACK]


def build_performance_context(
    items: list[IntentGoalPerformanceItem],
    *,
    locale: SessionLocale = "zh-CN",
) -> str:
    sections = "\n\n".join(
        f"{item.goal}\n{item.current_performance}"
        for item in items
    )
    disclaimer = localized_value(locale, _PERFORMANCE_CONTEXT_DISCLAIMERS)
    return f"{sections}\n\n{disclaimer}"


class IntentPerformanceDraft(BaseModel):
    locale: SessionLocale = Field(default="zh-CN", exclude=True)
    goal_performance_items: list[IntentGoalPerformanceItem] = Field(
        min_length=1,
    )

    @property
    def performance_context(self) -> str:
        return build_performance_context(
            self.goal_performance_items,
            locale=self.locale,
        )


class IntentPerformanceDraftOutput(BaseModel):
    goal_overview: str = Field(
        min_length=1,
        description=(
            "目标达成总览。归并覆盖全部目标，优先使用1至2条精练短句。"
        ),
    )
    positive_performance: str = Field(
        min_length=1,
        description="正向表现或已取得的进展，根据有效证据使用1至4条完整要点。",
    )
    performance_gaps: str = Field(
        min_length=1,
        description="现存差距与行为实例，根据有效证据使用1至4条完整要点。",
    )

    @field_validator(
        "goal_overview",
        "positive_performance",
        "performance_gaps",
    )
    @classmethod
    def normalize_current_performance_lines(
        cls,
        value: str,
    ) -> str:
        return _normalize_editable_text(
            value,
            field_name="performance section",
        )

    def bind_sections(
        self,
        output_locale: SessionLocale = "zh-CN",
    ) -> IntentPerformanceDraft:
        performances = (
            self.goal_overview,
            self.positive_performance,
            self.performance_gaps,
        )
        return IntentPerformanceDraft(
            goal_performance_items=[
                IntentGoalPerformanceItem(
                    goal=title,
                    current_performance=performance,
                    generation_reason=None,
                )
                for title, performance in zip(
                    performance_section_titles(output_locale),
                    performances,
                    strict=True,
                )
            ],
            locale=output_locale,
        )

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.retrieval import Citation, CitationReference
from backend.schemas.locale import SessionLocale


_GUIDANCE_TERMINATORS = frozenset("。！？!?；;.…")
_GUIDANCE_TRAILING_CLOSERS = frozenset("”’\"'）)]】》」』»›")
_GUIDANCE_DELIMITER_PAIRS = {
    "“": "”",
    "‘": "’",
    "„": "“",
    "‚": "‘",
    "«": "»",
    "‹": "›",
    "「": "」",
    "『": "』",
    "（": "）",
    "(": ")",
    "[": "]",
    "【": "】",
    "《": "》",
}
_GUIDANCE_DANGLING_ENDINGS = (
    "以及",
    "并且",
    "包括",
    "例如",
    "比如",
    "因为",
    "但是",
    "从而",
    "导致",
    "陷入",
    "转向",
    "基于",
    "围绕",
    "以便",
)
GUIDANCE_LIST_MIN_ITEMS = 1
GUIDANCE_LIST_MAX_ITEMS = 3
GUIDANCE_GROUP_MIN_ITEMS = 1
GUIDANCE_GROUP_MAX_ITEMS = 3
GUIDANCE_DETAIL_MIN_ITEMS = 2
GUIDANCE_DETAIL_MAX_ITEMS = 4
GUIDANCE_PLAN_DETAIL_MIN_ITEMS = 2
GUIDANCE_PLAN_DETAIL_MAX_ITEMS = 2
GUIDANCE_POINT_TITLE_MAX_LENGTH = 36
GUIDANCE_POINT_SUMMARY_MAX_LENGTH = 80
GUIDANCE_POINT_DETAIL_MAX_LENGTH = 430


class GuidanceDimensionPointContract(TypedDict):
    titles: tuple[str, ...]
    group_min_items: int
    group_max_items: int
    detail_min_items: int
    detail_max_items: int


GUIDANCE_DIMENSION_POINT_CONTRACTS: dict[str, GuidanceDimensionPointContract] = {
    "start": {
        "titles": ("开场定调与绩效结果对齐",),
        "group_min_items": 1,
        "group_max_items": 1,
        "detail_min_items": 2,
        "detail_max_items": 2,
    },
    "emotion": {
        "titles": ("认真倾听，理解情绪", "共情总结", "共同探索"),
        "group_min_items": 3,
        "group_max_items": 3,
        "detail_min_items": 2,
        "detail_max_items": 2,
    },
    "requirement": {
        "titles": ("目标方向的正确性", "结果的突破性", "高绩效文化"),
        "group_min_items": 3,
        "group_max_items": 3,
        "detail_min_items": 2,
        "detail_max_items": 2,
    },
    "plan": {
        "titles": ("职位发展", "Career Elements"),
        "group_min_items": 1,
        "group_max_items": 2,
        "detail_min_items": GUIDANCE_PLAN_DETAIL_MIN_ITEMS,
        "detail_max_items": GUIDANCE_PLAN_DETAIL_MAX_ITEMS,
    },
}
GuidanceSentence = Annotated[str, Field(min_length=1)]
GuidancePointList = Annotated[
    list[GuidanceSentence],
    Field(
        min_length=GUIDANCE_LIST_MIN_ITEMS,
        max_length=GUIDANCE_LIST_MAX_ITEMS,
    ),
]
GuidancePointTitle = Annotated[
    str,
    Field(min_length=1),
]
GuidancePointSummary = Annotated[
    str,
    Field(
        min_length=1,
        description="一句完整、简短的总体概述；具体建议放在 details。",
    ),
]
GuidancePointDetail = Annotated[
    str,
    Field(min_length=1),
]
GuidancePointDetailList = Annotated[
    list[GuidancePointDetail],
    Field(
        min_length=GUIDANCE_DETAIL_MIN_ITEMS,
        max_length=GUIDANCE_DETAIL_MAX_ITEMS,
    ),
]
GuidancePlanPointDetailList = Annotated[
    list[GuidancePointDetail],
    Field(
        min_length=GUIDANCE_PLAN_DETAIL_MIN_ITEMS,
        max_length=GUIDANCE_PLAN_DETAIL_MAX_ITEMS,
    ),
]
GuidanceStoredPlanPointDetailList = Annotated[
    list[GuidancePointDetail],
    Field(min_length=1, max_length=GUIDANCE_DETAIL_MAX_ITEMS),
]
GuidanceKnowledgeChunkIds = list[str]


def _guidance_sentence_quality_issues(value: str, *, field_name: str) -> list[str]:
    issues: list[str] = []
    if value != value.strip():
        issues.append(f"{field_name}:surrounding_whitespace")
    if len(value.strip()) < 8:
        issues.append(f"{field_name}:short_text")

    stack: list[str] = []
    closing_delimiters = set(_GUIDANCE_DELIMITER_PAIRS.values())
    delimiters_balanced = True
    for character in value:
        if stack and stack[-1] == character:
            stack.pop()
        elif (expected_closer := _GUIDANCE_DELIMITER_PAIRS.get(character)) is not None:
            stack.append(expected_closer)
        elif character in closing_delimiters:
            delimiters_balanced = False
            break
    if any(value.count(quote) % 2 for quote in ('"', "'")):
        delimiters_balanced = False
    if stack or not delimiters_balanced:
        issues.append(f"{field_name}:unbalanced_delimiters")

    terminal_candidate = value.rstrip()
    while terminal_candidate and terminal_candidate[-1] in _GUIDANCE_TRAILING_CLOSERS:
        terminal_candidate = terminal_candidate[:-1].rstrip()
    if not terminal_candidate or terminal_candidate[-1] not in _GUIDANCE_TERMINATORS:
        issues.append(f"{field_name}:missing_terminal_punctuation")
    semantic_candidate = terminal_candidate.rstrip(
        "".join(_GUIDANCE_TERMINATORS)
    ).rstrip()
    if semantic_candidate.endswith(_GUIDANCE_DANGLING_ENDINGS):
        issues.append(f"{field_name}:dangling_fragment")
    return issues


def guidance_quality_issues(payload: Mapping[str, object]) -> list[str]:
    issues: list[str] = []
    for field_name in ("purpose", "opening_suggestion"):
        value = payload.get(field_name)
        if isinstance(value, str):
            issues.extend(
                _guidance_sentence_quality_issues(value, field_name=field_name)
            )

    for field_name in ("risk_preview", "response_strategies", "safer_phrases"):
        value = payload.get(field_name)
        if not isinstance(value, list):
            continue
        if len(set(value)) != len(value):
            issues.append(f"{field_name}:duplicate_items")
        for index, item in enumerate(value):
            if isinstance(item, str):
                issues.extend(
                    _guidance_sentence_quality_issues(
                        item,
                        field_name=f"{field_name}[{index}]",
                    )
                )

    points = payload.get("points")
    if isinstance(points, list):
        issues.extend(_guidance_point_groups_quality_issues(points, field_name="points"))

    dimension_points = payload.get("dimension_points")
    if isinstance(dimension_points, Mapping):
        for dimension_name, dimension_groups in dimension_points.items():
            if isinstance(dimension_groups, list):
                issues.extend(
                    _guidance_point_groups_quality_issues(
                        dimension_groups,
                        field_name=f"dimension_points.{dimension_name}",
                    )
                )
    return issues


def _guidance_point_groups_quality_issues(
    groups: list[object],
    *,
    field_name: str,
) -> list[str]:
    issues: list[str] = []
    titles: list[str] = []
    for group_index, group in enumerate(groups):
        if not isinstance(group, Mapping):
            continue
        title = group.get("title")
        if isinstance(title, str):
            titles.append(title)
            if title != title.strip():
                issues.append(
                    f"{field_name}[{group_index}].title:surrounding_whitespace"
                )
        summary = group.get("summary")
        if isinstance(summary, str):
            issues.extend(
                _guidance_sentence_quality_issues(
                    summary,
                    field_name=f"{field_name}[{group_index}].summary",
                )
            )
        details = group.get("details")
        if not isinstance(details, list):
            continue
        string_details = [detail for detail in details if isinstance(detail, str)]
        if len(set(string_details)) != len(string_details):
            issues.append(f"{field_name}[{group_index}].details:duplicate_items")
        for detail_index, detail in enumerate(string_details):
            issues.extend(
                _guidance_sentence_quality_issues(
                    detail,
                    field_name=(
                        f"{field_name}[{group_index}].details[{detail_index}]"
                    ),
                )
            )
    if len(set(titles)) != len(titles):
        issues.append(f"{field_name}:duplicate_titles")
    return issues


class GuidanceContentModel(BaseModel):
    """Keep the wire shape strict while reporting prose quality separately."""

    model_config = ConfigDict(extra="forbid")


class GuidancePointGroup(GuidanceContentModel):
    title: GuidancePointTitle
    summary: GuidancePointSummary | None = None
    details: GuidancePointDetailList
    summary_knowledge_chunk_ids: GuidanceKnowledgeChunkIds = Field(
        default_factory=list
    )
    detail_knowledge_chunk_ids: list[GuidanceKnowledgeChunkIds] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_detail_knowledge_refs(self) -> "GuidancePointGroup":
        if (
            self.detail_knowledge_chunk_ids
            and len(self.detail_knowledge_chunk_ids) != len(self.details)
        ):
            raise ValueError(
                "detail_knowledge_chunk_ids must contain one array per details item"
            )
        return self


GuidanceCitationTarget = Literal[
    "summary",
    "detail_0",
    "detail_1",
    "detail_2",
    "detail_3",
]


class GuidanceGeneratedPointContent(GuidanceContentModel):
    """Model-facing point content; display titles are injected by the backend."""

    summary: GuidancePointSummary
    details: GuidancePointDetailList
    citation_targets: list[GuidanceCitationTarget] = Field(default_factory=list)
    citation_source_refs: list[str] = Field(default_factory=list)
    citation_highlight_texts: list[str] = Field(default_factory=list)
    citation_source_quotes: list[str] = Field(default_factory=list)

    def resolved_citation_refs(
        self,
    ) -> list[tuple[GuidanceCitationTarget, CitationReference]]:
        """Build valid references without allowing optional evidence to fail prose."""
        references: list[tuple[GuidanceCitationTarget, CitationReference]] = []
        target_counts: dict[str, int] = {}
        for target, source_ref, highlight_text, source_quote in zip(
            self.citation_targets,
            self.citation_source_refs,
            self.citation_highlight_texts,
            self.citation_source_quotes,
        ):
            if target != "summary":
                detail_index = int(target.removeprefix("detail_"))
                if detail_index >= len(self.details):
                    continue
            target_counts[target] = target_counts.get(target, 0) + 1
            if target_counts[target] > 4:
                continue
            try:
                reference = CitationReference(
                    source_ref=source_ref,
                    highlight_text=highlight_text,
                    source_quote=source_quote,
                )
            except ValueError:
                continue
            references.append((target, reference))
        return references


class GuidanceGeneratedPointGroup(GuidanceGeneratedPointContent):
    """Generated point after the backend attaches its configured display title."""

    title: GuidancePointTitle


class GuidanceGeneratedPlanPointContent(GuidanceGeneratedPointContent):
    """Strict model-facing plan content without a model-generated title."""

    details: GuidancePlanPointDetailList


class GuidanceGeneratedPlanPointGroup(GuidanceGeneratedPointGroup):
    """Plan point after the backend attaches its configured display title."""

    details: GuidancePlanPointDetailList


class GuidancePlanPointGroup(GuidancePointGroup):
    """Stored plan point tolerant of both current and historical reports."""

    details: GuidanceStoredPlanPointDetailList


GuidancePointGroupList = Annotated[
    list[GuidancePointGroup],
    Field(
        min_length=GUIDANCE_GROUP_MIN_ITEMS,
        max_length=GUIDANCE_GROUP_MAX_ITEMS,
    ),
]

GuidanceGeneratedPointGroupList = Annotated[
    list[GuidanceGeneratedPointGroup],
    Field(
        min_length=GUIDANCE_GROUP_MIN_ITEMS,
        max_length=GUIDANCE_GROUP_MAX_ITEMS,
    ),
]

GuidancePlanPointGroupList = Annotated[
    list[GuidancePlanPointGroup],
    Field(
        min_length=GUIDANCE_GROUP_MIN_ITEMS,
        max_length=GUIDANCE_GROUP_MAX_ITEMS,
    ),
]

class GuidanceDimensionPoints(GuidanceContentModel):
    start: GuidancePointGroupList
    emotion: GuidancePointGroupList
    requirement: GuidancePointGroupList
    plan: GuidancePlanPointGroupList


class GuidanceReport(GuidanceContentModel):
    session_id: str
    locale: SessionLocale = "zh-CN"
    intent_id: str
    guidance_version: str | None = None
    culture_version: str | None = None
    primary_motive_id: str | None = None
    secondary_motive_ids: list[str] = Field(default_factory=list)
    purpose: GuidanceSentence
    opening_suggestion: GuidanceSentence
    risk_preview: GuidancePointList
    response_strategies: GuidancePointList
    safer_phrases: GuidancePointList
    dimension_points: GuidanceDimensionPoints | None = None
    evidence_policy: str = "谈前指导不评价用户表现，不引用用户对话原话，不做评分。"
    citations: list[Citation] = Field(default_factory=list)
    disclaimer: str = "本建议用于演练准备，不替代 HR/Legal 或 Manager 的最终判断。"

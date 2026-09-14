from __future__ import annotations

from typing import Any, Literal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from backend.schemas.retrieval import (
    CitationReference,
    normalize_citation_references,
)

TaskStatus = Literal["success", "insufficient_information", "failed"]
RiskSeverity = Literal["critical", "high", "medium", "low"]
KnowledgeChunkIds = list[str]


class CoachDimensionIssue(BaseModel):
    """One model-identified issue backed by an exact Manager turn."""

    # Runtime parsing is deliberately tolerant because one recoverable issue
    # must not invalidate the dimension. The JSON schema remains strict so the
    # model is still guided toward the minimal contract.
    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        json_schema_extra={"additionalProperties": False},
    )

    turn_index: int = Field(ge=0)
    quote: str = Field(
        min_length=1,
        description=(
            "Copy one non-empty contiguous substring verbatim from the text of "
            "the Manager turn identified by turn_index. Do not summarize, "
            "paraphrase, join non-contiguous fragments, or rewrite punctuation."
        ),
    )
    diagnostic_dimension_id: str | None = Field(default=None, min_length=1)
    problem: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    suggestion: str = Field(
        min_length=1,
        description=(
            "One complete Manager utterance that can be said verbatim to the "
            "Employee. Do not return coaching instructions, an action checklist, "
            "a template preface, or multiple alternative versions."
        ),
    )
    reason: str = Field(min_length=1)
    knowledge_chunk_ids: KnowledgeChunkIds = Field(default_factory=list)
    problem_citation_refs: list[CitationReference] = Field(
        default_factory=list
    )
    impact_citation_refs: list[CitationReference] = Field(
        default_factory=list
    )
    suggestion_citation_refs: list[CitationReference] = Field(
        default_factory=list
    )
    reason_citation_refs: list[CitationReference] = Field(
        default_factory=list
    )

    @field_validator("diagnostic_dimension_id", mode="before")
    @classmethod
    def normalize_empty_diagnostic_dimension_id(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "problem_citation_refs",
        "impact_citation_refs",
        "suggestion_citation_refs",
        "reason_citation_refs",
        mode="before",
    )
    @classmethod
    def filter_optional_citation_refs(cls, value: Any) -> list[CitationReference]:
        return normalize_citation_references(value)


class CoachCareerElementAdviceOutput(BaseModel):
    """One model-generated Career Elements development recommendation."""

    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        json_schema_extra={"additionalProperties": False},
    )

    element: str = Field(min_length=1)
    suggestion: str = Field(
        min_length=1,
        description=(
            "A manager-facing Career Elements development action recommendation. "
            "This field is not a verbatim Manager-to-Employee phrase."
        ),
    )
    reason: str = Field(min_length=1)
    knowledge_chunk_ids: KnowledgeChunkIds = Field(default_factory=list)
    element_citation_refs: list[CitationReference] = Field(
        default_factory=list
    )
    suggestion_citation_refs: list[CitationReference] = Field(
        default_factory=list
    )
    reason_citation_refs: list[CitationReference] = Field(
        default_factory=list
    )

    @field_validator(
        "element_citation_refs",
        "suggestion_citation_refs",
        "reason_citation_refs",
        mode="before",
    )
    @classmethod
    def filter_optional_citation_refs(cls, value: Any) -> list[CitationReference]:
        return normalize_citation_references(value)


class CoachDimensionModelOutput(BaseModel):
    """Narrow model-facing contract; fixed report fields are added by the backend."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["success", "insufficient_information"]
    score: int | None = Field(default=None, ge=1, le=5)
    summary: str = Field(min_length=1)
    strengths: list[str] = Field(
        default_factory=list,
        description=(
            "When status is success, provide up to 4 complete sentences in the requested output language "
            "about concrete strengths actually demonstrated by the Manager."
        ),
    )
    basis: str = Field(min_length=1)
    summary_knowledge_chunk_ids: KnowledgeChunkIds = Field(default_factory=list)
    basis_knowledge_chunk_ids: KnowledgeChunkIds = Field(default_factory=list)
    summary_citation_refs: list[CitationReference] = Field(
        default_factory=list
    )
    basis_citation_refs: list[CitationReference] = Field(
        default_factory=list
    )
    issues: list[CoachDimensionIssue] = Field(
        default_factory=list,
        description=(
            "Up to 25 distinct, Manager-quote-backed issues. Up to 8 issues may "
            "share one diagnostic_dimension_id when they describe different "
            "observable problems and each has its own directly usable suggestion."
        ),
    )
    career_elements_advice: list[CoachCareerElementAdviceOutput] = Field(
        default_factory=list,
        description=(
            "Optional Career Elements development advice for the development-plan dimension "
            "only. Leave empty when it is not applicable or evidence is insufficient. "
            "Missing or invalid optional citation references do not invalidate the advice."
        ),
    )

    @field_validator("strengths", mode="before")
    @classmethod
    def filter_strength_sentences(cls, value: Any) -> list[str]:
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        sentences = [
            sentence.strip()
            for sentence in items
            if isinstance(sentence, str) and sentence.strip()
        ]
        return sentences

    @field_validator(
        "summary_citation_refs",
        "basis_citation_refs",
        mode="before",
    )
    @classmethod
    def filter_optional_citation_refs(cls, value: Any) -> list[CitationReference]:
        return normalize_citation_references(value)

    @field_validator("issues", mode="before")
    @classmethod
    def filter_structurally_invalid_issues(cls, value: Any) -> list[CoachDimensionIssue]:
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        issues: list[CoachDimensionIssue] = []
        for item in items:
            try:
                issues.append(CoachDimensionIssue.model_validate(item))
            except (TypeError, ValueError):
                continue
        return issues

    @field_validator("career_elements_advice", mode="before")
    @classmethod
    def filter_structurally_invalid_career_advice(
        cls,
        value: Any,
    ) -> list[CoachCareerElementAdviceOutput]:
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        advice: list[CoachCareerElementAdviceOutput] = []
        for item in items:
            try:
                advice.append(CoachCareerElementAdviceOutput.model_validate(item))
            except (TypeError, ValueError):
                continue
        return advice

    @model_validator(mode="after")
    def validate_status_contract(self) -> "CoachDimensionModelOutput":
        if self.status == "success" and self.score is None:
            raise ValueError("score is required when status is success")
        if self.status == "insufficient_information":
            if self.score is not None:
                raise ValueError(
                    "score must be null when status is insufficient_information"
                )
            if self.issues:
                raise ValueError(
                    "issues must be empty when status is insufficient_information"
                )
            if self.strengths:
                raise ValueError(
                    "strengths must be empty when status is insufficient_information"
                )
            if self.career_elements_advice:
                raise ValueError(
                    "career_elements_advice must be empty when status is insufficient_information"
                )
        return self


class EvidenceRef(BaseModel):
    turn_index: int
    speaker: str
    quote: str
    note: str | None = None


class DimensionScore(BaseModel):
    id: str
    name: str
    score: int | None = None
    level: str | None = None
    basis: str | None = None
    comment: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class BetterPhrase(BaseModel):
    diagnostic_dimension_id: str | None = None
    original: str | None = None
    suggestion: str
    reason: str


class CoachCareerElementAdvice(BaseModel):
    element: str
    suggestion: str
    reason: str


class RiskItem(BaseModel):
    rule_id: str | None = None
    severity: RiskSeverity = "low"
    category: str = "沟通风险"
    matched_text: str | None = None
    explanation: str
    safer_phrase: str | None = None


class CoachTaskResult(BaseModel):
    task_id: str
    task_name: str
    status: TaskStatus = "success"
    score: int | None = None
    summary: str
    dimension_scores: list[DimensionScore] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    improvement_points: list[str] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)
    better_phrases: list[BetterPhrase] = Field(default_factory=list)
    career_elements_advice: list[CoachCareerElementAdvice] = Field(
        default_factory=list
    )
    citations: list[dict] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"completed", "complete", "ok", "passed", "pass"}:
                return "success"
            if normalized in {"insufficient", "not_enough_information", "not_applicable", "n/a"}:
                return "insufficient_information"
            if normalized in {"error", "failure"}:
                return "failed"
        return value

    @field_validator(
        "dimension_scores",
        "evidence",
        "strengths",
        "improvement_points",
        "risks",
        "better_phrases",
        "career_elements_advice",
        "citations",
        mode="before",
    )
    @classmethod
    def normalize_list_fields(cls, value: Any, info: ValidationInfo) -> Any:
        if info.field_name == "dimension_scores":
            return cls._normalize_dimension_scores(value)
        if info.field_name == "risks":
            return cls._normalize_risks(value)
        if info.field_name == "better_phrases":
            return cls._normalize_better_phrases(value)
        if info.field_name == "citations":
            return cls._normalize_citations(value)
        if info.field_name in {"strengths", "improvement_points"}:
            return cls._normalize_text_points(value)
        return cls._as_list(value)

    @classmethod
    def _as_list(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, dict):
            if not value:
                return []
            if any(key in value for key in {"id", "turn_index", "suggestion", "rule_id", "source", "chunk_id"}):
                return [value]
            return list(value.values())
        return [value]

    @classmethod
    def _normalize_text_points(cls, value: Any) -> list[str]:
        items = [value] if isinstance(value, dict) and "point" in value else cls._as_list(value)
        normalized: list[str] = []
        for item in items:
            text = item.get("point") if isinstance(item, dict) else item if isinstance(item, str) else None
            if isinstance(text, str) and (cleaned := text.strip()):
                normalized.append(cleaned)
        return normalized

    @classmethod
    def _normalize_dimension_scores(cls, value: Any) -> list[Any]:
        items = cls._as_list(value)
        normalized: list[Any] = []
        if isinstance(value, dict) and not any(key in value for key in {"id", "name", "score", "level", "comment"}):
            items = [
                {"id": str(key), "name": str(key), "score": item}
                if isinstance(item, (int, float))
                else {"id": str(key), "name": str(key), **item}
                if isinstance(item, dict)
                else {"id": str(key), "name": str(key), "comment": str(item)}
                for key, item in value.items()
            ]
        for index, item in enumerate(items):
            if isinstance(item, dict):
                normalized.append(cls._normalize_dimension_score_item(item, index))
            elif isinstance(item, (int, float)):
                normalized.append({"id": f"dimension_{index + 1}", "name": f"dimension_{index + 1}", "score": item})
            else:
                normalized.append({"id": f"dimension_{index + 1}", "name": f"dimension_{index + 1}", "comment": str(item)})
        return normalized

    @classmethod
    def _normalize_dimension_score_item(cls, item: dict[str, Any], index: int) -> dict[str, Any]:
        normalized = dict(item)
        dimension_id = normalized.pop("dimension_id", None) or normalized.pop("dimension", None)
        dimension_name = (
            normalized.pop("dimension_name", None)
            or normalized.pop("dimension_label", None)
            or normalized.pop("label", None)
            or normalized.pop("title", None)
        )
        if "id" not in normalized:
            normalized["id"] = str(dimension_id or dimension_name or f"dimension_{index + 1}")
        if "name" not in normalized:
            normalized["name"] = str(dimension_name or dimension_id or normalized["id"])
        if "comment" not in normalized:
            comment = normalized.pop("feedback", None) or normalized.pop("explanation", None) or normalized.pop("reason", None)
            if comment is not None:
                normalized["comment"] = str(comment)
        return normalized

    @classmethod
    def _normalize_risks(cls, value: Any) -> list[Any]:
        normalized: list[Any] = []
        for item in cls._as_list(value):
            if isinstance(item, dict):
                if "explanation" not in item:
                    item = {**item, "explanation": str(item.get("description") or item.get("risk") or item.get("summary") or item)}
                normalized.append(item)
            else:
                normalized.append({"explanation": str(item)})
        return normalized

    @classmethod
    def _normalize_better_phrases(cls, value: Any) -> list[Any]:
        normalized: list[Any] = []
        for item in cls._as_list(value):
            if isinstance(item, dict):
                suggestion = item.get("suggestion") or item.get("suggested_phrase") or item.get("better_phrase") or item.get("safer_phrase") or item.get("phrase")
                reason = item.get("reason") or item.get("explanation") or "建议替换表达，降低沟通风险。"
                original = item.get("original") or item.get("original_context") or item.get("matched_text")
                diagnostic_dimension_id = item.get("diagnostic_dimension_id")
                normalized.append(
                    {
                        "diagnostic_dimension_id": (
                            str(diagnostic_dimension_id).strip()
                            if diagnostic_dimension_id
                            else None
                        ),
                        "original": original,
                        "suggestion": str(suggestion or item),
                        "reason": str(reason),
                    }
                )
            else:
                normalized.append({"suggestion": str(item), "reason": "建议替换表达，降低沟通风险。"})
        return normalized

    @classmethod
    def _normalize_citations(cls, value: Any) -> list[Any]:
        normalized: list[Any] = []
        for item in cls._as_list(value):
            if isinstance(item, dict):
                normalized.append(item)
            else:
                normalized.append({"source": str(item)})
        return normalized

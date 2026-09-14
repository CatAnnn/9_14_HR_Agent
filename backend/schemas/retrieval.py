from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RetrievalQuery(BaseModel):
    agent_name: str
    intent_id: str | None = None
    run_mode: str | None = None
    query: str
    metadata_filter: dict = Field(default_factory=dict)
    top_k: int = 5


class RetrievedChunk(BaseModel):
    chunk_id: str
    source_id: str
    title: str
    scope: str = "unknown"
    text: str
    score: float = 0.0
    metadata: dict = Field(default_factory=dict)


class CitationReference(BaseModel):
    """Minimal model-facing evidence reference, validated by the backend."""

    # Keep the generated response schema narrow, but do not reject an otherwise
    # usable reference solely because the model added harmless metadata.
    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        json_schema_extra={"additionalProperties": False},
    )

    source_ref: str = Field(min_length=1)
    highlight_text: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)


def normalize_citation_references(
    value: Any,
) -> list[CitationReference]:
    """Keep every individually valid optional reference.

    Citation enrichment must never invalidate its parent score, issue, or
    recommendation. Invalid entries are discarded independently; valid entries
    are not dropped merely because the model returned more than an expected
    presentation count.
    """

    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]

    normalized: list[CitationReference] = []
    for item in items:
        try:
            reference = CitationReference.model_validate(item)
        except (TypeError, ValueError):
            continue
        normalized.append(reference)
    return normalized


class CitationAnchor(BaseModel):
    """Validated link from one generated phrase to its exact source context."""

    target: str
    highlight_text: str
    source_quote: str
    source_context: str


class Citation(BaseModel):
    chunk_id: str | None = None
    source_id: str
    title: str
    scope: str = "unknown"
    quote: str | None = None
    targets: list[str] = Field(default_factory=list)
    source_type: Literal["knowledge_base", "skill"] | None = None
    anchors: list[CitationAnchor] = Field(default_factory=list)

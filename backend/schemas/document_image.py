from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class DocumentVisualNode(BaseModel):
    """A verifiable object or labelled region in a document visual."""

    node_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    role: str = ""
    evidence: str = ""
    uncertain: bool = False

    @field_validator("node_id", "label", "role", "evidence", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


class DocumentVisualRelation(BaseModel):
    """An explicitly visible connection between two visual nodes."""

    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    evidence: str = ""
    uncertain: bool = False

    @field_validator(
        "source_id",
        "target_id",
        "relation",
        "evidence",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


class DocumentVisualStep(BaseModel):
    """One explicitly ordered step in a process, cycle, or timeline."""

    order: int = Field(ge=1)
    text: str = Field(min_length=1)
    node_id: str = ""
    evidence: str = ""
    uncertain: bool = False

    @field_validator("text", "node_id", "evidence", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


class DocumentVisualTable(BaseModel):
    """A table whose row and column boundaries are visually supported."""

    title: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    evidence: str = ""
    uncertain: bool = False

    @field_validator("title", "evidence", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("headers", mode="before")
    @classmethod
    def normalize_headers(cls, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values]

    @field_validator("rows", mode="before")
    @classmethod
    def normalize_rows(cls, rows: Any) -> list[list[str]]:
        if not isinstance(rows, list):
            return []
        return [
            [str(value).strip() for value in row]
            for row in rows
            if isinstance(row, list)
        ]


class DocumentImageAnalysis(BaseModel):
    image_ref: str = Field(min_length=1)
    is_informative: bool
    category: Literal[
        "chart",
        "diagram",
        "table",
        "screenshot",
        "photo",
        "logo",
        "decorative",
        "other",
    ] = "other"
    description: str = ""
    visible_text: str = ""
    key_facts: list[str] = Field(default_factory=list)
    layout_type: Literal[
        "none",
        "flowchart",
        "timeline",
        "matrix",
        "hierarchy",
        "cycle",
        "process",
        "comparison",
        "chart",
        "table",
        "dashboard",
        "screenshot",
        "photo",
        "mixed",
        "other",
    ] = "none"
    nodes: list[DocumentVisualNode] = Field(default_factory=list)
    relations: list[DocumentVisualRelation] = Field(default_factory=list)
    ordered_steps: list[DocumentVisualStep] = Field(default_factory=list)
    tables: list[DocumentVisualTable] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("image_ref", "description", "visible_text", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator(
        "nodes",
        "relations",
        "ordered_steps",
        "tables",
        mode="before",
    )
    @classmethod
    def normalize_optional_structure_lists(cls, values: Any) -> list[Any]:
        return values if isinstance(values, list) else []

    @field_validator("key_facts", "evidence", "uncertainties", mode="before")
    @classmethod
    def normalize_text_list(cls, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = str(value).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            output.append(normalized)
        return output

    @model_validator(mode="after")
    def discard_unverifiable_structure(self) -> "DocumentImageAnalysis":
        """Keep one malformed optional item from invalidating the whole page."""

        node_ids = {node.node_id for node in self.nodes}
        valid_relations = [
            relation
            for relation in self.relations
            if relation.source_id in node_ids and relation.target_id in node_ids
        ]
        if len(valid_relations) != len(self.relations):
            self.relations = valid_relations
            message = "已忽略无法绑定到已识别节点的视觉关系。"
            if message not in self.uncertainties:
                self.uncertainties.append(message)
        valid_tables = [
            table
            for table in self.tables
            if any(table.headers) or any(cell for row in table.rows for cell in row)
        ]
        if len(valid_tables) != len(self.tables):
            self.tables = valid_tables
            message = "已忽略没有可辨认表头或单元格的视觉表格。"
            if message not in self.uncertainties:
                self.uncertainties.append(message)
        return self


class DocumentImageAnalysisBatch(BaseModel):
    analyses: list[DocumentImageAnalysis] = Field(default_factory=list)


class DocumentImageEnrichmentResult(BaseModel):
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

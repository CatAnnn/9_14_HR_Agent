from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import statistics
import sys
import unicodedata
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings  # noqa: E402
from backend.rag.structured_facts import exact_query_terms  # noqa: E402
from backend.repositories.postgres_repository import (  # noqa: E402
    PostgresRepository,
)
from backend.vectorstore.index_manager import IndexManager  # noqa: E402


@dataclass(frozen=True)
class StructuralCase:
    case_id: str
    query: str
    scope: str
    required_term_groups: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class UnitRecord:
    unit_id: str
    parent_chunk_id: str
    scope: str
    unit_type: str
    text: str
    search_text: str
    metadata: dict[str, Any]
    exact_facts: tuple[dict[str, Any], ...]
    parent_text: str
    is_retrieval_unit: bool = True


CASES = (
    StructuralCase(
        "g9_output_standard",
        "G9 岗位等级对应的结果、能力和行为要求",
        "job_level",
        (("G9",), ("结果",), ("能力",), ("行为",)),
    ),
    StructuralCase(
        "sl1_output_standard",
        "SL1 岗位等级对应的结果、能力和行为要求",
        "job_level",
        (("SL1",), ("结果",), ("能力",), ("行为",)),
    ),
    StructuralCase(
        "career_cross_divisional",
        "Career Elements Cross-divisional experience",
        "career",
        (("cross divisional", "cross division"),),
    ),
    StructuralCase(
        "career_cross_functional",
        "Career Elements Cross-functional experience",
        "career",
        (("cross functional", "cross function"),),
    ),
    StructuralCase(
        "career_international",
        "Career Elements International experience",
        "career",
        (("international experience",),),
    ),
    StructuralCase(
        "career_associate_leadership",
        "Career Elements Associate leadership experience",
        "career",
        (("associate leadership experience",),),
    ),
    StructuralCase(
        "career_project_leadership",
        "Career Elements Project leadership experience",
        "career",
        (("project leadership experience",),),
    ),
    StructuralCase(
        "career_all_five_elements",
        "Career Elements 五项职业要素",
        "career",
        (
            ("cross divisional", "cross division"),
            ("cross functional", "cross function"),
            ("international experience",),
            ("associate leadership experience",),
            ("project leadership experience",),
        ),
    ),
    StructuralCase(
        "culture_innovation_to_shape",
        "中国区高绩效文化 Innovation to SHAPE 创变未来",
        "culture",
        (("innovation to shape", "创变未来"),),
    ),
    StructuralCase(
        "culture_mission_label",
        "博世高绩效文化 使命必达",
        "culture",
        (("使命必达",),),
    ),
    StructuralCase(
        "culture_commitment_behaviors",
        "博世高绩效文化 Commitment to WIN QCD",
        "culture",
        (("commitment to win",), ("QCD",)),
    ),
    StructuralCase(
        "culture_collaboration_label",
        "博世高绩效文化 协同共进",
        "culture",
        (("协同共进",),),
    ),
    StructuralCase(
        "culture_collaboration_behaviors",
        "博世高绩效文化 Collaboration to DELIVER shared goals",
        "culture",
        (("collaboration to deliver",), ("shared goals",)),
    ),
    StructuralCase(
        "performance_asr_rating",
        "ASR rating",
        "performance",
        (("ASR",),),
    ),
    StructuralCase(
        "performance_what_dimension",
        "WHAT dimension",
        "performance",
        (("WHAT",),),
    ),
    StructuralCase(
        "performance_how_dimension",
        "HOW dimension",
        "performance",
        (("HOW",),),
    ),
    StructuralCase(
        "emotion_tactical_empathy",
        "策略性的同理心",
        "emotion",
        (("策略性的同理心",),),
    ),
    StructuralCase(
        "emotion_labeling",
        "标注消极情感",
        "emotion",
        (("标注",), ("消极情感",)),
    ),
    StructuralCase(
        "emotion_calibrated_questions",
        "准备好校准问题",
        "emotion",
        (("校准问题",),),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the persisted V2 retrieval-unit structure with the "
            "current in-memory structure without calling embedding or rerank models."
        )
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = "".join(
        character if character.isalnum() else " " for character in text
    )
    return " ".join(normalized.split())


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _unit_document(unit: UnitRecord) -> str:
    heading_path = unit.metadata.get("heading_path") or []
    table_headers = unit.metadata.get("table_headers") or []
    return "\n".join(
        (
            unit.text,
            unit.search_text,
            " ".join(str(value) for value in heading_path),
            " ".join(str(value) for value in table_headers),
            str(unit.metadata.get("previous_unit_text") or ""),
            str(unit.metadata.get("next_unit_text") or ""),
        )
    )


def _matches_group(text: str, group: Iterable[str]) -> bool:
    normalized = _normalize(text)
    return any(_normalize(alternative) in normalized for alternative in group)


def _coverage(texts: Iterable[str], groups: tuple[tuple[str, ...], ...]) -> float:
    combined = "\n".join(str(text or "") for text in texts)
    if not groups:
        return 1.0
    return sum(_matches_group(combined, group) for group in groups) / len(groups)


def _first_complete_rank(
    texts: list[str],
    groups: tuple[tuple[str, ...], ...],
) -> int | None:
    for rank, text in enumerate(texts, start=1):
        if _coverage((text,), groups) == 1.0:
            return rank
    return None


def _rank_units(
    units: list[UnitRecord],
    case: StructuralCase,
) -> list[UnitRecord]:
    eligible = [unit for unit in units if unit.scope == case.scope]
    documents = {unit.unit_id: _unit_document(unit) for unit in eligible}
    frequencies = []
    for group in case.required_term_groups:
        frequencies.append(
            max(
                1,
                sum(
                    _matches_group(document, group)
                    for document in documents.values()
                ),
            )
        )
    corpus_size = max(1, len(eligible))

    def score(unit: UnitRecord) -> tuple[float, float, int, str]:
        document = documents[unit.unit_id]
        matched = [
            _matches_group(document, group)
            for group in case.required_term_groups
        ]
        weighted = sum(
            math.log((corpus_size + 1) / (frequency + 1)) + 1.0
            for present, frequency in zip(matched, frequencies, strict=True)
            if present
        )
        coverage = sum(matched) / max(1, len(matched))
        complete_bonus = 4.0 if coverage == 1.0 else 0.0
        specificity = 1.0 / max(1.0, math.log2(len(unit.text) + 2))
        return (
            coverage,
            weighted + complete_bonus + specificity,
            -len(unit.text),
            unit.unit_id,
        )

    return sorted(eligible, key=score, reverse=True)


def _rank_parents(
    ranked_units: list[UnitRecord],
) -> list[tuple[str, str]]:
    grouped: dict[str, list[tuple[int, UnitRecord]]] = defaultdict(list)
    for rank, unit in enumerate(ranked_units, start=1):
        grouped[unit.parent_chunk_id].append((rank, unit))
    ranked = sorted(
        grouped.items(),
        key=lambda item: (
            min(rank for rank, _ in item[1]),
            -min(8, len(item[1])),
            item[0],
        ),
    )
    return [
        (parent_id, entries[0][1].parent_text)
        for parent_id, entries in ranked
    ]


def _exact_coverage(
    ranked_units: list[UnitRecord],
    query: str,
) -> float | None:
    expected = exact_query_terms(query)
    if not expected:
        return None
    indexed = {
        str(fact.get("normalized_value") or "")
        for unit in ranked_units[:8]
        for fact in unit.exact_facts
        if isinstance(fact, dict)
    }
    return len(set(expected) & indexed) / len(set(expected))


def _evaluate(
    units: list[UnitRecord],
    case: StructuralCase,
) -> dict[str, Any]:
    ranked_candidates = _rank_units(units, case)
    ranked_units = [
        unit for unit in ranked_candidates if unit.is_retrieval_unit
    ]
    unit_texts = [_unit_document(unit) for unit in ranked_units]
    ranked_parents = _rank_parents(ranked_candidates)
    parent_texts = [text for _, text in ranked_parents]
    unit_rank = _first_complete_rank(unit_texts, case.required_term_groups)
    parent_rank = _first_complete_rank(parent_texts, case.required_term_groups)
    complete_unit = ranked_units[unit_rank - 1] if unit_rank else None
    unit_chars = len(complete_unit.text) if complete_unit else None
    parent_chars = len(complete_unit.parent_text) if complete_unit else None
    exact_coverage = _exact_coverage(ranked_units, case.query)
    return {
        "case_id": case.case_id,
        "scope": case.scope,
        "unit_complete_rank": unit_rank,
        "parent_complete_rank": parent_rank,
        "unit_recall_at_1": bool(unit_rank and unit_rank <= 1),
        "unit_recall_at_3": bool(unit_rank and unit_rank <= 3),
        "unit_recall_at_8": bool(unit_rank and unit_rank <= 8),
        "parent_recall_at_1": bool(parent_rank and parent_rank <= 1),
        "parent_recall_at_3": bool(parent_rank and parent_rank <= 3),
        "parent_recall_at_8": bool(parent_rank and parent_rank <= 8),
        "unit_evidence_coverage_at_8": round(
            _coverage(unit_texts[:8], case.required_term_groups),
            4,
        ),
        "parent_evidence_coverage_at_8": round(
            _coverage(parent_texts[:8], case.required_term_groups),
            4,
        ),
        "indexed_exact_term_coverage_at_8": (
            round(exact_coverage, 4) if exact_coverage is not None else None
        ),
        "best_complete_unit_chars": unit_chars,
        "best_complete_parent_chars": parent_chars,
        "unit_parent_char_ratio": (
            round(unit_chars / parent_chars, 4)
            if unit_chars is not None and parent_chars
            else None
        ),
        "best_unit_id": complete_unit.unit_id if complete_unit else None,
        "best_parent_id": complete_unit.parent_chunk_id if complete_unit else None,
    }


def _load_v2_units(repository: PostgresRepository) -> list[UnitRecord]:
    with repository.connection() as connection:
        rows = connection.execute(
            """
            SELECT
                u.unit_id, u.parent_chunk_id, u.unit_type, u.text,
                u.search_text, u.metadata, c.scope, c.text AS parent_text
            FROM kb_retrieval_units u
            JOIN kb_chunks c ON c.chunk_id = u.parent_chunk_id
            ORDER BY u.scope, u.parent_chunk_id, u.unit_index, u.unit_id
            """
        ).fetchall()
        parent_rows = connection.execute(
            """
            SELECT chunk_id, scope, text, metadata
            FROM kb_chunks
            ORDER BY scope, chunk_id
            """
        ).fetchall()
    units = [
        UnitRecord(
            unit_id=str(row["unit_id"]),
            parent_chunk_id=str(row["parent_chunk_id"]),
            scope=str(row["scope"]),
            unit_type=str(row["unit_type"]),
            text=str(row["text"] or ""),
            search_text=str(row["search_text"] or ""),
            metadata=_mapping(row["metadata"]),
            exact_facts=(),
            parent_text=str(row["parent_text"] or ""),
        )
        for row in rows
    ]
    parent_ids = {unit.parent_chunk_id for unit in units}
    for row in parent_rows:
        chunk_id = str(row["chunk_id"])
        if chunk_id in parent_ids:
            continue
        metadata = _mapping(row["metadata"])
        parent_text = str(row["text"] or "")
        units.append(
            UnitRecord(
                unit_id=f"parent:{chunk_id}",
                parent_chunk_id=chunk_id,
                scope=str(row["scope"]),
                unit_type="parent_fallback",
                text=parent_text,
                search_text=str(
                    metadata.get("search_text") or parent_text
                ),
                metadata=metadata,
                exact_facts=(),
                parent_text=parent_text,
                is_retrieval_unit=False,
            )
        )
    return units


def _load_current_units(
    manager: IndexManager,
) -> tuple[list[UnitRecord], int, int]:
    raw_root = manager.settings.data_dir / "kb_raw"
    chunks_by_scope, documents = manager._build_chunks(raw_root)
    parent_chunks = {
        str(chunk.chunk_id): chunk
        for chunks in chunks_by_scope.values()
        for chunk in chunks
    }
    units_by_scope = manager._retrieval_units_by_scope(documents)
    units = []
    for scope, values in units_by_scope.items():
        for value in values:
            parent_chunk_id = str(value.get("parent_chunk_id") or "")
            parent = parent_chunks[parent_chunk_id]
            units.append(
                UnitRecord(
                    unit_id=str(value.get("unit_id") or ""),
                    parent_chunk_id=parent_chunk_id,
                    scope=scope,
                    unit_type=str(value.get("unit_type") or ""),
                    text=str(value.get("text") or ""),
                    search_text=str(value.get("search_text") or ""),
                    metadata=_mapping(value.get("metadata")),
                    exact_facts=tuple(value.get("exact_facts") or ()),
                    parent_text=str(parent.text or ""),
                )
            )
    parent_ids = {unit.parent_chunk_id for unit in units}
    for parent_chunk_id, parent in parent_chunks.items():
        if parent_chunk_id in parent_ids:
            continue
        metadata = dict(parent.metadata or {})
        units.append(
            UnitRecord(
                unit_id=f"parent:{parent_chunk_id}",
                parent_chunk_id=parent_chunk_id,
                scope=parent.scope,
                unit_type="parent_fallback",
                text=parent.text,
                search_text=str(
                    metadata.get("search_text") or parent.text
                ),
                metadata=metadata,
                exact_facts=(),
                parent_text=parent.text,
                is_retrieval_unit=False,
            )
        )
    return units, sum(map(len, chunks_by_scope.values())), len(documents)


def _summary(
    units: list[UnitRecord],
    results: list[dict[str, Any]],
    *,
    parent_chunk_count: int,
    document_count: int | None,
) -> dict[str, Any]:
    retrieval_units = [unit for unit in units if unit.is_retrieval_unit]
    exact_values = [
        float(result["indexed_exact_term_coverage_at_8"])
        for result in results
        if result["indexed_exact_term_coverage_at_8"] is not None
    ]
    unit_lengths = [
        int(result["best_complete_unit_chars"])
        for result in results
        if result["best_complete_unit_chars"] is not None
    ]
    return {
        "parent_chunk_count": parent_chunk_count,
        "document_count": document_count,
        "retrieval_unit_count": len(retrieval_units),
        "parent_fallback_count": len(units) - len(retrieval_units),
        "unit_types": dict(
            sorted(Counter(unit.unit_type for unit in retrieval_units).items())
        ),
        "locator_start_coverage": round(
            sum("parent_text_start" in unit.metadata for unit in retrieval_units)
            / max(1, len(retrieval_units)),
            4,
        ),
        "locator_end_coverage": round(
            sum("parent_text_end" in unit.metadata for unit in retrieval_units)
            / max(1, len(retrieval_units)),
            4,
        ),
        "semantic_item_coverage": round(
            sum(
                bool(unit.metadata.get("semantic_item_id"))
                for unit in retrieval_units
            )
            / max(1, len(retrieval_units)),
            4,
        ),
        "metrics": {
            field: round(
                sum(float(result[field]) for result in results) / len(results),
                4,
            )
            for field in (
                "unit_recall_at_1",
                "unit_recall_at_3",
                "unit_recall_at_8",
                "parent_recall_at_1",
                "parent_recall_at_3",
                "parent_recall_at_8",
                "unit_evidence_coverage_at_8",
                "parent_evidence_coverage_at_8",
            )
        }
        | {
            "indexed_exact_term_coverage_at_8": (
                round(sum(exact_values) / len(exact_values), 4)
                if exact_values
                else 0.0
            ),
            "median_best_complete_unit_chars": (
                round(statistics.median(unit_lengths), 1)
                if unit_lengths
                else None
            ),
        },
        "cases": results,
    }


def main() -> int:
    args = parse_args()
    settings = get_settings()
    repository = PostgresRepository(database_url=settings.admin_database_url)
    manager = IndexManager(settings=settings)

    v2_units = _load_v2_units(repository)
    current_units, current_parent_count, document_count = _load_current_units(manager)
    v2_results = [_evaluate(v2_units, case) for case in CASES]
    current_results = [_evaluate(current_units, case) for case in CASES]
    with repository.connection() as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM kb_chunks").fetchone()
        v2_parent_count = int(row["count"])

    v2 = _summary(
        v2_units,
        v2_results,
        parent_chunk_count=v2_parent_count,
        document_count=None,
    )
    current = _summary(
        current_units,
        current_results,
        parent_chunk_count=current_parent_count,
        document_count=document_count,
    )
    deltas = {
        key: round(current["metrics"][key] - value, 4)
        for key, value in v2["metrics"].items()
        if isinstance(value, (int, float))
        and isinstance(current["metrics"].get(key), (int, float))
    }
    report = {
        "comparison_kind": "deterministic_localization_probe",
        "limitations": (
            "Embedding and model reranking are intentionally excluded; this "
            "report compares index structure, exact evidence, localization, "
            "and required-information coverage."
        ),
        "index_version": settings.kb_index_version,
        "case_count": len(CASES),
        "v2": v2,
        "current": current,
        "metric_deltas_current_minus_v2": deltas,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

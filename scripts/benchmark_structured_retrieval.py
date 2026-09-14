from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time
import unicodedata
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings  # noqa: E402
from backend.rag.reranker import Reranker  # noqa: E402
from backend.services.retrieval_service import RetrievalService  # noqa: E402


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    query: str
    scope: str
    required_term_groups: tuple[tuple[str, ...], ...]


CASES = (
    RetrievalCase(
        "g9_output_standard",
        "G9 岗位等级对应的结果、能力和行为要求是什么",
        "job_level",
        (("G9",), ("结果",), ("能力",), ("行为",)),
    ),
    RetrievalCase(
        "sl1_output_standard",
        "SL1 岗位等级对应的结果、能力和行为要求是什么",
        "job_level",
        (("SL1",), ("结果",), ("能力",), ("行为",)),
    ),
    RetrievalCase(
        "career_cross_divisional",
        "Career Elements 中 Cross-divisional experience 的要求",
        "career",
        (("cross divisional", "cross divisional experience"),),
    ),
    RetrievalCase(
        "career_cross_functional",
        "Career Elements 中 Cross-functional experience 的要求",
        "career",
        (("cross functional", "cross functional experience"),),
    ),
    RetrievalCase(
        "career_international",
        "Career Elements 中 International experience 的要求",
        "career",
        (("international experience",),),
    ),
    RetrievalCase(
        "career_associate_leadership",
        "Career Elements 中 Associate leadership experience 的要求",
        "career",
        (("associate leadership experience",),),
    ),
    RetrievalCase(
        "career_project_leadership",
        "Career Elements 中 Project leadership experience 的要求",
        "career",
        (("project leadership experience",),),
    ),
    RetrievalCase(
        "career_all_five_elements",
        "Career Elements 五项职业要素分别是什么",
        "career",
        (
            ("cross divisional",),
            ("cross functional",),
            ("international experience",),
            ("associate leadership experience",),
            ("project leadership experience",),
        ),
    ),
    RetrievalCase(
        "culture_mission_accomplished",
        "博世高绩效文化中的使命必达是什么",
        "culture",
        (
            ("commitment to win",),
            ("qcd", "质量 成本 交付"),
        ),
    ),
    RetrievalCase(
        "culture_collaboration",
        "博世高绩效文化中的协同共进是什么",
        "culture",
        (
            ("collaboration to deliver",),
            ("shared goals", "共同目标"),
        ),
    ),
    RetrievalCase(
        "culture_customer_centricity",
        "Customer-centricity to GROW 聚力共赢 高绩效文化行为",
        "culture",
        (("customer-centricity to grow", "聚力共赢"),),
    ),
    RetrievalCase(
        "performance_asr_what_how",
        "ASR rating 中 WHAT 和 HOW 的绩效规则",
        "performance",
        (("ASR",), ("WHAT",), ("HOW",)),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare structured parent and retrieval-unit evidence using the "
            "configured production retrieval path."
        )
    )
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Run only the named case; repeat to select multiple cases.",
    )
    return parser.parse_args()


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = "".join(character if character.isalnum() else " " for character in text)
    return " ".join(normalized.split())


def _contains_group(text: str, alternatives: Iterable[str]) -> bool:
    normalized = _normalize(text)
    return any(_normalize(alternative) in normalized for alternative in alternatives)


def _coverage(texts: Iterable[str], groups: tuple[tuple[str, ...], ...]) -> float:
    combined = "\n".join(str(text or "") for text in texts)
    if not groups:
        return 1.0
    matched = sum(_contains_group(combined, group) for group in groups)
    return matched / len(groups)


def _first_complete_rank(
    texts: list[str],
    groups: tuple[tuple[str, ...], ...],
) -> int | None:
    for index, text in enumerate(texts, start=1):
        if _coverage((text,), groups) == 1.0:
            return index
    return None


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


def _unit_payloads(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values = metadata.get("retrieval_units")
    if not isinstance(values, list) or not values:
        selected = metadata.get("retrieval_unit")
        values = [selected] if isinstance(selected, dict) else []
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        unit = _mapping(value)
        unit_id = str(unit.get("unit_id") or "")
        if not unit or (unit_id and unit_id in seen):
            continue
        if unit_id:
            seen.add(unit_id)
        output.append(unit)
    return output


def _unit_document(unit: dict[str, Any]) -> str:
    metadata = _mapping(unit.get("metadata"))
    exact_facts = unit.get("exact_facts") or []
    fact_text = []
    for fact in exact_facts if isinstance(exact_facts, list) else []:
        if isinstance(fact, dict):
            fact_text.extend(
                (
                    str(fact.get("normalized_value") or ""),
                    str(fact.get("source_quote") or ""),
                )
            )
    return "\n".join(
        (
            str(unit.get("text") or ""),
            str(unit.get("search_text") or ""),
            str(unit.get("rerank_search_text") or ""),
            " ".join(str(item) for item in metadata.get("heading_path") or []),
            " ".join(str(item) for item in metadata.get("table_headers") or []),
            *fact_text,
        )
    )


def _case_result(
    service: RetrievalService,
    case: RetrievalCase,
    *,
    top_k: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    chunks = service.retrieve(
        "model_rag_tool",
        {
            "tool_query": case.query,
            "requested_scopes": [case.scope],
            "_disable_knowledge_skills": True,
        },
        top_k=top_k,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    parent_texts = [chunk.text for chunk in chunks]
    ranked_units: list[tuple[str, str, str]] = []
    for chunk in chunks:
        for unit in _unit_payloads(dict(chunk.metadata or {})):
            ranked_units.append(
                (
                    str(unit.get("unit_id") or ""),
                    chunk.chunk_id,
                    _unit_document(unit),
                )
            )
    unit_texts = [item[2] for item in ranked_units]
    parent_rank = _first_complete_rank(parent_texts, case.required_term_groups)
    unit_rank = _first_complete_rank(unit_texts, case.required_term_groups)

    return {
        "case_id": case.case_id,
        "scope": case.scope,
        "query": case.query,
        "elapsed_ms": round(elapsed_ms, 3),
        "returned_parent_count": len(chunks),
        "returned_unit_count": len(ranked_units),
        "parent_complete_rank": parent_rank,
        "unit_complete_rank": unit_rank,
        "parent_recall_at_1": bool(parent_rank and parent_rank <= 1),
        "parent_recall_at_3": bool(parent_rank and parent_rank <= 3),
        "parent_recall_at_8": bool(parent_rank and parent_rank <= 8),
        "unit_recall_at_1": bool(unit_rank and unit_rank <= 1),
        "unit_recall_at_3": bool(unit_rank and unit_rank <= 3),
        "unit_recall_at_8": bool(unit_rank and unit_rank <= 8),
        "parent_evidence_coverage_at_8": round(
            _coverage(parent_texts[:8], case.required_term_groups),
            4,
        ),
        "unit_evidence_coverage_at_8": round(
            _coverage(unit_texts[:8], case.required_term_groups),
            4,
        ),
        "parent_ids": [chunk.chunk_id for chunk in chunks[:8]],
        "unit_ids": [item[0] for item in ranked_units[:8]],
        "scope_valid": all(chunk.scope == case.scope for chunk in chunks),
    }


def _mean(results: list[dict[str, Any]], field: str) -> float:
    if not results:
        return 0.0
    return round(sum(float(result[field]) for result in results) / len(results), 4)


def main() -> int:
    args = parse_args()
    selected = set(args.case_ids or [])
    cases = [case for case in CASES if not selected or case.case_id in selected]
    missing = selected - {case.case_id for case in cases}
    if missing:
        raise SystemExit(f"Unknown benchmark cases: {sorted(missing)}")
    if not cases:
        raise SystemExit("No benchmark cases selected.")

    settings = get_settings()
    service = RetrievalService()
    try:
        results = [
            _case_result(service, case, top_k=max(1, int(args.top_k)))
            for case in cases
        ]
    finally:
        asyncio.run(service.shutdown())

    report = {
        "label": args.label,
        "retrieval_policy": Reranker.DOCUMENT_POLICY_VERSION,
        "embedding_model": settings.effective_embedding_model,
        "embedding_dimensions": settings.effective_embedding_dimensions,
        "embedding_profile_id": settings.embedding_profile_id,
        "index_version": settings.kb_index_version,
        "case_count": len(results),
        "metrics": {
            field: _mean(results, field)
            for field in (
                "parent_recall_at_1",
                "parent_recall_at_3",
                "parent_recall_at_8",
                "unit_recall_at_1",
                "unit_recall_at_3",
                "unit_recall_at_8",
                "parent_evidence_coverage_at_8",
                "unit_evidence_coverage_at_8",
                "scope_valid",
            )
        },
        "mean_elapsed_ms": round(
            sum(result["elapsed_ms"] for result in results) / len(results),
            3,
        ),
        "cases": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

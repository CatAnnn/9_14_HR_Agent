from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Iterable


_WHITESPACE = re.compile(r"\s+")
_ORGANIZATION_PATH_SEPARATOR = re.compile(r"\s*(?:>|\\|\||›|→|;|；)\s*")
_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:SL\d+(?:G\d+)?|G\d+|P\d+|M\d+|TP\d+|ETP\d+|TCL|ASR|WHAT|HOW|PIP)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

CULTURE_EXACT_FACT_POLICY_VERSION = "china-high-performance-culture-v3"

_CANONICAL_TERMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "career_element",
        "Cross-divisional experience",
        (
            "cross-divisional experience",
            "cross divisional experience",
            "cross division experience",
            "cross division",
            "跨事业部经验",
            "跨事业部经历",
        ),
    ),
    (
        "career_element",
        "Cross-functional experience",
        (
            "cross-functional experience",
            "cross functional experience",
            "cross function experience",
            "cross function",
            "跨职能经验",
            "跨职能经历",
        ),
    ),
    ("career_element", "International experience", ("international experience", "国际经验", "国际化经验")),
    ("career_element", "Associate leadership experience", ("associate leadership experience", "员工领导经验", "人员领导经验")),
    ("career_element", "Project leadership experience", ("project leadership experience", "项目领导经验")),
    (
        "culture",
        "Collaboration to DELIVER",
        ("collaboration to deliver", "协同共进"),
    ),
    (
        "culture",
        "Innovation to SHAPE",
        ("innovation to shape", "创变未来"),
    ),
    (
        "culture",
        "Customer-centricity to GROW",
        (
            "customer-centricity to grow",
            "customer centricity to grow",
            "聚力共赢",
        ),
    ),
    (
        "culture",
        "Commitment to WIN",
        ("commitment to win", "使命必达"),
    ),
    ("performance", "ASR rating", ("asr rating", "asr评级", "asr 评级")),
    (
        "performance",
        "Performance Rating",
        ("performance rating", "绩效评级", "绩效等级"),
    ),
    (
        "performance",
        "WHAT dimension",
        ("what dimension", "what维度", "what 维度", "what-rating"),
    ),
    (
        "performance",
        "HOW dimension",
        ("how dimension", "how维度", "how 维度", "how-rating"),
    ),
)


def normalize_content(value: str) -> str:
    """Return a stable identity form without changing stored source text."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE.sub(" ", normalized).strip().casefold()


def canonical_content_id(value: str) -> str:
    return hashlib.sha256(normalize_content(value).encode("utf-8")).hexdigest()


def contextual_search_text(
    *,
    scope: str,
    title: str,
    heading_path: Iterable[str],
    text: str,
    table_headers: Iterable[str] = (),
) -> str:
    """Build deterministic embedding/BM25 text while retaining raw chunk text."""

    path = " > ".join(str(value).strip() for value in heading_path if str(value).strip())
    headers = " | ".join(str(value).strip() for value in table_headers if str(value).strip())
    labels = [f"Knowledge scope: {scope}", f"Document: {title}"]
    if path:
        labels.append(f"Section: {path}")
    if headers:
        labels.append(f"Table columns: {headers}")
    labels.append(str(text or "").strip())
    return "\n".join(value for value in labels if value).strip()


def contextual_exact_fact_text(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Include source headings when extracting facts from a child unit."""

    record = metadata or {}
    raw_heading_path = record.get("heading_path") or []
    heading_path = (
        [
            str(value).strip()
            for value in raw_heading_path
            if str(value).strip()
        ]
        if isinstance(raw_heading_path, (list, tuple))
        else []
    )
    return "\n".join([*heading_path, str(text or "").strip()]).strip()


def extract_exact_facts(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract source-backed business terms/codes with no generative inference."""

    source = str(text or "")
    normalized_source = normalize_content(source)
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    record = metadata or {}
    scope = normalize_content(str(record.get("scope") or ""))

    def append(fact_type: str, value: str, quote: str) -> None:
        normalized_value = normalize_content(value)
        identity = (fact_type, normalized_value)
        if not normalized_value or identity in seen:
            return
        seen.add(identity)
        facts.append(
            {
                "fact_type": fact_type,
                "normalized_value": normalized_value,
                "display_value": value.strip(),
                "source_quote": quote.strip(),
            }
        )

    for match in _CODE_PATTERN.finditer(source):
        value = unicodedata.normalize("NFKC", match.group(0)).upper()
        append("business_code", value, match.group(0))
        if scope == "performance" and value in {"WHAT", "HOW"}:
            append(
                "performance",
                f"{value} dimension",
                match.group(0),
            )

    for fact_type, canonical, aliases in _CANONICAL_TERMS:
        for alias in aliases:
            if normalize_content(alias) in normalized_source:
                append(fact_type, canonical, _source_phrase(source, alias))
                break

    for key in ("org_unit_code", "department_code", "cost_center", "organization_code"):
        value = str(record.get(key) or "").strip()
        if value:
            append("organization_code", value, value)

    for key in ("department_codes", "department_code_aliases"):
        raw_values = record.get(key) or []
        values = (
            raw_values
            if isinstance(raw_values, (list, tuple, set))
            else [raw_values]
        )
        for raw_value in values:
            value = str(raw_value or "").strip()
            if value:
                append("organization_code", value, value)

    organization_paths: list[str] = []
    normalized_paths: set[str] = set()
    for key in ("department_path", "department_path_normalized"):
        raw_value = record.get(key)
        if isinstance(raw_value, (list, tuple)):
            value = " > ".join(
                str(item).strip() for item in raw_value if str(item).strip()
            )
        else:
            value = str(raw_value or "").strip()
        normalized_path = normalize_content(value)
        if normalized_path and normalized_path not in normalized_paths:
            normalized_paths.add(normalized_path)
            organization_paths.append(value)
            append("organization_path", value, value)

    for path in organization_paths:
        for segment in _ORGANIZATION_PATH_SEPARATOR.split(path):
            segment = segment.strip()
            if len(normalize_content(segment)) >= 2:
                append("organization_unit", segment, segment)
    return facts


def exact_query_terms(query: str) -> list[str]:
    terms = [str(item["normalized_value"]) for item in extract_exact_facts(query)]
    expansions = {
        "asr": "asr rating",
        "how": "how dimension",
        "what": "what dimension",
    }
    terms.extend(
        expansion for term, expansion in expansions.items() if term in terms
    )
    return list(dict.fromkeys(terms))


def _source_phrase(source: str, alias: str) -> str:
    normalized_source = unicodedata.normalize("NFKC", source)
    normalized_alias = unicodedata.normalize("NFKC", alias)
    match = re.search(re.escape(normalized_alias), normalized_source, re.IGNORECASE)
    return match.group(0) if match else alias

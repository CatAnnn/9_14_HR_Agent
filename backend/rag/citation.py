from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher

from backend.observability.metrics import log_metric
from backend.schemas.retrieval import (
    Citation,
    CitationAnchor,
    CitationReference,
    RetrievedChunk,
)


CITATION_POLICY_VERSION = "normalized-anchor-v5"
REFERENCEABLE_CITATION_SCOPES = frozenset(
    {
        "career",
        "culture",
        "employee",
        "development_dialog",
        "job_level",
        "performance",
    }
)
_SKILL_SHARED_PHRASE_MIN_CHARS = 8
_SKILL_TRIGGER_MIN_CHARS = 5
_MAX_SOURCE_CONTEXT_CHARS = 900
_MAX_HIGHLIGHT_CHARS = 160
_MAX_HIGHLIGHT_LINES = 2
_MAX_HIGHLIGHT_SENTENCE_BREAKS = 2
_MAX_HIGHLIGHT_LIST_SEPARATORS = 3
_MAX_SOURCE_QUOTE_CHARS = 600
_MAX_SOURCE_QUOTE_LINES = 8
_GENERIC_HIGHLIGHTS = frozenset(
    {
        "员工",
        "建议",
        "文化",
        "目标",
        "绩效",
        "反馈",
        "表现",
        "发展",
    }
)
_BUSINESS_CODE_PATTERN = re.compile(
    r"(?i)^(?:G\d+|SL\d+(?:G\d+)?|ETP\d+|TP\d+|P\d+|ASR(?:\s+RATING)?|WHAT|HOW)$"
)
_BUSINESS_CODE_SEARCH_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9])(?:SL\d+(?:G\d+)?|ETP\d+|TP\d+|G\d+|P\d+)(?![a-z0-9])"
)
_AUTHORITY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "culture",
        (
            "高绩效文化",
            "我们的中国区高绩效文化",
            "协同共进",
            "创变未来",
            "聚力共赢",
            "使命必达",
            "collaboration to deliver",
            "innovation to shape",
            "customer-centricity to grow",
            "commitment to win",
            "be like a bosch",
        ),
    ),
    (
        "performance",
        ("asr rating", "what/how", "what 和 how", "绩效评级规则", "绩效评价规则"),
    ),
    (
        "development_dialog",
        ("发展对话方法", "development dialog", "共同探索", "共情总结"),
    ),
    (
        "career",
        (
            "career elements",
            "career element",
            "职业发展要素",
            "职业要素",
            "cross-divisional",
            "cross-functional",
            "project leadership experience",
        ),
    ),
)

def chunks_to_citations(chunks: list[RetrievedChunk]) -> list[Citation]:
    seen: set[str] = set()
    citations: list[Citation] = []
    for chunk in chunks:
        key = f"{chunk.source_id}:{chunk.title}"
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            Citation(
                source_id=chunk.source_id,
                title=chunk.title,
                scope=chunk.scope,
                quote=chunk.text[:160],
            )
        )
    return citations


def referenced_chunks_to_citations(
    chunks: Sequence[RetrievedChunk],
    targets_by_chunk: Mapping[str, Sequence[str]],
    *,
    include_unreferenced: bool = False,
) -> list[Citation]:
    """Build target-aware citations, optionally preserving unreferenced sources."""
    citations: list[Citation] = []
    seen: set[str] = set()
    for chunk in chunks:
        targets = targets_by_chunk.get(chunk.chunk_id)
        if not targets and not include_unreferenced:
            continue
        identity = (
            chunk.chunk_id if targets else f"{chunk.source_id}:{chunk.title}"
        )
        if identity in seen:
            continue
        seen.add(identity)
        citations.append(
            Citation(
                chunk_id=chunk.chunk_id if targets else None,
                source_id=chunk.source_id,
                title=chunk.title,
                scope=chunk.scope,
                quote=chunk.text if targets else chunk.text[:160],
                targets=list(dict.fromkeys(targets or [])),
            )
        )
    return citations


def skill_payloads_to_citations(
    skills: Sequence[Mapping[str, object]],
    texts_by_target: Mapping[str, str],
) -> list[Citation]:
    """Link only output phrases that are actually present in active Skill text."""
    citations: list[Citation] = []
    for skill in skills:
        core_knowledge = str(skill.get("core_knowledge") or "").strip()
        if not core_knowledge or bool(skill.get("stale")):
            continue
        scope = next(
            (
                normalized
                for raw_scope in skill.get("scopes") or []
                if (
                    normalized := _normalize_scope(str(raw_scope))
                ) in REFERENCEABLE_CITATION_SCOPES
            ),
            None,
        )
        if scope is None:
            continue

        matched_triggers = [
            str(trigger)
            for trigger in skill.get("matched_triggers") or []
            if str(trigger).strip()
        ]
        targets = [
            target
            for target, text in texts_by_target.items()
            if _text_uses_skill(text, core_knowledge, matched_triggers)
        ]
        if not targets:
            continue

        skill_id = str(skill.get("id") or "knowledge_skill").strip()
        version = str(skill.get("version") or "unknown").strip()
        citations.append(
            Citation(
                chunk_id=f"skill:{skill_id}:{version}",
                source_id=f"skill:{skill_id}",
                title=_skill_title(core_knowledge, skill_id),
                scope=scope,
                quote=core_knowledge,
                targets=list(dict.fromkeys(targets)),
                source_type="skill",
            )
        )
    return citations


def merge_citations(citations: Sequence[Citation]) -> list[Citation]:
    """Merge repeated evidence while retaining target order and full source text."""
    merged: dict[str, Citation] = {}
    for citation in citations:
        source_type = citation.source_type or "knowledge_base"
        identity = citation.chunk_id or (
            f"{citation.source_id}:{citation.title}:{citation.scope}"
        )
        key = f"{source_type}:{identity}"
        current = merged.get(key)
        if current is None:
            merged[key] = citation.model_copy(deep=True)
            continue
        anchors = _merge_anchors([*current.anchors, *citation.anchors])
        targets = list(
            dict.fromkeys(
                [
                    *current.targets,
                    *citation.targets,
                    *(anchor.target for anchor in anchors),
                ]
            )
        )
        quote = current.quote
        if len(citation.quote or "") > len(quote or ""):
            quote = citation.quote
        merged[key] = current.model_copy(
            update={"targets": targets, "quote": quote, "anchors": anchors}
        )
    return list(merged.values())


def _normalize_scope(scope: str) -> str:
    normalized = re.sub(
        r"[\s-]+",
        "_",
        unicodedata.normalize("NFKC", scope).casefold().strip(),
    )
    return "job_level" if normalized == "joblevel" else normalized


def _comparable_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if character.isalnum() or character in "+#"
    )


def _text_uses_skill(
    text: str,
    core_knowledge: str,
    matched_triggers: Sequence[str],
) -> bool:
    target = _comparable_text(text)
    source = _comparable_text(core_knowledge)
    if (
        len(target) < _SKILL_SHARED_PHRASE_MIN_CHARS
        or len(source) < _SKILL_SHARED_PHRASE_MIN_CHARS
    ):
        return False

    for trigger in matched_triggers:
        comparable_trigger = _comparable_text(trigger)
        if (
            len(comparable_trigger) >= _SKILL_TRIGGER_MIN_CHARS
            and comparable_trigger in target
            and comparable_trigger in source
        ):
            return True

    match = SequenceMatcher(None, target, source, autojunk=False).find_longest_match(
        0,
        len(target),
        0,
        len(source),
    )
    return match.size >= _SKILL_SHARED_PHRASE_MIN_CHARS


def _skill_title(core_knowledge: str, skill_id: str) -> str:
    for line in core_knowledge.splitlines():
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            title = re.sub(r"[*_`]", "", heading.group(1)).strip()
            if title:
                return title
    return skill_id.replace("_", " ").strip() or "Knowledge Skill"


def _chunk_source_text(chunk: RetrievedChunk) -> str:
    """Search both the retrieved excerpt and its optional parent context."""

    chunk_text = str(chunk.text or "").strip()
    parent_context = str(
        (chunk.metadata or {}).get("parent_context") or ""
    ).strip()
    if not parent_context:
        return chunk_text
    comparable_chunk = _comparable_text(chunk_text)
    if (
        comparable_chunk
        and comparable_chunk in _comparable_text(parent_context)
    ):
        return parent_context
    return f"{chunk_text}\n\n{parent_context}".strip()


def validated_references_to_citations(
    chunks: Sequence[RetrievedChunk],
    skills: Sequence[Mapping[str, object]],
    references_by_target: Mapping[
        str,
        tuple[str, Sequence[CitationReference | Mapping[str, object]]],
    ],
) -> list[Citation]:
    """Validate references into exact anchors or trustworthy related sources."""
    sources: dict[str, tuple[Citation, str]] = {}
    for chunk in chunks:
        source_text = _chunk_source_text(chunk)
        sources[chunk.chunk_id] = (
            Citation(
                chunk_id=chunk.chunk_id,
                source_id=chunk.source_id,
                title=chunk.title,
                scope=_normalize_scope(chunk.scope),
                quote=source_text,
                source_type="knowledge_base",
            ),
            source_text,
        )

    for skill in skills:
        core_knowledge = str(skill.get("core_knowledge") or "").strip()
        if not core_knowledge or bool(skill.get("stale")):
            continue
        skill_id = str(skill.get("id") or "").strip()
        version = str(skill.get("version") or "").strip()
        if not skill_id or not version:
            continue
        scope = next(
            (
                normalized
                for raw_scope in skill.get("scopes") or []
                if (
                    normalized := _normalize_scope(str(raw_scope))
                ) in REFERENCEABLE_CITATION_SCOPES
            ),
            None,
        )
        if scope is None:
            continue
        source_ref = f"skill:{skill_id}:{version}"
        sources[source_ref] = (
            Citation(
                chunk_id=source_ref,
                source_id=f"skill:{skill_id}",
                title=_skill_title(core_knowledge, skill_id),
                scope=scope,
                quote=core_knowledge,
                source_type="skill",
            ),
            core_knowledge,
        )

    rejection_counts: dict[str, int] = {}
    total_count = 0
    accepted_count = 0
    recovered_highlight_count = 0
    recovered_source_quote_count = 0
    downgraded_counts: dict[str, int] = {}
    candidates: dict[str, tuple[Citation, CitationAnchor]] = {}
    related_candidates: dict[str, Citation] = {}
    for target, (target_text, raw_references) in references_by_target.items():
        for raw_reference in raw_references:
            total_count += 1
            try:
                reference = CitationReference.model_validate(raw_reference)
            except Exception:
                _count_rejection(rejection_counts, "invalid_shape")
                continue

            source = sources.get(reference.source_ref)
            if source is None:
                _count_rejection(rejection_counts, "unknown_source")
                continue
            citation, source_text = source
            resolved_highlight = _recover_contiguous_span(
                target_text,
                reference.highlight_text,
            )
            resolved_source_quote = _recover_contiguous_span(
                source_text,
                reference.source_quote,
            )
            if resolved_source_quote is None:
                _count_rejection(rejection_counts, "quote_not_in_source")
                continue

            highlight_for_validation = (
                resolved_highlight or reference.highlight_text
            )
            required_scopes = _authority_scopes(highlight_for_validation)
            if not required_scopes:
                required_scopes = _authority_scopes(resolved_source_quote)
            citation_scope = _normalize_scope(citation.scope)
            if required_scopes and citation_scope not in required_scopes:
                _count_rejection(rejection_counts, "authority_scope_conflict")
                continue

            highlight_codes = _business_codes(highlight_for_validation)
            source_codes = _business_codes(resolved_source_quote)
            if highlight_codes and source_codes - highlight_codes:
                _count_rejection(rejection_counts, "conflicting_business_code")
                continue

            accepted_count += 1
            if (
                resolved_highlight is not None
                and resolved_highlight != reference.highlight_text
            ):
                recovered_highlight_count += 1
            if resolved_source_quote != reference.source_quote:
                recovered_source_quote_count += 1

            downgrade_reason = ""
            if resolved_highlight is None:
                downgrade_reason = "highlight_not_in_output"
            elif not _meaningful_highlight(resolved_highlight):
                downgrade_reason = "generic_or_short_highlight"
            elif not _precise_source_quote(resolved_source_quote):
                downgrade_reason = "overbroad_source_quote"

            if downgrade_reason:
                _count_rejection(downgraded_counts, downgrade_reason)
                source_type = citation.source_type or "knowledge_base"
                source_identity = citation.chunk_id or (
                    f"{citation.source_id}:{citation.title}:{citation.scope}"
                )
                related_key = f"{source_type}:{source_identity}"
                current_related = related_candidates.get(related_key)
                if current_related is None:
                    related_candidates[related_key] = citation.model_copy(
                        deep=True,
                        update={"targets": [target], "anchors": []},
                    )
                else:
                    related_candidates[related_key] = current_related.model_copy(
                        update={
                            "targets": list(
                                dict.fromkeys([*current_related.targets, target])
                            )
                        }
                    )
                continue

            anchor = CitationAnchor(
                target=target,
                highlight_text=resolved_highlight,
                source_quote=resolved_source_quote,
                source_context=_source_context(
                    source_text,
                    resolved_source_quote,
                ),
            )
            identity = (
                f"{target}:"
                f"{_normalized_whitespace(resolved_highlight)}:"
                f"{_normalized_whitespace(resolved_source_quote)}"
            )
            current = candidates.get(identity)
            if current is None or _source_priority(citation) < _source_priority(
                current[0]
            ):
                candidates[identity] = (citation, anchor)

    grouped: dict[str, Citation] = {
        key: citation.model_copy(deep=True)
        for key, citation in related_candidates.items()
    }
    for citation, anchor in candidates.values():
        source_type = citation.source_type or "knowledge_base"
        identity = citation.chunk_id or (
            f"{citation.source_id}:{citation.title}:{citation.scope}"
        )
        key = f"{source_type}:{identity}"
        current = grouped.get(key)
        if current is None:
            grouped[key] = citation.model_copy(
                deep=True,
                update={"targets": [anchor.target], "anchors": [anchor]},
            )
            continue
        anchors = _merge_anchors([*current.anchors, anchor])
        grouped[key] = current.model_copy(
            update={
                "targets": list(
                    dict.fromkeys([*current.targets, anchor.target])
                ),
                "anchors": anchors,
            }
        )

    log_metric(
        "citation.validation",
        citation_policy=CITATION_POLICY_VERSION,
        citation_reference_count=total_count,
        citation_valid_count=accepted_count,
        citation_unique_anchor_count=len(candidates),
        citation_invalid_count=max(0, total_count - accepted_count),
        citation_valid_rate=(
            round(accepted_count / total_count, 4) if total_count else 1.0
        ),
        citation_generic_rejected=rejection_counts.get(
            "generic_or_short_highlight", 0
        ),
        citation_scope_conflicts=rejection_counts.get(
            "authority_scope_conflict", 0
        ),
        citation_quote_missing=rejection_counts.get("quote_not_in_source", 0),
        citation_output_missing=rejection_counts.get(
            "highlight_not_in_output", 0
        ),
        citation_unknown_source=rejection_counts.get("unknown_source", 0),
        citation_business_code_conflicts=rejection_counts.get(
            "conflicting_business_code", 0
        ),
        citation_source_quote_overbroad=rejection_counts.get(
            "overbroad_source_quote", 0
        ),
        citation_highlight_recovered=recovered_highlight_count,
        citation_source_quote_recovered=recovered_source_quote_count,
        citation_anchor_downgraded=sum(downgraded_counts.values()),
        citation_anchor_output_missing=downgraded_counts.get(
            "highlight_not_in_output", 0
        ),
        citation_anchor_generic=downgraded_counts.get(
            "generic_or_short_highlight", 0
        ),
        citation_anchor_source_quote_overbroad=downgraded_counts.get(
            "overbroad_source_quote", 0
        ),
    )
    return list(grouped.values())


def _count_rejection(counts: dict[str, int], reason: str) -> None:
    counts[reason] = counts.get(reason, 0) + 1


def _normalized_whitespace(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", value).casefold(),
    ).strip()


def _matchable_text_with_offsets(value: str) -> tuple[str, list[int]]:
    """Normalize harmless formatting while retaining offsets into ``value``."""

    normalized: list[str] = []
    source_offsets: list[int] = []
    for source_index, source_character in enumerate(value):
        comparable = unicodedata.normalize("NFKC", source_character).casefold()
        for character in comparable:
            if not (character.isalnum() or character in "+#"):
                continue
            normalized.append(character)
            source_offsets.append(source_index)
    return "".join(normalized), source_offsets


def _recover_contiguous_span(container: str, candidate: str) -> str | None:
    """Return the exact source span after formatting-insensitive matching.

    The match may ignore case, width, whitespace and punctuation, but it never
    permits word substitutions, omissions or reordering. Returning text from
    ``container`` keeps UI highlights and source quotes exactly traceable.
    """

    if candidate in container:
        return candidate
    normalized_container, offsets = _matchable_text_with_offsets(container)
    normalized_candidate, _ = _matchable_text_with_offsets(candidate)
    if not normalized_candidate:
        return None
    match_index = normalized_container.find(normalized_candidate)
    if match_index < 0:
        return None
    end_index = match_index + len(normalized_candidate) - 1
    if end_index >= len(offsets):
        return None
    return container[offsets[match_index] : offsets[end_index] + 1]


def _meaningful_highlight(value: str) -> bool:
    normalized = _normalized_whitespace(value)
    comparable = _comparable_text(value)
    if not comparable or comparable in {
        _comparable_text(item) for item in _GENERIC_HIGHLIGHTS
    }:
        return False
    non_empty_lines = [line for line in value.splitlines() if line.strip()]
    if (
        len(normalized) > _MAX_HIGHLIGHT_CHARS
        or len(non_empty_lines) > _MAX_HIGHLIGHT_LINES
    ):
        return False
    sentence_breaks = list(re.finditer(r"[。！？.!?；;]", normalized))
    if len(sentence_breaks) > _MAX_HIGHLIGHT_SENTENCE_BREAKS:
        return False
    if (
        len(re.findall(r"[,，、]", normalized))
        > _MAX_HIGHLIGHT_LIST_SEPARATORS
    ):
        return False
    if _BUSINESS_CODE_PATTERN.fullmatch(normalized):
        return True

    chinese_characters = re.findall(r"[\u3400-\u9fff]", normalized)
    if chinese_characters:
        return len(chinese_characters) >= 3
    english_words = re.findall(r"[a-z][a-z0-9+#.-]*", normalized)
    return len(english_words) >= 2


def _precise_source_quote(value: str) -> bool:
    """Keep hover evidence to one compact, contiguous source excerpt."""
    normalized = _normalized_whitespace(value)
    if not normalized or len(normalized) > _MAX_SOURCE_QUOTE_CHARS:
        return False
    non_empty_lines = [line for line in value.splitlines() if line.strip()]
    return len(non_empty_lines) <= _MAX_SOURCE_QUOTE_LINES


def _authority_scopes(value: str) -> set[str]:
    normalized = _normalized_whitespace(value)
    scopes = {
        scope
        for scope, terms in _AUTHORITY_TERMS
        if any(term in normalized for term in terms)
    }
    if re.search(
        r"(?i)(?<![a-z0-9])(?:g\d+|sl\d+(?:g\d+)?)(?![a-z0-9])",
        normalized,
    ) or any(
        term in normalized
        for term in ("岗位等级标准", "岗级标准", "job level")
    ):
        scopes.add("job_level")
    return scopes


def _business_codes(value: str) -> set[str]:
    return {
        match.group(0).upper()
        for match in _BUSINESS_CODE_SEARCH_PATTERN.finditer(
            unicodedata.normalize("NFKC", value)
        )
    }


def _source_priority(citation: Citation) -> int:
    return 1 if citation.source_type == "skill" else 0


def _merge_anchors(anchors: Sequence[CitationAnchor]) -> list[CitationAnchor]:
    merged: list[CitationAnchor] = []
    seen: set[tuple[str, str, str]] = set()
    for anchor in anchors:
        identity = (
            anchor.target,
            _normalized_whitespace(anchor.highlight_text),
            _normalized_whitespace(anchor.source_quote),
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(anchor)
    return merged


def _source_context(source_text: str, source_quote: str) -> str:
    lines = source_text.splitlines()
    normalized_quote = _normalized_whitespace(source_quote)
    match_index = next(
        (
            index
            for index, line in enumerate(lines)
            if normalized_quote
            and normalized_quote in _normalized_whitespace(line)
        ),
        -1,
    )
    if match_index < 0:
        quote_lines = [
            _normalized_whitespace(line)
            for line in source_quote.splitlines()
            if _normalized_whitespace(line)
        ]
        match_index = next(
            (
                index
                for index, line in enumerate(lines)
                if any(
                    quote_line in _normalized_whitespace(line)
                    or _normalized_whitespace(line) in quote_line
                    for quote_line in quote_lines
                    if len(quote_line) >= 4
                )
            ),
            -1,
        )
    if match_index < 0:
        return _limit_context(source_quote, source_quote)

    heading_index = -1
    for index in range(match_index, -1, -1):
        if _is_source_heading(lines[index]):
            heading_index = index
            break

    section_end = len(lines)
    for index in range(match_index + 1, len(lines)):
        if _is_source_heading(lines[index]):
            section_end = index
            break

    selected_indexes = [match_index]
    quote_codes = _business_codes(source_quote)
    for index in range(match_index + 1, section_end):
        if not lines[index].strip():
            continue
        if quote_codes and _business_codes(lines[index]) - quote_codes:
            break
        selected_indexes.append(index)
        break
    if len(selected_indexes) == 1:
        lower_bound = heading_index + 1 if heading_index >= 0 else 0
        for index in range(match_index - 1, lower_bound - 1, -1):
            if not lines[index].strip():
                continue
            if quote_codes and _business_codes(lines[index]) - quote_codes:
                break
            selected_indexes.insert(0, index)
            break
    if heading_index >= 0 and heading_index not in selected_indexes:
        selected_indexes.insert(0, heading_index)

    context = "\n\n".join(
        lines[index].strip()
        for index in selected_indexes
        if lines[index].strip()
    )
    if normalized_quote not in _normalized_whitespace(context):
        context = f"{context}\n\n{source_quote}".strip()
    return _limit_context(context, source_quote)


def _is_source_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if re.match(r"^#{1,6}\s+\S", stripped):
        return True
    if re.match(r"^(?:[-*+]\s+|\d+[.)、]\s*)", stripped):
        return False
    return len(stripped) <= 80 and stripped.endswith((":", "："))


def _limit_context(context: str, source_quote: str) -> str:
    if len(context) <= _MAX_SOURCE_CONTEXT_CHARS:
        return context
    quote_index = context.find(source_quote)
    if quote_index < 0:
        quote_index = 0
    budget = _MAX_SOURCE_CONTEXT_CHARS - 2
    start = max(0, quote_index - budget // 3)
    end = min(len(context), start + budget)
    start = max(0, end - budget)
    return (
        ("…" if start else "")
        + context[start:end].strip()
        + ("…" if end < len(context) else "")
    )

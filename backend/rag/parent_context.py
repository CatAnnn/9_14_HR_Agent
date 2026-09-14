from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


DEFAULT_PARENT_CONTEXT_MAX_CHARS = 12_000
DEFAULT_PARENT_CONTEXT_BOUNDARY_SCAN_CHARS = 800
DEFAULT_GENERATION_FOCUS_MIN_SAVING_CHARS = 240
DEFAULT_GENERATION_FOCUS_MAX_PARENT_RATIO = 0.85
DEFAULT_GENERATION_FOCUS_MAX_RERANKED_UNITS = 3

_JOB_LEVEL_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?:SL\d+(?:G\d+)?|G\d+|P\d+|M\d+)(?![A-Z0-9])",
    re.IGNORECASE,
)
_MARKDOWN_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_STRUCTURED_GENERATION_LINE = re.compile(
    r"(?m)^\s*(?:"
    r"#{1,6}\s+|"
    r">\s+|"
    r"[-*+]\s+\S|"
    r"(?:\d+|[一二三四五六七八九十]+)[.)、．]\s*\S|"
    r"[·•]\s*(?:&nbsp;\s*)*\S"
    r")"
)
_MARKDOWN_TABLE_ROW = re.compile(r"(?m)^\s*\|[^\n]*\|\s*$")
_HTML_STRUCTURE_TAG = re.compile(
    r"(?i)<\s*(?:table|thead|tbody|tr|th|td|ul|ol|li)\b"
)
_JOB_LEVEL_HEADER_NAMES = {
    "careerlevel",
    "grade",
    "jobgrade",
    "joblevel",
    "level",
    "岗位等级",
    "岗位职级",
    "职等",
    "职级",
}


@dataclass(frozen=True, slots=True)
class ParentContextSlice:
    text: str
    start_char: int | None
    end_char: int | None
    original_chars: int
    split: bool
    anchor_found: bool


def generation_context_text(
    text: str,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Use the bounded parent window for generation when retrieval supplied one."""
    normalized_metadata = metadata or {}
    focused_override = str(
        normalized_metadata.get("generation_context_override") or ""
    ).strip()
    if focused_override:
        return focused_override
    parent_context = str(normalized_metadata.get("parent_context") or "").strip()
    return parent_context or str(text or "").strip()


def generation_context_deduplication_key(
    *,
    scope: str,
    source_id: str,
    title: str,
    text: str,
) -> tuple[str, str, str, str] | None:
    """Return an exact semantic identity for final generation payload text."""

    def normalized_visible_value(value: str) -> str:
        return (
            unicodedata.normalize("NFKC", str(value or ""))
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )

    normalized_text = normalized_visible_value(text)
    exact_source_id = str(source_id or "").strip()
    if not exact_source_id or not normalized_text:
        return None
    return (
        normalized_visible_value(scope),
        exact_source_id,
        normalized_visible_value(title),
        normalized_text,
    )


def select_generation_context_focus(
    parent_text: str,
    metadata: Mapping[str, Any] | None = None,
    *,
    min_saving_chars: int = DEFAULT_GENERATION_FOCUS_MIN_SAVING_CHARS,
    max_parent_ratio: float = DEFAULT_GENERATION_FOCUS_MAX_PARENT_RATIO,
) -> str:
    """Return the smallest safe paragraph envelope around reranked units.

    The full parent remains available for citation validation.  Generation is
    focused only for ordinary prose: structured tables/lists are deliberately
    kept intact because a single leaf can lose headers, sibling requirements,
    or a complete evidence set such as Career Elements and WHAT/HOW.
    """

    parent = str(parent_text or "").strip()
    normalized_metadata = dict(metadata or {})
    if not parent or bool(normalized_metadata.get("atomic_structure")):
        return ""

    raw_units = normalized_metadata.get("retrieval_units")
    if raw_units is None:
        full_units: list[Mapping[str, Any]] = []
    elif not isinstance(raw_units, (list, tuple)):
        return ""
    elif any(not isinstance(unit, Mapping) for unit in raw_units):
        # Filtering malformed entries would change the reranked positions and
        # could promote a lower-ranked unit into the generation focus.
        return ""
    else:
        full_units = list(raw_units)
    if not full_units:
        raw_best = normalized_metadata.get("retrieval_unit")
        if not isinstance(raw_best, Mapping) or not raw_best:
            return ""
        full_units = [raw_best]
    # Inspect every candidate before narrowing it.  Structured or compound
    # evidence anywhere in the reranked set makes the complete parent safer.
    if any(_unsafe_generation_unit(unit) for unit in full_units):
        return ""

    rerank_metadata = normalized_metadata.get("retrieval_unit_rerank")
    if len(full_units) > DEFAULT_GENERATION_FOCUS_MAX_RERANKED_UNITS:
        if not _trusted_truncated_rerank_order(
            full_units,
            rerank_metadata,
        ):
            return ""
    elif isinstance(rerank_metadata, Mapping):
        best_unit_id = str(rerank_metadata.get("best_unit_id") or "").strip()
        first_unit_id = str(full_units[0].get("unit_id") or "").strip()
        if best_unit_id and best_unit_id != first_unit_id:
            return ""

    units = full_units[:DEFAULT_GENERATION_FOCUS_MAX_RERANKED_UNITS]
    spans: list[tuple[int, int]] = []
    for unit in units:
        unit_type = str(unit.get("unit_type") or "").strip().lower()
        if unit_type in {"", "parent", "table", "list", "ordered_sections"}:
            return ""
        unit_text = str(unit.get("text") or "").strip()
        if not unit_text:
            return ""
        span = _unique_text_span(parent, unit_text)
        if span is None:
            return ""
        spans.append(span)

    if not spans:
        return ""

    start_char = min(span[0] for span in spans)
    end_char = max(span[1] for span in spans)
    start_char = _paragraph_start(parent, start_char)
    end_char = _paragraph_end(parent, end_char)
    start_char = _nearby_heading_start(parent, start_char)
    focused = parent[start_char:end_char].strip()
    if not focused:
        return ""

    minimum_saving = max(0, int(min_saving_chars or 0))
    try:
        maximum_ratio = min(1.0, max(0.0, float(max_parent_ratio)))
    except (TypeError, ValueError):
        maximum_ratio = DEFAULT_GENERATION_FOCUS_MAX_PARENT_RATIO
    if len(parent) - len(focused) < minimum_saving:
        return ""
    if len(focused) > len(parent) * maximum_ratio:
        return ""
    return focused


def select_job_level_generation_context(
    parent_text: str,
    allowed_levels: Iterable[str] | None,
) -> str:
    """Project job-level Markdown tables to the applicable level rows.

    Retrieval keeps the complete parent section for citation validation.  This
    helper builds a separate generation-only view so a wide job-level table
    cannot expose requirements for levels that the current intent disallows.
    An empty string means that no safe projection could be made and callers
    should not install an override.

    Only the designated level column is inspected.  A row for ``G9`` may, for
    example, mention ``SL1`` in its requirement text without being mistaken
    for an ``SL1`` row.
    """

    parent = str(parent_text or "").strip()
    allowed = {
        level
        for value in (allowed_levels or ())
        if (level := _canonical_job_level(value)) is not None
    }
    if not parent or not allowed:
        return ""

    lines = parent.splitlines()
    projected_tables: list[str] = []
    index = 0
    while index + 1 < len(lines):
        header_cells = _markdown_table_cells(lines[index])
        separator_cells = _markdown_table_cells(lines[index + 1])
        if not _is_markdown_table_header(header_cells, separator_cells):
            index += 1
            continue

        row_index = index + 2
        rows: list[tuple[str, list[str]]] = []
        while row_index < len(lines):
            cells = _markdown_table_cells(lines[row_index])
            if not cells:
                break
            rows.append((lines[row_index], cells))
            row_index += 1

        level_column = _job_level_column_index(header_cells)
        if level_column is not None:
            selected_rows = [
                line
                for line, cells in rows
                if level_column < len(cells)
                and _job_levels_in_cell(cells[level_column]) & allowed
            ]
            if selected_rows:
                projected_tables.append(
                    "\n".join(
                        [lines[index], lines[index + 1], *selected_rows]
                    ).strip()
                )

        index = max(row_index, index + 1)

    return "\n\n".join(projected_tables)


def select_parent_context(
    text: str,
    *,
    child_text: str = "",
    child_source_start_char: Any = None,
    child_source_end_char: Any = None,
    parent_source_start_char: Any = None,
    max_chars: int = DEFAULT_PARENT_CONTEXT_MAX_CHARS,
    boundary_scan_chars: int = DEFAULT_PARENT_CONTEXT_BOUNDARY_SCAN_CHARS,
) -> ParentContextSlice:
    """Select a bounded source window containing the retrieved child chunk."""

    parent_text = str(text or "")
    original_chars = len(parent_text)
    max_chars = max(1, int(max_chars or DEFAULT_PARENT_CONTEXT_MAX_CHARS))
    boundary_scan_chars = max(0, int(boundary_scan_chars or 0))
    if original_chars <= max_chars:
        cleaned, start_char, end_char = _strip_with_offsets(parent_text, 0)
        return ParentContextSlice(
            text=cleaned,
            start_char=start_char,
            end_char=end_char,
            original_chars=original_chars,
            split=False,
            anchor_found=True,
        )

    anchor = _anchor_span(
        parent_text,
        child_text=child_text,
        child_source_start_char=child_source_start_char,
        child_source_end_char=child_source_end_char,
        parent_source_start_char=parent_source_start_char,
        max_chars=max_chars,
    )
    if anchor is None:
        fallback = str(child_text or "").strip()
        if len(fallback) > max_chars:
            fallback = fallback[:max_chars].rstrip()
        if not fallback:
            fallback = parent_text[:max_chars].strip()
        return ParentContextSlice(
            text=fallback,
            start_char=None,
            end_char=None,
            original_chars=original_chars,
            split=True,
            anchor_found=False,
        )

    anchor_start, anchor_end = anchor
    raw_start, raw_end = _centered_window(
        total_chars=original_chars,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
        max_chars=max_chars,
    )
    start_char = _move_start_to_boundary(
        parent_text,
        raw_start,
        anchor_start,
        boundary_scan_chars,
    )
    end_char = _move_end_to_boundary(
        parent_text,
        raw_end,
        anchor_end,
        boundary_scan_chars,
    )
    if start_char > anchor_start or end_char < anchor_end:
        start_char, end_char = raw_start, raw_end
    selected, selected_start, selected_end = _strip_with_offsets(
        parent_text[start_char:end_char],
        start_char,
    )
    return ParentContextSlice(
        text=selected,
        start_char=selected_start,
        end_char=selected_end,
        original_chars=original_chars,
        split=selected_start > 0 or selected_end < original_chars,
        anchor_found=True,
    )


def _anchor_span(
    parent_text: str,
    *,
    child_text: str,
    child_source_start_char: Any,
    child_source_end_char: Any,
    parent_source_start_char: Any,
    max_chars: int,
) -> tuple[int, int] | None:
    child_start = _optional_int(child_source_start_char)
    child_end = _optional_int(child_source_end_char)
    parent_start = _optional_int(parent_source_start_char)
    if child_start is not None and child_end is not None and parent_start is not None:
        local_start = child_start - parent_start
        local_end = child_end - parent_start
        if (
            0 <= local_start < local_end <= len(parent_text)
            and local_end - local_start <= max_chars
        ):
            return local_start, local_end

    child = str(child_text or "").strip()
    if child:
        position = parent_text.find(child)
        if position >= 0:
            return position, position + len(child)

    candidates = sorted(
        {
            line.strip()
            for line in child.splitlines()
            if len(line.strip()) >= 12
        },
        key=len,
        reverse=True,
    )
    for candidate in candidates:
        position = parent_text.find(candidate)
        if position >= 0:
            return position, position + len(candidate)
    return None


def _centered_window(
    *,
    total_chars: int,
    anchor_start: int,
    anchor_end: int,
    max_chars: int,
) -> tuple[int, int]:
    anchor_size = max(1, anchor_end - anchor_start)
    context_budget = max(0, max_chars - anchor_size)
    start_char = max(0, anchor_start - context_budget // 2)
    end_char = min(total_chars, start_char + max_chars)
    start_char = max(0, end_char - max_chars)
    if end_char < anchor_end:
        end_char = min(total_chars, anchor_end)
        start_char = max(0, end_char - max_chars)
    return start_char, end_char


def _move_start_to_boundary(
    text: str,
    start_char: int,
    anchor_start: int,
    scan_chars: int,
) -> int:
    if start_char <= 0 or scan_chars <= 0:
        return start_char
    search_end = min(anchor_start, start_char + scan_chars)
    for separator in ("\n\n", "\n", "。", "！", "？", ". "):
        position = text.find(separator, start_char, search_end)
        if position >= 0:
            return position + len(separator)
    return start_char


def _move_end_to_boundary(
    text: str,
    end_char: int,
    anchor_end: int,
    scan_chars: int,
) -> int:
    if end_char >= len(text) or scan_chars <= 0:
        return end_char
    search_start = max(anchor_end, end_char - scan_chars)
    for separator in ("\n\n", "\n", "。", "！", "？", ". "):
        position = text.rfind(separator, search_start, end_char)
        if position >= 0:
            return position + len(separator)
    return end_char


def _strip_with_offsets(text: str, base_start: int) -> tuple[str, int, int]:
    left_trimmed = text.lstrip()
    leading = len(text) - len(left_trimmed)
    cleaned = left_trimmed.rstrip()
    start_char = base_start + leading
    return cleaned, start_char, start_char + len(cleaned)


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unsafe_generation_unit(unit: Mapping[str, Any]) -> bool:
    """Return whether a unit cannot safely be detached from its parent."""
    if str(unit.get("unit_type") or "").strip().lower() != "paragraph":
        return True
    text = str(unit.get("text") or "")
    return bool(
        _STRUCTURED_GENERATION_LINE.search(text)
        or _MARKDOWN_TABLE_ROW.search(text)
        or _HTML_STRUCTURE_TAG.search(text)
    )


def _trusted_truncated_rerank_order(
    units: list[Mapping[str, Any]],
    rerank_metadata: Any,
) -> bool:
    """Validate the ordering contract before dropping lower-ranked units."""
    if not isinstance(rerank_metadata, Mapping):
        return False

    unit_ids = [str(unit.get("unit_id") or "").strip() for unit in units]
    if not all(unit_ids) or len(set(unit_ids)) != len(unit_ids):
        return False
    if str(rerank_metadata.get("best_unit_id") or "").strip() != unit_ids[0]:
        return False

    scores: list[float] = []
    for unit in units:
        raw_score = unit.get("raw_rerank_score")
        if raw_score is None or isinstance(raw_score, bool):
            return False
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(score):
            return False
        scores.append(score)

    metadata_score = rerank_metadata.get("raw_rerank_score")
    if metadata_score is None or isinstance(metadata_score, bool):
        return False
    try:
        normalized_metadata_score = float(metadata_score)
    except (TypeError, ValueError):
        return False
    if (
        not math.isfinite(normalized_metadata_score)
        or normalized_metadata_score != scores[0]
    ):
        return False

    if any(left < right for left, right in zip(scores, scores[1:])):
        return False

    cutoff = DEFAULT_GENERATION_FOCUS_MAX_RERANKED_UNITS
    if scores[cutoff - 1] == scores[cutoff]:
        # The metadata does not carry a trustworthy tie-break contract.  Keep
        # the complete parent instead of arbitrarily dropping a tied unit.
        return False
    return True


def _unique_text_span(text: str, needle: str) -> tuple[int, int] | None:
    position = text.find(needle)
    if position < 0:
        return None
    # Ambiguous repeated fragments could focus the wrong paragraph.  Falling
    # back to the complete parent is safer than guessing.
    if text.find(needle, position + 1) >= 0:
        return None
    return position, position + len(needle)


def _paragraph_start(text: str, position: int) -> int:
    boundary = text.rfind("\n\n", 0, max(0, position))
    return 0 if boundary < 0 else boundary + 2


def _paragraph_end(text: str, position: int) -> int:
    boundary = text.find("\n\n", min(len(text), position))
    return len(text) if boundary < 0 else boundary


def _nearby_heading_start(text: str, paragraph_start: int) -> int:
    """Include a nearby Markdown heading without pulling in distant prose."""

    search_start = max(0, paragraph_start - 400)
    prefix = text[search_start:paragraph_start]
    lines = prefix.splitlines(keepends=True)
    offset = search_start
    heading_start: int | None = None
    for line in lines:
        if line.lstrip().startswith("#"):
            stripped = line.lstrip()
            marker = len(stripped) - len(stripped.lstrip("#"))
            if marker and stripped[marker : marker + 1].isspace():
                heading_start = offset + (len(line) - len(line.lstrip()))
        offset += len(line)
    return heading_start if heading_start is not None else paragraph_start


def _canonical_job_level(value: Any) -> str | None:
    normalized = str(value or "").strip().upper()
    if _JOB_LEVEL_PATTERN.fullmatch(normalized):
        return normalized
    return None


def _markdown_table_cells(line: str) -> list[str]:
    """Split a Markdown table row while respecting escaped pipe characters."""

    raw = str(line or "").strip()
    if "|" not in raw:
        return []

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in raw:
        if character == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        if character == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    cells.append("".join(current).strip())

    if raw.startswith("|") and cells and not cells[0]:
        cells.pop(0)
    if raw.endswith("|") and cells and not cells[-1]:
        cells.pop()
    return cells


def _is_markdown_table_header(
    header_cells: list[str],
    separator_cells: list[str],
) -> bool:
    if len(header_cells) < 2 or len(separator_cells) != len(header_cells):
        return False
    return all(
        bool(_MARKDOWN_TABLE_SEPARATOR.fullmatch(cell.replace(" ", "")))
        for cell in separator_cells
    )


def _job_level_column_index(header_cells: list[str]) -> int | None:
    matches = [
        index
        for index, cell in enumerate(header_cells)
        if _normalized_table_header(cell) in _JOB_LEVEL_HEADER_NAMES
    ]
    return matches[0] if len(matches) == 1 else None


def _normalized_table_header(value: str) -> str:
    without_html = re.sub(r"<[^>]+>", "", str(value or ""))
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", without_html.lower())


def _job_levels_in_cell(value: str) -> set[str]:
    return {
        match.group(0).upper()
        for match in _JOB_LEVEL_PATTERN.finditer(str(value or ""))
    }

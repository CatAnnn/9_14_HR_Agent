from __future__ import annotations

import re
from typing import Any


DEFAULT_CHUNK_SIZE = 2048
DEFAULT_CHUNK_OVERLAP = 160
# Paragraph is the final natural split boundary. Do not recursively split into
# sentences or words; oversized single paragraphs fall back to hard split.
RECURSIVE_SEPARATORS = ("\n\n",)
ORGANIZATION_CHUNKING_VERSION = "organization-line-sentence-v4"

_ORGANIZATION_LIST_ITEM_PATTERN = re.compile(
    r"^(?:"
    r"[-*\u2022\u2013\u2014]\s+|"
    r"[\u2192\u25cf]\s+|"
    r"\([1-9]\d?\)(?:\s+|(?=[^\W\d_]))|"
    r"\d{1,3}[.)]\s+|"
    r"[A-Za-z][.)]\s+"
    r")"
)
_ORGANIZATION_SENTENCE_END_PATTERN = re.compile(
    r"[\u3002\uff01\uff1f\uff1b]|[.!?;](?=\s|$)"
)


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    chunk_size = max(1, int(chunk_size or DEFAULT_CHUNK_SIZE))
    overlap = max(0, min(int(overlap or 0), chunk_size // 2))
    splits = _recursive_split(text, chunk_size, RECURSIVE_SEPARATORS)
    return _merge_splits(splits, chunk_size=chunk_size, overlap=overlap)


def chunk_organization_text(
    text: str,
    chunk_size: int,
    overlap: int,
) -> list[str]:
    """Chunk one organization page at line/sentence boundaries.

    Overlap is composed only of complete logical units. Character tails are
    never prepended to the next chunk, so normal words and organization codes
    are not split at chunk boundaries.
    """

    normalized = str(text or "").strip()
    if not normalized:
        return []
    chunk_size = max(1, int(chunk_size))
    overlap = max(0, min(int(overlap or 0), chunk_size // 2))

    units: list[str] = []
    for raw_line in normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        units.extend(_split_organization_unit(line, chunk_size))

    if not units:
        return []
    return _pack_organization_units(
        units,
        chunk_size=chunk_size,
        overlap=overlap,
    )


def _split_organization_unit(text: str, chunk_size: int) -> list[str]:
    # A list marker gives all sentences on the line one shared semantic role.
    # Keep the complete item intact whenever it already fits in one chunk;
    # otherwise later sentences would lose the marker and become ambiguous.
    if (
        len(text) <= chunk_size
        and _ORGANIZATION_LIST_ITEM_PATTERN.match(text)
    ):
        return [text]

    sentences = [
        value.strip()
        for value in re.split(
            r"(?<=[。！？；])|(?<=[.!?;])\s+",
            text,
        )
        if value.strip()
    ]
    if len(sentences) > 1:
        output: list[str] = []
        for sentence in sentences:
            if len(sentence) <= chunk_size:
                output.append(sentence)
            else:
                output.extend(
                    _split_at_readable_boundary(sentence, chunk_size)
                )
        return output
    if len(text) <= chunk_size:
        return [text]
    return _split_at_readable_boundary(text, chunk_size)


def _split_at_readable_boundary(text: str, chunk_size: int) -> list[str]:
    output: list[str] = []
    remaining = text.strip()
    minimum_soft_cut = max(1, chunk_size // 2)
    while len(remaining) > chunk_size:
        # Keep one look-ahead character so an English punctuation mark exactly
        # at the limit is not mistaken for a sentence end when a word continues.
        sentence_window = remaining[: chunk_size + 1]
        sentence_cuts = [
            match.end()
            for match in _ORGANIZATION_SENTENCE_END_PATTERN.finditer(
                sentence_window
            )
            if minimum_soft_cut <= match.end() <= chunk_size
        ]
        if sentence_cuts:
            # Prefer a complete sentence even when an ordinary word boundary
            # appears slightly later. ``end`` also keeps CJK punctuation with
            # the sentence it terminates instead of moving it to the next part.
            cut = sentence_cuts[-1]
        else:
            window = remaining[:chunk_size]
            cut = window.rfind(" ")
            if cut < minimum_soft_cut:
                cut = chunk_size
        piece = remaining[:cut].strip()
        if not piece:
            cut = chunk_size
            piece = remaining[:cut].strip()
        output.append(piece)
        remaining = remaining[cut:].strip()
    if remaining:
        output.append(remaining)
    return output


def _pack_organization_units(
    units: list[str],
    *,
    chunk_size: int,
    overlap: int,
) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for unit in units:
        candidate = "\n".join([*current, unit])
        if not current or len(candidate) <= chunk_size:
            current.append(unit)
            continue

        chunks.append("\n".join(current))
        retained = _whole_unit_overlap(current, overlap)
        while retained and len("\n".join([*retained, unit])) > chunk_size:
            retained.pop(0)
        current = [*retained, unit]

    if current:
        chunks.append("\n".join(current))
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _whole_unit_overlap(units: list[str], overlap: int) -> list[str]:
    if overlap <= 0:
        return []
    retained: list[str] = []
    for unit in reversed(units):
        candidate = "\n".join([unit, *retained])
        if len(candidate) > overlap:
            break
        retained.insert(0, unit)
    return retained


def chunk_blocks(
    text: str,
    blocks: list[dict[str, Any]] | None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[dict[str, Any]]:
    """Create chunks while preserving complete Markdown structures."""

    chunk_size = max(1, int(chunk_size or DEFAULT_CHUNK_SIZE))
    overlap = max(0, min(int(overlap or 0), chunk_size // 2))
    output: list[dict[str, Any]] = []
    usable_blocks = [
        block
        for block in (blocks or [])
        if str(block.get("text") or "").strip()
    ]
    if not usable_blocks:
        for idx, chunk in enumerate(
            chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        ):
            output.append(
                {
                    "text": chunk,
                    "page": None,
                    "section": None,
                    "block_indexes": [],
                    "chunk_index": idx,
                }
            )
        return output

    buffer: list[tuple[int, dict[str, Any]]] = []

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        _append_block_chunk(
            output,
            chunk="\n".join(
                str(block.get("text") or "").strip()
                for _, block in buffer
            ),
            entries=buffer,
        )
        buffer = []

    for semantic_key, entries in _semantic_units(usable_blocks):
        if semantic_key is not None:
            flush()
            _append_semantic_chunks(
                output,
                entries=entries,
                semantic_key=semantic_key,
                chunk_size=chunk_size,
            )
            continue

        idx, block = entries[0]
        block_text = str(block.get("text") or "").strip()
        block_parent_id = block.get("parent_section_local_id")
        if (
            buffer
            and block_parent_id
            != buffer[0][1].get("parent_section_local_id")
        ):
            flush()

        if len(block_text) > chunk_size:
            flush()
            for piece in chunk_text(
                block_text,
                chunk_size=chunk_size,
                overlap=overlap,
            ):
                _append_block_chunk(
                    output,
                    chunk=piece,
                    entries=[(idx, block)],
                )
            continue

        pending_text = "\n".join(
            [
                *(
                    str(item.get("text") or "").strip()
                    for _, item in buffer
                ),
                block_text,
            ]
        )
        if buffer and len(pending_text) > chunk_size:
            flush()
        buffer.append((idx, block))

    flush()

    if not output:
        for idx, chunk in enumerate(
            chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        ):
            output.append(
                {
                    "text": chunk,
                    "page": None,
                    "section": None,
                    "block_indexes": [],
                    "chunk_index": idx,
                }
            )
    return output


def _semantic_units(
    blocks: list[dict[str, Any]],
) -> list[
    tuple[
        tuple[str, str] | None,
        list[tuple[int, dict[str, Any]]],
    ]
]:
    units: list[
        tuple[
            tuple[str, str] | None,
            list[tuple[int, dict[str, Any]]],
        ]
    ] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        key = _semantic_key(block)
        if key is None:
            units.append((None, [(index, block)]))
            index += 1
            continue

        entries = [(index, block)]
        index += 1
        while index < len(blocks) and _semantic_key(blocks[index]) == key:
            entries.append((index, blocks[index]))
            index += 1
        units.append((key, entries))
    return units


def _semantic_key(
    block: dict[str, Any],
) -> tuple[str, str] | None:
    semantic_group_id = str(block.get("semantic_group_id") or "").strip()
    if semantic_group_id:
        return ("ordered_sections", semantic_group_id)

    list_id = str(block.get("list_id") or "").strip()
    if list_id:
        return ("list", list_id)

    table_id = str(block.get("table_id") or "").strip()
    if table_id:
        return ("table", table_id)
    return None


def _append_semantic_chunks(
    output: list[dict[str, Any]],
    *,
    entries: list[tuple[int, dict[str, Any]]],
    semantic_key: tuple[str, str],
    chunk_size: int,
) -> None:
    semantic_type, semantic_group_id = semantic_key
    first_block = entries[0][1]
    if semantic_type == "ordered_sections":
        title = str(first_block.get("semantic_group_title") or "").strip()
        heading_path = [
            str(value)
            for value in (
                first_block.get("semantic_group_heading_path") or []
            )
            if str(value).strip()
        ]
        parent_section_local_id = semantic_group_id
        items = _ordered_section_items(entries)
    else:
        title = str(first_block.get("section") or "").strip()
        heading_path = [
            str(value)
            for value in (first_block.get("heading_path") or [])
            if str(value).strip()
        ]
        parent_section_local_id = first_block.get(
            "parent_section_local_id"
        )
        items = [
            (_render_structure_block(block), [(index, block)])
            for index, block in entries
        ]

    items = [
        (item_text.strip(), item_entries)
        for item_text, item_entries in items
        if item_text.strip()
    ]
    if not items:
        return

    heading = f"## {title}" if title else ""
    body = "\n\n".join(item_text for item_text, _ in items)
    if len(body) <= chunk_size:
        segments = [items]
    else:
        segments: list[
            list[
                tuple[
                    str,
                    list[tuple[int, dict[str, Any]]],
                ]
            ]
        ] = []
        current: list[
            tuple[
                str,
                list[tuple[int, dict[str, Any]]],
            ]
        ] = []
        for item in items:
            candidate = _semantic_chunk_text(
                heading,
                [*current, item],
            )
            if current and len(candidate) > chunk_size:
                segments.append(current)
                current = [item]
            else:
                current.append(item)
        if current:
            segments.append(current)

    semantic_split = len(segments) > 1
    retrieval_unit_offset = 0
    for segment in segments:
        segment_entries = [
            entry
            for _, item_entries in segment
            for entry in item_entries
        ]
        chunk = _semantic_chunk_text(heading, segment)
        retrieval_unit_items: list[
            tuple[
                str,
                list[tuple[int, dict[str, Any]]],
            ]
        ] = []
        for item_text, item_entries in segment:
            if semantic_type == "ordered_sections":
                retrieval_unit_items.extend(
                    _ordered_section_leaf_items(
                        item_text=item_text,
                        item_entries=item_entries,
                        chunk_size=chunk_size,
                    )
                )
            else:
                retrieval_unit_items.append((item_text, item_entries))

        retrieval_units = [
            _retrieval_unit_payload(
                semantic_type=semantic_type,
                semantic_group_id=semantic_group_id,
                semantic_group_title=title,
                semantic_item_index=retrieval_unit_offset + item_index,
                item_text=item_text,
                item_entries=item_entries,
                heading_path=heading_path,
            )
            for item_index, (item_text, item_entries) in enumerate(
                retrieval_unit_items
            )
        ]
        _append_block_chunk(
            output,
            chunk=chunk,
            entries=segment_entries,
            extra={
                "atomic_structure": True,
                "semantic_group_id": semantic_group_id,
                "semantic_group_type": semantic_type,
                "semantic_group_title": title,
                "semantic_group_item_count": len(items),
                "semantic_item_count": len(segment),
                "semantic_split": semantic_split,
                "oversized_atomic": len(chunk) > chunk_size,
                "parent_section_local_id": parent_section_local_id,
                "heading_path": heading_path,
                "section": title,
                "retrieval_units": retrieval_units,
            },
        )
        retrieval_unit_offset += len(retrieval_unit_items)


def _retrieval_unit_payload(
    *,
    semantic_type: str,
    semantic_group_id: str,
    semantic_group_title: str,
    semantic_item_index: int,
    item_text: str,
    item_entries: list[tuple[int, dict[str, Any]]],
    heading_path: list[str],
) -> dict[str, Any]:
    blocks = [block for _, block in item_entries]
    table_headers = list(
        dict.fromkeys(
            str(value)
            for block in blocks
            for value in (block.get("table_headers") or [])
            if str(value).strip()
        )
    )
    semantic_item_id = str(
        blocks[0].get("semantic_item_id") or ""
    ).strip()
    if not semantic_item_id:
        semantic_item_id = (
            f"{semantic_group_id}:{semantic_type}:{semantic_item_index}"
        )
    return {
        "unit_type": semantic_type,
        "unit_index": semantic_item_index,
        "semantic_group_id": semantic_group_id,
        "semantic_group_title": semantic_group_title,
        "semantic_item_id": semantic_item_id,
        "text": str(item_text or "").strip(),
        "heading_path": list(heading_path),
        "table_headers": table_headers,
        "source_start_line": blocks[0].get("source_start_line"),
        "source_end_line": blocks[-1].get("source_end_line"),
        "source_start_char": blocks[0].get("source_start_char"),
        "source_end_char": blocks[-1].get("source_end_char"),
    }


def _ordered_section_items(
    entries: list[tuple[int, dict[str, Any]]],
) -> list[
    tuple[
        str,
        list[tuple[int, dict[str, Any]]],
    ]
]:
    grouped: list[
        tuple[
            str,
            str,
            list[tuple[int, dict[str, Any]]],
        ]
    ] = []
    for index, block in entries:
        item_id = str(block.get("semantic_item_id") or "").strip()
        item_title = str(
            block.get("semantic_item_title") or block.get("section") or ""
        ).strip()
        if grouped and grouped[-1][0] == item_id:
            grouped[-1][2].append((index, block))
        else:
            grouped.append((item_id, item_title, [(index, block)]))

    output: list[
        tuple[
            str,
            list[tuple[int, dict[str, Any]]],
        ]
    ] = []
    for _, item_title, item_entries in grouped:
        rendered_blocks = [
            rendered
            for _, block in item_entries
            if (rendered := _render_structure_block(block))
        ]
        content = "\n\n".join(rendered_blocks)
        item_heading = f"### {item_title}" if item_title else ""
        item_text = "\n\n".join(
            value for value in (item_heading, content) if value
        )
        output.append((item_text, item_entries))
    return output


def _ordered_section_leaf_items(
    *,
    item_text: str,
    item_entries: list[tuple[int, dict[str, Any]]],
    chunk_size: int,
) -> list[
    tuple[
        str,
        list[tuple[int, dict[str, Any]]],
    ]
]:
    """Split only oversized retrieval units at parsed natural boundaries."""

    if len(item_text) <= chunk_size:
        return [(item_text, item_entries)]

    first_block = item_entries[0][1]
    item_title = str(
        first_block.get("semantic_item_title")
        or first_block.get("section")
        or ""
    ).strip()
    item_heading = f"### {item_title}" if item_title else ""
    leaves: list[
        tuple[
            str,
            list[tuple[int, dict[str, Any]]],
        ]
    ] = []
    for index, block in item_entries:
        rendered = _render_structure_block(block)
        if not rendered:
            continue
        leaf_text = "\n\n".join(
            value
            for value in (item_heading, rendered)
            if value
        )
        leaves.append((leaf_text, [(index, block)]))

    # A single parsed block has no natural boundary. Keep it whole rather than
    # introducing a character-, sentence-, or word-level split.
    return leaves if len(leaves) > 1 else [(item_text, item_entries)]


def _render_structure_block(block: dict[str, Any]) -> str:
    if str(block.get("type") or "") == "list_item":
        return str(
            block.get("source_text") or block.get("text") or ""
        ).strip()
    return str(block.get("text") or "").strip()


def _semantic_chunk_text(
    heading: str,
    items: list[
        tuple[
            str,
            list[tuple[int, dict[str, Any]]],
        ]
    ],
) -> str:
    body = "\n\n".join(item_text for item_text, _ in items)
    return "\n\n".join(
        value for value in (heading.strip(), body.strip()) if value
    )


def _ordinary_retrieval_units(
    *,
    parent_text: str,
    entries: list[tuple[int, dict[str, Any]]],
    heading_path: list[str],
    section: str | None,
    parent_section_local_id: Any,
) -> list[dict[str, Any]]:
    expected_parent_text = "\n".join(
        str(block.get("text") or "").strip()
        for _, block in entries
    ).strip()
    if parent_text != expected_parent_text:
        return []

    paragraph_count = sum(
        str(block.get("type") or "") == "paragraph"
        for _, block in entries
    )
    if not paragraph_count:
        return []

    if paragraph_count <= 1:
        return []

    items = [
        (
            str(block.get("text") or "").strip(),
            [(index, block)],
        )
        for index, block in entries
        if str(block.get("type") or "") == "paragraph"
        and str(block.get("text") or "").strip()
    ]

    group_id = str(parent_section_local_id or "")
    group_title = str(section or "")
    return [
        _retrieval_unit_payload(
            semantic_type="paragraph",
            semantic_group_id=group_id,
            semantic_group_title=group_title,
            semantic_item_index=item_index,
            item_text=item_text,
            item_entries=item_entries,
            heading_path=heading_path,
        )
        for item_index, (item_text, item_entries) in enumerate(items)
    ]



def _append_block_chunk(
    output: list[dict[str, Any]],
    *,
    chunk: str,
    entries: list[tuple[int, dict[str, Any]]],
    extra: dict[str, Any] | None = None,
) -> None:
    cleaned = chunk.strip()
    if not cleaned or not entries:
        return

    blocks = [block for _, block in entries]
    first_block = blocks[0]
    metadata = dict(extra or {})
    heading_path = metadata.pop(
        "heading_path",
        [
            str(value)
            for value in (first_block.get("heading_path") or [])
            if str(value).strip()
        ],
    )
    section = metadata.pop(
        "section",
        next(
            (
                str(block.get("section"))
                for block in reversed(blocks)
                if block.get("section")
            ),
            None,
        ),
    )
    parent_section_local_id = metadata.pop(
        "parent_section_local_id",
        first_block.get("parent_section_local_id"),
    )
    block_indexes = list(
        dict.fromkeys(index for index, _ in entries)
    )
    block_types = list(
        dict.fromkeys(
            str(block.get("type") or "unknown") for block in blocks
        )
    )
    table_headers = list(
        dict.fromkeys(
            str(value)
            for block in blocks
            for value in (block.get("table_headers") or [])
            if str(value).strip()
        )
    )
    if "retrieval_units" not in metadata:
        retrieval_units = _ordinary_retrieval_units(
            parent_text=cleaned,
            entries=entries,
            heading_path=list(heading_path),
            section=section,
            parent_section_local_id=parent_section_local_id,
        )
        if retrieval_units:
            metadata["retrieval_units"] = retrieval_units

    output.append(
        {
            "text": cleaned,
            "page": next(
                (
                    block.get("page")
                    for block in blocks
                    if block.get("page") is not None
                ),
                None,
            ),
            "section": section,
            "block_indexes": block_indexes,
            "chunk_index": len(output),
            "parent_section_local_id": parent_section_local_id,
            "chapter_local_section_id": first_block.get(
                "chapter_local_section_id"
            ),
            "heading_path": list(heading_path),
            "block_types": block_types,
            "table_headers": table_headers,
            "source_start_line": first_block.get("source_start_line"),
            "source_end_line": blocks[-1].get("source_end_line"),
            "source_start_char": first_block.get("source_start_char"),
            "source_end_char": blocks[-1].get("source_end_char"),
            **metadata,
        }
    )


def _recursive_split(
    text: str,
    chunk_size: int,
    separators: tuple[str, ...],
) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    if not separators:
        return _hard_split(text, chunk_size)

    separator = separators[0]
    if separator not in text:
        return _recursive_split(text, chunk_size, separators[1:])

    pieces = _split_keep_separator(text, separator)
    output: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) <= chunk_size:
            output.append(piece)
        else:
            output.extend(
                _recursive_split(piece, chunk_size, separators[1:])
            )
    return output or _hard_split(text, chunk_size)


def _split_keep_separator(text: str, separator: str) -> list[str]:
    parts = text.split(separator)
    if len(parts) == 1:
        return [text]
    pieces: list[str] = []
    for index, part in enumerate(parts):
        if not part:
            continue
        suffix = separator if index < len(parts) - 1 else ""
        pieces.append(part + suffix)
    return pieces


def _merge_splits(splits: list[str], chunk_size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for split in splits:
        piece = split.strip()
        if not piece:
            continue
        if not current:
            current = piece
            continue
        candidate = _join_text(current, piece)
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        chunks.extend(_hard_split(current, chunk_size))
        tail = _tail_overlap(current, overlap)
        candidate = _join_text(tail, piece) if tail else piece
        current = candidate if len(candidate) <= chunk_size else piece
    if current:
        chunks.extend(_hard_split(current, chunk_size))
    return [chunk for chunk in chunks if chunk.strip()]


def _join_text(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    return f"{left}\n{right}"


def _tail_overlap(text: str, overlap: int) -> str:
    if overlap <= 0:
        return ""
    return text.strip()[-overlap:].strip()


def _hard_split(text: str, chunk_size: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [
        text[index : index + chunk_size].strip()
        for index in range(0, len(text), chunk_size)
        if text[index : index + chunk_size].strip()
    ]

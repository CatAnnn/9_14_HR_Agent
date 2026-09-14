from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from backend.exceptions.parser_errors import ParserError


_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_LIST_MARKER = re.compile(r"^\s*((?:[-+*]|\d+[.)]))\s+")
_HTML_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_ORDERED_SECTION_HEADING = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百零〇两]+"
    r"(?:件事|个问题|问题|问|项|点|步|部分|方面|条)"
    r"|[一二三四五六七八九十百零〇两]+[、.)）]"
    r"|\d{1,2}[.)、）]"
    r"|[（(]\d{1,2}[）)]"
    r")\s*"
)


class StructuredMarkdownParser:
    """Parse processed Markdown into source-mapped sections and blocks."""

    VERSION = "structured-markdown-v4"

    def __init__(self) -> None:
        self._parser = MarkdownIt(
            "commonmark",
            {"html": False, "linkify": False, "typographer": False},
        ).enable("table")

    def parse(self, path: Path) -> tuple[str, dict[str, Any]]:
        try:
            raw_text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise ParserError(f"Markdown file cannot be read: {path}: {exc}") from exc

        text = (
            raw_text.replace("\r\n", "\n")
            .replace("\r", "\n")
            .lstrip("\ufeff")
            .strip()
        )
        if not text:
            raise ParserError(f"Markdown file is empty: {path}")

        try:
            tokens = self._parser.parse(text)
        except Exception as exc:  # noqa: BLE001
            raise ParserError(f"Markdown parsing failed: {path}: {exc}") from exc

        lines = text.splitlines(keepends=True)
        plain_lines = text.splitlines()
        line_offsets = self._line_offsets(lines)
        headings = self._headings(tokens)
        sections = self._sections(
            headings=headings,
            lines=lines,
            line_offsets=line_offsets,
            fallback_title=path.stem,
        )
        blocks = self._blocks(
            tokens=tokens,
            headings=headings,
            sections=sections,
            lines=plain_lines,
            line_offsets=line_offsets,
        )
        if not blocks:
            root = sections[0]
            blocks = [
                self._block_payload(
                    block_type="document",
                    text=text,
                    source_text=text,
                    start_line=0,
                    end_line=len(plain_lines),
                    line_offsets=line_offsets,
                    section=root,
                )
            ]

        return text, {
            "parser": "structured_markdown",
            "parser_version": self.VERSION,
            "input_path": str(path),
            "structured_blocks": blocks,
            "sections": sections,
            "section_count": len(sections),
            "block_count": len(blocks),
            "images": [],
        }

    @staticmethod
    def _line_offsets(lines: list[str]) -> list[int]:
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        return offsets

    @staticmethod
    def _headings(tokens: list[Any]) -> list[dict[str, Any]]:
        headings: list[dict[str, Any]] = []
        for index, token in enumerate(tokens):
            if token.type != "heading_open" or not token.map:
                continue
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            title = str(getattr(inline, "content", "") or "").strip()
            if not title:
                continue
            level = int(str(token.tag or "h1")[1:])
            headings.append(
                {
                    "title": title,
                    "level": level,
                    "start_line": int(token.map[0]),
                    "heading_end_line": int(token.map[1]),
                }
            )
        return headings

    def _sections(
        self,
        *,
        headings: list[dict[str, Any]],
        lines: list[str],
        line_offsets: list[int],
        fallback_title: str,
    ) -> list[dict[str, Any]]:
        total_lines = len(lines)
        document_id = self._local_id("document", 0, fallback_title)
        document = {
            "local_section_id": document_id,
            "parent_local_section_id": None,
            "chapter_local_section_id": document_id,
            "title": fallback_title,
            "level": 0,
            "heading_path": [fallback_title],
            "source_start_line": 1,
            "source_end_line": total_lines,
            "source_start_char": 0,
            "source_end_char": line_offsets[-1],
            "text": "".join(lines).strip(),
            "synthetic": True,
        }
        sections = [document]
        if not headings:
            return sections

        stack: list[dict[str, Any]] = []
        for index, heading in enumerate(headings):
            level = int(heading["level"])
            while stack and int(stack[-1]["level"]) >= level:
                stack.pop()
            parent = stack[-1] if stack else document
            subtree_end = total_lines
            for following in headings[index + 1 :]:
                if int(following["level"]) <= level:
                    subtree_end = int(following["start_line"])
                    break
            heading_path = [
                *(
                    list(parent["heading_path"])
                    if int(parent["level"]) > 0
                    else []
                ),
                str(heading["title"]),
            ]
            local_id = self._local_id(
                str(heading["title"]),
                int(heading["start_line"]),
                ">".join(heading_path),
            )
            chapter_id = (
                local_id
                if level == 1
                else str(parent.get("chapter_local_section_id") or document_id)
            )
            section = {
                "local_section_id": local_id,
                "parent_local_section_id": parent["local_section_id"],
                "chapter_local_section_id": chapter_id,
                "title": str(heading["title"]),
                "level": level,
                "heading_path": heading_path,
                "source_start_line": int(heading["start_line"]) + 1,
                "source_end_line": subtree_end,
                "source_start_char": line_offsets[int(heading["start_line"])],
                "source_end_char": line_offsets[subtree_end],
                "text": "".join(
                    lines[int(heading["start_line"]) : subtree_end]
                ).strip(),
                "synthetic": False,
            }
            sections.append(section)
            stack.append(section)
        return sections

    def _blocks(
        self,
        *,
        tokens: list[Any],
        headings: list[dict[str, Any]],
        sections: list[dict[str, Any]],
        lines: list[str],
        line_offsets: list[int],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        covered_ranges: list[tuple[int, int]] = []
        heading_ranges = {
            (int(item["start_line"]), int(item["heading_end_line"]))
            for item in headings
        }
        list_containers = self._list_containers(tokens)
        list_item_indexes: dict[str, int] = {}

        for token in tokens:
            if not token.map:
                continue
            start_line, end_line = (int(token.map[0]), int(token.map[1]))
            if (start_line, end_line) in heading_ranges and token.type.startswith(
                "heading"
            ):
                covered_ranges.append((start_line, end_line))
                continue
            if token.type == "table_open" and token.level == 0:
                candidates.extend(
                    self._table_blocks(
                        start_line=start_line,
                        end_line=end_line,
                        lines=lines,
                        line_offsets=line_offsets,
                        section=self._section_for_line(sections, start_line),
                    )
                )
                covered_ranges.append((start_line, end_line))
                continue
            if token.type == "list_item_open" and token.level == 1:
                source_text = "\n".join(lines[start_line:end_line]).strip()
                text = self._clean_list_item(source_text)
                list_metadata: dict[str, Any] = {"list_depth": 1}
                container = next(
                    (
                        item
                        for item in list_containers
                        if int(item["start_line"]) <= start_line
                        and end_line <= int(item["end_line"])
                    ),
                    None,
                )
                if container is not None:
                    list_id = str(container["list_id"])
                    item_index = list_item_indexes.get(list_id, 0)
                    list_item_indexes[list_id] = item_index + 1
                    marker_match = _LIST_MARKER.match(
                        source_text.splitlines()[0]
                    )
                    list_metadata.update(
                        {
                            "list_id": list_id,
                            "list_kind": container["list_kind"],
                            "list_marker": (
                                marker_match.group(1)
                                if marker_match is not None
                                else (
                                    f"{item_index + 1}."
                                    if container["list_kind"] == "ordered"
                                    else "-"
                                )
                            ),
                            "list_item_index": item_index,
                        }
                    )
                candidates.append(
                    self._block_payload(
                        block_type="list_item",
                        text=text,
                        source_text=source_text,
                        start_line=start_line,
                        end_line=end_line,
                        line_offsets=line_offsets,
                        section=self._section_for_line(sections, start_line),
                        extra=list_metadata,
                    )
                )
                covered_ranges.append((start_line, end_line))
                continue
            if token.level != 0:
                continue
            if token.type not in {
                "paragraph_open",
                "blockquote_open",
                "fence",
                "code_block",
                "html_block",
                "hr",
            }:
                continue
            source_text = "\n".join(lines[start_line:end_line]).strip()
            candidates.append(
                self._block_payload(
                    block_type=self._block_type(token.type),
                    text=source_text,
                    source_text=source_text,
                    start_line=start_line,
                    end_line=end_line,
                    line_offsets=line_offsets,
                    section=self._section_for_line(sections, start_line),
                )
            )
            covered_ranges.append((start_line, end_line))

        occupied = self._merge_ranges(covered_ranges)
        gap_start: int | None = None
        for line_index, line in enumerate(lines):
            covered = any(start <= line_index < end for start, end in occupied)
            if not covered and line.strip():
                if gap_start is None:
                    gap_start = line_index
                continue
            if gap_start is not None:
                candidates.append(
                    self._raw_gap_block(
                        lines=lines,
                        start_line=gap_start,
                        end_line=line_index,
                        line_offsets=line_offsets,
                        sections=sections,
                    )
                )
                gap_start = None
        if gap_start is not None:
            candidates.append(
                self._raw_gap_block(
                    lines=lines,
                    start_line=gap_start,
                    end_line=len(lines),
                    line_offsets=line_offsets,
                    sections=sections,
                )
            )

        candidates = [item for item in candidates if str(item["text"]).strip()]
        candidates.sort(
            key=lambda item: (
                int(item["source_start_line"]),
                int(item["source_end_line"]),
                str(item["type"]),
            )
        )
        self._annotate_ordered_section_groups(
            candidates=candidates,
            sections=sections,
        )
        for index, block in enumerate(candidates):
            block["block_index"] = index
        return candidates

    def _list_containers(self, tokens: list[Any]) -> list[dict[str, Any]]:
        containers: list[dict[str, Any]] = []
        for token in tokens:
            if (
                token.type not in {"bullet_list_open", "ordered_list_open"}
                or token.level != 0
                or not token.map
            ):
                continue
            start_line, end_line = (int(token.map[0]), int(token.map[1]))
            containers.append(
                {
                    "list_id": self._local_id(
                        "list",
                        start_line,
                        end_line,
                        token.type,
                    ),
                    "list_kind": (
                        "ordered"
                        if token.type == "ordered_list_open"
                        else "unordered"
                    ),
                    "start_line": start_line,
                    "end_line": end_line,
                }
            )
        return containers

    @staticmethod
    def _annotate_ordered_section_groups(
        *,
        candidates: list[dict[str, Any]],
        sections: list[dict[str, Any]],
    ) -> None:
        sections_by_id = {
            str(section.get("local_section_id") or ""): section
            for section in sections
        }
        parent_by_id = {
            section_id: str(
                section.get("parent_local_section_id") or ""
            )
            for section_id, section in sections_by_id.items()
        }
        ordered_by_parent: dict[str, list[dict[str, Any]]] = {}
        for section in sections:
            title = str(section.get("title") or "").strip()
            parent_id = str(
                section.get("parent_local_section_id") or ""
            )
            parent = sections_by_id.get(parent_id)
            if (
                not title
                or parent is None
                or int(parent.get("level") or 0) <= 0
                or _ORDERED_SECTION_HEADING.match(title) is None
            ):
                continue
            ordered_by_parent.setdefault(parent_id, []).append(section)

        item_metadata: dict[str, dict[str, Any]] = {}
        for parent_id, items in ordered_by_parent.items():
            if len(items) < 2:
                continue
            parent = sections_by_id[parent_id]
            ordered_items = sorted(
                items,
                key=lambda item: int(
                    item.get("source_start_line") or 0
                ),
            )
            for item_index, item in enumerate(ordered_items):
                item_metadata[str(item["local_section_id"])] = {
                    "semantic_group_id": parent_id,
                    "semantic_group_type": "ordered_sections",
                    "semantic_group_title": str(
                        parent.get("title") or ""
                    ),
                    "semantic_group_heading_path": list(
                        parent.get("heading_path") or []
                    ),
                    "semantic_item_id": str(item["local_section_id"]),
                    "semantic_item_title": str(
                        item.get("title") or ""
                    ),
                    "semantic_item_index": item_index,
                }

        for block in candidates:
            section_id = str(
                block.get("parent_section_local_id") or ""
            )
            visited: set[str] = set()
            while section_id and section_id not in visited:
                metadata = item_metadata.get(section_id)
                if metadata is not None:
                    block.update(metadata)
                    break
                visited.add(section_id)
                section_id = parent_by_id.get(section_id, "")

    def _table_blocks(
        self,
        *,
        start_line: int,
        end_line: int,
        lines: list[str],
        line_offsets: list[int],
        section: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_rows = lines[start_line:end_line]
        parsed_rows = [self._split_table_row(row) for row in raw_rows]
        if len(parsed_rows) < 2 or not self._is_separator_row(parsed_rows[1]):
            source_text = "\n".join(raw_rows).strip()
            return [
                self._block_payload(
                    block_type="table",
                    text=source_text,
                    source_text=source_text,
                    start_line=start_line,
                    end_line=end_line,
                    line_offsets=line_offsets,
                    section=section,
                )
            ]

        headers = [self._clean_table_cell(value) for value in parsed_rows[0]]
        table_id = self._local_id("table", start_line, "|".join(headers))
        output: list[dict[str, Any]] = []
        for row_offset, cells in enumerate(parsed_rows[2:], start=2):
            cleaned_cells = [self._clean_table_cell(value) for value in cells]
            if not any(cleaned_cells):
                continue
            row_headers = [
                headers[index]
                if index < len(headers) and headers[index]
                else f"Column {index + 1}"
                for index in range(len(cleaned_cells))
            ]
            row_text = "\n".join(
                f"{header}: {value}"
                for header, value in zip(row_headers, cleaned_cells, strict=True)
                if value
            )
            row_line = start_line + row_offset
            source_text = lines[row_line].strip()
            output.append(
                self._block_payload(
                    block_type="table_row",
                    text=row_text,
                    source_text=source_text,
                    start_line=row_line,
                    end_line=row_line + 1,
                    line_offsets=line_offsets,
                    section=section,
                    extra={
                        "table_id": table_id,
                        "table_headers": row_headers,
                        "table_cells": cleaned_cells,
                        "table_row_index": row_offset - 2,
                    },
                )
            )
        return output

    def _raw_gap_block(
        self,
        *,
        lines: list[str],
        start_line: int,
        end_line: int,
        line_offsets: list[int],
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        source_text = "\n".join(lines[start_line:end_line]).strip()
        return self._block_payload(
            block_type="raw",
            text=source_text,
            source_text=source_text,
            start_line=start_line,
            end_line=end_line,
            line_offsets=line_offsets,
            section=self._section_for_line(sections, start_line),
        )

    @staticmethod
    def _block_payload(
        *,
        block_type: str,
        text: str,
        source_text: str,
        start_line: int,
        end_line: int,
        line_offsets: list[int],
        section: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": block_type,
            "text": text.strip(),
            "source_text": source_text.strip(),
            "page": None,
            "section": section["title"],
            "heading_path": list(section["heading_path"]),
            "parent_section_local_id": section["local_section_id"],
            "chapter_local_section_id": section["chapter_local_section_id"],
            "source_start_line": start_line + 1,
            "source_end_line": end_line,
            "source_start_char": line_offsets[start_line],
            "source_end_char": line_offsets[end_line],
            **(extra or {}),
        }

    @staticmethod
    def _section_for_line(
        sections: list[dict[str, Any]],
        line_index: int,
    ) -> dict[str, Any]:
        selected = sections[0]
        for section in sections[1:]:
            start = int(section["source_start_line"]) - 1
            if start > line_index:
                break
            selected = section
        return selected

    @staticmethod
    def _block_type(token_type: str) -> str:
        return {
            "paragraph_open": "paragraph",
            "blockquote_open": "blockquote",
            "fence": "code",
            "code_block": "code",
            "html_block": "html",
            "hr": "separator",
        }.get(token_type, token_type)

    @staticmethod
    def _clean_list_item(value: str) -> str:
        lines = value.splitlines()
        if lines:
            lines[0] = _LIST_MARKER.sub("", lines[0], count=1)
        return "\n".join(lines).strip()

    @staticmethod
    def _split_table_row(value: str) -> list[str]:
        row = value.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|") and not row.endswith("\\|"):
            row = row[:-1]
        cells = re.split(r"(?<!\\)\|", row)
        return [cell.replace("\\|", "|").strip() for cell in cells]

    @staticmethod
    def _is_separator_row(cells: list[str]) -> bool:
        return bool(cells) and all(
            _TABLE_SEPARATOR_CELL.fullmatch(cell.replace(" ", ""))
            for cell in cells
        )

    @staticmethod
    def _clean_table_cell(value: str) -> str:
        return _HTML_BREAK.sub("\n", value).strip()

    @staticmethod
    def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        output: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if not output or start > output[-1][1]:
                output.append((start, end))
                continue
            output[-1] = (output[-1][0], max(output[-1][1], end))
        return output

    @staticmethod
    def _local_id(*parts: object) -> str:
        payload = "\x00".join(str(part) for part in parts)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

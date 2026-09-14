#!/usr/bin/env python3
"""Export the performance-management Word draft as trusted article HTML.

The exporter keeps the source wording and document order intact. LibreOffice is
used only to recover Word's visual block structure; diagrams are taken directly
from the DOCX package so vector originals are not rasterized.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


DIMENSION_SLUGS = (
    "overview",
    "goal-setting",
    "performance-feedback-review",
    "consequence-management",
    "under-performance-management",
)

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[HtmlNode | str] = field(default_factory=list)


class LooseHtmlTreeParser(HTMLParser):
    """Build a small tolerant tree from LibreOffice's HTML output."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == normalized:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


@dataclass
class RenderState:
    slug: str
    image_paths: list[str]
    image_cursor: int = 0
    heading_cursor: int = 0
    current_heading: str = ""
    sections: list[dict[str, str]] = field(default_factory=list)


def iter_nodes(node: HtmlNode) -> Iterable[HtmlNode]:
    yield node
    for child in node.children:
        if isinstance(child, HtmlNode):
            yield from iter_nodes(child)


def plain_text(node: HtmlNode | str) -> str:
    if isinstance(node, str):
        return node
    return "".join(plain_text(child) for child in node.children)


def normalized_text(node: HtmlNode | str) -> str:
    return re.sub(r"\s+", " ", plain_text(node)).strip()


def contains_content_image(node: HtmlNode) -> bool:
    for descendant in iter_nodes(node):
        if descendant.tag != "img":
            continue
        try:
            height = int(descendant.attrs.get("height", "0"))
        except ValueError:
            height = 0
        if height > 8:
            return True
    return False


def contains_toc_anchor(node: HtmlNode) -> bool:
    return any(
        descendant.tag == "a" and descendant.attrs.get("name", "").startswith("_Toc")
        for descendant in iter_nodes(node)
    )


def safe_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def render_children(node: HtmlNode, state: RenderState) -> str:
    return "".join(render_node(child, state) for child in node.children)


def render_node(node: HtmlNode | str, state: RenderState) -> str:
    if isinstance(node, str):
        return html.escape(node, quote=False)

    tag = node.tag
    if tag == "img":
        height = safe_int(node.attrs.get("height", ""))
        if height is not None and height <= 8:
            return ""
        if state.image_cursor >= len(state.image_paths):
            raise ValueError("LibreOffice output contains more content images than the DOCX package")
        src = state.image_paths[state.image_cursor]
        state.image_cursor += 1
        width = safe_int(node.attrs.get("width", ""))
        alt = normalized_text(state.current_heading) or node.attrs.get("alt") or node.attrs.get("name") or "绩效管理原稿图示"
        dimensions = ""
        if width and height:
            dimensions = f' width="{width}" height="{height}"'
        return (
            f'<img src="{html.escape(src, quote=True)}" alt="{html.escape(alt, quote=True)}"'
            f'{dimensions} loading="lazy" decoding="async">'
        )

    if tag == "p":
        has_image = contains_content_image(node)
        text = normalized_text(node)
        content = render_children(node, state).strip()
        if contains_toc_anchor(node) and re.match(r"^\d+\.\d+\b", text):
            state.heading_cursor += 1
            state.current_heading = text
            heading_id = f"{state.slug}-section-{state.heading_cursor}"
            state.sections.append({"id": heading_id, "title": text})
            return f'<h3 id="{heading_id}">{content}</h3>'
        if has_image and not text:
            return f'<figure class="pm-doc-figure">{content}</figure>' if content else ""
        if not text and not has_image:
            return ""
        return f"<p>{content}</p>"

    if tag in {"h3", "h4"}:
        title = normalized_text(node)
        if not title:
            return ""
        state.current_heading = title
        content = render_children(node, state).strip()
        if tag == "h3":
            state.heading_cursor += 1
            heading_id = f"{state.slug}-section-{state.heading_cursor}"
            state.sections.append({"id": heading_id, "title": title})
            return f'<h3 id="{heading_id}">{content}</h3>'
        return f"<h4>{content}</h4>"

    if tag == "table":
        content = render_children(node, state).strip()
        if not content:
            return ""
        return (
            '<div class="pm-doc-table-scroll" role="region" aria-label="文档表格" tabindex="0">'
            f"<table>{content}</table></div>"
        )

    if tag in {"thead", "tbody", "tfoot", "tr", "caption"}:
        content = render_children(node, state).strip()
        return f"<{tag}>{content}</{tag}>" if content else ""

    if tag in {"td", "th"}:
        content = render_children(node, state).strip()
        attrs = ""
        for name in ("colspan", "rowspan"):
            value = safe_int(node.attrs.get(name, ""))
            if value:
                attrs += f' {name}="{value}"'
        return f"<{tag}{attrs}>{content}</{tag}>"

    if tag in {"ul", "ol"}:
        content = render_children(node, state).strip()
        if "<li" not in content:
            return content
        start = ""
        if tag == "ol":
            start_value = safe_int(node.attrs.get("start", ""))
            if start_value:
                start = f' start="{start_value}"'
        return f"<{tag}{start}>{content}</{tag}>"

    if tag == "li":
        content = render_children(node, state).strip()
        return f"<li>{content}</li>" if content else ""

    if tag in {"b", "strong"}:
        content = render_children(node, state)
        return f"<strong>{content}</strong>"

    if tag in {"i", "em"}:
        content = render_children(node, state)
        return f"<em>{content}</em>"

    if tag == "u":
        content = render_children(node, state)
        return f'<span class="pm-doc-underline">{content}</span>'

    if tag == "br":
        return "<br>"

    if tag == "a":
        content = render_children(node, state)
        href = node.attrs.get("href", "")
        if href.startswith(("https://", "http://", "#")):
            return f'<a href="{html.escape(href, quote=True)}">{content}</a>'
        return content

    return render_children(node, state)


def preferred_docx_media(docx_path: Path, output_dir: Path) -> list[str]:
    with zipfile.ZipFile(docx_path) as archive:
        relationships = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        targets = {
            relationship.attrib["Id"]: relationship.attrib["Target"]
            for relationship in relationships.findall(f"{PACKAGE_REL_NS}Relationship")
            if relationship.attrib.get("Type", "").endswith("/image")
        }
        document = ET.fromstring(archive.read("word/document.xml"))
        selected: list[str] = []
        for paragraph in document.findall(f".//{W_NS}p"):
            paragraph_targets: list[str] = []
            for descendant in paragraph.iter():
                relationship_id = descendant.attrib.get(f"{R_NS}embed")
                target = targets.get(relationship_id or "")
                if target and target not in paragraph_targets:
                    paragraph_targets.append(target)
            if not paragraph_targets:
                continue
            vector = next((target for target in paragraph_targets if target.lower().endswith(".svg")), None)
            selected.append(vector or paragraph_targets[0])

        output_dir.mkdir(parents=True, exist_ok=True)
        public_paths: list[str] = []
        for target in selected:
            source_name = f"word/{target}"
            destination = output_dir / Path(target).name
            destination.write_bytes(archive.read(source_name))
            public_paths.append(f"/assets/resource/performance-management-document/{destination.name}")
        return public_paths


def parse_chapters(converted_html: str, image_paths: list[str]) -> list[dict[str, object]]:
    parser = LooseHtmlTreeParser()
    parser.feed(converted_html)
    document_body = next(
        (node for node in iter_nodes(parser.root) if node.tag == "body"),
        None,
    )
    if document_body is None:
        raise ValueError("Unable to find LibreOffice document body")

    raw_chapters: list[tuple[HtmlNode, list[HtmlNode | str]]] = []
    preface_nodes: list[HtmlNode | str] = []
    current_heading: HtmlNode | None = None
    current_nodes: list[HtmlNode | str] = []
    for child in document_body.children:
        if isinstance(child, HtmlNode) and child.tag == "h2":
            if current_heading is not None:
                raw_chapters.append((current_heading, current_nodes))
            current_heading = child
            current_nodes = []
        elif current_heading is not None:
            current_nodes.append(child)
        elif (
            not isinstance(child, HtmlNode)
            or (
                child.attrs.get("id") != "Table of Contents1"
                and child.attrs.get("title") not in {"header", "footer"}
            )
        ):
            preface_nodes.append(child)
    if current_heading is not None:
        raw_chapters.append((current_heading, current_nodes))

    if len(raw_chapters) != len(DIMENSION_SLUGS):
        raise ValueError(f"Expected {len(DIMENSION_SLUGS)} chapters, found {len(raw_chapters)}")

    first_heading, first_nodes = raw_chapters[0]
    raw_chapters[0] = (first_heading, [*preface_nodes, *first_nodes])

    chapters: list[dict[str, object]] = []
    image_cursor = 0
    for slug, (heading, nodes) in zip(DIMENSION_SLUGS, raw_chapters, strict=True):
        state = RenderState(slug=slug, image_paths=image_paths, image_cursor=image_cursor)
        body_html = "".join(render_node(node, state) for node in nodes).strip()
        image_cursor = state.image_cursor
        chapters.append({
            "slug": slug,
            "title": normalized_text(heading),
            "bodyHtml": body_html,
            "sections": state.sections,
            "imageCount": body_html.count("<img "),
        })

    if image_cursor != len(image_paths):
        raise ValueError(f"Expected to render {len(image_paths)} images, rendered {image_cursor}")
    return chapters


def write_typescript(chapters: list[dict[str, object]], output_path: Path) -> None:
    chapter_map = {str(chapter["slug"]): {key: value for key, value in chapter.items() if key != "slug"} for chapter in chapters}
    serialized = json.dumps(chapter_map, ensure_ascii=False, indent=2)
    source = f'''// Generated from 绩效管理草稿.docx by scripts/export_performance_management_document.py.
// Preserve the source wording and image order; regenerate instead of editing manually.

import type {{ PerformanceManagementDimensionSlug }} from './performance-management-content';

export interface PerformanceManagementDocumentSection {{
  id: string;
  title: string;
}}

export interface PerformanceManagementDocumentChapter {{
  title: string;
  bodyHtml: string;
  sections: readonly PerformanceManagementDocumentSection[];
  imageCount: number;
}}

export const performanceManagementDocumentChapters = Object.freeze({serialized}) satisfies Readonly<
  Record<PerformanceManagementDimensionSlug, PerformanceManagementDocumentChapter>
>;
'''
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(source, encoding="utf-8")


def convert_document(docx_path: Path, repo_root: Path) -> tuple[list[dict[str, object]], Path]:
    asset_dir = repo_root / "data/frontend/assets/resource/performance-management-document"
    image_paths = preferred_docx_media(docx_path, asset_dir)
    with tempfile.TemporaryDirectory(prefix="performance-management-docx-") as temp_directory:
        temp_path = Path(temp_directory)
        profile_path = temp_path / "libreoffice-profile"
        subprocess.run(
            [
                "libreoffice",
                "--headless",
                f"-env:UserInstallation={profile_path.as_uri()}",
                "--convert-to",
                "html",
                "--outdir",
                str(temp_path),
                str(docx_path),
            ],
            check=True,
        )
        converted_path = temp_path / f"{docx_path.stem}.html"
        chapters = parse_chapters(converted_path.read_text(encoding="utf-8"), image_paths)

    output_path = repo_root / "frontend/src/content/performance-management-document.ts"
    write_typescript(chapters, output_path)
    return chapters, output_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docx", type=Path, default=None)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    docx_path = (args.docx or repo_root / "绩效管理草稿.docx").resolve()
    if not docx_path.is_file():
        raise FileNotFoundError(docx_path)

    chapters, output_path = convert_document(docx_path, repo_root)
    print(f"exported={output_path}")
    print(f"chapters={len(chapters)} images={sum(int(chapter['imageCount']) for chapter in chapters)}")
    for chapter in chapters:
        print(
            f"{chapter['slug']}: sections={len(chapter['sections'])} "
            f"images={chapter['imageCount']} title={chapter['title']}"
        )


if __name__ == "__main__":
    main()

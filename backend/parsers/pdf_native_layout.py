from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Iterable, Literal, Sequence
import xml.etree.ElementTree as ET
import zlib


BBox = tuple[float, float, float, float]
PageSeverity = Literal["ok", "moderate", "severe", "unreadable"]

_TOKEN_PATTERN = re.compile(
    r"[0-9]+(?:[.,:/\-][0-9A-Za-z]+)*"
    r"|[A-Za-z\u00c0-\u024f]+(?:['\u2019.\-][A-Za-z\u00c0-\u024f]+)*"
    r"|[\u3400-\u9fff]"
    r"|[\u3040-\u30ff]"
    r"|[\uac00-\ud7af]",
    re.UNICODE,
)
_CJK_OR_KANA_PATTERN = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_VISUAL_TYPES = {"image", "figure", "chart", "diagram", "table", "table_body"}
_TABLE_TYPES = {"table", "table_body"}
_RELATION_VISUAL_TYPES = {
    "chart",
    "cycle",
    "dashboard",
    "diagram",
    "flowchart",
    "hierarchy",
    "matrix",
    "process",
    "timeline",
}


@dataclass(frozen=True)
class NativeWord:
    text: str
    bbox: BBox


@dataclass(frozen=True)
class NativeLine:
    text: str
    bbox: BBox
    words: tuple[NativeWord, ...] = ()


@dataclass(frozen=True)
class NativePage:
    page_index: int
    width: float
    height: float
    lines: tuple[NativeLine, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines if line.text.strip())


@dataclass(frozen=True)
class NativeLayoutExtraction:
    pages: tuple[NativePage, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PdfPageQuality:
    page_index: int
    native_token_count: int
    mineru_token_count: int
    lexical_recall: float | None
    severity: PageSeverity
    structure_risk: bool
    grid_like: bool
    has_mineru_table: bool
    largest_visual_ratio: float
    needs_native_fallback: bool
    visual_candidate: bool
    risk_score: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PdfQualityThresholds:
    severe_min_native_tokens: int = 80
    moderate_min_native_tokens: int = 20
    severe_recall: float = 0.65
    moderate_recall: float = 0.88
    large_visual_ratio: float = 0.45
    visual_candidate_max_pages: int = 3
    visual_candidate_ratio: float = 0.10


@dataclass(frozen=True)
class PdfNativeLayoutEnrichment:
    native_pages: tuple[NativePage, ...] = ()
    page_quality: tuple[PdfPageQuality, ...] = ()
    fallback_blocks: tuple[dict[str, Any], ...] = ()
    visual_candidate_pages: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()

    def quality_report(self) -> dict[str, Any]:
        return {
            "schema_version": "pdf-native-layout-v1",
            "pages": [asdict(item) for item in self.page_quality],
            "visual_candidate_pages": list(self.visual_candidate_pages),
            "fallback_block_count": len(self.fallback_blocks),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RenderedPdfPage:
    page_index: int
    image_path: Path
    dpi: int


@dataclass(frozen=True)
class PdfPageRenderResult:
    pages: tuple[RenderedPdfPage, ...] = ()
    warnings: tuple[str, ...] = ()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = str(data or "").strip()
        if value:
            self.parts.append(value)


def tokenize_layout_text(value: Any) -> list[str]:
    """Tokenize Latin, numeric, CJK, Japanese and Korean layout text."""

    return [match.group(0).casefold() for match in _TOKEN_PATTERN.finditer(str(value or ""))]


def lexical_token_recall(native_text: str, parsed_text: str) -> float | None:
    """Return multiset recall of native PDF tokens represented by parsed text."""

    native = Counter(tokenize_layout_text(native_text))
    if not native:
        return None
    parsed = Counter(tokenize_layout_text(parsed_text))
    matched = sum(min(count, parsed.get(token, 0)) for token, count in native.items())
    return matched / sum(native.values())


def extract_pdf_native_layout(
    pdf_path: Path,
    *,
    timeout_seconds: float = 120.0,
    executable: str = "pdftotext",
) -> NativeLayoutExtraction:
    """Extract exact PDF words and bounding boxes through Poppler.

    This is deliberately fail-open. A missing text layer, malformed PDF, absent
    Poppler binary, or timeout yields an empty result with a diagnostic warning;
    callers can keep the original MinerU output unchanged.
    """

    path = Path(pdf_path)
    if path.suffix.lower() != ".pdf":
        return NativeLayoutExtraction(warnings=("原生布局提取仅支持 PDF。",))
    if not path.is_file():
        return NativeLayoutExtraction(warnings=(f"PDF 不存在: {path}",))
    binary = shutil.which(executable)
    if not binary:
        return NativeLayoutExtraction(warnings=(f"未找到 {executable}，跳过 PDF 原生布局兜底。",))

    try:
        with tempfile.TemporaryDirectory(prefix="pdf-native-layout-") as temp_dir:
            output_path = Path(temp_dir) / "layout.xhtml"
            completed = subprocess.run(
                [
                    binary,
                    "-bbox-layout",
                    "-enc",
                    "UTF-8",
                    str(path),
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                timeout=max(1.0, float(timeout_seconds)),
            )
            if completed.returncode != 0 or not output_path.is_file():
                stderr = completed.stderr.decode("utf-8", errors="ignore").strip()
                detail = f": {stderr[-500:]}" if stderr else ""
                return NativeLayoutExtraction(
                    warnings=(f"pdftotext 原生布局提取失败{detail}",)
                )
            pages = _parse_bbox_layout(output_path)
    except subprocess.TimeoutExpired:
        return NativeLayoutExtraction(warnings=("pdftotext 原生布局提取超时。",))
    except (OSError, ET.ParseError, ValueError) as exc:
        return NativeLayoutExtraction(warnings=(f"PDF 原生布局解析失败: {exc}",))

    if not pages:
        return NativeLayoutExtraction(warnings=("PDF 没有可读取的原生文本页，可能是扫描件。",))
    if not any(page.lines for page in pages):
        return NativeLayoutExtraction(
            pages=tuple(pages),
            warnings=("PDF 没有可读取的原生文本层，保留现有解析结果。",),
        )
    return NativeLayoutExtraction(pages=tuple(pages))


def assess_pdf_page_quality(
    native_pages: Sequence[NativePage],
    mineru_content: Any,
    *,
    thresholds: PdfQualityThresholds | None = None,
) -> tuple[PdfPageQuality, ...]:
    """Compare native text coverage and structural signals page by page."""

    config = thresholds or PdfQualityThresholds()
    nodes = _coerce_content_nodes(mineru_content)
    mineru_by_page = _mineru_page_evidence(nodes)
    assessments: list[PdfPageQuality] = []

    for page in native_pages:
        evidence = mineru_by_page.get(page.page_index, _MineruPageEvidence())
        native_tokens = tokenize_layout_text(page.text)
        parsed_tokens = tokenize_layout_text(evidence.text)
        recall = lexical_token_recall(page.text, evidence.text)
        grid_like = _is_grid_like(page)
        large_visual = evidence.largest_visual_ratio >= config.large_visual_ratio
        ocr_only_page = not native_tokens and len(parsed_tokens) >= 20
        structure_risk = (
            (grid_like and not evidence.has_table)
            or evidence.has_relation_visual
            # A large visual can preserve every label while still losing all
            # arrows, containment, ordering or spatial meaning in plain OCR.
            or large_visual
            or ocr_only_page
        )
        reasons: list[str] = []

        if not native_tokens:
            severity: PageSeverity = "unreadable"
            reasons.append("native_text_unavailable")
        elif (
            len(native_tokens) >= config.severe_min_native_tokens
            and recall is not None
            and recall < config.severe_recall
        ):
            severity = "severe"
            reasons.append("severe_native_text_loss")
        elif (
            len(native_tokens) >= config.moderate_min_native_tokens
            and recall is not None
            and recall < config.moderate_recall
        ) or (
            len(native_tokens) < config.moderate_min_native_tokens
            and recall is not None
            and (1.0 - recall) * len(native_tokens) >= 8
        ):
            severity = "moderate"
            reasons.append("moderate_native_text_loss")
        elif structure_risk:
            severity = "moderate"
            reasons.append("layout_relation_risk")
        else:
            severity = "ok"

        if grid_like and not evidence.has_table:
            reasons.append("native_grid_without_structured_table")
        if evidence.has_relation_visual:
            reasons.append("explicit_relation_visual")
        if large_visual:
            reasons.append("large_visual_region")
        if ocr_only_page:
            reasons.append("ocr_only_page")

        needs_fallback = bool(native_tokens) and severity in {"moderate", "severe"}
        visual_candidate = severity in {"moderate", "severe"} or (
            severity == "unreadable"
            and (large_visual or ocr_only_page)
        )
        missing_ratio = 1.0 - recall if recall is not None else 0.0
        risk_score = (
            missing_ratio * 100.0
            + (30.0 if severity == "severe" else 12.0 if severity == "moderate" else 0.0)
            + (20.0 if grid_like and not evidence.has_table else 0.0)
            + (20.0 if evidence.has_relation_visual else 0.0)
            + (18.0 if ocr_only_page else 0.0)
            + evidence.largest_visual_ratio * 15.0
            + min(10.0, len(native_tokens) / 20.0)
        )
        assessments.append(
            PdfPageQuality(
                page_index=page.page_index,
                native_token_count=len(native_tokens),
                mineru_token_count=len(parsed_tokens),
                lexical_recall=round(recall, 6) if recall is not None else None,
                severity=severity,
                structure_risk=structure_risk,
                grid_like=grid_like,
                has_mineru_table=evidence.has_table,
                largest_visual_ratio=round(evidence.largest_visual_ratio, 6),
                needs_native_fallback=needs_fallback,
                visual_candidate=visual_candidate,
                risk_score=round(risk_score, 6),
                reasons=tuple(dict.fromkeys(reasons)),
            )
        )
    return tuple(assessments)


def select_visual_candidate_pages(
    page_quality: Sequence[PdfPageQuality],
    *,
    max_pages: int = 3,
    max_ratio: float = 0.10,
) -> tuple[int, ...]:
    """Choose only the highest-risk ~10% of pages, capped at three."""

    page_count = len(page_quality)
    if page_count <= 0 or max_pages <= 0 or max_ratio <= 0:
        return ()
    ratio_limit = max(1, math.ceil(page_count * max_ratio))
    limit = min(int(max_pages), ratio_limit)
    candidates = [item for item in page_quality if item.visual_candidate]
    candidates.sort(key=lambda item: (-item.risk_score, item.page_index))
    return tuple(item.page_index for item in candidates[:limit])


def build_native_layout_fallback_blocks(
    native_pages: Sequence[NativePage],
    mineru_content: Any,
    page_quality: Sequence[PdfPageQuality],
) -> tuple[dict[str, Any], ...]:
    """Create page-scoped blocks only for native rows MinerU did not preserve."""

    nodes = _coerce_content_nodes(mineru_content)
    mineru_by_page = _mineru_page_evidence(nodes)
    quality_by_page = {item.page_index: item for item in page_quality}
    output: list[dict[str, Any]] = []

    for page in native_pages:
        quality = quality_by_page.get(page.page_index)
        if quality is None or not quality.needs_native_fallback:
            continue
        existing_text = mineru_by_page.get(page.page_index, _MineruPageEvidence()).text
        available = Counter(tokenize_layout_text(existing_text))
        normalized_existing = _normalize_for_dedup(existing_text)
        rows = (
            _spatial_rows(page)
            if quality.grid_like
            else tuple(
                _SpatialRow(line.text, line.bbox, (line,))
                for line in sorted(page.lines, key=lambda item: (item.bbox[1], item.bbox[0]))
            )
        )
        seen_rows: list[tuple[str, BBox]] = []
        for row in rows:
            text = row.text.strip()
            row_tokens = tokenize_layout_text(text)
            if not text or not row_tokens:
                continue
            normalized_row = _normalize_for_dedup(text)
            if any(
                normalized_row == seen_text and _bbox_iou(row.bbox, seen_bbox) >= 0.8
                for seen_text, seen_bbox in seen_rows
            ):
                continue
            seen_rows.append((normalized_row, row.bbox))
            if normalized_row and normalized_row in normalized_existing:
                _consume_available_tokens(available, row_tokens)
                continue
            matched = sum(min(count, available.get(token, 0)) for token, count in Counter(row_tokens).items())
            missing_ratio = 1.0 - (matched / len(row_tokens))
            if missing_ratio < 0.15:
                _consume_available_tokens(available, row_tokens)
                continue
            _consume_available_tokens(available, row_tokens)
            output.append(
                {
                    "type": "native_layout_fallback",
                    "text": text,
                    "page": page.page_index,
                    "bbox": _normalized_bbox(row.bbox, page.width, page.height),
                    "native_bbox": [round(value, 3) for value in row.bbox],
                    "page_size": [round(page.width, 3), round(page.height, 3)],
                    "source": "pdf_native_text",
                    "quality_severity": quality.severity,
                    "lexical_recall": quality.lexical_recall,
                    "layout_items": [
                        {
                            "text": item.text,
                            "native_bbox": [round(value, 3) for value in item.bbox],
                        }
                        for item in row.items
                    ],
                }
            )
    return tuple(output)


def build_pdf_native_layout_enrichment(
    pdf_path: Path,
    mineru_content: Any,
    *,
    thresholds: PdfQualityThresholds | None = None,
    timeout_seconds: float = 120.0,
) -> PdfNativeLayoutEnrichment:
    """Run the complete deterministic quality and fallback stage."""

    config = thresholds or PdfQualityThresholds()
    extraction = extract_pdf_native_layout(pdf_path, timeout_seconds=timeout_seconds)
    native_pages = extraction.pages or _placeholder_pages_from_mineru(mineru_content)
    if not native_pages:
        return PdfNativeLayoutEnrichment(warnings=extraction.warnings)
    quality = assess_pdf_page_quality(
        native_pages,
        mineru_content,
        thresholds=config,
    )
    candidates = select_visual_candidate_pages(
        quality,
        max_pages=config.visual_candidate_max_pages,
        max_ratio=config.visual_candidate_ratio,
    )
    fallback = build_native_layout_fallback_blocks(
        native_pages,
        mineru_content,
        quality,
    )
    warnings = list(extraction.warnings)
    if not extraction.pages and native_pages:
        warnings.append(
            "PDF 原生文字页不可用，已根据 MinerU 页码继续筛选扫描页和大面积视觉页。"
        )
    return PdfNativeLayoutEnrichment(
        native_pages=native_pages,
        page_quality=quality,
        fallback_blocks=fallback,
        visual_candidate_pages=candidates,
        warnings=tuple(warnings),
    )


def _placeholder_pages_from_mineru(mineru_content: Any) -> tuple[NativePage, ...]:
    """Keep scanned/image-only pages eligible for selective vision."""

    evidence = _mineru_page_evidence(_coerce_content_nodes(mineru_content))
    return tuple(
        NativePage(page_index=page_index, width=1000.0, height=1000.0)
        for page_index in sorted(evidence)
        if page_index >= 0
    )


def render_pdf_pages(
    pdf_path: Path,
    page_indexes: Iterable[int],
    output_dir: Path,
    *,
    dpi: int = 180,
    timeout_seconds: float = 120.0,
    executable: str = "pdftoppm",
) -> PdfPageRenderResult:
    """Render selected complete pages for a downstream vision model.

    Rendering is page-isolated and fail-open: one bad page does not discard
    successfully rendered candidates or the source document parse.
    """

    path = Path(pdf_path)
    selected = sorted({int(index) for index in page_indexes if int(index) >= 0})
    if not selected:
        return PdfPageRenderResult()
    binary = shutil.which(executable)
    if not binary:
        return PdfPageRenderResult(
            warnings=(f"未找到 {executable}，跳过复杂页视觉分析。",)
        )
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    rendered: list[RenderedPdfPage] = []
    warnings: list[str] = []
    effective_dpi = max(72, int(dpi))

    for page_index in selected:
        image_path = destination / (
            f"page-{page_index + 1:04d}-vision-{effective_dpi}dpi.png"
        )
        if _is_valid_png(image_path):
            rendered.append(RenderedPdfPage(page_index, image_path, effective_dpi))
            continue
        temporary_prefix: Path | None = None
        temporary_image: Path | None = None
        try:
            # The same content hash can be processed concurrently. Render to a
            # unique sibling first, then publish atomically so workers never
            # inspect, delete, or reuse another worker's partial PNG.
            with tempfile.NamedTemporaryFile(
                dir=destination,
                prefix=f".{image_path.stem}.",
                suffix=".render",
                delete=False,
            ) as marker:
                temporary_prefix = Path(marker.name)
            cleanup_error = _safe_unlink(temporary_prefix)
            if cleanup_error:
                warnings.append(
                    f"第 {page_index + 1} 页临时渲染路径准备失败: {cleanup_error}"
                )
                continue
            temporary_image = Path(f"{temporary_prefix}.png")
            completed = subprocess.run(
                [
                    binary,
                    "-f",
                    str(page_index + 1),
                    "-l",
                    str(page_index + 1),
                    "-r",
                    str(effective_dpi),
                    "-png",
                    "-singlefile",
                    str(path),
                    str(temporary_prefix),
                ],
                check=False,
                capture_output=True,
                timeout=max(1.0, float(timeout_seconds)),
            )
        except subprocess.TimeoutExpired:
            warnings.append(f"第 {page_index + 1} 页渲染超时。")
            if temporary_image is not None:
                cleanup_error = _safe_unlink(temporary_image)
                if cleanup_error:
                    warnings.append(
                        f"第 {page_index + 1} 页超时产物清理失败: {cleanup_error}"
                    )
            continue
        except OSError as exc:
            warnings.append(f"第 {page_index + 1} 页渲染失败: {exc}")
            if temporary_image is not None:
                cleanup_error = _safe_unlink(temporary_image)
                if cleanup_error:
                    warnings.append(
                        f"第 {page_index + 1} 页失败产物清理失败: {cleanup_error}"
                    )
            continue
        finally:
            if temporary_prefix is not None:
                _safe_unlink(temporary_prefix)
        try:
            if temporary_image is None or completed.returncode != 0 or not _is_valid_png(
                temporary_image
            ):
                stderr_value = completed.stderr or b""
                stderr = stderr_value.decode("utf-8", errors="ignore").strip()
                detail = f": {stderr[-300:]}" if stderr else ""
                warnings.append(f"第 {page_index + 1} 页渲染失败{detail}")
                continue
            try:
                temporary_image.replace(image_path)
            except OSError as exc:
                # Another worker may have published the same valid page first.
                if not _is_valid_png(image_path):
                    warnings.append(f"第 {page_index + 1} 页渲染产物发布失败: {exc}")
                    continue
            if not _is_valid_png(image_path):
                warnings.append(f"第 {page_index + 1} 页渲染产物发布后校验失败。")
                continue
            rendered.append(RenderedPdfPage(page_index, image_path, effective_dpi))
        finally:
            if temporary_image is not None:
                cleanup_error = _safe_unlink(temporary_image)
                if cleanup_error:
                    warnings.append(
                        f"第 {page_index + 1} 页临时渲染文件清理失败: {cleanup_error}"
                    )
    return PdfPageRenderResult(tuple(rendered), tuple(warnings))


@dataclass(frozen=True)
class _MineruPageEvidence:
    text: str = ""
    has_table: bool = False
    has_relation_visual: bool = False
    largest_visual_ratio: float = 0.0


@dataclass(frozen=True)
class _SpatialRow:
    text: str
    bbox: BBox
    items: tuple[NativeLine, ...]


def _parse_bbox_layout(path: Path) -> list[NativePage]:
    root = ET.parse(path).getroot()
    pages: list[NativePage] = []
    for page_index, page_node in enumerate(root.findall(".//{*}page")):
        width = _safe_float(page_node.attrib.get("width"))
        height = _safe_float(page_node.attrib.get("height"))
        if width <= 0 or height <= 0:
            continue
        lines: list[NativeLine] = []
        for line_node in page_node.findall(".//{*}line"):
            words: list[NativeWord] = []
            for word_node in line_node.findall("./{*}word"):
                text = "".join(word_node.itertext()).strip()
                bbox = _bbox_from_attributes(word_node.attrib)
                if text and bbox:
                    words.append(NativeWord(text=text, bbox=bbox))
            if not words:
                continue
            bbox = _bbox_from_attributes(line_node.attrib) or _union_bbox(word.bbox for word in words)
            if bbox is None:
                continue
            text = _join_native_words(words)
            if text:
                lines.append(NativeLine(text=text, bbox=bbox, words=tuple(words)))
        pages.append(NativePage(page_index, width, height, tuple(lines)))
    return pages


def _join_native_words(words: Sequence[NativeWord]) -> str:
    if not words:
        return ""
    output = words[0].text
    previous = words[0]
    for word in words[1:]:
        if _needs_word_space(previous, word):
            output += " "
        output += word.text
        previous = word
    return output.strip()


def _needs_word_space(previous: NativeWord, current: NativeWord) -> bool:
    if not previous.text or not current.text:
        return False
    if current.text[0] in ",.;:!?%)]}\u3001\u3002\uff0c\uff1b\uff1a\uff01\uff1f":
        return False
    if previous.text[-1] in "([{":
        return False
    horizontal_gap = current.bbox[0] - previous.bbox[2]
    previous_height = max(1.0, previous.bbox[3] - previous.bbox[1])
    if (
        _CJK_OR_KANA_PATTERN.fullmatch(previous.text[-1])
        and _CJK_OR_KANA_PATTERN.fullmatch(current.text[0])
    ):
        return horizontal_gap > previous_height * 0.45
    return horizontal_gap > -previous_height * 0.12


def _coerce_content_nodes(content: Any) -> list[Any]:
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return []
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        return [content]
    return []


def _mineru_page_evidence(nodes: Sequence[Any]) -> dict[int, _MineruPageEvidence]:
    text_by_page: dict[int, list[str]] = {}
    has_table: dict[int, bool] = {}
    has_relation_visual: dict[int, bool] = {}
    largest_visual: dict[int, float] = {}

    def walk(node: Any, inherited_page: int | None = None) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, inherited_page)
            return
        if not isinstance(node, dict):
            return
        page = _page_index(node, inherited_page)
        block_type = str(
            node.get("type")
            or node.get("block_type")
            or node.get("category")
            or node.get("layout_type")
            or ""
        ).casefold()
        if page is not None:
            text = _node_text(node)
            if text:
                text_by_page.setdefault(page, []).append(text)
            if block_type in _TABLE_TYPES:
                has_table[page] = True
            if block_type in _RELATION_VISUAL_TYPES:
                has_relation_visual[page] = True
            if block_type in _VISUAL_TYPES:
                ratio = _visual_bbox_ratio(node.get("bbox") or node.get("poly") or node.get("box"))
                largest_visual[page] = max(largest_visual.get(page, 0.0), ratio)
        for key, value in node.items():
            if key in {"bbox", "poly", "box"}:
                continue
            if isinstance(value, (dict, list)):
                walk(value, page)

    for root in nodes:
        walk(root)
    page_indexes = (
        set(text_by_page)
        | set(has_table)
        | set(has_relation_visual)
        | set(largest_visual)
    )
    return {
        page: _MineruPageEvidence(
            text="\n".join(text_by_page.get(page, [])),
            has_table=has_table.get(page, False),
            has_relation_visual=has_relation_visual.get(page, False),
            largest_visual_ratio=largest_visual.get(page, 0.0),
        )
        for page in page_indexes
    }


def _node_text(node: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("text", "content", "md", "html", "table_body"):
        value = node.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        parts.append(_html_to_text(value) if "<" in value and ">" in value else value)
    for key in (
        "image_caption",
        "image_footnote",
        "table_caption",
        "table_footnote",
    ):
        value = node.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
        elif isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(dict.fromkeys(part.strip() for part in parts if part.strip()))


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return value
    return " ".join(parser.parts)


def _page_index(node: dict[str, Any], fallback: int | None = None) -> int | None:
    for key in ("page", "page_idx", "page_num", "page_no"):
        value = node.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return fallback


def _visual_bbox_ratio(value: Any) -> float:
    bbox = _coerce_bbox(value)
    if bbox is None:
        return 0.0
    width = max(0.0, bbox[2] - bbox[0])
    height = max(0.0, bbox[3] - bbox[1])
    return min(1.0, width * height / 1_000_000.0)


def _is_grid_like(page: NativePage) -> bool:
    rows = _spatial_rows(page)
    dense_rows = sum(1 for row in rows if len(row.items) >= 3)
    distinct_columns: set[int] = set()
    for line in page.lines:
        center = (line.bbox[0] + line.bbox[2]) / 2.0
        distinct_columns.add(min(4, int(center / max(1.0, page.width) * 5)))
    return dense_rows >= 3 and len(distinct_columns) >= 3


def _spatial_rows(page: NativePage) -> tuple[_SpatialRow, ...]:
    horizontal: list[NativeLine] = []
    vertical: list[NativeLine] = []
    for line in page.lines:
        width = max(1.0, line.bbox[2] - line.bbox[0])
        height = max(1.0, line.bbox[3] - line.bbox[1])
        (vertical if height > width * 2.5 else horizontal).append(line)
    horizontal.sort(key=lambda item: ((item.bbox[1] + item.bbox[3]) / 2.0, item.bbox[0]))
    groups: list[list[NativeLine]] = []
    for line in horizontal:
        center = (line.bbox[1] + line.bbox[3]) / 2.0
        line_height = max(1.0, line.bbox[3] - line.bbox[1])
        if groups:
            previous_center = sum(
                (item.bbox[1] + item.bbox[3]) / 2.0 for item in groups[-1]
            ) / len(groups[-1])
            previous_height = max(
                1.0,
                sum(item.bbox[3] - item.bbox[1] for item in groups[-1]) / len(groups[-1]),
            )
            if abs(center - previous_center) <= max(2.0, min(line_height, previous_height) * 0.55):
                groups[-1].append(line)
                continue
        groups.append([line])

    rows: list[_SpatialRow] = []
    for group in groups:
        ordered = sorted(group, key=lambda item: item.bbox[0])
        text = ordered[0].text
        previous = ordered[0]
        for item in ordered[1:]:
            gap = item.bbox[0] - previous.bbox[2]
            row_height = max(1.0, previous.bbox[3] - previous.bbox[1])
            text += (" | " if gap > row_height * 1.5 else " ") + item.text
            previous = item
        bbox = _union_bbox(item.bbox for item in ordered)
        if bbox:
            rows.append(_SpatialRow(text.strip(), bbox, tuple(ordered)))
    for line in vertical:
        rows.append(_SpatialRow(line.text, line.bbox, (line,)))
    rows.sort(key=lambda row: (row.bbox[1], row.bbox[0]))
    return tuple(rows)


def _consume_available_tokens(available: Counter[str], tokens: Sequence[str]) -> None:
    for token in tokens:
        if available.get(token, 0) > 0:
            available[token] -= 1


def _normalize_for_dedup(value: Any) -> str:
    return "".join(tokenize_layout_text(value))


def _bbox_iou(left: BBox, right: BBox) -> float:
    intersection_width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    intersection_height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    intersection = intersection_width * intersection_height
    if intersection <= 0:
        return 0.0
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _is_valid_png(path: Path) -> bool:
    """Validate the complete top-level PNG chunk stream.

    A signature and IHDR alone are insufficient: pdftoppm may have written
    those bytes before timing out. Requiring a bounded chunk stream with image
    data and a terminal IEND keeps truncated renders out of the cache without
    introducing an image-decoder dependency.
    """

    try:
        if path.stat().st_size > 512 * 1024 * 1024:
            return False
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
            saw_ihdr = False
            saw_idat = False
            chunk_count = 0
            while chunk_count < 100_000:
                chunk_count += 1
                chunk_header = handle.read(8)
                if len(chunk_header) != 8:
                    return False
                chunk_length = int.from_bytes(chunk_header[:4], "big")
                chunk_type = chunk_header[4:]
                if chunk_length > 256 * 1024 * 1024:
                    return False
                remaining = chunk_length
                crc = zlib.crc32(chunk_type)
                ihdr = bytearray()
                while remaining:
                    data = handle.read(min(64 * 1024, remaining))
                    if not data:
                        return False
                    if not saw_ihdr and chunk_type == b"IHDR":
                        ihdr.extend(data)
                    crc = zlib.crc32(data, crc)
                    remaining -= len(data)
                expected_crc = handle.read(4)
                if len(expected_crc) != 4:
                    return False
                if (crc & 0xFFFFFFFF) != int.from_bytes(expected_crc, "big"):
                    return False
                if not saw_ihdr:
                    if chunk_type != b"IHDR" or chunk_length != 13:
                        return False
                    width = int.from_bytes(ihdr[:4], "big")
                    height = int.from_bytes(ihdr[4:8], "big")
                    if width <= 0 or height <= 0:
                        return False
                    saw_ihdr = True
                elif chunk_type == b"IHDR":
                    return False
                if chunk_type == b"IDAT" and chunk_length > 0:
                    saw_idat = True
                if chunk_type == b"IEND":
                    return (
                        chunk_length == 0
                        and saw_ihdr
                        and saw_idat
                        and handle.read(1) == b""
                    )
            return False
    except OSError:
        return False


def _safe_unlink(path: Path) -> str:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return str(exc)
    return ""


def _normalized_bbox(bbox: BBox, width: float, height: float) -> list[int]:
    return [
        max(0, min(1000, round(bbox[0] / width * 1000))),
        max(0, min(1000, round(bbox[1] / height * 1000))),
        max(0, min(1000, round(bbox[2] / width * 1000))),
        max(0, min(1000, round(bbox[3] / height * 1000))),
    ]


def _bbox_from_attributes(attributes: dict[str, Any]) -> BBox | None:
    try:
        bbox = (
            float(attributes["xMin"]),
            float(attributes["yMin"]),
            float(attributes["xMax"]),
            float(attributes["yMax"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return bbox if bbox[2] >= bbox[0] and bbox[3] >= bbox[1] else None


def _coerce_bbox(value: Any) -> BBox | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if len(numbers) == 4:
        x0, y0, x1, y1 = numbers
    elif len(numbers) % 2 == 0:
        xs = numbers[0::2]
        ys = numbers[1::2]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    else:
        return None
    return (x0, y0, x1, y1) if x1 >= x0 and y1 >= y0 else None


def _union_bbox(values: Iterable[BBox]) -> BBox | None:
    items = list(values)
    if not items:
        return None
    return (
        min(item[0] for item in items),
        min(item[1] for item in items),
        max(item[2] for item in items),
        max(item[3] for item in items),
    )


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

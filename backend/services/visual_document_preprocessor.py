from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from backend.config.settings import Settings, get_settings
from backend.parsers.mineru import MinerUParser
from backend.schemas.document import ParsedDocument
from backend.services.document_image_analysis_service import (
    DocumentImageAnalysisService,
)


class VisualDocumentPreprocessor:
    """Prepare a visual PDF as validated Markdown for the processed KB.

    MinerU and the deterministic native-layout guard remain separate from the
    runtime indexer. Only the small set of risky full pages selected by the
    guard is sent to the document-vision model.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        parser: MinerUParser | None = None,
        image_analysis_service: DocumentImageAnalysisService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.parser = parser or MinerUParser(settings=self.settings)
        self.image_analysis_service = image_analysis_service

    def prepare(
        self,
        path: Path,
        *,
        document_id: str | None = None,
        enable_vision: bool = True,
        strict_vision: bool = False,
    ) -> ParsedDocument:
        source = Path(path)
        text = self.parser.parse(
            source,
            prepare_visual_pages=bool(
                enable_vision and self.settings.document_vision_enabled
            ),
        )
        metadata = dict(self.parser.last_metadata)
        blocks = [dict(block) for block in metadata.get("structured_blocks") or []]
        tables = [dict(table) for table in metadata.get("tables") or []]
        images = [dict(image) for image in metadata.get("images") or []]
        pages = [dict(page) for page in metadata.get("pages") or []]
        candidates = [
            image
            for image in images
            if image.get("analysis_scope") == "full_page"
            or bool(image.get("full_page"))
        ]
        warnings = list(metadata.get("artifact_warnings") or [])
        selected_pages = list(metadata.get("visual_candidate_pages") or [])
        selected_candidate_count = max(len(selected_pages), len(candidates))
        vision_requested = bool(
            enable_vision and self.settings.document_vision_enabled
        )
        render_failure_count = (
            max(0, selected_candidate_count - len(candidates))
            if vision_requested
            else 0
        )

        vision_stats = DocumentImageAnalysisService.empty_stats(
            enabled=vision_requested,
            candidate_count=selected_candidate_count if vision_requested else 0,
        )
        if render_failure_count:
            vision_stats["preparation_failed_count"] = render_failure_count
            message = (
                f"{render_failure_count} 个复杂页候选未生成整页图片，"
                "已保留 MinerU 与 PDF 原生文本结果。"
            )
            warnings.append(message)
            if strict_vision and vision_requested:
                raise RuntimeError(message)
        if vision_requested and candidates:
            try:
                service = self.image_analysis_service or DocumentImageAnalysisService(
                    settings=self.settings
                )
                enrichment = service.analyze_sync(
                    text=text,
                    blocks=blocks,
                    images=candidates,
                    source_id=source.name,
                )
            except Exception as exc:  # noqa: BLE001 - default is a safe fallback
                if strict_vision:
                    raise
                vision_stats["failed_count"] = len(candidates)
                warnings.append(
                    "复杂页视觉分析失败，已保留 MinerU 与 PDF 原生文本结果: "
                    f"{self._safe_error(exc)}"
                )
            else:
                failed_count = int(enrichment.stats.get("failed_count") or 0)
                preparation_failed_count = int(
                    enrichment.stats.get("preparation_failed_count") or 0
                )
                if strict_vision and (failed_count or preparation_failed_count):
                    raise RuntimeError(
                        "复杂页视觉分析未完整成功："
                        f"模型失败 {failed_count} 页，准备失败 {preparation_failed_count} 页。"
                    )
                blocks = [dict(block) for block in enrichment.blocks]
                images = self._merge_image_analysis(images, enrichment.images)
                tables = self._merge_visual_tables(tables, blocks)
                vision_stats = dict(enrichment.stats)
                vision_stats["candidate_count"] = selected_candidate_count
                vision_stats["preparation_failed_count"] = int(
                    vision_stats.get("preparation_failed_count") or 0
                ) + render_failure_count
                warnings.extend(enrichment.warnings)
                text = self._append_visual_analysis_markdown(text, blocks)

        metadata.update(
            {
                "structured_blocks": blocks,
                "blocks": blocks,
                "tables": tables,
                "images": images,
                "document_vision": vision_stats,
                "document_vision_candidate_count": len(candidates),
                "document_vision_warnings": warnings,
                "preprocessing_mode": "mineru_native_layout_selective_vision",
            }
        )
        resolved_document_id = document_id or self._document_id(source)
        return ParsedDocument(
            document_id=resolved_document_id,
            filename=source.name,
            text=text,
            pages=pages,
            blocks=blocks,
            tables=tables,
            images=images,
            metadata=metadata,
        )

    @staticmethod
    def _document_id(path: Path) -> str:
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
        return f"visual-document-{digest}"

    @classmethod
    def _merge_image_analysis(
        cls,
        images: list[dict[str, Any]],
        analyzed: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_ref = {
            cls._normalize_ref(item.get("image_ref") or item.get("relative_path")): item
            for item in analyzed
        }
        output: list[dict[str, Any]] = []
        for image in images:
            item = dict(image)
            ref = cls._normalize_ref(item.get("image_ref") or item.get("relative_path"))
            analyzed_item = by_ref.get(ref)
            if analyzed_item and isinstance(analyzed_item.get("analysis"), dict):
                item["analysis"] = dict(analyzed_item["analysis"])
            output.append(item)
        return output

    @staticmethod
    def _normalize_ref(value: Any) -> str:
        return str(value or "").replace("\\", "/").lstrip("./")

    @classmethod
    def _merge_visual_tables(
        cls,
        existing: list[dict[str, Any]],
        blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        output = [dict(table) for table in existing]
        signatures = {
            cls._visual_table_signature(table)
            for table in output
            if cls._visual_table_signature(table)
        }
        for block in blocks:
            if block.get("type") != "image_description":
                continue
            structure = block.get("visual_structure")
            if not isinstance(structure, dict):
                continue
            for table in structure.get("tables") or []:
                if not isinstance(table, dict):
                    continue
                headers = [str(value).strip() for value in table.get("headers") or []]
                rows = [
                    [str(value).strip() for value in row]
                    for row in table.get("rows") or []
                    if isinstance(row, list)
                ]
                title = str(table.get("title") or "").strip()
                lines = [title] if title else []
                if headers:
                    lines.append(" | ".join(headers))
                lines.extend(" | ".join(row) for row in rows if any(row))
                item = {
                    "type": "visual_table",
                    "text": "\n".join(line for line in lines if line).strip(),
                    "title": title,
                    "headers": headers,
                    "rows": rows,
                    "page": block.get("page"),
                    "image_ref": block.get("image_ref"),
                    "source": "document_vision",
                    "evidence": str(table.get("evidence") or "").strip(),
                    "uncertain": bool(table.get("uncertain")),
                }
                signature = cls._visual_table_signature(item)
                if not signature or signature in signatures:
                    continue
                signatures.add(signature)
                output.append(item)
        return output

    @staticmethod
    def _visual_table_signature(table: dict[str, Any]) -> str:
        text = str(table.get("text") or "").strip()
        if not text:
            title = str(table.get("title") or "").strip()
            headers = " | ".join(str(value) for value in table.get("headers") or [])
            rows = "\n".join(
                " | ".join(str(value) for value in row)
                for row in table.get("rows") or []
                if isinstance(row, list)
            )
            text = "\n".join(value for value in (title, headers, rows) if value)
        return "".join(character.casefold() for character in text if character.isalnum())

    @staticmethod
    def _append_visual_analysis_markdown(
        markdown_text: str,
        blocks: list[dict[str, Any]],
    ) -> str:
        marker = "<!-- document-vision-layout-relations:v2 -->"
        if marker in markdown_text:
            return markdown_text
        descriptions = [
            block
            for block in blocks
            if block.get("type") == "image_description"
            and str(block.get("text") or "").strip()
        ]
        if not descriptions:
            return markdown_text
        by_page: dict[int | None, list[str]] = {}
        for block in descriptions:
            try:
                page: int | None = int(block.get("page"))
            except (TypeError, ValueError):
                page = None
            text = str(block.get("text") or "").strip()
            if text not in by_page.setdefault(page, []):
                by_page[page].append(text)

        sections = [marker, "## 文档视觉关系补充"]
        for page, values in sorted(
            by_page.items(),
            key=lambda item: (item[0] is None, item[0] if item[0] is not None else 0),
        ):
            if page is not None:
                sections.append(f"### 第 {page + 1} 页")
            sections.append("\n\n".join(values))
        return markdown_text.rstrip() + "\n\n" + "\n\n".join(sections) + "\n"

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        message = " ".join(str(exc).split())
        return message[-500:] or exc.__class__.__name__

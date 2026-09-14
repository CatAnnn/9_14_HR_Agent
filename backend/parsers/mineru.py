from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import re
import shutil
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from backend.config.settings import Settings, get_settings
from backend.exceptions.parser_errors import ParserError


def normalize_space(value: Any) -> str:
    """Normalize parser text fields into compact single-space text."""
    return re.sub(r"\s+", " ", str(value)).strip()


class _HTMLTableTextExtractor(HTMLParser):
    """Turn MinerU's HTML table body into searchable row-oriented text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        tag = tag.lower()
        if tag == "tr":
            self._finish_row()
            self._row = []
        elif tag in {"td", "th"}:
            self._finish_cell()
            if self._row is None:
                self._row = []
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"}:
            self._finish_cell()
        elif tag == "tr":
            self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:
        super().close()
        self._finish_cell()
        self._finish_row()

    def _finish_cell(self) -> None:
        if self._cell is None:
            return
        if self._row is None:
            self._row = []
        self._row.append(normalize_space("".join(self._cell)))
        self._cell = None

    def _finish_row(self) -> None:
        self._finish_cell()
        if self._row is not None and any(self._row):
            self.rows.append(self._row)
        self._row = None

    def as_text(self) -> str:
        return "\n".join(" | ".join(row) for row in self.rows)


class MinerUParser:
    """MinerU HTTP API adapter for the official Docker deployment.

    The backend no longer embeds MinerU, torch, or vLLM. It uploads files to the
    dedicated MinerU API service and stores returned artifacts under runtime data
    so the existing chunking and metadata path can continue to work.
    """

    cache_schema_version = "mineru-3.4.5-adaptive-layout-v4"

    def __init__(self, *, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.last_metadata: dict[str, Any] = {}

    def parse(
        self,
        path: Path,
        *,
        prepare_visual_pages: bool | None = None,
    ) -> str:
        self.last_metadata = {"parser": "mineru", "input_path": str(path)}
        if not self.settings.mineru_enabled:
            raise ParserError("MINERU_ENABLED=false；当前项目要求 MinerU 必须启用。")
        if not path.exists():
            raise ParserError(f"待解析文件不存在: {path}")

        started_at = time.perf_counter()
        content_hash = self._file_digest(path)
        visual_page_preparation_enabled = (
            bool(self.settings.document_vision_enabled)
            if prepare_visual_pages is None
            else bool(prepare_visual_pages)
        )
        output_dir = self._make_output_dir(
            path,
            content_hash=content_hash,
            prepare_visual_pages=visual_page_preparation_enabled,
        )
        markdown_path = output_dir / f"{path.stem}.md"
        manifest_path = self._completion_manifest_path(output_dir, path.stem)
        image_analysis_enabled = self._image_analysis_enabled()
        self.last_metadata.update({
            "status": "running",
            "api_url": self.settings.mineru_api_url,
            "backend": self.settings.mineru_backend,
            "effort": self.settings.mineru_effort,
            "parse_method": self.settings.mineru_parse_method,
            "image_analysis_enabled": image_analysis_enabled,
            "visual_page_preparation_enabled": visual_page_preparation_enabled,
            "content_hash": content_hash,
            "output_dir": str(output_dir),
        })

        cached_text = self._cached_markdown(markdown_path)
        cache_lookup_elapsed = time.perf_counter() - started_at
        cache_complete, cache_invalid_reason = self._cache_complete(
            manifest_path=manifest_path,
            output_dir=output_dir,
            content_hash=content_hash,
            prepare_visual_pages=visual_page_preparation_enabled,
        )
        if (
            cached_text
            and cache_complete
            and self._cached_images_complete(output_dir, cached_text)
        ):
            collect_started_at = time.perf_counter()
            artifacts = self._collect_artifacts(
                output_dir=output_dir,
                markdown_path=markdown_path,
                markdown_text=cached_text,
            )
            collect_elapsed = time.perf_counter() - collect_started_at
            elapsed = time.perf_counter() - started_at
            self.last_metadata.update({
                "status": "cached",
                "cache_hit": True,
                "cache_lookup_elapsed_seconds": round(cache_lookup_elapsed, 3),
                "artifact_collect_elapsed_seconds": round(collect_elapsed, 3),
                "elapsed_seconds": round(elapsed, 3),
                "markdown_path": str(markdown_path),
                "markdown_chars": len(cached_text),
                "result_key": "cache",
                **artifacts,
            })
            return cached_text
        if cached_text:
            self.last_metadata["cache_invalid_reason"] = (
                cache_invalid_reason
                if not cache_complete
                else "Markdown 引用的本地图片产物缺失，重新请求 MinerU。"
            )
        self._invalidate_completion_manifest(manifest_path)
        self._remove_stale_api_json_artifacts(output_dir, path.stem)

        request_started_at = time.perf_counter()
        payload = self._request_parse(path)
        request_elapsed = time.perf_counter() - request_started_at
        text, result_key = self._extract_markdown(payload)
        layout_started_at = time.perf_counter()
        text, layout_metadata, layout_warnings = self._enrich_pdf_native_layout(
            path=path,
            output_dir=output_dir,
            source_stem=path.stem,
            payload=payload,
            result_key=result_key,
            markdown_text=text,
            prepare_visual_pages=visual_page_preparation_enabled,
        )
        layout_elapsed = time.perf_counter() - layout_started_at
        artifact_write_started_at = time.perf_counter()
        markdown_path.write_text(text, encoding="utf-8")
        artifact_warnings = self._write_api_artifacts(output_dir, path.stem, payload, result_key)
        artifact_warnings.extend(layout_warnings)
        artifact_write_elapsed = time.perf_counter() - artifact_write_started_at

        artifact_collect_started_at = time.perf_counter()
        artifacts = self._collect_artifacts(
            output_dir=output_dir,
            markdown_path=markdown_path,
            markdown_text=text,
        )
        selected_pages = {
            int(value)
            for value in layout_metadata.get("visual_candidate_pages") or []
            if self._is_int_like(value)
        }
        artifact_rendered_pages = {
            int(image.get("page"))
            for image in artifacts.get("images") or []
            if isinstance(image, dict)
            and (
                image.get("analysis_scope") == "full_page"
                or bool(image.get("full_page"))
            )
            and self._is_int_like(image.get("page"))
        }
        retryable_render_gap = bool(
            visual_page_preparation_enabled
            and not selected_pages.issubset(artifact_rendered_pages)
            and shutil.which("pdftoppm")
        )
        retryable_layout_failure = bool(
            layout_metadata.get("native_layout_retryable_failure")
        )
        if retryable_layout_failure:
            manifest_warning = (
                "PDF 原生布局校验本次未完成，结果不会标记为可复用缓存；"
                "下次处理会重新尝试。"
            )
        elif retryable_render_gap:
            manifest_warning = (
                "复杂页候选未全部渲染，本次结果不会标记为可复用缓存；"
                "下次处理会重新尝试。"
            )
        else:
            manifest_warning = self._write_completion_manifest(
                manifest_path=manifest_path,
                output_dir=output_dir,
                content_hash=content_hash,
                markdown_path=markdown_path,
                artifacts=artifacts,
                prepare_visual_pages=visual_page_preparation_enabled,
            )
        if manifest_warning:
            artifact_warnings.append(manifest_warning)
        else:
            artifact_paths = artifacts.setdefault("artifact_paths", [])
            manifest_value = str(manifest_path)
            if manifest_value not in artifact_paths:
                artifact_paths.append(manifest_value)
        artifact_collect_elapsed = time.perf_counter() - artifact_collect_started_at
        elapsed = time.perf_counter() - started_at
        self.last_metadata.update({
            "status": "success",
            "cache_hit": False,
            "cache_lookup_elapsed_seconds": round(cache_lookup_elapsed, 3),
            "request_elapsed_seconds": round(request_elapsed, 3),
            "native_layout_elapsed_seconds": round(layout_elapsed, 3),
            "artifact_write_elapsed_seconds": round(artifact_write_elapsed, 3),
            "artifact_collect_elapsed_seconds": round(artifact_collect_elapsed, 3),
            "elapsed_seconds": round(elapsed, 3),
            "markdown_path": str(markdown_path),
            "markdown_chars": len(text),
            "result_key": result_key,
            **artifacts,
            **layout_metadata,
        })
        if artifact_warnings:
            self.last_metadata["artifact_warnings"] = artifact_warnings
        return text

    def _enrich_pdf_native_layout(
        self,
        *,
        path: Path,
        output_dir: Path,
        source_stem: str,
        payload: dict[str, Any],
        result_key: str,
        markdown_text: str,
        prepare_visual_pages: bool,
    ) -> tuple[str, dict[str, Any], list[str]]:
        if path.suffix.lower() != ".pdf":
            return markdown_text, {}, []
        try:
            return self._enrich_pdf_native_layout_impl(
                path=path,
                output_dir=output_dir,
                source_stem=source_stem,
                payload=payload,
                result_key=result_key,
                markdown_text=markdown_text,
                prepare_visual_pages=prepare_visual_pages,
            )
        except Exception as exc:  # noqa: BLE001 - optional stage must be fail-open
            return markdown_text, {
                "native_layout_status": "failed",
                "native_layout_retryable_failure": bool(shutil.which("pdftotext")),
            }, [
                f"PDF 原生布局与复杂页准备失败，已保留 MinerU 结果: {exc}"
            ]

    def _enrich_pdf_native_layout_impl(
        self,
        *,
        path: Path,
        output_dir: Path,
        source_stem: str,
        payload: dict[str, Any],
        result_key: str,
        markdown_text: str,
        prepare_visual_pages: bool,
    ) -> tuple[str, dict[str, Any], list[str]]:
        """Recover exact PDF text and prepare only risky pages for vision.

        This deterministic stage is deliberately fail-open. MinerU remains the
        primary parser; native PDF text is added only where page-level coverage
        or layout checks find a material gap.
        """

        if path.suffix.lower() != ".pdf":
            return markdown_text, {}, []
        result = payload.get("results", {}).get(result_key)
        if not isinstance(result, dict):
            return markdown_text, {
                "native_layout_status": "failed",
                "native_layout_retryable_failure": True,
            }, ["MinerU 结果结构无效，已跳过 PDF 原生布局校验。"]
        raw_content = self._coerce_content_list(result.get("content_list"))
        if not raw_content:
            return markdown_text, {
                "native_layout_status": "failed",
                "native_layout_retryable_failure": True,
            }, ["MinerU 未返回 content_list，已保留原始 Markdown。"]

        try:
            from backend.parsers.pdf_native_layout import (
                build_pdf_native_layout_enrichment,
                render_pdf_pages,
            )

            enrichment = build_pdf_native_layout_enrichment(path, raw_content)
        except Exception as exc:  # noqa: BLE001 - parsing must remain fail-open
            return markdown_text, {
                "native_layout_status": "failed",
                "native_layout_retryable_failure": bool(shutil.which("pdftotext")),
            }, [f"PDF 原生布局校验失败，已保留 MinerU 结果: {exc}"]

        warnings = list(enrichment.warnings)
        native_layout_retryable_failure = self._native_layout_retryable_failure(
            enrichment.warnings
        )
        quality_report = enrichment.quality_report()
        raw_path = output_dir / f"{source_stem}_mineru_raw.json"
        quality_path = output_dir / f"{source_stem}_pdf_quality.json"
        try:
            raw_path.write_text(
                json.dumps(raw_content, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            quality_path.write_text(
                json.dumps(quality_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as exc:
            warnings.append(f"PDF 原生布局诊断产物写入失败，继续使用内存结果: {exc}")

        fallback_blocks = [
            {
                key: value
                for key, value in dict(block).items()
                if key != "layout_items"
            }
            for block in enrichment.fallback_blocks
        ]
        enriched_content = [dict(node) if isinstance(node, dict) else node for node in raw_content]
        enriched_content.extend(fallback_blocks)

        rendered_images: list[dict[str, Any]] = []
        if prepare_visual_pages and enrichment.visual_candidate_pages:
            rendered_pages: tuple[Any, ...] = ()
            try:
                render_result = render_pdf_pages(
                    path,
                    enrichment.visual_candidate_pages,
                    output_dir / "page_renders",
                    dpi=180,
                )
            except Exception as exc:  # noqa: BLE001 - optional vision is fail-open
                warnings.append(f"复杂页渲染失败，已保留文本解析结果: {exc}")
            else:
                warnings.extend(render_result.warnings)
                rendered_pages = render_result.pages
            native_text_by_page = {
                page.page_index: page.text for page in enrichment.native_pages
            }
            quality_by_page = {
                item.page_index: item for item in enrichment.page_quality
            }
            parser_text_by_page = self._content_text_by_page(raw_content)
            for page in rendered_pages:
                try:
                    relative_path = page.image_path.relative_to(output_dir).as_posix()
                except ValueError:
                    warnings.append(
                        f"第 {page.page_index + 1} 页渲染产物不在解析目录内，已跳过。"
                    )
                    continue
                quality = quality_by_page.get(page.page_index)
                image_node = {
                    "type": "image",
                    "img_path": relative_path,
                    "page_idx": page.page_index,
                    "bbox": [0, 0, 1000, 1000],
                    "full_page": True,
                    "analysis_scope": "full_page",
                    "render_profile": f"pdf-page-{page.dpi}dpi",
                    "render_dpi": page.dpi,
                    "native_page_text": native_text_by_page.get(page.page_index, ""),
                    "parser_page_text": parser_text_by_page.get(page.page_index, ""),
                    "quality_severity": quality.severity if quality else "unreadable",
                    "structure_risk": bool(quality and quality.structure_risk),
                    "analysis_reasons": list(quality.reasons) if quality else [],
                }
                enriched_content.append(image_node)
                rendered_images.append(image_node)

        result["content_list"] = enriched_content
        enriched_markdown = self._append_native_fallback_markdown(
            markdown_text,
            fallback_blocks,
        )
        metadata = {
            "native_layout_status": (
                "failed" if native_layout_retryable_failure else "success"
            ),
            "native_layout_retryable_failure": native_layout_retryable_failure,
            "pdf_native_layout": quality_report,
            "native_layout_fallback_count": len(fallback_blocks),
            "visual_candidate_pages": list(enrichment.visual_candidate_pages),
            "visual_page_render_count": len(rendered_images),
            "visual_rendered_pages": [
                int(image["page_idx"])
                for image in rendered_images
                if self._is_int_like(image.get("page_idx"))
            ],
            "pdf_quality_path": str(quality_path),
            "mineru_raw_content_path": str(raw_path),
        }
        return enriched_markdown, metadata, warnings

    @staticmethod
    def _native_layout_retryable_failure(warnings: Any) -> bool:
        retryable_markers = (
            "原生布局提取失败",
            "原生布局提取超时",
            "原生布局解析失败",
        )
        return any(
            any(marker in str(warning) for marker in retryable_markers)
            for warning in warnings or []
        )

    @staticmethod
    def _is_int_like(value: Any) -> bool:
        try:
            int(value)
        except (TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _coerce_content_list(value: Any) -> list[Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return []
        return list(value) if isinstance(value, list) else []

    def _content_text_by_page(self, content: list[Any]) -> dict[int, str]:
        blocks = self._extract_blocks_from_json(content, Path("mineru-response.json"))
        grouped: dict[int, list[str]] = {}
        for block in blocks:
            page = block.get("page")
            text = str(block.get("text") or "").strip()
            try:
                page_index = int(page)
            except (TypeError, ValueError):
                continue
            if text:
                grouped.setdefault(page_index, []).append(text)
        return {
            page: "\n".join(dict.fromkeys(values))
            for page, values in grouped.items()
        }

    @staticmethod
    def _append_native_fallback_markdown(
        markdown_text: str,
        fallback_blocks: list[dict[str, Any]],
    ) -> str:
        marker = "<!-- pdf-native-layout-fallback:v1 -->"
        if not fallback_blocks or marker in markdown_text:
            return markdown_text
        by_page: dict[int, list[str]] = {}
        for block in fallback_blocks:
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            try:
                page = int(block.get("page"))
            except (TypeError, ValueError):
                continue
            if text not in by_page.setdefault(page, []):
                by_page[page].append(text)
        if not by_page:
            return markdown_text
        sections = [marker, "## PDF 原生布局补充"]
        for page, values in sorted(by_page.items()):
            sections.extend([f"### 第 {page + 1} 页", "\n\n".join(values)])
        return markdown_text.rstrip() + "\n\n" + "\n\n".join(sections) + "\n"

    def _request_parse(self, path: Path) -> dict[str, Any]:
        url = self.settings.mineru_api_url.rstrip("/") + "/file_parse"
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form_data = {
            "lang_list": self.settings.mineru_lang,
            "backend": self.settings.mineru_backend,
            "effort": self.settings.mineru_effort,
            "parse_method": self.settings.mineru_parse_method,
            "formula_enable": "true",
            "table_enable": "true",
            # MinerU medium is the fast daily-document path and does not support
            # image analysis. High keeps the previous maximum-quality behavior.
            "image_analysis": "true" if self._image_analysis_enabled() else "false",
            "return_md": "true",
            # content_list already provides the structure consumed below. The
            # much larger middle_json is intentionally omitted from the normal
            # response to reduce serialization, transfer, and repeated JSON IO.
            "return_middle_json": "false",
            "return_model_output": "false",
            "return_content_list": "true",
            "return_images": "true",
            "response_format_zip": "false",
            "return_original_file": "false",
            "client_side_output_generation": "false",
            "start_page_id": "0",
            "end_page_id": "99999",
        }
        timeout = httpx.Timeout(self.settings.mineru_timeout_seconds)
        try:
            with path.open("rb") as handle:
                files = [("files", (path.name, handle, mime_type))]
                response = httpx.post(url, data=form_data, files=files, timeout=timeout)
        except httpx.TimeoutException as exc:
            message = f"MinerU API 解析超时: {path.name}"
            self.last_metadata.update({"status": "timeout", "error": message})
            raise ParserError(message) from exc
        except httpx.HTTPError as exc:
            message = f"MinerU API 请求失败: {exc}"
            self.last_metadata.update({"status": "http_error", "error": message})
            raise ParserError(message) from exc

        self.last_metadata.update({
            "http_status": response.status_code,
            "http_response_bytes": len(response.content),
        })
        if response.status_code >= 400:
            body = response.text[-1000:]
            message = f"MinerU API 解析失败，status={response.status_code}: {body}"
            self.last_metadata.update({"status": "failed", "error": message})
            raise ParserError(message)
        try:
            payload = response.json()
        except ValueError as exc:
            message = "MinerU API 返回非 JSON 响应。"
            self.last_metadata.update({"status": "invalid_json", "error": message, "body": response.text[-1000:]})
            raise ParserError(message) from exc
        return payload

    @staticmethod
    def _extract_markdown(payload: dict[str, Any]) -> tuple[str, str]:
        results = payload.get("results")
        if not isinstance(results, dict) or not results:
            raise ParserError("MinerU API 未返回 results。")
        for key, value in results.items():
            if isinstance(value, dict):
                text = str(value.get("md_content") or "").strip()
                if text:
                    return text, str(key)
        raise ParserError("MinerU API 未返回 Markdown 内容。")

    @classmethod
    def _write_api_artifacts(
        cls,
        output_dir: Path,
        source_stem: str,
        payload: dict[str, Any],
        result_key: str,
    ) -> list[str]:
        warnings: list[str] = []
        result = payload.get("results", {}).get(result_key, {})
        if not isinstance(result, dict):
            return warnings
        artifact_names = {
            "middle_json": f"{source_stem}_middle.json",
            "content_list": f"{source_stem}_content_list.json",
            "model_output": f"{source_stem}_model.json",
        }
        for field, filename in artifact_names.items():
            value = result.get(field)
            if value is None:
                continue
            target = output_dir / filename
            if isinstance(value, str):
                target.write_text(value, encoding="utf-8")
            else:
                target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

        images = result.get("images")
        if isinstance(images, dict) and images:
            image_dir = output_dir / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            for source_name, data_uri in images.items():
                try:
                    mime_type, content = cls._decode_image_data_uri(data_uri)
                    filename = cls._safe_image_filename(source_name, mime_type, content)
                    (image_dir / filename).write_bytes(content)
                except (ValueError, binascii.Error) as exc:
                    safe_source = Path(str(source_name).replace("\\", "/")).name or "unnamed"
                    warnings.append(f"图片产物 {safe_source} 解码失败: {exc}")
        return warnings

    @staticmethod
    def _decode_image_data_uri(value: Any) -> tuple[str, bytes]:
        if not isinstance(value, str):
            raise ValueError("响应值不是 Data URL")
        match = re.fullmatch(
            r"data:(image/(?:png|jpeg|jpg|webp|gif));base64,(.+)",
            value.strip(),
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            raise ValueError("响应值不是受支持的图片 Data URL")
        content = base64.b64decode(match.group(2), validate=True)
        if not content:
            raise ValueError("图片内容为空")
        return match.group(1).lower(), content

    @staticmethod
    def _safe_image_filename(source_name: Any, mime_type: str, content: bytes) -> str:
        raw_name = Path(str(source_name).replace("\\", "/")).name
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._")
        extension_by_mime = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        expected_suffix = extension_by_mime[mime_type]
        suffix = Path(sanitized).suffix.lower()
        compatible_suffixes = {expected_suffix, ".jpeg"} if expected_suffix == ".jpg" else {expected_suffix}
        if suffix not in compatible_suffixes:
            stem = Path(sanitized).stem or hashlib.sha256(content).hexdigest()[:20]
            sanitized = f"{stem}{expected_suffix}"
        return sanitized

    def _make_output_dir(
        self,
        path: Path,
        content_hash: str | None = None,
        *,
        prepare_visual_pages: bool | None = None,
    ) -> Path:
        digest = content_hash or self._file_digest(path)
        profile_digest = self._parse_profile_digest(
            prepare_visual_pages=prepare_visual_pages
        )
        output_dir = (
            self.settings.resolved_mineru_output_dir
            / self._kb_raw_relative_parent(path)
            / f"{path.stem}_{digest}_{profile_digest}"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _parse_profile_digest(
        self,
        *,
        prepare_visual_pages: bool | None = None,
    ) -> str:
        visual_page_preparation_enabled = (
            bool(self.settings.document_vision_enabled)
            if prepare_visual_pages is None
            else bool(prepare_visual_pages)
        )
        profile = {
            "cache_schema_version": self.cache_schema_version,
            "backend": self.settings.mineru_backend,
            "effort": self.settings.mineru_effort,
            "parse_method": self.settings.mineru_parse_method,
            "lang": self.settings.mineru_lang,
            "formula_enable": True,
            "table_enable": True,
            "image_analysis": self._image_analysis_enabled(),
            "return_middle_json": False,
            "return_content_list": True,
            "return_images": True,
            "native_layout_schema": "pdf-native-layout-v1",
            "native_layout_thresholds": (
                "80:0.65:20:0.88:0.45:large-visual:ocr-only:"
                "explicit-relations:top3:10pct"
            ),
            "native_layout_tools": {
                "pdftotext": bool(shutil.which("pdftotext")),
                "pdftoppm": bool(shutil.which("pdftoppm")),
            },
            "full_page_render": visual_page_preparation_enabled,
            "full_page_render_dpi": 180,
        }
        serialized = json.dumps(profile, ensure_ascii=True, sort_keys=True).encode("utf-8")
        return hashlib.sha1(serialized).hexdigest()[:10]

    def _image_analysis_enabled(self) -> bool:
        # effort only controls Hybrid parsing. Preserve image analysis for VLM
        # and other backends; Hybrid medium explicitly does not support it.
        hybrid_backend = self.settings.mineru_backend in {
            "hybrid-engine",
            "hybrid-http-client",
        }
        return not (hybrid_backend and self.settings.mineru_effort == "medium")

    def _kb_raw_relative_parent(self, path: Path) -> Path:
        raw_root = (self.settings.data_dir / "kb_raw").resolve()
        try:
            return path.resolve().parent.relative_to(raw_root)
        except ValueError:
            return Path()

    @staticmethod
    def _file_digest(path: Path) -> str:
        hasher = hashlib.sha1()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()[:10]

    @staticmethod
    def _cached_markdown(markdown_path: Path) -> str:
        if not markdown_path.exists() or not markdown_path.is_file():
            return ""
        return markdown_path.read_text(encoding="utf-8", errors="ignore").strip()

    @staticmethod
    def _completion_manifest_path(output_dir: Path, source_stem: str) -> Path:
        return output_dir / f"{source_stem}_parse_complete.json"

    def _cache_complete(
        self,
        *,
        manifest_path: Path,
        output_dir: Path,
        content_hash: str,
        prepare_visual_pages: bool | None = None,
    ) -> tuple[bool, str]:
        if not manifest_path.is_file():
            return False, "解析完成清单缺失，重新请求 MinerU。"
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8", errors="strict")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, "解析完成清单损坏，重新请求 MinerU。"
        if not isinstance(manifest, dict) or manifest.get("status") != "complete":
            return False, "解析完成清单状态无效，重新请求 MinerU。"
        if manifest.get("cache_schema_version") != self.cache_schema_version:
            return False, "解析缓存协议已变化，重新请求 MinerU。"
        if manifest.get("content_hash") != content_hash:
            return False, "解析缓存对应的源文件已变化，重新请求 MinerU。"
        if manifest.get("parse_profile_digest") != self._parse_profile_digest(
            prepare_visual_pages=prepare_visual_pages
        ):
            return False, "解析配置已变化，重新请求 MinerU。"

        artifact_paths = manifest.get("artifacts")
        if not isinstance(artifact_paths, list) or not artifact_paths:
            return False, "解析完成清单没有产物记录，重新请求 MinerU。"
        root = output_dir.resolve()
        for value in artifact_paths:
            if not isinstance(value, str) or not value.strip():
                return False, "解析完成清单包含无效产物路径，重新请求 MinerU。"
            candidate = output_dir / value
            try:
                candidate.resolve().relative_to(root)
            except ValueError:
                return False, "解析完成清单包含越界产物路径，重新请求 MinerU。"
            if not candidate.is_file():
                return False, "解析缓存产物不完整，重新请求 MinerU。"
        return True, ""

    def _write_completion_manifest(
        self,
        *,
        manifest_path: Path,
        output_dir: Path,
        content_hash: str,
        markdown_path: Path,
        artifacts: dict[str, Any],
        prepare_visual_pages: bool | None = None,
    ) -> str | None:
        root = output_dir.resolve()
        candidates = [markdown_path]
        candidates.extend(
            Path(value)
            for value in artifacts.get("artifact_paths") or []
            if isinstance(value, str) and value.strip()
        )
        relative_paths: list[str] = []
        for candidate in candidates:
            try:
                relative = candidate.resolve().relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                value = relative.as_posix()
                if value not in relative_paths:
                    relative_paths.append(value)
        manifest = {
            "status": "complete",
            "cache_schema_version": self.cache_schema_version,
            "content_hash": content_hash,
            "parse_profile_digest": self._parse_profile_digest(
                prepare_visual_pages=prepare_visual_pages
            ),
            "artifacts": relative_paths,
        }
        temporary_path: Path | None = None
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=manifest_path.parent,
                prefix=f".{manifest_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.flush()
                temporary_path = Path(handle.name)
            temporary_path.replace(manifest_path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            return f"解析完成清单写入失败，本次结果不会复用缓存: {exc}"
        return None

    @staticmethod
    def _invalidate_completion_manifest(manifest_path: Path) -> None:
        try:
            manifest_path.unlink(missing_ok=True)
        except OSError:
            # A later atomic manifest write still decides whether this cache is
            # reusable; inability to remove an old read-only file will surface
            # there without discarding the current parse result.
            pass

    @staticmethod
    def _remove_stale_api_json_artifacts(output_dir: Path, source_stem: str) -> None:
        for suffix in (
            "_middle.json",
            "_content_list.json",
            "_model.json",
            "_mineru_raw.json",
            "_pdf_quality.json",
        ):
            try:
                (output_dir / f"{source_stem}{suffix}").unlink(missing_ok=True)
            except OSError:
                # The next artifact write will either replace the file or report
                # the underlying filesystem failure through the normal parser.
                continue

    @classmethod
    def _cached_images_complete(cls, output_dir: Path, markdown_text: str) -> bool:
        references = re.findall(r"!\[[^\]]*\]\(\s*<?([^)>\s]+)>?", markdown_text)
        for raw_reference in references:
            reference = raw_reference.split("?", 1)[0].split("#", 1)[0].strip()
            if not reference or reference.startswith(("data:", "http://", "https://")):
                continue
            normalized = reference.replace("\\", "/").lstrip("./")
            candidates = [output_dir / normalized, output_dir / "images" / Path(normalized).name]
            if not any(cls._is_safe_cached_image(output_dir, candidate) for candidate in candidates):
                return False
        return True

    @staticmethod
    def _is_safe_cached_image(output_dir: Path, candidate: Path) -> bool:
        try:
            candidate.resolve().relative_to(output_dir.resolve())
        except ValueError:
            return False
        return candidate.is_file()

    def _collect_artifacts(self, output_dir: Path, markdown_path: Path, markdown_text: str) -> dict[str, Any]:
        json_paths = [p for p in output_dir.rglob("*.json") if p.is_file()]
        content_list_paths = [
            path for path in json_paths if "content_list" in path.stem.lower()
        ]
        structured_json_paths = content_list_paths or json_paths
        json_documents: list[tuple[Path, Any]] = []
        for json_path in structured_json_paths:
            try:
                data = json.loads(json_path.read_text(encoding="utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue
            json_documents.append((json_path, data))

        # Each JSON artifact is decoded once. Previously content_list was read
        # once for image metadata and again for blocks, which was costly for
        # large documents.
        image_metadata = self._content_list_image_metadata(json_documents)
        blocks: list[dict[str, Any]] = []
        pages: dict[str, dict[str, Any]] = {}
        for json_path, data in json_documents:
            extracted = self._extract_blocks_from_json(data, json_path)
            for block in extracted:
                blocks.append(block)
                page = block.get("page")
                if page is not None:
                    pages[str(page)] = {"page": page}

        if not blocks:
            blocks = self._blocks_from_markdown(markdown_text)
        tables = [block for block in blocks if str(block.get("type", "")).lower() in {"table", "table_body"}]
        tables.extend(self._markdown_table_blocks(markdown_text))
        tables = self._dedupe_table_blocks(tables)

        images: list[dict[str, Any]] = []
        markdown_image_refs = self._markdown_image_references(markdown_text)
        markdown_image_names = {Path(value).name for value in markdown_image_refs}
        for path in sorted(output_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                continue
            relative_path = path.relative_to(output_dir).as_posix()
            metadata = image_metadata.get(relative_path) or image_metadata.get(path.name) or {}
            if (
                not metadata
                and relative_path not in markdown_image_refs
                and path.name not in markdown_image_names
            ):
                continue
            page = metadata.get("page") if "page" in metadata else self._page_from_path(path)
            images.append({
                "path": str(path),
                "relative_path": relative_path,
                "image_ref": metadata.get("image_ref") or relative_path,
                "type": "image",
                "page": page,
                **{
                    key: metadata[key]
                    for key in (
                        "full_page",
                        "analysis_scope",
                        "render_profile",
                        "render_dpi",
                        "native_page_text",
                        "parser_page_text",
                        "quality_severity",
                        "structure_risk",
                        "analysis_reasons",
                    )
                    if metadata.get(key) not in (None, "", False, [])
                },
            })
        for image in images:
            page = image.get("page")
            if page is not None:
                pages[str(page)] = {"page": page}

        quality_report: dict[str, Any] = {}
        for quality_path in json_paths:
            if not quality_path.stem.endswith("_pdf_quality"):
                continue
            try:
                value = json.loads(
                    quality_path.read_text(encoding="utf-8", errors="ignore")
                )
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                quality_report = value
                break
        fallback_count = sum(
            1 for block in blocks if block.get("type") == "native_layout_fallback"
        )
        visual_page_images = [
            image for image in images if image.get("analysis_scope") == "full_page"
        ]
        artifact_paths = [str(markdown_path)] + [str(p) for p in json_paths] + [img["path"] for img in images]
        return {
            "pages": list(pages.values()),
            "page_count": len(pages),
            "structured_blocks": blocks,
            "block_count": len(blocks),
            "tables": tables,
            "table_count": len(tables),
            "images": images,
            "image_count": len(images),
            "pdf_native_layout": quality_report,
            "native_layout_fallback_count": fallback_count,
            "visual_candidate_pages": list(
                quality_report.get("visual_candidate_pages") or []
            ),
            "visual_page_render_count": len(visual_page_images),
            "artifact_paths": artifact_paths,
        }

    @classmethod
    def _content_list_image_metadata(
        cls,
        json_documents: list[tuple[Path, Any]],
    ) -> dict[str, dict[str, Any]]:
        metadata: dict[str, dict[str, Any]] = {}

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return
            image_ref = node.get("img_path") or node.get("image_path")
            if image_ref:
                normalized_ref = str(image_ref).replace("\\", "/").lstrip("./")
                page = cls._first_not_none(
                    node.get("page"),
                    node.get("page_idx"),
                    node.get("page_num"),
                    node.get("page_no"),
                )
                item = {
                    "image_ref": normalized_ref,
                    "page": page,
                    "full_page": bool(node.get("full_page")),
                    "analysis_scope": node.get("analysis_scope"),
                    "render_profile": node.get("render_profile"),
                    "render_dpi": node.get("render_dpi"),
                    "native_page_text": node.get("native_page_text"),
                    "parser_page_text": node.get("parser_page_text"),
                    "quality_severity": node.get("quality_severity"),
                    "structure_risk": node.get("structure_risk"),
                    "analysis_reasons": node.get("analysis_reasons"),
                }
                metadata[normalized_ref] = item
                metadata[Path(normalized_ref).name] = item
                metadata[f"images/{Path(normalized_ref).name}"] = item
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

        for json_path, data in json_documents:
            if "content_list" not in json_path.stem.lower():
                continue
            walk(data)
        return metadata

    @staticmethod
    def _markdown_image_references(markdown_text: str) -> set[str]:
        output: set[str] = set()
        for raw_reference in re.findall(
            r"!\[[^\]]*\]\(\s*<?([^)>\s]+)>?",
            markdown_text,
        ):
            reference = raw_reference.split("?", 1)[0].split("#", 1)[0].strip()
            if not reference or reference.startswith(("data:", "http://", "https://")):
                continue
            output.add(reference.replace("\\", "/").lstrip("./"))
        return output

    def _extract_blocks_from_json(self, data: Any, json_path: Path) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        normal_block_count = 0
        normal_block_limit = 5000

        def walk(node: Any) -> None:
            nonlocal normal_block_count
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return
            block_type = node.get("type") or node.get("block_type") or node.get("category") or node.get("layout_type")
            table_body = node.get("table_body")
            if table_body is not None and not block_type:
                block_type = "table"
            text = self._node_text(node)
            page = self._first_not_none(
                node.get("page"),
                node.get("page_idx"),
                node.get("page_num"),
                node.get("page_no"),
            )
            bbox = node.get("bbox") or node.get("poly") or node.get("box")
            image_ref = node.get("img_path") or node.get("image_path")
            critical_enrichment = (
                str(block_type or "").casefold() == "native_layout_fallback"
                or bool(node.get("full_page"))
                or str(node.get("analysis_scope") or "").casefold() == "full_page"
            )
            if (
                (block_type or text or bbox or image_ref)
                and (critical_enrichment or normal_block_count < normal_block_limit)
            ):
                if text is None:
                    normalized_text = None
                elif table_body is not None:
                    normalized_text = "\n".join(
                        normalized_line
                        for line in str(text).splitlines()
                        if (normalized_line := normalize_space(line))
                    )
                else:
                    normalized_text = normalize_space(text)
                blocks.append({
                    "type": str(block_type or "text"),
                    "text": normalized_text,
                    "page": page,
                    "bbox": bbox,
                    "image_ref": str(image_ref).replace("\\", "/").lstrip("./") if image_ref else None,
                    "source_json": str(json_path),
                })
                if not critical_enrichment:
                    normal_block_count += 1
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

        walk(data)
        return blocks

    @classmethod
    def _node_text(cls, node: dict[str, Any]) -> str | None:
        primary = cls._first_not_none(
            node.get("text"),
            node.get("content"),
            node.get("md"),
            node.get("html"),
        )
        table_body = node.get("table_body")
        if table_body is None:
            return str(primary) if primary is not None else None

        parts = [
            cls._auxiliary_text(node.get("table_caption")),
            cls._table_body_text(table_body),
            cls._auxiliary_text(node.get("table_footnote")),
        ]
        if primary is not None:
            parts.insert(0, normalize_space(primary))
        text = "\n".join(part for part in parts if part)
        return text or None

    @staticmethod
    def _table_body_text(value: Any) -> str:
        if isinstance(value, str):
            body = value.strip()
            if not body:
                return ""
            if "<" not in body or ">" not in body:
                return normalize_space(body)
            parser = _HTMLTableTextExtractor()
            try:
                parser.feed(body)
                parser.close()
            except (TypeError, ValueError):
                # Malformed HTML must not make the whole document fail. Even
                # the conservative tag-stripped fallback keeps table terms
                # available to downstream chunking and retrieval.
                return normalize_space(re.sub(r"<[^>]+>", " ", body))
            return parser.as_text() or normalize_space(re.sub(r"<[^>]+>", " ", body))
        if isinstance(value, list):
            rows: list[str] = []
            for row in value:
                if isinstance(row, (list, tuple)):
                    rows.append(" | ".join(normalize_space(cell) for cell in row))
                else:
                    text = MinerUParser._auxiliary_text(row)
                    if text:
                        rows.append(text)
            return "\n".join(rows)
        return MinerUParser._auxiliary_text(value)

    @staticmethod
    def _auxiliary_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return normalize_space(value)
        if isinstance(value, (list, tuple)):
            return "\n".join(
                text
                for item in value
                if (text := MinerUParser._auxiliary_text(item))
            )
        if isinstance(value, dict):
            preferred = MinerUParser._first_not_none(
                value.get("text"),
                value.get("content"),
                value.get("md"),
                value.get("html"),
            )
            if preferred is not None:
                return normalize_space(preferred)
            return "\n".join(
                text
                for item in value.values()
                if (text := MinerUParser._auxiliary_text(item))
            )
        return normalize_space(value)

    @staticmethod
    def _first_not_none(*values: Any) -> Any:
        return next((value for value in values if value is not None), None)

    @staticmethod
    def _blocks_from_markdown(markdown_text: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        current_section = None
        for idx, raw_line in enumerate(markdown_text.splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                current_section = line.lstrip("#").strip() or None
                block_type = "heading"
            elif line.startswith("!") and "](" in line:
                block_type = "image_reference"
            elif "|" in line and re.search(r"\|\s*-{3,}\s*\|", line):
                block_type = "table_separator"
            elif "|" in line:
                block_type = "table"
            else:
                block_type = "text"
            blocks.append({"type": block_type, "text": line, "page": None, "section": current_section, "line_index": idx})
        return blocks

    @staticmethod
    def _markdown_table_blocks(markdown_text: str) -> list[dict[str, Any]]:
        tables: list[dict[str, Any]] = []
        rows: list[str] = []
        for line in markdown_text.splitlines() + [""]:
            if "|" in line.strip():
                rows.append(line.strip())
                continue
            if len(rows) >= 2:
                tables.append({"type": "table", "text": "\n".join(rows), "page": None})
            rows = []
        return tables

    @staticmethod
    def _dedupe_table_blocks(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        signatures: dict[str, list[Any]] = {}
        for table in tables:
            text = normalize_space(table.get("text") or "")
            if not text:
                output.append(table)
                continue
            signature = "".join(token.casefold() for token in re.findall(r"\w+", text))
            page = table.get("page")
            existing_pages = signatures.setdefault(signature, [])
            if any(
                existing_page == page or existing_page is None or page is None
                for existing_page in existing_pages
            ):
                continue
            existing_pages.append(page)
            output.append(table)
        return output

    @staticmethod
    def _page_from_path(path: Path) -> int | None:
        vision_render = re.search(
            r"page[-_](\d+)[-_]vision[-_]\d+dpi$",
            path.stem,
            re.I,
        )
        if vision_render:
            return max(0, int(vision_render.group(1)) - 1)
        match = re.search(r"(?:page|p)[_-]?(\d+)", path.stem, re.I)
        if match:
            return int(match.group(1))
        return None

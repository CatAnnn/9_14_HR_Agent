from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.config.settings import Settings, get_settings
from backend.exceptions.llm_errors import StructuredOutputError
from backend.repositories.document_image_analysis_repository import (
    DocumentImageAnalysisRepository,
)
from backend.schemas.document_image import (
    DocumentImageAnalysis,
    DocumentImageAnalysisBatch,
    DocumentImageEnrichmentResult,
)
from backend.services.langchain_llm_service import LangChainLLMService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedImage:
    image_index: int
    image_ref: str
    path: Path
    page: Any
    sha256: str
    cache_key: str
    context: str
    analysis_scope: str
    render_profile: str
    quality_reasons: tuple[str, ...]
    requires_structure: bool


class DocumentImageAnalysisService:
    """Enrich MinerU image artifacts before KB chunking and embedding."""

    _ANALYSIS_SCHEMA_VERSION = "layout-relations-v3"
    _STRUCTURE_REQUIRED_REASONS = frozenset(
        {
            "explicit_relation_visual",
            "native_grid_without_structured_table",
        }
    )
    _RELATIONAL_LAYOUT_TYPES = frozenset(
        {
            "flowchart",
            "timeline",
            "hierarchy",
            "cycle",
            "process",
        }
    )
    _TABULAR_LAYOUT_TYPES = frozenset({"matrix", "table"})
    _RICH_LAYOUT_TYPES = frozenset({"comparison", "dashboard", "mixed"})

    _PREFLIGHT_IMAGE = (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAGUlEQVR4nGP8//8/"
        "AymAiSTVoxpGNQwpDQBVbQMdPVIhQwAAAABJRU5ErkJggg=="
    )
    _PROMPT = """
你是企业知识库文档视觉解析器。只输出当前图片中可验证的信息，不推测被遮挡、模糊或不存在的内容。
对每张图片：
1. 必须原样返回给定的 image_ref，并判断它是否包含可用于知识检索的信息。
2. description 给出简洁的整体解读；visible_text 按原语言、原数字记录清晰可辨的文字；key_facts 只记录有视觉依据的关键事实。
3. layout_type 识别页面或图片的主要结构。对流程图、时间线、矩阵、层级图、循环图、比较图、仪表盘及混合版式，不得只做概括性图片描述。
4. nodes 为清晰可识别的视觉对象或带标签区域分配本图内稳定 node_id。relations 只记录由箭头、连线、包含关系、明确空间编码或顺序标记直接支持的关系；source_id 和 target_id 必须引用 nodes 中的 node_id。不得仅因两个对象相邻就推断关系。
5. ordered_steps 仅在图片明确表达步骤、时间或顺序时填写，并保留原始顺序；没有明确顺序时返回空列表。
6. tables 仅在行列边界或对齐关系清楚时重建。无法辨认的单元格留空，不猜测内容；一张图片可包含多个表格。
7. evidence 记录支持整体判断的可见文字、符号或版式锚点。每个节点、关系、步骤和表格也应尽量填写对应 evidence；存在歧义时设置 uncertain=true，并在 uncertainties 中说明，不能确定时宁可不输出关系或顺序。
8. 提供的附近正文、原生页面文本和解析器页面文本只用于消歧与核对，不是图片内容的证据，也不得覆盖图片中清晰可见的原文。
9. 装饰图、纯图标、空白图或无法辨认的图片设为 is_informative=false，并将结构化字段留空。
不要在结果中包含 Base64、密钥、文件绝对路径或未提供的公司信息。
""".strip()

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        llm_service: LangChainLLMService | None = None,
        repository: DocumentImageAnalysisRepository | None = None,
    ):
        self.settings = settings or get_settings()
        self.llm = llm_service or LangChainLLMService(settings=self.settings)
        self._repository = repository
        self._preflight_complete = False
        self._preflight_error: str | None = None

    @property
    def repository(self) -> DocumentImageAnalysisRepository:
        if self._repository is None:
            self._repository = DocumentImageAnalysisRepository()
        return self._repository

    @staticmethod
    def empty_stats(*, enabled: bool = False, candidate_count: int = 0) -> dict[str, Any]:
        return {
            "enabled": enabled,
            "candidate_count": candidate_count,
            "analysis_count": 0,
            "cache_hit_count": 0,
            "description_count": 0,
            "duplicate_count": 0,
            "skipped_count": 0,
            "preparation_failed_count": 0,
            "failed_count": 0,
            "elapsed_seconds": 0.0,
        }

    def analyze_sync(
        self,
        *,
        text: str,
        blocks: list[dict[str, Any]],
        images: list[dict[str, Any]],
        source_id: str,
    ) -> DocumentImageEnrichmentResult:
        kwargs = {"text": text, "blocks": blocks, "images": images, "source_id": source_id}
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.analyze(**kwargs))
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-vision") as executor:
            return executor.submit(lambda: asyncio.run(self.analyze(**kwargs))).result()

    def preflight_real_image_sync(
        self,
        *,
        text: str,
        blocks: list[dict[str, Any]],
        image: dict[str, Any],
        source_id: str,
    ) -> dict[str, Any]:
        kwargs = {"text": text, "blocks": blocks, "image": image, "source_id": source_id}
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.preflight_real_image(**kwargs))
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-vision") as executor:
            return executor.submit(
                lambda: asyncio.run(self.preflight_real_image(**kwargs))
            ).result()

    async def preflight_real_image(
        self,
        *,
        text: str,
        blocks: list[dict[str, Any]],
        image: dict[str, Any],
        source_id: str,
    ) -> dict[str, Any]:
        if not self.settings.document_vision_enabled:
            raise RuntimeError("Document vision is disabled.")
        item = self._prepare_image(
            image_index=0,
            image=image,
            text=text,
            blocks=blocks,
        )
        if self.settings.document_vision_preflight_enabled:
            await self._ensure_preflight()
        generated = await self._invoke_batch([item])
        analysis = generated[item.cache_key]
        return {
            "source_id": source_id,
            "image_ref": item.image_ref,
            "model": self.settings.document_vision_model,
            **self.inference_profile(),
            "is_informative": bool(analysis.get("is_informative")),
            "category": str(analysis.get("category") or "unknown"),
        }

    def inference_profile(self) -> dict[str, bool]:
        thinking = self.settings.enable_thinking_for_task("document_vision") is True
        return {"enable_thinking": thinking, "stream": thinking}

    def _effective_prompt_version(self) -> str:
        configured = str(self.settings.document_vision_prompt_version or "v1").strip()
        return f"{configured}+{self._ANALYSIS_SCHEMA_VERSION}"

    async def analyze(
        self,
        *,
        text: str,
        blocks: list[dict[str, Any]],
        images: list[dict[str, Any]],
        source_id: str,
    ) -> DocumentImageEnrichmentResult:
        started_at = time.perf_counter()
        input_blocks = [dict(block) for block in blocks]
        enriched_images = [dict(image) for image in images]
        stats = self.empty_stats(
            enabled=bool(self.settings.document_vision_enabled),
            candidate_count=len(images),
        )
        warnings: list[str] = []
        if not self.settings.document_vision_enabled or not images:
            stats["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
            return DocumentImageEnrichmentResult(
                text=text,
                blocks=input_blocks,
                images=enriched_images,
                stats=stats,
                warnings=warnings,
            )

        prepared: list[_PreparedImage] = []
        unique_by_cache_key: dict[str, _PreparedImage] = {}
        for index, image in enumerate(images):
            try:
                item = self._prepare_image(
                    image_index=index,
                    image=image,
                    text=text,
                    blocks=input_blocks,
                )
            except (OSError, ValueError) as exc:
                stats["skipped_count"] += 1
                stats["preparation_failed_count"] += 1
                warnings.append(
                    f"图片 {self._safe_ref(image.get('image_ref') or image.get('relative_path'))} 已跳过: "
                    f"{self._safe_error(exc)}"
                )
                continue
            prepared.append(item)
            if item.cache_key in unique_by_cache_key:
                stats["duplicate_count"] += 1
            else:
                unique_by_cache_key[item.cache_key] = item

        analyses_by_key: dict[str, dict[str, Any]] = {}
        misses: list[_PreparedImage] = []
        for item in unique_by_cache_key.values():
            try:
                cached = await asyncio.to_thread(self.repository.get, item.cache_key)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"图片分析缓存读取失败，将继续调用模型: {self._safe_error(exc)}")
                cached = None
            normalized = self._normalize_cached_analysis(cached, image_ref=item.image_ref)
            if normalized is None:
                misses.append(item)
                continue
            try:
                self._validate_analysis_quality(item, normalized)
            except StructuredOutputError:
                misses.append(item)
                continue
            analyses_by_key[item.cache_key] = normalized
            stats["cache_hit_count"] += 1

        if misses and self.settings.document_vision_preflight_enabled:
            try:
                await self._ensure_preflight()
            except Exception as exc:  # noqa: BLE001
                message = (
                    "文档图片分析已停止：当前 Model Farm China endpoint、API Key、"
                    f"Base64 Data URL 或模型 {self.settings.document_vision_model} 不可用。"
                )
                warnings.append(f"{message} {self._safe_error(exc)}")
                stats["failed_count"] += len(misses)
                if self.settings.document_vision_fail_on_error:
                    raise RuntimeError(message) from exc
                misses = []

        if misses:
            generated, generated_warnings, failed_count = await self._analyze_misses(misses)
            warnings.extend(generated_warnings)
            stats["failed_count"] += failed_count
            if failed_count and self.settings.document_vision_fail_on_error:
                raise RuntimeError(
                    f"{failed_count} 张文档图片在批量及逐图重试后仍分析失败。"
                )
            stats["analysis_count"] += len(generated)
            analyses_by_key.update(generated)
            for item in misses:
                analysis = generated.get(item.cache_key)
                if analysis is None:
                    continue
                try:
                    await asyncio.to_thread(
                        self.repository.save,
                        cache_key=item.cache_key,
                        image_sha256=item.sha256,
                        model_name=self.settings.document_vision_model,
                        prompt_version=self._effective_prompt_version(),
                        analysis=analysis,
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"图片分析缓存写入失败: {self._safe_error(exc)}")

        description_blocks: list[dict[str, Any]] = []
        for item in prepared:
            analysis = analyses_by_key.get(item.cache_key)
            if analysis is None:
                continue
            enriched_images[item.image_index]["analysis"] = {
                **analysis,
                "image_sha256": item.sha256,
                "model_name": self.settings.document_vision_model,
                "prompt_version": self._effective_prompt_version(),
                "analysis_scope": item.analysis_scope,
                "render_profile": item.render_profile,
                **self.inference_profile(),
            }
            if not analysis.get("is_informative") or not self._has_searchable_content(analysis):
                stats["skipped_count"] += 1
                continue
            description_blocks.append(self._description_block(item, analysis))
            stats["description_count"] += 1

        merged_blocks = self._insert_description_blocks(input_blocks, description_blocks)
        stats["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
        if warnings:
            logger.warning(
                "Document image analysis completed with warnings source_id=%s warning_count=%s",
                self._safe_ref(source_id),
                len(warnings),
            )
        return DocumentImageEnrichmentResult(
            text=text,
            blocks=merged_blocks,
            images=enriched_images,
            stats=stats,
            warnings=warnings,
        )

    def _prepare_image(
        self,
        *,
        image_index: int,
        image: dict[str, Any],
        text: str,
        blocks: list[dict[str, Any]],
    ) -> _PreparedImage:
        path = Path(str(image.get("path") or ""))
        if not path.is_file():
            raise ValueError("本地图片文件不存在")
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("图片文件为空")
        if size > max(1, self.settings.document_vision_max_image_bytes):
            raise ValueError(f"图片超过大小限制 {self.settings.document_vision_max_image_bytes} bytes")
        mime_type = mimetypes.guess_type(path.name)[0] or ""
        if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise ValueError(f"不支持的图片类型 {mime_type or path.suffix}")
        image_sha256 = self._file_sha256(path)
        profile = self.inference_profile()
        analysis_scope = str(
            image.get("analysis_scope")
            or ("full_page" if image.get("full_page") else "embedded_image")
        ).strip() or "embedded_image"
        render_profile = str(
            image.get("render_profile")
            or (
                f"dpi-{image.get('render_dpi')}"
                if image.get("render_dpi") is not None
                else "source"
            )
        ).strip() or "source"
        image_ref = str(
            image.get("image_ref")
            or image.get("relative_path")
            or path.name
        ).replace("\\", "/")
        page = image.get("page")
        nearby_context = self._nearby_context(
            text=text,
            blocks=blocks,
            page=page,
            image_ref=image_ref,
        )
        supplied_context: list[tuple[str, str]] = []
        native_page_text = str(image.get("native_page_text") or "").strip()
        parser_page_text = str(image.get("parser_page_text") or "").strip()
        if native_page_text:
            supplied_context.append(("原生页面文本（仅供核对）", native_page_text))
        if parser_page_text:
            supplied_context.append(("解析器页面文本（仅供核对）", parser_page_text))
        if nearby_context:
            supplied_context.append(("附近正文（仅供消歧）", nearby_context))
        context_limit = max(100, self.settings.document_vision_context_chars)
        per_source_limit = max(40, context_limit // max(1, len(supplied_context)))
        context = "\n\n".join(
            f"{label}：\n{value[:per_source_limit]}"
            for label, value in supplied_context
        )[:context_limit]
        raw_quality_reasons = image.get("analysis_reasons")
        if not isinstance(raw_quality_reasons, (list, tuple, set)):
            raw_quality_reasons = (
                [raw_quality_reasons] if raw_quality_reasons else []
            )
        quality_reasons = tuple(
            sorted(
                set(
                    str(reason).strip()
                    for reason in raw_quality_reasons
                    if str(reason).strip()
                )
            )
        )
        requires_structure = bool(
            self._STRUCTURE_REQUIRED_REASONS.intersection(quality_reasons)
        )
        context_sha256 = hashlib.sha256(context.encode("utf-8")).hexdigest()
        cache_key = hashlib.sha256(
            (
                f"{image_sha256}:{self.settings.document_vision_model}:"
                f"{self._effective_prompt_version()}:"
                f"scope={analysis_scope}:render={render_profile}:"
                f"quality={','.join(quality_reasons)}:"
                f"requires_structure={str(requires_structure).lower()}:"
                f"context={context_sha256}:"
                f"temperature={self.settings.document_vision_temperature:g}:"
                f"max_tokens={self.settings.document_vision_max_tokens}:"
                f"thinking={str(profile['enable_thinking']).lower()}:"
                f"stream={str(profile['stream']).lower()}"
            ).encode("utf-8")
        ).hexdigest()
        return _PreparedImage(
            image_index=image_index,
            image_ref=image_ref,
            path=path,
            page=page,
            sha256=image_sha256,
            cache_key=cache_key,
            context=context,
            analysis_scope=analysis_scope,
            render_profile=render_profile,
            quality_reasons=quality_reasons,
            requires_structure=requires_structure,
        )

    async def _ensure_preflight(self) -> None:
        if self._preflight_complete:
            return
        if self._preflight_error:
            raise RuntimeError(self._preflight_error)
        content = [
            {
                "type": "text",
                "text": (
                    "这是无敏感信息的传输能力检查。请为 image_ref=preflight 返回一条结构化分析，"
                    "并将 is_informative 设为 false。"
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{self._PREFLIGHT_IMAGE}"},
            },
        ]
        try:
            result = await self._invoke_model(content)
            if not result.analyses:
                raise RuntimeError("预检未返回结构化图片结果")
        except Exception as exc:  # noqa: BLE001
            self._preflight_error = self._safe_error(exc)
            raise
        self._preflight_complete = True

    async def _analyze_misses(
        self,
        items: list[_PreparedImage],
    ) -> tuple[dict[str, dict[str, Any]], list[str], int]:
        # Complex complete pages need their own output budget. They still run
        # concurrently, but do not compete with other pages inside one tool
        # response for nodes, relations, steps and table cells.
        batch_size = (
            1
            if any(item.analysis_scope == "full_page" for item in items)
            else max(1, self.settings.document_vision_batch_size)
        )
        batches = [items[index : index + batch_size] for index in range(0, len(items), batch_size)]
        semaphore = asyncio.Semaphore(max(1, self.settings.document_vision_max_concurrency))

        async def run(batch: list[_PreparedImage]):
            async with semaphore:
                return await self._analyze_batch_with_fallback(batch)

        results = await asyncio.gather(*(run(batch) for batch in batches))
        analyses: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        failed_count = 0
        for generated, batch_warnings, batch_failures in results:
            analyses.update(generated)
            warnings.extend(batch_warnings)
            failed_count += batch_failures
        return analyses, warnings, failed_count

    async def _analyze_batch_with_fallback(
        self,
        batch: list[_PreparedImage],
    ) -> tuple[dict[str, dict[str, Any]], list[str], int]:
        try:
            return await self._invoke_batch(batch), [], 0
        except Exception as batch_error:  # noqa: BLE001
            warnings = [f"图片批量分析失败，改为逐图重试: {self._safe_error(batch_error)}"]
            if len(batch) == 1:
                if self._is_retryable_format_error(batch_error):
                    warnings.append("图片结构化格式错误，执行唯一一次格式重试。")
                    try:
                        return await self._invoke_batch(
                            batch,
                            validation_retry=True,
                        ), warnings, 0
                    except Exception as retry_error:  # noqa: BLE001
                        warnings.append(
                            f"图片格式重试失败: {self._safe_error(retry_error)}"
                        )
                return {}, warnings, 1
            generated: dict[str, dict[str, Any]] = {}
            failures = 0
            for item in batch:
                try:
                    generated.update(await self._invoke_batch([item]))
                except Exception as exc:  # noqa: BLE001
                    if self._is_retryable_format_error(exc):
                        warnings.append(
                            f"图片 {self._safe_ref(item.image_ref)} 结构化格式错误，"
                            "执行唯一一次格式重试。"
                        )
                        try:
                            generated.update(
                                await self._invoke_batch(
                                    [item],
                                    validation_retry=True,
                                )
                            )
                            continue
                        except Exception as retry_error:  # noqa: BLE001
                            exc = retry_error
                    failures += 1
                    warnings.append(
                        f"图片 {self._safe_ref(item.image_ref)} 分析失败: {self._safe_error(exc)}"
                    )
            return generated, warnings, failures

    @staticmethod
    def _is_retryable_format_error(exc: Exception) -> bool:
        return isinstance(exc, StructuredOutputError) and exc.code != "timeout"

    async def _invoke_batch(
        self,
        batch: list[_PreparedImage],
        *,
        validation_retry: bool = False,
    ) -> dict[str, dict[str, Any]]:
        result = await self._invoke_model(
            self._multimodal_content(batch, validation_retry=validation_retry)
        )
        by_ref = {analysis.image_ref: analysis for analysis in result.analyses}
        output: dict[str, dict[str, Any]] = {}
        for item in batch:
            analysis = by_ref.get(item.image_ref)
            if analysis is None and len(batch) == 1 and len(result.analyses) == 1:
                analysis = result.analyses[0]
            if analysis is None:
                raise StructuredOutputError(
                    "business_validation",
                    f"模型未返回 image_ref={self._safe_ref(item.image_ref)} 的结果",
                )
            normalized = analysis.model_dump(exclude={"image_ref"})
            self._validate_analysis_quality(item, normalized)
            output[item.cache_key] = normalized
        return output

    def _validate_analysis_quality(
        self,
        item: _PreparedImage,
        analysis: dict[str, Any],
    ) -> None:
        """Retry a flagged relation/table page that returned only a caption."""

        declared_layout = str(analysis.get("layout_type") or "none").strip()
        relations = [
            value
            for value in self._mapping_items(analysis.get("relations"))
            if str(value.get("relation") or "").strip()
        ]
        ordered_steps = [
            value
            for value in self._mapping_items(analysis.get("ordered_steps"))
            if str(value.get("text") or "").strip()
        ]
        tables = [
            value
            for index, value in enumerate(
                self._mapping_items(analysis.get("tables")),
                start=1,
            )
            if self._serialize_table(value, table_index=index)
        ]
        has_relational_structure = bool(relations or ordered_steps)
        has_table_structure = bool(tables)
        has_rich_detail = bool(
            has_relational_structure
            or has_table_structure
            or self._mapping_items(analysis.get("nodes"))
            or self._clean_text_values(analysis.get("key_facts"))
            or self._clean_text_values(analysis.get("evidence"))
        )
        requires_table = (
            "native_grid_without_structured_table" in item.quality_reasons
            or declared_layout in self._TABULAR_LAYOUT_TYPES
        )
        requires_relations = (
            "explicit_relation_visual" in item.quality_reasons
            or declared_layout in self._RELATIONAL_LAYOUT_TYPES
        )
        requires_rich_detail = declared_layout in self._RICH_LAYOUT_TYPES
        if not (requires_table or requires_relations or requires_rich_detail):
            return
        if requires_table and not has_table_structure:
            expected = "可验证的 table"
        elif requires_relations and not has_relational_structure:
            expected = "可验证的 relation 或 ordered_step"
        elif requires_rich_detail and not has_rich_detail:
            expected = "节点、关系、顺序、表格、关键事实或视觉证据"
        else:
            return
        raise StructuredOutputError(
            "business_validation",
            (
                f"复杂页 {self._safe_ref(item.image_ref)} 的风险类型或版式要求"
                f"返回{expected}，但当前结果只有概括性描述。"
            ),
        )

    async def _invoke_model(
        self,
        content: list[dict[str, Any]],
    ) -> DocumentImageAnalysisBatch:
        return await self.llm.ainvoke_multimodal_structured(
            content=content,
            schema=DocumentImageAnalysisBatch,
            task_name="document_vision",
            model=self.settings.document_vision_model,
            temperature=self.settings.document_vision_temperature,
            max_tokens=self.settings.document_vision_max_tokens,
            strategy="tool",
            timeout_seconds=self.settings.document_vision_timeout_seconds,
            enable_thinking=self.settings.enable_thinking_for_task("document_vision"),
        )

    def _multimodal_content(
        self,
        batch: list[_PreparedImage],
        *,
        validation_retry: bool = False,
    ) -> list[dict[str, Any]]:
        retry_instruction = (
            "\n这是一次结构修复重试：上次结果缺少当前风险类型要求的结构。"
            "请重新查看图片；表格风险必须返回 tables，关系/流程风险必须返回"
            " relations 或 ordered_steps。不要只改写 description。"
            if validation_retry
            else ""
        )
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"{self._PROMPT}\nPrompt version: {self._effective_prompt_version()}\n"
                    f"本批次共 {len(batch)} 张图片。{retry_instruction}"
                ),
            }
        ]
        for item in batch:
            content.extend(
                [
                    {
                        "type": "text",
                        "text": (
                            f"image_ref: {item.image_ref}\n"
                            f"page_idx: {item.page}\n"
                            f"analysis_scope: {item.analysis_scope}\n"
                            f"render_profile: {item.render_profile}\n"
                            f"结构提取要求: {'必须返回可验证结构' if item.requires_structure else '按图片实际内容判断'}\n"
                            f"候选原因: {', '.join(item.quality_reasons) or '无'}\n"
                            f"核对上下文:\n{item.context or '无可用核对上下文'}"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": self._image_data_url(item.path)}},
                ]
            )
        return content

    @staticmethod
    def _file_sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _image_data_url(path: Path) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        content = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{content}"

    def _normalize_cached_analysis(
        self,
        cached: dict[str, Any] | None,
        *,
        image_ref: str,
    ) -> dict[str, Any] | None:
        if not cached:
            return None
        try:
            validated = DocumentImageAnalysis.model_validate({"image_ref": image_ref, **cached})
        except Exception:  # noqa: BLE001
            return None
        return validated.model_dump(exclude={"image_ref"})

    def _nearby_context(
        self,
        *,
        text: str,
        blocks: list[dict[str, Any]],
        page: Any,
        image_ref: str,
    ) -> str:
        limit = max(100, self.settings.document_vision_context_chars)
        matched_index = next(
            (
                index
                for index, block in enumerate(blocks)
                if self._refs_match(image_ref, block.get("image_ref"))
            ),
            None,
        )
        candidates: list[str] = []
        for index, block in enumerate(blocks):
            block_text = str(block.get("text") or "").strip()
            if not block_text:
                continue
            same_page = page is not None and block.get("page") == page
            near_image = matched_index is not None and abs(index - matched_index) <= 4
            if same_page or near_image:
                candidates.append(block_text)
        context = "\n".join(candidates).strip() or text[:limit].strip()
        return context[:limit]

    def _description_block(
        self,
        item: _PreparedImage,
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        parts: list[str] = []
        description = str(analysis.get("description") or "").strip()
        if description:
            parts.append(f"图片解读：{description}")
        visible_text = str(analysis.get("visible_text") or "").strip()
        if visible_text:
            parts.append(f"图片可见文字：{visible_text}")
        key_facts = self._clean_text_values(analysis.get("key_facts"))
        if key_facts:
            parts.append("图片关键信息：" + "；".join(key_facts))
        layout_type = str(analysis.get("layout_type") or "none").strip()
        if layout_type not in {"", "none"}:
            parts.append(f"视觉版式：{layout_type}")

        nodes = self._mapping_items(analysis.get("nodes"))
        node_labels = {
            str(node.get("node_id") or "").strip(): str(node.get("label") or "").strip()
            for node in nodes
        }
        serialized_nodes = [
            self._serialize_node(node)
            for node in nodes
            if str(node.get("node_id") or "").strip()
            and str(node.get("label") or "").strip()
        ]
        if serialized_nodes:
            parts.append("视觉节点：" + "；".join(serialized_nodes))

        relations = self._mapping_items(analysis.get("relations"))
        serialized_relations = [
            self._serialize_relation(relation, node_labels)
            for relation in relations
            if str(relation.get("relation") or "").strip()
        ]
        if serialized_relations:
            parts.append("视觉关系：" + "；".join(serialized_relations))

        ordered_steps = sorted(
            self._mapping_items(analysis.get("ordered_steps")),
            key=lambda step: self._safe_order(step.get("order")),
        )
        serialized_steps = [
            self._serialize_step(step)
            for step in ordered_steps
            if str(step.get("text") or "").strip()
        ]
        if serialized_steps:
            parts.append("明确顺序：" + "；".join(serialized_steps))

        tables = self._mapping_items(analysis.get("tables"))
        for table_index, table in enumerate(tables, start=1):
            table_text = self._serialize_table(table, table_index=table_index)
            if table_text:
                parts.append(table_text)

        evidence = self._clean_text_values(analysis.get("evidence"))
        if evidence:
            parts.append("视觉证据：" + "；".join(evidence))
        uncertainties = self._clean_text_values(analysis.get("uncertainties"))
        if uncertainties:
            parts.append("不确定项：" + "；".join(uncertainties))

        visual_structure = {
            "layout_type": layout_type,
            "nodes": nodes,
            "relations": relations,
            "ordered_steps": ordered_steps,
            "tables": tables,
            "evidence": evidence,
            "uncertainties": uncertainties,
        }
        return {
            "type": "image_description",
            "text": "\n".join(parts),
            "page": item.page,
            "section": None,
            "image_ref": item.image_ref,
            "image_path": str(item.path),
            "image_sha256": item.sha256,
            "image_category": analysis.get("category"),
            "image_confidence": analysis.get("confidence"),
            "analysis_scope": item.analysis_scope,
            "render_profile": item.render_profile,
            "visual_structure": visual_structure,
            "model_name": self.settings.document_vision_model,
            "prompt_version": self._effective_prompt_version(),
        }

    @staticmethod
    def _clean_text_values(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            output.append(text)
        return output

    @staticmethod
    def _mapping_items(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            return []
        output: list[dict[str, Any]] = []
        for value in values:
            if isinstance(value, dict):
                output.append(dict(value))
                continue
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                output.append(dict(model_dump()))
        return output

    @classmethod
    def _has_searchable_content(cls, analysis: dict[str, Any]) -> bool:
        if any(
            str(analysis.get(key) or "").strip()
            for key in ("description", "visible_text")
        ):
            return True
        if cls._clean_text_values(analysis.get("key_facts")):
            return True
        nodes = cls._mapping_items(analysis.get("nodes"))
        if any(
            str(node.get("node_id") or "").strip()
            and str(node.get("label") or "").strip()
            for node in nodes
        ):
            return True
        relations = cls._mapping_items(analysis.get("relations"))
        if any(str(relation.get("relation") or "").strip() for relation in relations):
            return True
        steps = cls._mapping_items(analysis.get("ordered_steps"))
        if any(str(step.get("text") or "").strip() for step in steps):
            return True
        return any(
            cls._serialize_table(table, table_index=index)
            for index, table in enumerate(
                cls._mapping_items(analysis.get("tables")),
                start=1,
            )
        )

    @staticmethod
    def _uncertainty_suffix(value: dict[str, Any]) -> str:
        evidence = str(value.get("evidence") or "").strip()
        details: list[str] = []
        if evidence:
            details.append(f"证据：{evidence}")
        if value.get("uncertain"):
            details.append("存在不确定性")
        return f"（{'；'.join(details)}）" if details else ""

    @classmethod
    def _serialize_node(cls, node: dict[str, Any]) -> str:
        node_id = str(node.get("node_id") or "").strip()
        label = str(node.get("label") or "").strip()
        role = str(node.get("role") or "").strip()
        role_suffix = f"[{role}]" if role else ""
        return f"{node_id}={label}{role_suffix}{cls._uncertainty_suffix(node)}"

    @classmethod
    def _serialize_relation(
        cls,
        relation: dict[str, Any],
        node_labels: dict[str, str],
    ) -> str:
        source_id = str(relation.get("source_id") or "").strip()
        target_id = str(relation.get("target_id") or "").strip()
        source = node_labels.get(source_id) or source_id
        target = node_labels.get(target_id) or target_id
        relation_text = str(relation.get("relation") or "").strip()
        return (
            f"{source} --{relation_text}--> {target}"
            f"{cls._uncertainty_suffix(relation)}"
        )

    @classmethod
    def _serialize_step(cls, step: dict[str, Any]) -> str:
        order = cls._safe_order(step.get("order"))
        text = str(step.get("text") or "").strip()
        node_id = str(step.get("node_id") or "").strip()
        node_suffix = f"[{node_id}]" if node_id else ""
        return f"{order}. {text}{node_suffix}{cls._uncertainty_suffix(step)}"

    @staticmethod
    def _safe_order(value: Any) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 1

    @classmethod
    def _serialize_table(cls, table: dict[str, Any], *, table_index: int) -> str:
        title = str(table.get("title") or "").strip() or f"表格 {table_index}"
        raw_headers = table.get("headers")
        headers = (
            [str(value).strip() for value in raw_headers]
            if isinstance(raw_headers, list)
            else []
        )
        raw_rows = table.get("rows")
        rows = raw_rows if isinstance(raw_rows, list) else []
        lines = [f"图片表格：{title}{cls._uncertainty_suffix(table)}"]
        if any(headers):
            lines.append(" | ".join(headers))
        for row in rows:
            if not isinstance(row, list):
                continue
            cells = [str(value).strip() for value in row]
            if any(cells):
                lines.append(" | ".join(cells))
        return "\n".join(lines) if len(lines) > 1 else ""

    def _insert_description_blocks(
        self,
        blocks: list[dict[str, Any]],
        descriptions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        pending = [dict(description) for description in descriptions]
        for block in blocks:
            output.append(dict(block))
            matches = [
                description
                for description in pending
                if self._refs_match(description.get("image_ref"), block.get("image_ref"))
            ]
            for description in matches:
                description["section"] = block.get("section")
                output.append(description)
                pending.remove(description)
        for description in pending:
            insert_at = next(
                (
                    index + 1
                    for index in range(len(output) - 1, -1, -1)
                    if output[index].get("page") == description.get("page")
                ),
                len(output),
            )
            output.insert(insert_at, description)
        return output

    @staticmethod
    def _refs_match(left: Any, right: Any) -> bool:
        if not left or not right:
            return False
        normalized_left = str(left).replace("\\", "/").lstrip("./")
        normalized_right = str(right).replace("\\", "/").lstrip("./")
        return normalized_left == normalized_right or Path(normalized_left).name == Path(normalized_right).name

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        message = re.sub(
            r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+",
            "[redacted image data]",
            message,
            flags=re.IGNORECASE,
        )
        if self.settings.api_key:
            message = message.replace(self.settings.api_key, "[redacted api key]")
        return " ".join(message.split())[:300] or type(exc).__name__

    @staticmethod
    def _safe_ref(value: Any) -> str:
        return " ".join(str(value or "unknown").split())[:120]

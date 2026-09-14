from __future__ import annotations

import asyncio
import base64
import struct
from contextlib import contextmanager
from pathlib import Path

import pytest

from backend.config.settings import Settings
from backend.exceptions.llm_errors import StructuredOutputError
from backend.exceptions.parser_errors import UnsupportedFileTypeError
from backend.schemas.document_image import (
    DocumentImageAnalysis,
    DocumentImageAnalysisBatch,
    DocumentImageEnrichmentResult,
    DocumentVisualNode,
    DocumentVisualRelation,
    DocumentVisualStep,
    DocumentVisualTable,
)
from backend.repositories.postgres_repository import PostgresRepository
from backend.services.document_image_analysis_service import (
    DocumentImageAnalysisService,
)
from backend.vectorstore.index_manager import IndexManager


class MemoryImageAnalysisRepository:
    def __init__(self):
        self.cache = {}
        self.save_calls = []

    def get(self, cache_key):
        return self.cache.get(cache_key)

    def save(self, **kwargs):
        self.save_calls.append(kwargs)
        self.cache[kwargs["cache_key"]] = kwargs["analysis"]
        return kwargs["analysis"]


def _image_refs(content):
    refs = []
    for block in content:
        if block.get("type") != "text":
            continue
        text = str(block.get("text") or "")
        if "image_ref=preflight" in text:
            refs.append("preflight")
        for line in text.splitlines():
            if line.startswith("image_ref: "):
                refs.append(line.removeprefix("image_ref: ").strip())
    return refs


class SuccessfulImageLLM:
    def __init__(self):
        self.calls = []

    async def ainvoke_multimodal_structured(self, **kwargs):
        self.calls.append(kwargs)
        refs = _image_refs(kwargs["content"])
        return DocumentImageAnalysisBatch(
            analyses=[
                DocumentImageAnalysis(
                    image_ref=image_ref,
                    is_informative=image_ref != "preflight",
                    category="chart" if image_ref != "preflight" else "decorative",
                    description="" if image_ref == "preflight" else f"{image_ref} 显示季度销售额增长",
                    visible_text="" if image_ref == "preflight" else "Q1 100，Q2 120",
                    key_facts=[] if image_ref == "preflight" else ["Q2 比 Q1 增长 20%"],
                    layout_type="none" if image_ref == "preflight" else "timeline",
                    nodes=[]
                    if image_ref == "preflight"
                    else [
                        DocumentVisualNode(
                            node_id="q1",
                            label="Q1 100",
                            role="起点",
                            evidence="Q1 100",
                        ),
                        DocumentVisualNode(
                            node_id="q2",
                            label="Q2 120",
                            role="终点",
                            evidence="Q2 120",
                        ),
                    ],
                    relations=[]
                    if image_ref == "preflight"
                    else [
                        DocumentVisualRelation(
                            source_id="q1",
                            target_id="q2",
                            relation="先于",
                            evidence="从左到右的箭头",
                        )
                    ],
                    ordered_steps=[]
                    if image_ref == "preflight"
                    else [
                        DocumentVisualStep(
                            order=1,
                            node_id="q1",
                            text="Q1 100",
                            evidence="数字 1",
                        ),
                        DocumentVisualStep(
                            order=2,
                            node_id="q2",
                            text="Q2 120",
                            evidence="数字 2",
                        ),
                    ],
                    evidence=[] if image_ref == "preflight" else ["Q1", "Q2", "箭头"],
                    confidence=0.95,
                )
                for image_ref in refs
            ]
        )


def _settings(**overrides):
    values = {
        "document_vision_enabled": True,
        "document_vision_model": "qwen3.7-plus",
        "document_vision_preflight_enabled": False,
        "document_vision_batch_size": 4,
        "document_vision_max_concurrency": 2,
        "llm_document_vision_enable_thinking": True,
    }
    values.update(overrides)
    return Settings(**values)


def _write_image(path: Path, content: bytes) -> dict:
    path.write_bytes(content)
    return {
        "path": str(path),
        "relative_path": f"images/{path.name}",
        "image_ref": f"images/{path.name}",
        "page": 0,
        "type": "image",
    }


@pytest.mark.asyncio
async def test_image_analysis_deduplicates_hash_and_adds_page_blocks(tmp_path: Path):
    first = _write_image(tmp_path / "first.png", b"same image bytes")
    second = _write_image(tmp_path / "second.png", b"same image bytes")
    blocks = [
        {"type": "text", "text": "季度经营结果", "page": 0},
        {"type": "image", "text": None, "page": 0, "image_ref": first["image_ref"]},
        {"type": "image", "text": None, "page": 0, "image_ref": second["image_ref"]},
    ]
    repository = MemoryImageAnalysisRepository()
    llm = SuccessfulImageLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=llm,
        repository=repository,
    )

    result = await service.analyze(
        text="季度经营结果",
        blocks=blocks,
        images=[first, second],
        source_id="performance/report.pdf",
    )

    assert result.stats["analysis_count"] == 1
    assert result.stats["duplicate_count"] == 1
    assert result.stats["description_count"] == 2
    assert len(repository.save_calls) == 1
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == "qwen3.7-plus"
    assert llm.calls[0]["enable_thinking"] is True
    assert result.images[0]["analysis"]["stream"] is True
    assert result.images[0]["analysis"]["enable_thinking"] is True
    assert any(
        block.get("type") == "image_url"
        and block["image_url"]["url"].startswith("data:image/png;base64,")
        for block in llm.calls[0]["content"]
    )
    description_blocks = [
        block for block in result.blocks if block.get("type") == "image_description"
    ]
    assert len(description_blocks) == 2
    assert {block["page"] for block in description_blocks} == {0}
    assert all(block["analysis_scope"] == "embedded_image" for block in description_blocks)
    assert all("视觉关系：Q1 100 --先于--> Q2 120" in block["text"] for block in description_blocks)
    assert all("明确顺序：1. Q1 100" in block["text"] for block in description_blocks)
    assert all(block["visual_structure"]["layout_type"] == "timeline" for block in description_blocks)
    prompt = llm.calls[0]["content"][0]["text"]
    assert "relations" in prompt
    assert "ordered_steps" in prompt
    assert "tables" in prompt
    assert "不得仅因两个对象相邻就推断关系" in prompt


@pytest.mark.asyncio
async def test_image_analysis_uses_hash_model_prompt_cache(tmp_path: Path):
    image = _write_image(tmp_path / "cached.png", b"cached image bytes")
    repository = MemoryImageAnalysisRepository()
    first_llm = SuccessfulImageLLM()
    settings = _settings()
    first_service = DocumentImageAnalysisService(
        settings=settings,
        llm_service=first_llm,
        repository=repository,
    )
    first = await first_service.analyze(
        text="正文",
        blocks=[],
        images=[image],
        source_id="policy/a.pdf",
    )
    second_llm = SuccessfulImageLLM()
    second_service = DocumentImageAnalysisService(
        settings=settings,
        llm_service=second_llm,
        repository=repository,
    )

    second = await second_service.analyze(
        text="正文",
        blocks=[],
        images=[image],
        source_id="policy/a.pdf",
    )
    nonthinking_llm = SuccessfulImageLLM()
    nonthinking_service = DocumentImageAnalysisService(
        settings=settings.model_copy(
            update={"llm_document_vision_enable_thinking": False}
        ),
        llm_service=nonthinking_llm,
        repository=repository,
    )
    nonthinking = await nonthinking_service.analyze(
        text="正文",
        blocks=[],
        images=[image],
        source_id="policy/a.pdf",
    )

    assert first.stats["analysis_count"] == 1
    assert second.stats["cache_hit_count"] == 1
    assert second.stats["analysis_count"] == 0
    assert second_llm.calls == []
    assert nonthinking.stats["analysis_count"] == 1
    assert len(nonthinking_llm.calls) == 1
    save = repository.save_calls[0]
    assert save["model_name"] == "qwen3.7-plus"
    assert save["prompt_version"] == (
        f"{settings.document_vision_prompt_version}+layout-relations-v3"
    )
    assert len(save["image_sha256"]) == 64
    assert len(save["cache_key"]) == 64
    assert repository.save_calls[0]["cache_key"] != repository.save_calls[1]["cache_key"]


@pytest.mark.asyncio
async def test_image_analysis_cache_changes_with_verification_context(tmp_path: Path):
    image = _write_image(tmp_path / "context.png", b"same visual")
    repository = MemoryImageAnalysisRepository()
    first_llm = SuccessfulImageLLM()
    first_service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=first_llm,
        repository=repository,
    )
    await first_service.analyze(
        text="旧版页面核对文本",
        blocks=[],
        images=[image],
        source_id="policy/context.pdf",
    )

    second_llm = SuccessfulImageLLM()
    second_service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=second_llm,
        repository=repository,
    )
    second = await second_service.analyze(
        text="修正后的页面核对文本",
        blocks=[],
        images=[image],
        source_id="policy/context.pdf",
    )

    assert second.stats["cache_hit_count"] == 0
    assert second.stats["analysis_count"] == 1
    assert len(second_llm.calls) == 1
    assert len(repository.save_calls) == 2
    assert repository.save_calls[0]["cache_key"] != repository.save_calls[1]["cache_key"]


@pytest.mark.asyncio
async def test_full_page_candidates_use_individual_output_budgets(tmp_path: Path):
    images = []
    for index in range(3):
        image = _write_image(tmp_path / f"page-{index}.png", f"page-{index}".encode())
        image.update({"full_page": True, "analysis_scope": "full_page"})
        images.append(image)
    llm = SuccessfulImageLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(document_vision_batch_size=4),
        llm_service=llm,
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="复杂页面",
        blocks=[],
        images=images,
        source_id="development/complex.pdf",
    )

    assert result.stats["analysis_count"] == 3
    assert len(llm.calls) == 3
    assert all(len(_image_refs(call["content"])) == 1 for call in llm.calls)


@pytest.mark.asyncio
async def test_relation_risk_page_retries_caption_only_result(tmp_path: Path):
    image = _write_image(tmp_path / "relation.png", b"relation visual")
    image.update(
        {
            "full_page": True,
            "analysis_scope": "full_page",
            "structure_risk": True,
            "analysis_reasons": ["explicit_relation_visual"],
        }
    )

    class CaptionThenStructureLLM(SuccessfulImageLLM):
        async def ainvoke_multimodal_structured(self, **kwargs):
            self.calls.append(kwargs)
            refs = _image_refs(kwargs["content"])
            if len(self.calls) == 1:
                return DocumentImageAnalysisBatch(
                    analyses=[
                        DocumentImageAnalysis(
                            image_ref=refs[0],
                            is_informative=True,
                            category="diagram",
                            description="一张流程关系图",
                            confidence=0.8,
                        )
                    ]
                )
            return DocumentImageAnalysisBatch(
                analyses=[
                    DocumentImageAnalysis(
                        image_ref=refs[0],
                        is_informative=True,
                        category="diagram",
                        layout_type="flowchart",
                        nodes=[
                            DocumentVisualNode(node_id="a", label="开始"),
                            DocumentVisualNode(node_id="b", label="结束"),
                        ],
                        relations=[
                            DocumentVisualRelation(
                                source_id="a",
                                target_id="b",
                                relation="流向",
                                evidence="箭头",
                            )
                        ],
                        confidence=0.9,
                    )
                ]
            )

    llm = CaptionThenStructureLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=llm,
        repository=MemoryImageAnalysisRepository(),
    )
    result = await service.analyze(
        text="流程",
        blocks=[],
        images=[image],
        source_id="general/relation.pdf",
    )

    assert len(llm.calls) == 2
    assert result.stats["failed_count"] == 0
    assert result.stats["description_count"] == 1
    assert any("唯一一次格式重试" in warning for warning in result.warnings)
    block = next(block for block in result.blocks if block["type"] == "image_description")
    assert "视觉关系：开始 --流向--> 结束" in block["text"]
    assert "这是一次结构修复重试" in llm.calls[1]["content"][0]["text"]


@pytest.mark.asyncio
async def test_table_risk_is_not_satisfied_by_unrelated_ordered_steps(tmp_path: Path):
    image = _write_image(tmp_path / "grid.png", b"grid visual")
    image["analysis_reasons"] = ["native_grid_without_structured_table"]

    class StepsThenTableLLM:
        def __init__(self):
            self.calls = []

        async def ainvoke_multimodal_structured(self, **kwargs):
            self.calls.append(kwargs)
            image_ref = _image_refs(kwargs["content"])[0]
            if len(self.calls) == 1:
                return DocumentImageAnalysisBatch(
                    analyses=[
                        DocumentImageAnalysis(
                            image_ref=image_ref,
                            is_informative=True,
                            category="table",
                            description="疑似表格",
                            ordered_steps=[DocumentVisualStep(order=1, text="第一项")],
                        )
                    ]
                )
            return DocumentImageAnalysisBatch(
                analyses=[
                    DocumentImageAnalysis(
                        image_ref=image_ref,
                        is_informative=True,
                        category="table",
                        layout_type="table",
                        tables=[
                            DocumentVisualTable(
                                headers=["能力", "结果"],
                                rows=[["协作", "完成"]],
                                evidence="清晰列对齐",
                            )
                        ],
                    )
                ]
            )

    llm = StepsThenTableLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=llm,
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="能力表",
        blocks=[],
        images=[image],
        source_id="development/grid.pdf",
    )

    assert len(llm.calls) == 2
    assert result.stats["failed_count"] == 0
    block = next(block for block in result.blocks if block["type"] == "image_description")
    assert "能力 | 结果" in block["text"]


@pytest.mark.asyncio
async def test_plain_chart_with_verified_facts_does_not_require_fake_relations(
    tmp_path: Path,
):
    image = _write_image(tmp_path / "chart.png", b"chart visual")

    class ChartLLM:
        def __init__(self):
            self.calls = []

        async def ainvoke_multimodal_structured(self, **kwargs):
            self.calls.append(kwargs)
            return DocumentImageAnalysisBatch(
                analyses=[
                    DocumentImageAnalysis(
                        image_ref=_image_refs(kwargs["content"])[0],
                        is_informative=True,
                        category="chart",
                        layout_type="chart",
                        description="季度趋势图",
                        visible_text="Q1 100，Q2 120",
                        key_facts=["Q2 高于 Q1"],
                    )
                ]
            )

    llm = ChartLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=llm,
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="季度趋势",
        blocks=[],
        images=[image],
        source_id="performance/chart.pdf",
    )

    assert len(llm.calls) == 1
    assert result.stats["description_count"] == 1
    assert result.stats["failed_count"] == 0


@pytest.mark.asyncio
async def test_missing_image_ref_result_gets_one_corrective_retry(tmp_path: Path):
    image = _write_image(tmp_path / "missing-ref.png", b"visual")

    class MissingThenValidLLM:
        def __init__(self):
            self.calls = []

        async def ainvoke_multimodal_structured(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return DocumentImageAnalysisBatch(analyses=[])
            return DocumentImageAnalysisBatch(
                analyses=[
                    DocumentImageAnalysis(
                        image_ref=_image_refs(kwargs["content"])[0],
                        is_informative=True,
                        category="photo",
                        layout_type="photo",
                        description="可验证的现场照片",
                    )
                ]
            )

    llm = MissingThenValidLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=llm,
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="现场记录",
        blocks=[],
        images=[image],
        source_id="general/photo.pdf",
    )

    assert len(llm.calls) == 2
    assert result.stats["failed_count"] == 0
    assert "这是一次结构修复重试" in llm.calls[1]["content"][0]["text"]


@pytest.mark.asyncio
async def test_structured_table_is_searchable_without_generic_description(tmp_path: Path):
    image = _write_image(tmp_path / "table.png", b"table image")
    image.update(
        {
            "full_page": True,
            "render_profile": "pdf-page-180dpi",
            "native_page_text": "能力 | 当前表现 | 下一步",
            "parser_page_text": "能力 当前表现 下一步",
        }
    )

    class TableLLM:
        async def ainvoke_multimodal_structured(self, **kwargs):
            refs = _image_refs(kwargs["content"])
            return DocumentImageAnalysisBatch(
                analyses=[
                    DocumentImageAnalysis(
                        image_ref=refs[0],
                        is_informative=True,
                        category="table",
                        description="",
                        layout_type="table",
                        tables=[
                            DocumentVisualTable(
                                title="发展计划",
                                headers=["能力", "当前表现", "下一步"],
                                rows=[["协作", "跨团队交付", "主导项目"]],
                                evidence="清晰的三列表格",
                            )
                        ],
                        evidence=["能力", "当前表现", "下一步"],
                        confidence=0.91,
                    )
                ]
            )

    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=TableLLM(),
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="发展计划",
        blocks=[],
        images=[image],
        source_id="development/plan.pdf",
    )

    block = next(block for block in result.blocks if block["type"] == "image_description")
    assert result.stats["description_count"] == 1
    assert block["analysis_scope"] == "full_page"
    assert block["render_profile"] == "pdf-page-180dpi"
    assert "图片表格：发展计划" in block["text"]
    assert "能力 | 当前表现 | 下一步" in block["text"]
    assert "协作 | 跨团队交付 | 主导项目" in block["text"]
    assert block["visual_structure"]["tables"][0]["title"] == "发展计划"


def test_unbound_visual_relation_is_filtered_and_reported_as_uncertain():
    analysis = DocumentImageAnalysis(
        image_ref="page-1.png",
        is_informative=True,
        category="diagram",
        nodes=[DocumentVisualNode(node_id="known", label="已识别节点")],
        relations=[
            DocumentVisualRelation(
                source_id="known",
                target_id="missing",
                relation="指向",
                evidence="模糊连线",
                uncertain=True,
            )
        ],
    )

    assert analysis.relations == []
    assert "已忽略无法绑定到已识别节点的视觉关系。" in analysis.uncertainties


def test_empty_visual_table_is_filtered_without_discarding_page():
    analysis = DocumentImageAnalysis(
        image_ref="page-1.png",
        is_informative=True,
        category="diagram",
        description="仍然有效的页面说明",
        tables=[DocumentVisualTable()],
    )

    assert analysis.tables == []
    assert "已忽略没有可辨认表头或单元格的视觉表格。" in analysis.uncertainties


def test_whitespace_only_required_visual_fields_are_rejected():
    with pytest.raises(ValueError):
        DocumentVisualNode(node_id=" ", label="有效标签")


def test_null_optional_visual_fields_do_not_discard_valid_page():
    analysis = DocumentImageAnalysis.model_validate(
        {
            "image_ref": "page-1.png",
            "is_informative": True,
            "description": "有效说明",
            "nodes": None,
            "relations": None,
            "ordered_steps": None,
            "tables": [{"title": None, "headers": None, "rows": None}],
            "key_facts": None,
            "evidence": None,
            "uncertainties": None,
        }
    )

    assert analysis.description == "有效说明"
    assert analysis.nodes == []
    assert analysis.tables == []


@pytest.mark.asyncio
async def test_batch_failure_falls_back_to_individual_images(tmp_path: Path):
    images = [
        _write_image(tmp_path / "one.png", b"one"),
        _write_image(tmp_path / "two.png", b"two"),
    ]

    class BatchFailingLLM(SuccessfulImageLLM):
        async def ainvoke_multimodal_structured(self, **kwargs):
            refs = _image_refs(kwargs["content"])
            self.calls.append(kwargs)
            if len(refs) > 1:
                raise RuntimeError("batch unsupported")
            return DocumentImageAnalysisBatch(
                analyses=[
                    DocumentImageAnalysis(
                        image_ref=refs[0],
                        is_informative=True,
                        category="diagram",
                        description=f"{refs[0]} 的流程关系",
                        confidence=0.8,
                    )
                ]
            )

    llm = BatchFailingLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=llm,
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="流程",
        blocks=[],
        images=images,
        source_id="culture/process.pdf",
    )

    assert len(llm.calls) == 3
    assert result.stats["analysis_count"] == 2
    assert result.stats["failed_count"] == 0
    assert result.stats["description_count"] == 2
    assert any("逐图重试" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_single_image_retries_format_error_once_but_not_timeout(tmp_path: Path):
    image = _write_image(tmp_path / "format.png", b"format image")

    class FormatFlakyLLM(SuccessfulImageLLM):
        async def ainvoke_multimodal_structured(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise StructuredOutputError("schema_validation", "invalid category")
            refs = _image_refs(kwargs["content"])
            return DocumentImageAnalysisBatch(
                analyses=[
                    DocumentImageAnalysis(
                        image_ref=refs[0],
                        is_informative=True,
                        category="diagram",
                        description="有效流程图",
                        confidence=0.9,
                    )
                ]
            )

    format_llm = FormatFlakyLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=format_llm,
        repository=MemoryImageAnalysisRepository(),
    )
    result = await service.analyze(
        text="流程",
        blocks=[],
        images=[image],
        source_id="general/format.pdf",
    )

    assert result.stats["failed_count"] == 0
    assert len(format_llm.calls) == 2
    assert any("唯一一次格式重试" in warning for warning in result.warnings)

    class TimeoutLLM:
        def __init__(self):
            self.calls = 0

        async def ainvoke_multimodal_structured(self, **kwargs):
            self.calls += 1
            raise StructuredOutputError("timeout", "timed out")

    timeout_llm = TimeoutLLM()
    timeout_service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=timeout_llm,
        repository=MemoryImageAnalysisRepository(),
    )
    timeout_result = await timeout_service.analyze(
        text="流程",
        blocks=[],
        images=[image],
        source_id="general/timeout.pdf",
    )

    assert timeout_result.stats["failed_count"] == 1
    assert timeout_llm.calls == 1


@pytest.mark.asyncio
async def test_image_analysis_respects_concurrency_limit(tmp_path: Path):
    images = [
        _write_image(tmp_path / f"{index}.png", f"image-{index}".encode())
        for index in range(4)
    ]

    class TimedLLM(SuccessfulImageLLM):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def ainvoke_multimodal_structured(self, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.03)
                return await super().ainvoke_multimodal_structured(**kwargs)
            finally:
                self.active -= 1

    llm = TimedLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(document_vision_batch_size=1, document_vision_max_concurrency=2),
        llm_service=llm,
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="图片集合",
        blocks=[],
        images=images,
        source_id="general/images.pdf",
    )

    assert result.stats["analysis_count"] == 4
    assert llm.max_active == 2


@pytest.mark.asyncio
async def test_preflight_uses_non_sensitive_data_url_before_document_image(tmp_path: Path):
    image = _write_image(tmp_path / "real.png", b"real document image")
    llm = SuccessfulImageLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(document_vision_preflight_enabled=True),
        llm_service=llm,
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="正文",
        blocks=[],
        images=[image],
        source_id="general/preflight.pdf",
    )

    assert result.stats["analysis_count"] == 1
    assert len(llm.calls) == 2
    assert _image_refs(llm.calls[0]["content"]) == ["preflight"]
    preflight_urls = [
        block["image_url"]["url"]
        for block in llm.calls[0]["content"]
        if block.get("type") == "image_url"
    ]
    assert preflight_urls == [
        f"data:image/png;base64,{DocumentImageAnalysisService._PREFLIGHT_IMAGE}"
    ]
    png = base64.b64decode(DocumentImageAnalysisService._PREFLIGHT_IMAGE)
    assert struct.unpack(">II", png[16:24]) == (16, 16)


@pytest.mark.asyncio
async def test_real_image_preflight_does_not_write_analysis_cache(tmp_path: Path):
    image = _write_image(tmp_path / "real-preflight.png", b"real preflight image")
    repository = MemoryImageAnalysisRepository()
    llm = SuccessfulImageLLM()
    service = DocumentImageAnalysisService(
        settings=_settings(document_vision_preflight_enabled=True),
        llm_service=llm,
        repository=repository,
    )

    result = await service.preflight_real_image(
        text="真实文档正文",
        blocks=[],
        image=image,
        source_id="general/real.pdf",
    )

    assert result["source_id"] == "general/real.pdf"
    assert result["enable_thinking"] is True
    assert result["stream"] is True
    assert len(llm.calls) == 2
    assert repository.save_calls == []


@pytest.mark.asyncio
async def test_image_preparation_failure_is_reported_separately(tmp_path: Path):
    missing = {
        "path": str(tmp_path / "missing.png"),
        "image_ref": "images/missing.png",
        "page": 0,
    }
    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=SuccessfulImageLLM(),
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="正文",
        blocks=[],
        images=[missing],
        source_id="general/missing.pdf",
    )

    assert result.stats["preparation_failed_count"] == 1
    assert result.stats["failed_count"] == 0


def test_strict_image_summary_rejects_incomplete_enrichment():
    manager = object.__new__(IndexManager)
    manager._image_analysis_summary = {
        **DocumentImageAnalysisService.empty_stats(enabled=True, candidate_count=2),
        "analysis_count": 1,
        "description_count": 1,
        "failed_count": 1,
    }

    with pytest.raises(RuntimeError, match="incomplete image enrichment"):
        manager._validate_strict_image_summary()

    manager._image_analysis_summary.update(
        {"analysis_count": 2, "description_count": 1, "failed_count": 0}
    )
    manager._validate_strict_image_summary()


@pytest.mark.asyncio
async def test_single_image_failure_keeps_text_and_strict_mode_raises(tmp_path: Path):
    image = _write_image(tmp_path / "failed.png", b"failed image")

    class FailingLLM:
        async def ainvoke_multimodal_structured(self, **kwargs):
            raise RuntimeError("model rejected image")

    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=FailingLLM(),
        repository=MemoryImageAnalysisRepository(),
    )

    result = await service.analyze(
        text="正文仍需入库",
        blocks=[],
        images=[image],
        source_id="general/failed.pdf",
    )

    assert result.text == "正文仍需入库"
    assert result.stats["failed_count"] == 1
    assert not any(block.get("type") == "image_description" for block in result.blocks)

    strict_service = DocumentImageAnalysisService(
        settings=_settings(document_vision_fail_on_error=True),
        llm_service=FailingLLM(),
        repository=MemoryImageAnalysisRepository(),
    )
    with pytest.raises(RuntimeError, match="仍分析失败"):
        await strict_service.analyze(
            text="正文",
            blocks=[],
            images=[image],
            source_id="general/strict.pdf",
        )


def test_analyze_sync_preserves_existing_text_when_there_are_no_images():
    service = DocumentImageAnalysisService(
        settings=_settings(),
        llm_service=SuccessfulImageLLM(),
        repository=MemoryImageAnalysisRepository(),
    )

    result = service.analyze_sync(
        text="纯文本知识",
        blocks=[{"type": "text", "text": "纯文本知识", "page": None}],
        images=[],
        source_id="general/plain.md",
    )

    assert result.text == "纯文本知识"
    assert result.blocks[0]["text"] == "纯文本知识"
    assert result.stats["candidate_count"] == 0


def test_index_manager_rejects_raw_visual_document_before_image_analysis(
    tmp_path: Path,
):
    raw_root = tmp_path / "kb_raw"
    source = raw_root / "performance" / "visual.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")

    with pytest.raises(UnsupportedFileTypeError, match="visual.pdf"):
        IndexManager._validate_source_tree(raw_root)

def test_postgres_schema_includes_document_image_analysis_cache():
    statements = []

    class Connection:
        def execute(self, sql, params=None):
            statements.append(str(sql))
            return self

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    @contextmanager
    def connection():
        yield Connection()

    repository = object.__new__(PostgresRepository)
    repository.connection = connection

    repository.init_schema()

    schema_sql = "\n".join(statements)
    assert "CREATE TABLE IF NOT EXISTS document_image_analyses" in schema_sql
    assert "cache_key TEXT PRIMARY KEY" in schema_sql
    assert "analysis_json JSONB NOT NULL" in schema_sql

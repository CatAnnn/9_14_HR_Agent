from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import backend.services.visual_document_preprocessor as preprocessor_module
from backend.config.settings import Settings
from backend.schemas.document_image import DocumentImageEnrichmentResult
from backend.services.document_image_analysis_service import (
    DocumentImageAnalysisService,
)
from backend.services.visual_document_preprocessor import VisualDocumentPreprocessor
from scripts.prepare_visual_document import _write_text_atomic


def test_processed_markdown_is_published_atomically(tmp_path: Path):
    output = tmp_path / "nested" / "knowledge.md"
    output.parent.mkdir()
    output.write_text("旧内容", encoding="utf-8")

    _write_text_atomic(output, "完整的新内容")

    assert output.read_text(encoding="utf-8") == "完整的新内容"
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_atomic_publication_never_overwrites_without_permission(tmp_path: Path):
    output = tmp_path / "knowledge.md"
    output.write_text("先完成的内容", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _write_text_atomic(output, "后完成的内容", overwrite=False)

    assert output.read_text(encoding="utf-8") == "先完成的内容"
    assert list(tmp_path.glob(f".{output.name}.*.tmp")) == []


class StubMinerUParser:
    def __init__(
        self,
        *,
        text: str = "MinerU 正文",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.text = text
        self.last_metadata = metadata or {}
        self.parse_calls: list[Path] = []
        self.prepare_visual_pages: list[bool | None] = []

    def parse(
        self,
        path: Path,
        *,
        prepare_visual_pages: bool | None = None,
    ) -> str:
        self.parse_calls.append(path)
        self.prepare_visual_pages.append(prepare_visual_pages)
        return self.text


class StubImageAnalysisService:
    def __init__(
        self,
        *,
        result: DocumentImageEnrichmentResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def analyze_sync(self, **kwargs: Any) -> DocumentImageEnrichmentResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _settings(*, vision_enabled: bool = True) -> Settings:
    return Settings(
        document_vision_enabled=vision_enabled,
        document_vision_preflight_enabled=False,
    )


def _source_metadata() -> dict[str, Any]:
    return {
        "structured_blocks": [
            {"type": "text", "text": "第一页正文", "page": 0},
            {
                "type": "image",
                "text": None,
                "page": 1,
                "image_ref": "page_renders/page-2.png",
            },
        ],
        "tables": [{"page": 0, "html": "<table><tr><td>A</td></tr></table>"}],
        "images": [
            {
                "type": "image",
                "path": "/tmp/embedded.png",
                "image_ref": "images/embedded.png",
                "page": 0,
                "analysis_scope": "embedded_image",
            },
            {
                "type": "image",
                "path": "/tmp/page-2.png",
                "image_ref": "page_renders/page-2.png",
                "page": 1,
                "analysis_scope": "full_page",
                "full_page": True,
                "render_profile": "pdf-page-180dpi",
            },
            {
                "type": "image",
                "path": "/tmp/page-3.png",
                "image_ref": "page_renders/page-3.png",
                "page": 2,
                "full_page": True,
                "render_profile": "pdf-page-180dpi",
            },
        ],
        "pages": [{"page": 0, "text": "第一页正文"}],
        "artifact_warnings": ["原解析器警告"],
    }


def _successful_enrichment() -> DocumentImageEnrichmentResult:
    return DocumentImageEnrichmentResult(
        text="MinerU 正文",
        blocks=[
            {"type": "text", "text": "第一页正文", "page": 0},
            {
                "type": "image_description",
                "text": (
                    "视觉版式：timeline\n"
                    "视觉节点：n1=目标设定；n2=年终评价\n"
                    "视觉关系：目标设定 --先于--> 年终评价"
                ),
                "page": 1,
                "image_ref": "page_renders/page-2.png",
                "visual_structure": {
                    "layout_type": "timeline",
                    "nodes": [
                        {"node_id": "n1", "label": "目标设定"},
                        {"node_id": "n2", "label": "年终评价"},
                    ],
                    "relations": [
                        {
                            "source_id": "n1",
                            "target_id": "n2",
                            "relation": "先于",
                        }
                    ],
                    "tables": [
                        {
                            "title": "里程碑",
                            "headers": ["阶段", "结果"],
                            "rows": [["目标设定", "完成"]],
                            "evidence": "页面中的两列表格",
                            "uncertain": False,
                        }
                    ],
                },
            },
        ],
        images=[
            {
                "image_ref": "page_renders/page-2.png",
                "analysis": {
                    "is_informative": True,
                    "layout_type": "timeline",
                    "relations": [
                        {
                            "source_id": "n1",
                            "target_id": "n2",
                            "relation": "先于",
                        }
                    ],
                },
            },
            {
                "image_ref": "page_renders/page-3.png",
                "analysis": {
                    "is_informative": False,
                    "layout_type": "none",
                },
            },
        ],
        stats={
            **DocumentImageAnalysisService.empty_stats(
                enabled=True,
                candidate_count=2,
            ),
            "analysis_count": 2,
            "description_count": 1,
        },
        warnings=["一张页面只有装饰内容"],
    )


def test_prepare_analyzes_only_full_page_candidates_and_merges_visual_text(
    tmp_path: Path,
):
    source = tmp_path / "visual.pdf"
    source.write_bytes(b"pdf")
    parser = StubMinerUParser(metadata=_source_metadata())
    vision = StubImageAnalysisService(result=_successful_enrichment())
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(),
        parser=parser,
        image_analysis_service=vision,
    )

    result = preprocessor.prepare(source, document_id="visual-1")

    assert result.document_id == "visual-1"
    assert parser.parse_calls == [source]
    assert len(vision.calls) == 1
    sent_images = vision.calls[0]["images"]
    assert [image["image_ref"] for image in sent_images] == [
        "page_renders/page-2.png",
        "page_renders/page-3.png",
    ]
    assert all(
        image.get("analysis_scope") == "full_page" or image.get("full_page")
        for image in sent_images
    )
    assert "images/embedded.png" not in {
        image["image_ref"] for image in sent_images
    }

    assert "<!-- document-vision-layout-relations:v2 -->" in result.text
    assert "## 文档视觉关系补充" in result.text
    assert "### 第 2 页" in result.text
    assert "视觉关系：目标设定 --先于--> 年终评价" in result.text
    description = next(
        block for block in result.blocks if block["type"] == "image_description"
    )
    assert description["visual_structure"]["layout_type"] == "timeline"
    visual_table = next(
        table for table in result.tables if table.get("type") == "visual_table"
    )
    assert visual_table["text"] == "里程碑\n阶段 | 结果\n目标设定 | 完成"
    assert visual_table["source"] == "document_vision"

    embedded, full_page, decorative_page = result.images
    assert "analysis" not in embedded
    assert full_page["analysis"]["layout_type"] == "timeline"
    assert decorative_page["analysis"]["is_informative"] is False
    assert result.metadata["document_vision_candidate_count"] == 2
    assert result.metadata["document_vision"]["analysis_count"] == 2
    assert result.metadata["document_vision_warnings"] == [
        "原解析器警告",
        "一张页面只有装饰内容",
    ]


def test_prepare_safely_keeps_parser_result_when_vision_service_fails(
    tmp_path: Path,
):
    source = tmp_path / "fallback.pdf"
    source.write_bytes(b"pdf")
    metadata = _source_metadata()
    parser = StubMinerUParser(text="必须保留的原始正文", metadata=metadata)
    vision = StubImageAnalysisService(error=RuntimeError("vision endpoint timed out"))
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(),
        parser=parser,
        image_analysis_service=vision,
    )

    result = preprocessor.prepare(source, strict_vision=False)

    assert result.text == "必须保留的原始正文"
    assert result.blocks == metadata["structured_blocks"]
    assert result.images == metadata["images"]
    assert result.metadata["document_vision"]["enabled"] is True
    assert result.metadata["document_vision"]["candidate_count"] == 2
    assert result.metadata["document_vision"]["analysis_count"] == 0
    assert result.metadata["document_vision"]["failed_count"] == 2
    assert any(
        "复杂页视觉分析失败，已保留 MinerU 与 PDF 原生文本结果" in warning
        and "vision endpoint timed out" in warning
        for warning in result.metadata["document_vision_warnings"]
    )


def test_prepare_safely_degrades_when_vision_service_cannot_be_constructed(
    monkeypatch,
    tmp_path: Path,
):
    source = tmp_path / "constructor-fallback.pdf"
    source.write_bytes(b"pdf")
    parser = StubMinerUParser(text="必须保留的原始正文", metadata=_source_metadata())

    class FailingService:
        empty_stats = staticmethod(DocumentImageAnalysisService.empty_stats)

        def __init__(self, **kwargs):
            raise RuntimeError("vision configuration unavailable")

    monkeypatch.setattr(
        preprocessor_module,
        "DocumentImageAnalysisService",
        FailingService,
    )
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(),
        parser=parser,
    )

    result = preprocessor.prepare(source, strict_vision=False)

    assert result.text == "必须保留的原始正文"
    assert result.metadata["document_vision"]["failed_count"] == 2
    assert any(
        "vision configuration unavailable" in warning
        for warning in result.metadata["document_vision_warnings"]
    )


def test_strict_vision_reraises_service_exception(tmp_path: Path):
    source = tmp_path / "strict-error.pdf"
    source.write_bytes(b"pdf")
    parser = StubMinerUParser(metadata=_source_metadata())
    vision = StubImageAnalysisService(error=RuntimeError("vision unavailable"))
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(),
        parser=parser,
        image_analysis_service=vision,
    )

    with pytest.raises(RuntimeError, match="vision unavailable"):
        preprocessor.prepare(source, strict_vision=True)


@pytest.mark.parametrize(
    ("failure_updates", "expected_message"),
    [
        ({"analysis_count": 1, "failed_count": 1}, "模型失败 1 页"),
        (
            {"analysis_count": 1, "preparation_failed_count": 1},
            "准备失败 1 页",
        ),
    ],
)
def test_strict_vision_rejects_reported_analysis_failures(
    tmp_path: Path,
    failure_updates: dict[str, int],
    expected_message: str,
):
    source = tmp_path / "strict-failed-count.pdf"
    source.write_bytes(b"pdf")
    parser = StubMinerUParser(metadata=_source_metadata())
    enrichment = _successful_enrichment()
    enrichment.stats.update(failure_updates)
    vision = StubImageAnalysisService(result=enrichment)
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(),
        parser=parser,
        image_analysis_service=vision,
    )

    with pytest.raises(RuntimeError, match=expected_message):
        preprocessor.prepare(source, strict_vision=True)


def test_non_strict_vision_keeps_partial_result_with_reported_failures(
    tmp_path: Path,
):
    source = tmp_path / "partial.pdf"
    source.write_bytes(b"pdf")
    parser = StubMinerUParser(metadata=_source_metadata())
    enrichment = _successful_enrichment()
    enrichment.stats["analysis_count"] = 1
    enrichment.stats["failed_count"] = 1
    enrichment.warnings.append("第三页视觉分析失败")
    vision = StubImageAnalysisService(result=enrichment)
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(),
        parser=parser,
        image_analysis_service=vision,
    )

    result = preprocessor.prepare(source, strict_vision=False)

    assert "视觉关系：目标设定 --先于--> 年终评价" in result.text
    assert result.metadata["document_vision"]["failed_count"] == 1
    assert "第三页视觉分析失败" in result.metadata["document_vision_warnings"]


def test_non_strict_vision_reports_selected_page_that_failed_to_render(
    tmp_path: Path,
):
    source = tmp_path / "render-partial.pdf"
    source.write_bytes(b"pdf")
    metadata = _source_metadata()
    metadata["visual_candidate_pages"] = [1, 2, 3]
    parser = StubMinerUParser(metadata=metadata)
    enrichment = _successful_enrichment()
    vision = StubImageAnalysisService(result=enrichment)
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(),
        parser=parser,
        image_analysis_service=vision,
    )

    result = preprocessor.prepare(source, strict_vision=False)

    assert result.metadata["document_vision"]["candidate_count"] == 3
    assert result.metadata["document_vision"]["preparation_failed_count"] == 1
    assert any(
        "1 个复杂页候选未生成整页图片" in warning
        for warning in result.metadata["document_vision_warnings"]
    )


def test_strict_vision_rejects_selected_page_that_failed_to_render(
    tmp_path: Path,
):
    source = tmp_path / "render-strict.pdf"
    source.write_bytes(b"pdf")
    metadata = _source_metadata()
    metadata["visual_candidate_pages"] = [1, 2, 3]
    parser = StubMinerUParser(metadata=metadata)
    vision = StubImageAnalysisService(result=_successful_enrichment())
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(),
        parser=parser,
        image_analysis_service=vision,
    )

    with pytest.raises(RuntimeError, match="未生成整页图片"):
        preprocessor.prepare(source, strict_vision=True)

    assert vision.calls == []


def test_disabled_vision_does_not_invoke_service(tmp_path: Path):
    source = tmp_path / "disabled.pdf"
    source.write_bytes(b"pdf")
    parser = StubMinerUParser(metadata=_source_metadata())
    vision = StubImageAnalysisService(error=AssertionError("must not be called"))
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(vision_enabled=False),
        parser=parser,
        image_analysis_service=vision,
    )

    result = preprocessor.prepare(source, enable_vision=True, strict_vision=True)

    assert vision.calls == []
    assert parser.prepare_visual_pages == [False]
    assert result.text == "MinerU 正文"
    assert result.metadata["document_vision"]["enabled"] is False
    assert result.metadata["document_vision_candidate_count"] == 2


def test_intentionally_skipped_vision_does_not_report_render_failure(tmp_path: Path):
    source = tmp_path / "skip.pdf"
    source.write_bytes(b"pdf")
    metadata = _source_metadata()
    metadata["images"] = []
    metadata["visual_candidate_pages"] = [1, 2]
    parser = StubMinerUParser(metadata=metadata)
    vision = StubImageAnalysisService(error=AssertionError("must not be called"))
    preprocessor = VisualDocumentPreprocessor(
        settings=_settings(),
        parser=parser,
        image_analysis_service=vision,
    )

    result = preprocessor.prepare(source, enable_vision=False, strict_vision=True)

    assert parser.prepare_visual_pages == [False]
    assert vision.calls == []
    assert result.metadata["document_vision"]["preparation_failed_count"] == 0
    assert not any(
        "未生成整页图片" in warning
        for warning in result.metadata["document_vision_warnings"]
    )

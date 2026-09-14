import base64
import json
from pathlib import Path
import threading
import time

import pytest

import backend.parsers.mineru as mineru_module
from backend.parsers.mineru import MinerUParser
from backend.parsers.pdf_native_layout import (
    NativeLine,
    NativePage,
    PdfNativeLayoutEnrichment,
    PdfPageQuality,
    PdfPageRenderResult,
    RenderedPdfPage,
)
from backend.rag.chunking import chunk_blocks
from backend.vectorstore.pgvector_client import PGVectorClient


def test_mineru_uses_stable_processed_output_dir_mirrored_from_kb_raw(tmp_path: Path):
    parser = MinerUParser()
    parser.settings.runtime_data_dir = tmp_path
    parser.settings.mineru_output_dir = tmp_path / "kb_processed"
    source = tmp_path / "kb_raw" / "job_level" / "sample.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"same document bytes")

    output_dir = parser._make_output_dir(source)
    second_output_dir = parser._make_output_dir(source)

    assert output_dir == second_output_dir
    assert output_dir.parent == tmp_path / "kb_processed" / "job_level"
    assert output_dir.exists()
    assert output_dir.name == (
        f"sample_{parser._file_digest(source)}_{parser._parse_profile_digest()}"
    )


def test_mineru_cache_path_changes_when_parse_profile_changes(monkeypatch, tmp_path: Path):
    parser = MinerUParser()
    monkeypatch.setattr(parser.settings, "runtime_data_dir", tmp_path)
    monkeypatch.setattr(parser.settings, "mineru_output_dir", tmp_path / "kb_processed")
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"same document bytes")

    monkeypatch.setattr(parser.settings, "mineru_effort", "medium")
    medium_output = parser._make_output_dir(source)
    monkeypatch.setattr(parser.settings, "mineru_effort", "high")
    high_output = parser._make_output_dir(source)

    assert medium_output != high_output


def test_mineru_cache_path_separates_visual_page_preparation(tmp_path: Path):
    parser = MinerUParser()
    parser.settings.runtime_data_dir = tmp_path
    parser.settings.mineru_output_dir = tmp_path / "kb_processed"
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"same document bytes")

    text_only_output = parser._make_output_dir(
        source,
        prepare_visual_pages=False,
    )
    visual_output = parser._make_output_dir(
        source,
        prepare_visual_pages=True,
    )

    assert text_only_output != visual_output


def test_mineru_reuses_cached_markdown_without_api(tmp_path: Path):
    parser = MinerUParser()
    parser.settings.runtime_data_dir = tmp_path
    parser.settings.mineru_output_dir = tmp_path / "kb_processed"
    source = tmp_path / "kb_raw" / "performance" / "sample.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"same document bytes")
    output_dir = parser._make_output_dir(source)
    markdown_path = output_dir / "sample.md"
    markdown_path.write_text("cached markdown", encoding="utf-8")
    (output_dir / "sample_content_list.json").write_text(
        '[{"type":"text","text":"cached block","page_idx":0}]',
        encoding="utf-8",
    )
    artifacts = parser._collect_artifacts(output_dir, markdown_path, "cached markdown")
    manifest_path = parser._completion_manifest_path(output_dir, source.stem)
    assert parser._write_completion_manifest(
        manifest_path=manifest_path,
        output_dir=output_dir,
        content_hash=parser._file_digest(source),
        markdown_path=markdown_path,
        artifacts=artifacts,
    ) is None

    def fail_request(path):
        raise AssertionError("MinerU API should not be called on cache hit")

    parser._request_parse = fail_request

    text = parser.parse(source)

    assert text == "cached markdown"
    assert parser.last_metadata["status"] == "cached"
    assert parser.last_metadata["cache_hit"] is True
    assert parser.last_metadata["markdown_path"] == str(markdown_path)
    assert parser.last_metadata["block_count"] >= 1


def test_mineru_does_not_reuse_partial_cache_without_completion_manifest(
    tmp_path: Path,
):
    parser = MinerUParser()
    parser.settings.runtime_data_dir = tmp_path
    parser.settings.mineru_output_dir = tmp_path / "kb_processed"
    parser.settings.document_vision_enabled = False
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"same document bytes")
    output_dir = parser._make_output_dir(source)
    (output_dir / "sample.md").write_text("partial markdown", encoding="utf-8")
    calls: list[Path] = []

    def request_parse(path: Path):
        calls.append(path)
        return {
            "results": {
                "sample": {
                    "md_content": "complete markdown",
                    "content_list": [
                        {"type": "text", "text": "complete block", "page_idx": 0}
                    ],
                }
            }
        }

    parser._request_parse = request_parse
    parser._enrich_pdf_native_layout = lambda **kwargs: (
        kwargs["markdown_text"],
        {},
        [],
    )

    assert parser.parse(source).startswith("complete markdown")
    assert calls == [source]
    assert parser.last_metadata["cache_hit"] is False
    assert "完成清单缺失" in parser.last_metadata["cache_invalid_reason"]
    assert parser._completion_manifest_path(output_dir, source.stem).is_file()


@pytest.mark.parametrize(
    ("prepare_visual_pages", "expected_request_count", "manifest_expected"),
    ((False, 1, True), (True, 2, False)),
)
def test_mineru_retries_render_gap_only_when_visual_pages_were_requested(
    monkeypatch,
    tmp_path: Path,
    prepare_visual_pages: bool,
    expected_request_count: int,
    manifest_expected: bool,
):
    parser = MinerUParser()
    parser.settings.runtime_data_dir = tmp_path
    parser.settings.mineru_output_dir = tmp_path / "kb_processed"
    parser.settings.document_vision_enabled = True
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"same document bytes")
    requests: list[Path] = []

    def request_parse(path: Path):
        requests.append(path)
        return {
            "results": {
                "sample": {
                    "md_content": "parsed markdown",
                    "content_list": [
                        {"type": "text", "text": "parsed block", "page_idx": 0}
                    ],
                }
            }
        }

    monkeypatch.setattr(parser, "_request_parse", request_parse)
    monkeypatch.setattr(
        parser,
        "_enrich_pdf_native_layout",
        lambda **kwargs: (
            kwargs["markdown_text"],
            {"visual_candidate_pages": [0], "visual_page_render_count": 0},
            [],
        ),
    )
    monkeypatch.setattr(
        mineru_module.shutil,
        "which",
        lambda executable: f"/usr/bin/{executable}",
    )

    assert parser.parse(
        source,
        prepare_visual_pages=prepare_visual_pages,
    ) == "parsed markdown"
    assert parser.parse(
        source,
        prepare_visual_pages=prepare_visual_pages,
    ) == "parsed markdown"

    output_dir = parser._make_output_dir(
        source,
        prepare_visual_pages=prepare_visual_pages,
    )
    manifest_path = parser._completion_manifest_path(output_dir, source.stem)
    assert len(requests) == expected_request_count
    assert manifest_path.is_file() is manifest_expected
    assert parser.last_metadata["visual_page_preparation_enabled"] is prepare_visual_pages


def test_mineru_does_not_cache_retryable_native_layout_failure(
    monkeypatch,
    tmp_path: Path,
):
    parser = MinerUParser()
    parser.settings.runtime_data_dir = tmp_path
    parser.settings.mineru_output_dir = tmp_path / "kb_processed"
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"same document bytes")
    requests: list[Path] = []

    def request_parse(path: Path):
        requests.append(path)
        return {
            "results": {
                "sample": {
                    "md_content": "parsed markdown",
                    "content_list": [
                        {"type": "text", "text": "parsed block", "page_idx": 0}
                    ],
                }
            }
        }

    monkeypatch.setattr(parser, "_request_parse", request_parse)
    monkeypatch.setattr(
        parser,
        "_enrich_pdf_native_layout",
        lambda **kwargs: (
            kwargs["markdown_text"],
            {
                "native_layout_status": "failed",
                "native_layout_retryable_failure": True,
            },
            ["pdftotext 原生布局提取超时。"],
        ),
    )

    parser.parse(source, prepare_visual_pages=True)
    parser.parse(source, prepare_visual_pages=True)

    output_dir = parser._make_output_dir(source, prepare_visual_pages=True)
    manifest_path = parser._completion_manifest_path(output_dir, source.stem)
    assert requests == [source, source]
    assert not manifest_path.exists()
    assert any(
        "下次处理会重新尝试" in warning
        for warning in parser.last_metadata["artifact_warnings"]
    )


def test_mineru_decodes_json_images_and_uses_content_list_page_zero(tmp_path: Path):
    parser = MinerUParser()
    output_dir = tmp_path / "processed"
    output_dir.mkdir()
    markdown_path = output_dir / "sample.md"
    markdown = "正文\n\n![chart](images/chart.png)"
    markdown_path.write_text(markdown, encoding="utf-8")
    image_bytes = b"png image bytes"
    payload = {
        "results": {
            "sample": {
                "md_content": markdown,
                "content_list": [
                    {"type": "image", "img_path": "images/chart.png", "page_idx": 0}
                ],
                "images": {
                    "../chart.png": (
                        "data:image/png;base64,"
                        + base64.b64encode(image_bytes).decode("ascii")
                    )
                },
            }
        }
    }

    warnings = parser._write_api_artifacts(output_dir, "sample", payload, "sample")
    artifacts = parser._collect_artifacts(output_dir, markdown_path, markdown)

    assert warnings == []
    assert (output_dir / "images" / "chart.png").read_bytes() == image_bytes
    assert artifacts["images"][0]["page"] == 0
    assert artifacts["images"][0]["image_ref"] == "images/chart.png"
    assert any(
        block.get("image_ref") == "images/chart.png" and block.get("page") == 0
        for block in artifacts["structured_blocks"]
    )


def test_mineru_preserves_html_table_body_as_searchable_structured_text(tmp_path: Path):
    parser = MinerUParser()
    output_dir = tmp_path / "processed"
    output_dir.mkdir()
    markdown_path = output_dir / "sample.md"
    markdown_path.write_text("正文里没有表格内容。", encoding="utf-8")
    (output_dir / "sample_content_list.json").write_text(
        """[
          {
            "type": "table",
            "table_caption": ["Development options"],
            "table_body": "<table><tr><th>Group</th><th>Participants</th></tr><tr><td>TP3</td><td>Associate &amp; HRBP</td></tr><tr><td>TP1&amp;2</td><td>Associate, manager</td></tr></table>",
            "table_footnote": ["Documentation remains optional"],
            "bbox": [34, 195, 995, 783],
            "page_idx": 20
          }
        ]""",
        encoding="utf-8",
    )

    artifacts = parser._collect_artifacts(
        output_dir,
        markdown_path,
        "正文里没有表格内容。",
    )

    table = next(
        block
        for block in artifacts["structured_blocks"]
        if block["type"] == "table"
    )
    assert table["page"] == 20
    assert table["text"].splitlines() == [
        "Development options",
        "Group | Participants",
        "TP3 | Associate & HRBP",
        "TP1&2 | Associate, manager",
        "Documentation remains optional",
    ]
    assert artifacts["table_count"] == 1

    chunks = chunk_blocks("正文里没有表格内容。", artifacts["structured_blocks"])
    indexed_text = "\n".join(chunk["text"] for chunk in chunks)
    assert "TP3" in indexed_text
    assert "Associate & HRBP" in indexed_text
    assert "Documentation remains optional" in indexed_text


def test_mineru_infers_table_type_when_table_body_has_no_type(tmp_path: Path):
    parser = MinerUParser()
    blocks = parser._extract_blocks_from_json(
        [{"table_body": [["Name", "Value"], ["Latency", "0.5s"]], "page": 1}],
        tmp_path / "sample_content_list.json",
    )

    assert blocks == [
        {
            "type": "table",
            "text": "Name | Value\nLatency | 0.5s",
            "page": 1,
            "bbox": None,
            "image_ref": None,
            "source_json": str(tmp_path / "sample_content_list.json"),
        }
    ]


def test_mineru_keeps_native_fallback_after_normal_block_limit(tmp_path: Path):
    parser = MinerUParser()
    normal = [
        {"type": "text", "text": f"normal-{index}", "page_idx": 0}
        for index in range(5005)
    ]
    fallback = {
        "type": "native_layout_fallback",
        "text": "must survive",
        "page": 1,
    }

    blocks = parser._extract_blocks_from_json(
        [*normal, fallback],
        tmp_path / "sample_content_list.json",
    )

    assert sum(block["type"] == "text" for block in blocks) == 5000
    assert any(
        block["type"] == "native_layout_fallback"
        and block["text"] == "must survive"
        for block in blocks
    )


def test_mineru_page_from_full_page_render_is_zero_based():
    assert MinerUParser._page_from_path(
        Path("page-0025-vision-180dpi.png")
    ) == 24


def test_mineru_skips_corrupt_base64_without_writing_image(tmp_path: Path):
    parser = MinerUParser()
    output_dir = tmp_path / "processed"
    output_dir.mkdir()
    payload = {
        "results": {
            "sample": {
                "images": {"chart.png": "data:image/png;base64,not-valid-%%%"}
            }
        }
    }

    warnings = parser._write_api_artifacts(output_dir, "sample", payload, "sample")

    assert len(warnings) == 1
    assert "chart.png" in warnings[0]
    assert not (output_dir / "images" / "chart.png").exists()


def test_mineru_reparses_cached_markdown_when_referenced_image_is_missing(tmp_path: Path):
    parser = MinerUParser()
    parser.settings.runtime_data_dir = tmp_path
    parser.settings.mineru_output_dir = tmp_path / "kb_processed"
    source = tmp_path / "kb_raw" / "performance" / "sample.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"same document bytes")
    output_dir = parser._make_output_dir(source)
    markdown_path = output_dir / "sample.md"
    markdown_path.write_text("![chart](images/chart.png)", encoding="utf-8")
    calls = []
    image_data = base64.b64encode(b"new image").decode("ascii")

    def request_parse(path):
        calls.append(path)
        return {
            "results": {
                "sample": {
                    "md_content": "![chart](images/chart.png)",
                    "content_list": [
                        {"type": "image", "img_path": "images/chart.png", "page_idx": 1}
                    ],
                    "images": {"chart.png": f"data:image/png;base64,{image_data}"},
                }
            }
        }

    parser._request_parse = request_parse

    parser.parse(source)

    assert calls == [source]
    assert parser.last_metadata["cache_hit"] is False
    assert "cache_invalid_reason" in parser.last_metadata
    assert (output_dir / "images" / "chart.png").exists()


@pytest.mark.parametrize(
    ("backend", "effort", "expected_image_analysis"),
    (
        ("hybrid-engine", "medium", "false"),
        ("hybrid-engine", "high", "true"),
        ("hybrid-http-client", "medium", "false"),
        ("vlm-engine", "medium", "true"),
        ("pipeline", "medium", "true"),
    ),
)
def test_mineru_request_uses_fast_payload_without_losing_required_artifacts(
    monkeypatch,
    tmp_path: Path,
    backend: str,
    effort: str,
    expected_image_analysis: str,
):
    parser = MinerUParser()
    monkeypatch.setattr(parser.settings, "mineru_backend", backend)
    monkeypatch.setattr(parser.settings, "mineru_effort", effort)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        content = b'{"results":{"sample":{"md_content":"ok"}}}'
        text = content.decode("utf-8")

        @staticmethod
        def json():
            return {"results": {"sample": {"md_content": "ok"}}}

    def fake_post(url, *, data, files, timeout):
        captured.update({"url": url, "data": data, "files": files, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(mineru_module.httpx, "post", fake_post)

    parser._request_parse(source)

    assert captured["data"]["image_analysis"] == expected_image_analysis
    assert captured["data"]["return_middle_json"] == "false"
    assert captured["data"]["return_content_list"] == "true"
    assert captured["data"]["return_images"] == "true"
    assert parser.last_metadata["http_response_bytes"] == len(FakeResponse.content)


def test_mineru_elapsed_time_covers_request_and_artifact_processing(monkeypatch, tmp_path: Path):
    parser = MinerUParser()
    monkeypatch.setattr(parser.settings, "runtime_data_dir", tmp_path)
    monkeypatch.setattr(parser.settings, "mineru_output_dir", tmp_path / "kb_processed")
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"pdf")
    clock = iter((0.0, 1.0, 1.0, 4.0, 4.0, 5.0, 5.0, 7.0, 7.0, 10.0, 11.0))

    monkeypatch.setattr(mineru_module.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(
        parser,
        "_request_parse",
        lambda path: {"results": {"sample": {"md_content": "parsed"}}},
    )
    monkeypatch.setattr(
        parser,
        "_enrich_pdf_native_layout",
        lambda **kwargs: (kwargs["markdown_text"], {}, []),
    )
    monkeypatch.setattr(parser, "_write_api_artifacts", lambda *args, **kwargs: [])
    monkeypatch.setattr(parser, "_collect_artifacts", lambda *args, **kwargs: {})

    assert parser.parse(source) == "parsed"
    assert parser.last_metadata["cache_lookup_elapsed_seconds"] == 1.0
    assert parser.last_metadata["request_elapsed_seconds"] == 3.0
    assert parser.last_metadata["native_layout_elapsed_seconds"] == 1.0
    assert parser.last_metadata["artifact_write_elapsed_seconds"] == 2.0
    assert parser.last_metadata["artifact_collect_elapsed_seconds"] == 3.0
    assert parser.last_metadata["elapsed_seconds"] == 11.0


def test_mineru_parse_persists_native_fallback_and_full_page_render_metadata(
    monkeypatch,
    tmp_path: Path,
):
    import backend.parsers.pdf_native_layout as native_layout_module

    parser = MinerUParser()
    monkeypatch.setattr(parser.settings, "runtime_data_dir", tmp_path)
    monkeypatch.setattr(parser.settings, "mineru_output_dir", tmp_path / "kb_processed")
    monkeypatch.setattr(parser.settings, "document_vision_enabled", True)
    source = tmp_path / "kb_raw" / "development" / "sample.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    raw_content = [
        {
            "type": "text",
            "text": "Parsed heading",
            "bbox": [10, 10, 500, 80],
            "page_idx": 0,
        }
    ]
    payload = {
        "results": {
            "sample": {
                "md_content": "# Parsed heading",
                "content_list": raw_content,
            }
        }
    }
    monkeypatch.setattr(parser, "_request_parse", lambda path: payload)
    quality = PdfPageQuality(
        page_index=0,
        native_token_count=120,
        mineru_token_count=10,
        lexical_recall=0.25,
        severity="severe",
        structure_risk=True,
        grid_like=True,
        has_mineru_table=False,
        largest_visual_ratio=0.72,
        needs_native_fallback=True,
        visual_candidate=True,
        risk_score=145.0,
        reasons=("severe_native_text_loss",),
    )
    native_page = NativePage(
        page_index=0,
        width=600,
        height=800,
        lines=(NativeLine("Parsed heading\nMissing milestone", (10, 10, 500, 120)),),
    )
    fallback = {
        "type": "native_layout_fallback",
        "text": "Missing milestone",
        "page": 0,
        "bbox": [20, 100, 800, 200],
        "native_bbox": [12.0, 80.0, 480.0, 160.0],
        "source": "pdf_native_text",
        "layout_items": [{"text": "Missing milestone"}],
    }
    enrichment = PdfNativeLayoutEnrichment(
        native_pages=(native_page,),
        page_quality=(quality,),
        fallback_blocks=(fallback,),
        visual_candidate_pages=(0,),
        warnings=("layout warning",),
    )
    monkeypatch.setattr(
        native_layout_module,
        "build_pdf_native_layout_enrichment",
        lambda path, content: enrichment,
    )
    render_calls = []

    def fake_render(path, page_indexes, output_dir, *, dpi):
        render_calls.append((path, tuple(page_indexes), output_dir, dpi))
        image_path = output_dir / "page-0001-vision-180dpi.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"png")
        return PdfPageRenderResult(
            pages=(RenderedPdfPage(page_index=0, image_path=image_path, dpi=180),),
        )

    monkeypatch.setattr(native_layout_module, "render_pdf_pages", fake_render)

    text = parser.parse(source)
    output_dir = parser._make_output_dir(source)
    content_path = output_dir / "sample_content_list.json"
    raw_path = output_dir / "sample_mineru_raw.json"
    quality_path = output_dir / "sample_pdf_quality.json"
    stored_content = json.loads(content_path.read_text(encoding="utf-8"))

    assert "<!-- pdf-native-layout-fallback:v1 -->" in text
    assert "### 第 1 页" in text
    assert "Missing milestone" in text
    assert json.loads(raw_path.read_text(encoding="utf-8")) == raw_content
    quality_sidecar = json.loads(quality_path.read_text(encoding="utf-8"))
    assert quality_sidecar["visual_candidate_pages"] == [0]
    assert quality_sidecar["fallback_block_count"] == 1

    fallback_node = next(
        node for node in stored_content if node.get("type") == "native_layout_fallback"
    )
    assert fallback_node["text"] == "Missing milestone"
    assert "layout_items" not in fallback_node
    page_image = next(node for node in stored_content if node.get("full_page"))
    assert page_image["analysis_scope"] == "full_page"
    assert page_image["render_profile"] == "pdf-page-180dpi"
    assert page_image["native_page_text"] == "Parsed heading\nMissing milestone"
    assert page_image["parser_page_text"] == "Parsed heading"
    assert render_calls == [(source, (0,), output_dir / "page_renders", 180)]

    assert parser.last_metadata["native_layout_fallback_count"] == 1
    assert parser.last_metadata["visual_candidate_pages"] == [0]
    assert parser.last_metadata["visual_page_render_count"] == 1
    assert parser.last_metadata["pdf_quality_path"] == str(quality_path)
    assert parser.last_metadata["mineru_raw_content_path"] == str(raw_path)
    assert "layout warning" in parser.last_metadata["artifact_warnings"]
    image_metadata = next(
        image
        for image in parser.last_metadata["images"]
        if image.get("analysis_scope") == "full_page"
    )
    assert image_metadata["page"] == 0
    assert image_metadata["render_dpi"] == 180
    assert image_metadata["native_page_text"] == "Parsed heading\nMissing milestone"
    assert str(quality_path) in parser.last_metadata["artifact_paths"]
    assert str(raw_path) in parser.last_metadata["artifact_paths"]


def test_pgvector_where_from_filter_supports_scope_and_scalar():
    client = object.__new__(PGVectorClient)
    where, params = PGVectorClient._where_from_filter(client, {"scope": ["policy", "performance"], "intent_id": "pip"})
    assert "scope = ANY" in where
    assert "metadata @> %s::jsonb" in where
    assert params == [["policy", "performance"], '{"intent_id": "pip"}']


def test_retrieval_filters_configured_scopes_to_indexed_scopes():
    from backend.services.retrieval_service import RetrievalService

    service = RetrievalService()

    class Repo:
        def load(self):
            return {
                "collections": {
                    "policy": {"vector_count": 0},
                    "performance": {"vector_count": 3},
                    "emotion": {"chunk_count": 2},
                }
            }

    service.vector_repo = Repo()
    assert service._available_scopes(["policy", "performance", "emotion", "examples"]) == ["performance", "emotion"]


def test_retrieval_raw_scope_fallback_uses_existing_kb_folders(tmp_path: Path):
    from backend.services.retrieval_service import RetrievalService

    raw_root = tmp_path / "kb_raw"
    (raw_root / "performance").mkdir(parents=True)
    (raw_root / "performance" / "a.md").write_text("x", encoding="utf-8")
    (raw_root / "empty").mkdir(parents=True)

    assert RetrievalService._raw_kb_scopes(raw_root) == {"performance"}


def test_retrieval_returns_empty_when_configured_scopes_are_unavailable():
    from backend.services.retrieval_service import RetrievalService

    service = RetrievalService()

    class Loader:
        def query_config(self):
            return {
                "queries": {
                    "redline_check": {
                        "enabled": True,
                        "scopes": ["policy"],
                        "query_templates": ["红线风险"],
                    }
                }
            }

    class Repo:
        def load(self):
            return {"collections": {"performance": {"vector_count": 3}, "emotion": {"chunk_count": 2}}}

    service.loader = Loader()
    service.vector_repo = Repo()

    assert service.retrieve("redline_check", {}) == []


@pytest.mark.asyncio
async def test_retrieval_runs_vector_queries_concurrently(monkeypatch):
    from backend.schemas.retrieval import RetrievedChunk
    from backend.services.retrieval_service import RetrievalService
    import backend.services.retrieval_service as retrieval_module

    active = 0
    max_active = 0
    lock = threading.Lock()

    class Loader:
        def query_config(self):
            return {
                "queries": {
                    "guidance": {
                        "enabled": True,
                        "vector_top_k": 1,
                        "rerank_enabled": False,
                        "scopes": ["policy", "performance"],
                        "query_templates": ["query one", "query two"],
                        "query_parallelism": 4,
                    }
                }
            }

    class Repo:
        def load(self):
            return {"collections": {"policy": {"vector_count": 1}, "performance": {"vector_count": 1}}}

    class EmbeddingService:
        def embed_queries(self, queries):
            assert queries == ["query one", "query two"]
            return [[1.0], [2.0]], [False, False]

        async def aembed_queries(self, queries):
            return self.embed_queries(queries)

    class Client:
        def __init__(self, collection_name, embedding_service=None):
            self.collection_name = collection_name
            self.embedding_service = embedding_service

        def search_by_embedding(self, query_embedding, top_k=5, metadata_filter=None):
            nonlocal active, max_active
            scope = str((metadata_filter or {}).get("scope") or "unknown")
            query = str(query_embedding[0])
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                return [
                    RetrievedChunk(
                        chunk_id=f"{scope}:{query}",
                        source_id="source",
                        title="title",
                        scope=scope,
                        text=query,
                        score=1.0,
                    )
                ]
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(retrieval_module, "PGVectorClient", Client)
    RetrievalService.clear_result_cache()
    service = RetrievalService()
    service.loader = Loader()
    service.vector_repo = Repo()
    service.embedding_service = EmbeddingService()

    chunks = await service.aretrieve("guidance", {})
    service._executor.shutdown(wait=True)

    assert len(chunks) == 4
    assert max_active > 1


def test_reranker_runs_batches_concurrently():
    from backend.rag.reranker import Reranker
    from backend.schemas.retrieval import RetrievedChunk

    active = 0
    max_active = 0
    lock = threading.Lock()
    instructions = []

    class Service:
        def rerank(
            self,
            query,
            documents,
            top_n=None,
            *,
            instruction=None,
        ):
            nonlocal active, max_active
            assert query == "query"
            instructions.append(instruction)
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                limit = top_n or len(documents)
                return [(index, float(limit - index)) for index in range(min(limit, len(documents)))]
            finally:
                with lock:
                    active -= 1

    chunks = [
        RetrievedChunk(
            chunk_id=f"chunk-{index}",
            source_id="source",
            title="title",
            scope="policy",
            text=f"text {index}",
            score=0.0,
        )
        for index in range(9)
    ]
    reranker = Reranker()
    reranker.service = Service()

    ranked = reranker.rerank(
        chunks,
        query="query",
        top_k=5,
        parallelism=3,
        instruction="Rank task-specific evidence.",
    )

    assert len(ranked) == 5
    assert max_active > 1
    assert instructions == ["Rank task-specific evidence."] * 3


def test_reranker_single_batch_sends_all_documents_in_one_request():
    from backend.rag.reranker import Reranker
    from backend.schemas.retrieval import RetrievedChunk

    calls = []

    class Service:
        def rerank(
            self,
            query,
            documents,
            top_n=None,
            *,
            instruction=None,
        ):
            calls.append(
                {
                    "query": query,
                    "documents": list(documents),
                    "top_n": top_n,
                    "instruction": instruction,
                }
            )
            return [
                (index, float(len(documents) - index))
                for index in range(len(documents))
            ]

    chunks = [
        RetrievedChunk(
            chunk_id=f"chunk-{index}",
            source_id="source",
            title="title",
            scope="policy",
            text=f"text {index}",
            score=0.0,
        )
        for index in range(9)
    ]
    reranker = Reranker()
    reranker.service = Service()

    ranked = reranker.rerank(
        chunks,
        query="coach query",
        top_k=5,
        parallelism=1,
        instruction="Rank coach evidence.",
    )

    assert calls == [
        {
            "query": "coach query",
            "documents": [
                f"Knowledge scope: policy\nDocument: title\ntext {index}"
                for index in range(9)
            ],
            "top_n": 5,
            "instruction": "Rank coach evidence.",
        }
    ]
    assert [chunk.chunk_id for chunk in ranked] == [
        "chunk-0",
        "chunk-1",
        "chunk-2",
        "chunk-3",
        "chunk-4",
    ]


@pytest.mark.asyncio
async def test_retrieval_serializes_each_query_vector_once_across_scopes(monkeypatch):
    from backend.schemas.retrieval import RetrievedChunk
    from backend.services.retrieval_service import RetrievalService
    import backend.services.retrieval_service as retrieval_module

    literal_calls: list[tuple[float, ...]] = []
    received_literals: list[str] = []

    class Loader:
        def query_config_snapshot(self):
            return {
                "queries": {
                    "guidance": {
                        "enabled": True,
                        "dense_top_k": 1,
                        "rerank_enabled": False,
                        "hybrid_search_enabled": False,
                        "scopes": ["policy", "performance"],
                        "query_templates": ["same query"],
                    }
                }
            }

    class Repo:
        def load(self):
            return {
                "collections": {
                    "policy": {"vector_count": 1},
                    "performance": {"vector_count": 1},
                }
            }

    class EmbeddingService:
        async def aembed_queries(self, queries):
            assert queries == ["same query"]
            return [[1.0, 2.0]], [False]

    class Client:
        supports_query_vector_literal = True

        def __init__(self, collection_name, embedding_service=None):
            self.collection_name = collection_name

        def search_by_embedding(
            self,
            query_embedding,
            *,
            top_k,
            metadata_filter,
            query_vector_literal,
        ):
            received_literals.append(query_vector_literal)
            scope = metadata_filter["scope"]
            return [
                RetrievedChunk(
                    chunk_id=f"{scope}-chunk",
                    source_id="source",
                    title="title",
                    scope=scope,
                    text="text",
                    score=1.0,
                )
            ]

    def vector_literal(values):
        literal_calls.append(tuple(values))
        return "[1.0,2.0]"

    monkeypatch.setattr(retrieval_module, "PGVectorClient", Client)
    monkeypatch.setattr(
        retrieval_module.PostgresRepository,
        "vector_literal",
        staticmethod(vector_literal),
    )
    RetrievalService.clear_result_cache()
    service = RetrievalService()
    service.loader = Loader()
    service.vector_repo = Repo()
    service.embedding_service = EmbeddingService()

    chunks = await service.aretrieve("guidance", {})

    assert {chunk.scope for chunk in chunks} == {"policy", "performance"}
    assert literal_calls == [(1.0, 2.0)]
    assert received_literals == ["[1.0,2.0]", "[1.0,2.0]"]


def test_reranker_uses_contextual_search_text_without_changing_output_text():
    from backend.rag.reranker import Reranker
    from backend.schemas.retrieval import RetrievedChunk

    calls = []

    class Service:
        def rerank(
            self,
            query,
            documents,
            top_n=None,
            *,
            instruction=None,
        ):
            calls.append(list(documents))
            return [(0, 1.0), (1, 0.5)]

    chunks = [
        RetrievedChunk(
            chunk_id="contextual",
            source_id="career/career.md",
            title="Career Elements",
            scope="career",
            text="Cross-functional experience",
            score=0.0,
            metadata={
                "search_text": (
                    "scope: career\nheading: Career Elements > Future Requirements\n"
                    "Cross-functional experience"
                )
            },
        ),
        RetrievedChunk(
            chunk_id="fallback",
            source_id="general/general.md",
            title="General",
            scope="general",
            text="Fallback body",
            score=0.0,
        ),
    ]
    reranker = Reranker()
    reranker.service = Service()

    ranked = reranker.rerank(chunks, query="career development", top_k=2)

    assert calls == [
        [
            "scope: career\nheading: Career Elements > Future Requirements\n"
            "Cross-functional experience",
            "Knowledge scope: general\nDocument: General\nFallback body",
        ]
    ]
    assert [chunk.text for chunk in ranked] == [
        "Cross-functional experience",
        "Fallback body",
    ]


@pytest.mark.asyncio
async def test_async_reranker_uses_contextual_search_text():
    from backend.rag.reranker import Reranker
    from backend.schemas.retrieval import RetrievedChunk

    calls = []

    class Service:
        async def arerank(
            self,
            query,
            documents,
            top_n=None,
            *,
            instruction=None,
        ):
            calls.append(
                {
                    "documents": list(documents),
                    "instruction": instruction,
                }
            )
            return [(0, 1.0)]

    chunk = RetrievedChunk(
        chunk_id="contextual",
        source_id="development_dialog/guide.md",
        title="Agreements & Next Steps",
        scope="development_dialog",
        text="Agree the next step with the employee.",
        score=0.0,
        metadata={"heading_path": ["Agreements & Next Steps"]},
    )
    reranker = Reranker()
    reranker.service = Service()

    ranked = await reranker.arerank(
        [chunk],
        query="next steps",
        top_k=1,
        instruction="Rank follow-up evidence.",
    )

    assert calls == [
        {
            "documents": [
                "Knowledge scope: development_dialog\n"
                "Document: Agreements & Next Steps\n"
                "Section: Agreements & Next Steps\n"
                "Agree the next step with the employee."
            ],
            "instruction": "Rank follow-up evidence.",
        }
    ]
    assert ranked[0].text == chunk.text

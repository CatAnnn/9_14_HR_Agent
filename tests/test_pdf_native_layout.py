import base64
from pathlib import Path
import subprocess

import pytest

import backend.parsers.pdf_native_layout as native_layout
from backend.parsers.pdf_native_layout import (
    NativeLine,
    NativeLayoutExtraction,
    NativePage,
    PdfPageQuality,
    assess_pdf_page_quality,
    build_pdf_native_layout_enrichment,
    build_native_layout_fallback_blocks,
    extract_pdf_native_layout,
    lexical_token_recall,
    render_pdf_pages,
    select_visual_candidate_pages,
    tokenize_layout_text,
)


def _line(text: str, x: float, y: float, width: float = 80.0) -> NativeLine:
    return NativeLine(text=text, bbox=(x, y, x + width, y + 10.0))


def _quality(page_index: int, risk_score: float) -> PdfPageQuality:
    return PdfPageQuality(
        page_index=page_index,
        native_token_count=100,
        mineru_token_count=10,
        lexical_recall=0.1,
        severity="severe",
        structure_risk=True,
        grid_like=True,
        has_mineru_table=False,
        largest_visual_ratio=0.7,
        needs_native_fallback=True,
        visual_candidate=True,
        risk_score=risk_score,
        reasons=("severe_native_text_loss",),
    )


def test_multilingual_tokens_and_recall_preserve_key_terms():
    source = "绩效 ASR WHAT/HOW Überprüfung 日本語 2026-09-07"
    parsed = "绩效 ASR WHAT Überprüfung 日本語"

    tokens = tokenize_layout_text(source)

    assert "üBerprüfung".casefold() in tokens
    assert "2026-09-07" in tokens
    assert 0 < lexical_token_recall(source, parsed) < 1


def test_extract_pdf_native_layout_reads_words_and_exact_bboxes(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"pdf")
    xhtml = """<?xml version="1.0" encoding="UTF-8"?>
    <html xmlns="http://www.w3.org/1999/xhtml"><body><doc>
      <page width="600" height="800"><flow><block>
        <line xMin="10" yMin="20" xMax="155" yMax="35">
          <word xMin="10" yMin="20" xMax="50" yMax="35">WHAT</word>
          <word xMin="55" yMin="20" xMax="90" yMax="35">与</word>
          <word xMin="95" yMin="20" xMax="155" yMax="35">HOW</word>
        </line>
      </block></flow></page>
    </doc></body></html>"""

    monkeypatch.setattr(native_layout.shutil, "which", lambda executable: "/usr/bin/pdftotext")

    def fake_run(command, **kwargs):
        Path(command[-1]).write_text(xhtml, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(native_layout.subprocess, "run", fake_run)

    extraction = extract_pdf_native_layout(pdf_path)

    assert extraction.warnings == ()
    assert len(extraction.pages) == 1
    assert extraction.pages[0].text == "WHAT 与 HOW"
    assert extraction.pages[0].lines[0].bbox == (10.0, 20.0, 155.0, 35.0)
    assert extraction.pages[0].lines[0].words[1].bbox == (55.0, 20.0, 90.0, 35.0)


def test_quality_marks_large_native_text_loss_and_missing_grid_structure():
    lines = []
    for row in range(16):
        for column in range(3):
            lines.append(_line(f"milestone-{row}-{column}", column * 180.0, row * 20.0))
    page = NativePage(page_index=0, width=600.0, height=800.0, lines=tuple(lines))
    mineru = [
        {
            "type": "image",
            "content": "",
            "bbox": [20, 100, 950, 900],
            "page_idx": 0,
        },
        {"type": "text", "text": "milestone-0-0", "page_idx": 0},
    ]

    result = assess_pdf_page_quality([page], mineru)[0]

    assert result.severity == "severe"
    assert result.lexical_recall is not None and result.lexical_recall < 0.65
    assert result.grid_like is True
    assert result.has_mineru_table is False
    assert result.structure_risk is True
    assert result.needs_native_fallback is True
    assert result.visual_candidate is True


def test_explicit_diagram_is_visual_candidate_even_when_all_labels_were_parsed():
    page = NativePage(
        page_index=0,
        width=600,
        height=800,
        lines=(
            _line("Goal setting", 30, 100),
            _line("Midyear review", 240, 100),
            _line("Year end", 430, 100),
        ),
    )
    mineru = [
        {
            "type": "diagram",
            "text": page.text,
            "bbox": [20, 80, 980, 420],
            "page_idx": 0,
        }
    ]

    result = assess_pdf_page_quality([page], mineru)[0]

    assert result.lexical_recall == 1.0
    assert result.structure_risk is True
    assert result.visual_candidate is True
    assert "explicit_relation_visual" in result.reasons


def test_large_generic_visual_is_candidate_even_when_ocr_has_every_label():
    page = NativePage(
        page_index=0,
        width=600,
        height=800,
        lines=(
            _line("Discover", 30, 100),
            _line("Develop", 240, 100),
            _line("Deliver", 430, 100),
        ),
    )
    mineru = [
        {
            "type": "image",
            "text": page.text,
            "bbox": [20, 40, 980, 900],
            "page_idx": 0,
        }
    ]

    result = assess_pdf_page_quality([page], mineru)[0]

    assert result.lexical_recall == 1.0
    assert result.structure_risk is True
    assert result.visual_candidate is True
    assert "large_visual_region" in result.reasons


def test_ocr_only_page_is_candidate_without_native_text_layer():
    page = NativePage(page_index=0, width=600, height=800, lines=())
    mineru = [
        {
            "type": "text",
            "text": " ".join(f"ocr-label-{index}" for index in range(24)),
            "page_idx": 0,
        }
    ]

    result = assess_pdf_page_quality([page], mineru)[0]

    assert result.severity == "unreadable"
    assert result.visual_candidate is True
    assert result.needs_native_fallback is False
    assert "ocr_only_page" in result.reasons


def test_scanned_pdf_uses_mineru_pages_for_visual_candidate_selection(
    monkeypatch,
    tmp_path: Path,
):
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"pdf")
    monkeypatch.setattr(
        native_layout,
        "extract_pdf_native_layout",
        lambda *args, **kwargs: NativeLayoutExtraction(
            warnings=("PDF 没有可读取的原生文本页，可能是扫描件。",)
        ),
    )
    mineru = [
        {
            "type": "image",
            "text": " ".join(f"scan-label-{index}" for index in range(24)),
            "bbox": [0, 0, 1000, 1000],
            "page_idx": 4,
        }
    ]

    enrichment = build_pdf_native_layout_enrichment(pdf_path, mineru)

    assert [page.page_index for page in enrichment.native_pages] == [4]
    assert enrichment.visual_candidate_pages == (4,)
    assert enrichment.fallback_blocks == ()
    assert any("继续筛选扫描页" in warning for warning in enrichment.warnings)


def test_table_body_counts_as_structured_table_and_searchable_text():
    page = NativePage(
        page_index=0,
        width=600,
        height=800,
        lines=tuple(
            _line(text, x, y)
            for y, row in enumerate(("Career Element", "Cross-functional", "Project leadership"))
            for x, text in enumerate((row, "Available", "Evidence"))
        ),
    )
    mineru = [
        {
            "type": "table",
            "table_body": (
                "<table><tr><td>Career Element</td><td>Available</td></tr>"
                "<tr><td>Cross-functional</td><td>Evidence</td></tr></table>"
            ),
            "bbox": [100, 100, 900, 700],
            "page_idx": 0,
        }
    ]

    result = assess_pdf_page_quality([page], mineru)[0]

    assert result.has_mineru_table is True
    assert result.mineru_token_count > 0
    assert "native_grid_without_structured_table" not in result.reasons


def test_fallback_blocks_skip_rows_already_represented_by_mineru():
    page = NativePage(
        page_index=4,
        width=600,
        height=800,
        lines=(
            _line("Already parsed", 10, 10),
            _line("Jun Jul Aug Sep", 10, 40, 160),
            _line("Additional consultation for HR BP", 10, 70, 240),
        ),
    )
    quality = PdfPageQuality(
        page_index=4,
        native_token_count=10,
        mineru_token_count=2,
        lexical_recall=0.2,
        severity="moderate",
        structure_risk=True,
        grid_like=False,
        has_mineru_table=False,
        largest_visual_ratio=0.8,
        needs_native_fallback=True,
        visual_candidate=True,
        risk_score=80,
        reasons=("moderate_native_text_loss",),
    )

    blocks = build_native_layout_fallback_blocks(
        [page],
        [{"type": "text", "text": "Already parsed", "page_idx": 4}],
        [quality],
    )

    assert [block["text"] for block in blocks] == [
        "Jun Jul Aug Sep",
        "Additional consultation for HR BP",
    ]
    assert all(block["page"] == 4 for block in blocks)
    assert all(block["source"] == "pdf_native_text" for block in blocks)
    assert all(block["bbox"] != block["native_bbox"] for block in blocks)


def test_non_grid_fallback_does_not_join_unrelated_two_column_lines():
    page = NativePage(
        page_index=0,
        width=600,
        height=800,
        lines=(
            _line("Left paragraph", 20, 100, 180),
            _line("Right paragraph", 340, 100, 180),
        ),
    )
    quality = PdfPageQuality(
        page_index=0,
        native_token_count=4,
        mineru_token_count=0,
        lexical_recall=0.0,
        severity="moderate",
        structure_risk=False,
        grid_like=False,
        has_mineru_table=False,
        largest_visual_ratio=0.0,
        needs_native_fallback=True,
        visual_candidate=True,
        risk_score=20,
    )

    blocks = build_native_layout_fallback_blocks([page], [], [quality])

    assert [block["text"] for block in blocks] == [
        "Left paragraph",
        "Right paragraph",
    ]


@pytest.mark.parametrize(
    ("page_count", "expected_count"),
    ((4, 1), (15, 2), (31, 3), (80, 3)),
)
def test_visual_candidate_selection_caps_at_ten_percent_and_three_pages(
    page_count: int,
    expected_count: int,
):
    quality = [_quality(index, float(index)) for index in range(page_count)]

    selected = select_visual_candidate_pages(quality)

    assert len(selected) == expected_count
    assert selected == tuple(range(page_count - 1, page_count - expected_count - 1, -1))


def test_scanned_page_fails_open_without_fabricating_fallback():
    page = NativePage(page_index=0, width=600, height=800, lines=())
    mineru = [{"type": "image", "bbox": [0, 0, 1000, 1000], "page_idx": 0}]

    quality = assess_pdf_page_quality([page], mineru)
    fallback = build_native_layout_fallback_blocks([page], mineru, quality)

    assert quality[0].severity == "unreadable"
    assert quality[0].visual_candidate is True
    assert quality[0].needs_native_fallback is False
    assert fallback == ()


def test_render_pdf_pages_uses_full_page_180_dpi_and_reuses_cache(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"pdf")
    calls = []
    monkeypatch.setattr(native_layout.shutil, "which", lambda executable: "/usr/bin/pdftoppm")

    def fake_run(command, **kwargs):
        calls.append(command)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
            "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        Path(f"{command[-1]}.png").write_bytes(png)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(native_layout.subprocess, "run", fake_run)
    output_dir = tmp_path / "renders"

    first = render_pdf_pages(pdf_path, [2], output_dir)
    second = render_pdf_pages(pdf_path, [2], output_dir)

    assert first.warnings == ()
    assert first.pages[0].page_index == 2
    assert first.pages[0].dpi == 180
    assert first.pages[0].image_path.read_bytes().startswith(b"\x89PNG")
    assert calls[0][calls[0].index("-f") + 1] == "3"
    assert calls[0][calls[0].index("-l") + 1] == "3"
    assert calls[0][calls[0].index("-r") + 1] == "180"
    assert second.pages == first.pages
    assert len(calls) == 1


def test_render_pdf_pages_discards_truncated_png_and_retries(
    monkeypatch,
    tmp_path: Path,
):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"pdf")
    output_dir = tmp_path / "renders"
    output_dir.mkdir()
    image_path = output_dir / "page-0001-vision-180dpi.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x10\x00\x00\x00\x10"
    )
    monkeypatch.setattr(native_layout.shutil, "which", lambda executable: "/usr/bin/pdftoppm")
    calls = []

    def fake_timeout(command, **kwargs):
        calls.append(command)
        Path(f"{command[-1]}.png").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(native_layout.subprocess, "run", fake_timeout)

    rendered = render_pdf_pages(pdf_path, [0], output_dir)

    assert len(calls) == 1
    assert rendered.pages == ()
    assert "超时" in rendered.warnings[0]
    assert native_layout._is_valid_png(image_path) is False
    assert not any(path.suffix == ".png" for path in output_dir.glob(".*.render.png"))


def test_png_cache_validation_rejects_crc_corruption(tmp_path: Path):
    png = bytearray(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
            "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    idat_data_index = png.index(b"IDAT") + 4
    png[idat_data_index] ^= 0x01
    image_path = tmp_path / "corrupt.png"
    image_path.write_bytes(png)

    assert native_layout._is_valid_png(image_path) is False


def test_missing_poppler_is_non_fatal(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"pdf")
    monkeypatch.setattr(native_layout.shutil, "which", lambda executable: None)

    extraction = extract_pdf_native_layout(pdf_path)
    rendered = render_pdf_pages(pdf_path, [0], tmp_path / "renders")

    assert extraction.pages == ()
    assert "跳过" in extraction.warnings[0]
    assert rendered.pages == ()
    assert "跳过" in rendered.warnings[0]

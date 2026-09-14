from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

from backend.parsers.organization_unit_xlsx import OrganizationUnitXlsxParser
from backend.parsers.parser_router import ParserRouter
from backend.rag.chunking import (
    _ORGANIZATION_LIST_ITEM_PATTERN,
    _split_at_readable_boundary,
    chunk_organization_text,
)
from backend.rag.structured_facts import extract_exact_facts
from backend.vectorstore.index_manager import IndexManager
from backend.vectorstore.pgvector_client import PGVectorClient


SOURCE_HEADERS = [
    "Organization",
    "Discovered In Organizations",
    "Ownership Confidence",
    "Department Level 1",
    "Department Level 2",
    "Department Level 3",
    "Page Topic",
    "Title",
    "Department Path",
    "Page Owner Department",
    "Last Changed",
    "Language",
    "URL",
    "Sidebar Complete",
    "Content",
]

REMOVED_SOURCE_HEADERS = {
    "Page Owner Department",
    "Last Changed",
    "Language",
    "URL",
    "Sidebar Complete",
}


def _source_row(**overrides: str) -> dict[str, str]:
    row = {
        "Organization": "Corporate Headquarters and Service Areas",
        "Discovered In Organizations": "Corporate Headquarters and Service Areas",
        "Ownership Confidence": "high",
        "Department Level 1": "C/HR3",
        "Department Level 2": "C/HR3-AP",
        "Department Level 3": "",
        "Page Topic": "People strategy",
        "Title": "People Operations",
        "Department Path": (
            "Corporate Headquarters and Service Areas > C/HR3 > C/HR3-AP"
        ),
        "Page Owner Department": "C/HR3-AP",
        "Last Changed": "2026-08-21",
        "Language": "en",
        "URL": "https://example.test/org/people-operations",
        "Sidebar Complete": "true",
        "Content": "The team owns workforce planning and leadership development.",
    }
    row.update(overrides)
    return row


def _write_workbook(path: Path, rows: list[dict[str, str]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Pages"
    sheet.append(SOURCE_HEADERS)
    for row in rows:
        sheet.append([row.get(header, "") for header in SOURCE_HEADERS])
    workbook.save(path)
    workbook.close()


def _parse_records(path: Path) -> tuple[OrganizationUnitXlsxParser, list[dict]]:
    parser = OrganizationUnitXlsxParser()
    parser.parse(path)
    return parser, parser.last_metadata["structured_records"]


def test_contact_and_navigation_noise_is_removed_without_losing_business_facts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "organization_unit.xlsx"
    content = "\n".join(
        [
            "People Operations",
            "[Image: Contact person]",
            "Anna Example",
            "Department Head",
            "Responsibilities",
            "Leads workforce planning and leadership development.",
            "[Image: Address]",
            "Robert-Bosch-Platz 1, Stuttgart",
            "[Image: Phone number]",
            "+49 711 12345",
            "E-Mail",
            "anna.example@example.test",
            "[Image: Visit profile on Bosch.Connect] Bosch Connect",
            "pdf",
            "pptx",
            "Back to top",
            "Quick link to",
            "Skip to content",
            ":",
        ]
    )
    _write_workbook(source, [_source_row(Content=content)])

    parser, records = _parse_records(source)
    assert len(records) == 1
    cleaned = records[0]["content"]
    cleaned_folded = cleaned.casefold()

    for noise in (
        "[image:",
        "+49 711 12345",
        "e-mail",
        "anna.example@example.test",
        "bosch.connect",
        "\npdf",
        "\npptx",
        "back to top",
        "quick link to",
        "skip to content",
    ):
        assert noise not in cleaned_folded

    assert "Anna Example" in cleaned
    assert "Department Head" in cleaned
    assert "Leads workforce planning and leadership development." in cleaned
    assert "Robert-Bosch-Platz 1, Stuttgart" in cleaned
    assert records[0]["metadata"]["removed_contact_value_line_count"] == 2
    assert parser.last_metadata["removed_noise_line_count"] >= 9


def test_same_path_keeps_distinct_url_and_url_less_pages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "organization_unit.xlsx"
    shared_path = "Corporate Headquarters and Service Areas > C/HR3 > C/HR3-AP"
    rows = [
        _source_row(
            URL="https://example.test/org/responsibilities",
            **{
                "Page Topic": "Responsibilities",
                "Title": "Responsibilities",
                "Department Path": shared_path,
                "Content": "Owns workforce planning and policy governance.",
            },
        ),
        _source_row(
            URL="https://example.test/org/collaboration",
            **{
                "Page Topic": "Collaboration",
                "Title": "Collaboration",
                "Department Path": shared_path,
                "Content": "Coordinates HR delivery with regional business partners.",
            },
        ),
        _source_row(
            URL="",
            Language="de",
            **{
                "Page Topic": "Overview",
                "Title": "Overview",
                "Department Path": shared_path,
                "Content": "Die Abteilung unterstuetzt die Personalplanung.",
            },
        ),
        _source_row(
            URL="",
            Language="en",
            **{
                "Page Topic": "Overview",
                "Title": "Overview",
                "Department Path": shared_path,
                "Content": "The department supports people planning.",
            },
        ),
    ]
    _write_workbook(source, rows)

    parser, records = _parse_records(source)

    assert parser.last_metadata["source_row_count"] == 4
    assert parser.last_metadata["structured_record_count"] == 4
    assert parser.last_metadata["duplicate_row_count"] == 0
    assert parser.last_metadata["path_collision_group_count"] == 1
    assert parser.last_metadata["path_collision_record_count"] == 4
    assert {record["metadata"]["source_url"] for record in records} == {
        "",
        "https://example.test/org/responsibilities",
        "https://example.test/org/collaboration",
    }
    url_less = [
        record
        for record in records
        if not record["metadata"]["source_url"]
    ]
    assert {record["metadata"]["language"] for record in url_less} == {
        "de",
        "en",
    }
    assert {record["content"] for record in url_less} == {
        "Die Abteilung unterstuetzt die Personalplanung.",
        "The department supports people planning.",
    }


def test_compact_source_columns_keep_distinct_same_path_pages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "organization_unit.xlsx"
    compact_headers = [
        header
        for header in SOURCE_HEADERS
        if header not in REMOVED_SOURCE_HEADERS
    ]
    first = _source_row(
        **{
            "Page Topic": "Organization",
            "Title": "Organization",
            "Content": "The first team owns workforce planning.",
        }
    )
    duplicate = dict(first)
    second = {
        **first,
        "Content": "The second team owns leadership development.",
    }

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Pages"
    sheet.append(compact_headers)
    for row in (first, duplicate, second):
        sheet.append([row.get(header, "") for header in compact_headers])
    workbook.save(source)
    workbook.close()

    parser, records = _parse_records(source)

    assert parser.matches(source)
    assert parser.last_metadata["source_row_count"] == 3
    assert parser.last_metadata["structured_record_count"] == 2
    assert parser.last_metadata["duplicate_row_count"] == 1
    assert parser.last_metadata["path_collision_group_count"] == 1
    assert {record["content"] for record in records} == {
        "The first team owns workforce planning.",
        "The second team owns leadership development.",
    }
    assert all(not record["metadata"]["source_url"] for record in records)


def test_shared_body_across_paths_is_retained_and_grouped(
    tmp_path: Path,
) -> None:
    source = tmp_path / "organization_unit.xlsx"
    shared_content = "The shared team standard governs workforce planning."
    rows = [
        _source_row(
            URL="",
            Content=shared_content,
            **{
                "Department Level 2": "C/HR3-AP",
                "Department Path": (
                    "Corporate Headquarters and Service Areas > C/HR3 > "
                    "C/HR3-AP"
                ),
            },
        ),
        _source_row(
            URL="",
            Content=shared_content,
            **{
                "Department Level 2": "C/HR3-BP",
                "Department Path": (
                    "Corporate Headquarters and Service Areas > C/HR3 > "
                    "C/HR3-BP"
                ),
            },
        ),
    ]
    _write_workbook(source, rows)

    parser, records = _parse_records(source)

    assert len(records) == 2
    assert len({record["metadata"]["canonical_body_hash"] for record in records}) == 1
    assert all(
        record["metadata"]["canonical_body_group_id"]
        == record["metadata"]["canonical_body_hash"]
        for record in records
    )
    assert all(
        record["metadata"]["canonical_body_group_size"] == 2
        for record in records
    )
    assert parser.last_metadata["canonical_body_group_count"] == 1
    assert parser.last_metadata["canonical_body_group_record_count"] == 2


def test_legacy_fifteen_column_source_remains_backward_compatible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "organization_unit.xlsx"
    source_values = _source_row(
        **{
            "Discovered In Organizations": "Corporate; Mobility",
            "Ownership Confidence": "medium",
            "Department Level 3": "C/HR3-AP1",
            "Page Topic": "Delivery model",
            "Title": "People delivery model",
            "Department Path": (
                "Corporate Headquarters and Service Areas > C/HR3 > "
                "C/HR3-AP > C/HR3-AP1"
            ),
            "Page Owner Department": "C/HR3-AP1",
            "Last Changed": "2026-08-20 09:30",
            "URL": "https://example.test/org/delivery-model",
            "Sidebar Complete": "false",
            "Content": "Owns regional workforce planning and delivery standards.",
        }
    )
    _write_workbook(source, [source_values])

    _, records = _parse_records(source)
    record = records[0]
    metadata = record["metadata"]
    preserved_source_projection = {
        "Organization": metadata["organization"],
        "Discovered In Organizations": metadata["discovered_in_organizations"],
        "Ownership Confidence": metadata["ownership_confidence"],
        "Department Level 1": metadata["department_level_1"],
        "Department Level 2": metadata["department_level_2"],
        "Department Level 3": metadata["department_level_3"],
        "Page Topic": metadata["page_topic"],
        "Title": metadata["page_title"],
        "Department Path": metadata["department_path"],
        "Page Owner Department": metadata["page_owner_department"],
        "Last Changed": metadata["last_changed"],
        "Language": metadata["language"],
        "URL": metadata["source_url"],
        "Sidebar Complete": metadata["sidebar_complete"],
        "Content": record["content"],
    }

    assert len(preserved_source_projection) == len(SOURCE_HEADERS) == 15
    assert preserved_source_projection == source_values


def test_organization_chunker_prefers_lines_sentences_and_words_with_whole_overlap() -> None:
    lines = [
        "Alpha unit owns planning.",
        "Beta unit owns staffing.",
        "Gamma unit owns rewards.",
        "Delta unit owns learning.",
    ]
    line_chunks = chunk_organization_text(
        "\n".join(lines),
        chunk_size=55,
        overlap=30,
    )

    assert line_chunks == [
        "\n".join(lines[:2]),
        "\n".join(lines[1:3]),
        "\n".join(lines[2:]),
    ]
    assert set(line_chunks[0].splitlines()) & set(line_chunks[1].splitlines()) == {
        lines[1]
    }
    assert set(line_chunks[1].splitlines()) & set(line_chunks[2].splitlines()) == {
        lines[2]
    }

    sentences = [
        "Alpha team owns planning.",
        "Beta team owns staffing.",
        "Gamma team owns rewards.",
    ]
    sentence_chunks = chunk_organization_text(
        " ".join(sentences),
        chunk_size=34,
        overlap=0,
    )
    assert sentence_chunks == sentences

    word_text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    word_chunks = chunk_organization_text(word_text, chunk_size=18, overlap=0)
    assert [word for chunk in word_chunks for word in chunk.split()] == word_text.split()
    assert all(len(chunk) <= 55 for chunk in line_chunks)
    assert all(len(chunk) <= 34 for chunk in sentence_chunks)
    assert all(len(chunk) <= 18 for chunk in word_chunks)


def test_organization_chunker_keeps_cjk_punctuation_with_previous_piece() -> None:
    text = ("甲" * 31) + "。" + ("乙" * 30)

    chunks = _split_at_readable_boundary(text, chunk_size=40)

    assert chunks == [("甲" * 31) + "。", "乙" * 30]
    assert not chunks[1].startswith("。")


def test_organization_chunker_prefers_sentence_end_over_later_space() -> None:
    text = ("a" * 24) + ". " + "later words continue beyond boundary"

    chunks = _split_at_readable_boundary(text, chunk_size=34)

    assert chunks[0] == ("a" * 24) + "."
    assert chunks[1].startswith("later words")


def test_readable_boundary_does_not_treat_midword_period_as_sentence_end() -> None:
    text = ("a" * 20) + ". " + ("b" * 9) + ".continued text"

    chunks = _split_at_readable_boundary(text, chunk_size=32)

    assert chunks[0] == ("a" * 20) + "."
    assert chunks[1].startswith(("b" * 9) + ".continued")


def test_organization_chunker_keeps_fitting_multi_sentence_list_item_atomic() -> None:
    list_item = "- Owns workforce planning. Coordinates hiring and development."
    following_line = "The regional partner supports delivery."

    chunks = chunk_organization_text(
        f"{list_item}\n{following_line}",
        chunk_size=len(list_item),
        overlap=0,
    )

    assert chunks == [list_item, following_line]
    assert all(
        "Coordinates hiring and development." not in chunk
        or chunk.startswith("-")
        for chunk in chunks
    )


def test_organization_chunker_supports_only_confirmed_extended_list_markers() -> None:
    list_items = (
        "(1)Die Einheit plant Ressourcen. Sie koordiniert die Umsetzung.",
        "→ The team owns planning. It coordinates implementation.",
        "● The team owns staffing. It coordinates development.",
    )

    for list_item in list_items:
        chunks = chunk_organization_text(
            f"{list_item}\nFollowing context.",
            chunk_size=len(list_item),
            overlap=0,
        )
        assert chunks[0] == list_item

    for false_positive in (
        "P.O. Box 123",
        "Div. Organization",
        "13.03.2026 report date",
        "9:00 meeting time",
        "(248) 613-6886",
        "(77). reference",
    ):
        assert not _ORGANIZATION_LIST_ITEM_PATTERN.match(false_positive)


def test_index_manager_uses_parser_chunk_contract_and_indexes_path_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_root = tmp_path / "kb_large"
    source_dir = raw_root / "organization_unit"
    source_dir.mkdir(parents=True)
    source = source_dir / "organization_unit.xlsx"
    long_content = "\n".join(
        f"Responsibility {index:02d}: "
        + ("supports precise workforce planning and business delivery. " * 3)
        for index in range(18)
    )
    row = _source_row(
        Content=long_content,
        **{
            "Department Path": (
                "Corporate Headquarters and Service Areas > Organization > "
                "Topics A-Z > C/HR3 > C/HR3-AP"
            )
        },
    )
    _write_workbook(source, [row])

    captured: dict[str, int] = {}
    original = IndexManager._build_structured_record_chunks

    def capture_contract(self, **kwargs):
        captured["chunk_size"] = kwargs["chunk_size"]
        captured["chunk_overlap"] = kwargs["chunk_overlap"]
        return original(self, **kwargs)

    monkeypatch.setattr(
        IndexManager,
        "_build_structured_record_chunks",
        capture_contract,
    )

    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(
        kb_index_version="quality-test-v1",
        kb_chunk_size=640,
        kb_chunk_overlap=20,
        effective_embedding_model="test-embedding",
        embedding_dimensions=16,
        collection_name_for_scope=lambda scope: f"test_{scope}",
    )
    manager.parser = ParserRouter()
    manager.image_analysis_service = None
    manager._image_analysis_warnings = []

    chunks_by_scope, _ = manager._build_chunks(raw_root)
    chunks = chunks_by_scope["organization_unit"]
    path = row["Department Path"]
    compact_path = (
        "Corporate Headquarters and Service Areas > C/HR3 > C/HR3-AP"
    )

    assert captured == {
        "chunk_size": OrganizationUnitXlsxParser.CHUNK_SIZE,
        "chunk_overlap": OrganizationUnitXlsxParser.CHUNK_OVERLAP,
    }
    assert len(chunks) > 1
    assert max(len(chunk.text) for chunk in chunks) > manager.settings.kb_chunk_size
    assert all(len(chunk.text) <= OrganizationUnitXlsxParser.CHUNK_SIZE for chunk in chunks)
    assert all(path in chunk.text for chunk in chunks)
    assert all(path not in chunk.metadata["search_text"] for chunk in chunks)
    assert all(
        chunk.metadata["search_text"].count(compact_path) == 1
        for chunk in chunks
    )
    assert all(chunk.metadata["heading_path"] == [path] for chunk in chunks)


def test_dataset_fingerprints_are_isolated_without_changing_default_core_contract() -> None:
    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(
        kb_index_version="quality-test-v1",
        kb_chunk_size=2048,
        kb_chunk_overlap=160,
    )
    legacy_core_payload = {
        "fingerprint_version": "kb-content-v4",
        "chunking": {
            "index_version": "quality-test-v1",
            "chunk_size": 2048,
            "chunk_overlap": 160,
        },
        "parser": {
            "mode": "processed_data_only",
            "markdown": "structured-markdown-v4",
            "organization_unit": "organization-unit-xlsx",
            "retrieval_units": "semantic-items-v2-unit-locators",
        },
    }
    serialized = json.dumps(
        legacy_core_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_core_fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    default_fingerprint = manager._ingestion_fingerprint()
    core_fingerprint = manager._ingestion_fingerprint(dataset="core")
    organization_fingerprint = manager._ingestion_fingerprint(
        dataset="organization_unit"
    )

    assert default_fingerprint == core_fingerprint == legacy_core_fingerprint
    assert organization_fingerprint != core_fingerprint

    manager.settings = SimpleNamespace(
        kb_index_version="quality-test-v1",
        kb_chunk_size=1024,
        kb_chunk_overlap=80,
    )
    assert manager._ingestion_fingerprint(dataset="core") != core_fingerprint
    assert (
        manager._ingestion_fingerprint(dataset="organization_unit")
        == organization_fingerprint
    )


def test_department_codes_and_aliases_are_materialized_as_exact_facts() -> None:
    facts = extract_exact_facts(
        "Owns people strategy and workforce planning.",
        metadata={
            "scope": "organization_unit",
            "department_codes": ["C/HR3", "C/HR3-AP"],
            "department_code_aliases": ["CHR3", "CHR3AP", "C HR3 AP"],
            "department_path": (
                "Corporate Headquarters and Service Areas > C/HR3 > C/HR3-AP"
            ),
        },
    )
    exact_codes = {
        fact["display_value"]
        for fact in facts
        if fact["fact_type"] == "organization_code"
    }

    assert {
        "C/HR3",
        "C/HR3-AP",
        "CHR3",
        "CHR3AP",
        "C HR3 AP",
    } <= exact_codes


def test_widget_cleanup_is_anchored_and_preserves_meaningful_distant_lines() -> None:
    content = "\n".join(
        [
            "Today",
            "[Image: Plant Map]",
            "The plant map shows all manufacturing locations.",
            "Quarterly operations update",
            "Production planning remains on schedule.",
            "Staffing coverage is stable.",
            "Customer deliveries remain reliable.",
            "Quality targets are unchanged.",
            "Company&nbsp;&amp;&nbsp;People",
            "Trusted\x00 facts\x07 remain\u200b available.",
            "Bosch Connect content is loading.",
            "Today",
            "Comments",
            "[Image: image]",
            "[Image: undefined]",
            "[Image: .]",
            "{{date}}",
            "[Image: {{article-img-alt}}] Bosch Connect",
            "{{article-headline}}",
            "{{article-content}}",
            "Content provided by Bosch Connect",
            "News",
            "More news",
        ]
    )
    cleaned, metadata = OrganizationUnitXlsxParser._clean_content_with_stats(
        content
    )

    assert cleaned.splitlines().count("Today") == 1
    assert "[Image: Plant Map]" in cleaned
    assert "The plant map shows all manufacturing locations." in cleaned
    assert "Company & People" in cleaned
    assert "Trusted facts remain available." in cleaned
    for noise in (
        "Bosch Connect content is loading.",
        "Comments",
        "[Image: image]",
        "[Image: undefined]",
        "[Image: .]",
        "{{date}}",
        "{{article-headline}}",
        "{{article-content}}",
        "Content provided by Bosch Connect",
        "More news",
        "&nbsp;",
        "&amp;",
        "\x00",
        "\x07",
        "\u200b",
    ):
        assert noise not in cleaned

    assert metadata["removed_widget_line_count"] >= 8
    assert metadata["removed_control_character_count"] == 3
    assert metadata["decoded_html_entity_count"] == 3


def test_exact_german_bosch_connect_widget_lines_are_removed() -> None:
    content = "\n".join(
        [
            "Bosch Connect Blogbeiträge werden geladen.",
            "Bosch Connect Inhalt konnte nicht geladen werden.",
            "Bosch Connect Inhalt wird geladen.",
            "Der User hat kein Bosch Connect Profil.",
            "Du hast kein Zugriff auf diese Bosch Connect Community",
            "Bosch Connect unterstützt die bereichsübergreifende Zusammenarbeit.",
        ]
    )

    cleaned, metadata = OrganizationUnitXlsxParser._clean_content_with_stats(
        content
    )

    assert cleaned == (
        "Bosch Connect unterstützt die bereichsübergreifende Zusammenarbeit."
    )
    assert metadata["removed_widget_line_count"] == 5


def test_mixed_case_hyphen_codes_and_page_owner_are_exact_facts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "organization_unit.xlsx"
    row = _source_row(
        **{
            "Department Level 1": "GR/FCM-Le",
            "Department Level 2": "PS-CT/xx",
            "Department Level 3": "Product Engineering",
            "Department Path": (
                "Corporate Headquarters and Service Areas > GR/FCM-Le > "
                "PS-CT/xx > Product Engineering"
            ),
            "Page Owner Department": "ETAS-Sid/Xpc-Mu",
        }
    )
    _write_workbook(source, [row])

    _, records = _parse_records(source)
    metadata = records[0]["metadata"]
    codes = set(metadata["department_codes"])

    assert {
        "GR/FCM-LE",
        "PS-CT/XX",
        "ETAS-SID/XPC-MU",
    } <= codes
    assert "GR/FCM-" not in codes
    assert "ETAS-SID/XPC-" not in codes
    assert metadata["page_owner_department"] == "ETAS-Sid/Xpc-Mu"

    facts = extract_exact_facts(
        records[0]["content"],
        metadata={**metadata, "scope": "organization_unit"},
    )
    exact_codes = {
        fact["display_value"]
        for fact in facts
        if fact["fact_type"] == "organization_code"
    }
    assert {
        "GR/FCM-LE",
        "PS-CT/XX",
        "ETAS-SID/XPC-MU",
        "ETAS SID XPC MU",
        "ETASSIDXPCMU",
    } <= exact_codes


def test_organization_path_keeps_slash_code_as_one_unit() -> None:
    facts = extract_exact_facts(
        "Owns product engineering and delivery.",
        metadata={
            "scope": "organization_unit",
            "department_path": "Mobility (BBM) > PS/EE > Product Engineering",
        },
    )
    units = {
        fact["display_value"]
        for fact in facts
        if fact["fact_type"] == "organization_unit"
    }

    assert "PS/EE" in units
    assert units.isdisjoint({"PS", "EE"})


def test_organization_exact_sql_uses_code_boundaries_instead_of_raw_substring() -> None:
    sql = PGVectorClient._exact_scope_rows_sql(
        where_sql="",
        organization_context_enabled=True,
    )
    normalized_sql = " ".join(sql.split())
    compact_sql = "".join(sql.split())

    assert "strpos(q.normalized_query,f.normalized_value)" not in compact_sql
    assert "regexp_replace(q.normalized_query" in compact_sql
    assert "regexp_replace(f.normalized_value" in compact_sql
    assert "[^[:alnum:]_/-]+" in normalized_sql
    assert "' ' || regexp_replace(" in normalized_sql
    assert ") || ' '" in normalized_sql


def test_short_multi_sentence_line_provides_complete_sentence_overlap() -> None:
    first = "Alpha team owns workforce planning."
    overlap_sentence = "Beta team coordinates staffing decisions."
    final = "Gamma team manages leadership development."
    first_line = f"{first} {overlap_sentence}"

    assert len(first_line) < 90
    chunks = chunk_organization_text(
        f"{first_line}\n{final}",
        chunk_size=90,
        overlap=45,
    )

    assert chunks == [
        f"{first}\n{overlap_sentence}",
        f"{overlap_sentence}\n{final}",
    ]

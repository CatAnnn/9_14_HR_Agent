from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook
import pytest

from backend.business_config.loader import get_config_loader
from backend.exceptions.parser_errors import UnsupportedFileTypeError
from backend.parsers.organization_unit_xlsx import OrganizationUnitXlsxParser
from backend.parsers.parser_router import ParserRouter
from backend.services.retrieval_service import RetrievalService
from backend.vectorstore.index_manager import IndexManager


HEADERS = list(OrganizationUnitXlsxParser.REQUIRED_HEADERS)


def _write_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Pages"
    sheet.append(HEADERS)
    sheet.append(
        [
            "Corporate Headquarters and Service Areas",
            "C/HR3",
            "C/HR3-AP",
            "",
            "Corporate Headquarters and Service Areas > C/HR3 > C/HR3-AP",
            "\n\n".join(
                f"Responsibility {index}: the department supports people strategy, workforce planning, and business delivery."
                for index in range(45)
            ),
        ]
    )
    sheet.append(
        [
            "Corporate Headquarters and Service Areas",
            "C/IT",
            "",
            "",
            "Corporate Headquarters and Service Areas > C/IT",
            "",
        ]
    )
    sheet.append(
        [
            "Corporate Headquarters and Service Areas",
            "CR Preparation",
            "RTC-NA_to-be-deleted",
            "CR/RME-NA",
            "Corporate Headquarters and Service Areas > CR Preparation > RTC-NA_to-be-deleted > CR/RME-NA",
            "obsolete content",
        ]
    )
    sheet.append(
        [
            "Mobility (BBM)",
            "",
            "",
            "",
            "Mobility (BBM)",
            "Wir entwickeln Lösungen für die Mobilität und arbeiten mit Kunden.",
        ]
    )
    sheet.append(
        [
            "Mobility (BBM)",
            "",
            "",
            "",
            "Mobility (BBM)",
            "We develop mobility solutions for customers and work with partners.",
        ]
    )
    workbook.save(path)
    workbook.close()


def test_organization_workbook_is_parsed_by_row_with_deduplication(tmp_path):
    source = tmp_path / "organization_unit.xlsx"
    _write_workbook(source)
    parser = OrganizationUnitXlsxParser()

    text = parser.parse(source)
    metadata = parser.last_metadata
    records = metadata["structured_records"]

    assert metadata["source_row_count"] == 5
    assert metadata["structured_record_count"] == 4
    assert metadata["deprecated_row_count"] == 1
    assert metadata["duplicate_row_count"] == 0
    assert metadata["empty_content_count"] == 1
    assert "to-be-deleted" not in text

    mobility = next(
        record
        for record in records
        if record["metadata"]["department_path"] == "Mobility (BBM)"
        and record["metadata"]["language"] == "en"
    )
    assert mobility["metadata"]["language"] == "en"
    assert mobility["metadata"]["duplicate_source_rows"] == [6]
    assert "We develop mobility solutions" in mobility["content"]
    assert "M/xx" in mobility["metadata"]["organization_aliases"]

    hr = next(
        record
        for record in records
        if "C/HR3-AP" in record["metadata"]["department_path"]
    )
    assert hr["metadata"]["department_codes"] == ["C/HR3", "C/HR3-AP"]
    assert "CHR3AP" in hr["metadata"]["department_code_aliases"]
    assert hr["metadata"]["record_type"] == "organization_unit"


def test_structured_chunks_repeat_path_and_never_mix_department_rows(tmp_path):
    source = tmp_path / "organization_unit.xlsx"
    _write_workbook(source)
    parser = OrganizationUnitXlsxParser()
    parser.parse(source)
    records = parser.last_metadata["structured_records"]

    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(kb_index_version="v2")
    chunks = manager._build_structured_record_chunks(
        records=records,
        source_id="organization_unit/organization_unit.xlsx",
        source_path=source,
        doc_id="doc-1",
        scope="organization_unit",
        collection_name="hr_agent_kb_organization_unit",
        content_hash="hash-1",
        parser_name="organization_unit_xlsx",
        chunk_size=1600,
        chunk_overlap=120,
    )

    hr_chunks = [
        chunk
        for chunk in chunks
        if chunk.metadata["department_path"].endswith("C/HR3-AP")
    ]
    assert len(hr_chunks) > 1
    assert all(
        "完整组织路径 / Department Path: Corporate Headquarters and Service Areas > C/HR3 > C/HR3-AP"
        in chunk.text
        for chunk in hr_chunks
    )
    assert all("Mobility (BBM)" not in chunk.text for chunk in hr_chunks)
    assert all(len(chunk.text) <= 1600 for chunk in hr_chunks)
    assert all(chunk.metadata["parser"] == "organization_unit_xlsx" for chunk in chunks)

    hierarchy_only = next(
        chunk for chunk in chunks if chunk.metadata["status"] == "hierarchy_only"
    )
    assert "源文件未提供正文" in hierarchy_only.text
    assert "源文件未提供正文" not in hierarchy_only.metadata["search_text"]
    assert "Content Status" not in hierarchy_only.metadata["search_text"]

    reversed_chunks = manager._build_structured_record_chunks(
        records=list(reversed(records)),
        source_id="organization_unit/organization_unit.xlsx",
        source_path=source,
        doc_id="doc-1",
        scope="organization_unit",
        collection_name="hr_agent_kb_organization_unit",
        content_hash="hash-1",
        parser_name="organization_unit_xlsx",
        chunk_size=1600,
        chunk_overlap=120,
    )
    assert [chunk.chunk_id for chunk in chunks] == [
        chunk.chunk_id for chunk in reversed_chunks
    ]


def test_parser_router_only_specializes_matching_organization_workbooks(tmp_path):
    organization_source = tmp_path / "organization.xlsx"
    _write_workbook(organization_source)

    router = ParserRouter()
    text, metadata = router.parse_file(organization_source)

    assert text.startswith("完整组织路径 / Department Path:")
    assert metadata["parser"] == "organization_unit_xlsx"

    generic_source = tmp_path / "generic.xlsx"
    workbook = Workbook()
    workbook.active.append(["Name", "Value"])
    workbook.active.append(["A", "B"])
    workbook.save(generic_source)
    workbook.close()

    with pytest.raises(UnsupportedFileTypeError, match="generic XLSX"):
        router.parse_file(generic_source)


def test_index_manager_uses_structured_branch_without_document_vision(tmp_path):
    raw_root = tmp_path / "kb_raw"
    source_dir = raw_root / "organization_unit"
    source_dir.mkdir(parents=True)
    source = source_dir / "organization_unit.xlsx"
    _write_workbook(source)

    class NoImageAnalysis:
        def analyze_sync(self, **_kwargs):
            raise AssertionError("organization rows must not enter document vision")

    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(
        kb_index_version="v2",
        kb_chunk_size=2048,
        kb_chunk_overlap=160,
        effective_embedding_model="embedding-model",
        embedding_dimensions=2048,
        collection_name_for_scope=lambda scope: f"hr_agent_kb_{scope}",
    )
    manager.parser = ParserRouter()
    manager.image_analysis_service = NoImageAnalysis()
    manager._image_analysis_warnings = []

    chunks_by_scope, documents = manager._build_chunks(raw_root)

    assert set(chunks_by_scope) == {"organization_unit"}
    assert len(chunks_by_scope["organization_unit"]) > 3
    assert len(documents) == 1
    assert documents[0]["scope"] == "organization_unit"
    assert documents[0]["parse_metadata"]["structured_record_count"] == 4
    assert "structured_records" not in documents[0]["parse_metadata"]


def test_organization_scope_is_limited_to_context_sensitive_rag_tasks():
    queries = get_config_loader().query_config()["queries"]
    expected = {
        "intent_performance_organization_unit",
        "guidance_requirement",
        "output_expectations_evaluation",
        "development_plan_evaluation",
        "model_rag_tool",
    }
    configured = {
        name
        for name, config in queries.items()
        if "organization_unit" in config.get("scopes", [])
    }

    assert configured == expected
    for name in expected:
        assert any(
            template["scopes"] == ["organization_unit"]
            for template in queries[name]["query_templates"]
        )

    intent_config = queries["intent_performance_organization_unit"]
    service = object.__new__(RetrievalService)
    specs = service._render_query_specs(
        templates=intent_config["query_templates"],
        context={
            "intent": {"name": "发展型反馈"},
            "profile": {
                "department": "BD/xx",
                "reporting_line": "BD/xx-AI",
                "role": "软件工程师",
                "level": "SL1",
                "key_goals": ["按期交付"],
                "performance_rating": "2",
                "tcl": "++",
            },
        },
        fallback_scopes=intent_config["scopes"],
        available_scopes=set(intent_config["scopes"]),
    )
    organization_specs = [spec for spec in specs if spec.scopes == ["organization_unit"]]
    assert len(organization_specs) == 1
    assert "BD/xx" in organization_specs[0].query
    assert "BD/xx-AI" in organization_specs[0].query
    assert "软件工程师" in organization_specs[0].query


@pytest.mark.parametrize(
    "agent_name",
    [
        "guidance_requirement",
        "output_expectations_evaluation",
        "development_plan_evaluation",
    ],
)
def test_business_organization_scope_renders_without_department(agent_name):
    config = get_config_loader().query_config()["queries"][agent_name]
    service = object.__new__(RetrievalService)

    specs = service._render_query_specs(
        templates=config["query_templates"],
        context={
            "profile": {},
            "intent": {},
            "career_elements_applicable": False,
            "current_career_elements": [],
        },
        fallback_scopes=config["scopes"],
        available_scopes=set(config["scopes"]),
    )

    organization_pairs = [
        (spec.query, scope)
        for spec in specs
        for scope in spec.scopes
        if scope == "organization_unit"
    ]
    assert len(organization_pairs) == 1
    assert organization_pairs[0][0].strip()


def test_unavailable_organization_scope_is_not_forced():
    service = object.__new__(RetrievalService)

    specs = service._render_query_specs(
        templates=[
            {
                "template": "通用绩效反馈",
                "scopes": ["general"],
                "weight": 1.0,
            }
        ],
        context={"profile": {}},
        fallback_scopes=["general", "organization_unit"],
        available_scopes={"general"},
    )

    assert [(spec.query, spec.scopes) for spec in specs] == [
        ("通用绩效反馈", ["general"])
    ]

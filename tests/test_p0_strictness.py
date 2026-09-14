from pathlib import Path


def test_kb_ingestion_uses_pgvector_without_local_index_shim():
    assert not Path("backend/rag/indexing.py").exists()
    text = Path("backend/services/kb_ingestion.py").read_text(encoding="utf-8")
    assert "IndexManager" in text
    assert "build_local_chunk_index" not in text



def test_profile_agent_has_no_validation_fallback():
    text = Path("backend/agents/profile_extraction.py").read_text(encoding="utf-8")
    assert "except ValidationError" not in text
    assert "EmployeeProfile(**{k: v" not in text
    assert "schema=EmployeeProfileExtractionOutput" in text
    assert "ainvoke_structured" in text


def test_profile_model_schema_excludes_backend_only_fields():
    from backend.schemas.profile import EmployeeProfileExtractionOutput

    properties = EmployeeProfileExtractionOutput.model_json_schema()["properties"]
    assert "source_profile_text" not in properties
    assert "supplemental_info" not in properties
    assert "extraction_notes" not in properties


def test_guidance_agent_uses_one_structured_call_without_default_copy():
    text = Path("backend/agents/guidance_agent.py").read_text(encoding="utf-8")
    assert "GuidanceReport(" not in text
    assert "GuidanceReport.model_validate(payload)" in text
    assert "准备绩效谈话" not in text
    assert "先说明谈话目的" not in text
    assert "GuidanceTextSectionOutput" not in text
    assert "GuidanceListSectionOutput" not in text
    assert "ainvoke_text" not in text
    assert "astream_text" not in text
    assert "ainvoke_structured_single" in text


def test_tests_do_not_skip_missing_langchain_dependency():
    for path in Path("tests").glob("test_*.py"):
        assert "importorskip(\"langchain_core\")" not in path.read_text(encoding="utf-8")

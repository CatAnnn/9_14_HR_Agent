from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents.coach_agent.generic_agent import GenericCoachAgent
from backend.agents.guidance_agent import GuidanceAgent
from backend.exceptions.parser_errors import UnsupportedFileTypeError
from backend.parsers.parser_router import ParserRouter
from backend.parsers.structured_markdown import StructuredMarkdownParser
from backend.rag.citation import _chunk_source_text
from backend.rag.chunking import chunk_blocks, chunk_text
from backend.rag.parent_context import (
    generation_context_text,
    select_generation_context_focus,
    select_job_level_generation_context,
)
from backend.rag.reranker import Reranker
from backend.repositories.postgres_repository import PostgresRepository
from backend.rag.structured_facts import (
    canonical_content_id,
    contextual_exact_fact_text,
    exact_query_terms,
    extract_exact_facts,
    normalize_content,
)
from backend.schemas.retrieval import RetrievedChunk
from backend.services.retrieval_service import RetrievalService
from backend.vectorstore.index_manager import IndexManager
from backend.vectorstore.pgvector_client import PGVectorClient


def _write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_structured_markdown_preserves_complete_parent_and_source_map(tmp_path):
    long_body = "A" * 2600
    source = tmp_path / "guide.md"
    markdown = (
        "# Performance Guide\n\n"
        "Overview.\n\n"
        "## G9 expectations\n\n"
        f"{long_body}\n\n"
        "## Development plan\n\n"
        "Use Career Elements."
    )
    _write_markdown(source, markdown)

    text, metadata = StructuredMarkdownParser().parse(source)
    sections = metadata["sections"]
    blocks = metadata["structured_blocks"]
    root = sections[0]
    g9 = next(section for section in sections if section["title"] == "G9 expectations")

    assert text == markdown
    assert root["text"] == markdown
    assert long_body in g9["text"]
    assert len(g9["text"]) > 2048
    assert g9["source_start_line"] < g9["source_end_line"]
    assert g9["source_end_char"] > g9["source_start_char"]

    chunks = chunk_blocks(text, blocks, chunk_size=2048, overlap=160)
    g9_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("parent_section_local_id") == g9["local_section_id"]
    ]
    assert len(g9_chunks) == 2
    assert all(len(chunk["text"]) <= 2048 for chunk in chunks)
    assert g9_chunks[0]["text"][-160:] == g9_chunks[1]["text"][:160]
    assert all(
        "Development plan" not in chunk["text"] for chunk in g9_chunks
    )


def test_markdown_table_rows_repeat_headers_and_keep_original_source(tmp_path):
    source = tmp_path / "levels.md"
    _write_markdown(
        source,
        "# Levels\n\n"
        "| Level | Expected output |\n"
        "| --- | --- |\n"
        "| G9 | Cross-functional impact |\n"
        "| SL1 | Business responsibility |\n",
    )

    _, metadata = StructuredMarkdownParser().parse(source)
    rows = [
        block
        for block in metadata["structured_blocks"]
        if block["type"] == "table_row"
    ]

    assert [row["table_headers"] for row in rows] == [
        ["Level", "Expected output"],
        ["Level", "Expected output"],
    ]
    assert rows[0]["text"] == (
        "Level: G9\nExpected output: Cross-functional impact"
    )
    assert rows[0]["source_text"] == "| G9 | Cross-functional impact |"
    assert rows[0]["source_start_line"] == 5


def test_short_complete_list_is_one_atomic_chunk(tmp_path):
    source = tmp_path / "questions.md"
    _write_markdown(
        source,
        "# Guide\n\n"
        "## Four questions\n\n"
        "1. What result was achieved?\n"
        "2. Which path produced it?\n"
        "3. What standard applies?\n"
        "4. What happens next?\n",
    )

    text, metadata = StructuredMarkdownParser().parse(source)
    list_blocks = [
        block
        for block in metadata["structured_blocks"]
        if block["type"] == "list_item"
    ]
    chunks = chunk_blocks(
        text,
        metadata["structured_blocks"],
        chunk_size=2048,
        overlap=160,
    )
    list_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("semantic_group_type") == "list"
    ]

    assert metadata["parser_version"] == "structured-markdown-v4"
    assert len({block["list_id"] for block in list_blocks}) == 1
    assert [block["list_item_index"] for block in list_blocks] == [0, 1, 2, 3]
    assert len(list_chunks) == 1
    assert list_chunks[0]["atomic_structure"] is True
    assert list_chunks[0]["semantic_split"] is False
    assert list_chunks[0]["text"].startswith("## Four questions")
    assert all(
        item in list_chunks[0]["text"]
        for item in (
            "1. What result was achieved?",
            "2. Which path produced it?",
            "3. What standard applies?",
            "4. What happens next?",
        )
    )
    retrieval_units = list_chunks[0]["retrieval_units"]
    assert len(retrieval_units) == 4
    assert [unit["unit_index"] for unit in retrieval_units] == [0, 1, 2, 3]
    assert retrieval_units[2]["text"] == "3. What standard applies?"


def test_long_list_splits_only_between_items_and_repeats_heading(tmp_path):
    source = tmp_path / "actions.md"
    item_texts = [
        f"ITEM_{index} " + (letter * 760)
        for index, letter in enumerate("ABCD", start=1)
    ]
    markdown = (
        "# Guide\n\n## Action list\n\n"
        + "\n".join(
            f"{index}. {item}"
            for index, item in enumerate(item_texts, start=1)
        )
    )
    _write_markdown(source, markdown)

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(
        text,
        metadata["structured_blocks"],
        chunk_size=2048,
        overlap=160,
    )
    list_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("semantic_group_type") == "list"
    ]

    assert len(list_chunks) == 2
    assert all(chunk["text"].startswith("## Action list") for chunk in list_chunks)
    assert all(chunk["semantic_split"] is True for chunk in list_chunks)
    assert all(chunk["oversized_atomic"] is False for chunk in list_chunks)
    for index, item_text in enumerate(item_texts, start=1):
        complete_item = f"{index}. {item_text}"
        assert sum(
            complete_item in chunk["text"] for chunk in list_chunks
        ) == 1


def test_single_oversized_list_item_is_never_hard_split(tmp_path):
    source = tmp_path / "oversized-item.md"
    item_body = "ONE_COMPLETE_ITEM " + ("X" * 2600)
    _write_markdown(
        source,
        "# Guide\n\n## Evidence\n\n- " + item_body,
    )

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(
        text,
        metadata["structured_blocks"],
        chunk_size=2048,
        overlap=160,
    )
    list_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("semantic_group_type") == "list"
    ]

    assert len(list_chunks) == 1
    assert f"- {item_body}" in list_chunks[0]["text"]
    assert list_chunks[0]["oversized_atomic"] is True
    assert len(list_chunks[0]["text"]) > 2048


def test_short_table_is_one_atomic_chunk(tmp_path):
    source = tmp_path / "ratings.md"
    _write_markdown(
        source,
        "# Performance\n\n"
        "## Rating definitions\n\n"
        "| Rating | Meaning |\n"
        "| --- | --- |\n"
        "| 1 | Exceeds expectations |\n"
        "| 2 | Meets expectations |\n"
        "| 3 | Partly meets expectations |\n",
    )

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(text, metadata["structured_blocks"])
    table_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("semantic_group_type") == "table"
    ]

    assert len(table_chunks) == 1
    assert table_chunks[0]["text"].startswith("## Rating definitions")
    assert table_chunks[0]["semantic_group_item_count"] == 3
    assert "Rating: 1\nMeaning: Exceeds expectations" in table_chunks[0]["text"]
    assert "Rating: 3\nMeaning: Partly meets expectations" in table_chunks[0]["text"]
    assert [
        unit["text"] for unit in table_chunks[0]["retrieval_units"]
    ] == [
        "Rating: 1\nMeaning: Exceeds expectations",
        "Rating: 2\nMeaning: Meets expectations",
        "Rating: 3\nMeaning: Partly meets expectations",
    ]


def test_long_table_splits_only_between_rows_and_repeats_heading(tmp_path):
    source = tmp_path / "levels.md"
    row_values = [
        f"ROW_{index} " + (letter * 760)
        for index, letter in enumerate("ABCD", start=1)
    ]
    markdown = (
        "# Levels\n\n## Expected output\n\n"
        "| Level | Evidence |\n"
        "| --- | --- |\n"
        + "\n".join(
            f"| G{index} | {value} |"
            for index, value in enumerate(row_values, start=1)
        )
    )
    _write_markdown(source, markdown)

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(
        text,
        metadata["structured_blocks"],
        chunk_size=2048,
        overlap=160,
    )
    table_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("semantic_group_type") == "table"
    ]

    assert len(table_chunks) == 2
    assert all(
        chunk["text"].startswith("## Expected output")
        for chunk in table_chunks
    )
    assert all(chunk["semantic_split"] is True for chunk in table_chunks)
    for index, row_value in enumerate(row_values, start=1):
        complete_row = f"Level: G{index}\nEvidence: {row_value}"
        assert sum(
            complete_row in chunk["text"] for chunk in table_chunks
        ) == 1


def test_ordered_sibling_sections_are_one_semantic_unit(tmp_path):
    source = tmp_path / "feedback.md"
    _write_markdown(
        source,
        "# Feedback\n\n"
        "## What should feedback cover?\n\n"
        "### 第一件事：结果\n\n说明结果。\n\n"
        "### 第二件事：路径\n\n说明路径。\n\n"
        "### 第三件事：判断\n\n说明判断。\n\n"
        "### 第四件事：下一步\n\n说明下一步。\n",
    )

    text, metadata = StructuredMarkdownParser().parse(source)
    grouped_blocks = [
        block
        for block in metadata["structured_blocks"]
        if block.get("semantic_group_type") == "ordered_sections"
    ]
    chunks = chunk_blocks(text, metadata["structured_blocks"])
    semantic_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("semantic_group_type") == "ordered_sections"
    ]

    assert len({block["semantic_group_id"] for block in grouped_blocks}) == 1
    assert len({block["semantic_item_id"] for block in grouped_blocks}) == 4
    assert len(semantic_chunks) == 1
    assert semantic_chunks[0]["text"].startswith(
        "## What should feedback cover?"
    )
    assert semantic_chunks[0]["semantic_group_item_count"] == 4
    assert all(
        title in semantic_chunks[0]["text"]
        for title in (
            "### 第一件事：结果",
            "### 第二件事：路径",
            "### 第三件事：判断",
            "### 第四件事：下一步",
        )
    )
    assert len(semantic_chunks[0]["retrieval_units"]) == 4
    assert semantic_chunks[0]["retrieval_units"][3]["text"].startswith(
        "### 第四件事：下一步"
    )


def test_oversized_ordered_section_uses_natural_retrieval_leaves(tmp_path):
    source = tmp_path / "oversized-ordered-section.md"
    long_paragraph = "LONG_PARAGRAPH " + ("A" * 2600)
    second_paragraph = "SECOND_PARAGRAPH " + ("B" * 180)
    first_list_item = "FIRST_LIST_ITEM " + ("C" * 180)
    second_list_item = "SECOND_LIST_ITEM " + ("D" * 180)
    first_row = "FIRST_TABLE_ROW " + ("E" * 180)
    second_row = "SECOND_TABLE_ROW " + ("F" * 180)
    markdown = (
        "# Guide\n\n"
        "## Evidence\n\n"
        "### 1. Complete evidence\n\n"
        f"{long_paragraph}\n\n"
        f"{second_paragraph}\n\n"
        f"- {first_list_item}\n"
        f"- {second_list_item}\n\n"
        "| Level | Evidence |\n"
        "| --- | --- |\n"
        f"| G9 | {first_row} |\n"
        f"| SL1 | {second_row} |\n\n"
        "### 2. Follow-up\n\n"
        "Keep this item complete."
    )
    _write_markdown(source, markdown)

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(
        text,
        metadata["structured_blocks"],
        chunk_size=2048,
        overlap=160,
    )
    semantic_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("semantic_group_type") == "ordered_sections"
    ]
    oversized_chunk = next(
        chunk
        for chunk in semantic_chunks
        if "### 1. Complete evidence" in chunk["text"]
    )

    assert oversized_chunk["oversized_atomic"] is True
    assert len(oversized_chunk["text"]) > 2048
    for complete_content in (
        long_paragraph,
        second_paragraph,
        f"- {first_list_item}",
        f"- {second_list_item}",
        f"Level: G9\nEvidence: {first_row}",
        f"Level: SL1\nEvidence: {second_row}",
    ):
        assert oversized_chunk["text"].count(complete_content) == 1

    retrieval_units = oversized_chunk["retrieval_units"]
    assert len(retrieval_units) == 6
    assert [unit["unit_index"] for unit in retrieval_units] == list(range(6))
    assert all(
        unit["text"].startswith("### 1. Complete evidence\n\n")
        for unit in retrieval_units
    )
    assert long_paragraph in retrieval_units[0]["text"]
    assert len(retrieval_units[0]["text"]) > 2048
    assert second_paragraph not in retrieval_units[0]["text"]
    assert retrieval_units[1]["text"].endswith(second_paragraph)
    assert retrieval_units[2]["text"].endswith(f"- {first_list_item}")
    assert retrieval_units[3]["text"].endswith(f"- {second_list_item}")
    assert retrieval_units[4]["text"].endswith(
        f"Level: G9\nEvidence: {first_row}"
    )
    assert retrieval_units[5]["text"].endswith(
        f"Level: SL1\nEvidence: {second_row}"
    )
    assert len(
        {unit["source_start_line"] for unit in retrieval_units}
    ) == len(retrieval_units)

    all_unit_indexes = [
        unit["unit_index"]
        for chunk in semantic_chunks
        for unit in chunk["retrieval_units"]
    ]
    assert all_unit_indexes == list(range(7))


def test_multi_paragraph_parent_exposes_independent_retrieval_leaves(tmp_path):
    source = tmp_path / "paragraphs.md"
    first_paragraph = "The first paragraph explains the applicable context."
    second_paragraph = "The second paragraph defines the expected result."
    _write_markdown(
        source,
        "# Guide\n\n"
        "## Context\n\n"
        f"{first_paragraph}\n\n"
        f"{second_paragraph}",
    )

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(text, metadata["structured_blocks"])
    context_chunk = next(
        chunk for chunk in chunks if chunk.get("section") == "Context"
    )

    assert context_chunk["text"] == f"{first_paragraph}\n{second_paragraph}"
    assert [
        (unit["unit_type"], unit["unit_index"], unit["text"])
        for unit in context_chunk["retrieval_units"]
    ] == [
        ("paragraph", 0, first_paragraph),
        ("paragraph", 1, second_paragraph),
    ]
    assert context_chunk["retrieval_units"][0]["source_end_char"] <= (
        context_chunk["retrieval_units"][1]["source_start_char"]
    )


def test_single_term_definition_does_not_create_retrieval_leaf(tmp_path):
    source = tmp_path / "definition.md"
    definition = (
        "**Definition:**\n"
        "Performance consistently meets the role requirements."
    )
    _write_markdown(
        source,
        "# Performance\n\n## Achieved\n\n" + definition,
    )

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(text, metadata["structured_blocks"])
    achieved_chunk = next(
        chunk for chunk in chunks if chunk.get("section") == "Achieved"
    )

    assert achieved_chunk["text"] == definition
    assert "retrieval_units" not in achieved_chunk


def test_single_plain_term_definition_does_not_create_retrieval_leaf(tmp_path):
    source = tmp_path / "plain-definition.md"
    definition = "核心环节：目标设定与达成。"
    _write_markdown(
        source,
        "# Performance\n\n## Process\n\n" + definition,
    )

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(text, metadata["structured_blocks"])
    process_chunk = next(
        chunk for chunk in chunks if chunk.get("section") == "Process"
    )

    assert "retrieval_units" not in process_chunk


def test_dialogue_with_colon_is_not_treated_as_definition(tmp_path):
    source = tmp_path / "dialogue.md"
    dialogue = "我说：“这不是术语定义。”"
    _write_markdown(
        source,
        "# Guide\n\n## Example\n\n" + dialogue,
    )

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(text, metadata["structured_blocks"])
    example_chunk = next(
        chunk for chunk in chunks if chunk.get("section") == "Example"
    )

    assert "retrieval_units" not in example_chunk


def test_single_plain_paragraph_does_not_duplicate_parent_as_leaf(tmp_path):
    source = tmp_path / "single-paragraph.md"
    _write_markdown(
        source,
        "# Guide\n\n## Context\n\nOne standalone paragraph.",
    )

    text, metadata = StructuredMarkdownParser().parse(source)
    chunks = chunk_blocks(text, metadata["structured_blocks"])
    context_chunk = next(
        chunk for chunk in chunks if chunk.get("section") == "Context"
    )

    assert "retrieval_units" not in context_chunk


def test_fixed_child_chunk_defaults_apply_overlap_to_long_paragraph():
    text = "0123456789" * 500
    chunks = chunk_text(text)

    assert len(chunks[0]) == 2048
    assert chunks[0][-160:] == chunks[1][:160]
    assert all(len(chunk) <= 2048 for chunk in chunks)


def test_exact_facts_keep_codes_and_business_terms_deterministic():
    source = (
        "G9 uses Cross-functional experience and Collaboration to DELIVER, "
        "Innovation to SHAPE, Customer-centricity to GROW and Commitment to WIN. "
        "The applicable result is ASR rating."
    )
    facts = extract_exact_facts(source)
    identities = {
        (fact["fact_type"], fact["normalized_value"]) for fact in facts
    }

    assert ("business_code", "g9") in identities
    assert ("business_code", "asr") in identities
    assert ("career_element", "cross-functional experience") in identities
    assert ("culture", "collaboration to deliver") in identities
    assert ("culture", "innovation to shape") in identities
    assert ("culture", "customer-centricity to grow") in identities
    assert ("culture", "commitment to win") in identities
    assert ("culture", "future and result focus") not in identities
    assert ("performance", "asr rating") in identities
    assert exact_query_terms("请查询 G9 的 ASR rating") == [
        "g9",
        "asr",
        "asr rating",
    ]
    assert exact_query_terms("Mercer P4 与 M2 岗位要求") == ["p4", "m2"]
    assert normalize_content("  Ｇ9\n标准 ") == "g9 标准"
    assert canonical_content_id("A  B") == canonical_content_id("a\n b")


@pytest.mark.parametrize(
    ("name_zh", "canonical"),
    [
        ("协同共进", "collaboration to deliver"),
        ("创变未来", "innovation to shape"),
        ("聚力共赢", "customer-centricity to grow"),
        ("使命必达", "commitment to win"),
    ],
)
def test_exact_facts_map_china_culture_names_to_current_canonical_terms(
    name_zh,
    canonical,
):
    identities = {
        (fact["fact_type"], fact["normalized_value"])
        for fact in extract_exact_facts(name_zh, metadata={"scope": "culture"})
    }

    assert ("culture", canonical) in identities


def test_child_unit_exact_facts_include_its_source_heading():
    source = contextual_exact_fact_text(
        "主动聆听客户或用户并识别深层需求。",
        metadata={
            "heading_path": [
                "我们的中国区高绩效文化",
                "Customer-centricity to GROW",
            ]
        },
    )

    identities = {
        (fact["fact_type"], fact["normalized_value"])
        for fact in extract_exact_facts(source, metadata={"scope": "culture"})
    }

    assert ("culture", "customer-centricity to grow") in identities


def test_exact_facts_cover_source_career_performance_and_organization_terms():
    facts = extract_exact_facts(
        "Perspective Experience includes Cross Division and Cross Function. "
        "绩效评级由 WHAT 维度与 HOW 维度共同支持。",
        metadata={
            "department_path": "Bosch > Mobility > Engineering",
        },
    )
    identities = {
        (fact["fact_type"], fact["normalized_value"]) for fact in facts
    }

    assert (
        "career_element",
        "cross-divisional experience",
    ) in identities
    assert (
        "career_element",
        "cross-functional experience",
    ) in identities
    assert ("performance", "performance rating") in identities
    assert ("performance", "what dimension") in identities
    assert ("performance", "how dimension") in identities
    assert (
        "organization_path",
        "bosch > mobility > engineering",
    ) in identities
    assert ("organization_unit", "bosch") in identities
    assert ("organization_unit", "mobility") in identities
    assert ("organization_unit", "engineering") in identities


def test_standalone_what_how_are_performance_facts_only_in_performance_scope():
    performance_facts = extract_exact_facts(
        "WHAT and HOW",
        metadata={"scope": "performance"},
    )
    general_facts = extract_exact_facts(
        "WHAT and HOW",
        metadata={"scope": "general"},
    )

    assert {
        (fact["fact_type"], fact["normalized_value"])
        for fact in performance_facts
    } >= {
        ("performance", "what dimension"),
        ("performance", "how dimension"),
    }
    assert not any(
        fact["fact_type"] == "performance"
        for fact in general_facts
    )
    assert exact_query_terms("WHAT and HOW") == [
        "what",
        "how",
        "how dimension",
        "what dimension",
    ]


def test_index_manager_rejects_unprocessed_source_tree(tmp_path):
    raw_root = tmp_path / "kb_raw"
    raw_source = raw_root / "general" / "raw.pdf"
    raw_source.parent.mkdir(parents=True)
    raw_source.write_bytes(b"not processed")

    with pytest.raises(UnsupportedFileTypeError, match="raw.pdf"):
        IndexManager._validate_source_tree(raw_root)


def test_index_manager_builds_v3_children_and_complete_sections(tmp_path):
    raw_root = tmp_path / "kb_raw"
    source = raw_root / "job_level" / "job_level.md"
    _write_markdown(
        source,
        "# Job level\n\n## G9\n\n" + ("职责与影响。" * 600),
    )
    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(
        kb_index_version="v3",
        kb_chunk_size=2048,
        kb_chunk_overlap=160,
        effective_embedding_model="Qwen/Qwen3-Embedding-4B",
        embedding_dimensions=2560,
        document_vision_model=None,
        collection_name_for_scope=lambda scope: f"hr_agent_kb_{scope}",
    )
    manager.parser = ParserRouter()

    chunks_by_scope, documents = manager._build_chunks(raw_root)
    chunks = chunks_by_scope["job_level"]
    document = documents[0]

    assert document["doc_id"].startswith("v3__")
    assert document["_sections"]
    assert max(len(section["text"]) for section in document["_sections"]) > 2048
    assert all(chunk.metadata["index_version"] == "v3" for chunk in chunks)
    assert all(chunk.metadata["parent_section_id"] for chunk in chunks)
    assert all(
        chunk.metadata["search_text"].startswith("Knowledge scope: job_level")
        for chunk in chunks
    )
    assert all(len(chunk.text) <= 2048 for chunk in chunks)


def test_index_manager_propagates_atomic_structure_metadata(tmp_path):
    raw_root = tmp_path / "kb_raw"
    source = raw_root / "feedback" / "questions.md"
    _write_markdown(
        source,
        "# Feedback\n\n"
        "## Questions\n\n"
        "1. What result was achieved?\n"
        "2. Which path produced it?\n"
        "3. What standard applies?\n"
        "4. What happens next?\n",
    )
    manager = object.__new__(IndexManager)
    manager.settings = SimpleNamespace(
        kb_index_version="v3",
        kb_chunk_size=2048,
        kb_chunk_overlap=160,
        effective_embedding_model="Qwen/Qwen3-Embedding-4B",
        embedding_dimensions=2560,
        document_vision_model=None,
        collection_name_for_scope=lambda scope: f"hr_agent_kb_{scope}",
    )
    manager.parser = ParserRouter()

    chunks_by_scope, documents = manager._build_chunks(raw_root)
    list_chunk = next(
        chunk
        for chunk in chunks_by_scope["feedback"]
        if chunk.metadata.get("semantic_group_type") == "list"
    )

    assert list_chunk.metadata["atomic_structure"] is True
    assert list_chunk.metadata["semantic_group_item_count"] == 4
    assert list_chunk.metadata["semantic_split"] is False
    assert list_chunk.metadata["oversized_atomic"] is False
    assert list_chunk.metadata["parent_section_id"]
    retrieval_units = documents[0]["_retrieval_units"]
    assert len(retrieval_units) == 4
    assert all(
        unit["parent_chunk_id"] == list_chunk.chunk_id
        for unit in retrieval_units
    )
    assert any(
        fact["normalized_value"] == "what"
        for fact in retrieval_units[2]["exact_facts"]
    )
    assert retrieval_units[2]["search_text"].endswith(
        "3. What standard applies?"
    )
    assert list_chunk.text.startswith("## Questions")
    assert documents[0]["parse_metadata"]["parser_version"] == (
        "structured-markdown-v4"
    )


def test_postgres_schema_contains_structured_sections_facts_and_search_text():
    statements: list[str] = []

    class Cursor:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class Connection:
        def execute(self, statement, params=None):
            statements.append(str(statement))
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    repository = object.__new__(PostgresRepository)
    repository.connection = connection
    repository.init_schema()

    schema_sql = "\n".join(statements)
    assert "search_text TEXT NOT NULL" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS kb_sections" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS kb_exact_facts" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS kb_retrieval_units" in schema_sql
    assert (
        "CREATE TABLE IF NOT EXISTS kb_retrieval_unit_exact_facts"
        in schema_sql
    )
    assert "document_frequency INTEGER NOT NULL DEFAULT 1" in schema_sql
    assert (
        "CREATE TABLE IF NOT EXISTS kb_retrieval_unit_embeddings"
        in schema_sql
    )
    assert "idx_kb_exact_facts_lookup" in schema_sql
    assert "idx_kb_exact_facts_global_lookup" in schema_sql
    assert "2026-08-21-structured-kb-v3" in schema_sql
    assert "2026-08-22-retrieval-units-v1" in schema_sql
    assert "2026-08-23-unit-exact-facts-v1" in schema_sql
    assert "DELETE FROM kb_exact_facts WHERE fact_type = %s" in schema_sql
    assert "DELETE FROM kb_exact_facts\n" not in schema_sql


def test_culture_exact_fact_policy_migration_is_scoped_and_idempotent():
    statements: list[tuple[str, tuple | None]] = []
    migration_exists = False

    class Cursor:
        def fetchone(self):
            return {"version": "done"} if migration_exists else None

    class Connection:
        def execute(self, statement, params=None):
            statements.append(
                (str(statement), tuple(params) if params else None)
            )
            return Cursor()

    repository = object.__new__(PostgresRepository)
    rebuilds: list[bool] = []
    repository._rebuild_exact_fact_indexes_on_connection = (
        lambda conn: rebuilds.append(True)
    )
    connection = Connection()

    assert repository._migrate_exact_fact_policy_on_connection(connection)
    assert rebuilds == [True]
    assert any(
        "DELETE FROM kb_exact_facts WHERE fact_type = %s" in statement
        and params == ("culture",)
        for statement, params in statements
    )
    assert all(
        "DELETE FROM kb_chunks" not in statement
        and "DELETE FROM kb_chunk_embeddings" not in statement
        for statement, _ in statements
    )

    migration_exists = True
    statements.clear()
    assert not repository._migrate_exact_fact_policy_on_connection(connection)
    assert len(statements) == 1
    assert rebuilds == [True]


def test_retrieval_unit_replacement_persists_source_backed_exact_facts():
    statements: list[tuple[str, tuple | None]] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append(
                (str(statement), tuple(params) if params else None)
            )
            return SimpleNamespace(rowcount=1)

    repository = object.__new__(PostgresRepository)
    count = repository.replace_retrieval_units(
        Connection(),
        retrieval_units=[
            {
                "unit_id": "ru-g9",
                "parent_chunk_id": "parent-levels",
                "doc_id": "job-levels",
                "collection_name": "kb_job_level",
                "scope": "job_level",
                "unit_type": "table",
                "unit_index": 0,
                "text": "Level: G9",
                "search_text": "Job level table\nLevel: G9",
                "metadata": {},
                "index_version": "v3",
                "content_hash": "hash",
                "exact_facts": [
                    {
                        "fact_type": "business_code",
                        "normalized_value": "g9",
                        "source_quote": "G9",
                    }
                ],
            }
        ],
        refresh_document_frequencies=False,
    )

    exact_insert = next(
        (sql, params)
        for sql, params in statements
        if "INSERT INTO kb_retrieval_unit_exact_facts" in sql
    )
    assert count == 1
    assert exact_insert[1] == (
        "ru-g9",
        "parent-levels",
        "business_code",
        "g9",
        "G9",
    )


def test_retrieval_unit_exact_df_uses_global_parent_chunk_frequency():
    statements: list[str] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append(str(statement))
            return SimpleNamespace(rowcount=3)

    updated = PostgresRepository.refresh_retrieval_unit_exact_fact_document_frequencies(
        Connection()
    )
    sql = statements[0]

    assert updated == 3
    assert "COUNT(DISTINCT chunk_id)" in sql
    assert "FROM kb_exact_facts" in sql
    assert "collection_name" not in sql
    assert "GROUP BY fact_type, normalized_value" in sql


def test_child_retrieval_match_collapses_to_complete_parent_for_reranker():
    parent = {
        "chunk_id": "parent-1",
        "source_id": "job_level/levels.md",
        "title": "Job levels",
        "scope": "job_level",
        "text": "## Levels\n\nLevel: G9\nOutput: broad impact\n\nLevel: SL1\nOutput: business responsibility",
        "metadata": {"heading_path": ["Levels"]},
        "score": 0.81,
    }
    focused = {
        **parent,
        "metadata": {
            "heading_path": ["Levels"],
            "search_text": (
                "Knowledge scope: job_level\nDocument: Job levels\n"
                "Section: Levels\nLevel: G9\nOutput: broad impact"
            ),
            "retrieval_unit": {
                "unit_id": "ru-g9",
                "unit_type": "table",
                "unit_index": 0,
                "text": "Level: G9\nOutput: broad impact",
            },
        },
        "score": 0.93,
    }

    collapsed = PGVectorClient._collapse_parent_rows(
        [parent, focused],
        top_k=8,
    )
    chunk = PGVectorClient._row_to_chunk(collapsed[0])

    assert len(collapsed) == 1
    assert chunk.chunk_id == "parent-1"
    assert "Level: SL1" in chunk.text
    assert Reranker._document_text(chunk).endswith(
        "Level: G9\nOutput: broad impact"
    )


def test_parent_collapse_keeps_at_most_eight_unique_retrieval_units():
    parent = {
        "chunk_id": "parent-1",
        "source_id": "career/career.md",
        "title": "Career Elements",
        "scope": "career",
        "text": "complete parent text",
        "metadata": {"heading_path": ["Career Elements"]},
        "score": 0.5,
    }
    unit_rows = []
    for index in range(10):
        unit_rows.append(
            {
                **parent,
                "metadata": {
                    "heading_path": ["Career Elements"],
                    "search_text": f"context for unit {index}",
                    "retrieval_unit": {
                        "unit_id": f"unit-{index}",
                        "unit_type": "list",
                        "unit_index": index,
                        "text": f"unit text {index}",
                    },
                },
                "score": 1.0 - index / 100,
            }
        )
    unit_rows.append({**unit_rows[0], "score": 0.1})

    collapsed = PGVectorClient._collapse_parent_rows(
        [parent, *unit_rows],
        top_k=8,
    )
    chunk = PGVectorClient._row_to_chunk(collapsed[0])
    units = chunk.metadata["retrieval_units"]

    assert len(collapsed) == 1
    assert chunk.text == "complete parent text"
    assert len(units) == 8
    assert [unit["unit_id"] for unit in units] == [
        f"unit-{index}" for index in range(8)
    ]
    assert len({unit["unit_id"] for unit in units}) == 8
    assert "context for unit 0" in chunk.metadata["search_text"]
    assert "context for unit 8" not in chunk.metadata["search_text"]


def test_materialized_units_have_parent_offsets_and_ordered_neighbors():
    parent_text = (
        "## Standards\n\n"
        "### Expected output\n\n"
        "G9 delivers broad impact.\n\n"
        "SL1 owns business outcomes."
    )
    units = IndexManager._materialize_retrieval_units(
        raw_units=[
            {
                "unit_type": "paragraph",
                "unit_index": 0,
                "semantic_group_id": "standards",
                "semantic_group_title": "Standards",
                "semantic_item_id": "expected-output",
                "text": (
                    "### Expected output\n\n"
                    "G9 delivers broad impact."
                ),
                "heading_path": ["Standards"],
            },
            {
                "unit_type": "paragraph",
                "unit_index": 1,
                "semantic_group_id": "standards",
                "semantic_group_title": "Standards",
                "semantic_item_id": "expected-output",
                "text": (
                    "### Expected output\n\n"
                    "SL1 owns business outcomes."
                ),
                "heading_path": ["Standards"],
            },
        ],
        parent_chunk_id="parent-standards",
        parent_text=parent_text,
        doc_id="standards-doc",
        collection_name="kb_job_level",
        scope="job_level",
        title="Role standards",
        index_version="v3",
        content_hash="hash",
    )

    assert len(units) == 2
    first_metadata = units[0]["metadata"]
    second_metadata = units[1]["metadata"]
    assert parent_text[
        first_metadata["parent_text_start"]:
        first_metadata["parent_text_end"]
    ] == units[0]["text"]
    assert parent_text[
        second_metadata["parent_text_start"]:
        second_metadata["parent_text_end"]
    ] == "SL1 owns business outcomes."
    assert first_metadata["semantic_item_id"] == "expected-output"
    assert first_metadata["previous_unit_id"] is None
    assert first_metadata["next_unit_id"] == units[1]["unit_id"]
    assert first_metadata["next_unit_text"] == units[1]["text"]
    assert second_metadata["previous_unit_id"] == units[0]["unit_id"]
    assert second_metadata["previous_unit_text"] == units[0]["text"]
    assert second_metadata["next_unit_id"] is None


def test_parent_collapse_removes_overlapping_units_in_same_semantic_group():
    def row(unit_id: str, start: int, end: int, score: float):
        return {
            "chunk_id": "parent-1",
            "source_id": "career/career.md",
            "title": "Career",
            "scope": "career",
            "text": "complete parent",
            "metadata": {
                "retrieval_unit": {
                    "unit_id": unit_id,
                    "unit_type": "paragraph",
                    "unit_index": start,
                    "text": f"evidence {unit_id}",
                    "metadata": {
                        "semantic_group_id": "group-1",
                        "parent_text_start": start,
                        "parent_text_end": end,
                    },
                }
            },
            "score": score,
        }

    collapsed = PGVectorClient._collapse_parent_rows(
        [
            row("unit-primary", 10, 40, 0.95),
            row("unit-overlap", 12, 38, 0.90),
            row("unit-distinct", 50, 80, 0.85),
        ],
        top_k=8,
    )
    units = PGVectorClient._row_to_chunk(
        collapsed[0]
    ).metadata["retrieval_units"]

    assert [unit["unit_id"] for unit in units] == [
        "unit-primary",
        "unit-distinct",
    ]


def test_retrieval_unit_candidate_pool_caps_each_parent_before_global_limit():
    def row(parent_id: str, unit_index: int, score: float):
        return {
            "chunk_id": parent_id,
            "metadata": {
                "retrieval_unit": {
                    "unit_id": f"{parent_id}-unit-{unit_index}",
                    "unit_type": "list",
                    "unit_index": unit_index,
                    "text": f"{parent_id} evidence {unit_index}",
                    "metadata": {
                        "semantic_group_id": parent_id,
                        "parent_text_start": unit_index * 10,
                        "parent_text_end": unit_index * 10 + 5,
                    },
                }
            },
            "score": score,
        }

    rows = [
        row("large-parent", index, 1.0 - index / 100)
        for index in range(12)
    ]
    rows.append(row("other-parent", 0, 0.5))

    diversified = PGVectorClient._diversify_retrieval_unit_rows(
        rows,
        top_k=2,
    )

    assert sum(
        item["chunk_id"] == "large-parent"
        for item in diversified
    ) == 8
    assert any(
        item["chunk_id"] == "other-parent"
        for item in diversified
    )


def test_child_bm25_failure_keeps_complete_parent_bm25_result():
    statements: list[str] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append(str(statement))
            return SimpleNamespace(fetchall=lambda: [])

    class Repository:
        def __init__(self):
            self.connection_count = 0

        @contextmanager
        def connection(self):
            self.connection_count += 1
            yield Connection()

        @staticmethod
        def vector_literal(vector):
            return "[1.0,0.0]"

        @staticmethod
        def retrieval_unit_bm25_index_name():
            return "idx_kb_retrieval_units_bm25"

        @staticmethod
        def retrieval_unit_bm25_tokenizer_name():
            return "kb_jieba_v1_ru"

    parent = {
        "chunk_id": "parent-1",
        "source_id": "job_level/levels.md",
        "title": "Job levels",
        "scope": "job_level",
        "text": "## Levels\n\nLevel: G9\nOutput: broad impact",
        "metadata": {},
        "score": 0.81,
    }
    client = object.__new__(PGVectorClient)
    client.collection_name = "kb_job_level"
    client.repo = Repository()
    client.settings = SimpleNamespace(
        postgres_create_hnsw_index=False,
        postgres_bm25_limit=200,
        rag_hybrid_search_enabled=True,
    )
    client._active_embedding_identity = lambda dimension: (  # type: ignore[method-assign]
        "profile",
        "00000000-0000-0000-0000-000000000001",
    )
    client._dense_rows = lambda conn, **kwargs: []  # type: ignore[method-assign]
    client._dense_retrieval_unit_rows = (  # type: ignore[method-assign]
        lambda conn, **kwargs: []
    )
    client._lexical_rows = (  # type: ignore[method-assign]
        lambda conn, **kwargs: [parent]
    )

    def fail_child_bm25(conn, **kwargs):
        raise RuntimeError("child index is not ready")

    client._lexical_retrieval_unit_rows = (  # type: ignore[method-assign]
        fail_child_bm25
    )

    result = client.search_hybrid_by_embedding(
        "G9",
        [1.0, 0.0],
        scope="job_level",
        exact_enabled=False,
    )

    assert [chunk.chunk_id for chunk in result.lexical] == ["parent-1"]
    assert result.lexical[0].text == parent["text"]
    assert result.lexical_error is None
    assert client.repo.connection_count == 3
    assert not any("SAVEPOINT" in statement for statement in statements)


def test_parent_bm25_failure_does_not_abort_dense_or_exact_retrieval():
    class Connection:
        def execute(self, statement, params=None):
            return SimpleNamespace(fetchall=lambda: [])

    class Repository:
        def __init__(self):
            self.connection_count = 0

        @contextmanager
        def connection(self):
            self.connection_count += 1
            yield Connection()

        @staticmethod
        def vector_literal(vector):
            return "[1.0,0.0]"

    parent = {
        "chunk_id": "parent-1",
        "source_id": "employee/organization_unit.md",
        "title": "Organization unit",
        "scope": "employee",
        "text": "Organization context",
        "metadata": {},
        "score": 0.91,
    }
    client = object.__new__(PGVectorClient)
    client.collection_name = "kb_employee"
    client.repo = Repository()
    client.settings = SimpleNamespace(
        postgres_create_hnsw_index=False,
        postgres_bm25_limit=200,
        rag_hybrid_search_enabled=True,
    )
    client._active_embedding_identity = lambda dimension: (  # type: ignore[method-assign]
        "profile",
        "00000000-0000-0000-0000-000000000001",
    )
    client._dense_rows = lambda conn, **kwargs: [parent]  # type: ignore[method-assign]
    client._dense_retrieval_unit_rows = (  # type: ignore[method-assign]
        lambda conn, **kwargs: []
    )
    client._exact_rows = lambda conn, **kwargs: [parent]  # type: ignore[method-assign]

    def fail_parent_bm25(conn, **kwargs):
        raise RuntimeError("corrupt parent BM25 index")

    client._lexical_rows = fail_parent_bm25  # type: ignore[method-assign]

    result = client.search_hybrid_by_embedding(
        "organization context",
        [1.0, 0.0],
        scope="employee",
    )

    assert [chunk.chunk_id for chunk in result.dense] == ["parent-1"]
    assert [chunk.chunk_id for chunk in result.exact] == ["parent-1"]
    assert result.lexical == []
    assert result.lexical_error == (
        "RuntimeError: corrupt parent BM25 index"
    )
    assert client.repo.connection_count == 2


def test_parent_collapse_preserves_existing_order_for_equal_scores():
    rows = [
        {"chunk_id": "parent-b", "metadata": {}, "score": 0.8},
        {"chunk_id": "parent-a", "metadata": {}, "score": 0.8},
    ]

    collapsed = PGVectorClient._collapse_parent_rows(rows, top_k=8)

    assert [row["chunk_id"] for row in collapsed] == [
        "parent-b",
        "parent-a",
    ]


def test_structured_storage_writes_complete_parent_without_truncation():
    statements: list[tuple[str, tuple | None]] = []

    class Connection:
        def execute(self, statement, params=None):
            statements.append((str(statement), tuple(params) if params else None))
            return self

    parent_text = "完整章节。" * 50_000
    chunk = RetrievedChunk(
        chunk_id="chunk-1",
        source_id="career/career.md",
        title="Career",
        scope="career",
        text="Cross-functional experience",
        metadata={
            "doc_id": "v3__career",
            "parent_section_id": "section-1",
            "index_version": "v3",
            "exact_facts": [
                {
                    "fact_type": "career_element",
                    "normalized_value": "cross-functional experience",
                    "display_value": "Cross-functional experience",
                    "source_quote": "Cross-functional experience",
                }
            ],
        },
    )
    document = {
        "doc_id": "v3__career",
        "relative_path": "career/career.md",
        "scope": "career",
        "index_version": "v3",
        "_sections": [
            {
                "section_id": "section-1",
                "source_id": "career/career.md",
                "scope": "career",
                "index_version": "v3",
                "title": "Career Elements",
                "level": 1,
                "heading_path": ["Career Elements"],
                "text": parent_text,
            }
        ],
    }

    repository = object.__new__(PostgresRepository)
    repository.replace_knowledge_structure(
        Connection(),
        document=document,
        chunks=[chunk],
    )

    section_params = next(
        params
        for sql, params in statements
        if "INSERT INTO kb_sections" in sql
    )
    assert section_params is not None
    assert section_params[14] == parent_text
    assert len(section_params[14]) > 200_000
    assert any("INSERT INTO kb_exact_facts" in sql for sql, _ in statements)


def test_parent_context_hydration_preserves_rank_and_complete_text():
    parent_text = "# G9\n\n" + ("完整章节。" * 1000)
    chunks = [
        RetrievedChunk(
            chunk_id="c1",
            source_id="job_level/job_level.md",
            title="job_level",
            scope="job_level",
            text="G9 产出要求",
            score=0.91,
            metadata={"parent_section_id": "s1"},
        ),
        RetrievedChunk(
            chunk_id="c2",
            source_id="career/career.md",
            title="career",
            scope="career",
            text="Cross-functional experience",
            score=0.72,
            metadata={"parent_section_id": "s2"},
        ),
    ]

    class Repository:
        def fetch_chunk_parent_contexts(self, chunk_ids):
            assert chunk_ids == ["c1", "c2"]
            return {
                "c1": {
                    "section_id": "s1",
                    "title": "G9",
                    "heading_path": ["Job level", "G9"],
                    "text": parent_text,
                    "source_start_line": 1,
                    "source_end_line": 300,
                }
            }

    service = object.__new__(RetrievalService)
    service.repository = Repository()
    hydrated = service._hydrate_parent_contexts(chunks)

    assert [chunk.chunk_id for chunk in hydrated] == ["c1", "c2"]
    assert [chunk.score for chunk in hydrated] == [0.91, 0.72]
    assert hydrated[0].metadata["parent_context"] == parent_text
    assert hydrated[0].metadata["parent_heading_path"] == ["Job level", "G9"]
    assert "parent_context" not in chunks[0].metadata
    assert hydrated[1].metadata == chunks[1].metadata
    assert RetrievalService._needs_parent_context("guidance_plan") is True
    assert RetrievalService._needs_parent_context(
        "development_plan_evaluation"
    ) is True
    assert RetrievalService._needs_parent_context("employee_response") is False


def test_job_level_generation_context_projects_only_allowed_table_rows():
    parent_text = (
        "# Bosch 职级体系（G6 - SL2）\n\n"
        "| 职级 | 绩效等级 | 等级依据 |\n"
        "| --- | :---: | --- |\n"
        "| G9 | High | 主导重点项目，能力接近 SL1 层级 |\n"
        "| G9 | Medium | 承担复杂专项任务 |\n"
        "| SL1 | High | 开拓新业务 |\n"
        "| SL1 | Medium | 全盘负责业务板块 |\n"
        "| SL2 | High | 引领赛道业务方向 |"
    )

    projected = select_job_level_generation_context(parent_text, ["sl1"])

    assert projected == (
        "| 职级 | 绩效等级 | 等级依据 |\n"
        "| --- | :---: | --- |\n"
        "| SL1 | High | 开拓新业务 |\n"
        "| SL1 | Medium | 全盘负责业务板块 |"
    )
    assert parent_text.startswith("# Bosch 职级体系")
    assert "G9 | High" in parent_text
    assert "SL2 | High" in parent_text


def test_parent_hydration_projects_job_level_only_for_generation():
    parent_text = (
        "| 职级 | 绩效等级 | 等级依据 |\n"
        "| --- | --- | --- |\n"
        "| G9 | High | 能力接近 SL1 |\n"
        "| SL1 | Medium | 全盘负责业务板块 |\n"
        "| SL2 | High | 引领赛道业务方向 |"
    )
    chunk = RetrievedChunk(
        chunk_id="job-level-table",
        source_id="job_level/job_level.md",
        title="job_level",
        scope="job_level",
        text=parent_text,
        score=0.9,
        metadata={"atomic_structure": True},
    )

    class Repository:
        def fetch_chunk_parent_contexts(self, _chunk_ids):
            return {
                chunk.chunk_id: {
                    "title": "job_level",
                    "text": parent_text,
                    "source_start_char": 0,
                    "source_end_char": len(parent_text),
                }
            }

    service = object.__new__(RetrievalService)
    service.repository = Repository()
    hydrated = service._hydrate_parent_contexts(
        [chunk],
        applicable_job_levels=("SL1",),
    )[0]

    assert hydrated.metadata["parent_context"] == parent_text
    generation_text = generation_context_text(
        hydrated.text,
        hydrated.metadata,
    )
    assert "| SL1 | Medium |" in generation_text
    assert "| G9 | High |" not in generation_text
    assert "| SL2 | High |" not in generation_text
    assert hydrated.metadata["generation_context_job_levels"] == ["SL1"]


def test_job_level_generation_context_uses_designated_level_column():
    parent_text = (
        "| Position | Job level | Expected output |\n"
        "| --- | --- | --- |\n"
        "| Advanced Expert | G9 | Demonstrates readiness for SL1 |\n"
        "| Senior Expert | SL1 | Shapes company direction |\n"
        "| Chief Expert | SL2 | Creates strategic value |"
    )

    projected = select_job_level_generation_context(parent_text, ["SL1"])

    assert "Senior Expert | SL1" in projected
    assert "Advanced Expert | G9" not in projected
    assert "Chief Expert | SL2" not in projected


def test_job_level_generation_context_supports_current_and_next_levels():
    parent_text = (
        "| 岗位职级 | 要求 |\n"
        "| --- | --- |\n"
        "| G8 | 独立负责业务模块 |\n"
        "| G9 | 影响部门成果 |\n"
        "| SL1 | 影响公司战略 |"
    )

    projected = select_job_level_generation_context(
        parent_text,
        ["G8", "g9", "unknown"],
    )

    assert "| G8 | 独立负责业务模块 |" in projected
    assert "| G9 | 影响部门成果 |" in projected
    assert "| SL1 | 影响公司战略 |" not in projected


@pytest.mark.parametrize(
    ("parent_text", "allowed_levels"),
    [
        ("SL1 的一般性说明，不是结构化表格。", ["SL1"]),
        (
            "| 绩效等级 | 等级依据 |\n| --- | --- |\n| High | SL1 要求 |",
            ["SL1"],
        ),
        (
            "| 职级 | 要求 |\n| --- | --- |\n| G9 | 复杂专项 |",
            ["SL1"],
        ),
        (
            "| 职级 | 要求 |\n| --- | --- |\n| G9 | 复杂专项 |",
            [],
        ),
    ],
)
def test_job_level_generation_context_skips_unsafe_projection(
    parent_text,
    allowed_levels,
):
    assert (
        select_job_level_generation_context(parent_text, allowed_levels) == ""
    )


def test_parent_context_hydration_failure_keeps_ranked_chunks():
    chunk = RetrievedChunk(
        chunk_id="c1",
        source_id="source",
        title="title",
        scope="general",
        text="evidence",
        score=0.5,
    )

    class Repository:
        def fetch_chunk_parent_contexts(self, _chunk_ids):
            raise RuntimeError("schema unavailable")

    service = object.__new__(RetrievalService)
    service.repository = Repository()

    assert service._hydrate_parent_contexts([chunk]) == [chunk]


def test_parent_context_hydration_deduplicates_generation_text_only():
    first_child = "第一条聚焦证据"
    second_child = "第二条聚焦证据"
    parent_text = (
        "# 共同父章节\n\n"
        f"{first_child}\n\n"
        f"{second_child}\n\n"
        "用于解释两条证据的共同上下文。"
    )
    chunks = [
        RetrievedChunk(
            chunk_id="shared-parent-first",
            source_id="culture/shared.md",
            title="Shared",
            scope="culture",
            text=first_child,
            score=0.9,
            metadata={"parent_section_id": "shared-section"},
        ),
        RetrievedChunk(
            chunk_id="shared-parent-second",
            source_id="culture/shared.md",
            title="Shared",
            scope="culture",
            text=second_child,
            score=0.8,
            metadata={"parent_section_id": "shared-section"},
        ),
    ]

    class Repository:
        def fetch_chunk_parent_contexts(self, chunk_ids):
            assert chunk_ids == [
                "shared-parent-first",
                "shared-parent-second",
            ]
            context = {
                "section_id": "shared-section",
                "title": "共同父章节",
                "heading_path": ["共同父章节"],
                "text": parent_text,
                "source_start_line": 1,
                "source_end_line": 7,
                "source_start_char": 0,
                "source_end_char": len(parent_text),
            }
            return {chunk_id: dict(context) for chunk_id in chunk_ids}

    service = object.__new__(RetrievalService)
    service.repository = Repository()

    hydrated = service._hydrate_parent_contexts(chunks)

    assert [chunk.chunk_id for chunk in hydrated] == [
        "shared-parent-first",
        "shared-parent-second",
    ]
    assert len(hydrated) == 2
    assert all(
        chunk.metadata["parent_context"] == parent_text
        for chunk in hydrated
    )
    assert "generation_context_override" not in hydrated[0].metadata
    assert (
        hydrated[1].metadata["generation_context_override"]
        == second_child
    )

    generation_texts = [
        generation_context_text(chunk.text, chunk.metadata)
        for chunk in hydrated
    ]
    assert generation_texts == [parent_text, second_child]
    assert len(set(generation_texts)) == len(generation_texts)

    # Prompt generation can use the focused override while citation matching
    # retains access to the complete source section.
    assert _chunk_source_text(hydrated[1]) == parent_text
    assert all("parent_context" not in chunk.metadata for chunk in chunks)
    assert all(
        "generation_context_override" not in chunk.metadata
        for chunk in chunks
    )


def test_parent_context_hydration_focuses_prose_around_all_reranked_units():
    first_rule = "第一步先用有效停顿，让对方把当前判断说完整。"
    middle_rule = "第四步进行释义，并核对自己是否理解准确。"
    last_rule = "第六步再用校准问题共同澄清下一步。"
    parent_text = (
        "# 谈判故事\n\n"
        + ("与当前任务无关的历史背景。" * 80)
        + "\n\n## 可直接使用的方法\n\n"
        + first_rule
        + "\n\n"
        + middle_rule
        + "\n\n"
        + last_rule
        + "\n\n"
        + ("无关的章节结尾。" * 80)
    )
    chunk = RetrievedChunk(
        chunk_id="focused-prose",
        source_id="emotion/methods.md",
        title="方法",
        scope="emotion",
        text=parent_text,
        score=0.9,
        metadata={
            "parent_section_id": "methods",
            "atomic_structure": False,
            "retrieval_units": [
                {"unit_type": "paragraph", "text": first_rule},
                {"unit_type": "paragraph", "text": last_rule},
            ],
            "retrieval_unit": {
                "unit_type": "paragraph",
                "text": first_rule,
            },
        },
    )

    class Repository:
        def fetch_chunk_parent_contexts(self, _chunk_ids):
            return {
                chunk.chunk_id: {
                    "section_id": "methods",
                    "title": "方法",
                    "heading_path": ["可直接使用的方法"],
                    "text": parent_text,
                    "source_start_line": 1,
                    "source_end_line": 20,
                    "source_start_char": 0,
                    "source_end_char": len(parent_text),
                }
            }

    service = object.__new__(RetrievalService)
    service.repository = Repository()
    hydrated = service._hydrate_parent_contexts([chunk])[0]
    generation_text = generation_context_text(
        hydrated.text,
        hydrated.metadata,
    )

    assert hydrated.metadata["parent_context"] == parent_text
    assert hydrated.metadata["generation_context_focused"] is True
    assert "## 可直接使用的方法" in generation_text
    assert first_rule in generation_text
    assert middle_rule in generation_text
    assert last_rule in generation_text
    assert "与当前任务无关的历史背景" not in generation_text
    assert "无关的章节结尾" not in generation_text


def test_generation_focus_uses_top_three_reranked_units_without_adjacency_guessing():
    first = "先确认对方当前的核心判断。"
    page_marker = "—— PDF 第 12 页 ——"
    empathy = "再用同理心承接对方尚未说完的感受。"
    fourth = "远端第四项不应扩大生成上下文。"
    parent_text = (
        ("无关开头。" * 100)
        + "\n\n## 方法\n\n"
        + first
        + "\n\n"
        + page_marker
        + "\n\n"
        + empathy
        + "\n\n"
        + ("无关中段。" * 100)
        + "\n\n"
        + fourth
        + "\n\n"
        + ("无关结尾。" * 100)
    )
    metadata = {
        "retrieval_units": [
            {
                "unit_id": "first",
                "unit_type": "paragraph",
                "text": first,
                "raw_rerank_score": 0.9,
            },
            {
                "unit_id": "marker",
                "unit_type": "paragraph",
                "text": page_marker,
                "raw_rerank_score": 0.8,
            },
            {
                "unit_id": "empathy",
                "unit_type": "paragraph",
                "text": empathy,
                "raw_rerank_score": 0.7,
            },
            {
                "unit_id": "fourth",
                "unit_type": "paragraph",
                "text": fourth,
                "raw_rerank_score": 0.6,
            },
        ],
        "retrieval_unit_rerank": {
            "best_unit_id": "first",
            "raw_rerank_score": 0.9,
        },
    }

    focused = select_generation_context_focus(parent_text, metadata)

    assert first in focused
    assert page_marker in focused
    assert empathy in focused
    assert fourth not in focused


def test_generation_focus_checks_protected_compounds_before_top_three_limit():
    rules = [f"规则 {index}：具体方法。" for index in range(1, 5)]
    parent_text = ("无关开头。" * 100) + "\n\n" + "\n\n".join(rules)
    metadata = {
        "retrieval_units": [
            {
                "unit_id": f"rule-{index}",
                "unit_type": "paragraph",
                "text": rule,
            }
            for index, rule in enumerate(rules[:3], start=1)
        ]
        + [
            {
                "unit_id": "complete-set",
                "unit_type": "semantic_compound",
                "text": rules[3],
                "metadata": {"evidence_set_complete": True},
            }
        ],
        "retrieval_unit_rerank": {"best_unit_id": "rule-1"},
    }

    assert select_generation_context_focus(parent_text, metadata) == ""


@pytest.mark.parametrize(
    "unsafe_unit_type",
    [
        "table",
        "list",
        "ordered_sections",
        "semantic_compound",
        "future_unknown_type",
    ],
)
def test_generation_focus_scans_all_units_for_structured_evidence(
    unsafe_unit_type,
):
    rules = [f"候选 {index} 的独立内容。" for index in range(1, 5)]
    parent_text = (
        ("无关背景。" * 100)
        + "\n\n"
        + "\n\n".join(rules)
        + "\n\n"
        + ("无关结尾。" * 100)
    )
    metadata = {
        "retrieval_units": [
            {
                "unit_id": f"unit-{index}",
                "unit_type": (
                    unsafe_unit_type if index == 4 else "paragraph"
                ),
                "text": rule,
                "raw_rerank_score": score,
            }
            for index, (rule, score) in enumerate(
                zip(rules, [0.9, 0.8, 0.7, 0.6]),
                start=1,
            )
        ],
        "retrieval_unit_rerank": {
            "best_unit_id": "unit-1",
            "raw_rerank_score": 0.9,
        },
    }

    assert select_generation_context_focus(parent_text, metadata) == ""


@pytest.mark.parametrize(
    "structured_text",
    [
        "| 要求 | 结果 |\n| --- | --- |\n| 倾听 | 已确认 |",
        "- 先倾听\n- 再共同澄清",
        "1. 先确认事实\n2. 再讨论下一步",
        "<table><tr><td>Career Element</td></tr></table>",
    ],
)
def test_generation_focus_rejects_structured_text_mislabeled_as_paragraph(
    structured_text,
):
    parent_text = (
        ("无关背景。" * 100)
        + "\n\n"
        + structured_text
        + "\n\n"
        + ("无关结尾。" * 100)
    )
    metadata = {
        "retrieval_units": [
            {
                "unit_id": "mislabeled-structure",
                "unit_type": "paragraph",
                "text": structured_text,
            }
        ]
    }

    assert select_generation_context_focus(parent_text, metadata) == ""


@pytest.mark.parametrize(
    "rerank_metadata",
    [
        None,
        "invalid metadata",
        {},
        {"best_unit_id": "unit-1"},
        {"best_unit_id": "unit-2", "raw_rerank_score": 0.9},
        {"best_unit_id": "unit-1", "raw_rerank_score": float("nan")},
        {"best_unit_id": "unit-1", "raw_rerank_score": 0.8},
    ],
)
def test_generation_focus_requires_trustworthy_metadata_before_truncating(
    rerank_metadata,
):
    rules = [f"候选 {index} 的独立内容。" for index in range(1, 5)]
    parent_text = (
        ("无关背景。" * 100)
        + "\n\n"
        + "\n\n".join(rules)
        + "\n\n"
        + ("无关结尾。" * 100)
    )
    metadata = {
        "retrieval_units": [
            {
                "unit_id": f"unit-{index}",
                "unit_type": "paragraph",
                "text": rule,
                "raw_rerank_score": score,
            }
            for index, (rule, score) in enumerate(
                zip(rules, [0.9, 0.8, 0.7, 0.6]),
                start=1,
            )
        ],
        "retrieval_unit_rerank": rerank_metadata,
    }

    assert select_generation_context_focus(parent_text, metadata) == ""


@pytest.mark.parametrize(
    "bad_score",
    [None, True, "not-a-score", float("nan"), float("inf"), float("-inf")],
)
def test_generation_focus_requires_finite_unit_scores_before_truncating(
    bad_score,
):
    rules = [f"候选 {index} 的独立内容。" for index in range(1, 5)]
    units = [
        {
            "unit_id": f"unit-{index}",
            "unit_type": "paragraph",
            "text": rule,
            "raw_rerank_score": score,
        }
        for index, (rule, score) in enumerate(
            zip(rules, [0.9, 0.8, 0.7, 0.6]),
            start=1,
        )
    ]
    units[1]["raw_rerank_score"] = bad_score
    metadata = {
        "retrieval_units": units,
        "retrieval_unit_rerank": {
            "best_unit_id": "unit-1",
            "raw_rerank_score": 0.9,
        },
    }
    parent_text = (
        ("无关背景。" * 100)
        + "\n\n"
        + "\n\n".join(rules)
        + "\n\n"
        + ("无关结尾。" * 100)
    )

    assert select_generation_context_focus(parent_text, metadata) == ""


@pytest.mark.parametrize(
    "scores",
    [
        [0.9, 0.6, 0.7, 0.5],
        [0.9, 0.8, 0.7, 0.7],
    ],
)
def test_generation_focus_rejects_untrusted_order_or_cutoff_tie(scores):
    rules = [f"候选 {index} 的独立内容。" for index in range(1, 5)]
    metadata = {
        "retrieval_units": [
            {
                "unit_id": f"unit-{index}",
                "unit_type": "paragraph",
                "text": rule,
                "raw_rerank_score": score,
            }
            for index, (rule, score) in enumerate(
                zip(rules, scores),
                start=1,
            )
        ],
        "retrieval_unit_rerank": {
            "best_unit_id": "unit-1",
            "raw_rerank_score": 0.9,
        },
    }
    parent_text = (
        ("无关背景。" * 100)
        + "\n\n"
        + "\n\n".join(rules)
        + "\n\n"
        + ("无关结尾。" * 100)
    )

    assert select_generation_context_focus(parent_text, metadata) == ""


@pytest.mark.parametrize(
    "raw_units",
    [
        "not a unit list",
        [
            {
                "unit_id": "unit-1",
                "unit_type": "paragraph",
                "text": "第一项",
            },
            None,
        ],
    ],
)
def test_generation_focus_falls_back_on_malformed_raw_units(raw_units):
    parent_text = ("无关背景。" * 100) + "\n\n第一项\n\n第二项"
    metadata = {
        "retrieval_units": raw_units,
        "retrieval_unit": {
            "unit_id": "fallback",
            "unit_type": "paragraph",
            "text": "第一项",
        },
    }

    assert select_generation_context_focus(parent_text, metadata) == ""


def test_generation_focus_falls_back_on_repeated_text_ambiguity():
    repeated = "这段证据在父文档中重复出现。"
    parent_text = (
        ("无关开头。" * 100)
        + "\n\n"
        + repeated
        + "\n\n"
        + ("无关中段。" * 100)
        + "\n\n"
        + repeated
        + "\n\n"
        + ("无关结尾。" * 100)
    )
    metadata = {
        "retrieval_units": [
            {
                "unit_id": "repeated",
                "unit_type": "paragraph",
                "text": repeated,
            }
        ]
    }

    assert select_generation_context_focus(parent_text, metadata) == ""


@pytest.mark.parametrize(
    "unsafe_unit",
    [
        {"unit_id": "missing", "unit_type": "paragraph", "text": ""},
        {
            "unit_id": "unmatched",
            "unit_type": "paragraph",
            "text": "父文档中不存在的内容",
        },
        {"unit_id": "table", "unit_type": "table", "text": "第二项"},
    ],
)
def test_generation_focus_falls_back_when_any_top_three_unit_is_unsafe(
    unsafe_unit,
):
    parent_text = ("无关背景。" * 100) + "\n\n第一项\n\n第二项\n\n第三项"
    metadata = {
        "retrieval_units": [
            {"unit_id": "first", "unit_type": "paragraph", "text": "第一项"},
            unsafe_unit,
            {"unit_id": "third", "unit_type": "paragraph", "text": "第三项"},
        ],
        "retrieval_unit_rerank": {"best_unit_id": "first"},
    }

    assert select_generation_context_focus(parent_text, metadata) == ""


def test_generation_focus_falls_back_when_best_unit_metadata_disagrees():
    parent_text = ("无关背景。" * 100) + "\n\n第一项\n\n第二项"
    metadata = {
        "retrieval_units": [
            {"unit_id": "first", "unit_type": "paragraph", "text": "第一项"},
            {"unit_id": "second", "unit_type": "paragraph", "text": "第二项"},
        ],
        "retrieval_unit_rerank": {"best_unit_id": "second"},
    }

    assert select_generation_context_focus(parent_text, metadata) == ""


def test_parent_context_hydration_keeps_atomic_semantic_group_complete():
    parent_text = (
        "| Category | Requirement |\n"
        "| --- | --- |\n"
        "| Cross-functional experience | 跨职能经历要求 |\n"
        "| International experience | 国际经历要求 |"
    )
    first_row = (
        "Category: Cross-functional experience | "
        "Requirement: 跨职能经历要求"
    )
    chunk = RetrievedChunk(
        chunk_id="career-table",
        source_id="career/career.md",
        title="Career Elements",
        scope="career",
        text=parent_text,
        score=0.9,
        metadata={
            "parent_section_id": "career-elements",
            "atomic_structure": True,
            "retrieval_units": [
                {"unit_type": "table", "text": first_row},
            ],
            "retrieval_unit": {
                "unit_type": "table",
                "text": first_row,
            },
        },
    )

    class Repository:
        def fetch_chunk_parent_contexts(self, _chunk_ids):
            return {
                chunk.chunk_id: {
                    "section_id": "career-elements",
                    "title": "Career Elements",
                    "heading_path": ["Career Elements"],
                    "text": parent_text,
                    "source_start_line": 1,
                    "source_end_line": 4,
                    "source_start_char": 0,
                    "source_end_char": len(parent_text),
                }
            }

    service = object.__new__(RetrievalService)
    service.repository = Repository()
    hydrated = service._hydrate_parent_contexts([chunk])[0]

    assert generation_context_text(hydrated.text, hydrated.metadata) == parent_text
    assert "generation_context_focused" not in hydrated.metadata


def test_parent_context_hydration_does_not_deduplicate_across_sources_or_scopes():
    parent_text = "# Shared wording\n\nThe same wording appears in distinct sources."
    chunks = [
        RetrievedChunk(
            chunk_id="culture-first",
            source_id="culture/first.md",
            title="First",
            scope="culture",
            text="same wording",
            score=0.9,
        ),
        RetrievedChunk(
            chunk_id="culture-second",
            source_id="culture/second.md",
            title="Second",
            scope="culture",
            text="same wording",
            score=0.8,
        ),
        RetrievedChunk(
            chunk_id="job-level-first",
            source_id="culture/first.md",
            title="Third",
            scope="job_level",
            text="same wording",
            score=0.7,
        ),
    ]

    class Repository:
        def fetch_chunk_parent_contexts(self, chunk_ids):
            return {
                chunk_id: {
                    "section_id": chunk_id,
                    "title": "Shared wording",
                    "heading_path": ["Shared wording"],
                    "text": parent_text,
                }
                for chunk_id in chunk_ids
            }

    service = object.__new__(RetrievalService)
    service.repository = Repository()

    hydrated = service._hydrate_parent_contexts(chunks)

    assert all(
        chunk.metadata["parent_context"] == parent_text
        for chunk in hydrated
    )
    assert all(
        "generation_context_override" not in chunk.metadata
        for chunk in hydrated
    )


def test_chunk_upsert_keeps_index_context_out_of_public_metadata():
    calls: list[tuple[str, tuple]] = []

    class Connection:
        def execute(self, statement, params=None):
            calls.append((str(statement), tuple(params or ())))
            return self

    class Repository:
        @staticmethod
        def vector_literal(vector):
            return "[" + ",".join(str(value) for value in vector) + "]"

    chunk = RetrievedChunk(
        chunk_id="chunk-1",
        source_id="career/career.md",
        title="Career Elements",
        scope="career",
        text="Cross-functional experience",
        metadata={
            "doc_id": "v3__career",
            "index_version": "v3",
            "parent_section_id": "section-1",
            "search_text": (
                "Knowledge scope: career\nCareer Elements\n"
                "Cross-functional experience"
            ),
            "exact_facts": [{"fact_type": "career_element"}],
        },
    )
    client = object.__new__(PGVectorClient)
    client.collection_name = "hr_agent_kb_career"
    client.repo = Repository()
    client.settings = SimpleNamespace(kb_index_version="v3")

    client._upsert_chunks_on_connection(
        Connection(),
        [chunk],
        [[0.1, 0.2]],
    )

    params = calls[0][1]
    stored_metadata = json.loads(params[8])
    assert params[7].startswith("Knowledge scope: career")
    assert stored_metadata["parent_section_id"] == "section-1"
    assert "search_text" not in stored_metadata
    assert "exact_facts" not in stored_metadata

@pytest.mark.asyncio
async def test_cached_guidance_result_hydrates_parent_without_mutating_cache():
    cached_chunk = RetrievedChunk(
        chunk_id="cached-child",
        source_id="career/career.md",
        title="Career Elements",
        scope="career",
        text="Cross-functional experience",
        score=0.8,
        metadata={"parent_section_id": "career-parent"},
    )

    class Cache:
        async def aget(self, _key):
            return [cached_chunk]

    service = object.__new__(RetrievalService)
    service.result_cache = Cache()
    service._build_plan = lambda agent_name, context, top_k: SimpleNamespace(
        agent_name="guidance_plan",
        cache_key="guidance-cache",
    )
    service._retrieval_admission_priority = lambda _context: 0
    service._log_cache_hit = lambda *_args: None

    async def hydrate(
        chunks,
        *,
        admission_priority,
        applicable_job_levels=(),
    ):
        assert admission_priority == 0
        assert applicable_job_levels == ()
        return [
            chunks[0].model_copy(
                deep=True,
                update={
                    "metadata": {
                        **chunks[0].metadata,
                        "parent_context": "完整 Career Elements 章节",
                    }
                },
            )
        ]

    service._ahydrate_parent_contexts = hydrate

    result = await service.aretrieve("guidance_plan", {})

    assert result[0].metadata["parent_context"] == "完整 Career Elements 章节"
    assert "parent_context" not in cached_chunk.metadata



def test_parent_context_hydration_slices_oversized_section_around_source_span():
    before = "BEFORE_START\n\n" + "\n\n".join(
        f"before paragraph {index} " + ("A" * 80) for index in range(80)
    )
    target = "RAW_TARGET_EVIDENCE for the selected employee level."
    after = "\n\n".join(
        f"after paragraph {index} " + ("Z" * 80) for index in range(80)
    ) + "\n\nAFTER_END"
    parent_text = f"{before}\n\n{target}\n\n{after}"
    target_start = parent_text.index(target)
    chunk = RetrievedChunk(
        chunk_id="oversized-child",
        source_id="job_level/job_level.md",
        title="G9",
        scope="job_level",
        text="Normalized table representation that is not in the raw parent.",
        score=0.9,
        metadata={
            "parent_section_id": "g9-parent",
            "source_start_char": target_start,
            "source_end_char": target_start + len(target),
        },
    )

    class Repository:
        def fetch_chunk_parent_contexts(self, _chunk_ids):
            return {
                chunk.chunk_id: {
                    "section_id": "g9-parent",
                    "title": "G9",
                    "heading_path": ["Job level", "G9"],
                    "text": parent_text,
                    "source_start_line": 1,
                    "source_end_line": 200,
                    "source_start_char": 0,
                    "source_end_char": len(parent_text),
                }
            }

    service = object.__new__(RetrievalService)
    service.repository = Repository()
    service.settings = SimpleNamespace(
        kb_parent_context_max_chars=1_200,
        kb_parent_context_boundary_scan_chars=160,
    )

    hydrated = service._hydrate_parent_contexts([chunk])
    metadata = hydrated[0].metadata

    assert target in metadata["parent_context"]
    assert len(metadata["parent_context"]) <= 1_200
    assert "BEFORE_START" not in metadata["parent_context"]
    assert "AFTER_END" not in metadata["parent_context"]
    assert metadata["parent_context_split"] is True
    assert metadata["parent_context_anchor_found"] is True
    assert metadata["parent_context_original_chars"] == len(parent_text)
    assert "parent_context" not in chunk.metadata


def test_parent_context_hydration_uses_child_when_oversized_parent_has_no_anchor():
    child_text = "Indexed child remains the safest citation context."
    parent_text = "unrelated paragraph\n\n" * 1_000
    chunk = RetrievedChunk(
        chunk_id="unmatched-child",
        source_id="general/general.md",
        title="General",
        scope="general",
        text=child_text,
        score=0.7,
        metadata={"parent_section_id": "general-parent"},
    )

    class Repository:
        def fetch_chunk_parent_contexts(self, _chunk_ids):
            return {
                chunk.chunk_id: {
                    "section_id": "general-parent",
                    "title": "General",
                    "heading_path": ["General"],
                    "text": parent_text,
                    "source_start_line": 1,
                    "source_end_line": 1_000,
                }
            }

    service = object.__new__(RetrievalService)
    service.repository = Repository()
    service.settings = SimpleNamespace(
        kb_parent_context_max_chars=1_000,
        kb_parent_context_boundary_scan_chars=100,
    )

    hydrated = service._hydrate_parent_contexts([chunk])
    metadata = hydrated[0].metadata

    assert metadata["parent_context"] == child_text
    assert metadata["parent_context_split"] is True
    assert metadata["parent_context_anchor_found"] is False


def test_guidance_and_coach_generation_use_bounded_parent_context():
    child_text = "small indexed child"
    parent_context = "complete but bounded parent section"
    chunk = RetrievedChunk(
        chunk_id="parent-child",
        source_id="career/career.md",
        title="Career",
        scope="career",
        text=child_text,
        score=0.9,
        metadata={
            "parent_context": parent_context,
            "parent_context_split": True,
        },
    )

    guidance_payload = GuidanceAgent._chunks_payload([chunk])
    coach_payload = GenericCoachAgent.chunks_payload([chunk])

    assert guidance_payload[0]["chunk_id"] == chunk.chunk_id
    assert guidance_payload[0]["text"] == parent_context
    assert coach_payload[0]["chunk_id"] == chunk.chunk_id
    assert coach_payload[0]["text"] == parent_context
    assert chunk.text == child_text


def test_guidance_and_coach_payloads_drop_only_exact_final_context_duplicates():
    shared_context = "同一来源中完全相同的最终生成上下文 ＡＢＣ。"
    chunks = [
        RetrievedChunk(
            chunk_id="best",
            source_id="job_level/job_level.md",
            title="Best",
            scope="job_level",
            text="best child",
            score=0.9,
            metadata={"generation_context_override": shared_context},
        ),
        RetrievedChunk(
            chunk_id="duplicate",
            source_id="job_level/job_level.md",
            title="Best",
            scope="job_level",
            text="different child",
            score=0.8,
            metadata={
                "generation_context_override": (
                    "同一来源中完全相同的最终生成上下文 ABC。"
                )
            },
        ),
        RetrievedChunk(
            chunk_id="different-scope",
            source_id="job_level/job_level.md",
            title="Different scope",
            scope="general",
            text="third child",
            score=0.7,
            metadata={"generation_context_override": shared_context},
        ),
        RetrievedChunk(
            chunk_id="different-title",
            source_id="job_level/job_level.md",
            title="Different title",
            scope="job_level",
            text="fourth child",
            score=0.6,
            metadata={"generation_context_override": shared_context},
        ),
        RetrievedChunk(
            chunk_id="different-whitespace",
            source_id="job_level/job_level.md",
            title="Best",
            scope="job_level",
            text="whitespace child",
            score=0.55,
            metadata={
                "generation_context_override": (
                    "同一来源中  完全相同的最终生成上下文 ABC。"
                )
            },
        ),
        RetrievedChunk(
            chunk_id="different-source-case",
            source_id="JOB_LEVEL/job_level.md",
            title="Best",
            scope="job_level",
            text="source case child",
            score=0.525,
            metadata={"generation_context_override": shared_context},
        ),
        RetrievedChunk(
            chunk_id="missing-source-first",
            source_id="",
            title="Missing source",
            scope="job_level",
            text="fifth child",
            score=0.5,
            metadata={"generation_context_override": shared_context},
        ),
        RetrievedChunk(
            chunk_id="missing-source-second",
            source_id="",
            title="Missing source",
            scope="job_level",
            text="sixth child",
            score=0.4,
            metadata={"generation_context_override": shared_context},
        ),
    ]

    guidance_payload = GuidanceAgent._chunks_payload(chunks)
    coach_payload = GenericCoachAgent.chunks_payload(
        chunks,
        max_text_chars=1_000,
    )

    assert [item["chunk_id"] for item in guidance_payload] == [
        "best",
        "different-scope",
        "different-title",
        "different-whitespace",
        "different-source-case",
        "missing-source-first",
        "missing-source-second",
    ]
    assert [item["chunk_id"] for item in coach_payload] == [
        "best",
        "different-scope",
        "different-title",
        "different-whitespace",
        "different-source-case",
        "missing-source-first",
        "missing-source-second",
    ]
    assert [chunk.chunk_id for chunk in chunks] == [
        "best",
        "duplicate",
        "different-scope",
        "different-title",
        "different-whitespace",
        "different-source-case",
        "missing-source-first",
        "missing-source-second",
    ]


def test_coach_payload_deduplicates_when_context_exactly_fills_budget():
    first_anchor = "FIRST-EVIDENCE"
    second_anchor = "SECOND-EVIDENCE"
    shared_context = (
        "开头背景。"
        + first_anchor
        + "中间背景。"
        + second_anchor
        + "结尾背景。"
    )
    chunks = [
        RetrievedChunk(
            chunk_id="first",
            source_id="performance/performance.md",
            title="Performance",
            scope="performance",
            text=first_anchor,
            score=0.9,
            metadata={"generation_context_override": shared_context},
        ),
        RetrievedChunk(
            chunk_id="second",
            source_id="performance/performance.md",
            title="Performance",
            scope="performance",
            text=second_anchor,
            score=0.8,
            metadata={"generation_context_override": shared_context},
        ),
    ]

    payload = GenericCoachAgent.chunks_payload(
        chunks,
        max_text_chars=len(shared_context),
    )

    assert [item["chunk_id"] for item in payload] == ["first"]
    assert payload[0]["text"] == shared_context
    assert first_anchor in payload[0]["text"]
    assert second_anchor in payload[0]["text"]
    assert sum(len(item["text"]) for item in payload) == len(shared_context)


def test_coach_payload_keeps_duplicate_contexts_when_deduplicated_text_exceeds_budget_by_one(
):
    first_anchor = "FIRST-EVIDENCE"
    second_anchor = "SECOND-EVIDENCE"
    shared_context = (
        ("开头背景。" * 8)
        + first_anchor
        + ("中间背景。" * 8)
        + second_anchor
        + ("结尾背景。" * 8)
    )
    chunks = [
        RetrievedChunk(
            chunk_id="first",
            source_id="performance/performance.md",
            title="Performance",
            scope="performance",
            text=first_anchor,
            score=0.9,
            metadata={"generation_context_override": shared_context},
        ),
        RetrievedChunk(
            chunk_id="second",
            source_id="performance/performance.md",
            title="Performance",
            scope="performance",
            text=second_anchor,
            score=0.8,
            metadata={"generation_context_override": shared_context},
        ),
    ]

    budget = len(shared_context) - 1
    payload = GenericCoachAgent.chunks_payload(
        chunks,
        max_text_chars=budget,
    )

    assert [item["chunk_id"] for item in payload] == ["first", "second"]
    assert sum(len(item["text"]) for item in payload) <= budget
    assert first_anchor in payload[0]["text"]
    assert second_anchor in payload[1]["text"]

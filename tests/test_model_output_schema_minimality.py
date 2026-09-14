from backend.agents.guidance_agent import (
    GuidanceEmotionOutput,
    GuidancePlanOutput,
    GuidanceRequirementOutput,
    GuidanceStartOutput,
)
from backend.agents.intent_recognition import IntentRecognitionStructuredOutput
from backend.schemas.document_image import (
    DocumentImageAnalysis,
    DocumentImageAnalysisBatch,
)
from backend.schemas.guidance import GuidancePointGroup
from backend.schemas.intent import IntentPerformanceDraftOutput
from backend.schemas.personality_facets import PersonalityFacetGenerationOutput
from backend.schemas.profile import EmployeeProfileExtractionOutput
from backend.schemas.rehearsal_dimensions import RehearsalDimensionEvaluation
from backend.schemas.simulation import (
    EmployeeStateTransitionStructuredOutput,
    EmotionTransitionStructuredOutput,
    MotivationScoringStructuredOutput,
)
from backend.schemas.task import CoachDimensionIssue, CoachDimensionModelOutput
from backend.tools.rag_search_tool import RagSearchInput


def _properties(schema: type) -> set[str]:
    return set(schema.model_json_schema().get("properties", {}))


def test_model_output_schemas_only_expose_consumed_top_level_fields():
    for schema in (
        GuidanceStartOutput,
        GuidanceEmotionOutput,
        GuidanceRequirementOutput,
        GuidancePlanOutput,
    ):
        assert _properties(schema) == {"points"}

    assert _properties(CoachDimensionModelOutput) == {
        "status",
        "score",
        "summary",
        "strengths",
        "basis",
        "summary_knowledge_chunk_ids",
        "basis_knowledge_chunk_ids",
        "summary_citation_refs",
        "basis_citation_refs",
        "issues",
        "career_elements_advice",
    }
    assert _properties(IntentPerformanceDraftOutput) == {
        "goal_overview",
        "positive_performance",
        "performance_gaps",
    }
    assert _properties(IntentRecognitionStructuredOutput) == {"intent_id"}
    assert _properties(EmotionTransitionStructuredOutput) == {
        "vad_delta",
        "transition_strategy",
        "appraisal_tags",
        "reason_summary",
    }
    assert _properties(MotivationScoringStructuredOutput) == {
        "primary_score_delta",
        "secondary_score_deltas",
        "reason_summary",
    }
    assert _properties(EmployeeStateTransitionStructuredOutput) == {
        "vad_delta",
        "transition_strategy",
        "appraisal_tags",
        "reason_summary",
        "pattern_dynamics",
    }
    assert _properties(RehearsalDimensionEvaluation) == {"covered_dimensions"}
    assert _properties(PersonalityFacetGenerationOutput) == {
        "openness",
        "conscientiousness",
        "extraversion",
        "agreeableness",
        "neuroticism",
    }
    assert _properties(DocumentImageAnalysisBatch) == {"analyses"}
    assert _properties(DocumentImageAnalysis) == {
        "image_ref",
        "is_informative",
        "category",
        "description",
        "visible_text",
        "key_facts",
        "layout_type",
        "nodes",
        "relations",
        "ordered_steps",
        "tables",
        "evidence",
        "uncertainties",
        "confidence",
    }
    assert _properties(RagSearchInput) == {"query", "scopes"}


def test_profile_extraction_schema_excludes_backend_managed_fields():
    properties = _properties(EmployeeProfileExtractionOutput)
    assert {
        "source_profile_text",
        "supplemental_info",
        "extraction_notes",
    }.isdisjoint(properties)


def test_model_citation_fields_are_optional_enrichment():
    guidance_schema = GuidanceStartOutput.model_json_schema()
    point_ref = guidance_schema["properties"]["points"]["items"]["$ref"]
    point_schema = guidance_schema["$defs"][point_ref.rsplit("/", 1)[-1]]
    assert set(point_schema["properties"]) == {
        "summary",
        "details",
        "citation_targets",
        "citation_source_refs",
        "citation_highlight_texts",
        "citation_source_quotes",
    }
    assert {
        "summary_knowledge_chunk_ids",
        "detail_knowledge_chunk_ids",
        "summary_citation_refs",
        "detail_citation_refs",
    }.isdisjoint(point_schema["properties"])
    assert set(point_schema["required"]) == {"summary", "details"}

    stored_point_schema = GuidancePointGroup.model_json_schema()
    assert "title" in stored_point_schema["properties"]
    assert "title" in stored_point_schema["required"]

    coach_schema = CoachDimensionModelOutput.model_json_schema()
    assert set(coach_schema["required"]) == {"status", "summary", "basis"}
    assert "maxItems" not in coach_schema["properties"]["issues"]
    assert "maxItems" not in coach_schema["properties"]["strengths"]
    assert "maxItems" not in coach_schema["properties"]["career_elements_advice"]


def test_coach_issue_schema_stays_narrow_but_runtime_ignores_extra_fields():
    schema = CoachDimensionIssue.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "turn_index",
        "quote",
        "diagnostic_dimension_id",
        "problem",
        "impact",
        "suggestion",
        "reason",
        "knowledge_chunk_ids",
        "problem_citation_refs",
        "impact_citation_refs",
        "suggestion_citation_refs",
        "reason_citation_refs",
    }
    assert set(schema["required"]) == {
        "turn_index",
        "quote",
        "problem",
        "impact",
        "suggestion",
        "reason",
    }
    assert {
        "dimension",
        "dimension_judgment",
        "issue",
        "evidence",
        "action",
    }.isdisjoint(schema["properties"])

    issue = CoachDimensionIssue.model_validate(
        {
            "turn_index": 1,
            "quote": "我们先看事实。",
            "problem": "没有说明需要核对的范围。",
            "impact": "员工难以准备对应证据。",
            "suggestion": "我们先核对目标结果和关键案例。",
            "reason": "明确范围可以提高讨论效率。",
            "evidence": {"speaker": "manager"},
        }
    )
    assert not hasattr(issue, "evidence")

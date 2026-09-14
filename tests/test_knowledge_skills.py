from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import yaml

from backend.agents.guidance_agent import GuidanceAgent
from backend.business_config.loader import BusinessConfigLoader
from backend.schemas.prompt_context import CoachPromptContext, prompt_context_json
from backend.services.knowledge_skill_service import (
    KNOWLEDGE_SKILL_AGENTS,
    KnowledgeSkillRouter,
)
from backend.services.prompt_service import PromptService
from backend.services.retrieval_service import RetrievalService


def test_all_matching_skills_activate_without_a_count_limit():
    router = KnowledgeSkillRouter()

    active = router.select(
        "guidance_plan",
        {
            "supplemental_info": (
                "请结合 Career Elements、博世高绩效文化、岗位职级和绩效 rating "
                "准备发展计划。"
            )
        },
        emit_metrics=False,
    )

    assert {item.skill.id for item in active} == {
        "career_development",
        "high_performance_culture",
        "job_level",
        "performance",
    }
    assert len(active) == 4
    assert len(router.retrieval_templates(active)) == 4
    assert all(not item.stale for item in active)
    assert all(item.core_knowledge for item in active)


def test_router_normalizes_nfkc_and_enforces_english_word_boundaries():
    router = KnowledgeSkillRouter()

    normalized = router.select(
        "guidance_start",
        {"supplemental_info": "ＣＡＲＥＥＲ   ＤＥＶＥＬＯＰＭＥＮＴ"},
        emit_metrics=False,
    )
    non_boundary = router.select(
        "guidance_start",
        {"supplemental_info": "career developmentally"},
        emit_metrics=False,
    )

    assert [item.skill.id for item in normalized] == ["career_development"]
    assert non_boundary == []


def test_career_skill_respects_authoritative_applicability_flag():
    router = KnowledgeSkillRouter()
    context = {"supplemental_info": "需要讨论 Career Elements 发展路径"}

    disabled = router.select(
        "guidance_plan",
        {**context, "career_elements_applicable": False},
        emit_metrics=False,
    )
    enabled = router.select(
        "guidance_plan",
        {**context, "career_elements_applicable": True},
        emit_metrics=False,
    )

    assert "career_development" not in {item.skill.id for item in disabled}
    assert "career_development" in {item.skill.id for item in enabled}


def test_employee_and_unknown_agents_cannot_activate_knowledge_skills():
    router = KnowledgeSkillRouter()
    context = {"supplemental_info": "绩效、岗位、职业发展、博世高绩效文化"}

    assert router.select("employee_response", context, emit_metrics=False) == []
    assert router.select("unknown_agent", context, emit_metrics=False) == []


def _temporary_router(
    tmp_path: Path,
    *,
    core_knowledge: str = "经过人工复核的绩效知识。",
) -> tuple[KnowledgeSkillRouter, Path]:
    config_dir = tmp_path / "business_config"
    skill_dir = config_dir / "skills"
    skill_dir.mkdir(parents=True)
    data_dir = tmp_path / "data"
    source = data_dir / "kb_raw" / "performance" / "source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("reviewed source", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    config = {
        "id": "performance",
        "version": "test-v1",
        "enabled": True,
        "priority": 100,
        "applies_to": sorted(KNOWLEDGE_SKILL_AGENTS),
        "triggers": ["绩效"],
        "scopes": ["performance"],
        "core_knowledge": core_knowledge,
        "usage_rules": ["不得把通用知识当作当前事实。"],
        "retrieval_queries": ["绩效反馈标准"],
        "source_files": ["performance/source.txt"],
        "reviewed_source_sha256": {"performance/source.txt": digest},
        "reviewed_at": "2026-07-22",
    }
    (skill_dir / "performance.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    router = KnowledgeSkillRouter(
        config_loader=BusinessConfigLoader(config_dir),
        data_dir=data_dir,
    )
    return router, source


def test_skill_core_knowledge_has_no_character_limit(tmp_path):
    core_knowledge = "经过人工复核的知识。" * 1000

    router, _source = _temporary_router(
        tmp_path,
        core_knowledge=core_knowledge,
    )

    assert router.skills[0].core_knowledge == core_knowledge
    assert len(router.skills[0].core_knowledge) > 2500


def test_stale_skill_disables_only_embedded_core_and_keeps_rag(tmp_path):
    router, source = _temporary_router(tmp_path)
    context = {"supplemental_info": "需要绩效反馈"}

    reviewed = router.select(
        "guidance_requirement",
        context,
        emit_metrics=False,
    )[0]
    source.write_text("changed source with a different size", encoding="utf-8")
    stale = router.select(
        "guidance_requirement",
        context,
        emit_metrics=False,
    )[0]

    assert reviewed.stale is False
    assert reviewed.core_knowledge
    assert stale.stale is True
    assert stale.core_knowledge == ""
    assert stale.content_hash != reviewed.content_hash
    assert stale.prompt_payload()["stale_source_count"] == 1
    assert "stale_sources" not in stale.prompt_payload()
    assert router.retrieval_templates([stale]) == [
        {
            "template": "绩效反馈标准",
            "scopes": ["performance"],
            "weight": 1.0,
        }
    ]


def _retrieval_service(router: KnowledgeSkillRouter) -> RetrievalService:
    service = object.__new__(RetrievalService)
    service.loader = SimpleNamespace(
        query_config=lambda: {
            "defaults": {
                "dense_top_k": 18,
                "lexical_top_k": 18,
                "rrf_k": 60,
                "fusion_top_n": 46,
                "rerank_candidate_top_n": 32,
                "rerank_top_n": 8,
            },
            "queries": {
                "guidance_plan": {
                    "enabled": True,
                    "scopes": ["general", "performance", "job_level", "career"],
                    "query_templates": [
                        {
                            "template": "基础计划查询",
                            "scopes": ["general"],
                            "weight": 1.0,
                        }
                    ],
                }
            },
        }
    )
    service.settings = SimpleNamespace(
        rag_hybrid_search_enabled=True,
        postgres_bm25_tokenizer_name="kb_jieba_v1",
        kb_index_version="v2",
        effective_embedding_provider="local_qwen",
        effective_embedding_model="embedding",
        embedding_url="http://embedding/v1/embeddings",
        effective_rerank_provider="local_qwen",
        effective_rerank_model="reranker",
        rerank_url="http://reranker/v1/completions",
    )
    service.knowledge_skill_router = router
    service._available_scopes = lambda scopes: list(scopes)
    return service


def test_retrieval_plan_adds_every_skill_query_and_cache_identity():
    router = KnowledgeSkillRouter()
    service = _retrieval_service(router)

    performance_plan = service._build_plan(
        "guidance_plan",
        {"supplemental_info": "讨论绩效反馈"},
        None,
    )
    combined_plan = service._build_plan(
        "guidance_plan",
        {"supplemental_info": "讨论绩效反馈和岗位职级"},
        None,
    )

    assert performance_plan is not None
    assert combined_plan is not None
    assert [item.skill.id for item in performance_plan.active_skills] == [
        "performance"
    ]
    assert {item.skill.id for item in combined_plan.active_skills} == {
        "performance",
        "job_level",
    }
    assert len(performance_plan.query_specs) == 2
    assert len(combined_plan.query_specs) == 3
    assert performance_plan.cache_key != combined_plan.cache_key


def test_skill_queries_are_limited_to_the_dimension_scope_allowlist():
    router = KnowledgeSkillRouter()
    service = _retrieval_service(router)
    config = service.loader.query_config()
    config["queries"]["guidance_plan"]["scopes"] = ["general", "performance"]
    service.loader = SimpleNamespace(query_config=lambda: config)

    plan = service._build_plan(
        "guidance_plan",
        {"supplemental_info": "讨论绩效反馈和岗位职级"},
        None,
    )

    assert plan is not None
    assert {item.skill.id for item in plan.active_skills} == {
        "performance",
        "job_level",
    }
    assert all(
        set(spec.scopes) <= {"general", "performance"}
        for spec in plan.query_specs
    )
    assert any(spec.scopes == ["performance"] for spec in plan.query_specs)
    assert all("job_level" not in spec.scopes for spec in plan.query_specs)


def test_query_scope_pairs_are_deduplicated_without_dropping_other_scopes():
    service = object.__new__(RetrievalService)

    specs = service._render_query_specs(
        templates=[
            {"template": "同一查询", "scopes": ["general"]},
            {"template": "同一查询", "scopes": ["general", "culture"]},
        ],
        context={},
        fallback_scopes=[],
        available_scopes={"general", "culture"},
    )

    assert [(spec.query, spec.scopes) for spec in specs] == [
        ("同一查询", ["general"]),
        ("同一查询", ["culture"]),
    ]


def test_guidance_context_excludes_inapplicable_career_skill():
    state = SimpleNamespace(
        locale="zh-CN",
        employee_profile=None,
        supplemental_info="需要讨论绩效、岗位职级与职业发展。",
        intent=None,
        personality=None,
        motivation=None,
    )

    context = GuidanceAgent._build_context(
        state,
        [],
        agent_name="guidance_plan",
    )

    assert {item.id for item in context.active_skills} == {
        "performance",
        "job_level",
    }


def test_all_coach_prompts_render_the_complete_active_skill_payload():
    active_skills = [
        {
            "id": "performance",
            "version": "1.0.0",
            "priority": 100,
            "matched_triggers": ["绩效"],
            "scopes": ["performance"],
            "core_knowledge": "绩效核心知识",
            "usage_rules": ["仅用于建议"],
            "stale": False,
            "stale_source_count": 0,
        },
        {
            "id": "job_level",
            "version": "1.0.0",
            "priority": 95,
            "matched_triggers": ["职级"],
            "scopes": ["job_level"],
            "core_knowledge": "职级核心知识",
            "usage_rules": ["需要核对适用标准"],
            "stale": False,
            "stale_source_count": 0,
        },
    ]
    context_json = prompt_context_json(
        CoachPromptContext(
            intent_id="development",
            active_skills=active_skills,
            dimension_config={},
        )
    )
    prompts = PromptService()

    for template in (
        "coach/start.jinja2",
        "coach/emotion.jinja2",
        "coach/requirement.jinja2",
        "coach/plan.jinja2",
    ):
        rendered = prompts.render(template, context_json=context_json)
        assert '"active_skills":[' in rendered
        assert "绩效核心知识" in rendered
        assert "职级核心知识" in rendered

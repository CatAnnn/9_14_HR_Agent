from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from yaml.constructor import ConstructorError

from backend.business_config.loader import BusinessConfigLoader, get_config_loader
from backend.schemas.retrieval import RetrievedChunk
from backend.services.retrieval_service import (
    RetrievalService,
    _RankedBranch,
    _SearchJobResult,
)


class _Loader:
    def __init__(self, config):
        self.config = config

    def query_config(self):
        return self.config


class _NoSkills:
    @staticmethod
    def select(agent_name, context):
        return []

    @staticmethod
    def retrieval_templates(active_skills):
        return []


def _settings():
    return SimpleNamespace(
        rag_hybrid_search_enabled=False,
        postgres_bm25_tokenizer_name="test-tokenizer",
        kb_index_version="test-index",
        effective_embedding_provider="local_qwen",
        effective_embedding_model="test-embedding",
        embedding_url="http://embedding/v1/embeddings",
        effective_rerank_provider="local_qwen",
        effective_rerank_model="test-reranker",
        rerank_url="http://reranker/v1/completions",
    )


def _chunk(index: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{index}",
        source_id=f"source-{index}",
        title=f"title-{index}",
        scope="general",
        text=f"text-{index}",
        score=1.0,
    )


@pytest.mark.parametrize(
    ("payload", "key", "first_line", "duplicate_line"),
    [
        ("version: v1\nversion: v2\n", "version", 1, 2),
        (
            "defaults:\n  query_parallelism: 4\n  query_parallelism: 8\n",
            "query_parallelism",
            2,
            3,
        ),
    ],
)
def test_business_config_loader_reports_duplicate_key_location(
    tmp_path: Path,
    payload: str,
    key: str,
    first_line: int,
    duplicate_line: int,
):
    config_path = tmp_path / "duplicate.yaml"
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ConstructorError) as raised:
        BusinessConfigLoader(tmp_path).load_yaml(config_path.name)

    message = str(raised.value)
    assert str(config_path) in message
    assert f"duplicate key {key!r}" in message
    assert f"first defined at line {first_line}" in message
    assert f"line {duplicate_line}," in message


def test_business_config_loader_accepts_valid_nested_mapping(tmp_path: Path):
    config_path = tmp_path / "valid.yaml"
    config_path.write_text(
        "defaults:\n  query_parallelism: 8\n  rerank_parallelism: 8\n",
        encoding="utf-8",
    )

    assert BusinessConfigLoader(tmp_path).load_yaml(config_path.name) == {
        "defaults": {
            "query_parallelism": 8,
            "rerank_parallelism": 8,
        }
    }


def test_query_config_defaults_bound_parallelism_to_eight():
    defaults = get_config_loader().query_config()["defaults"]

    assert defaults["query_parallelism"] == 8
    assert defaults["rerank_parallelism"] == 8
    assert defaults["rerank_units_per_parent"] == 5
    assert defaults["rerank_semantic_group_candidate_enabled"] is False
    assert defaults["rerank_compound_evidence_sets"] == []
    assert defaults["rerank_rank_fusion_k"] == 60
    assert defaults["rerank_rank_weight"] == 2.0
    assert defaults["first_stage_rank_weight"] == 1.0


def test_compound_evidence_stays_disabled_without_a_measured_gain():
    queries = get_config_loader().query_config()["queries"]
    enabled = {
        name
        for name, config in queries.items()
        if config.get("rerank_semantic_group_candidate_enabled") is True
    }

    assert enabled == set()


def test_plan_tasks_reserve_job_level_citation_capacity():
    queries = get_config_loader().query_config()["queries"]

    assert queries["guidance_plan"]["citation_scope_minimums"][
        "job_level"
    ] == 1
    assert queries["development_plan_evaluation"][
        "citation_scope_minimums"
    ]["job_level"] == 1


def test_culture_tasks_use_supplied_context_for_targeted_recall():
    queries = get_config_loader().query_config()["queries"]
    task_names = {
        "guidance_emotion",
        "guidance_requirement",
        "guidance_plan",
        "emotion_evaluation",
        "development_plan_evaluation",
    }

    for name in task_names:
        config = queries[name]
        culture_templates = [
            item["template"]
            for item in config["query_templates"]
            if item.get("scopes") == ["culture"]
        ]
        assert any(
            "supplemental_info" in template
            for template in culture_templates
        ), name


def test_all_enabled_queries_define_task_specific_rerank_controls():
    queries = get_config_loader().query_config()["queries"]

    for name, config in queries.items():
        if not config.get("enabled", True):
            continue
        assert str(config.get("rerank_instruction") or "").strip(), name
        assert str(config.get("rerank_query_template") or "").strip(), name


def test_all_task_rerank_queries_render_as_one_non_empty_query():
    queries = get_config_loader().query_config()["queries"]
    context = {
        "intent": {"name": "发展"},
        "profile": {
            "role": "招聘专家",
            "level": "G9",
            "department": "HR",
            "reporting_line": "HR Manager",
            "conversation_topic": "年度绩效",
            "key_goals": ["高端岗位招聘", "招聘策略"],
        },
        "motivation": {"primary_motive_id": "Recognition"},
        "emotion_state": {"current_anchor_id": "concerned"},
        "latest_manager_message": "请谈谈今年的招聘交付。",
        "user_query": "如何制定发展计划？",
        "tool_query": "Career Elements 如何用于发展计划？",
        "requested_scopes": ["career", "job_level"],
        "career_elements_applicable": True,
        "current_career_elements": ["Project leadership experience"],
    }

    for name, config in queries.items():
        if not config.get("enabled", True):
            continue
        query, instruction = RetrievalService._render_rerank_controls(
            agent_cfg=config,
            defaults={},
            context=context,
            query_specs=[SimpleNamespace(query="fallback query")],
        )
        assert query.strip(), name
        assert "\n" not in query, name
        assert instruction, name


def test_career_plan_rerank_query_treats_existing_elements_as_exclusions():
    config = get_config_loader().query_config()["queries"]["guidance_plan"]

    query, _ = RetrievalService._render_rerank_controls(
        agent_cfg=config,
        defaults={},
        context={
            "intent": {"name": "发展"},
            "profile": {
                "role": "招聘专家",
                "level": "G9",
                "conversation_topic": "年度绩效",
                "key_goals": ["高端岗位招聘"],
            },
            "career_elements_applicable": True,
            "current_career_elements": ["Project leadership experience"],
        },
        query_specs=[SimpleNamespace(query="fallback query")],
    )

    assert "当前已具备、仅用于排除" in query
    assert query.count("Project leadership experience") == 1


@pytest.mark.parametrize(
    ("task_name", "expected_level", "excluded_level"),
    [
        ("guidance_requirement", "G9", "SL1"),
        ("guidance_plan", "SL1", "G9"),
        ("output_expectations_evaluation", "G9", "SL1"),
        ("development_plan_evaluation", "SL1", "G9"),
    ],
)
def test_level_and_culture_sensitive_rerank_queries_keep_exact_context(
    task_name,
    expected_level,
    excluded_level,
):
    config = get_config_loader().query_config()["queries"][task_name]

    query, instruction = RetrievalService._render_rerank_controls(
        agent_cfg=config,
        defaults={},
        context={
            "intent": {"id": "development", "name": "发展"},
            "profile": {
                "role": "招聘专家",
                "level": "G9",
                "conversation_topic": "年度绩效",
                "key_goals": ["高端岗位招聘"],
            },
            "supplemental_info": (
                "本次需结合使命必达（Commitment to WIN）的行为标准。"
            ),
            "career_elements_applicable": True,
            "current_career_elements": [],
        },
        query_specs=[SimpleNamespace(query="fallback query")],
    )

    assert expected_level in query
    assert excluded_level not in query
    assert "使命必达" in query
    assert "Commitment to WIN" in query
    assert "exact labels" in instruction
    assert "exact value" in instruction
    if task_name == "guidance_requirement":
        assert "WHAT 70%" in query
        assert "HOW 30%" in query
        assert "complete WHAT 70% and HOW 30%" in instruction


def test_guidance_and_coach_queries_use_dimension_scope_allowlists():
    queries = get_config_loader().query_config()["queries"]
    expected = {
        "guidance_start": {
            "job_level", "redline", "culture", "general", "performance",
            "career", "development_dialog", "feedback",
        },
        "opening_evaluation": {
            "job_level", "culture", "general", "performance",
            "career", "development_dialog", "feedback",
        },
        "guidance_emotion": {
            "culture", "emotion",
        },
        "emotion_evaluation": {
            "culture", "emotion",
        },
        "guidance_requirement": {
            "job_level", "culture", "general", "performance", "career",
            "development_dialog", "feedback", "organization_unit",
        },
        "output_expectations_evaluation": {
            "job_level", "culture", "general", "performance", "career",
            "development_dialog", "feedback", "organization_unit",
        },
        "guidance_plan": {
            "job_level", "culture", "general", "performance",
            "career", "development_dialog", "feedback",
        },
        "development_plan_evaluation": {
            "job_level", "culture", "general", "performance",
            "career", "development_dialog", "feedback", "organization_unit",
        },
    }
    for name, allowed_scopes in expected.items():
        assert set(queries[name]["scopes"]) == allowed_scopes
        assert all(
            set(template["scopes"]) <= allowed_scopes
            for template in queries[name]["query_templates"]
        )


def _build_plan(
    *,
    default_value=None,
    override_value=None,
    agent_config_updates=None,
    context=None,
):
    scopes = [f"scope-{index}" for index in range(10)]
    defaults = {"rerank_semantic_group_candidate_enabled": True}
    if default_value is not None:
        defaults["query_parallelism"] = default_value
    agent_config = {
        "enabled": True,
        "scopes": scopes,
        "query_templates": [
            {"template": "query", "scopes": scopes, "weight": 1.0}
        ],
    }
    if override_value is not None:
        agent_config["query_parallelism"] = override_value
    agent_config.update(agent_config_updates or {})

    service = object.__new__(RetrievalService)
    service.loader = _Loader(
        {"defaults": defaults, "queries": {"test-agent": agent_config}}
    )
    service.settings = _settings()
    service.knowledge_skill_router = _NoSkills()
    service._available_scopes = lambda configured: configured
    return service._build_plan("test-agent", context or {}, None)


def test_task_level_organization_scope_always_creates_a_search_spec():
    service = object.__new__(RetrievalService)
    service.loader = _Loader(
        {
            "defaults": {},
            "queries": {
                "test-agent": {
                    "enabled": True,
                    "scopes": ["general", "culture", "organization_unit"],
                    "query_templates": [
                        {
                            "template": "绩效反馈标准",
                            "scopes": ["general"],
                            "weight": 1.0,
                        }
                    ],
                }
            },
        }
    )
    service.settings = _settings()
    service.knowledge_skill_router = _NoSkills()
    service._available_scopes = lambda configured: configured

    plan = service._build_plan("test-agent", {"profile": {}}, None)

    assert plan is not None
    query_scope_pairs = [
        (spec.query, scope)
        for spec in plan.query_specs
        for scope in spec.scopes
    ]
    assert query_scope_pairs[0] == ("绩效反馈标准", "general")
    assert len(query_scope_pairs) == 2
    assert query_scope_pairs[1][1] == "organization_unit"
    assert "组织单元" in query_scope_pairs[1][0]
    assert all(scope != "culture" for _, scope in query_scope_pairs)


@pytest.mark.parametrize(
    ("default_value", "override_value", "expected"),
    [
        (None, None, 8),
        (4, None, 4),
        (8, 3, 3),
        (8, 99, 8),
        (99, None, 8),
    ],
)
def test_async_query_parallelism_uses_yaml_with_hard_limit_eight(
    default_value,
    override_value,
    expected,
):
    plan = _build_plan(
        default_value=default_value,
        override_value=override_value,
    )

    assert plan is not None
    assert plan.query_parallelism == expected


def test_async_plan_renders_task_rerank_query_and_instruction():
    plan = _build_plan(
        context={"profile": {"role": "招聘专家", "level": "G9"}},
        agent_config_updates={
            "rerank_query_template": (
                "{{ profile.level }} {{ profile.role }} 当前发展目标"
            ),
            "rerank_instruction": "Rank career-development evidence.",
        },
    )

    assert plan is not None
    assert plan.rerank_query == "G9 招聘专家 当前发展目标"
    assert plan.rerank_instruction == "Rank career-development evidence."
    assert plan.rerank_semantic_group_candidate_enabled is True
    assert plan.rerank_compound_evidence_sets == ()


@pytest.mark.parametrize(
    (
        "units_per_parent",
        "rank_fusion_k",
        "expected_units_per_parent",
        "expected_rank_fusion_k",
    ),
    [
        (1, 0, 2, 1),
        (3, 17, 3, 17),
        (99, -8, 5, 1),
        ("invalid", "invalid", 5, 60),
    ],
)
def test_async_plan_normalizes_task_rerank_aggregation_overrides(
    units_per_parent,
    rank_fusion_k,
    expected_units_per_parent,
    expected_rank_fusion_k,
):
    plan = _build_plan(
        agent_config_updates={
            "rerank_units_per_parent": units_per_parent,
            "rerank_rank_fusion_k": rank_fusion_k,
        }
    )

    assert plan is not None
    assert plan.rerank_units_per_parent == expected_units_per_parent
    assert plan.rerank_rank_fusion_k == expected_rank_fusion_k
    assert plan.rerank_rank_weight == pytest.approx(4 / 3)
    assert plan.first_stage_rank_weight == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    ("rerank_weight", "first_stage_weight", "expected"),
    [
        (2.0, 1.0, (4 / 3, 2 / 3)),
        (4.0, 2.0, (4 / 3, 2 / 3)),
        (0.0, 1.0, (0.0, 2.0)),
        (0.0, 0.0, (4 / 3, 2 / 3)),
        (-1.0, 1.0, (4 / 3, 2 / 3)),
        (float("inf"), 1.0, (4 / 3, 2 / 3)),
        (1e308, 1e308, (1.0, 1.0)),
        (1e308, 5e307, (4 / 3, 2 / 3)),
        ("invalid", 1.0, (4 / 3, 2 / 3)),
    ],
)
def test_rerank_rank_weights_are_safe_and_keep_the_score_scale(
    rerank_weight,
    first_stage_weight,
    expected,
):
    actual = RetrievalService._normalize_rerank_rank_weights(
        rerank_weight,
        first_stage_weight,
    )

    assert actual == pytest.approx(expected)
    assert sum(actual) == pytest.approx(2.0)


def test_async_plan_uses_configured_rerank_rank_weight_ratio():
    plan = _build_plan(
        agent_config_updates={
            "rerank_rank_weight": 6.0,
            "first_stage_rank_weight": 1.0,
        }
    )

    assert plan is not None
    assert plan.rerank_rank_weight == pytest.approx(12 / 7)
    assert plan.first_stage_rank_weight == pytest.approx(2 / 7)


class _EmbeddingService:
    def __init__(self):
        self.queries = []

    def embed_queries(self, queries):
        self.queries = list(queries)
        return [[1.0] for _ in queries], [False for _ in queries]


class _CapturingReranker:
    def __init__(self):
        self.parallelism = None
        self.query = None
        self.instruction = None
        self.units_per_parent = None
        self.semantic_group_candidate_enabled = None
        self.rank_fusion_k = None
        self.rerank_rank_weight = None
        self.first_stage_rank_weight = None

    def rerank(
        self,
        chunks,
        *,
        query,
        top_k,
        parallelism,
        instruction=None,
    ):
        self.parallelism = parallelism
        self.query = query
        self.instruction = instruction
        return chunks[:top_k]


def _run_sync_retrieval(
    *,
    default_value=None,
    override_value=None,
    agent_config_updates=None,
    context=None,
):
    defaults = {
        "hybrid_search_enabled": False,
        "fusion_top_n": 12,
        "rerank_candidate_top_n": 12,
        "rerank_top_n": 8,
        "rerank_semantic_group_candidate_enabled": True,
    }
    if default_value is not None:
        defaults["rerank_parallelism"] = default_value
    agent_config = {
        "enabled": True,
        "scopes": ["general"],
        "query_templates": [
            {"template": "query", "scopes": ["general"], "weight": 1.0}
        ],
    }
    if override_value is not None:
        agent_config["rerank_parallelism"] = override_value
    agent_config.update(agent_config_updates or {})

    reranker = _CapturingReranker()
    embedding_service = _EmbeddingService()
    service = object.__new__(RetrievalService)
    service.loader = _Loader(
        {"defaults": defaults, "queries": {"test-agent": agent_config}}
    )
    service.settings = _settings()
    service.knowledge_skill_router = _NoSkills()
    service.embedding_service = embedding_service
    service.reranker = reranker
    service._available_scopes = lambda configured: configured
    service._get_cached_result = lambda cache_key: None
    service._cache_result = lambda cache_key, chunks: None
    service._search_scope = lambda **kwargs: _SearchJobResult(
        branches=[
            _RankedBranch(
                branch_id="0:general:dense",
                modality="dense",
                scope="general",
                weight=1.0,
                chunks=[_chunk(index) for index in range(12)],
            )
        ],
        lexical_error=None,
        semaphore_wait_ms=0.0,
        lexical_executed=False,
        active_count=1,
    )
    original_expand = RetrievalService._expand_unit_rerank_candidates
    original_aggregate = RetrievalService._aggregate_unit_rerank_results

    def capture_expand(
        parent_candidates,
        *,
        max_units_per_parent,
        include_semantic_group_candidate=False,
    ):
        reranker.units_per_parent = max_units_per_parent
        reranker.semantic_group_candidate_enabled = (
            include_semantic_group_candidate
        )
        return original_expand(
            parent_candidates,
            max_units_per_parent=max_units_per_parent,
            include_semantic_group_candidate=(
                include_semantic_group_candidate
            ),
        )

    def capture_aggregate(
        parent_candidates,
        ranked_units,
        *,
        rank_fusion_k,
        rerank_rank_weight,
        first_stage_rank_weight,
    ):
        reranker.rank_fusion_k = rank_fusion_k
        reranker.rerank_rank_weight = rerank_rank_weight
        reranker.first_stage_rank_weight = first_stage_rank_weight
        return original_aggregate(
            parent_candidates,
            ranked_units,
            rank_fusion_k=rank_fusion_k,
            rerank_rank_weight=rerank_rank_weight,
            first_stage_rank_weight=first_stage_rank_weight,
        )

    service._expand_unit_rerank_candidates = capture_expand
    service._aggregate_unit_rerank_results = capture_aggregate

    result = service.retrieve("test-agent", context or {})
    return reranker, embedding_service, result


@pytest.mark.parametrize(
    ("default_value", "override_value", "expected"),
    [
        (None, None, 8),
        (8, 3, 3),
        (99, None, 8),
    ],
)
def test_sync_rerank_parallelism_uses_yaml_with_hard_limit_eight(
    default_value,
    override_value,
    expected,
):
    reranker, _, result = _run_sync_retrieval(
        default_value=default_value,
        override_value=override_value,
    )

    assert reranker.parallelism == expected
    assert len(result) == 8


def test_sync_retrieval_uses_task_rerank_query_and_instruction():
    reranker, embeddings, result = _run_sync_retrieval(
        context={"profile": {"role": "招聘专家"}},
        agent_config_updates={
            "query_templates": [
                {
                    "template": "召回查询一",
                    "scopes": ["general"],
                    "weight": 1.0,
                },
                {
                    "template": "召回查询二",
                    "scopes": ["general"],
                    "weight": 1.0,
                },
            ],
            "rerank_query_template": "{{ profile.role }} 当前发展目标",
            "rerank_instruction": "Rank career-development evidence.",
        },
    )

    assert embeddings.queries == ["召回查询一", "召回查询二"]
    assert reranker.query == "招聘专家 当前发展目标"
    assert reranker.instruction == "Rank career-development evidence."
    assert reranker.semantic_group_candidate_enabled is True
    assert len(result) == 8


@pytest.mark.parametrize(
    (
        "units_per_parent",
        "rank_fusion_k",
        "expected_units_per_parent",
        "expected_rank_fusion_k",
    ),
    [
        (1, 0, 2, 1),
        (4, 23, 4, 23),
        (99, -4, 5, 1),
        ("invalid", "invalid", 5, 60),
    ],
)
def test_sync_retrieval_normalizes_task_rerank_aggregation_overrides(
    units_per_parent,
    rank_fusion_k,
    expected_units_per_parent,
    expected_rank_fusion_k,
):
    reranker, _, result = _run_sync_retrieval(
        agent_config_updates={
            "rerank_units_per_parent": units_per_parent,
            "rerank_rank_fusion_k": rank_fusion_k,
        }
    )

    assert reranker.units_per_parent == expected_units_per_parent
    assert reranker.rank_fusion_k == expected_rank_fusion_k
    assert reranker.rerank_rank_weight == pytest.approx(4 / 3)
    assert reranker.first_stage_rank_weight == pytest.approx(2 / 3)
    assert len(result) == 8


def test_sync_retrieval_uses_configured_rerank_rank_weight_ratio():
    reranker, _, result = _run_sync_retrieval(
        agent_config_updates={
            "rerank_rank_weight": 6.0,
            "first_stage_rank_weight": 1.0,
        }
    )

    assert reranker.rerank_rank_weight == pytest.approx(12 / 7)
    assert reranker.first_stage_rank_weight == pytest.approx(2 / 7)
    assert len(result) == 8


def test_rerank_query_fallback_uses_only_first_recall_query():
    query, instruction = RetrievalService._render_rerank_controls(
        agent_cfg={},
        defaults={},
        context={},
        query_specs=[
            SimpleNamespace(query="召回查询一"),
            SimpleNamespace(query="召回查询二"),
        ],
    )

    assert query == "召回查询一"
    assert instruction is None


def test_rerank_query_adds_only_missing_canonical_business_terms():
    query, instruction = RetrievalService._render_rerank_controls(
        agent_cfg={"rerank_query_template": "{{ user_query }}"},
        defaults={},
        context={"user_query": "博世高绩效文化中的使命必达是什么"},
        query_specs=[SimpleNamespace(query="broad recall expansion")],
    )

    assert query.startswith("博世高绩效文化中的使命必达是什么；")
    assert "commitment to win" in query.casefold()
    assert "broad recall expansion" not in query
    assert "\n" not in query
    assert instruction is None


def test_rerank_query_does_not_duplicate_existing_canonical_term():
    query, _ = RetrievalService._render_rerank_controls(
        agent_cfg={"rerank_query_template": "{{ user_query }}"},
        defaults={},
        context={
            "user_query": "使命必达 Commitment to WIN 的可观察行为"
        },
        query_specs=[SimpleNamespace(query="fallback")],
    )

    assert query.casefold().count("commitment to win") == 1


@pytest.mark.parametrize(
    "agent_config",
    [
        {"rerank_query_template": "{{ missing_value }}"},
        {"rerank_instruction": "   "},
    ],
)
def test_configured_empty_rerank_control_is_rejected(agent_config):
    with pytest.raises(ValueError, match="Configured rerank_"):
        RetrievalService._render_rerank_controls(
            agent_cfg=agent_config,
            defaults={},
            context={},
            query_specs=[SimpleNamespace(query="召回查询")],
        )

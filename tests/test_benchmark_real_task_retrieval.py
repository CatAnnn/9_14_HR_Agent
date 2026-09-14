from __future__ import annotations

import json
from copy import deepcopy

import pytest

from backend.schemas.retrieval import RetrievedChunk
from scripts import benchmark_real_task_retrieval as benchmark


def test_fixture_expands_to_the_exact_production_task_matrix() -> None:
    fixture = benchmark.load_fixture()
    cases = benchmark.validate_and_expand_fixture(fixture)

    assert len(fixture["scenarios"]) == 6
    assert [task["task_id"] for task in fixture["tasks"]] == list(
        benchmark.EXPECTED_TASK_IDS
    )
    assert len(cases) == 48
    assert len({case["case_id"] for case in cases}) == 48
    assert {scenario["level"] for scenario in fixture["scenarios"]} == {
        "G8",
        "G9",
        "SL1",
    }
    assert {
        scenario["career_state"] for scenario in fixture["scenarios"]
    } == {
        "not_applicable",
        "unknown",
        "known_partial",
        "known_all_five",
    }
    assert {scenario["culture_term"] for scenario in fixture["scenarios"]} == {
        "协同共进",
        "创变未来",
        "聚力共赢",
        "使命必达",
    }
    assert len(
        {scenario["asr_combination"] for scenario in fixture["scenarios"]}
    ) == 6


def test_top3_contract_is_distinct_from_complete_rank_eight_contract() -> None:
    cases = {
        case["case_id"]: case
        for case in benchmark.validate_and_expand_fixture(
            benchmark.load_fixture()
        )
    }

    requirement = cases[
        "g8_development_collaboration::guidance_requirement"
    ]
    plan = cases["g9_development_known_elements_innovation::guidance_plan"]

    assert len(requirement["top3_required_term_groups"]) == 10
    assert len(requirement["required_term_groups"]) == 13
    assert len(plan["top3_required_term_groups"]) == 13
    assert len(plan["required_term_groups"]) == 15
    assert any(
        "下一步明确动作" in group
        for group in plan["top3_required_term_groups"]
    )


def test_plan_top3_scope_contract_only_adds_selected_active_profiles() -> None:
    fixture = benchmark.load_fixture()
    tasks = {task["task_id"]: task for task in fixture["tasks"]}
    plan_task_ids = ("guidance_plan", "development_plan_evaluation")
    task_scope_group = [["development_dialog", "general", "feedback"]]

    for task_id in plan_task_ids:
        assert tasks[task_id]["top3_required_scope_groups"] == task_scope_group
        assert tasks[task_id]["top3_evidence_profiles"] == [
            "job_level",
            "career",
        ]

    cases = {
        case["case_id"]: case
        for case in benchmark.validate_and_expand_fixture(fixture)
    }
    applicable = cases[
        "g9_development_known_elements_innovation::guidance_plan"
    ]
    assert applicable["top3_required_scope_groups"] == [
        *task_scope_group,
        ["job_level"],
        ["career"],
    ]
    assert applicable["required_scope_groups"] == [
        *task_scope_group,
        ["job_level"],
        ["career"],
        ["culture"],
    ]

    career_inapplicable = cases[
        "g8_development_collaboration::development_plan_evaluation"
    ]
    assert career_inapplicable["top3_required_scope_groups"] == [
        *task_scope_group,
        ["job_level"],
    ]
    assert ["culture"] not in career_inapplicable[
        "top3_required_scope_groups"
    ]


def test_validate_only_never_starts_live_retrieval(monkeypatch, capsys) -> None:
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("validate-only must not start asyncio retrieval")

    monkeypatch.setattr(benchmark.asyncio, "run", unexpected_run)

    assert benchmark.main(["--validate-only"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "benchmark_id": "real-task-retrieval-v1",
        "schema_version": "1.1",
        "case_count": 48,
        "complete_case_matrix": True,
        "effective_top_k": 8,
        "fixture_case_count": 48,
        "fixture_top_k": 8,
        "mode": "validate-only",
        "release_eligible": False,
        "release_ineligibility_reasons": [
            "validate-only does not execute release gates",
            "release gates require identity-bound manual judgments",
            "fixture comparison gates require --baseline-report",
        ],
        "release_prerequisites_met": False,
        "scenario_count": 6,
        "task_count": 8,
        "valid": True,
    }


def test_retrieval_variant_overrides_are_explicit_and_isolated() -> None:
    args = benchmark.parse_args(
        [
            "--rerank-units-per-parent",
            "5",
            "--semantic-group-candidate",
            "off",
            "--rerank-rank-fusion-k",
            "60",
            "--rerank-rank-weight",
            "2",
            "--first-stage-rank-weight",
            "1",
        ]
    )

    assert benchmark.retrieval_overrides_from_args(args) == {
        "rerank_units_per_parent": 5,
        "rerank_semantic_group_candidate_enabled": False,
        "rerank_rank_fusion_k": 60,
        "rerank_rank_weight": 2.0,
        "first_stage_rank_weight": 1.0,
    }
    assert benchmark.retrieval_overrides_from_args(
        benchmark.parse_args([])
    ) == {}


def test_case_metrics_and_release_gates_are_deterministic() -> None:
    case = {
        "case_id": "scenario::task",
        "scenario_id": "scenario",
        "task_id": "task",
        "phase": "coach",
        "scenario_context": {"career_elements_applicable": True},
        "required_term_groups": [["G9"], ["WHAT"], ["HOW"]],
        "required_scope_groups": [["job_level"], ["performance"]],
        "required_scope_minimums": {"job_level": 1, "performance": 1},
        "critical_profiles": {
            "job_level": {
                "required_term_groups": [["G9"]],
                "required_scope_groups": [["job_level"]],
            },
            "asr": {
                "required_term_groups": [["WHAT"], ["HOW"]],
                "required_scope_groups": [["performance"]],
            },
        },
        "review_rules": [],
        "manual_review_context": {},
    }
    chunks = [
        RetrievedChunk(
            chunk_id="job",
            source_id="job.md",
            title="G9 standard",
            scope="job_level",
            text="G9 output standard",
            metadata={
                "parent_context": "G8 baseline\nG9 output standard\nSL1 target",
                "generation_context_override": "G9 output standard",
                "generation_context_focused": True,
            },
        ),
        RetrievedChunk(
            chunk_id="asr",
            source_id="asr.md",
            title="ASR",
            scope="performance",
            text="WHAT and HOW",
            metadata={},
        ),
    ]

    result = benchmark.evaluate_case(case, chunks, 12.5)
    metrics = benchmark.summarize([result])
    gates = benchmark.evaluate_gates(
        metrics,
        {
            "mean_term_coverage_at_3": {
                "operator": ">=",
                "threshold": 1.0,
            },
            "critical_evidence_failure_count": {
                "operator": "<=",
                "threshold": 0,
            },
            "prompt_critical_evidence_failure_count": {
                "operator": "<=",
                "threshold": 0,
            },
            "structured_bundle_failure_count": {
                "operator": "<=",
                "threshold": 0,
            },
            "prompt_anchor_loss_count": {
                "operator": "<=",
                "threshold": 0,
            },
        },
    )

    assert result["term_coverage_at_3"] == 1.0
    assert result["scope_domain_coverage_at_3"] == 1.0
    assert result["scope_quota_coverage_at_8"] == 1.0
    assert result["critical_evidence_failures"] == []
    assert result["generation_context_chars_at_8"] == len(
        "G9 output standard"
    ) + len("WHAT and HOW")
    assert result["source_context_chars_at_8"] == len(
        "G8 baseline\nG9 output standard\nSL1 target"
    ) + len("WHAT and HOW")
    assert result["focused_chunk_count_at_8"] == 1
    assert metrics["mean_generation_context_chars_at_8"] == float(
        result["generation_context_chars_at_8"]
    )
    assert metrics["mean_focused_chunk_count_at_8"] == 1.0
    assert metrics["mean_generation_context_reduction_rate_at_8"] > 0
    assert (
        result["prompt_context_chars_at_8"]
        == result["generation_context_chars_at_8"]
    )
    assert result["prompt_term_coverage_at_8"] == 1.0
    assert result["prompt_critical_evidence_failures"] == []
    assert result["structured_bundle_failures"] == []
    assert result["prompt_deduplicated_chunk_count_at_8"] == 0
    assert metrics["full_prompt_term_coverage_at_8_rate"] == 1.0
    assert metrics["prompt_critical_evidence_failure_count"] == 0
    assert metrics["structured_bundle_failure_count"] == 0
    assert metrics["prompt_anchor_loss_count"] == 0
    assert metrics["critical_full_coverage_at_8_rate"] == 1.0
    assert len(result["results"][0]["content_hash"]) == 64
    assert all(gate["passed"] for gate in gates)


def test_content_hash_covers_complete_prompt_visible_body() -> None:
    shared_prefix = "prompt-visible-" * 100
    first = shared_prefix + "first-tail"
    second = shared_prefix + "second-tail"

    assert first[:1200] == second[:1200]
    assert benchmark._result_content_hash(
        generation_visible_text=first
    ) == benchmark._result_content_hash(generation_visible_text=first)
    assert benchmark._result_content_hash(
        generation_visible_text=first
    ) != benchmark._result_content_hash(generation_visible_text=second)


def test_case_top3_uses_prefix_contract_but_top8_uses_complete_contract() -> None:
    case = {
        "case_id": "scenario::task",
        "scenario_id": "scenario",
        "task_id": "task",
        "phase": "coach",
        "scenario_context": {"career_elements_applicable": False},
        "top3_required_term_groups": [["G9"], ["WHAT"]],
        "required_term_groups": [["G9"], ["WHAT"], ["follow-up"]],
        "required_scope_groups": [["job_level"], ["performance"]],
        "required_scope_minimums": {"job_level": 1, "performance": 1},
        "critical_profiles": {},
        "review_rules": [],
        "manual_review_context": {},
    }
    chunks = [
        RetrievedChunk(
            chunk_id="job",
            source_id="job.md",
            title="G9",
            scope="job_level",
            text="G9",
        ),
        RetrievedChunk(
            chunk_id="asr",
            source_id="asr.md",
            title="ASR",
            scope="performance",
            text="WHAT",
        ),
        RetrievedChunk(
            chunk_id="culture",
            source_id="culture.md",
            title="Culture",
            scope="culture",
            text="culture",
        ),
        RetrievedChunk(
            chunk_id="follow-up",
            source_id="feedback.md",
            title="Follow-up",
            scope="feedback",
            text="follow-up",
        ),
    ]

    result = benchmark.evaluate_case(case, chunks, 1.0)

    assert result["term_coverage_at_3"] == 1.0
    assert result["term_coverage_at_8"] == 1.0


def test_case_top3_scope_contract_is_independent_and_defaults_to_full_contract() -> None:
    case = {
        "case_id": "scenario::plan",
        "scenario_id": "scenario",
        "task_id": "plan",
        "phase": "coach",
        "scenario_context": {"career_elements_applicable": True},
        "required_term_groups": [],
        "top3_required_scope_groups": [
            ["development_dialog", "general", "feedback"],
            ["job_level"],
            ["career"],
        ],
        "required_scope_groups": [
            ["development_dialog", "general", "feedback"],
            ["job_level"],
            ["career"],
            ["culture"],
        ],
        "required_scope_minimums": {
            "development_dialog": 1,
            "job_level": 1,
            "career": 1,
            "culture": 1,
        },
        "critical_profiles": {},
        "review_rules": [],
        "manual_review_context": {},
    }
    chunks = [
        RetrievedChunk(
            chunk_id=scope,
            source_id=f"{scope}.md",
            title=scope,
            scope=scope,
            text=scope,
        )
        for scope in (
            "development_dialog",
            "job_level",
            "career",
            "culture",
        )
    ]

    independent_result = benchmark.evaluate_case(case, chunks, 1.0)
    assert independent_result["scope_domain_coverage_at_3"] == 1.0
    assert independent_result["scope_quota_coverage_at_8"] == 1.0

    legacy_case = dict(case)
    legacy_case.pop("top3_required_scope_groups")
    fallback_result = benchmark.evaluate_case(legacy_case, chunks, 1.0)
    assert fallback_result["scope_domain_coverage_at_3"] == 0.75
    assert fallback_result["scope_quota_coverage_at_8"] == 1.0


def test_prompt_metrics_detect_split_critical_evidence_bundle() -> None:
    case = {
        "case_id": "scenario::task",
        "scenario_id": "scenario",
        "task_id": "task",
        "phase": "coach",
        "scenario_context": {"career_elements_applicable": False},
        "required_term_groups": [["WHAT"], ["HOW"], ["70%"], ["30%"]],
        "required_scope_groups": [["performance"]],
        "required_scope_minimums": {"performance": 1},
        "critical_profiles": {
            "asr": {
                "required_term_groups": [
                    ["WHAT"],
                    ["HOW"],
                    ["70%"],
                    ["30%"],
                ],
                "required_scope_groups": [["performance"]],
            }
        },
        "review_rules": [],
        "manual_review_context": {},
    }
    split_chunks = [
        RetrievedChunk(
            chunk_id="what",
            source_id="asr.md",
            title="ASR WHAT",
            scope="performance",
            text="WHAT 70%",
        ),
        RetrievedChunk(
            chunk_id="how",
            source_id="asr.md",
            title="ASR HOW",
            scope="performance",
            text="HOW 30%",
        ),
    ]

    split_result = benchmark.evaluate_case(case, split_chunks, 1.0)

    assert split_result["critical_evidence_failures"] == []
    assert split_result["prompt_critical_evidence_failures"] == []
    assert split_result["structured_bundle_failures"] == ["asr"]

    complete_result = benchmark.evaluate_case(
        case,
        [
            RetrievedChunk(
                chunk_id="asr",
                source_id="asr.md",
                title="ASR",
                scope="performance",
                text="WHAT 70% HOW 30%",
            )
        ],
        1.0,
    )

    assert complete_result["structured_bundle_failures"] == []


def test_prompt_metrics_detect_evidence_lost_to_coach_budget() -> None:
    case = {
        "case_id": "scenario::task",
        "scenario_id": "scenario",
        "task_id": "task",
        "phase": "coach",
        "scenario_context": {"career_elements_applicable": False},
        "required_term_groups": [["TAIL-EVIDENCE"]],
        "required_scope_groups": [["performance"]],
        "required_scope_minimums": {"performance": 1},
        "critical_profiles": {
            "tail": {
                "required_term_groups": [["TAIL-EVIDENCE"]],
                "required_scope_groups": [["performance"]],
            }
        },
        "review_rules": [],
        "manual_review_context": {},
    }
    chunks = [
        RetrievedChunk(
            chunk_id="long",
            source_id="performance.md",
            title="Performance",
            scope="performance",
            text="RETRIEVED-ANCHOR",
            metadata={
                "generation_context_override": (
                    ("前置背景" * 100)
                    + " RETRIEVED-ANCHOR "
                    + ("后置背景" * 100)
                    + " TAIL-EVIDENCE"
                )
            },
        )
    ]

    result = benchmark.evaluate_case(
        case,
        chunks,
        1.0,
        coach_rag_context_max_chars=80,
    )

    assert result["term_coverage_at_8"] == 1.0
    assert result["critical_evidence_failures"] == []
    assert result["prompt_term_coverage_at_8"] == 0.0
    assert result["prompt_critical_evidence_failures"] == [
        "tail:missing_terms_at_8"
    ]
    assert result["prompt_budget_truncated_chunk_count_at_8"] == 1
    assert result["prompt_anchor_loss_count_at_8"] == 0


def test_prompt_metrics_detect_retrieval_anchor_lost_to_tiny_budget() -> None:
    long_anchor = "ANCHOR-" * 20
    case = {
        "case_id": "scenario::task",
        "scenario_id": "scenario",
        "task_id": "task",
        "phase": "coach",
        "scenario_context": {"career_elements_applicable": False},
        "required_term_groups": [[long_anchor]],
        "required_scope_groups": [["performance"]],
        "required_scope_minimums": {"performance": 1},
        "critical_profiles": {},
        "review_rules": [],
        "manual_review_context": {},
    }
    result = benchmark.evaluate_case(
        case,
        [
            RetrievedChunk(
                chunk_id="long-anchor",
                source_id="performance.md",
                title="Performance",
                scope="performance",
                text=long_anchor,
                metadata={
                    "generation_context_override": (
                        ("前置背景" * 100) + long_anchor + ("后置背景" * 100)
                    )
                },
            )
        ],
        1.0,
        coach_rag_context_max_chars=80,
    )

    assert result["term_coverage_at_8"] == 1.0
    assert result["prompt_term_coverage_at_8"] == 0.0
    assert result["prompt_budget_truncated_chunk_count_at_8"] == 1
    assert result["prompt_anchor_loss_count_at_8"] == 1


def test_prompt_metrics_count_exact_payload_deduplication() -> None:
    case = {
        "case_id": "scenario::task",
        "scenario_id": "scenario",
        "task_id": "task",
        "phase": "guidance",
        "scenario_context": {"career_elements_applicable": False},
        "required_term_groups": [["完整证据"]],
        "required_scope_groups": [["performance"]],
        "required_scope_minimums": {"performance": 1},
        "critical_profiles": {},
        "review_rules": [],
        "manual_review_context": {},
    }
    chunks = [
        RetrievedChunk(
            chunk_id=chunk_id,
            source_id="performance.md",
            title="Performance",
            scope="performance",
            text=f"child-{chunk_id}",
            metadata={"generation_context_override": "完整证据"},
        )
        for chunk_id in ("first", "duplicate")
    ]

    result = benchmark.evaluate_case(case, chunks, 1.0)

    assert result["prompt_deduplicated_chunk_count_at_8"] == 1
    assert result["prompt_context_chars_at_8"] == len("完整证据")
    assert result["prompt_term_coverage_at_8"] == 1.0


@pytest.mark.parametrize(
    ("job_level_text", "expected_failures"),
    [
        (
            "Mercer P4 M2 结果 能力 行为",
            ["job_level:missing_terms_at_8"],
        ),
        ("Bosch SL2 结果 能力 行为", []),
    ],
)
def test_critical_terms_must_come_from_the_profile_scope(
    job_level_text,
    expected_failures,
) -> None:
    case = {
        "case_id": "scenario::task",
        "scenario_id": "scenario",
        "task_id": "task",
        "phase": "coach",
        "scenario_context": {"career_elements_applicable": True},
        "required_term_groups": [["SL2"], ["结果"], ["能力"], ["行为"]],
        "required_scope_groups": [["job_level"]],
        "required_scope_minimums": {"job_level": 1},
        "critical_profiles": {
            "job_level": {
                "required_term_groups": [
                    ["SL2"],
                    ["结果"],
                    ["能力"],
                    ["行为"],
                ],
                "required_scope_groups": [["job_level"]],
            }
        },
        "review_rules": [],
        "manual_review_context": {},
    }
    chunks = [
        RetrievedChunk(
            chunk_id="career",
            source_id="career.md",
            title="Career Elements",
            scope="career",
            text="目标 SL2",
            metadata={},
        ),
        RetrievedChunk(
            chunk_id="job",
            source_id="job.md",
            title="Job level",
            scope="job_level",
            text=job_level_text,
            metadata={},
        ),
    ]

    result = benchmark.evaluate_case(case, chunks, 1.0)

    assert result["critical_evidence_failures"] == expected_failures
    assert "job_level:missing_scope_at_8" not in result[
        "critical_evidence_failures"
    ]


def test_scope_coverage_counts_configured_quotas_and_skips_inapplicable_career() -> None:
    base_case = {
        "case_id": "scenario::task",
        "scenario_id": "scenario",
        "task_id": "task",
        "phase": "guidance",
        "required_term_groups": [],
        "required_scope_groups": [
            ["development_dialog"],
            ["career"],
            ["culture"],
        ],
        "required_scope_minimums": {
            "development_dialog": 2,
            "career": 2,
            "culture": 2,
        },
        "critical_profiles": {},
        "review_rules": [],
        "manual_review_context": {},
    }
    chunks = [
        RetrievedChunk(
            chunk_id=f"chunk-{index}",
            source_id=f"{scope}.md",
            title=scope,
            scope=scope,
            text=scope,
            metadata={},
        )
        for index, scope in enumerate(
            [
                "development_dialog",
                "career",
                "culture",
                "development_dialog",
                "career",
                "culture",
            ]
        )
    ]

    applicable = {
        **base_case,
        "scenario_context": {"career_elements_applicable": True},
    }
    applicable_result = benchmark.evaluate_case(applicable, chunks, 1.0)
    assert applicable_result["scope_domain_coverage_at_3"] == 1.0
    assert applicable_result["scope_quota_coverage_at_8"] == 1.0
    assert applicable_result["required_scope_minimums"]["career"] == 2
    assert applicable_result["inapplicable_career_prompt_chars_at_8"] == 0

    inapplicable = {
        **base_case,
        "scenario_context": {"career_elements_applicable": False},
    }
    inapplicable_result = benchmark.evaluate_case(
        inapplicable,
        [chunks[0], chunks[2], chunks[3], chunks[5]],
        1.0,
    )
    assert inapplicable_result["scope_domain_coverage_at_3"] == 1.0
    assert inapplicable_result["scope_quota_coverage_at_8"] == 1.0
    assert "career" not in inapplicable_result["required_scope_minimums"]
    assert inapplicable_result["inapplicable_career_prompt_chars_at_8"] == 0

    leaked_career_result = benchmark.evaluate_case(
        inapplicable,
        chunks,
        1.0,
    )
    assert leaked_career_result["inapplicable_career_prompt_chars_at_8"] > 0


def test_production_context_matches_guidance_and_coach_boundaries() -> None:
    cases = benchmark.validate_and_expand_fixture(benchmark.load_fixture())
    guidance_case = next(
        case for case in cases if case["task_id"] == "guidance_emotion"
    )
    coach_case = next(
        case for case in cases if case["task_id"] == "emotion_evaluation"
    )

    guidance = benchmark.production_retrieval_context(
        guidance_case,
        company_value_terms="House of Orientation 协同共进 创变未来",
    )
    assert guidance["company_value_terms"].startswith("House of Orientation")
    assert "emotion_state" not in guidance
    assert "conversation" not in guidance
    assert guidance["knowledge_skill_context"]["conversation"] == []
    assert (
        guidance["profile"]["current_career_elements"]
        == guidance["current_career_elements"]
    )
    assert set(guidance["motivation"]) == {
        "primary_motive_id",
        "secondary_motive_ids",
    }

    coach = benchmark.production_retrieval_context(
        coach_case,
        company_value_terms="must not leak into coach context",
    )
    assert "company_value_terms" not in coach
    assert coach["emotion_state"]
    assert coach["conversation"]
    assert coach["knowledge_skill_context"]["conversation"] == coach["conversation"]
    assert (
        coach["knowledge_skill_context"]["intent_id"]
        == coach["intent"]["id"]
    )


def test_fixture_scope_quotas_match_live_query_config() -> None:
    from backend.business_config.loader import get_config_loader

    fixture = benchmark.load_fixture()
    configured = get_config_loader().query_config()["queries"]
    for task in fixture["tasks"]:
        assert task["required_scope_minimums"] == configured[
            task["task_id"]
        ]["citation_scope_minimums"]


def test_fixture_accepts_source_grounded_semantic_alternatives() -> None:
    tasks = {
        task["task_id"]: task
        for task in benchmark.load_fixture()["tasks"]
    }
    for task_id in ("guidance_start", "opening_evaluation"):
        opening_terms = set(tasks[task_id]["required_term_groups"][-1])
        assert {"确保员工知晓", "确保员工了解"} <= opening_terms

    for task_id in (
        "guidance_requirement",
        "output_expectations_evaluation",
    ):
        groups = tasks[task_id]["required_term_groups"]
        assert {"目标与结果对齐", "实际结果"} <= set(groups[0])
        assert {"差距 / 偏差", "差距/偏差"} <= set(groups[1])


def test_job_level_ground_truth_changes_by_task_and_intent() -> None:
    cases = {
        case["case_id"]: case
        for case in benchmark.validate_and_expand_fixture(
            benchmark.load_fixture()
        )
    }
    expected = {
        "g8_development_collaboration::guidance_requirement": ["G8"],
        "g8_development_collaboration::guidance_plan": ["G9"],
        "g9_mixed_improvement_collaboration::guidance_requirement": ["G9"],
        "g9_mixed_improvement_collaboration::guidance_plan": ["G9", "SL1"],
        "sl1_improvement_exit_no_career::guidance_requirement": ["SL1"],
        "sl1_improvement_exit_no_career::guidance_plan": ["SL1"],
    }
    for case_id, levels in expected.items():
        assert cases[case_id]["manual_review_context"][
            "expected_job_levels"
        ] == levels
        assert cases[case_id]["critical_profiles"]["job_level"][
            "expected_levels"
        ] == levels


def test_plan_cases_require_the_scenario_culture_evidence() -> None:
    cases = benchmark.validate_and_expand_fixture(benchmark.load_fixture())
    plans = [
        case
        for case in cases
        if case["task_id"]
        in {"guidance_plan", "development_plan_evaluation"}
    ]
    assert len(plans) == 12
    assert all("culture" in case["critical_profiles"] for case in plans)
    for case in plans:
        culture_term = case["manual_review_context"]["culture_term"]
        culture_groups = case["critical_profiles"]["culture"][
            "required_term_groups"
        ]
        assert any(culture_term in group for group in culture_groups)


def test_short_ascii_terms_use_boundaries_and_percent_is_preserved() -> None:
    groups = [["WHAT"], ["HOW"], ["ASR"], ["70%"], ["30%"]]
    assert benchmark._term_coverage(
        ["whatever shows some ASR framework at 70 and 30 percent"],
        groups,
    ) == pytest.approx(0.2)
    assert benchmark._term_coverage(
        ["WHAT and HOW are used for ASR with 70% and 30%."],
        groups,
    ) == 1.0


def test_customer_culture_profile_accepts_actual_source_wording() -> None:
    cases = benchmark.validate_and_expand_fixture(benchmark.load_fixture())
    case = next(
        item
        for item in cases
        if item["case_id"]
        == "sl1_development_all_elements_customer::guidance_requirement"
    )
    culture_groups = case["critical_profiles"]["culture"][
        "required_term_groups"
    ]

    assert benchmark._term_coverage(
        [
            "Customer-centricity to GROW；uncover hidden needs and pain "
            "points；Deliver end-to-end solutions."
        ],
        culture_groups,
    ) == 1.0


def _manual_result(case_id: str, suffix: str) -> dict:
    return {
        "case_id": case_id,
        "results": [
            {
                "rank": rank,
                "chunk_id": f"chunk-{suffix}-{rank}",
                "source_id": f"source-{suffix}-{rank}.md",
                "content_hash": f"hash-{suffix}-{rank}",
            }
            for rank in range(1, 4)
        ],
    }


def _manual_judgment(
    result: dict,
    grades: list[int],
    *,
    critical_business_violation: bool,
) -> dict:
    return {
        "results": [
            {
                "rank": item["rank"],
                "chunk_id": item["chunk_id"],
                "source_id": item["source_id"],
                "content_hash": item["content_hash"],
                "grade": grade,
            }
            for item, grade in zip(result["results"], grades, strict=True)
        ],
        "critical_business_violation": critical_business_violation,
        "notes": "Optional reviewer context.",
    }


def test_manual_grading_fixture_documents_identity_bound_results() -> None:
    example = benchmark.load_fixture()["manual_grading"]["judgment_format"][
        "cases"
    ]["scenario_id::task_id"]

    assert len(example["results"]) == 3
    assert [item["rank"] for item in example["results"]] == [1, 2, 3]
    assert all(
        set(item) == {
            "rank",
            "chunk_id",
            "source_id",
            "content_hash",
            "grade",
        }
        for item in example["results"]
    )
    assert isinstance(example["critical_business_violation"], bool)
    assert isinstance(example["notes"], str)


def test_manual_review_context_and_identity_bound_judgment_gates_are_executable(
) -> None:
    cases = benchmark.validate_and_expand_fixture(benchmark.load_fixture())
    assert all(case["manual_review_context"]["asr_combination"] for case in cases)
    assert all(case["manual_review_context"]["career_state"] for case in cases)
    assert all(case["manual_review_context"]["culture_term"] for case in cases)

    results = [
        _manual_result(case["case_id"], str(index))
        for index, case in enumerate(cases[:2], start=1)
    ]
    judgments = {
        results[0]["case_id"]: _manual_judgment(
            results[0],
            [3, 2, 1],
            critical_business_violation=False,
        ),
        results[1]["case_id"]: _manual_judgment(
            results[1],
            [2, 3, 1],
            critical_business_violation=True,
        ),
    }
    metrics = benchmark.evaluate_manual_judgments(results, judgments)
    assert metrics["top1_usable_rate"] == 1.0
    assert metrics["top1_direct_complete_rate"] == 0.5
    assert metrics["top1_grade_zero_count"] == 0
    assert 0 < metrics["normalized_dcg_at_3"] <= 1
    assert metrics["critical_business_violation_count"] == 1


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("rank", 2),
        ("chunk_id", "different-chunk"),
        ("source_id", "different-source.md"),
        ("content_hash", "different-content-hash"),
    ],
)
def test_manual_judgment_rejects_stale_top3_identity(
    field: str,
    invalid_value: object,
) -> None:
    result = _manual_result("scenario::task", "identity")
    judgment = _manual_judgment(
        result,
        [3, 2, 1],
        critical_business_violation=False,
    )
    judgment["results"][0][field] = invalid_value

    with pytest.raises(benchmark.BenchmarkContractError):
        benchmark.evaluate_manual_judgments(
            [result],
            {result["case_id"]: judgment},
        )


def test_manual_judgment_requires_exactly_three_bound_results() -> None:
    result = _manual_result("scenario::task", "missing")
    judgment = _manual_judgment(
        result,
        [3, 2, 1],
        critical_business_violation=False,
    )
    judgment["results"] = judgment["results"][:2]

    with pytest.raises(benchmark.BenchmarkContractError):
        benchmark.evaluate_manual_judgments(
            [result],
            {result["case_id"]: judgment},
        )


def test_manual_judgment_requires_boolean_business_violation() -> None:
    result = _manual_result("scenario::task", "violation")
    judgment = _manual_judgment(
        result,
        [3, 2, 1],
        critical_business_violation=False,
    )
    invalid_judgment = deepcopy(judgment)
    invalid_judgment["critical_business_violation"] = "false"

    with pytest.raises(benchmark.BenchmarkContractError):
        benchmark.evaluate_manual_judgments(
            [result],
            {result["case_id"]: invalid_judgment},
        )


def test_fixture_rejects_a_missing_production_task() -> None:
    fixture = benchmark.load_fixture()
    fixture["tasks"] = fixture["tasks"][:-1]

    with pytest.raises(
        benchmark.BenchmarkContractError,
        match="exactly eight tasks",
    ):
        benchmark.validate_and_expand_fixture(fixture)


def test_comparison_gates_use_baseline_minus_candidate() -> None:
    fixture = benchmark.load_fixture()
    baseline_report = {
        "benchmark_id": fixture["benchmark_id"],
        "schema_version": fixture["schema_version"],
        "case_count": fixture["case_count"],
        "effective_top_k": fixture["top_k"],
        "metrics": {
            "mean_term_coverage_at_3": 0.98,
            "critical_full_coverage_at_8_rate": 0.97,
        },
        "manual_metrics": {"top1_usable_rate": 0.96},
    }

    regressions, gates = benchmark.evaluate_comparison(
        fixture=fixture,
        baseline_report=baseline_report,
        candidate_metrics={
            "mean_term_coverage_at_3": 0.97,
            "critical_full_coverage_at_8_rate": 0.95,
        },
        candidate_manual_metrics={"top1_usable_rate": 0.95},
        candidate_case_count=fixture["case_count"],
        effective_top_k=fixture["top_k"],
    )

    assert regressions == {
        "mean_term_coverage_at_3_regression": 0.01,
        "critical_full_coverage_at_8_rate_regression": 0.02,
        "top1_usable_rate_regression": 0.01,
    }
    assert all(gate["passed"] for gate in gates)


@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    [
        ("benchmark_id", "other", "benchmark_id"),
        ("schema_version", "other", "schema_version"),
        ("case_count", 47, "case_count"),
        ("effective_top_k", 3, "effective_top_k"),
    ],
)
def test_comparison_rejects_incompatible_baseline_report(
    field: str,
    invalid_value: object,
    message: str,
) -> None:
    fixture = benchmark.load_fixture()
    baseline_report = {
        "benchmark_id": fixture["benchmark_id"],
        "schema_version": fixture["schema_version"],
        "case_count": fixture["case_count"],
        "effective_top_k": fixture["top_k"],
        "metrics": {
            "mean_term_coverage_at_3": 1.0,
            "critical_full_coverage_at_8_rate": 1.0,
        },
        "manual_metrics": {"top1_usable_rate": 1.0},
    }
    baseline_report[field] = invalid_value

    with pytest.raises(benchmark.BenchmarkContractError, match=message):
        benchmark.evaluate_comparison(
            fixture=fixture,
            baseline_report=baseline_report,
            candidate_metrics={
                "mean_term_coverage_at_3": 1.0,
                "critical_full_coverage_at_8_rate": 1.0,
            },
            candidate_manual_metrics={"top1_usable_rate": 1.0},
            candidate_case_count=fixture["case_count"],
            effective_top_k=fixture["top_k"],
        )


def test_comparison_requires_every_configured_metric() -> None:
    fixture = benchmark.load_fixture()
    baseline_report = {
        "benchmark_id": fixture["benchmark_id"],
        "schema_version": fixture["schema_version"],
        "case_count": fixture["case_count"],
        "effective_top_k": fixture["top_k"],
        "metrics": {
            "mean_term_coverage_at_3": 1.0,
            "critical_full_coverage_at_8_rate": 1.0,
            "top1_usable_rate": 1.0,
        },
        "manual_metrics": {},
    }

    with pytest.raises(
        benchmark.BenchmarkContractError,
        match="baseline comparison metric is missing: top1_usable_rate",
    ):
        benchmark.evaluate_comparison(
            fixture=fixture,
            baseline_report=baseline_report,
            candidate_metrics={
                "mean_term_coverage_at_3": 1.0,
                "critical_full_coverage_at_8_rate": 1.0,
            },
            candidate_manual_metrics={"top1_usable_rate": 1.0},
            candidate_case_count=fixture["case_count"],
            effective_top_k=fixture["top_k"],
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            [
                "--case",
                "g8_development_collaboration::guidance_start",
                "--judgments",
                "judgments.json",
                "--baseline-report",
                "baseline.json",
            ],
            "complete fixture case matrix",
        ),
        (
            [
                "--top-k",
                "3",
                "--judgments",
                "judgments.json",
                "--baseline-report",
                "baseline.json",
            ],
            "effective_top_k",
        ),
        (["--baseline-report", "baseline.json"], "manual judgments"),
        (["--judgments", "judgments.json"], "baseline-report"),
    ],
)
def test_enforced_release_rejects_ineligible_invocation_before_retrieval(
    arguments: list[str],
    message: str,
) -> None:
    with pytest.raises(benchmark.BenchmarkContractError, match=message):
        benchmark.main(["--enforce-gates", *arguments])


def _compatible_baseline_report() -> dict:
    fixture = benchmark.load_fixture()
    return {
        "benchmark_id": fixture["benchmark_id"],
        "schema_version": fixture["schema_version"],
        "case_count": fixture["case_count"],
        "effective_top_k": fixture["top_k"],
        "metrics": {
            "mean_term_coverage_at_3": 1.0,
            "critical_full_coverage_at_8_rate": 1.0,
        },
        "manual_metrics": {"top1_usable_rate": 1.0},
    }


def test_enforced_release_validates_judgment_completeness_before_retrieval(
    monkeypatch,
) -> None:
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("invalid judgments must fail before retrieval")

    monkeypatch.setattr(benchmark.asyncio, "run", unexpected_run)
    monkeypatch.setattr(benchmark, "load_manual_judgments", lambda _path: {})
    monkeypatch.setattr(
        benchmark,
        "load_baseline_report",
        lambda _path: _compatible_baseline_report(),
    )

    with pytest.raises(
        benchmark.BenchmarkContractError,
        match="manual judgments missing cases",
    ):
        benchmark.main(
            [
                "--enforce-gates",
                "--judgments",
                "judgments.json",
                "--baseline-report",
                "baseline.json",
            ]
        )


def test_enforced_release_validates_required_baseline_metric_before_retrieval(
    monkeypatch,
) -> None:
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("invalid baseline must fail before retrieval")

    cases = benchmark.validate_and_expand_fixture(benchmark.load_fixture())
    judgments = {
        case["case_id"]: {
            "results": [
                {
                    "rank": rank,
                    "chunk_id": f"chunk-{rank}",
                    "source_id": f"source-{rank}.md",
                    "content_hash": f"hash-{rank}",
                    "grade": 3,
                }
                for rank in range(1, 4)
            ],
            "critical_business_violation": False,
        }
        for case in cases
    }
    baseline_report = _compatible_baseline_report()
    baseline_report["metrics"]["top1_usable_rate"] = 1.0
    baseline_report["manual_metrics"] = {}
    monkeypatch.setattr(benchmark.asyncio, "run", unexpected_run)
    monkeypatch.setattr(
        benchmark,
        "load_manual_judgments",
        lambda _path: judgments,
    )
    monkeypatch.setattr(
        benchmark,
        "load_baseline_report",
        lambda _path: baseline_report,
    )

    with pytest.raises(
        benchmark.BenchmarkContractError,
        match="baseline comparison metric is missing: top1_usable_rate",
    ):
        benchmark.main(
            [
                "--enforce-gates",
                "--judgments",
                "judgments.json",
                "--baseline-report",
                "baseline.json",
            ]
        )


def test_exploratory_subset_validate_only_is_allowed_but_not_release_eligible(
    capsys,
) -> None:
    case_id = "g8_development_collaboration::guidance_start"

    assert benchmark.main(["--validate-only", "--case", case_id]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["case_count"] == 1
    assert report["effective_top_k"] == 8
    assert report["complete_case_matrix"] is False
    assert report["release_eligible"] is False

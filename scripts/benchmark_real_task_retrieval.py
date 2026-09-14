from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
import unicodedata
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_FIXTURE = (
    PROJECT_ROOT / "data/benchmarks/real_task_retrieval_v1.json"
)
EXPECTED_TASK_IDS = (
    "guidance_start",
    "guidance_emotion",
    "guidance_requirement",
    "guidance_plan",
    "opening_evaluation",
    "emotion_evaluation",
    "output_expectations_evaluation",
    "development_plan_evaluation",
)
OPERATORS = {
    ">=": lambda actual, expected: actual >= expected,
    "<=": lambda actual, expected: actual <= expected,
}
FORMAL_CAREER_ELEMENTS = {
    "Cross-divisional experience",
    "Cross-functional experience",
    "International experience",
    "Associate leadership experience",
    "Project leadership experience",
}
MANUAL_COMPARISON_METRICS = frozenset({"top1_usable_rate"})
LEVEL_ORDER = {level: index for index, level in enumerate(
    ("G6", "G7", "G8", "G9", "SL1", "SL2", "SL3", "SL4")
)}


class BenchmarkContractError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BenchmarkContractError(message)


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), "benchmark fixture must be an object")
    return payload


def _groups(value: Any, *, label: str) -> list[list[str]]:
    _require(isinstance(value, list), f"{label} must be a list")
    output: list[list[str]] = []
    for index, raw_group in enumerate(value):
        _require(
            isinstance(raw_group, list) and raw_group,
            f"{label}[{index}] must be a non-empty list",
        )
        group = [str(item).strip() for item in raw_group if str(item).strip()]
        _require(bool(group), f"{label}[{index}] has no usable alternatives")
        output.append(group)
    return output


def _deduplicate_groups(groups: Iterable[list[str]]) -> list[list[str]]:
    output: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for group in groups:
        key = tuple(sorted(_normalize(item) for item in group))
        if key and key not in seen:
            output.append(list(group))
            seen.add(key)
    return output


def _validate_career_state(scenario: dict[str, Any], context: dict[str, Any]) -> None:
    scenario_id = str(scenario["scenario_id"])
    level = str(context["profile"].get("level") or "")
    intent_id = str((context.get("intent") or {}).get("id") or "")
    applicable = bool(context.get("career_elements_applicable"))
    expected_applicable = (
        LEVEL_ORDER.get(level, -1) >= LEVEL_ORDER["G9"]
        and intent_id in {"development", "development_improvement"}
    )
    _require(
        applicable == expected_applicable,
        f"{scenario_id} Career Elements applicability conflicts with level/intent",
    )
    current = [str(item) for item in context.get("current_career_elements") or []]
    _require(
        set(current) <= FORMAL_CAREER_ELEMENTS,
        f"{scenario_id} contains an unknown Career Element",
    )
    state = str(scenario.get("career_state") or "")
    if state == "unknown":
        _require(applicable and not current, f"{scenario_id} unknown state is invalid")
    elif state == "known_partial":
        _require(
            applicable and 0 < len(set(current)) < len(FORMAL_CAREER_ELEMENTS),
            f"{scenario_id} partial Career state is invalid",
        )
    elif state == "known_all_five":
        _require(
            applicable and set(current) == FORMAL_CAREER_ELEMENTS,
            f"{scenario_id} all-five Career state is invalid",
        )
    elif state == "not_applicable":
        _require(not applicable, f"{scenario_id} not-applicable state is invalid")
    else:
        raise BenchmarkContractError(f"{scenario_id} has unknown Career state")


def _task_job_level_profile(
    profile: dict[str, Any],
    *,
    job_level_mode: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    output = deepcopy(profile)
    applicable_levels = [
        str(level) for level in profile.get("applicable_levels") or []
    ]
    if not job_level_mode:
        return output
    current_level = str(context["profile"].get("level") or "")
    _require(
        current_level in applicable_levels,
        f"job-level profile does not include current level {current_level}",
    )
    intent_id = str((context.get("intent") or {}).get("id") or "")
    if job_level_mode == "current":
        expected_levels = [current_level]
    elif job_level_mode == "intent_plan":
        if intent_id == "development":
            expected_levels = [
                level for level in applicable_levels if level != current_level
            ]
        elif intent_id == "development_improvement":
            expected_levels = list(applicable_levels)
        else:
            expected_levels = [current_level]
    else:
        raise BenchmarkContractError(
            f"unsupported job_level_mode: {job_level_mode}"
        )
    _require(bool(expected_levels), "plan has no applicable job-level standard")
    level_set = {_normalize(level) for level in applicable_levels}
    non_level_groups = [
        group
        for group in _groups(
            profile.get("required_term_groups") or [],
            label="job_level.required_term_groups",
        )
        if not (len(group) == 1 and _normalize(group[0]) in level_set)
    ]
    output["required_term_groups"] = [
        *[[level] for level in expected_levels],
        *non_level_groups,
    ]
    output["expected_levels"] = expected_levels
    return output


def validate_and_expand_fixture(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = payload.get("tasks")
    scenarios = payload.get("scenarios")
    _require(isinstance(tasks, list), "tasks must be a list")
    _require(isinstance(scenarios, list), "scenarios must be a list")
    _require(len(tasks) == 8, "benchmark must define exactly eight tasks")
    _require(len(scenarios) == 6, "benchmark must define exactly six scenarios")

    task_ids = [str(task.get("task_id") or "") for task in tasks]
    _require(
        tuple(task_ids) == EXPECTED_TASK_IDS,
        "tasks must match the eight production retrieval task ids in order",
    )
    scenario_ids = [
        str(scenario.get("scenario_id") or "") for scenario in scenarios
    ]
    _require(
        len(set(scenario_ids)) == len(scenario_ids) and all(scenario_ids),
        "scenario ids must be non-empty and unique",
    )

    contract = payload.get("coverage_contract") or {}
    _require(
        {str(item) for item in contract.get("levels") or []}
        == {"G8", "G9", "SL1"},
        "coverage contract must include G8, G9 and SL1",
    )
    _require(
        {str(item) for item in contract.get("culture_terms") or []}
        == {"协同共进", "创变未来", "聚力共赢", "使命必达"},
        "coverage contract must include all four culture terms",
    )
    _require(
        {str(item) for item in contract.get("career_states") or []}
        == {"not_applicable", "unknown", "known_partial", "known_all_five"},
        "coverage contract must include all four Career Elements states",
    )
    _require(
        len({str(item) for item in contract.get("asr_combinations") or []})
        == 6,
        "coverage contract must include six distinct WHAT/HOW combinations",
    )

    cases: list[dict[str, Any]] = []
    for scenario in scenarios:
        scenario_id = str(scenario["scenario_id"])
        context = scenario.get("context")
        profiles = scenario.get("evidence_profiles")
        _require(isinstance(context, dict), f"{scenario_id}.context must be an object")
        _require(
            isinstance(context.get("profile"), dict),
            f"{scenario_id}.context.profile must be an object",
        )
        _require(
            str(context["profile"].get("level") or "")
            == str(scenario.get("level") or ""),
            f"{scenario_id} level does not match its context",
        )
        _require(
            isinstance(profiles, dict),
            f"{scenario_id}.evidence_profiles must be an object",
        )
        _validate_career_state(scenario, context)
        for task in tasks:
            task_id = str(task["task_id"])
            phase = str(task.get("phase") or "").strip()
            job_level_mode = str(task.get("job_level_mode") or "").strip()
            _require(
                phase in {"guidance", "coach"},
                f"{task_id}.phase must be guidance or coach",
            )
            raw_scope_minimums = task.get("required_scope_minimums") or {}
            _require(
                isinstance(raw_scope_minimums, dict),
                f"{task_id}.required_scope_minimums must be an object",
            )
            scope_minimums: dict[str, int] = {}
            for raw_scope, raw_minimum in raw_scope_minimums.items():
                scope = str(raw_scope).strip()
                _require(bool(scope), f"{task_id} has an empty required scope")
                try:
                    minimum = int(raw_minimum)
                except (TypeError, ValueError) as exc:
                    raise BenchmarkContractError(
                        f"{task_id}.{scope} minimum must be an integer"
                    ) from exc
                _require(
                    minimum > 0,
                    f"{task_id}.{scope} minimum must be positive",
                )
                scope_minimums[scope] = minimum
            term_groups = _groups(
                task.get("required_term_groups") or [],
                label=f"{task_id}.required_term_groups",
            )
            top3_term_groups = _groups(
                task.get("top3_required_term_groups")
                if "top3_required_term_groups" in task
                else task.get("required_term_groups") or [],
                label=f"{task_id}.top3_required_term_groups",
            )
            raw_top3_profile_ids = task.get("top3_evidence_profiles") or []
            _require(
                isinstance(raw_top3_profile_ids, list),
                f"{task_id}.top3_evidence_profiles must be a list",
            )
            top3_profile_ids = [
                str(profile_id).strip()
                for profile_id in raw_top3_profile_ids
            ]
            _require(
                all(top3_profile_ids),
                f"{task_id}.top3_evidence_profiles has an empty id",
            )
            _require(
                len(set(top3_profile_ids)) == len(top3_profile_ids),
                f"{task_id}.top3_evidence_profiles has duplicates",
            )
            declared_profile_ids = {
                str(profile_id)
                for profile_id in task.get("evidence_profiles") or []
            }
            _require(
                set(top3_profile_ids).issubset(declared_profile_ids),
                f"{task_id}.top3_evidence_profiles must be declared evidence profiles",
            )
            scope_groups = _groups(
                task.get("required_scope_groups") or [],
                label=f"{task_id}.required_scope_groups",
            )
            top3_scope_groups = _groups(
                task.get("top3_required_scope_groups")
                if "top3_required_scope_groups" in task
                else task.get("required_scope_groups") or [],
                label=f"{task_id}.top3_required_scope_groups",
            )
            active_profiles: dict[str, dict[str, Any]] = {}
            for profile_id in task.get("evidence_profiles") or []:
                profile = profiles.get(profile_id)
                _require(
                    isinstance(profile, dict),
                    f"{scenario_id} is missing evidence profile {profile_id}",
                )
                if not bool(profile.get("active")):
                    continue
                if profile_id == "job_level":
                    profile = _task_job_level_profile(
                        profile,
                        job_level_mode=job_level_mode,
                        context=context,
                    )
                active_profiles[str(profile_id)] = profile
                term_groups.extend(
                    _groups(
                        profile.get("required_term_groups") or [],
                        label=f"{scenario_id}.{profile_id}.required_term_groups",
                    )
                )
                if str(profile_id) in top3_profile_ids:
                    top3_term_groups.extend(
                        _groups(
                            profile.get("required_term_groups") or [],
                            label=(
                                f"{scenario_id}.{profile_id}."
                                "top3_required_term_groups"
                            ),
                        )
                    )
                profile_scope_groups = _groups(
                    profile.get("required_scope_groups") or [],
                    label=f"{scenario_id}.{profile_id}.required_scope_groups",
                )
                scope_groups.extend(profile_scope_groups)
                if str(profile_id) in top3_profile_ids:
                    top3_scope_groups.extend(profile_scope_groups)
            _require(
                bool(top3_term_groups),
                f"{scenario_id}.{task_id} has no Top-3 evidence contract",
            )
            cases.append(
                {
                    "case_id": f"{scenario_id}::{task_id}",
                    "scenario_id": scenario_id,
                    "task_id": task_id,
                    "phase": phase,
                    "job_level_mode": job_level_mode,
                    "scenario_context": deepcopy(context),
                    "required_scope_minimums": scope_minimums,
                    "top3_required_term_groups": _deduplicate_groups(
                        top3_term_groups
                    ),
                    "required_term_groups": _deduplicate_groups(term_groups),
                    "top3_required_scope_groups": _deduplicate_groups(
                        top3_scope_groups
                    ),
                    "required_scope_groups": _deduplicate_groups(scope_groups),
                    "critical_profiles": {
                        profile_id: deepcopy(active_profiles[profile_id])
                        for profile_id in scenario.get("critical_profiles") or []
                        if profile_id in active_profiles
                    },
                    "review_rules": list(scenario.get("review_rules") or []),
                    "manual_review_context": {
                        "employee_level": str(scenario.get("level") or ""),
                        "intent_id": str((context.get("intent") or {}).get("id") or ""),
                        "career_state": str(scenario.get("career_state") or ""),
                        "current_career_elements": deepcopy(
                            context.get("current_career_elements") or []
                        ),
                        "asr_combination": str(
                            scenario.get("asr_combination") or ""
                        ),
                        "culture_term": str(scenario.get("culture_term") or ""),
                        "expected_job_levels": deepcopy(
                            (active_profiles.get("job_level") or {}).get(
                                "expected_levels"
                            )
                            or []
                        ),
                    },
                }
            )

    expected_count = int(payload.get("case_count") or 0)
    _require(expected_count == 48, "fixture case_count must be 48")
    _require(len(cases) == expected_count, "expanded case count must be 48")
    _require(
        len({case["case_id"] for case in cases}) == expected_count,
        "expanded case ids must be unique",
    )
    return cases


def production_retrieval_context(
    case: dict[str, Any],
    *,
    company_value_terms: str,
) -> dict[str, Any]:
    """Build the same context shape used by the production task family."""

    source = deepcopy(case["scenario_context"])
    intent = deepcopy(source.get("intent") or {})
    profile = deepcopy(source.get("profile") or {})
    current_career_elements = deepcopy(
        source.get("current_career_elements") or []
    )
    profile["current_career_elements"] = deepcopy(current_career_elements)
    supplemental_info = str(source.get("supplemental_info") or "")
    performance_context = str(source.get("performance_context") or "")
    conversation = deepcopy(source.get("conversation") or [])
    intent_id = str(intent.get("id") or intent.get("intent_id") or "")
    knowledge_skill_context = {
        "profile": deepcopy(profile),
        "supplemental_info": supplemental_info,
        "performance_context": performance_context,
        "conversation": (
            [] if case["phase"] == "guidance" else deepcopy(conversation)
        ),
        "intent_id": intent_id,
    }
    common = {
        "output_locale": source.get("output_locale") or "zh-CN",
        "intent": intent,
        "profile": profile,
        "supplemental_info": supplemental_info,
        "performance_context": performance_context,
        "personality": deepcopy(source.get("personality") or {}),
        "motivation": deepcopy(source.get("motivation") or {}),
        "run_mode": source.get("run_mode") or "guidance_then_rehearsal",
        "career_elements_applicable": bool(
            source.get("career_elements_applicable")
        ),
        "current_career_elements": deepcopy(
            current_career_elements
        ),
        "knowledge_skill_context": knowledge_skill_context,
    }
    if case["phase"] == "guidance":
        motivation = common["motivation"]
        common["motivation"] = {
            "primary_motive_id": motivation.get("primary_motive_id"),
            "secondary_motive_ids": list(
                motivation.get("secondary_motive_ids") or []
            ),
        }
        common["company_value_terms"] = str(company_value_terms or "")
        return common

    common.update(
        {
            "personality_behavior_profile": deepcopy(
                source.get("personality_behavior_profile") or {}
            ),
            "emotion_state": deepcopy(source.get("emotion_state") or {}),
            "conversation": conversation,
        }
    )
    return common


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(
        "".join(
            character if character.isalnum() or character == "%" else " "
            for character in text
        ).split()
    )


def _contains_term(combined: str, alternative: str) -> bool:
    needle = _normalize(alternative)
    if not needle:
        return False
    if re.fullmatch(r"[a-z0-9%]+", needle) and (
        len(needle.replace("%", "")) <= 4 or "%" in needle
    ):
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
                combined,
            )
        )
    return needle in combined


def _term_coverage(texts: Iterable[str], groups: list[list[str]]) -> float:
    if not groups:
        return 1.0
    combined = _normalize("\n".join(str(text or "") for text in texts))
    matched = sum(
        any(_contains_term(combined, alternative) for alternative in group)
        for group in groups
    )
    return matched / len(groups)


def _scope_minimum_coverage(
    scopes: Iterable[str],
    minimums: dict[str, int],
) -> float:
    if not minimums:
        return 1.0
    normalized_scopes = [_normalize(scope) for scope in scopes]
    matched = 0
    required = 0
    for raw_scope, raw_minimum in minimums.items():
        minimum = max(0, int(raw_minimum))
        if minimum <= 0:
            continue
        required += minimum
        normalized_scope = _normalize(raw_scope)
        matched += min(minimum, normalized_scopes.count(normalized_scope))
    return matched / required if required else 1.0


def _applicable_scope_minimums(case: dict[str, Any]) -> dict[str, int]:
    minimums = dict(case.get("required_scope_minimums") or {})
    if not bool(
        case.get("scenario_context", {}).get("career_elements_applicable")
    ):
        minimums.pop("career", None)
    return minimums


def _applicable_scope_groups(
    case: dict[str, Any],
    *,
    top3: bool = False,
) -> list[list[str]]:
    if top3 and "top3_required_scope_groups" in case:
        groups = deepcopy(case.get("top3_required_scope_groups") or [])
    else:
        groups = deepcopy(case.get("required_scope_groups") or [])
    if bool(
        case.get("scenario_context", {}).get("career_elements_applicable")
    ):
        return groups
    return [
        group
        for group in groups
        if not (len(group) == 1 and _normalize(group[0]) == "career")
    ]


def _scope_group_coverage(
    scopes: Iterable[str],
    groups: list[list[str]],
) -> float:
    if not groups:
        return 1.0
    available = {_normalize(scope) for scope in scopes}
    matched = sum(
        any(_normalize(alternative) in available for alternative in group)
        for group in groups
    )
    return matched / len(groups)


def _chunk_generation_context(chunk: Any) -> str:
    from backend.rag.parent_context import generation_context_text

    metadata = dict(getattr(chunk, "metadata", None) or {})
    return generation_context_text(getattr(chunk, "text", ""), metadata)


def _chunk_source_context(chunk: Any) -> str:
    metadata = dict(getattr(chunk, "metadata", None) or {})
    return str(
        metadata.get("parent_context")
        or getattr(chunk, "text", "")
        or ""
    ).strip()


def _chunk_document(chunk: Any) -> str:
    return "\n".join(
        (
            str(getattr(chunk, "title", "") or ""),
            _chunk_generation_context(chunk),
        )
    )


def _result_content_hash(*, generation_visible_text: str) -> str:
    """Return a stable identity for the complete prompt-visible passage body."""

    return hashlib.sha256(
        str(generation_visible_text or "").encode("utf-8")
    ).hexdigest()


def _prompt_effective_payload(
    case: dict[str, Any],
    chunks: list[Any],
    *,
    coach_rag_context_max_chars: int,
) -> list[dict[str, Any]]:
    selected = list(chunks[:8])
    if case.get("phase") == "guidance":
        from backend.agents.guidance_agent import GuidanceAgent
        from backend.business_config.loader import get_config_loader

        non_culture = [chunk for chunk in selected if chunk.scope != "culture"]
        company_values = GuidanceAgent._company_values_prompt_payload(
            get_config_loader().company_values()
        )
        culture_enabled = company_values.enabled and bool(company_values.values)
        culture = [
            chunk
            for chunk in selected
            if culture_enabled and chunk.scope == "culture"
        ]
        return [
            *GuidanceAgent._chunks_payload(non_culture),
            *GuidanceAgent._chunks_payload(culture),
        ]

    from backend.agents.coach_agent.generic_agent import GenericCoachAgent

    return GenericCoachAgent.chunks_payload(
        selected,
        max_text_chars=coach_rag_context_max_chars,
    )


def _critical_evidence_failures(
    case: dict[str, Any],
    documents: list[str],
    scopes: list[str],
) -> list[str]:
    failures: list[str] = []
    for profile_id, profile in case["critical_profiles"].items():
        profile_terms = _groups(
            profile.get("required_term_groups") or [],
            label=f"{case['case_id']}.{profile_id}.required_term_groups",
        )
        profile_scopes = _groups(
            profile.get("required_scope_groups") or [],
            label=f"{case['case_id']}.{profile_id}.required_scope_groups",
        )
        allowed_profile_scopes = {
            _normalize(alternative)
            for group in profile_scopes
            for alternative in group
        }
        scoped_profile_documents = [
            document
            for document, scope in zip(documents, scopes, strict=True)
            if _normalize(scope) in allowed_profile_scopes
        ]
        profile_documents = (
            scoped_profile_documents
            if allowed_profile_scopes
            else documents
        )
        if _term_coverage(profile_documents, profile_terms) < 1.0:
            failures.append(f"{profile_id}:missing_terms_at_8")
        available_scopes = {_normalize(scope) for scope in scopes}
        if any(
            not any(
                _normalize(alternative) in available_scopes
                for alternative in group
            )
            for group in profile_scopes
        ):
            failures.append(f"{profile_id}:missing_scope_at_8")
    return failures


def _structured_bundle_failures(
    case: dict[str, Any],
    documents: list[str],
    scopes: list[str],
) -> list[str]:
    failures: list[str] = []
    for profile_id, profile in case["critical_profiles"].items():
        profile_terms = _groups(
            profile.get("required_term_groups") or [],
            label=f"{case['case_id']}.{profile_id}.required_term_groups",
        )
        profile_scopes = _groups(
            profile.get("required_scope_groups") or [],
            label=f"{case['case_id']}.{profile_id}.required_scope_groups",
        )
        allowed_profile_scopes = {
            _normalize(alternative)
            for group in profile_scopes
            for alternative in group
        }
        scoped_documents = [
            document
            for document, scope in zip(documents, scopes, strict=True)
            if not allowed_profile_scopes
            or _normalize(scope) in allowed_profile_scopes
        ]
        best_single_document_coverage = max(
            (
                _term_coverage([document], profile_terms)
                for document in scoped_documents
            ),
            default=0.0,
        )
        if best_single_document_coverage < 1.0:
            failures.append(str(profile_id))
    return failures


def evaluate_case(
    case: dict[str, Any],
    chunks: list[Any],
    elapsed_ms: float,
    *,
    coach_rag_context_max_chars: int = 24_000,
) -> dict[str, Any]:
    documents = [_chunk_document(chunk) for chunk in chunks]
    scopes = [str(getattr(chunk, "scope", "") or "") for chunk in chunks]
    top_chunks = chunks[:8]
    generation_contexts = [
        _chunk_generation_context(chunk) for chunk in top_chunks
    ]
    source_contexts = [_chunk_source_context(chunk) for chunk in top_chunks]
    generation_context_chars = sum(len(text) for text in generation_contexts)
    source_context_chars = sum(len(text) for text in source_contexts)
    generation_context_reduction_rate = (
        1.0 - (generation_context_chars / source_context_chars)
        if source_context_chars
        else 0.0
    )
    focused_chunk_count = sum(
        bool(
            dict(getattr(chunk, "metadata", None) or {}).get(
                "generation_context_focused"
            )
        )
        for chunk in top_chunks
    )
    prompt_payload = _prompt_effective_payload(
        case,
        top_chunks,
        coach_rag_context_max_chars=coach_rag_context_max_chars,
    )
    prompt_documents = [
        "\n".join(
            (
                str(item.get("title") or ""),
                str(item.get("text") or ""),
            )
        )
        for item in prompt_payload
    ]
    prompt_scopes = [str(item.get("scope") or "") for item in prompt_payload]
    prompt_texts = [str(item.get("text") or "") for item in prompt_payload]
    prompt_text_by_chunk_id = {
        str(item.get("chunk_id") or ""): str(item.get("text") or "")
        for item in prompt_payload
    }
    prompt_context_chars = sum(
        len(str(item.get("text") or "")) for item in prompt_payload
    )
    prompt_term_at_8 = _term_coverage(
        prompt_documents,
        case["required_term_groups"],
    )
    prompt_critical_failures = _critical_evidence_failures(
        case,
        prompt_documents,
        prompt_scopes,
    )
    structured_bundle_failures = _structured_bundle_failures(
        case,
        prompt_documents,
        prompt_scopes,
    )
    prompt_budget_truncated_count = 0
    prompt_anchor_loss_count = 0
    for index, chunk in enumerate(top_chunks):
        chunk_id = str(getattr(chunk, "chunk_id", "") or "")
        generation_text = generation_contexts[index]
        prompt_text = prompt_text_by_chunk_id.get(chunk_id)
        if prompt_text is not None and prompt_text != generation_text:
            prompt_budget_truncated_count += 1
        child_text = str(getattr(chunk, "text", "") or "").strip()
        anchor_was_available = (
            bool(child_text)
            and child_text != generation_text
            and child_text in generation_text
        )
        if anchor_was_available and not any(
            child_text in candidate for candidate in prompt_texts
        ):
            prompt_anchor_loss_count += 1
    prompt_deduplicated_count = max(0, len(top_chunks) - len(prompt_payload))
    inapplicable_career_prompt_chars = (
        sum(
            len(str(item.get("text") or ""))
            for item in prompt_payload
            if _normalize(str(item.get("scope") or "")) == "career"
        )
        if not bool(
            (case.get("scenario_context") or {}).get(
                "career_elements_applicable"
            )
        )
        else 0
    )
    top3_term_groups = (
        case["top3_required_term_groups"]
        if "top3_required_term_groups" in case
        else case["required_term_groups"]
    )
    term_at_3 = _term_coverage(documents[:3], top3_term_groups)
    term_at_8 = _term_coverage(documents[:8], case["required_term_groups"])
    scope_minimums = _applicable_scope_minimums(case)
    top3_scope_groups = _applicable_scope_groups(case, top3=True)
    scope_domain_at_3 = _scope_group_coverage(
        scopes[:3],
        top3_scope_groups,
    )
    scope_quota_at_8 = _scope_minimum_coverage(scopes[:8], scope_minimums)

    critical_failures = _critical_evidence_failures(
        case,
        documents[:8],
        scopes[:8],
    )

    result_rows: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks[:8], start=1):
        chunk_id = str(getattr(chunk, "chunk_id", "") or "")
        prompt_text = prompt_text_by_chunk_id.get(chunk_id, "")
        excerpt = documents[index - 1][:1200]
        prompt_excerpt = prompt_text[:1200]
        child_text = str(getattr(chunk, "text", "") or "").strip()
        result_rows.append(
            {
                "rank": index,
                "chunk_id": chunk_id,
                "scope": str(getattr(chunk, "scope", "") or ""),
                "source_id": str(getattr(chunk, "source_id", "") or ""),
                "title": str(getattr(chunk, "title", "") or ""),
                "score": float(getattr(chunk, "score", 0.0) or 0.0),
                "generation_context_chars": len(generation_contexts[index - 1]),
                "source_context_chars": len(source_contexts[index - 1]),
                "generation_context_focused": bool(
                    dict(getattr(chunk, "metadata", None) or {}).get(
                        "generation_context_focused"
                    )
                ),
                "prompt_context_chars": len(prompt_text),
                "prompt_chunk_retained": chunk_id in prompt_text_by_chunk_id,
                "prompt_budget_truncated": (
                    chunk_id in prompt_text_by_chunk_id
                    and prompt_text != generation_contexts[index - 1]
                ),
                "prompt_anchor_retained": (
                    not (
                        child_text
                        and child_text != generation_contexts[index - 1]
                        and child_text in generation_contexts[index - 1]
                    )
                    or any(child_text in candidate for candidate in prompt_texts)
                ),
                "prompt_excerpt": prompt_excerpt,
                "excerpt": excerpt,
                "content_hash": _result_content_hash(
                    generation_visible_text=prompt_text,
                ),
            }
        )

    return {
        "case_id": case["case_id"],
        "scenario_id": case["scenario_id"],
        "task_id": case["task_id"],
        "elapsed_ms": round(elapsed_ms, 3),
        "returned_count": len(chunks),
        "term_coverage_at_3": round(term_at_3, 4),
        "term_coverage_at_8": round(term_at_8, 4),
        "full_term_coverage_at_8": term_at_8 == 1.0,
        "scope_domain_coverage_at_3": round(scope_domain_at_3, 4),
        "scope_quota_coverage_at_8": round(scope_quota_at_8, 4),
        "full_scope_quota_coverage_at_8": scope_quota_at_8 == 1.0,
        "generation_context_chars_at_8": generation_context_chars,
        "source_context_chars_at_8": source_context_chars,
        "generation_context_reduction_rate_at_8": round(
            generation_context_reduction_rate,
            4,
        ),
        "focused_chunk_count_at_8": focused_chunk_count,
        "prompt_context_chars_at_8": prompt_context_chars,
        "prompt_term_coverage_at_8": round(prompt_term_at_8, 4),
        "full_prompt_term_coverage_at_8": prompt_term_at_8 == 1.0,
        "prompt_critical_evidence_failures": prompt_critical_failures,
        "structured_bundle_failures": structured_bundle_failures,
        "prompt_budget_truncated_chunk_count_at_8": (
            prompt_budget_truncated_count
        ),
        "prompt_anchor_loss_count_at_8": prompt_anchor_loss_count,
        "prompt_deduplicated_chunk_count_at_8": prompt_deduplicated_count,
        "inapplicable_career_prompt_chars_at_8": (
            inapplicable_career_prompt_chars
        ),
        "required_scope_minimums": scope_minimums,
        "critical_evidence_failures": critical_failures,
        "review_rules": case["review_rules"],
        "manual_review_context": case["manual_review_context"],
        "results": result_rows,
    }


def _mean(results: list[dict[str, Any]], field: str) -> float:
    return round(
        sum(float(result[field]) for result in results) / max(1, len(results)),
        4,
    )


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "mean_term_coverage_at_3": _mean(results, "term_coverage_at_3"),
        "mean_term_coverage_at_8": _mean(results, "term_coverage_at_8"),
        "full_term_coverage_at_8_rate": _mean(
            results, "full_term_coverage_at_8"
        ),
        "mean_scope_domain_coverage_at_3": _mean(
            results, "scope_domain_coverage_at_3"
        ),
        "full_scope_quota_coverage_at_8_rate": _mean(
            results, "full_scope_quota_coverage_at_8"
        ),
        "mean_generation_context_chars_at_8": _mean(
            results, "generation_context_chars_at_8"
        ),
        "mean_source_context_chars_at_8": _mean(
            results, "source_context_chars_at_8"
        ),
        "mean_generation_context_reduction_rate_at_8": _mean(
            results, "generation_context_reduction_rate_at_8"
        ),
        "mean_focused_chunk_count_at_8": _mean(
            results, "focused_chunk_count_at_8"
        ),
        "mean_prompt_context_chars_at_8": _mean(
            results, "prompt_context_chars_at_8"
        ),
        "mean_prompt_term_coverage_at_8": _mean(
            results, "prompt_term_coverage_at_8"
        ),
        "full_prompt_term_coverage_at_8_rate": _mean(
            results, "full_prompt_term_coverage_at_8"
        ),
        "prompt_critical_evidence_failure_count": sum(
            len(result["prompt_critical_evidence_failures"])
            for result in results
        ),
        "structured_bundle_failure_count": sum(
            len(result["structured_bundle_failures"])
            for result in results
        ),
        "prompt_budget_truncated_chunk_count": sum(
            int(result["prompt_budget_truncated_chunk_count_at_8"])
            for result in results
        ),
        "prompt_anchor_loss_count": sum(
            int(result["prompt_anchor_loss_count_at_8"])
            for result in results
        ),
        "prompt_deduplicated_chunk_count": sum(
            int(result["prompt_deduplicated_chunk_count_at_8"])
            for result in results
        ),
        "inapplicable_career_prompt_chars_total": sum(
            int(result["inapplicable_career_prompt_chars_at_8"])
            for result in results
        ),
        "critical_evidence_failure_count": sum(
            len(result["critical_evidence_failures"]) for result in results
        ),
        "critical_full_coverage_at_8_rate": round(
            sum(not result["critical_evidence_failures"] for result in results)
            / max(1, len(results)),
            4,
        ),
        "mean_elapsed_ms": round(
            sum(float(result["elapsed_ms"]) for result in results)
            / max(1, len(results)),
            3,
        ),
    }


def evaluate_gates(
    metrics: dict[str, Any], gates: dict[str, Any]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for metric_name, rule in gates.items():
        operator = str(rule.get("operator") or "")
        _require(operator in OPERATORS, f"unsupported gate operator: {operator}")
        _require(metric_name in metrics, f"gate metric is missing: {metric_name}")
        actual = float(metrics[metric_name])
        threshold = float(rule["threshold"])
        output.append(
            {
                "metric": metric_name,
                "actual": actual,
                "operator": operator,
                "threshold": threshold,
                "passed": bool(OPERATORS[operator](actual, threshold)),
            }
        )
    return output


def load_manual_judgments(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_cases = payload.get("cases") if isinstance(payload, dict) else None
    _require(isinstance(raw_cases, dict), "manual judgments must contain cases")
    return {
        str(case_id): dict(judgment)
        for case_id, judgment in raw_cases.items()
        if isinstance(judgment, dict)
    }


def load_baseline_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), "baseline report must be an object")
    return payload


def _normalized_dcg(grades: list[int]) -> float:
    gains = [(2**grade) - 1 for grade in grades]
    dcg = sum(
        gain / math.log2(rank + 1)
        for rank, gain in enumerate(gains, start=1)
    )
    ideal = sorted(gains, reverse=True)
    idcg = sum(
        gain / math.log2(rank + 1)
        for rank, gain in enumerate(ideal, start=1)
    )
    return dcg / idcg if idcg else 0.0


def validate_manual_judgment_contract(
    case_ids: Iterable[str],
    judgments: dict[str, dict[str, Any]],
) -> None:
    """Validate review completeness and shape without retrieval identities."""

    expected_ids = {str(case_id) for case_id in case_ids}
    missing = sorted(expected_ids - set(judgments))
    _require(not missing, f"manual judgments missing cases: {missing}")
    for case_id in sorted(expected_ids):
        judgment = judgments[case_id]
        raw_results = judgment.get("results")
        _require(
            isinstance(raw_results, list) and len(raw_results) == 3,
            f"{case_id}.results must contain exactly three identity-bound grades",
        )
        for expected_rank, raw_result in enumerate(raw_results, start=1):
            _require(
                isinstance(raw_result, dict),
                f"{case_id}.results[{expected_rank - 1}] must be an object",
            )
            raw_rank = raw_result.get("rank")
            _require(
                isinstance(raw_rank, int)
                and not isinstance(raw_rank, bool)
                and raw_rank == expected_rank,
                f"{case_id}.results[{expected_rank - 1}] rank must be {expected_rank}",
            )
            for field in ("chunk_id", "source_id", "content_hash"):
                identity = raw_result.get(field)
                _require(
                    isinstance(identity, str) and bool(identity),
                    (
                        f"{case_id}.results[{expected_rank - 1}] "
                        f"{field} must be a non-empty string"
                    ),
                )
            raw_grade = raw_result.get("grade")
            _require(
                isinstance(raw_grade, int)
                and not isinstance(raw_grade, bool)
                and 0 <= raw_grade <= 3,
                f"{case_id}.results[{expected_rank - 1}] grade must be an integer 0..3",
            )
        raw_violation = judgment.get("critical_business_violation")
        _require(
            isinstance(raw_violation, bool),
            f"{case_id}.critical_business_violation must be a boolean",
        )
        if "notes" in judgment:
            _require(
                isinstance(judgment["notes"], str),
                f"{case_id}.notes must be a string",
            )


def evaluate_manual_judgments(
    results: list[dict[str, Any]],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    validate_manual_judgment_contract(
        (result["case_id"] for result in results),
        judgments,
    )
    top1_grades: list[int] = []
    ndcg_values: list[float] = []
    violation_count = 0
    for result in results:
        case_id = result["case_id"]
        judgment = judgments[case_id]
        expected_results = result.get("results") or []
        _require(
            isinstance(expected_results, list) and len(expected_results) >= 3,
            f"{case_id} benchmark result must contain at least three ranked passages",
        )
        raw_results = judgment["results"]
        for expected_rank, (raw_result, expected_result) in enumerate(
            zip(raw_results, expected_results[:3], strict=True),
            start=1,
        ):
            raw_rank = raw_result.get("rank")
            _require(
                raw_rank == expected_rank
                and raw_rank == expected_result.get("rank"),
                f"{case_id}.results[{expected_rank - 1}] rank identity mismatch",
            )
            for field in ("chunk_id", "source_id", "content_hash"):
                actual_identity = raw_result.get(field)
                expected_identity = expected_result.get(field)
                _require(
                    isinstance(actual_identity, str)
                    and actual_identity == expected_identity,
                    (
                        f"{case_id}.results[{expected_rank - 1}] "
                        f"{field} identity mismatch"
                    ),
                )
        grades = [raw_result["grade"] for raw_result in raw_results]
        raw_violation = judgment["critical_business_violation"]
        top1_grades.append(grades[0])
        ndcg_values.append(_normalized_dcg(grades))
        violation_count += int(raw_violation)
    count = max(1, len(results))
    return {
        "top1_usable_rate": round(
            sum(grade >= 2 for grade in top1_grades) / count,
            4,
        ),
        "top1_direct_complete_rate": round(
            sum(grade == 3 for grade in top1_grades) / count,
            4,
        ),
        "top1_grade_zero_count": sum(grade == 0 for grade in top1_grades),
        "normalized_dcg_at_3": round(sum(ndcg_values) / count, 4),
        "critical_business_violation_count": violation_count,
    }


def _comparison_metric_value(
    *,
    metric_name: str,
    automatic_metrics: dict[str, Any] | None,
    manual_metrics: dict[str, Any] | None,
    report_label: str,
) -> float:
    metrics = (
        manual_metrics
        if metric_name in MANUAL_COMPARISON_METRICS
        else automatic_metrics
    )
    _require(
        isinstance(metrics, dict) and metric_name in metrics,
        f"{report_label} comparison metric is missing: {metric_name}",
    )
    raw_value = metrics[metric_name]
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise BenchmarkContractError(
            f"{report_label} comparison metric is not numeric: {metric_name}"
        ) from exc
    _require(
        math.isfinite(value),
        f"{report_label} comparison metric is not finite: {metric_name}",
    )
    return value


def validate_baseline_report_contract(
    *,
    fixture: dict[str, Any],
    baseline_report: dict[str, Any],
) -> None:
    """Reject an incompatible or incomplete baseline before live retrieval."""

    benchmark_id = str(fixture.get("benchmark_id") or "")
    schema_version = str(fixture.get("schema_version") or "")
    fixture_case_count = int(fixture.get("case_count") or 0)
    fixture_top_k = int(fixture.get("top_k") or 0)
    _require(
        str(baseline_report.get("benchmark_id") or "") == benchmark_id,
        "baseline benchmark_id does not match fixture",
    )
    _require(
        str(baseline_report.get("schema_version") or "") == schema_version,
        "baseline schema_version does not match fixture",
    )
    _require(
        baseline_report.get("case_count") == fixture_case_count,
        "baseline case_count does not match fixture",
    )
    _require(
        baseline_report.get("effective_top_k") == fixture_top_k,
        "baseline effective_top_k does not match fixture",
    )
    comparison_gates = (
        (fixture.get("release_gates") or {}).get("comparison") or {}
    )
    _require(
        isinstance(comparison_gates, dict) and bool(comparison_gates),
        "fixture has no comparison gates",
    )
    baseline_metrics = baseline_report.get("metrics")
    baseline_manual_metrics = baseline_report.get("manual_metrics")
    _require(
        isinstance(baseline_metrics, dict),
        "baseline report metrics must be an object",
    )
    for regression_name in comparison_gates:
        _require(
            regression_name.endswith("_regression"),
            f"comparison gate must end with _regression: {regression_name}",
        )
        _comparison_metric_value(
            metric_name=regression_name.removesuffix("_regression"),
            automatic_metrics=baseline_metrics,
            manual_metrics=(
                baseline_manual_metrics
                if isinstance(baseline_manual_metrics, dict)
                else None
            ),
            report_label="baseline",
        )


def evaluate_comparison(
    *,
    fixture: dict[str, Any],
    baseline_report: dict[str, Any],
    candidate_metrics: dict[str, Any],
    candidate_manual_metrics: dict[str, Any] | None,
    candidate_case_count: int,
    effective_top_k: int,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Validate a like-for-like baseline and evaluate configured regressions."""

    fixture_case_count = int(fixture.get("case_count") or 0)
    fixture_top_k = int(fixture.get("top_k") or 0)
    validate_baseline_report_contract(
        fixture=fixture,
        baseline_report=baseline_report,
    )
    _require(
        candidate_case_count == fixture_case_count,
        "candidate comparison requires the complete fixture case matrix",
    )
    _require(
        effective_top_k == fixture_top_k,
        "candidate comparison top_k does not match fixture",
    )

    comparison_gates = (
        (fixture.get("release_gates") or {}).get("comparison") or {}
    )
    baseline_metrics = baseline_report.get("metrics")
    baseline_manual_metrics = baseline_report.get("manual_metrics")

    regressions: dict[str, float] = {}
    for regression_name in comparison_gates:
        _require(
            regression_name.endswith("_regression"),
            f"comparison gate must end with _regression: {regression_name}",
        )
        metric_name = regression_name.removesuffix("_regression")
        baseline_value = _comparison_metric_value(
            metric_name=metric_name,
            automatic_metrics=baseline_metrics,
            manual_metrics=(
                baseline_manual_metrics
                if isinstance(baseline_manual_metrics, dict)
                else None
            ),
            report_label="baseline",
        )
        candidate_value = _comparison_metric_value(
            metric_name=metric_name,
            automatic_metrics=candidate_metrics,
            manual_metrics=candidate_manual_metrics,
            report_label="candidate",
        )
        regressions[regression_name] = round(
            baseline_value - candidate_value,
            4,
        )
    return regressions, evaluate_gates(regressions, comparison_gates)


async def run_benchmark(
    cases: list[dict[str, Any]],
    *,
    top_k: int,
    concurrency: int,
    retrieval_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from backend.services.retrieval_service import RetrievalService
    from backend.business_config.loader import get_config_loader
    from backend.config.settings import get_settings

    service = RetrievalService()
    if retrieval_overrides:
        query_config = deepcopy(service._query_config())
        query_config.setdefault("defaults", {}).update(retrieval_overrides)
        for task_id in EXPECTED_TASK_IDS:
            task_config = (query_config.get("queries") or {}).get(task_id)
            if isinstance(task_config, dict):
                task_config.update(retrieval_overrides)
        service._query_config = lambda: query_config
    company_value_terms = get_config_loader().company_value_terms()
    coach_rag_context_max_chars = get_settings().coach_rag_context_max_chars
    limiter = asyncio.Semaphore(max(1, int(concurrency)))

    async def run_case(case: dict[str, Any]) -> dict[str, Any]:
        async with limiter:
            started = time.perf_counter()
            chunks = await service.aretrieve(
                case["task_id"],
                production_retrieval_context(
                    case,
                    company_value_terms=company_value_terms,
                ),
                top_k=max(1, int(top_k)),
            )
            return evaluate_case(
                case,
                chunks,
                (time.perf_counter() - started) * 1000,
                coach_rag_context_max_chars=coach_rag_context_max_chars,
            )

    try:
        return list(await asyncio.gather(*(run_case(case) for case in cases)))
    finally:
        await service.shutdown()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the 48-case production-task retrieval release gate."
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--enforce-gates", action="store_true")
    parser.add_argument("--judgments", type=Path)
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--rerank-units-per-parent", type=int)
    parser.add_argument(
        "--semantic-group-candidate",
        choices=("on", "off"),
    )
    parser.add_argument("--rerank-rank-fusion-k", type=int)
    parser.add_argument("--rerank-rank-weight", type=float)
    parser.add_argument("--first-stage-rank-weight", type=float)
    return parser.parse_args(argv)


def retrieval_overrides_from_args(
    args: argparse.Namespace,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    scalar_options = {
        "rerank_units_per_parent": args.rerank_units_per_parent,
        "rerank_rank_fusion_k": args.rerank_rank_fusion_k,
        "rerank_rank_weight": args.rerank_rank_weight,
        "first_stage_rank_weight": args.first_stage_rank_weight,
    }
    overrides.update(
        {
            name: value
            for name, value in scalar_options.items()
            if value is not None
        }
    )
    if args.semantic_group_candidate is not None:
        overrides["rerank_semantic_group_candidate_enabled"] = (
            args.semantic_group_candidate == "on"
        )
    return overrides


def _release_prerequisite_reasons(
    *,
    validate_only: bool,
    complete_case_matrix: bool,
    effective_top_k: int,
    fixture_top_k: int,
    has_judgments: bool,
    comparison_required: bool,
    has_baseline_report: bool,
) -> list[str]:
    reasons: list[str] = []
    if validate_only:
        reasons.append("validate-only does not execute release gates")
    if not complete_case_matrix:
        reasons.append("release gates require the complete fixture case matrix")
    if effective_top_k != fixture_top_k:
        reasons.append(
            "release gates require effective_top_k to match fixture top_k"
        )
    if not has_judgments:
        reasons.append("release gates require identity-bound manual judgments")
    if comparison_required and not has_baseline_report:
        reasons.append(
            "fixture comparison gates require --baseline-report"
        )
    return reasons


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fixture = load_fixture(args.fixture)
    all_cases = validate_and_expand_fixture(fixture)
    cases = list(all_cases)
    selected = set(args.case_ids or [])
    if selected:
        known = {case["case_id"] for case in cases}
        _require(selected <= known, f"unknown cases: {sorted(selected - known)}")
        cases = [case for case in cases if case["case_id"] in selected]

    fixture_top_k = int(fixture.get("top_k") or 0)
    _require(fixture_top_k > 0, "fixture top_k must be positive")
    effective_top_k = (
        int(args.top_k) if args.top_k is not None else fixture_top_k
    )
    _require(effective_top_k > 0, "effective top_k must be positive")
    complete_case_matrix = (
        len(cases) == len(all_cases)
        and {case["case_id"] for case in cases}
        == {case["case_id"] for case in all_cases}
    )
    comparison_config = (
        (fixture.get("release_gates") or {}).get("comparison") or {}
    )
    _require(
        isinstance(comparison_config, dict),
        "fixture comparison gates must be an object",
    )
    prerequisite_reasons = _release_prerequisite_reasons(
        validate_only=bool(args.validate_only),
        complete_case_matrix=complete_case_matrix,
        effective_top_k=effective_top_k,
        fixture_top_k=fixture_top_k,
        has_judgments=bool(args.judgments),
        comparison_required=bool(comparison_config),
        has_baseline_report=bool(args.baseline_report),
    )
    if args.enforce_gates:
        _require(
            not prerequisite_reasons,
            "release gate prerequisites failed: "
            + "; ".join(prerequisite_reasons),
        )

    if args.validate_only:
        report: dict[str, Any] = {
            "benchmark_id": fixture["benchmark_id"],
            "schema_version": fixture.get("schema_version"),
            "mode": "validate-only",
            "scenario_count": len(fixture["scenarios"]),
            "task_count": len(fixture["tasks"]),
            "case_count": len(cases),
            "fixture_case_count": len(all_cases),
            "effective_top_k": effective_top_k,
            "fixture_top_k": fixture_top_k,
            "complete_case_matrix": complete_case_matrix,
            "release_prerequisites_met": False,
            "release_eligible": False,
            "release_ineligibility_reasons": prerequisite_reasons,
            "valid": True,
        }
        exit_code = 0
    else:
        preloaded_judgments = None
        if args.judgments:
            preloaded_judgments = load_manual_judgments(args.judgments)
            validate_manual_judgment_contract(
                (case["case_id"] for case in cases),
                preloaded_judgments,
            )
        preloaded_baseline_report = None
        if args.baseline_report:
            preloaded_baseline_report = load_baseline_report(
                args.baseline_report
            )
            validate_baseline_report_contract(
                fixture=fixture,
                baseline_report=preloaded_baseline_report,
            )
            _require(
                complete_case_matrix,
                "candidate comparison requires the complete fixture case matrix",
            )
            _require(
                effective_top_k == fixture_top_k,
                "candidate comparison top_k does not match fixture",
            )

        from backend.rag.reranker import Reranker
        from backend.config.settings import get_settings

        retrieval_overrides = retrieval_overrides_from_args(args)
        coach_rag_context_max_chars = get_settings().coach_rag_context_max_chars
        results = asyncio.run(
            run_benchmark(
                cases,
                top_k=effective_top_k,
                concurrency=args.concurrency,
                retrieval_overrides=retrieval_overrides,
            )
        )
        metrics = summarize(results)
        gate_results = evaluate_gates(
            metrics,
            (fixture.get("release_gates") or {}).get("automatic") or {},
        )
        manual_metrics = None
        manual_gate_results: list[dict[str, Any]] = []
        if preloaded_judgments is not None:
            manual_metrics = evaluate_manual_judgments(
                results,
                preloaded_judgments,
            )
            manual_gate_results = evaluate_gates(
                manual_metrics,
                (fixture.get("release_gates") or {}).get("manual") or {},
            )
        manual_gates_passed = (
            all(gate["passed"] for gate in manual_gate_results)
            if preloaded_judgments is not None
            else None
        )
        comparison_metrics = None
        comparison_gate_results: list[dict[str, Any]] = []
        comparison_gates_passed = None
        baseline_summary = None
        if preloaded_baseline_report is not None:
            baseline_report = preloaded_baseline_report
            comparison_metrics, comparison_gate_results = evaluate_comparison(
                fixture=fixture,
                baseline_report=baseline_report,
                candidate_metrics=metrics,
                candidate_manual_metrics=manual_metrics,
                candidate_case_count=len(results),
                effective_top_k=effective_top_k,
            )
            comparison_gates_passed = all(
                gate["passed"] for gate in comparison_gate_results
            )
            baseline_summary = {
                "label": baseline_report.get("label"),
                "benchmark_id": baseline_report.get("benchmark_id"),
                "schema_version": baseline_report.get("schema_version"),
                "case_count": baseline_report.get("case_count"),
                "effective_top_k": baseline_report.get("effective_top_k"),
            }
        automatic_gates_passed = all(
            gate["passed"] for gate in gate_results
        )
        release_ineligibility_reasons = list(prerequisite_reasons)
        if not automatic_gates_passed:
            release_ineligibility_reasons.append("automatic gates failed")
        if manual_gates_passed is False:
            release_ineligibility_reasons.append("manual gates failed")
        if comparison_gates_passed is False:
            release_ineligibility_reasons.append("comparison gates failed")
        release_prerequisites_met = not prerequisite_reasons
        release_eligible = bool(
            release_prerequisites_met
            and automatic_gates_passed
            and manual_gates_passed is True
            and (
                not comparison_config
                or comparison_gates_passed is True
            )
        )
        report = {
            "benchmark_id": fixture["benchmark_id"],
            "schema_version": fixture.get("schema_version"),
            "label": args.label,
            "retrieval_policy": Reranker.DOCUMENT_POLICY_VERSION,
            "retrieval_overrides": retrieval_overrides,
            "coach_rag_context_max_chars": coach_rag_context_max_chars,
            "case_count": len(results),
            "fixture_case_count": len(all_cases),
            "effective_top_k": effective_top_k,
            "fixture_top_k": fixture_top_k,
            "complete_case_matrix": complete_case_matrix,
            "metrics": metrics,
            "automatic_gates": gate_results,
            "automatic_gates_passed": automatic_gates_passed,
            "manual_metrics": manual_metrics,
            "manual_gates": manual_gate_results,
            "manual_gates_passed": manual_gates_passed,
            "manual_review_required": not bool(args.judgments),
            "baseline": baseline_summary,
            "comparison_metrics": comparison_metrics,
            "comparison_gates": comparison_gate_results,
            "comparison_gates_passed": comparison_gates_passed,
            "release_prerequisites_met": release_prerequisites_met,
            "release_eligible": release_eligible,
            "release_ineligibility_reasons": release_ineligibility_reasons,
            "evaluation_boundaries": fixture.get("evaluation_boundaries") or {},
            "cases": results,
        }
        exit_code = int(
            args.enforce_gates
            and not release_eligible
        )

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if not args.quiet:
        print(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

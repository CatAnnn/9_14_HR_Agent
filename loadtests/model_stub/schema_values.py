from __future__ import annotations

import copy
from typing import Any


_PERSONALITY_FACETS = {
    "openness": (
        "fantasy",
        "aesthetics",
        "feelings",
        "actions",
        "ideas",
        "values",
    ),
    "conscientiousness": (
        "competence",
        "order",
        "dutifulness",
        "achievement_striving",
        "self_discipline",
        "deliberation",
    ),
    "extraversion": (
        "warmth",
        "gregariousness",
        "assertiveness",
        "activity",
        "excitement_seeking",
        "positive_emotions",
    ),
    "agreeableness": (
        "trust",
        "straightforwardness",
        "altruism",
        "compliance",
        "modesty",
        "tender_mindedness",
    ),
    "neuroticism": (
        "anxiety",
        "angry_hostility",
        "depression",
        "self_consciousness",
        "impulsiveness",
        "vulnerability",
    ),
}


def _known_schema_value(schema_name: str) -> dict[str, Any] | None:
    if schema_name == "IntentPerformanceDraftOutput":
        return {
            "current_performances": [
                (
                    "- 两项重点目标整体按计划推进。\n"
                    "- 关键里程碑和质量要求基本达成。"
                ),
                (
                    "- 能够主动协调跨职能资源。\n"
                    "- 已将项目经验沉淀为可复用方法。"
                ),
                (
                    "- 复杂事项授权仍需形成更稳定的结果。\n"
                    "- 更广范围的协作影响需要继续加强。"
                ),
            ],
            "generation_reasons": [
                "- 依据输入中的贡献目标、绩效等级、岗位和意图进行有限推演。",
                "- 结合已提供的项目事实与正向反馈，不补造未给出的业务数据。",
                "- 对照岗位要求识别待核实差距，未将推演内容作为正式评价。",
            ],
        }
    if schema_name.startswith("Guidance") and schema_name.endswith("Output"):
        return {
            "points": [
                {
                    "title": "用具体事实建立共同判断",
                    "summary": "先确认关键目标和已知结果，再邀请员工补充其视角。",
                    "details": [
                        "清楚说明本次沟通目的，并按目标、事实和影响组织信息。",
                        "使用开放问题核对员工理解，避免在事实未对齐时直接下结论。",
                    ],
                    "citation_targets": [],
                    "citation_source_refs": [],
                    "citation_highlight_texts": [],
                    "citation_source_quotes": [],
                }
            ]
        }
    if schema_name == "CoachDimensionModelOutput":
        return {
            "status": "insufficient_information",
            "score": None,
            "summary": "当前对话信息不足以形成可靠评分。",
            "basis": "需要更多经理与员工的完整交流证据。",
            "summary_knowledge_chunk_ids": [],
            "basis_knowledge_chunk_ids": [],
            "summary_citation_refs": [],
            "basis_citation_refs": [],
            "issues": [],
        }
    if schema_name == "MotivationScoringStructuredOutput":
        return {
            "primary_score_delta": 1.0,
            "secondary_score_deltas": {"security": 0.5, "affiliation": 0.5},
            "reason_summary": "经理提供了清晰信息并邀请员工表达。",
        }
    if schema_name == "EmotionTransitionStructuredOutput":
        return {
            "vad_delta": {"valence": 0.05, "arousal": 0.02, "dominance": 0.03},
            "transition_strategy": "expected_value",
            "reason_summary": "本轮沟通温和且具有一定支持性。",
        }
    if schema_name == "RehearsalDimensionEvaluation":
        return {"covered_dimensions": ["fact", "motivation", "emotion", "plan"]}
    if schema_name == "PersonalityFacetGenerationOutput":
        return {
            dimension: {
                "score": 50,
                "facets": {facet: 50 for facet in facets},
            }
            for dimension, facets in _PERSONALITY_FACETS.items()
        }
    return None


def _resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        return {}
    current: Any = root
    for part in ref[2:].split("/"):
        current = current.get(part.replace("~1", "/").replace("~0", "~"), {})
    return current if isinstance(current, dict) else {}


def value_for_schema(schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    known = _known_schema_value(schema_name)
    if known is not None:
        return known
    generated = _value_for_node(schema, schema)
    return generated if isinstance(generated, dict) else {"result": generated}


def _value_for_node(node: dict[str, Any], root: dict[str, Any]) -> Any:
    if "const" in node:
        return copy.deepcopy(node["const"])
    if "default" in node:
        return copy.deepcopy(node["default"])
    if "enum" in node and node["enum"]:
        return copy.deepcopy(node["enum"][0])
    if "$ref" in node:
        return _value_for_node(_resolve_ref(root, str(node["$ref"])), root)
    for combinator in ("anyOf", "oneOf", "allOf"):
        options = node.get(combinator)
        if isinstance(options, list) and options:
            non_null = [item for item in options if item.get("type") != "null"]
            return _value_for_node(non_null[0] if non_null else options[0], root)
    node_type = node.get("type")
    if node_type == "object" or "properties" in node:
        properties = node.get("properties") or {}
        required = set(node.get("required") or properties.keys())
        return {
            key: _value_for_node(value, root)
            for key, value in properties.items()
            if key in required or "default" in value
        }
    if node_type == "array":
        count = max(1, int(node.get("minItems", 1)))
        return [_value_for_node(node.get("items") or {}, root) for _ in range(count)]
    if node_type == "integer":
        return int(node.get("minimum", 1))
    if node_type == "number":
        return float(node.get("minimum", 0.0))
    if node_type == "boolean":
        return True
    if node_type == "null":
        return None
    return "压测结构化响应。"

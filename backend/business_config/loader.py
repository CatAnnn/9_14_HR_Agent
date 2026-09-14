from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from backend.config.settings import get_settings
from backend.schemas.intent import IntentConfig
from backend.schemas.personality_behavior import PersonalityBehaviorMap
from backend.schemas.simulation import (
    BigFivePersonality,
    EmotionAnchor,
    EmotionTransitionModelConfig,
    MotiveOption,
)


COACH_DIMENSION_CONFIG_FILES = (
    "start.yaml",
    "emotion.yaml",
    "requirement.yaml",
    "plan.yaml",
)

GUIDANCE_PROMPT_FILES = (
    "guidance/guidance.jinja2",
    "guidance/start.jinja2",
    "guidance/emotion.jinja2",
    "guidance/requirement.jinja2",
    "guidance/plan.jinja2",
)
COACH_PROMPT_FILES = (
    "coach/_dimension_evaluation.jinja2",
    "coach/_knowledge_skills.jinja2",
    "coach/start.jinja2",
    "coach/emotion.jinja2",
    "coach/requirement.jinja2",
    "coach/plan.jinja2",
)
GUIDANCE_QUERY_NAMES = (
    "guidance_start",
    "guidance_emotion",
    "guidance_requirement",
    "guidance_plan",
)
COACH_QUERY_NAMES = (
    "opening_evaluation",
    "emotion_evaluation",
    "output_expectations_evaluation",
    "development_plan_evaluation",
)

EMOTION_EXPRESSION_PROFILE_FIELDS = (
    "stance_cue",
    "rhythm_cue",
    "agency_cue",
    "avoid",
)
EMOTION_EXPRESSION_POLICY_FIELDS = (
    "role",
    "invariants",
    "field_semantics",
)
EMOTION_ANCHOR_SELECTION_POLICY_FIELDS = (
    "version",
    "mode",
    "missing_tags_policy",
    "unknown_tags_policy",
    "preserve_current_anchor",
    "max_output_tags",
    "direct_vad_anchor_ids",
    "appraisal_tags",
    "contextual_anchor_gates",
)


class _StrictSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        key_lines: dict[Any, int] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                hash(key)
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from exc
            if key in mapping:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}; first defined at line {key_lines[key]}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
            key_lines[key] = key_node.start_mark.line + 1
        return mapping


class BusinessConfigLoader:
    def __init__(
        self,
        config_dir: Path | None = None,
        *,
        prompt_dir: Path | None = None,
        auto_reload: bool | None = None,
    ):
        settings = get_settings()
        self.config_dir = config_dir or settings.business_config_dir
        self.prompt_dir = prompt_dir or settings.prompt_dir
        self.prompt_auto_reload_enabled = (
            settings.prompt_auto_reload_enabled
            if auto_reload is None
            else auto_reload
        )

    @lru_cache(maxsize=64)
    def _load_yaml_cached(self, relative_path: str) -> dict[str, Any]:
        path = self.config_dir / relative_path
        if not path.exists():
            raise FileNotFoundError(f"Missing business config: {path}")
        with path.open("r", encoding="utf-8") as stream:
            data = yaml.load(stream, Loader=_StrictSafeLoader) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Business config must be a mapping: {path}")
        return data

    def load_yaml(self, relative_path: str) -> dict[str, Any]:
        # Config objects are shared across requests, but callers may normalize or
        # enrich their copy. Keep the cached source immutable from their perspective.
        return deepcopy(self._load_yaml_cached(relative_path))

    def clear_cache(self) -> None:
        self._load_yaml_cached.cache_clear()
        self._query_config_snapshot_cached.cache_clear()
        self._company_values_cached.cache_clear()
        self._personality_behavior_map_cached.cache_clear()
        self._personality_behavior_map_hash_cached.cache_clear()
        self._coach_version_cached.cache_clear()
        self._guidance_version_cached.cache_clear()

    def intents(self) -> dict[str, IntentConfig]:
        data = self.load_yaml("intents.yaml")
        return {item["id"]: IntentConfig(**item) for item in data.get("supported_intents", [])}

    def default_intent_id(self) -> str:
        data = self.load_yaml("intents.yaml")
        return data.get("default_intent", "improvement")

    def motives(self) -> dict[str, MotiveOption]:
        data = self.load_yaml("motives.yaml")
        return {item["id"]: MotiveOption(**item) for item in data.get("motives", [])}

    def motive_recommendation(self, intent_id: str | None) -> dict[str, Any]:
        data = self.load_yaml("motives.yaml")
        recommendations = data.get("intent_recommendations", {})
        default = data.get("default_recommendation", {})
        return recommendations.get(intent_id or "", default) or default

    def emotion_anchors(self) -> dict[str, EmotionAnchor]:
        data = self.load_yaml("emotion_space.yaml")
        raw_anchors = data.get("anchors") or []
        if not isinstance(raw_anchors, list) or not raw_anchors:
            raise ValueError("emotion_space.yaml anchors must be a non-empty list")
        anchors: dict[str, EmotionAnchor] = {}
        for index, raw_anchor in enumerate(raw_anchors):
            if not isinstance(raw_anchor, dict):
                raise ValueError(f"emotion_space.yaml anchors[{index}] must be a mapping")
            anchor = EmotionAnchor.model_validate(raw_anchor)
            if not anchor.id.strip():
                raise ValueError(f"emotion_space.yaml anchors[{index}].id cannot be empty")
            if anchor.id in anchors:
                raise ValueError(f"Duplicate emotion anchor id: {anchor.id}")
            anchors[anchor.id] = anchor
        return anchors

    def emotion_expression_profiles(self) -> dict[str, dict[str, str]]:
        data = self.load_yaml("emotion_space.yaml")
        raw_profiles = data.get("expression_profiles")
        if not isinstance(raw_profiles, dict):
            raise ValueError("emotion_space.yaml expression_profiles must be a mapping")

        required_fields = set(EMOTION_EXPRESSION_PROFILE_FIELDS)
        profiles: dict[str, dict[str, str]] = {}
        for raw_anchor_id, raw_profile in raw_profiles.items():
            if not isinstance(raw_anchor_id, str):
                raise ValueError("Emotion expression profile ids must be strings")
            anchor_id = raw_anchor_id.strip()
            if not anchor_id or anchor_id in profiles:
                raise ValueError(f"Invalid emotion expression profile id: {anchor_id}")
            if not isinstance(raw_profile, dict):
                raise ValueError(
                    f"emotion_space.yaml expression_profiles.{anchor_id} must be a mapping"
                )
            profile_fields = set(raw_profile)
            if profile_fields != required_fields:
                missing = sorted(required_fields - profile_fields)
                unexpected = sorted(profile_fields - required_fields)
                raise ValueError(
                    f"Invalid emotion expression profile fields for {anchor_id}: "
                    f"missing={missing}, unexpected={unexpected}"
                )
            if any(
                not isinstance(raw_profile[field], str)
                or not raw_profile[field].strip()
                for field in EMOTION_EXPRESSION_PROFILE_FIELDS
            ):
                raise ValueError(
                    "Emotion expression profile fields must be non-empty strings: "
                    f"{anchor_id}"
                )
            normalized = {
                field: raw_profile[field].strip()
                for field in EMOTION_EXPRESSION_PROFILE_FIELDS
            }
            profiles[anchor_id] = normalized

        anchors = self.emotion_anchors()
        if set(profiles) != set(anchors):
            missing = sorted(set(anchors) - set(profiles))
            unexpected = sorted(set(profiles) - set(anchors))
            raise ValueError(
                "Emotion expression profiles must match emotion anchors exactly: "
                f"missing={missing}, unexpected={unexpected}"
            )
        return {anchor_id: profiles[anchor_id] for anchor_id in anchors}

    def emotion_expression_policy(self) -> dict[str, Any]:
        data = self.load_yaml("emotion_space.yaml")
        raw_policy = data.get("expression_policy")
        if not isinstance(raw_policy, dict):
            raise ValueError("emotion_space.yaml expression_policy must be a mapping")

        required_fields = set(EMOTION_EXPRESSION_POLICY_FIELDS)
        policy_fields = set(raw_policy)
        if policy_fields != required_fields:
            missing = sorted(required_fields - policy_fields)
            unexpected = sorted(policy_fields - required_fields)
            raise ValueError(
                "Invalid emotion expression policy fields: "
                f"missing={missing}, unexpected={unexpected}"
            )

        raw_role = raw_policy.get("role")
        raw_invariants = raw_policy.get("invariants")
        raw_field_semantics = raw_policy.get("field_semantics")
        if not isinstance(raw_role, str) or not raw_role.strip():
            raise ValueError("Emotion expression policy role cannot be empty")
        role = raw_role.strip()
        if not isinstance(raw_invariants, list) or not raw_invariants:
            raise ValueError("Emotion expression policy invariants must be a non-empty list")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in raw_invariants
        ):
            raise ValueError(
                "Emotion expression policy invariants must be non-empty strings"
            )
        invariants = [item.strip() for item in raw_invariants]
        if not isinstance(raw_field_semantics, dict):
            raise ValueError("Emotion expression policy field_semantics must be a mapping")

        required_semantics = set(EMOTION_EXPRESSION_PROFILE_FIELDS)
        semantic_fields = set(raw_field_semantics)
        if semantic_fields != required_semantics:
            missing = sorted(required_semantics - semantic_fields)
            unexpected = sorted(semantic_fields - required_semantics)
            raise ValueError(
                "Invalid emotion expression policy field_semantics: "
                f"missing={missing}, unexpected={unexpected}"
            )
        if any(
            not isinstance(raw_field_semantics[field], str)
            or not raw_field_semantics[field].strip()
            for field in EMOTION_EXPRESSION_PROFILE_FIELDS
        ):
            raise ValueError(
                "Emotion expression policy field_semantics must be non-empty strings"
            )
        field_semantics = {
            field: raw_field_semantics[field].strip()
            for field in EMOTION_EXPRESSION_PROFILE_FIELDS
        }

        return {
            "role": role,
            "invariants": invariants,
            "field_semantics": field_semantics,
        }

    def emotion_anchor_selection_policy(self) -> dict[str, Any]:
        data = self.load_yaml("emotion_space.yaml")
        raw_policy = data.get("anchor_selection_policy")
        if not isinstance(raw_policy, dict):
            raise ValueError("emotion_space.yaml anchor_selection_policy must be a mapping")

        required_fields = set(EMOTION_ANCHOR_SELECTION_POLICY_FIELDS)
        policy_fields = set(raw_policy)
        if policy_fields != required_fields:
            missing = sorted(required_fields - policy_fields)
            unexpected = sorted(policy_fields - required_fields)
            raise ValueError(
                "Invalid emotion anchor selection policy fields: "
                f"missing={missing}, unexpected={unexpected}"
            )

        version = str(raw_policy.get("version") or "").strip()
        mode = str(raw_policy.get("mode") or "").strip()
        missing_tags_policy = str(raw_policy.get("missing_tags_policy") or "").strip()
        unknown_tags_policy = str(raw_policy.get("unknown_tags_policy") or "").strip()
        preserve_current_anchor = raw_policy.get("preserve_current_anchor")
        max_output_tags = raw_policy.get("max_output_tags")
        if version != "v1":
            raise ValueError("Emotion anchor selection policy version must be v1")
        if mode not in {"observe", "enforce"}:
            raise ValueError("Emotion anchor selection policy mode must be observe or enforce")
        if missing_tags_policy != "direct_only":
            raise ValueError("Emotion anchor missing_tags_policy must be direct_only")
        if unknown_tags_policy != "ignore":
            raise ValueError("Emotion anchor unknown_tags_policy must be ignore")
        if not isinstance(preserve_current_anchor, bool):
            raise ValueError("Emotion anchor preserve_current_anchor must be boolean")
        if isinstance(max_output_tags, bool) or not isinstance(max_output_tags, int):
            raise ValueError("Emotion anchor max_output_tags must be an integer")
        if not 1 <= max_output_tags <= 8:
            raise ValueError("Emotion anchor max_output_tags must be within [1, 8]")

        anchors = self.emotion_anchors()
        anchor_ids = set(anchors)
        raw_direct_ids = raw_policy.get("direct_vad_anchor_ids")
        if not isinstance(raw_direct_ids, list) or not raw_direct_ids:
            raise ValueError("Emotion direct_vad_anchor_ids must be a non-empty list")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in raw_direct_ids
        ):
            raise ValueError(
                "Emotion direct_vad_anchor_ids must contain non-empty strings"
            )
        direct_ids = [item.strip() for item in raw_direct_ids]
        if len(set(direct_ids)) != len(direct_ids):
            raise ValueError("Emotion direct_vad_anchor_ids must be unique and non-empty")

        raw_tags = raw_policy.get("appraisal_tags")
        if not isinstance(raw_tags, dict) or not raw_tags:
            raise ValueError("Emotion appraisal_tags must be a non-empty mapping")
        appraisal_tags: dict[str, str] = {}
        for raw_tag_id, raw_description in raw_tags.items():
            if not isinstance(raw_tag_id, str) or not isinstance(
                raw_description,
                str,
            ):
                raise ValueError(
                    "Emotion appraisal tag ids and descriptions must be strings"
                )
            tag_id = raw_tag_id.strip()
            description = raw_description.strip()
            if not tag_id or tag_id in appraisal_tags or not description:
                raise ValueError("Emotion appraisal tags must be unique and non-empty")
            appraisal_tags[tag_id] = description

        raw_gates = raw_policy.get("contextual_anchor_gates")
        if not isinstance(raw_gates, dict) or not raw_gates:
            raise ValueError("Emotion contextual_anchor_gates must be a non-empty mapping")
        contextual_gates: dict[str, dict[str, list[str]]] = {}
        for raw_anchor_id, raw_gate in raw_gates.items():
            if not isinstance(raw_anchor_id, str):
                raise ValueError("Emotion contextual gate ids must be strings")
            anchor_id = raw_anchor_id.strip()
            if not anchor_id or anchor_id in contextual_gates:
                raise ValueError("Emotion contextual gate ids must be unique and non-empty")
            if not isinstance(raw_gate, dict):
                raise ValueError(f"Emotion contextual gate for {anchor_id} must be a mapping")
            unexpected = set(raw_gate) - {"requires_any", "requires_all"}
            if unexpected:
                raise ValueError(
                    f"Unexpected emotion contextual gate fields for {anchor_id}: "
                    f"{sorted(unexpected)}"
                )

            normalized_gate: dict[str, list[str]] = {}
            for gate_field in ("requires_any", "requires_all"):
                raw_values = raw_gate.get(gate_field, [])
                if not isinstance(raw_values, list):
                    raise ValueError(
                        f"Emotion contextual gate {anchor_id}.{gate_field} must be a list"
                    )
                if any(
                    not isinstance(item, str) or not item.strip()
                    for item in raw_values
                ):
                    raise ValueError(
                        f"Emotion contextual gate {anchor_id}.{gate_field} "
                        "must contain non-empty strings"
                    )
                values = [item.strip() for item in raw_values]
                if len(set(values)) != len(values):
                    raise ValueError(
                        f"Emotion contextual gate {anchor_id}.{gate_field} "
                        "must contain unique non-empty tags"
                    )
                unknown_tags = sorted(set(values) - set(appraisal_tags))
                if unknown_tags:
                    raise ValueError(
                        f"Emotion contextual gate {anchor_id}.{gate_field} "
                        f"contains unknown tags: {unknown_tags}"
                    )
                normalized_gate[gate_field] = values
            if not normalized_gate["requires_any"] and not normalized_gate["requires_all"]:
                raise ValueError(f"Emotion contextual gate {anchor_id} cannot be empty")
            required_any = set(normalized_gate["requires_any"])
            required_all = set(normalized_gate["requires_all"])
            minimum_output_tags = len(required_all) + int(
                bool(required_any) and not bool(required_any & required_all)
            )
            if minimum_output_tags > max_output_tags:
                raise ValueError(
                    f"Emotion contextual gate {anchor_id} requires more tags than "
                    "max_output_tags permits"
                )
            contextual_gates[anchor_id] = normalized_gate

        direct_id_set = set(direct_ids)
        contextual_id_set = set(contextual_gates)
        unknown_anchors = sorted((direct_id_set | contextual_id_set) - anchor_ids)
        overlap = sorted(direct_id_set & contextual_id_set)
        missing_anchors = sorted(anchor_ids - (direct_id_set | contextual_id_set))
        if unknown_anchors:
            raise ValueError(f"Emotion anchor selection contains unknown anchors: {unknown_anchors}")
        if overlap:
            raise ValueError(f"Emotion direct and contextual anchors overlap: {overlap}")
        if missing_anchors:
            raise ValueError(f"Emotion anchor selection does not cover anchors: {missing_anchors}")
        default_anchor_id = str(data.get("default_anchor") or "neutral").strip()
        if default_anchor_id not in direct_id_set:
            raise ValueError("Emotion default_anchor must be a direct VAD anchor")
        raw_initial_anchors = data.get("initial_anchor_by_intent") or {}
        if not isinstance(raw_initial_anchors, dict):
            raise ValueError("Emotion initial_anchor_by_intent must be a mapping")
        for raw_intent_id, raw_anchor_id in raw_initial_anchors.items():
            if not isinstance(raw_intent_id, str) or not isinstance(
                raw_anchor_id,
                str,
            ):
                raise ValueError(
                    "Emotion initial intent and anchor ids must be strings"
                )
            if raw_anchor_id.strip() not in direct_id_set:
                raise ValueError(
                    "Emotion initial anchors must be direct VAD anchors: "
                    f"{raw_intent_id} -> {raw_anchor_id}"
                )

        return {
            "version": version,
            "mode": mode,
            "missing_tags_policy": missing_tags_policy,
            "unknown_tags_policy": unknown_tags_policy,
            "preserve_current_anchor": preserve_current_anchor,
            "max_output_tags": max_output_tags,
            "direct_vad_anchor_ids": direct_ids,
            "appraisal_tags": appraisal_tags,
            "contextual_anchor_gates": contextual_gates,
        }

    def default_big_five(self) -> BigFivePersonality:
        data = self.load_yaml("emotion_space.yaml")
        return BigFivePersonality(**(data.get("default_big_five") or {}))

    def personality_vad_weights(self) -> dict[str, Any]:
        data = self.load_yaml("emotion_space.yaml")
        weights = data.get("personality_vad_weights") or {}
        return weights if isinstance(weights, dict) else {}

    def personality_transition_vad_weights(self) -> dict[str, Any]:
        return self.personality_vad_weights()

    def personality_initial_vad_weights(self) -> dict[str, Any]:
        return self.personality_vad_weights()

    @lru_cache(maxsize=1)
    def _personality_behavior_map_cached(self) -> PersonalityBehaviorMap:
        return PersonalityBehaviorMap.model_validate(
            self.load_yaml("personality_behavior_map.yaml")
        )

    def personality_behavior_map(self) -> PersonalityBehaviorMap:
        return self._personality_behavior_map_cached()

    @lru_cache(maxsize=1)
    def _personality_behavior_map_hash_cached(self) -> str:
        canonical = json.dumps(
            self.personality_behavior_map().model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def personality_behavior_map_hash(self) -> str:
        return self._personality_behavior_map_hash_cached()

    def emotion_transition_model_config(self) -> EmotionTransitionModelConfig:
        data = self.load_yaml("emotion_space.yaml")
        return EmotionTransitionModelConfig.model_validate(data.get("transition_model") or {})

    def emotion_anchor_aliases(self) -> dict[str, str]:
        data = self.load_yaml("emotion_space.yaml")
        raw_aliases = data.get("legacy_anchor_aliases") or {}
        if not isinstance(raw_aliases, dict):
            raise ValueError("emotion_space.yaml legacy_anchor_aliases must be a mapping")
        anchors = self.emotion_anchors()
        aliases: dict[str, str] = {}
        for raw_source, raw_target in raw_aliases.items():
            source = str(raw_source).strip()
            target = str(raw_target).strip()
            if not source or target not in anchors:
                raise ValueError(f"Invalid legacy emotion anchor mapping: {source} -> {target}")
            aliases[source] = target
        return aliases

    def resolve_emotion_anchor_id(self, anchor_id: str | None) -> str | None:
        if anchor_id is None:
            return None
        anchors = self.emotion_anchors()
        if anchor_id in anchors:
            return anchor_id
        return self.emotion_anchor_aliases().get(anchor_id, anchor_id)

    def default_emotion_anchor_id(self, intent_id: str | None = None) -> str | None:
        data = self.load_yaml("emotion_space.yaml")
        by_intent = data.get("initial_anchor_by_intent", {})
        anchor_id = by_intent.get(intent_id or "") or data.get("default_anchor")
        anchors = self.emotion_anchors()
        if anchor_id not in anchors:
            raise ValueError(f"Unknown default emotion anchor: {anchor_id}")
        return anchor_id

    def query_config(self) -> dict[str, Any]:
        return self.load_yaml("query.yaml")

    @lru_cache(maxsize=1)
    def _query_config_snapshot_cached(self) -> dict[str, Any]:
        return self._load_yaml_cached("query.yaml")

    def query_config_snapshot(self) -> dict[str, Any]:
        """Return the process-owned read-only-by-contract retrieval configuration."""

        return self._query_config_snapshot_cached()

    @lru_cache(maxsize=1)
    def _company_values_cached(self) -> dict[str, Any]:
        data = self.load_yaml("company_values.yaml")
        version = str(data.get("version") or "").strip()
        if not version:
            raise ValueError("company_values.yaml requires a non-empty version")

        raw_culture_name = data.get("culture_name") or {}
        if not isinstance(raw_culture_name, dict):
            raise ValueError("company_values.yaml culture_name must be a mapping")
        culture_name = {
            str(language).strip(): str(label).strip()
            for language, label in raw_culture_name.items()
            if str(language).strip() and str(label).strip()
        }

        raw_values = data.get("values") or []
        if not isinstance(raw_values, list):
            raise ValueError("company_values.yaml values must be a list")

        values: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, raw_value in enumerate(raw_values):
            if not isinstance(raw_value, dict):
                raise ValueError(f"company_values.yaml values[{index}] must be a mapping")
            value_id = str(raw_value.get("id") or "").strip()
            name = str(raw_value.get("name") or "").strip()
            definition = str(raw_value.get("definition") or "").strip()
            if not value_id or not name or not definition:
                raise ValueError(
                    f"company_values.yaml values[{index}] requires id, name, and definition"
                )
            if value_id in seen_ids:
                raise ValueError(f"Duplicate company value id: {value_id}")
            seen_ids.add(value_id)

            normalized = dict(raw_value)
            normalized.update(
                {
                    "id": value_id,
                    "name": name,
                    "name_zh": str(raw_value.get("name_zh") or "").strip(),
                    "definition": definition,
                }
            )
            for field in (
                "desired_behaviors",
                "desired_behaviors_zh",
                "anti_patterns",
                "manager_applications",
                "source_refs",
            ):
                raw_items = normalized.get(field) or []
                if not isinstance(raw_items, list):
                    raise ValueError(
                        f"company_values.yaml values[{index}].{field} must be a list"
                    )
                normalized[field] = [str(item).strip() for item in raw_items if str(item).strip()]
            values.append(normalized)

        enabled = bool(data.get("enabled", False))
        if enabled and not values:
            raise ValueError("company_values.yaml cannot be enabled without values")

        normalized_data = dict(data)
        normalized_data.update(
            {
                "version": version,
                "enabled": enabled,
                "culture_name": culture_name,
                "values": values,
            }
        )
        return normalized_data

    def company_values(self) -> dict[str, Any]:
        return deepcopy(self._company_values_cached())

    def company_values_enabled(self) -> bool:
        config = self._company_values_cached()
        return bool(config.get("enabled") and config.get("values"))

    def company_value_terms(self) -> str:
        config = self._company_values_cached()
        if not config.get("enabled"):
            return ""

        terms: list[str] = []
        culture_name = config.get("culture_name") or {}
        if isinstance(culture_name, dict):
            terms.extend(str(label).strip() for label in culture_name.values())
        for value in config.get("values", []):
            terms.extend(
                str(value.get(field) or "").strip()
                for field in ("name", "name_zh")
            )
        return " ".join(dict.fromkeys(term for term in terms if term))

    def culture_version(self) -> str | None:
        config = self._company_values_cached()
        return str(config["version"]) if config.get("enabled") and config.get("values") else None

    def coach_config(self, filename: str) -> dict[str, Any]:
        return self.load_yaml(f"coach/{filename}")

    def _prompt_bundle(self, relative_paths: tuple[str, ...]) -> dict[str, str]:
        bundle: dict[str, str] = {}
        for relative_path in relative_paths:
            path = self.prompt_dir / relative_path
            bundle[relative_path] = path.read_text(encoding="utf-8")
        return bundle

    def _prompt_revision(
        self,
        relative_paths: tuple[str, ...],
    ) -> tuple[tuple[str, int, int, int, int], ...]:
        """Return a bounded metadata key for prompt-version cache invalidation."""

        if not self.prompt_auto_reload_enabled:
            return ()
        revision: list[tuple[str, int, int, int, int]] = []
        for relative_path in relative_paths:
            stat = (self.prompt_dir / relative_path).stat()
            revision.append(
                (
                    relative_path,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
            )
        return tuple(revision)

    def _query_contract(self, names: tuple[str, ...]) -> dict[str, Any]:
        query_config = self.query_config()
        all_queries = (
            query_config.get("queries")
            or query_config.get("agent_queries")
            or {}
        )
        return {
            "version": query_config.get("version"),
            "defaults": query_config.get("defaults") or {},
            "queries": {name: all_queries.get(name) for name in names},
        }

    @lru_cache(maxsize=8)
    def _coach_version_cached(
        self,
        _prompt_revision: tuple[tuple[str, int, int, int, int], ...],
    ) -> str:
        from backend.rag.citation import CITATION_POLICY_VERSION
        from backend.schemas.task import CoachDimensionModelOutput

        payload = {
            "dimension_configs": {
                filename: self.coach_config(filename)
                for filename in COACH_DIMENSION_CONFIG_FILES
            },
            "prompts": self._prompt_bundle(COACH_PROMPT_FILES),
            "queries": self._query_contract(COACH_QUERY_NAMES),
            "output_schema": CoachDimensionModelOutput.model_json_schema(),
            "citation_policy": CITATION_POLICY_VERSION,
            "personality_behavior_map": self.personality_behavior_map_hash(),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"coach-{hashlib.sha256(canonical).hexdigest()[:16]}"

    def coach_version(self) -> str:
        return self._coach_version_cached(
            self._prompt_revision(COACH_PROMPT_FILES)
        )

    @lru_cache(maxsize=8)
    def _guidance_version_cached(
        self,
        _prompt_revision: tuple[tuple[str, int, int, int, int], ...],
    ) -> str:
        from pydantic import TypeAdapter

        from backend.rag.citation import CITATION_POLICY_VERSION
        from backend.schemas.guidance import (
            GUIDANCE_DETAIL_MAX_ITEMS,
            GUIDANCE_DETAIL_MIN_ITEMS,
            GUIDANCE_DIMENSION_POINT_CONTRACTS,
            GUIDANCE_GROUP_MAX_ITEMS,
            GUIDANCE_GROUP_MIN_ITEMS,
            GuidanceGeneratedPlanPointContent,
            GuidanceGeneratedPointGroup,
            GuidanceGeneratedPointGroupList,
            GuidanceGeneratedPointContent,
            GuidanceGeneratedPlanPointGroup,
            GuidancePlanPointGroup,
            GuidancePointGroup,
            GuidancePointGroupList,
            GuidanceReport,
        )

        payload = {
            "dimension_configs": {
                filename: self.coach_config(filename)
                for filename in COACH_DIMENSION_CONFIG_FILES
            },
            "prompts": self._prompt_bundle(GUIDANCE_PROMPT_FILES),
            "queries": self._query_contract(GUIDANCE_QUERY_NAMES),
            "citation_policy": CITATION_POLICY_VERSION,
            "personality_behavior_map": self.personality_behavior_map_hash(),
            "output_schema": {
                "dimension_point_contracts": GUIDANCE_DIMENSION_POINT_CONTRACTS,
                "generated_point_content": (
                    GuidanceGeneratedPointContent.model_json_schema()
                ),
                "generated_plan_point_content": (
                    GuidanceGeneratedPlanPointContent.model_json_schema()
                ),
                "generated_point_group": GuidanceGeneratedPointGroup.model_json_schema(),
                "generated_plan_point_group": GuidanceGeneratedPlanPointGroup.model_json_schema(),
                "generated_points": TypeAdapter(GuidanceGeneratedPointGroupList).json_schema(),
                "stored_point_group": GuidancePointGroup.model_json_schema(),
                "stored_plan_point_group": GuidancePlanPointGroup.model_json_schema(),
                "stored_points": TypeAdapter(GuidancePointGroupList).json_schema(),
                "report": GuidanceReport.model_json_schema(),
                "group_min_items": GUIDANCE_GROUP_MIN_ITEMS,
                "group_max_items": GUIDANCE_GROUP_MAX_ITEMS,
                "detail_min_items": GUIDANCE_DETAIL_MIN_ITEMS,
                "detail_max_items": GUIDANCE_DETAIL_MAX_ITEMS,
            },
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"guidance-{hashlib.sha256(canonical).hexdigest()[:16]}"

    def guidance_version(self) -> str:
        return self._guidance_version_cached(
            self._prompt_revision(GUIDANCE_PROMPT_FILES)
        )

@lru_cache(maxsize=1)
def get_config_loader() -> BusinessConfigLoader:
    return BusinessConfigLoader()

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any, Mapping
import unicodedata

from backend.business_config.loader import BusinessConfigLoader, get_config_loader
from backend.config.settings import get_settings
from backend.observability.metrics import log_metric
from backend.services.career_elements import career_elements_applicable


GUIDANCE_SKILL_AGENTS = frozenset(
    {
        "guidance_start",
        "guidance_emotion",
        "guidance_requirement",
        "guidance_plan",
    }
)
COACH_SKILL_AGENTS = frozenset(
    {
        "opening_evaluation",
        "emotion_evaluation",
        "output_expectations_evaluation",
        "development_plan_evaluation",
    }
)
KNOWLEDGE_SKILL_AGENTS = GUIDANCE_SKILL_AGENTS | COACH_SKILL_AGENTS


def _string_tuple(
    value: object,
    *,
    field_name: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    normalized = tuple(str(item).strip() for item in value if str(item).strip())
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} contains duplicate values")
    return normalized


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class KnowledgeSkill:
    id: str
    version: str
    enabled: bool
    priority: int
    applies_to: tuple[str, ...]
    triggers: tuple[str, ...]
    scopes: tuple[str, ...]
    context_requirements: dict[str, object]
    core_knowledge: str
    usage_rules: tuple[str, ...]
    retrieval_queries: tuple[str, ...]
    source_files: tuple[str, ...]
    reviewed_source_sha256: dict[str, str]
    reviewed_at: str
    config_hash: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, source_name: str) -> KnowledgeSkill:
        skill_id = str(raw.get("id") or "").strip()
        version = str(raw.get("version") or "").strip()
        reviewed_at = str(raw.get("reviewed_at") or "").strip()
        if not skill_id or not re.fullmatch(r"[a-z0-9_]+", skill_id):
            raise ValueError(
                f"{source_name}: id must use lowercase letters, digits, or underscores"
            )
        if not version:
            raise ValueError(f"{source_name}: version cannot be empty")
        if not reviewed_at:
            raise ValueError(f"{source_name}: reviewed_at cannot be empty")

        core_knowledge = str(raw.get("core_knowledge") or "").strip()
        if not core_knowledge:
            raise ValueError(f"{source_name}: core_knowledge cannot be empty")

        applies_to = _string_tuple(
            raw.get("applies_to"),
            field_name=f"{source_name}.applies_to",
        )
        unsupported_agents = sorted(set(applies_to) - KNOWLEDGE_SKILL_AGENTS)
        if unsupported_agents:
            raise ValueError(
                f"{source_name}: unsupported applies_to agents: {unsupported_agents}"
            )
        retrieval_queries = _string_tuple(
            raw.get("retrieval_queries") or [],
            field_name=f"{source_name}.retrieval_queries",
            allow_empty=True,
        )
        if len(retrieval_queries) > 1:
            raise ValueError(
                f"{source_name}: each skill may define at most one retrieval query"
            )

        source_files = _string_tuple(
            raw.get("source_files"),
            field_name=f"{source_name}.source_files",
        )
        for source_file in source_files:
            source_path = Path(source_file)
            if source_path.is_absolute() or ".." in source_path.parts:
                raise ValueError(
                    f"{source_name}: source_files must stay inside data/kb_raw"
                )
        raw_hashes = raw.get("reviewed_source_sha256")
        if not isinstance(raw_hashes, dict):
            raise ValueError(f"{source_name}.reviewed_source_sha256 must be a mapping")
        reviewed_hashes = {
            str(path).strip(): str(digest).strip().lower()
            for path, digest in raw_hashes.items()
            if str(path).strip()
        }
        if set(reviewed_hashes) != set(source_files):
            raise ValueError(
                f"{source_name}: source_files and reviewed_source_sha256 must match"
            )
        for path, digest in reviewed_hashes.items():
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"{source_name}: invalid SHA-256 for {path}")

        raw_context_requirements = raw.get("context_requirements") or {}
        if not isinstance(raw_context_requirements, Mapping):
            raise ValueError(
                f"{source_name}.context_requirements must be a mapping"
            )
        context_requirements = {
            str(key).strip(): value
            for key, value in raw_context_requirements.items()
            if str(key).strip()
        }

        canonical = {
            key: value
            for key, value in raw.items()
            if key != "config_hash"
        }
        try:
            priority = int(raw.get("priority", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{source_name}: priority must be an integer") from exc
        return cls(
            id=skill_id,
            version=version,
            enabled=bool(raw.get("enabled", True)),
            priority=priority,
            applies_to=applies_to,
            triggers=_string_tuple(
                raw.get("triggers"),
                field_name=f"{source_name}.triggers",
            ),
            scopes=_string_tuple(
                raw.get("scopes"),
                field_name=f"{source_name}.scopes",
            ),
            context_requirements=context_requirements,
            core_knowledge=core_knowledge,
            usage_rules=_string_tuple(
                raw.get("usage_rules"),
                field_name=f"{source_name}.usage_rules",
            ),
            retrieval_queries=retrieval_queries,
            source_files=source_files,
            reviewed_source_sha256=reviewed_hashes,
            reviewed_at=reviewed_at,
            config_hash=_canonical_hash(canonical),
        )


@dataclass(frozen=True, slots=True)
class ActiveKnowledgeSkill:
    skill: KnowledgeSkill
    matched_triggers: tuple[str, ...]
    stale_sources: tuple[str, ...]
    content_hash: str

    @property
    def stale(self) -> bool:
        return bool(self.stale_sources)

    @property
    def core_knowledge(self) -> str:
        return "" if self.stale else self.skill.core_knowledge

    def cache_identity(self) -> dict[str, object]:
        return {
            "id": self.skill.id,
            "version": self.skill.version,
            "stale": self.stale,
            "content_hash": self.content_hash,
        }

    def prompt_payload(self) -> dict[str, object]:
        return {
            "id": self.skill.id,
            "version": self.skill.version,
            "source_ref": f"skill:{self.skill.id}:{self.skill.version}",
            "priority": self.skill.priority,
            "matched_triggers": list(self.matched_triggers),
            "scopes": list(self.skill.scopes),
            "core_knowledge": self.core_knowledge,
            "usage_rules": list(self.skill.usage_rules),
            "stale": self.stale,
            "stale_source_count": len(self.stale_sources),
        }


class KnowledgeSkillRouter:
    """Deterministic domain-skill routing shared by retrieval and prompts."""

    def __init__(
        self,
        *,
        config_loader: BusinessConfigLoader | None = None,
        data_dir: Path | None = None,
    ):
        self.loader = config_loader or get_config_loader()
        self.data_dir = Path(data_dir or get_settings().data_dir)
        self._hash_cache: dict[Path, tuple[int, int, int, str]] = {}
        self._hash_lock = threading.Lock()
        self.skills = self._load_skills()

    def _load_skills(self) -> tuple[KnowledgeSkill, ...]:
        skill_dir = self.loader.config_dir / "skills"
        paths = sorted(skill_dir.glob("*.yaml"))
        if not paths:
            raise ValueError(f"No knowledge skill configs found in {skill_dir}")
        skills: list[KnowledgeSkill] = []
        seen_ids: set[str] = set()
        for path in paths:
            relative_path = str(path.relative_to(self.loader.config_dir))
            skill = KnowledgeSkill.from_mapping(
                self.loader.load_yaml(relative_path),
                source_name=relative_path,
            )
            if skill.id in seen_ids:
                raise ValueError(f"Duplicate knowledge skill id: {skill.id}")
            seen_ids.add(skill.id)
            skills.append(skill)
        return tuple(skills)

    def select(
        self,
        agent_name: str,
        context: Mapping[str, Any] | object,
        *,
        emit_metrics: bool = True,
    ) -> list[ActiveKnowledgeSkill]:
        if agent_name not in KNOWLEDGE_SKILL_AGENTS:
            return []
        normalized_context = self._normalized_context(context)
        active: list[ActiveKnowledgeSkill] = []
        for skill in self.skills:
            if not skill.enabled or agent_name not in skill.applies_to:
                continue
            if not self._context_requirements_met(skill, context):
                continue
            matched = tuple(
                trigger
                for trigger in skill.triggers
                if self._trigger_matches(trigger, normalized_context)
            )
            if not matched:
                continue
            stale_sources, source_state = self._source_state(skill)
            active.append(
                ActiveKnowledgeSkill(
                    skill=skill,
                    matched_triggers=matched,
                    stale_sources=stale_sources,
                    content_hash=_canonical_hash(
                        {
                            "config_hash": skill.config_hash,
                            "source_state": source_state,
                            "stale_sources": stale_sources,
                        }
                    ),
                )
            )

        # Priority affects presentation order only. No matched skill is discarded.
        active.sort(
            key=lambda item: (
                -len(item.matched_triggers),
                -item.skill.priority,
                item.skill.id,
            )
        )
        if emit_metrics:
            self._log_selection(agent_name, active)
        return active

    def retrieval_templates(
        self,
        active_skills: list[ActiveKnowledgeSkill],
    ) -> list[dict[str, object]]:
        templates: list[dict[str, object]] = []
        for active in active_skills:
            for query in active.skill.retrieval_queries:
                templates.append(
                    {
                        "template": query,
                        "scopes": list(active.skill.scopes),
                        "weight": 1.0,
                    }
                )
        return templates

    @staticmethod
    def prompt_payload(
        active_skills: list[ActiveKnowledgeSkill],
    ) -> list[dict[str, object]]:
        return [active.prompt_payload() for active in active_skills]

    def _source_state(
        self,
        skill: KnowledgeSkill,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        stale: list[str] = []
        state: dict[str, str] = {}
        for relative_path in skill.source_files:
            source_path = self.data_dir / "kb_raw" / relative_path
            current_hash = self._file_hash(source_path)
            state[relative_path] = current_hash or "missing"
            if current_hash != skill.reviewed_source_sha256[relative_path]:
                stale.append(relative_path)
        return tuple(stale), state

    @staticmethod
    def _context_requirements_met(
        skill: KnowledgeSkill,
        context: Mapping[str, Any] | object,
    ) -> bool:
        if not skill.context_requirements:
            return True
        if not isinstance(context, Mapping):
            return True
        nested = context.get("knowledge_skill_context")
        values = dict(nested) if isinstance(nested, Mapping) else {}
        values.update(context)
        for key, expected in skill.context_requirements.items():
            # Missing means an older/non-production caller has not computed
            # applicability.  Only an explicit conflicting value disables the
            # skill, preserving backward compatibility without overriding an
            # authoritative false result.
            if key in values and values[key] != expected:
                return False
        return True

    def _file_hash(self, path: Path) -> str | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        with self._hash_lock:
            cached = self._hash_cache.get(path)
            signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            if cached and cached[:3] == signature:
                return cached[3]
            digest = hashlib.sha256()
            try:
                with path.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError:
                return None
            value = digest.hexdigest()
            self._hash_cache[path] = (*signature, value)
            return value

    @classmethod
    def _normalized_context(cls, context: Mapping[str, Any] | object) -> str:
        payload: object = context
        if isinstance(context, Mapping) and "knowledge_skill_context" in context:
            payload = context["knowledge_skill_context"]
        elif isinstance(context, Mapping):
            payload = {
                key: context[key]
                for key in (
                    "profile",
                    "supplemental_info",
                    "conversation",
                    "message",
                    "query",
                    "user_input",
                    "intent_id",
                )
                if key in context
            }
        fragments: list[str] = []
        cls._collect_values(payload, fragments)
        normalized = unicodedata.normalize("NFKC", " ".join(fragments)).casefold()
        return re.sub(r"\s+", " ", normalized).strip()

    @classmethod
    def _collect_values(cls, value: object, target: list[str]) -> None:
        if value is None:
            return
        if isinstance(value, str):
            target.append(value)
            return
        if isinstance(value, Mapping):
            for item in value.values():
                cls._collect_values(item, target)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                cls._collect_values(item, target)
            return
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            cls._collect_values(model_dump(exclude_none=True), target)
            return
        target.append(str(value))

    @staticmethod
    def _trigger_matches(trigger: str, normalized_context: str) -> bool:
        normalized_trigger = re.sub(
            r"\s+",
            " ",
            unicodedata.normalize("NFKC", trigger).casefold(),
        ).strip()
        if not normalized_trigger:
            return False
        if any("\u4e00" <= char <= "\u9fff" for char in normalized_trigger):
            return normalized_trigger in normalized_context
        pattern = re.escape(normalized_trigger).replace(r"\ ", r"\s+")
        return (
            re.search(
                rf"(?<![a-z0-9]){pattern}(?![a-z0-9])",
                normalized_context,
            )
            is not None
        )

    @staticmethod
    def _log_selection(
        agent_name: str,
        active_skills: list[ActiveKnowledgeSkill],
    ) -> None:
        stale = [active.skill.id for active in active_skills if active.stale]
        log_metric(
            "knowledge.skills",
            agent_name=agent_name,
            knowledge_skill_active_count=len(active_skills),
            knowledge_skill_ids=",".join(active.skill.id for active in active_skills),
            knowledge_skill_stale_ids=",".join(stale),
            knowledge_skill_query_count=sum(
                len(active.skill.retrieval_queries) for active in active_skills
            ),
            knowledge_skill_core_chars=sum(
                len(active.core_knowledge) for active in active_skills
            ),
        )
        for active in active_skills:
            if active.stale:
                log_metric(
                    "knowledge.skill.stale",
                    agent_name=agent_name,
                    knowledge_skill_id=active.skill.id,
                    stale_source_count=len(active.stale_sources),
                )


def build_knowledge_skill_context(
    state: object,
    *,
    include_conversation: bool,
) -> dict[str, object]:
    profile = getattr(state, "employee_profile", None)
    intent = getattr(state, "intent", None)
    supplemental = getattr(state, "supplemental_info", "") or ""
    if include_conversation:
        excerpt = getattr(state, "supplemental_info_excerpt", None)
        if callable(excerpt):
            supplemental = excerpt()
    intent_id = getattr(intent, "intent_id", "") if intent else ""
    return {
        "profile": profile,
        "supplemental_info": supplemental,
        "performance_context": (
            getattr(intent, "performance_context", "") if intent else ""
        )
        or "",
        "conversation": (
            getattr(state, "conversation", []) if include_conversation else []
        ),
        "intent_id": intent_id,
        "career_elements_applicable": career_elements_applicable(
            profile,
            intent_id,
        ),
    }


@lru_cache(maxsize=1)
def get_knowledge_skill_router() -> KnowledgeSkillRouter:
    return KnowledgeSkillRouter()

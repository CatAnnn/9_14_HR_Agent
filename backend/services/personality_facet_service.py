from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from datetime import datetime, timezone
import hashlib
import json
import logging
import random

from backend.config.settings import Settings, get_settings
from backend.exceptions.llm_errors import StructuredOutputError
from backend.observability.metrics import elapsed_ms, log_metric, now_ms
from backend.repositories.personality_facet_repository import (
    PersonalityFacetRepository,
)
from backend.schemas.personality_facets import (
    PersonalityFacetGenerationOutput,
    PersonalityFacetState,
)
from backend.schemas.simulation import BigFivePersonality
from backend.schemas.state import SessionState
from backend.services.executor_utils import run_db_with_context
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.model_scheduler import ModelScheduler
from backend.services.prompt_service import PromptService


logger = logging.getLogger(__name__)

FACET_GENERATOR_VERSION = "big-five-facets-v2"
FACET_PROMPT_TEMPLATE = "personality/facet_initialization.jinja2"
FACET_GENERATION_POLICY_VERSION = "facet-generation-policy-v2"
FACET_STRUCTURED_TRANSPORT = "json_schema"
FACET_JSON_SCHEMA_STRICT = False
FACET_DOMAINS = (
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
)


class PersonalityFacetConstraintError(ValueError):
    pass


class PersonalityFacetService:
    """Creates and reuses stable 30-facet profiles without exposing them publicly."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        model_scheduler: ModelScheduler | None = None,
        llm_service: LangChainLLMService | None = None,
        repository: PersonalityFacetRepository | None = None,
        executor: Executor | None = None,
    ):
        self.settings = settings or get_settings()
        self.model_scheduler = model_scheduler or ModelScheduler(self.settings)
        self.llm_service = llm_service or LangChainLLMService()
        self.repository = repository or PersonalityFacetRepository()
        self.executor = executor
        self._inflight: dict[str, asyncio.Task[PersonalityFacetState]] = {}
        self._inflight_lock = asyncio.Lock()

    async def ensure_for_state(
        self,
        state: SessionState,
        *,
        allow_model: bool,
    ) -> PersonalityFacetState:
        if state.personality is None:
            raise ValueError("Personality facets require confirmed Big Five scores.")
        identity = self._identity(state)
        existing = state.personality_facets
        if existing is not None and existing.input_signature == identity["signature"]:
            return existing
        if (
            existing is not None
            and state.conversation
            and self._matches_parent_scores(existing, state.personality)
        ):
            log_metric(
                "personality_facets.active_session_version_preserved",
                existing_generator_version=existing.generator_version,
                current_generator_version=FACET_GENERATOR_VERSION,
            )
            return existing

        profile_key = identity["profile_key"]
        async with self._inflight_lock:
            task = self._inflight.get(profile_key)
            if task is None or task.done():
                task = asyncio.create_task(
                    self._load_or_create(
                        state=state.model_copy(deep=True),
                        identity=identity,
                        allow_model=allow_model,
                    ),
                    name=f"personality-facets-{profile_key[:12]}",
                )
                self._inflight[profile_key] = task
        try:
            facets = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._inflight_lock:
                    if self._inflight.get(profile_key) is task:
                        self._inflight.pop(profile_key, None)
        state.personality_facets = facets
        return facets

    def deterministic_for_state(self, state: SessionState) -> PersonalityFacetState:
        if state.personality is None:
            raise ValueError("Personality facets require confirmed Big Five scores.")
        identity = self._identity(state)
        existing = state.personality_facets
        if existing is not None and existing.input_signature == identity["signature"]:
            return existing
        if (
            existing is not None
            and state.conversation
            and self._matches_parent_scores(existing, state.personality)
        ):
            return existing
        facets = self._deterministic_state(
            personality=state.personality,
            signature=identity["signature"],
            seed=identity["profile_key"],
        )
        state.personality_facets = facets
        return facets

    async def _load_or_create(
        self,
        *,
        state: SessionState,
        identity: dict[str, str],
        allow_model: bool,
    ) -> PersonalityFacetState:
        cached = await run_db_with_context(
            self.executor,
            "personality_facets.read",
            self.repository.get,
            identity["profile_key"],
        )
        if cached is not None:
            log_metric("personality_facets.initialize", source="database_cache")
            return cached

        started = now_ms()
        generated: PersonalityFacetState | None = None
        if allow_model:
            generated = await self._generate_with_model(state, identity)
        if generated is None:
            generated = self._deterministic_state(
                personality=state.personality,
                signature=identity["signature"],
                seed=identity["profile_key"],
            )

        canonical = await run_db_with_context(
            self.executor,
            "personality_facets.save",
            self.repository.save_or_get,
            profile_key=identity["profile_key"],
            employee_key_hash=identity["employee_key_hash"],
            parent_scores=self._parent_scores(state.personality),
            facets=generated,
        )
        log_metric(
            "personality_facets.initialize",
            source=canonical.source,
            model=canonical.model_name,
            duration_ms=elapsed_ms(started),
        )
        return canonical

    async def _generate_with_model(
        self,
        state: SessionState,
        identity: dict[str, str],
    ) -> PersonalityFacetState | None:
        assert state.personality is not None
        prompt = PromptService(
            prompt_dir=self.settings.prompt_dir,
            auto_reload=self.settings.prompt_auto_reload_enabled,
        ).render(
            FACET_PROMPT_TEMPLATE,
            **self._parent_scores(state.personality),
            employee_id=identity["employee_key_hash"],
            stable_personality_evidence="",
            personality_facets="",
        )
        model_name = self.settings.model_for_task("personality_facets")
        for attempt in range(2):
            try:
                async with self.model_scheduler.slot(
                    session_id=state.session_id,
                    category="guidance",
                    endpoint=self.settings.chat_url,
                    model=model_name,
                ):
                    output = await self.llm_service.ainvoke_structured_single(
                        prompt=prompt,
                        schema=PersonalityFacetGenerationOutput,
                        task_name="personality_facets",
                        model=model_name,
                        structured_transport=FACET_STRUCTURED_TRANSPORT,
                        json_schema_strict=FACET_JSON_SCHEMA_STRICT,
                    )
                self.validate_output(output, state.personality)
                return PersonalityFacetState(
                    **output.model_dump(),
                    input_signature=identity["signature"],
                    source="model",
                    generator_version=FACET_GENERATOR_VERSION,
                    model_name=model_name,
                    created_at=datetime.now(timezone.utc),
                )
            except StructuredOutputError as exc:
                retryable = exc.code != "timeout" and attempt == 0
                log_metric(
                    "personality_facets.model_error",
                    attempt=attempt + 1,
                    error_code=exc.code,
                    retrying=retryable,
                )
                if retryable:
                    continue
                return None
            except PersonalityFacetConstraintError as exc:
                retryable = attempt == 0
                log_metric(
                    "personality_facets.constraint_error",
                    attempt=attempt + 1,
                    retrying=retryable,
                    error=str(exc)[:200],
                )
                if retryable:
                    continue
                return None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Personality facet model initialization failed: %s",
                    type(exc).__name__,
                )
                log_metric(
                    "personality_facets.model_error",
                    attempt=attempt + 1,
                    error_code=type(exc).__name__,
                    retrying=False,
                )
                return None
        return None

    @classmethod
    def validate_output(
        cls,
        output: PersonalityFacetGenerationOutput,
        personality: BigFivePersonality,
    ) -> None:
        for domain in FACET_DOMAINS:
            parent = int(getattr(personality, domain))
            group = getattr(output, domain)
            if group.score != parent:
                raise PersonalityFacetConstraintError(
                    f"{domain}.score must equal its parent score."
                )
            values = list(group.facets.model_dump().values())
            if len(values) != 6 or sum(values) != parent * 6:
                raise PersonalityFacetConstraintError(
                    f"{domain} facets must have an exact mean of {parent}."
                )
            limit = cls._offset_limit(parent)
            if any(abs(value - parent) > limit for value in values):
                raise PersonalityFacetConstraintError(
                    f"{domain} facet offset exceeds {limit}."
                )
            if parent not in {0, 100} and len(set(values)) == 1:
                raise PersonalityFacetConstraintError(
                    f"{domain} facets must contain a bounded internal difference."
                )

    @classmethod
    def _deterministic_state(
        cls,
        *,
        personality: BigFivePersonality,
        signature: str,
        seed: str,
    ) -> PersonalityFacetState:
        output: dict[str, object] = {}
        for domain in FACET_DOMAINS:
            parent = int(getattr(personality, domain))
            facet_model = PersonalityFacetGenerationOutput.model_fields[
                domain
            ].annotation.model_fields["facets"].annotation
            names = tuple(facet_model.model_fields)
            if parent in {0, 100}:
                values = [parent] * 6
            else:
                limit = min(cls._offset_limit(parent), parent, 100 - parent)
                rng = random.Random(f"{seed}:{domain}")
                magnitudes = [rng.randint(1, limit) for _ in range(3)]
                offsets = [*magnitudes, *(-value for value in magnitudes)]
                rng.shuffle(offsets)
                values = [parent + offset for offset in offsets]
            output[domain] = {
                "score": parent,
                "facets": dict(zip(names, values, strict=True)),
            }
        generated = PersonalityFacetGenerationOutput.model_validate(output)
        cls.validate_output(generated, personality)
        return PersonalityFacetState(
            **generated.model_dump(),
            input_signature=signature,
            source="deterministic_fallback",
            generator_version=FACET_GENERATOR_VERSION,
            model_name=None,
            created_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _offset_limit(parent: int) -> int:
        if 40 <= parent <= 60:
            return 6
        if 25 <= parent <= 39 or 61 <= parent <= 75:
            return 5
        if 10 <= parent <= 24 or 76 <= parent <= 90:
            return 4
        return 3

    @staticmethod
    def _parent_scores(personality: BigFivePersonality) -> dict[str, int]:
        return {
            domain: int(getattr(personality, domain))
            for domain in FACET_DOMAINS
        }

    @classmethod
    def _matches_parent_scores(
        cls,
        facets: PersonalityFacetState,
        personality: BigFivePersonality,
    ) -> bool:
        return all(
            int(getattr(facets, domain).score)
            == int(getattr(personality, domain))
            for domain in FACET_DOMAINS
        )

    def _generation_fingerprint(self) -> str:
        prompt_path = self.settings.prompt_dir / FACET_PROMPT_TEMPLATE
        prompt_sha256 = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
        schema_sha256 = hashlib.sha256(
            json.dumps(
                PersonalityFacetGenerationOutput.model_json_schema(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        payload = {
            "policy_version": FACET_GENERATION_POLICY_VERSION,
            "prompt_sha256": prompt_sha256,
            "schema_sha256": schema_sha256,
            "model": self.settings.model_for_task("personality_facets"),
            "provider": self.settings.llm_provider,
            "endpoints": self.settings.chat_urls,
            "temperature": self.settings.temperature_for_task(
                "personality_facets"
            ),
            "top_p": self.settings.llm_top_p,
            "max_tokens": self.settings.max_tokens_for_task(
                "personality_facets"
            ),
            "timeout_seconds": self.settings.timeout_for_task(
                "personality_facets"
            ),
            "enable_thinking": self.settings.enable_thinking_for_task(
                "personality_facets"
            ),
            "thinking_budget": self.settings.llm_thinking_budget,
            "structured_transport": FACET_STRUCTURED_TRANSPORT,
            "json_schema_strict": FACET_JSON_SCHEMA_STRICT,
            "format_retry_count": 1,
            "timeout_retry_count": 0,
            "constraint_policy": "exact-parent-mean-and-bounded-offset-v1",
            "fallback_policy": "deterministic-balanced-offset-v1",
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _identity(self, state: SessionState) -> dict[str, str]:
        assert state.personality is not None
        profile = state.employee_profile
        raw_key = (
            getattr(profile, "employee_id", None)
            or getattr(profile, "employee_alias", None)
            or state.session_id
        )
        normalized_key = " ".join(str(raw_key).strip().casefold().split())
        employee_key_hash = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
        signature_payload = {
            "employee_key_hash": employee_key_hash,
            "generator_version": FACET_GENERATOR_VERSION,
            "generation_fingerprint": self._generation_fingerprint(),
            "parent_scores": self._parent_scores(state.personality),
        }
        signature = hashlib.sha256(
            json.dumps(
                signature_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        profile_key = hashlib.sha256(
            f"{employee_key_hash}:{signature}".encode("utf-8")
        ).hexdigest()
        return {
            "employee_key_hash": employee_key_hash,
            "signature": signature,
            "profile_key": profile_key,
        }

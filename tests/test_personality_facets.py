from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path

import pytest

from backend.config.settings import get_settings
from backend.exceptions.llm_errors import StructuredOutputError
from backend.schemas.conversation import ConversationTurn
from backend.schemas.personality_facets import PersonalityFacetGenerationOutput
from backend.schemas.profile import EmployeeProfile
from backend.schemas.simulation import BigFivePersonality
from backend.schemas.state import SessionState
from backend.services.guidance_service import GuidanceService
from backend.services.personality_facet_service import (
    PersonalityFacetConstraintError,
    PersonalityFacetService,
)


class _MemoryFacetRepository:
    def __init__(self):
        self.items = {}

    def get(self, profile_key):
        return self.items.get(profile_key)

    def save_or_get(self, *, profile_key, facets, **_kwargs):
        self.items.setdefault(profile_key, facets)
        return self.items[profile_key]


class _Scheduler:
    @asynccontextmanager
    async def slot(self, **_kwargs):
        yield


class _LLM:
    def __init__(self, output=None, errors=None):
        self.output = output
        self.errors = list(errors or [])
        self.calls = []

    async def ainvoke_structured_single(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return self.output


def _personality() -> BigFivePersonality:
    return BigFivePersonality(
        openness=67,
        conscientiousness=74,
        extraversion=38,
        agreeableness=52,
        neuroticism=19,
    )


def _state(employee_id: str = "employee-001") -> SessionState:
    return SessionState(
        session_id=f"session-{employee_id}",
        setup_ready=True,
        employee_profile=EmployeeProfile(
            employee_id=employee_id,
            employee_alias="不会进入模型的员工姓名",
            role="不会进入模型的独特岗位",
        ),
        personality=_personality(),
    )


def _settings_with_facet_prompt(
    root: Path,
    *,
    suffix: str = "",
):
    source = Path(
        "backend/prompts/personality/facet_initialization.jinja2"
    ).read_bytes()
    prompt_path = (
        root
        / "backend"
        / "prompts"
        / "personality"
        / "facet_initialization.jinja2"
    )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_bytes(source + suffix.encode("utf-8"))
    return get_settings().model_copy(update={"project_root": root})


def _generation_output(state: SessionState) -> PersonalityFacetGenerationOutput:
    generated = PersonalityFacetService._deterministic_state(
        personality=state.personality,
        signature="a" * 64,
        seed="test-seed",
    )
    return PersonalityFacetGenerationOutput.model_validate(
        generated.model_context()
    )


def test_installed_prompt_is_the_requested_exact_text():
    prompt_path = Path("backend/prompts/personality/facet_initialization.jinja2")
    assert hashlib.sha256(prompt_path.read_bytes()).hexdigest() == (
        "bc0a6a5984b23fbdfd372b77ca40289a49ee2e90f4448d1ae0dc39682d9e6997"
    )


def test_facet_identity_changes_with_prompt_schema_policy_fingerprint(
    tmp_path: Path,
):
    first_settings = _settings_with_facet_prompt(tmp_path / "first")
    second_settings = _settings_with_facet_prompt(
        tmp_path / "second",
        suffix="\n{# fingerprint change #}\n",
    )
    first_service = PersonalityFacetService(
        settings=first_settings,
        repository=_MemoryFacetRepository(),
    )
    second_service = PersonalityFacetService(
        settings=second_settings,
        repository=_MemoryFacetRepository(),
    )

    first_identity = first_service._identity(_state())
    second_identity = second_service._identity(_state())

    assert first_service._generation_fingerprint() != (
        second_service._generation_fingerprint()
    )
    assert first_identity["signature"] != second_identity["signature"]
    assert first_identity["profile_key"] != second_identity["profile_key"]


def test_facet_identity_changes_with_model_and_generation_config():
    base = get_settings()
    first_settings = base.model_copy(
        update={
            "chat_model": "",
            "personality_facet_model": "facet-cache-model-a",
            "llm_personality_facet_temperature": 0.1,
        }
    )
    second_settings = base.model_copy(
        update={
            "chat_model": "",
            "personality_facet_model": "facet-cache-model-b",
            "llm_personality_facet_temperature": 0.3,
        }
    )

    first = PersonalityFacetService(
        settings=first_settings,
        repository=_MemoryFacetRepository(),
    )._identity(_state())
    second = PersonalityFacetService(
        settings=second_settings,
        repository=_MemoryFacetRepository(),
    )._identity(_state())

    assert first["signature"] != second["signature"]
    assert first["profile_key"] != second["profile_key"]


@pytest.mark.asyncio
async def test_changed_facet_prompt_does_not_reuse_old_database_cache(
    tmp_path: Path,
):
    repository = _MemoryFacetRepository()
    first_service = PersonalityFacetService(
        settings=_settings_with_facet_prompt(tmp_path / "first"),
        repository=repository,
    )
    first = await first_service.ensure_for_state(
        _state("same-employee"),
        allow_model=False,
    )

    second_service = PersonalityFacetService(
        settings=_settings_with_facet_prompt(
            tmp_path / "second",
            suffix="\n{# new initialization policy #}\n",
        ),
        repository=repository,
    )
    second = await second_service.ensure_for_state(
        _state("same-employee"),
        allow_model=False,
    )

    assert first.input_signature != second.input_signature
    assert len(repository.items) == 2


def test_active_conversation_keeps_its_existing_facets_after_policy_change(
    tmp_path: Path,
):
    state = _state("active-conversation")
    first_service = PersonalityFacetService(
        settings=_settings_with_facet_prompt(tmp_path / "first"),
        repository=_MemoryFacetRepository(),
    )
    existing = first_service.deterministic_for_state(state)
    state.conversation.append(
        ConversationTurn(
            turn_index=1,
            speaker="manager",
            text="我们继续刚才的反馈。",
        )
    )

    second_service = PersonalityFacetService(
        settings=_settings_with_facet_prompt(
            tmp_path / "second",
            suffix="\n{# changed after conversation started #}\n",
        ),
        repository=_MemoryFacetRepository(),
    )

    assert second_service.deterministic_for_state(state) is existing


def test_deterministic_fallback_has_exact_means_and_is_reproducible():
    first_state = _state()
    second_state = _state()
    first = PersonalityFacetService._deterministic_state(
        personality=first_state.personality,
        signature="b" * 64,
        seed="same-employee",
    )
    second = PersonalityFacetService._deterministic_state(
        personality=second_state.personality,
        signature="b" * 64,
        seed="same-employee",
    )

    assert first.model_context() == second.model_context()
    PersonalityFacetService.validate_output(first, first_state.personality)
    for domain, parent in first_state.personality.model_dump().items():
        values = list(getattr(first, domain).facets.model_dump().values())
        assert len(values) == 6
        assert sum(values) == parent * 6
        assert len(set(values)) > 1


def test_extreme_parent_scores_keep_valid_exact_facets():
    personality = BigFivePersonality(
        openness=0,
        conscientiousness=100,
        extraversion=1,
        agreeableness=99,
        neuroticism=50,
    )
    generated = PersonalityFacetService._deterministic_state(
        personality=personality,
        signature="c" * 64,
        seed="edge-case",
    )
    PersonalityFacetService.validate_output(generated, personality)
    assert set(generated.openness.facets.model_dump().values()) == {0}
    assert set(generated.conscientiousness.facets.model_dump().values()) == {100}


def test_constraint_validation_rejects_parent_mean_drift():
    personality = _personality()
    output = _generation_output(_state())
    payload = output.model_dump()
    payload["openness"]["facets"]["fantasy"] += 1
    invalid = PersonalityFacetGenerationOutput.model_validate(payload)

    with pytest.raises(PersonalityFacetConstraintError, match="exact mean"):
        PersonalityFacetService.validate_output(invalid, personality)


@pytest.mark.asyncio
async def test_model_initialization_uses_json_schema_and_hides_employee_context():
    state = _state()
    output = _generation_output(state)
    llm = _LLM(output=output)
    service = PersonalityFacetService(
        settings=get_settings(),
        model_scheduler=_Scheduler(),
        llm_service=llm,
        repository=_MemoryFacetRepository(),
    )

    facets = await service.ensure_for_state(state, allow_model=True)

    assert facets.source == "model"
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["schema"] is PersonalityFacetGenerationOutput
    assert call["task_name"] == "personality_facets"
    assert call["structured_transport"] == "json_schema"
    assert call["json_schema_strict"] is False
    assert "tools" not in call
    assert "tool_choice" not in call
    assert "不会进入模型的员工姓名" not in call["prompt"]
    assert "不会进入模型的独特岗位" not in call["prompt"]
    assert "只属于 Employee 角色模拟使用的内部稳定人格参数" in call["prompt"]


@pytest.mark.asyncio
async def test_format_error_retries_once_but_timeout_does_not_retry():
    state = _state("format-retry")
    output = _generation_output(state)
    format_llm = _LLM(
        output=output,
        errors=[StructuredOutputError("schema_validation", "invalid")],
    )
    format_service = PersonalityFacetService(
        settings=get_settings(),
        model_scheduler=_Scheduler(),
        llm_service=format_llm,
        repository=_MemoryFacetRepository(),
    )
    result = await format_service.ensure_for_state(state, allow_model=True)
    assert result.source == "model"
    assert len(format_llm.calls) == 2

    timeout_state = _state("timeout")
    timeout_llm = _LLM(
        errors=[StructuredOutputError("timeout", "late")],
    )
    timeout_service = PersonalityFacetService(
        settings=get_settings(),
        model_scheduler=_Scheduler(),
        llm_service=timeout_llm,
        repository=_MemoryFacetRepository(),
    )
    result = await timeout_service.ensure_for_state(
        timeout_state,
        allow_model=True,
    )
    assert result.source == "deterministic_fallback"
    assert len(timeout_llm.calls) == 1


def test_facets_are_hidden_from_api_dump_but_included_in_persistence():
    state = _state()
    PersonalityFacetService(
        settings=get_settings(),
        repository=_MemoryFacetRepository(),
    ).deterministic_for_state(state)

    assert "personality_facets" not in state.model_dump(mode="json")
    payload = state.persistence_payload()
    assert "personality_facets" in payload
    restored = SessionState.model_validate(payload)
    assert restored.personality_facets is not None
    assert restored.personality_facets.model_context() == (
        state.personality_facets.model_context()
    )


def test_personality_facet_task_uses_dedicated_model_settings():
    settings = get_settings()
    assert settings.model_for_task("personality_facets") == (
        settings.personality_facet_model or settings.employee_model
    )
    assert settings.max_tokens_for_task("personality_facets") == 1200
    assert settings.timeout_for_task("personality_facets") == 45
    assert settings.enable_thinking_for_task("personality_facets") is False


@pytest.mark.asyncio
async def test_guidance_starts_facets_in_background_and_waits_at_final_barrier():
    state = _state("guidance-parallel")
    started = asyncio.Event()
    release = asyncio.Event()
    expected = PersonalityFacetService._deterministic_state(
        personality=state.personality,
        signature="d" * 64,
        seed="guidance-parallel",
    )

    class _DelayedFacets:
        async def ensure_for_state(self, current_state, *, allow_model):
            assert allow_model is True
            started.set()
            await release.wait()
            return expected

    service = object.__new__(GuidanceService)
    service.personality_facets = _DelayedFacets()
    task = service._start_personality_facets(state)
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    barrier = asyncio.create_task(
        service._finish_personality_facets(state, task)
    )
    await asyncio.sleep(0)
    assert not barrier.done()
    release.set()
    await asyncio.wait_for(barrier, timeout=1)
    assert state.personality_facets is expected

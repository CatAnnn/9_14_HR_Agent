from __future__ import annotations

import asyncio
from types import SimpleNamespace
import time

import pytest

from backend.config.settings import Settings
from backend.exceptions.llm_errors import StructuredOutputError
from backend.schemas.simulation import MotivationState
from backend.schemas.state import (
    REHEARSAL_DIMENSION_COVERAGE_VERSION,
    RehearsalRuntimeContext,
    SessionState,
)
from backend.services.emotion_transition_service import EmotionTransitionService
from backend.services.http_client import close_shared_async_client, get_shared_async_client
from backend.services.local_model_runtime import LocalModelRuntimeRecycler
from backend.services.motivation_scoring_service import MotivationScoringService
from backend.services.rehearsal_service import RehearsalService
from backend.services.session_service import SessionService
import backend.services.emotion_transition_service as emotion_module
import backend.services.motivation_scoring_service as motivation_module


class FakeRuntimeRecycler:
    def __init__(self) -> None:
        self.acquired: list[tuple[str, tuple[str, ...]]] = []
        self.released: list[str] = []

    def acquire_session_lease(self, session_id: str, service_names) -> None:
        self.acquired.append((session_id, tuple(service_names)))

    def release_session_lease(self, session_id: str) -> None:
        self.released.append(session_id)


class InMemorySessionStore:
    def __init__(self, state: SessionState) -> None:
        self.state = state

    def get_session(self, session_id: str) -> SessionState:
        assert session_id == self.state.session_id
        return self.state

    def save_session(self, state: SessionState) -> SessionState:
        self.state = state
        return state


class InMemorySessionRepository:
    def __init__(self, state: SessionState) -> None:
        self.state = state

    def get(self, session_id: str) -> SessionState:
        assert session_id == self.state.session_id
        return self.state

    def save(self, state: SessionState) -> SessionState:
        self.state = state
        return state



@pytest.mark.parametrize(
    ("coverage_version", "expected_dimensions"),
    [
        (0, []),
        (1, []),
        (REHEARSAL_DIMENSION_COVERAGE_VERSION, ["start"]),
    ],
)
def test_session_service_only_exposes_verified_manager_dimension_coverage(
    coverage_version: int,
    expected_dimensions: list[str],
) -> None:
    state = SessionState(
        session_id="session-dimensions",
        rehearsal_context=RehearsalRuntimeContext(
            covered_dimensions=["start"],
            dimension_coverage_version=coverage_version,
        ),
    )
    service = SessionService(
        repo=InMemorySessionRepository(state),
        runtime_recycler=FakeRuntimeRecycler(),
    )

    loaded = service.get_session("session-dimensions")

    assert loaded.rehearsal_context.covered_dimensions == expected_dimensions

@pytest.mark.asyncio
async def test_model_api_async_client_reuses_one_connection_pool() -> None:
    await close_shared_async_client()
    try:
        first = get_shared_async_client()
        second = get_shared_async_client()

        assert first is second

        await close_shared_async_client()
        assert first.is_closed

        third = get_shared_async_client()
        assert third is not first
    finally:
        await close_shared_async_client()


@pytest.mark.asyncio
async def test_named_http_pools_are_isolated_reused_and_closed_together() -> None:
    await close_shared_async_client()
    try:
        model_client = get_shared_async_client("model_farm")
        local_client = get_shared_async_client("local_inference")
        speech_client = get_shared_async_client("speech")

        assert get_shared_async_client("model_farm") is model_client
        assert get_shared_async_client("local_inference") is local_client
        assert len({id(model_client), id(local_client), id(speech_client)}) == 3

        await close_shared_async_client()
        assert model_client.is_closed
        assert local_client.is_closed
        assert speech_client.is_closed
    finally:
        await close_shared_async_client()


def test_local_models_remain_pinned_until_session_lease_release(monkeypatch) -> None:
    recycler = LocalModelRuntimeRecycler(
        Settings(
            model_provider_mode="local",
            local_model_auto_recycle_enabled=True,
            local_model_auto_start_enabled=True,
            local_model_pinned_services="qwen3_asr",
            local_model_idle_ttl_seconds=1,
        )
    )
    stops: list[str] = []
    monkeypatch.setattr(recycler, "_ensure_monitor_started", lambda: None)
    monkeypatch.setattr(recycler, "_stop_service", lambda service: stops.append(service) or True)
    services = ("qwen_embedding", "qwen_reranker")

    recycler.acquire_session_lease("session-1", services)
    recycler.acquire_session_lease("session-1", services)

    assert {service: recycler._active_uses[service] for service in services} == {
        "qwen_embedding": 1,
        "qwen_reranker": 1,
    }
    for service in services:
        recycler._last_used_at[service] = time.monotonic() - 5
        recycler._stop_if_idle(service)
    assert stops == []

    released_at = time.monotonic()
    recycler.release_session_lease("session-1")

    assert all(recycler._active_uses[service] == 0 for service in services)
    assert all(recycler._last_used_at[service] >= released_at for service in services)
    for service in services:
        recycler._stop_if_idle(service)
    assert stops == []

    for service in services:
        recycler._last_used_at[service] = time.monotonic() - 5
        recycler._stop_if_idle(service)
    assert stops == ["qwen_embedding", "qwen_reranker"]


def test_rehearsal_end_only_clears_legacy_model_session_lease() -> None:
    runtime = FakeRuntimeRecycler()
    service = RehearsalService(
        settings=Settings(
            model_provider_mode="local",
        ),
        runtime_recycler=runtime,
    )
    service.session_service = InMemorySessionStore(
        SessionState(session_id="session-1", setup_ready=True, run_mode="rehearsal_report")
    )

    ended = service.end_rehearsal("session-1")

    assert runtime.acquired == []
    assert runtime.released == ["session-1"]
    assert ended.rehearsal_ended_at is not None


def test_ending_session_releases_local_model_lease() -> None:
    runtime = FakeRuntimeRecycler()
    repository = InMemorySessionRepository(SessionState(session_id="session-1"))
    service = SessionService(repo=repository, runtime_recycler=runtime)

    ended = service.end_session("session-1")

    assert ended.stage == "ended"
    assert runtime.released == ["session-1"]


@pytest.mark.asyncio
async def test_motivation_structured_call_uses_task_config_and_fallback(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FailingLLM:
        async def ainvoke_structured_single(self, **kwargs):
            captured.update(kwargs)
            raise StructuredOutputError("timeout", "test timeout")

    settings = SimpleNamespace(
        model_for_task=lambda task: "qwen3.6-flash",
        temperature_for_task=lambda task: 0.0,
        max_tokens_for_task=lambda task: 384,
        timeout_for_task=lambda task: 0.01,
        enable_thinking_for_task=lambda task: False,
    )
    monkeypatch.setattr(motivation_module, "LangChainLLMService", FailingLLM)
    monkeypatch.setattr(motivation_module, "get_settings", lambda: settings)
    state = SessionState(
        session_id="session-1",
        motivation=MotivationState(
            primary_motive_id="commerce",
            secondary_motive_ids=["security"],
        ),
    )

    updated = await MotivationScoringService().update_after_manager_message(state, "neutral")

    assert captured["task_name"] == "motivation_scoring"
    assert captured["model"] == "qwen3.6-flash"
    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == 384
    assert captured["timeout_seconds"] == 0.01
    assert captured["enable_thinking"] is False
    assert callable(captured["payload_normalizer"])
    assert any("timeout" in warning for warning in updated.warnings)


@pytest.mark.parametrize(
    ("message", "expected_primary", "expected_secondary"),
    [
        ("我不理解你的压力，也不会提供资源支持", -18.0, -12.0),
        ("我理解你的压力，也会提供资源支持", 23.0, 13.0),
        ("我不理解你的压力，但会提供资源支持", -3.0, -4.0),
        ("我不是不理解你的压力，也不是不会提供资源支持", 23.0, 13.0),
        ("我不会忽视你的压力，也不会停止提供资源支持", 23.0, 13.0),
    ],
)
def test_motivation_fallback_respects_negation_scope(
    message: str,
    expected_primary: float,
    expected_secondary: float,
) -> None:
    state = SessionState(
        session_id="motivation-negation",
        motivation=MotivationState(
            primary_motive_id="commerce",
            secondary_motive_ids=["security"],
        ),
    )

    output = MotivationScoringService()._fallback_score(state, message)

    assert output.primary_score_delta == expected_primary
    assert output.secondary_score_deltas == {"security": expected_secondary}


def test_motivation_fallback_records_negated_positive_cues_as_denial() -> None:
    state = SessionState(
        session_id="motivation-negation-reason",
        motivation=MotivationState(primary_motive_id="commerce"),
    )

    output = MotivationScoringService()._fallback_score(
        state,
        "我不理解你的压力，也不会提供资源支持",
    )

    assert output.reason_summary == "negated_empathy;withheld_action_path;denial_or_pressure"


@pytest.mark.asyncio
async def test_emotion_structured_call_uses_task_config_and_fallback(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FailingLLM:
        async def ainvoke_structured_single(self, **kwargs):
            captured.update(kwargs)
            raise StructuredOutputError("timeout", "test timeout")

    settings = SimpleNamespace(
        model_for_task=lambda task: "qwen3.6-flash",
        temperature_for_task=lambda task: 0.0,
        max_tokens_for_task=lambda task: 384,
        timeout_for_task=lambda task: 0.01,
        enable_thinking_for_task=lambda task: False,
    )
    monkeypatch.setattr(emotion_module, "LangChainLLMService", FailingLLM)
    monkeypatch.setattr(emotion_module, "get_settings", lambda: settings)
    service = EmotionTransitionService()
    state = SessionState(
        session_id="session-1",
        emotion_state=service.initial_state(None),
    )

    updated = await service.update_after_manager_message(state, "neutral")

    assert captured["task_name"] == "emotion_transition"
    assert captured["model"] == "qwen3.6-flash"
    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == 384
    assert captured["timeout_seconds"] == 0.01
    assert captured["enable_thinking"] is False
    assert callable(captured["payload_normalizer"])
    assert any("timeout" in warning for warning in updated.warnings)


def test_motivation_payload_normalizer_repairs_aliases_ranges_and_motive_ids() -> None:
    service = MotivationScoringService()
    state = SessionState(
        session_id="session-1",
        motivation=MotivationState(
            primary_motive_id="commerce",
            secondary_motive_ids=["security"],
        ),
    )

    payload, repair_steps = service._normalize_structured_payload(
        state,
        {
            "primaryScoreDelta": "125",
            "secondaryScoreDeltas": {"SECURITY": "-150", "unknown": 20},
            "detectedBehaviors": "empathy",
            "redlineHits": None,
            "reasonSummary": ["clear", "path"],
        },
    )

    assert payload["primary_score_delta"] == 100.0
    assert payload["secondary_score_deltas"] == {"security": -100.0}
    assert payload["reason_summary"] == "clear; path"
    assert set(payload) == {
        "primary_score_delta",
        "secondary_score_deltas",
        "reason_summary",
    }
    assert "normalized_secondary_motive_id" in repair_steps
    assert "discarded_unknown_secondary_motive" in repair_steps
    assert any(step.startswith("clamped_primary_score_delta") for step in repair_steps)


def test_emotion_payload_normalizer_repairs_vad_strategy_and_text_fields() -> None:
    payload, repair_steps = EmotionTransitionService()._normalize_structured_payload(
        {
            "vad": ["20%", -2, "0.4"],
            "strategy": "最大概率",
            "triggers": "pressure",
            "guidance": ["stay", "defensive"],
            "summary": None,
        }
    )

    assert payload["vad_delta"] == {
        "valence": 0.2,
        "arousal": -1.0,
        "dominance": 0.4,
    }
    assert payload["transition_strategy"] == "maximum_probability"
    assert payload["appraisal_tags"] == []
    assert payload["reason_summary"] == ""
    assert set(payload) == {
        "vad_delta",
        "transition_strategy",
        "appraisal_tags",
        "reason_summary",
    }
    assert "converted_vad_array_to_object" in repair_steps
    assert "normalized_transition_strategy" in repair_steps
    assert any(step.startswith("clamped_vad_delta.arousal") for step in repair_steps)


def test_task_payload_normalizers_reject_missing_core_fields() -> None:
    state = SessionState(
        session_id="session-1",
        motivation=MotivationState(
            primary_motive_id="commerce",
            secondary_motive_ids=["security"],
        ),
    )
    with pytest.raises(StructuredOutputError) as motivation_error:
        MotivationScoringService()._normalize_structured_payload(state, {})
    assert motivation_error.value.code == "missing_core_field"

    with pytest.raises(StructuredOutputError) as emotion_error:
        EmotionTransitionService()._normalize_structured_payload(
            {"transition_strategy": "expected_value"}
        )
    assert emotion_error.value.code == "missing_core_field"


def test_task_prompts_state_exact_required_output_contract() -> None:
    motivation_state = SessionState(
        session_id="session-1",
        motivation=MotivationState(
            primary_motive_id="commerce",
            secondary_motive_ids=["security"],
        ),
    )
    motivation_prompt = MotivationScoringService()._build_prompt(motivation_state, "message")
    assert "primary_score_delta 是必填" in motivation_prompt
    assert '"security"' in motivation_prompt
    assert "[-100,100]" in motivation_prompt

    emotion_service = EmotionTransitionService()
    emotion_state = SessionState(
        session_id="session-1",
        emotion_state=emotion_service.initial_state(None),
    )
    emotion_prompt = emotion_service._build_prompt(emotion_state, "message")
    assert "vad_delta是必填对象" in emotion_prompt
    assert "[-1,1]" in emotion_prompt
    assert "maximum_probability" in emotion_prompt

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.agents.guidance_agent import GUIDANCE_DIMENSION_SPECS
from backend.business_config.loader import get_config_loader
from backend.config.settings import Settings
from backend.exceptions.llm_errors import (
    ModelInvocationError,
    StructuredOutputError,
    is_raceable_model_error,
    is_retryable_http_status,
)
from backend.exceptions.workflow_errors import WorkflowError
from backend.schemas.intent import (
    PERFORMANCE_SECTION_TITLES,
    IntentGoalPerformanceItem,
    IntentPerformanceDraft,
)
from backend.schemas.profile import EmployeeProfile
from backend.schemas.state import SessionState
from backend.schemas.task import CoachTaskResult
from backend.services.coach_service import CoachService
from backend.services.guidance_service import GuidanceService
from backend.services.model_retry_race import (
    MODEL_RETRY_RACE_WIDTH,
    MODEL_TRANSIENT_RETRY_DELAYS_SECONDS,
    ModelRetryRaceExhausted,
    first_valid_model_result,
)
from backend.services.setup_service import SetupService


class _Scheduler:
    def __init__(self):
        self.entries = 0
        self.models: list[str] = []
        self.exits = 0
        self.active = 0
        self.max_active = 0

    @asynccontextmanager
    async def slot(self, **kwargs):
        self.entries += 1
        self.models.append(kwargs["model"])
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield
        finally:
            self.active -= 1
            self.exits += 1


def _settings():
    return SimpleNamespace(
        chat_url="https://model.example/v1/chat/completions",
        model_for_task=lambda task_name: f"{task_name}-model",
        model_retry_race_model="deepseek-v4-pro",
    )


def _intent_draft() -> IntentPerformanceDraft:
    return IntentPerformanceDraft(
        goal_performance_items=[
            IntentGoalPerformanceItem(
                goal=title,
                current_performance=f"{title}内容完整。",
                generation_reason="依据员工资料和当前沟通意图。",
            )
            for title in PERFORMANCE_SECTION_TITLES
        ]
    )


def _intent_setup_service(agent, scheduler):
    state = SessionState(
        session_id="intent-performance-race",
        employee_profile=EmployeeProfile(
            employee_alias="测试员工",
            performance_rating="2",
            key_goals=["完成核心交付目标"],
        ),
        supplemental_info="完整补充资料",
    )

    class SessionService:
        def get_session(self, _session_id):
            return state

    class Retrieval:
        async def aretrieve(self, *args, **kwargs):
            return []

    service = object.__new__(SetupService)
    service.session_service = SessionService()
    service.loader = get_config_loader()
    service.intent_performance_agent = agent
    service.model_scheduler = scheduler
    service.retrieval = Retrieval()
    service.settings = _settings()
    return service, state


def test_retry_race_model_defaults_to_deepseek():
    assert Settings.model_fields["model_retry_race_model"].default == "deepseek-v4-pro"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (425, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_retryable_http_statuses_are_explicit(status_code, expected):
    assert is_retryable_http_status(status_code) is expected


def test_only_output_and_transient_model_errors_are_raceable():
    assert is_raceable_model_error(
        StructuredOutputError("schema_validation", "invalid")
    )
    assert is_raceable_model_error(
        StructuredOutputError("business_validation", "invalid")
    )
    assert is_raceable_model_error(
        ModelInvocationError("http_503", "unavailable", retryable=True)
    )
    assert not is_raceable_model_error(
        ModelInvocationError(
            "http_400",
            "bad request",
            retryable=False,
            status_code=400,
        )
    )
    assert not is_raceable_model_error(ValueError("configuration error"))


@pytest.mark.asyncio
async def test_first_valid_result_ignores_fast_failure_and_cancels_losers():
    started: set[int] = set()
    canceled: set[int] = set()

    async def attempt(candidate_index: int) -> str:
        started.add(candidate_index)
        while len(started) < MODEL_RETRY_RACE_WIDTH:
            await asyncio.sleep(0)
        if candidate_index == 1:
            raise StructuredOutputError("schema_validation", "invalid")
        if candidate_index == 2:
            await asyncio.sleep(0.001)
            return "winner"
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            canceled.add(candidate_index)
            raise

    result = await first_valid_model_result(attempt)

    assert result.value == "winner"
    assert result.winner_index == 2
    assert result.failed_count == 1
    assert result.canceled_count == 3
    assert started == set(range(1, MODEL_RETRY_RACE_WIDTH + 1))
    assert canceled == {3, 4, 5}


@pytest.mark.asyncio
async def test_first_valid_result_reports_all_five_failures():
    async def attempt(candidate_index: int) -> str:
        raise StructuredOutputError(
            "schema_validation",
            f"candidate {candidate_index} invalid",
        )

    with pytest.raises(ModelRetryRaceExhausted) as exc_info:
        await first_valid_model_result(attempt)

    assert len(exc_info.value.errors) == MODEL_RETRY_RACE_WIDTH


@pytest.mark.asyncio
async def test_first_valid_result_staggers_candidates_by_configured_delays(
    monkeypatch,
):
    sleeps: list[float] = []

    async def fake_sleep(delay_seconds: float):
        sleeps.append(delay_seconds)

    async def attempt(candidate_index: int) -> str:
        raise RuntimeError(f"candidate {candidate_index} failed")

    monkeypatch.setattr(
        "backend.services.model_retry_race.asyncio.sleep",
        fake_sleep,
    )

    with pytest.raises(ModelRetryRaceExhausted):
        await first_valid_model_result(
            attempt,
            start_delays_seconds=MODEL_TRANSIENT_RETRY_DELAYS_SECONDS,
        )

    assert sorted(sleeps) == list(MODEL_TRANSIENT_RETRY_DELAYS_SECONDS[1:])


@pytest.mark.asyncio
async def test_guidance_service_launches_five_retries_and_cancels_losers():
    scheduler = _Scheduler()

    class Agent:
        def __init__(self):
            self.calls = 0
            self.retry_calls = 0
            self.canceled: set[int] = set()
            self.retry_models: list[str | None] = []
            self.prepare_calls = 0
            self.prepared = object()
            self.seen_prepared: list[object | None] = []

        def prepare_dimension(self, state, chunks, spec):
            self.prepare_calls += 1
            return self.prepared

        async def generate_dimension(
            self,
            state,
            chunks,
            spec,
            *,
            retry=False,
            retry_model=None,
            prepared=None,
        ):
            self.calls += 1
            self.seen_prepared.append(prepared)
            if not retry:
                raise StructuredOutputError("schema_validation", "initial invalid")
            self.retry_models.append(retry_model)
            self.retry_calls += 1
            candidate_index = self.retry_calls
            while self.retry_calls < MODEL_RETRY_RACE_WIDTH:
                await asyncio.sleep(0)
            if candidate_index == 1:
                raise StructuredOutputError("schema_validation", "retry invalid")
            if candidate_index == 2:
                await asyncio.sleep(0.001)
                return {"purpose": "first valid"}
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.canceled.add(candidate_index)
                raise

    agent = Agent()
    service = object.__new__(GuidanceService)
    service.agent = agent
    service.model_scheduler = scheduler
    service.settings = _settings()

    _spec, sections, _duration_ms, historical_match = await service._generate_dimension(
        "guidance-race",
        SessionState(session_id="guidance-race"),
        [],
        GUIDANCE_DIMENSION_SPECS[0],
    )

    assert sections == {"purpose": "first valid"}
    assert historical_match is None
    assert agent.calls == 1 + MODEL_RETRY_RACE_WIDTH
    assert agent.canceled == {3, 4, 5}
    assert scheduler.entries == 1 + MODEL_RETRY_RACE_WIDTH
    assert scheduler.exits == scheduler.entries
    assert scheduler.active == 0
    assert scheduler.max_active == MODEL_RETRY_RACE_WIDTH
    assert agent.retry_models == ["deepseek-v4-pro"] * MODEL_RETRY_RACE_WIDTH
    assert scheduler.models[0] == "guidance-model"
    assert scheduler.models[1:] == ["deepseek-v4-pro"] * MODEL_RETRY_RACE_WIDTH
    assert agent.prepare_calls == 1
    assert agent.seen_prepared == [agent.prepared] * (1 + MODEL_RETRY_RACE_WIDTH)


@pytest.mark.asyncio
async def test_guidance_service_does_not_race_http_400():
    scheduler = _Scheduler()

    class Agent:
        calls = 0

        async def generate_dimension(self, state, chunks, spec, *, retry=False):
            self.calls += 1
            raise ModelInvocationError(
                "http_400",
                "bad request",
                retryable=False,
                status_code=400,
            )

    agent = Agent()
    service = object.__new__(GuidanceService)
    service.agent = agent
    service.model_scheduler = scheduler
    service.settings = _settings()

    with pytest.raises(ModelInvocationError):
        await service._generate_dimension(
            "guidance-no-race",
            SessionState(session_id="guidance-no-race"),
            [],
            GUIDANCE_DIMENSION_SPECS[0],
        )

    assert agent.calls == 1
    assert scheduler.entries == scheduler.exits == 1


@pytest.mark.asyncio
async def test_intent_performance_runs_one_primary_then_five_retries():
    scheduler = _Scheduler()

    class Agent:
        def __init__(self):
            self.calls = 0
            self.retry_calls = 0
            self.canceled: set[int] = set()
            self.retry_models: list[str | None] = []
            self.prepare_calls = 0
            self.prepared = object()
            self.seen_prepared: list[object | None] = []

        def prepare_generation(self, **kwargs):
            self.prepare_calls += 1
            return self.prepared

        async def generate(
            self,
            *,
            retry=False,
            retry_model=None,
            prepared=None,
            **kwargs,
        ):
            self.calls += 1
            self.seen_prepared.append(prepared)
            if not retry:
                raise StructuredOutputError("schema_validation", "initial invalid")
            self.retry_models.append(retry_model)
            self.retry_calls += 1
            candidate_index = self.retry_calls
            while self.retry_calls < MODEL_RETRY_RACE_WIDTH:
                await asyncio.sleep(0)
            if candidate_index == 1:
                raise StructuredOutputError("schema_validation", "retry invalid")
            if candidate_index == 2:
                await asyncio.sleep(0.001)
                return _intent_draft()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.canceled.add(candidate_index)
                raise

    agent = Agent()
    service, state = _intent_setup_service(agent, scheduler)

    result = await service.generate_intent_performance_draft(
        state.session_id,
        intent_id="development",
    )

    assert result.performance_items == _intent_draft().goal_performance_items
    assert agent.calls == 1 + MODEL_RETRY_RACE_WIDTH
    assert agent.canceled == {3, 4, 5}
    assert agent.retry_models == ["deepseek-v4-pro"] * MODEL_RETRY_RACE_WIDTH
    assert scheduler.models == ["intent_performance-model"] + (
        ["deepseek-v4-pro"] * MODEL_RETRY_RACE_WIDTH
    )
    assert scheduler.entries == scheduler.exits == 1 + MODEL_RETRY_RACE_WIDTH
    assert scheduler.active == 0
    assert scheduler.max_active == MODEL_RETRY_RACE_WIDTH
    assert agent.prepare_calls == 1
    assert agent.seen_prepared == [agent.prepared] * (
        1 + MODEL_RETRY_RACE_WIDTH
    )


@pytest.mark.asyncio
async def test_intent_performance_staggers_network_retries_on_single_endpoint(
    monkeypatch,
):
    scheduler = _Scheduler()
    sleeps: list[float] = []
    blocked = asyncio.Event()

    async def fake_sleep(delay_seconds: float):
        sleeps.append(delay_seconds)
        await blocked.wait()

    monkeypatch.setattr(
        "backend.services.model_retry_race.asyncio.sleep",
        fake_sleep,
    )

    class Agent:
        def __init__(self):
            self.calls = 0
            self.retry_models: list[str | None] = []

        async def generate(self, *, retry=False, retry_model=None, **kwargs):
            self.calls += 1
            if not retry:
                raise ModelInvocationError(
                    "network",
                    "temporary gateway failure",
                    retryable=True,
                )
            self.retry_models.append(retry_model)
            return _intent_draft()

    agent = Agent()
    service, state = _intent_setup_service(agent, scheduler)

    result = await service.generate_intent_performance_draft(
        state.session_id,
        intent_id="development",
    )

    assert result.performance_items == _intent_draft().goal_performance_items
    assert agent.calls == 2
    assert agent.retry_models == ["deepseek-v4-pro"]
    assert sorted(sleeps) == list(MODEL_TRANSIENT_RETRY_DELAYS_SECONDS[1:])
    assert scheduler.entries == scheduler.exits == 2
    assert scheduler.active == 0
    assert scheduler.max_active == 1


@pytest.mark.asyncio
async def test_intent_performance_does_not_retry_http_400():
    scheduler = _Scheduler()

    class Agent:
        calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            raise ModelInvocationError(
                "http_400",
                "bad request",
                retryable=False,
                status_code=400,
            )

    agent = Agent()
    service, state = _intent_setup_service(agent, scheduler)

    with pytest.raises(WorkflowError):
        await service.generate_intent_performance_draft(
            state.session_id,
            intent_id="development",
        )

    assert agent.calls == 1
    assert scheduler.entries == scheduler.exits == 1
    assert scheduler.models == ["intent_performance-model"]


@pytest.mark.asyncio
async def test_coach_service_launches_five_retries_with_deepseek_model():
    scheduler = _Scheduler()

    class Orchestrator:
        def __init__(self):
            self.calls = 0
            self.retry_calls = 0
            self.canceled: set[int] = set()
            self.retry_models: list[str | None] = []
            self.prepare_calls = 0
            self.prepared = object()
            self.seen_prepared: list[object | None] = []

        def prepare_task(self, task_id, state, chunks):
            self.prepare_calls += 1
            return self.prepared

        async def run_task(
            self,
            task_id,
            state,
            chunks,
            *,
            retry=False,
            retry_model=None,
            prepared=None,
        ):
            self.calls += 1
            self.seen_prepared.append(prepared)
            if not retry:
                raise StructuredOutputError("schema_validation", "initial invalid")
            self.retry_models.append(retry_model)
            self.retry_calls += 1
            candidate_index = self.retry_calls
            while self.retry_calls < MODEL_RETRY_RACE_WIDTH:
                await asyncio.sleep(0)
            if candidate_index == 1:
                raise StructuredOutputError("schema_validation", "retry invalid")
            if candidate_index == 2:
                await asyncio.sleep(0.001)
                return CoachTaskResult(
                    task_id=task_id,
                    task_name="开场评估",
                    status="success",
                    score=4,
                    summary="first valid",
                )
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.canceled.add(candidate_index)
                raise

    orchestrator = Orchestrator()
    service = object.__new__(CoachService)
    service.orchestrator = orchestrator
    service.model_scheduler = scheduler
    service.settings = _settings()

    task_id, task_name, result, error, _duration_ms = await service._run_task(
        SessionState(session_id="coach-race"),
        [],
        "opening_evaluation",
        "开场评估",
    )

    assert task_id == "opening_evaluation"
    assert task_name == "开场评估"
    assert result.status == "success"
    assert error is None
    assert orchestrator.calls == 1 + MODEL_RETRY_RACE_WIDTH
    assert orchestrator.canceled == {3, 4, 5}
    assert orchestrator.retry_models == ["deepseek-v4-pro"] * MODEL_RETRY_RACE_WIDTH
    assert scheduler.models[0] == "coach_evaluator-model"
    assert scheduler.models[1:] == ["deepseek-v4-pro"] * MODEL_RETRY_RACE_WIDTH
    assert scheduler.entries == scheduler.exits == 1 + MODEL_RETRY_RACE_WIDTH
    assert scheduler.active == 0
    assert scheduler.max_active == MODEL_RETRY_RACE_WIDTH
    assert orchestrator.prepare_calls == 1
    assert orchestrator.seen_prepared == [orchestrator.prepared] * (
        1 + MODEL_RETRY_RACE_WIDTH
    )

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from backend.agents.coach_agent.coach_orchestrator import COACH_TASK_SPECS
from backend.agents.employee_agent import EmployeeAgent
from backend.agents.guidance_agent import GUIDANCE_DIMENSION_SPECS, GuidanceAgent
from backend.config.settings import Settings
from backend.exceptions.llm_errors import LLMError, StructuredOutputError
from backend.repositories.model_history_repository import (
    HistoricalSessionRecord,
    ModelHistoryRepository,
)
from backend.repositories.report_repository import ReportRepository
from backend.schemas.coach import CoachReport
from backend.schemas.conversation import ConversationTurn
from backend.schemas.conversation_summary import ConversationPromptContext
from backend.schemas.intent import IntentConfig, IntentGoalPerformanceItem, IntentResult
from backend.schemas.profile import EmployeeProfile
from backend.schemas.simulation import BigFivePersonality, MotivationState
from backend.schemas.state import SessionState
from backend.schemas.task import (
    BetterPhrase,
    CoachTaskResult,
    EvidenceRef,
    RiskItem,
)
from backend.services.model_history_fallback_service import (
    EMPLOYEE_REPLY_HISTORY_WARNING,
    HISTORY_FALLBACK_METADATA_KEY,
    HistoricalCoachTaskMatch,
    HistoricalEmployeeReplyMatch,
    HistoricalGuidanceMatch,
    ModelHistoryFallbackService,
)
from backend.services.coach_service import CoachService
from backend.services.guidance_service import GuidanceService
from backend.services.model_retry_race import ModelRetryRaceExhausted
from tests.test_guidance_streaming import _dimension_result


class _Repository:
    def __init__(self, records: list[HistoricalSessionRecord]):
        self.records = records
        self.calls = 0

    def list_candidates(
        self,
        _state,
        *,
        limit: int,
        max_age_days: int,
        guidance_version: str | None = None,
        culture_version: str | None = None,
        coach_version: str | None = None,
    ):
        self.calls += 1
        assert limit == 50
        assert max_age_days == 730
        return list(self.records)


def _settings(**overrides) -> Settings:
    return Settings(
        model_history_fallback_enabled=True,
        model_history_fallback_min_session_similarity=0.78,
        model_history_fallback_min_reply_similarity=0.72,
        model_history_fallback_min_conversation_similarity=0.88,
        **overrides,
    )


def _state(
    session_id: str,
    *,
    locale: str = "zh-CN",
    employee_id: str = "80000007",
    intent_id: str = "development",
    personality: BigFivePersonality | None = None,
    primary_motive_id: str = "commerce",
    secondary_motive_ids: list[str] | None = None,
    performance: str = "核心业务招聘目标基本达成，高端人才引进仍需提升。",
    conversation: list[ConversationTurn] | None = None,
) -> SessionState:
    personality = personality or BigFivePersonality(
        openness=70,
        conscientiousness=79,
        extraversion=80,
        agreeableness=17,
        neuroticism=85,
    )
    secondary_motive_ids = secondary_motive_ids or []
    return SessionState(
        session_id=session_id,
        locale=locale,
        setup_ready=True,
        employee_profile=EmployeeProfile(
            employee_id=employee_id,
            employee_alias="Mr. Recruiter",
            role="Recruiting Manager",
            department="PAC",
            level="G9",
            review_cycle="当前绩效周期",
            performance_rating="3",
            key_goals=["精准补齐自动驾驶人才梯队"],
            historical_feedback=["高端人才储备需要继续加强"],
        ),
        intent=IntentResult(
            intent_id=intent_id,
            config=IntentConfig(id=intent_id, name="发展型反馈"),
            performance_context=performance,
            performance_items=[
                IntentGoalPerformanceItem(
                    goal="目标达成总览",
                    current_performance=performance,
                )
            ],
        ),
        personality=personality,
        motivation=MotivationState(
            primary_motive_id=primary_motive_id,
            secondary_motive_ids=secondary_motive_ids,
        ),
        conversation=conversation or [],
    )


def _guidance_report(state: SessionState, *, version: str, culture: str):
    sections = {}
    for spec in GUIDANCE_DIMENSION_SPECS:
        output = _dimension_result(spec.schema)
        expected_count = len(GuidanceAgent._fixed_point_titles(state, spec))
        output_points = list(output.points)
        while len(output_points) < expected_count:
            output_points.append(output_points[-1].model_copy())
        generated_points = GuidanceAgent._attach_fixed_titles(
            state,
            spec,
            output_points[:expected_count],
        )
        points = [
            GuidanceAgent._stored_point_group(point)
            for point in generated_points
        ]
        sections.update(GuidanceAgent._legacy_sections(spec, points))
        sections[spec.point_group_key] = [
            point.model_dump(mode="json") for point in points
        ]
    report = GuidanceAgent.report_from_sections(state, [], sections)
    return report.model_copy(
        update={
            "guidance_version": version,
            "culture_version": culture,
        }
    )


def _coach_report(
    state: SessionState,
    *,
    version: str,
    quote: str,
) -> CoachReport:
    task_results: list[CoachTaskResult] = []
    for task_id, task_name in COACH_TASK_SPECS:
        if task_id != "opening_evaluation":
            task_results.append(
                CoachTaskResult(
                    task_id=task_id,
                    task_name=task_name,
                    status="insufficient_information",
                    summary="当前维度信息不足。",
                )
            )
            continue
        evidence = EvidenceRef(
            turn_index=1,
            speaker="manager",
            quote=quote,
        )
        task_results.append(
            CoachTaskResult(
                task_id=task_id,
                task_name=task_name,
                status="success",
                score=4,
                summary="经理能够先说明本次沟通目的。",
                evidence=[evidence],
                dimension_scores=[
                    {
                        "id": "opening",
                        "name": "开场",
                        "score": 4,
                        "evidence": [evidence.model_dump(mode="json")],
                    }
                ],
                better_phrases=[
                    BetterPhrase(
                        original=quote,
                        suggestion="我们先对齐今天要讨论的重点。",
                        reason="让沟通目标更清晰。",
                    )
                ],
                risks=[
                    RiskItem(
                        matched_text=quote,
                        explanation="需要继续保持事实对齐。",
                    )
                ],
                citations=[{"chunk_id": "stale-kb"}],
            )
        )
    return CoachReport(
        session_id=state.session_id,
        locale=state.locale,
        coach_version=version,
        status="success",
        task_results=task_results,
    )


def _record(
    state: SessionState,
    *,
    guidance_report=None,
    coach_report=None,
    guidance_state: SessionState | None = None,
    coach_state: SessionState | None = None,
) -> HistoricalSessionRecord:
    return HistoricalSessionRecord(
        state=state,
        updated_at=datetime.now(timezone.utc),
        guidance_report=guidance_report,
        coach_report=coach_report,
        guidance_state=guidance_state,
        coach_state=coach_state,
    )


def test_history_fallback_settings_are_bounded_and_default_off():
    assert Settings.model_fields["model_history_fallback_enabled"].default is False
    assert Settings.model_fields["model_history_fallback_max_sessions"].default == 50
    with pytest.raises(ValueError):
        Settings(model_history_fallback_min_session_similarity=1.1)


def test_motive_similarity_uses_primary_and_secondary_weights_only():
    current = MotivationState(
        primary_motive_id="commerce",
        secondary_motive_ids=["recognition", "power"],
        primary_score=-80,
    )
    same_selection = MotivationState(
        primary_motive_id="commerce",
        secondary_motive_ids=["recognition", "power"],
        primary_score=100,
    )
    one_secondary_matches = MotivationState(
        primary_motive_id="commerce",
        secondary_motive_ids=["recognition", "security"],
    )

    assert ModelHistoryFallbackService.motive_similarity(
        current,
        same_selection,
    ) == pytest.approx(1.0)
    assert ModelHistoryFallbackService.motive_similarity(
        current,
        one_secondary_matches,
    ) == pytest.approx(0.85 / 1.15)


def test_guidance_selects_best_same_employee_intent_personality_and_motives():
    current = _state("current", secondary_motive_ids=["recognition"])
    close = _state("close", secondary_motive_ids=["recognition"])
    far = _state(
        "far",
        personality=BigFivePersonality(
            openness=10,
            conscientiousness=10,
            extraversion=10,
            agreeableness=90,
            neuroticism=10,
        ),
        primary_motive_id="security",
    )
    version = "guidance-v-test"
    culture = "culture-v-test"
    repository = _Repository(
        [
            _record(far, guidance_report=_guidance_report(far, version=version, culture=culture)),
            _record(close, guidance_report=_guidance_report(close, version=version, culture=culture)),
        ]
    )
    service = ModelHistoryFallbackService(
        repository=repository,
        settings=_settings(),
    )

    match = service.find_guidance(
        current,
        guidance_version=version,
        culture_version=culture,
    )

    assert match is not None
    assert match.source_session_id == "close"
    assert match.report.session_id == "current"
    assert match.report.citations == []
    assert match.personality_similarity == pytest.approx(1.0)
    assert match.motive_similarity == pytest.approx(1.0)


def test_guidance_similarity_uses_immutable_generation_snapshot():
    current = _state("current")
    current_shaped_session = _state("historical")
    generation_snapshot = _state(
        "historical",
        personality=BigFivePersonality(
            openness=10,
            conscientiousness=10,
            extraversion=10,
            agreeableness=90,
            neuroticism=10,
        ),
    )
    version = "guidance-v-test"
    culture = "culture-v-test"
    report = _guidance_report(
        generation_snapshot,
        version=version,
        culture=culture,
    )
    service = ModelHistoryFallbackService(
        repository=_Repository(
            [
                _record(
                    current_shaped_session,
                    guidance_report=report,
                    guidance_state=generation_snapshot,
                )
            ]
        ),
        settings=_settings(),
    )

    assert service.find_guidance(
        current,
        guidance_version=version,
        culture_version=culture,
    ) is None


def test_guidance_history_accepts_legacy_snapshot_without_locale():
    current = _state("current", locale="de")
    historical = _state("historical", locale="de")
    legacy_payload = historical.model_dump(mode="json")
    legacy_payload.pop("locale")
    legacy_snapshot = SessionState.model_validate(legacy_payload)
    assert "locale" not in legacy_snapshot.model_fields_set
    version = "guidance-v-test"
    culture = "culture-v-test"
    report = _guidance_report(historical, version=version, culture=culture)
    service = ModelHistoryFallbackService(
        repository=_Repository(
            [
                _record(
                    historical,
                    guidance_report=report,
                    guidance_state=legacy_snapshot,
                )
            ]
        ),
        settings=_settings(),
    )

    match = service.find_guidance(
        current,
        guidance_version=version,
        culture_version=culture,
    )

    assert match is not None
    assert match.report.locale == "de"


def test_guidance_history_rejects_report_locale_mismatch():
    current = _state("current", locale="en")
    historical = _state("historical", locale="en")
    version = "guidance-v-test"
    culture = "culture-v-test"
    report = _guidance_report(
        historical,
        version=version,
        culture=culture,
    ).model_copy(update={"locale": "ja"})
    service = ModelHistoryFallbackService(
        repository=_Repository([_record(historical, guidance_report=report)]),
        settings=_settings(),
    )

    assert service.find_guidance(
        current,
        guidance_version=version,
        culture_version=culture,
    ) is None


def test_report_locale_binding_rejects_an_explicit_snapshot_conflict():
    explicit_japanese = _state("explicit", locale="ja")

    assert ModelHistoryFallbackService._state_for_report_locale(
        explicit_japanese,
        "de",
    ) is None


def test_guidance_fallback_removes_nested_stale_knowledge_references():
    current = _state("current")
    historical = _state("historical")
    version = "guidance-v-test"
    culture = "culture-v-test"
    report = _guidance_report(historical, version=version, culture=culture)
    assert report.dimension_points is not None
    point = report.dimension_points.start[0]
    point.summary_knowledge_chunk_ids = ["stale-summary"]
    point.detail_knowledge_chunk_ids = [
        ["stale-detail"]
        for _ in point.details
    ]
    service = ModelHistoryFallbackService(
        repository=_Repository(
            [_record(historical, guidance_report=report)]
        ),
        settings=_settings(),
    )

    match = service.find_guidance(
        current,
        guidance_version=version,
        culture_version=culture,
        dimension_key="start",
    )

    assert match is not None
    assert match.report.dimension_points is not None
    adapted = match.report.dimension_points.start[0]
    assert adapted.summary_knowledge_chunk_ids == []
    assert adapted.detail_knowledge_chunk_ids == []


@pytest.mark.asyncio
async def test_guidance_retry_exhaustion_falls_back_only_failed_dimension(monkeypatch):
    state = _state("current")
    historical = _state("historical")
    report = _guidance_report(
        historical,
        version="guidance-v-test",
        culture="culture-v-test",
    )
    match = HistoricalGuidanceMatch(
        report=report.model_copy(update={"session_id": state.session_id}),
        source_session_id=historical.session_id,
        score=1.0,
        personality_similarity=1.0,
        motive_similarity=1.0,
        context_similarity=1.0,
    )

    class FailingAgent:
        calls = 0

        async def generate_dimension(self, *args, **kwargs):
            self.calls += 1
            raise StructuredOutputError("schema_validation", "invalid")

    class Scheduler:
        @asynccontextmanager
        async def slot(self, **kwargs):
            yield None

    async def exhaust_all(attempt):
        errors = []
        for candidate_index in range(1, 6):
            try:
                await attempt(candidate_index)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        raise ModelRetryRaceExhausted(errors)

    monkeypatch.setattr(
        "backend.services.guidance_service.first_valid_model_result",
        exhaust_all,
    )
    service = object.__new__(GuidanceService)
    service.agent = FailingAgent()
    service.model_scheduler = Scheduler()
    service.settings = SimpleNamespace(
        chat_url="https://model.example/v1/chat/completions",
        model_for_task=lambda _task: "guidance-model",
        model_retry_race_model="retry-model",
    )

    async def historical_match(current_state, *, dimension_key):
        assert current_state is state
        assert dimension_key == "emotion"
        return match

    service._historical_guidance_match = historical_match
    spec = next(item for item in GUIDANCE_DIMENSION_SPECS if item.key == "emotion")

    _, sections, _, selected_match = await service._generate_dimension(
        state.session_id,
        state,
        [],
        spec,
    )

    assert selected_match is match
    assert set(sections) == {"risk_preview", "emotion_points"}
    assert sections["risk_preview"] == report.risk_preview
    assert "purpose" not in sections
    assert service.agent.calls == 6


def test_guidance_dimension_fallback_rechecks_fixed_titles_for_current_state():
    state = _state("current")
    report = _guidance_report(
        _state("historical"),
        version="guidance-v-test",
        culture="culture-v-test",
    )
    assert report.dimension_points is not None
    report.dimension_points.plan[-1].title = "历史模型生成的非固定标题"
    spec = next(item for item in GUIDANCE_DIMENSION_SPECS if item.key == "plan")

    assert GuidanceService._guidance_dimension_sections(
        report,
        spec,
        state,
    ) is None


@pytest.mark.parametrize(
    ("employee_id", "intent_id"),
    [("different-employee", "development"), ("80000007", "improvement")],
)
def test_guidance_never_crosses_employee_or_intent(employee_id: str, intent_id: str):
    current = _state("current")
    candidate = _state(
        "candidate",
        employee_id=employee_id,
        intent_id=intent_id,
    )
    version = "guidance-v-test"
    culture = "culture-v-test"
    service = ModelHistoryFallbackService(
        repository=_Repository(
            [
                _record(
                    candidate,
                    guidance_report=_guidance_report(
                        candidate,
                        version=version,
                        culture=culture,
                    ),
                )
            ]
        ),
        settings=_settings(),
    )

    assert service.find_guidance(
        current,
        guidance_version=version,
        culture_version=culture,
    ) is None


def test_employee_reply_can_choose_a_previous_session_and_marks_no_chain():
    manager_message = "我们来讨论下一阶段的发展计划和需要的资源支持。"
    current = _state(
        "current",
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text="我们先简单回顾一下日常工作。",
            ),
            ConversationTurn(
                turn_index=2,
                speaker="employee",
                text="好的，可以。",
            ),
            ConversationTurn(
                turn_index=3,
                speaker="manager",
                text=manager_message,
            ),
        ],
    )
    historical = _state(
        "historical",
        conversation=[
            ConversationTurn(
                turn_index=7,
                speaker="manager",
                text=manager_message,
            ),
            ConversationTurn(
                turn_index=8,
                speaker="employee",
                text="我希望先参与人才盘点项目，也需要业务负责人的资源支持。",
            ),
        ],
    )
    service = ModelHistoryFallbackService(
        repository=_Repository([_record(historical)]),
        settings=_settings(),
    )

    match = service.find_employee_reply(current, manager_message)

    assert match is not None
    assert match.source_session_id == "historical"
    assert match.reply.startswith("我希望先参与人才盘点")
    service.mark_employee_reply_fallback(current, manager_message, match)
    assert current.conversation[-1].metadata[HISTORY_FALLBACK_METADATA_KEY]
    assert EMPLOYEE_REPLY_HISTORY_WARNING in current.warnings


def test_employee_reply_does_not_reuse_a_previous_fallback_pair():
    manager_message = "我们来讨论下一阶段的发展计划和需要的资源支持。"
    historical = _state(
        "historical",
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text=manager_message,
                metadata={HISTORY_FALLBACK_METADATA_KEY: {"artifact": "employee_reply"}},
            ),
            ConversationTurn(
                turn_index=2,
                speaker="employee",
                text="这是已经兜底过的回复。",
            ),
        ],
    )
    service = ModelHistoryFallbackService(
        repository=_Repository([_record(historical)]),
        settings=_settings(),
    )

    assert service.find_employee_reply(_state("current"), manager_message) is None


@pytest.mark.parametrize(
    ("current_locale", "turn_locale", "expected_reply"),
    [
        ("de", "de", True),
        ("ja", "de", False),
        ("de", None, False),
        ("zh-CN", None, True),
    ],
)
def test_employee_reply_history_isolated_by_turn_locale(
    current_locale: str,
    turn_locale: str | None,
    expected_reply: bool,
):
    manager_message = "Please describe your next development step and support needs."
    metadata = {"locale": turn_locale} if turn_locale is not None else {}
    current = _state(
        "current",
        locale=current_locale,
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text=manager_message,
                metadata={"locale": current_locale},
            )
        ],
    )
    historical = _state(
        "historical",
        locale=current_locale,
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text=manager_message,
                metadata=metadata,
            ),
            ConversationTurn(
                turn_index=2,
                speaker="employee",
                text="I would start with a scoped development project.",
                metadata=metadata,
            ),
        ],
    )
    service = ModelHistoryFallbackService(
        repository=_Repository([_record(historical)]),
        settings=_settings(),
    )

    match = service.find_employee_reply(current, manager_message)

    assert (match is not None) is expected_reply


@pytest.mark.asyncio
async def test_employee_stream_uses_history_only_before_visible_output(monkeypatch):
    manager_message = "我们来讨论下一阶段的发展计划和资源支持。"
    state = _state(
        "current",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text=manager_message)
        ],
    )
    match = HistoricalEmployeeReplyMatch(
        reply="我希望先参与人才盘点，也需要业务资源支持。",
        source_session_id="historical",
        source_manager_turn_index=7,
        source_employee_turn_index=8,
        score=0.95,
        manager_message_similarity=0.98,
    )

    class Summary:
        async def context_for_reply(self, _state):
            return ConversationPromptContext()

    class FailingLLM:
        async def astream_text(self, **kwargs):
            if False:
                yield ""
            raise LLMError("all retries failed")

    monkeypatch.setattr(
        "backend.agents.employee_agent.LangChainLLMService",
        lambda: FailingLLM(),
    )
    agent = object.__new__(EmployeeAgent)
    agent.summary_service = Summary()
    agent._build_reply_prompt = lambda *args, **kwargs: "prompt"
    agent._reply_prefixes = lambda *_args: []

    async def historical_reply(current_state, current_message):
        assert current_state is state
        assert current_message == manager_message
        return match

    agent._historical_employee_reply = historical_reply

    pieces = [
        piece
        async for piece in agent.stream_reply(
            state,
            manager_message,
            retrieved_chunks=[],
        )
    ]

    assert pieces == [match.reply]
    assert EMPLOYEE_REPLY_HISTORY_WARNING in state.warnings


@pytest.mark.asyncio
async def test_employee_stream_never_appends_history_after_visible_output(monkeypatch):
    manager_message = "我们来讨论下一阶段的发展计划和资源支持。"
    state = _state("current")

    class Summary:
        async def context_for_reply(self, _state):
            return ConversationPromptContext()

    class PartialLLM:
        async def astream_text(self, **kwargs):
            yield "这是已经发送给前端的部分回复"
            raise LLMError("stream interrupted")

    monkeypatch.setattr(
        "backend.agents.employee_agent.LangChainLLMService",
        lambda: PartialLLM(),
    )
    agent = object.__new__(EmployeeAgent)
    agent.summary_service = Summary()
    agent._build_reply_prompt = lambda *args, **kwargs: "prompt"
    agent._reply_prefixes = lambda *_args: []
    history_called = False

    async def historical_reply(*args, **kwargs):
        nonlocal history_called
        history_called = True
        return None

    agent._historical_employee_reply = historical_reply
    pieces = []
    with pytest.raises(LLMError):
        async for piece in agent.stream_reply(
            state,
            manager_message,
            retrieved_chunks=[],
        ):
            pieces.append(piece)

    assert pieces
    assert history_called is False


def test_coach_history_rebinds_exact_quotes_and_removes_stale_citations():
    quote = "今天我们先对齐绩效结果，再讨论下一步发展。"
    historical = _state(
        "historical",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text=quote),
            ConversationTurn(turn_index=2, speaker="employee", text="可以，我想先听结论。"),
        ],
    )
    current = _state(
        "current",
        conversation=[
            ConversationTurn(turn_index=11, speaker="manager", text=quote),
            ConversationTurn(turn_index=12, speaker="employee", text="可以，我想先听结论。"),
        ],
    )
    version = "coach-v-test"
    service = ModelHistoryFallbackService(
        repository=_Repository(
            [
                _record(
                    historical,
                    coach_report=_coach_report(
                        historical,
                        version=version,
                        quote=quote,
                    ),
                )
            ]
        ),
        settings=_settings(),
    )

    match = service.find_coach_task(
        current,
        task_id="opening_evaluation",
        coach_version=version,
    )

    assert match is not None
    assert match.source_session_id == "historical"
    assert match.result.evidence[0].turn_index == 11
    assert match.result.dimension_scores[0].evidence[0].turn_index == 11
    assert match.result.citations == []


def test_coach_history_accepts_legacy_snapshot_without_locale():
    quote = "Heute gleichen wir zuerst das Ergebnis ab."
    conversation = [
        ConversationTurn(turn_index=1, speaker="manager", text=quote),
        ConversationTurn(turn_index=2, speaker="employee", text="In Ordnung."),
    ]
    historical = _state("historical", locale="de", conversation=conversation)
    current = _state("current", locale="de", conversation=conversation)
    legacy_payload = historical.model_dump(mode="json")
    legacy_payload.pop("locale")
    legacy_snapshot = SessionState.model_validate(legacy_payload)
    version = "coach-v-test"
    report = _coach_report(historical, version=version, quote=quote)
    service = ModelHistoryFallbackService(
        repository=_Repository(
            [
                _record(
                    historical,
                    coach_report=report,
                    coach_state=legacy_snapshot,
                )
            ]
        ),
        settings=_settings(),
    )

    match = service.find_coach_task(
        current,
        task_id="opening_evaluation",
        coach_version=version,
    )

    assert match is not None


def test_coach_history_rejects_report_locale_mismatch():
    quote = "Today we will align on the performance result."
    conversation = [
        ConversationTurn(turn_index=1, speaker="manager", text=quote),
        ConversationTurn(turn_index=2, speaker="employee", text="Okay."),
    ]
    historical = _state("historical", locale="en", conversation=conversation)
    current = _state("current", locale="en", conversation=conversation)
    version = "coach-v-test"
    report = _coach_report(
        historical,
        version=version,
        quote=quote,
    ).model_copy(update={"locale": "ja"})
    service = ModelHistoryFallbackService(
        repository=_Repository([_record(historical, coach_report=report)]),
        settings=_settings(),
    )

    assert service.find_coach_task(
        current,
        task_id="opening_evaluation",
        coach_version=version,
    ) is None


def test_coach_history_rejects_low_score_task_without_an_issue():
    quote = "今天我们先对齐绩效结果，再讨论下一步发展。"
    historical = _state(
        "historical",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text=quote),
            ConversationTurn(turn_index=2, speaker="employee", text="可以，我想先听结论。"),
        ],
    )
    current = _state(
        "current",
        conversation=[
            ConversationTurn(turn_index=11, speaker="manager", text=quote),
            ConversationTurn(turn_index=12, speaker="employee", text="可以，我想先听结论。"),
        ],
    )
    version = "coach-v-test"
    report = _coach_report(historical, version=version, quote=quote)
    report.task_results = [
        result.model_copy(
            update={
                "score": 2,
                "summary": "经理没有承接员工情绪。",
                "improvement_points": [],
            }
        )
        if result.task_id == "opening_evaluation"
        else result
        for result in report.task_results
    ]
    service = ModelHistoryFallbackService(
        repository=_Repository([_record(historical, coach_report=report)]),
        settings=_settings(),
    )

    assert service.find_coach_task(
        current,
        task_id="opening_evaluation",
        coach_version=version,
    ) is None


def test_coach_history_rejects_scored_task_without_mappable_manager_evidence():
    quote = "今天我们先对齐绩效结果，再讨论下一步发展。"
    historical = _state(
        "historical",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text=quote),
            ConversationTurn(turn_index=2, speaker="employee", text="可以。"),
        ],
    )
    current = _state(
        "current",
        conversation=[
            ConversationTurn(turn_index=11, speaker="manager", text=quote),
            ConversationTurn(turn_index=12, speaker="employee", text="可以。"),
        ],
    )
    version = "coach-v-test"
    report = _coach_report(historical, version=version, quote=quote)
    report.task_results = [
        result.model_copy(
            update={
                "evidence": [],
                "dimension_scores": [],
                "better_phrases": [],
                "risks": [],
            }
        )
        if result.task_id == "opening_evaluation"
        else result
        for result in report.task_results
    ]
    service = ModelHistoryFallbackService(
        repository=_Repository([_record(historical, coach_report=report)]),
        settings=_settings(),
    )

    assert service.find_coach_task(
        current,
        task_id="opening_evaluation",
        coach_version=version,
    ) is None


@pytest.mark.asyncio
async def test_coach_retry_exhaustion_falls_back_only_failed_task(monkeypatch):
    state = _state("current")
    quote = "今天我们先对齐绩效结果，再讨论下一步发展。"
    state.conversation = [
        ConversationTurn(turn_index=11, speaker="manager", text=quote),
        ConversationTurn(turn_index=12, speaker="employee", text="可以。"),
    ]
    historical_result = next(
        result
        for result in _coach_report(
            _state("historical"),
            version="coach-v-test",
            quote=quote,
        ).task_results
        if result.task_id == "opening_evaluation"
    )
    match = HistoricalCoachTaskMatch(
        result=historical_result,
        source_session_id="historical",
        score=0.94,
        conversation_similarity=0.93,
    )
    service = object.__new__(CoachService)
    service.orchestrator = SimpleNamespace()
    service.settings = SimpleNamespace(model_retry_race_model="retry-model")
    calls = 0

    async def invoke(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise StructuredOutputError("schema_validation", "invalid")

    async def exhaust_all(attempt):
        errors = []
        for candidate_index in range(1, 6):
            try:
                await attempt(candidate_index)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        raise ModelRetryRaceExhausted(errors)

    async def historical_task(current_state, task_id):
        assert current_state is state
        assert task_id == "opening_evaluation"
        return match

    service._invoke_task_attempt = invoke
    service._historical_coach_task_match = historical_task
    monkeypatch.setattr(
        "backend.services.coach_service.first_valid_model_result",
        exhaust_all,
    )

    task_id, _, result, error, _ = await service._run_task(
        state,
        [],
        "opening_evaluation",
        "开场与绩效对齐",
    )

    assert task_id == "opening_evaluation"
    assert error is None
    assert result.extra[HISTORY_FALLBACK_METADATA_KEY]["artifact"] == "coach_task"
    assert calls == 6


def test_coach_history_allows_highly_similar_non_identical_conversation():
    quote = "今天我们先对齐绩效结果，再讨论下一步发展。"
    historical = _state(
        "historical",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text=quote),
            ConversationTurn(turn_index=2, speaker="employee", text="可以，我想先听结论。"),
        ],
    )
    current = _state(
        "current",
        conversation=[
            ConversationTurn(turn_index=11, speaker="manager", text=f"{quote}可以吗？"),
            ConversationTurn(turn_index=12, speaker="employee", text="可以，我想先听一下结论。"),
        ],
    )
    version = "coach-v-test"
    service = ModelHistoryFallbackService(
        repository=_Repository(
            [
                _record(
                    historical,
                    coach_report=_coach_report(
                        historical,
                        version=version,
                        quote=quote,
                    ),
                )
            ]
        ),
        settings=_settings(),
    )

    match = service.find_coach_task(
        current,
        task_id="opening_evaluation",
        coach_version=version,
    )

    assert match is not None
    assert match.result.evidence[0].turn_index == 11


def test_coach_insufficient_task_allows_near_identical_not_exact_conversation():
    quote = "今天我们先对齐绩效结果，再讨论下一步发展。"
    historical = _state(
        "historical",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text=quote),
            ConversationTurn(turn_index=2, speaker="employee", text="可以。"),
        ],
    )
    current = _state(
        "current",
        conversation=[
            ConversationTurn(turn_index=11, speaker="manager", text=f"{quote}吧"),
            ConversationTurn(turn_index=12, speaker="employee", text="可以。"),
        ],
    )
    version = "coach-v-test"
    service = ModelHistoryFallbackService(
        repository=_Repository(
            [
                _record(
                    historical,
                    coach_report=_coach_report(
                        historical,
                        version=version,
                        quote=quote,
                    ),
                )
            ]
        ),
        settings=_settings(),
    )

    match = service.find_coach_task(
        current,
        task_id="emotion_evaluation",
        coach_version=version,
    )

    assert match is not None
    assert match.result.status == "insufficient_information"


def test_report_input_snapshot_keeps_only_semantic_conversation_projection():
    state = _state(
        "snapshot",
        locale="ja",
        conversation=[
            ConversationTurn(
                turn_index=3,
                speaker="manager",
                text="请说明你的判断。",
                metadata={"rehearsal_timing": {"total_ms": 3500}},
            )
        ],
    )

    payload = json.loads(
        ReportRepository._serialized_input_context(
            state,
            include_conversation=True,
        )
    )

    assert payload["snapshot_version"] == 2
    assert payload["locale"] == "ja"
    assert SessionState.model_validate(payload).locale == "ja"
    assert payload["conversation"] == [
        {"turn_index": 3, "speaker": "manager", "text": "请说明你的判断。"}
    ]
    assert "created_at" not in payload["conversation"][0]
    assert "metadata" not in payload["conversation"][0]


def test_repository_disables_cross_session_history_without_authenticated_owner(
    monkeypatch,
):
    class NoConnectionRepository:
        def connection(self):
            raise AssertionError("database must not be accessed")

    monkeypatch.setattr(
        "backend.repositories.model_history_repository.get_current_auth_user_id",
        lambda: "auth-disabled",
    )
    repository = ModelHistoryRepository(repository=NoConnectionRepository())

    assert repository.list_candidates(
        _state("current"),
        limit=10,
        max_age_days=30,
    ) == []


def test_repository_query_hard_filters_owner_employee_intent_and_report_age(
    monkeypatch,
):
    captured = {}

    class Connection:
        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params
            return self

        def fetchall(self):
            return []

    class Repository:
        @contextmanager
        def connection(self):
            yield Connection()

    owner_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(
        "backend.repositories.model_history_repository.get_current_auth_user_id",
        lambda: owner_id,
    )
    repository = ModelHistoryRepository(repository=Repository())

    assert repository.list_candidates(
        _state("current"),
        limit=50,
        max_age_days=730,
        guidance_version="guidance-v-test",
        culture_version="culture-v-test",
    ) == []

    query = captured["query"]
    params = captured["params"]
    assert "candidate.owner_user_id = %s" in query
    assert "history.created_at >= %s" in query
    assert "history.source IN ('generation', 'legacy_backfill')" in query
    assert "NULLIF(BTRIM(" in query
    assert owner_id in params
    assert query.count("%s") == len(params)


def test_historical_fallback_reports_are_not_treated_as_permanent_cache():
    class GuidanceRepository:
        @staticmethod
        def get_guidance_source(_session_id):
            return "historical_fallback"

        @staticmethod
        def get_guidance(_session_id):
            raise AssertionError("fallback cache must be bypassed")

    guidance = object.__new__(GuidanceService)
    guidance.report_repo = GuidanceRepository()
    assert guidance._cached_report("session") is None

    class CoachRepository:
        @staticmethod
        def get_coach_source(_session_id):
            return "historical_fallback"

        @staticmethod
        def get_coach(_session_id):
            raise AssertionError("fallback cache must be bypassed")

    coach = object.__new__(CoachService)
    coach.report_repo = CoachRepository()
    assert coach._cached_report(
        "session",
        SimpleNamespace(coach_report_id="session"),
    ) is None


def test_coach_history_rejects_an_unmappable_quote():
    spoken = "今天我们先对齐绩效结果，再讨论下一步发展。"
    historical = _state(
        "historical",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text=spoken),
            ConversationTurn(turn_index=2, speaker="employee", text="可以。"),
        ],
    )
    current = _state(
        "current",
        conversation=[
            ConversationTurn(turn_index=11, speaker="manager", text=spoken),
            ConversationTurn(turn_index=12, speaker="employee", text="可以。"),
        ],
    )
    version = "coach-v-test"
    service = ModelHistoryFallbackService(
        repository=_Repository(
            [
                _record(
                    historical,
                    coach_report=_coach_report(
                        historical,
                        version=version,
                        quote="本次对话从未出现的经理原话。",
                    ),
                )
            ]
        ),
        settings=_settings(),
    )

    assert service.find_coach_task(
        current,
        task_id="opening_evaluation",
        coach_version=version,
    ) is None

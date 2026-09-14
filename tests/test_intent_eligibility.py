from __future__ import annotations

import pytest

from backend.business_config.loader import get_config_loader
from backend.schemas.intent import (
    PERFORMANCE_SECTION_TITLES,
    IntentGoalPerformanceItem,
    IntentResult,
)
from backend.schemas.profile import EmployeeProfile
from backend.schemas.simulation import BigFivePersonality, EmotionState, MotivationState
from backend.schemas.state import SessionState
from backend.services.intent_eligibility import evaluate_intent_eligibility
from backend.services.setup_service import SetupService
from backend.utils.rating_values import normalize_tcl
from scripts.import_employees import build_profile


EXPECTED_RULES = {
    "development": ([1, 2, 3], ["++", "+++", "++++"]),
    "improvement": ([4, 5], ["#", "+"]),
    "exit": ([4, 5], ["#", "+"]),
    "development_improvement": ([3, 4, 5], ["#", "+", "++"]),
    "improvement_exit": ([4, 5], ["#", "+"]),
}

EXPECTED_DESCRIPTIONS = {
    "development": "聚焦优势，规划长期成长",
    "improvement": "指出短板，推动达标整改",
    "exit": "长期绩效不达标，告知后续处置措施",
    "development_improvement": "肯定优势，同步整改不足",
    "improvement_exit": "限期整改，说明未达标将启动对应处置措施",
}


class _SessionService:
    def __init__(self, state: SessionState):
        self.state = state
        self.save_count = 0

    def get_session(self, _session_id: str) -> SessionState:
        return self.state

    def save_session(self, state: SessionState) -> SessionState:
        self.state = state
        self.save_count += 1
        return state


class _IntentAgent:
    async def recognize(self, **kwargs) -> IntentResult:
        return IntentResult(
            intent_id=kwargs["intent_id"],
            confidence=1.0,
            reason="test",
        )


def _intent(intent_id: str):
    return get_config_loader().intents()[intent_id]


def _service(state: SessionState) -> SetupService:
    service = object.__new__(SetupService)
    service.session_service = _SessionService(state)
    service.intent_agent = _IntentAgent()
    service.loader = get_config_loader()
    return service


def _performance_items() -> list[IntentGoalPerformanceItem]:
    return [
        IntentGoalPerformanceItem(
            goal=title,
            current_performance=f"{title}测试内容。",
        )
        for title in PERFORMANCE_SECTION_TITLES
    ]


def test_all_configured_intents_expose_the_expected_eligibility_matrix():
    intents = get_config_loader().intents()

    assert set(intents) == set(EXPECTED_RULES)
    for intent_id, (ratings, tcl_values) in EXPECTED_RULES.items():
        rule = intents[intent_id].eligibility
        assert rule is not None
        assert rule.performance_ratings == ratings
        assert rule.tcl_values == tcl_values
        assert rule.match_mode == "any"


def test_setup_options_expose_configured_intent_descriptions():
    service = object.__new__(SetupService)
    service.loader = get_config_loader()

    options = service.list_options()

    assert {
        item["id"]: item["description"] for item in options["intents"]
    } == EXPECTED_DESCRIPTIONS


@pytest.mark.parametrize(
    ("intent_id", "rating", "tcl"),
    [
        ("development", "1", None),
        ("development", "3", None),
        ("development", None, "++"),
        ("improvement", "4", None),
        ("improvement", "5", None),
        ("improvement", None, "#"),
        ("exit", "4", None),
        ("exit", None, "+"),
        ("development_improvement", "3", None),
        ("development_improvement", "5", None),
        ("development_improvement", None, "++"),
        ("improvement_exit", "4", None),
        ("improvement_exit", None, "+"),
    ],
)
def test_intent_is_allowed_when_rating_or_tcl_matches(
    intent_id: str,
    rating: str | None,
    tcl: str | None,
):
    result = evaluate_intent_eligibility(
        EmployeeProfile(performance_rating=rating, tcl=tcl),
        _intent(intent_id),
    )

    assert result.allowed is True
    assert result.status == "allowed"


@pytest.mark.parametrize(
    ("intent_id", "rating", "tcl"),
    [
        ("development", "4", "+"),
        ("improvement", "2", "++"),
        ("exit", "3", "++"),
        ("development_improvement", "2", "+++"),
        ("improvement_exit", "1", "++++"),
    ],
)
def test_intent_mismatch_is_advisory_when_neither_rating_nor_tcl_matches(
    intent_id: str,
    rating: str,
    tcl: str,
):
    result = evaluate_intent_eligibility(
        EmployeeProfile(performance_rating=rating, tcl=tcl),
        _intent(intent_id),
    )

    assert result.allowed is True
    assert result.status == "mismatch"
    assert "仍可确认当前选择并继续" in (result.message or "")


def test_conflicting_sources_still_allow_when_either_source_matches():
    result = evaluate_intent_eligibility(
        EmployeeProfile(performance_rating="2", tcl="+"),
        _intent("development"),
    )

    assert result.allowed is True
    assert result.performance_rating == 2
    assert result.tcl == "+"


def test_composite_tcl_uses_the_performance_component():
    raw_tcl = "Performance+,Size of Task++,Job Market ++"

    assert normalize_tcl(raw_tcl) == "+"
    assert evaluate_intent_eligibility(
        EmployeeProfile(tcl=raw_tcl),
        _intent("improvement"),
    ).allowed
    mismatch = evaluate_intent_eligibility(
        EmployeeProfile(tcl=raw_tcl),
        _intent("development"),
    )
    assert mismatch.allowed is True
    assert mismatch.status == "mismatch"


def test_legacy_source_profile_text_is_used_as_a_fallback():
    profile = EmployeeProfile(
        source_profile_text=(
            "Name：示例员工\n"
            "TCL (SLx)：Performance+,Size of Task++,Job Market ++"
        )
    )

    result = evaluate_intent_eligibility(profile, _intent("exit"))

    assert result.allowed is True
    assert result.tcl == "+"


@pytest.mark.parametrize("rating", [None, "", "M-", "unknown", "6"])
def test_missing_or_unrecognizable_values_show_advice_without_blocking(rating: str | None):
    result = evaluate_intent_eligibility(
        EmployeeProfile(performance_rating=rating),
        _intent("development"),
    )

    assert result.allowed is True
    assert result.status == "unknown"
    assert "仍可继续使用当前沟通意图" in (result.message or "")


def test_profile_readiness_accepts_a_recognizable_tcl_instead_of_rating():
    profile = EmployeeProfile(
        employee_alias="示例员工",
        role="工程师",
        tcl="Performance++,Size of Task+,Job Market +",
        review_cycle="2026",
        conversation_topic="绩效沟通",
        key_goals=["完成项目"],
    )

    assert "performance_rating_or_tcl" not in profile.missing_required_fields()
    assert profile.is_ready_for_setup()


def test_employee_import_maps_tcl_to_the_structured_profile_field():
    profile = build_profile(
        {
            "Name": "示例员工",
            "Position": "工程师",
            "TCL (SLx)": "Performance+,Size of Task++,Job Market ++",
        }
    )

    assert profile.tcl == "Performance+,Size of Task++,Job Market ++"


@pytest.mark.asyncio
async def test_confirm_intent_saves_a_nonrecommended_selection():
    state = SessionState(
        session_id="eligibility-confirm",
        employee_profile=EmployeeProfile(performance_rating="2"),
    )
    service = _service(state)

    result = await service.confirm_intent(
        state.session_id,
        intent_id="improvement",
        performance_items=_performance_items(),
    )

    assert result.intent is not None
    assert result.intent.intent_id == "improvement"
    assert result.intent.config is not None
    assert service.session_service.save_count == 1


@pytest.mark.asyncio
async def test_confirm_intent_saves_a_matching_selection():
    state = SessionState(
        session_id="eligibility-confirm-valid",
        employee_profile=EmployeeProfile(performance_rating="4"),
    )
    service = _service(state)

    result = await service.confirm_intent(
        state.session_id,
        intent_id="improvement",
        performance_items=_performance_items(),
    )

    assert result.intent is not None
    assert result.intent.intent_id == "improvement"
    assert result.intent.config is not None
    assert service.session_service.save_count == 1


def test_complete_setup_allows_a_previously_saved_nonrecommended_intent():
    state = SessionState(
        session_id="eligibility-complete",
        employee_profile=EmployeeProfile(
            employee_alias="示例员工",
            role="工程师",
            performance_rating="1",
            review_cycle="2026",
            conversation_topic="绩效沟通",
            key_goals=["完成项目"],
        ),
        intent=IntentResult(intent_id="exit"),
        personality=BigFivePersonality(),
        motivation=MotivationState(
            primary_motive_id="development",
            secondary_motive_ids=["recognition"],
        ),
        emotion_state=EmotionState(),
    )
    service = _service(state)

    result = service.complete_setup(state.session_id)

    assert result.setup_ready is True
    assert result.stage == "setup_ready"
    assert service.session_service.save_count == 1

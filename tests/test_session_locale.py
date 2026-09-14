from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.agents.employee_agent import EmployeeAgent
from backend.agents.coach_agent.coach_orchestrator import CoachOrchestrator
from backend.schemas.guidance import GuidanceReport
from backend.schemas.conversation import ConversationTurn
from backend.schemas.intent import (
    IntentGoalPerformanceItem,
    IntentPerformanceDraftOutput,
    IntentResult,
)
from backend.schemas.locale import (
    locale_from_accept_language,
    normalize_session_locale,
)
from backend.schemas.state import SessionState
from backend.services.coach_service import CoachService
from backend.services.guidance_service import GuidanceService
from backend.services.session_service import SessionService


class _StateRepository:
    def __init__(self, state: SessionState):
        self.state = state

    def get(self, session_id: str) -> SessionState:
        assert session_id == self.state.session_id
        return self.state

    def save(self, state: SessionState) -> SessionState:
        self.state = state
        return state


def test_session_locale_defaults_for_legacy_state_and_is_strict() -> None:
    assert SessionState.model_validate({"session_id": "legacy"}).locale == "zh-CN"
    for locale in ("zh-CN", "en", "de", "ja"):
        assert SessionState(session_id=f"valid-{locale}", locale=locale).locale == locale

    with pytest.raises(ValidationError):
        SessionState(session_id="invalid", locale="en-US")


@pytest.mark.parametrize(
    ("raw_locale", "expected"),
    [
        ("zh-Hans-CN", "zh-CN"),
        ("en-US", "en"),
        ("de-DE", "de"),
        ("ja-JP", "ja"),
        ("jp", "ja"),
    ],
)
def test_browser_locale_aliases_normalize_to_supported_session_locales(
    raw_locale: str,
    expected: str,
) -> None:
    assert normalize_session_locale(raw_locale) == expected
    assert locale_from_accept_language(f"fr-FR;q=1, {raw_locale};q=0.9") == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("de;q=0,en;q=1", "en"),
        ("ja;q=0.1,en;q=0.9", "en"),
        ("de-DE;q=0.8,ja-JP;q=0.8", "de"),
        ("fr-FR;q=1,ja-JP;q=0.7", "ja"),
        ("de;q=invalid,en;q=0.5", "en"),
        ("de;q=0,ja;q=0", "zh-CN"),
    ],
)
def test_accept_language_honors_quality_and_rejects_zero_weight(
    header: str,
    expected: str,
) -> None:
    assert locale_from_accept_language(header) == expected


def test_switching_a_confirmed_chinese_session_keeps_setup_but_invalidates_reports() -> None:
    state = SessionState(
        session_id="locale-switch",
        locale="zh-CN",
        stage="report_ready",
        setup_ready=True,
        guidance_report_id="guidance-zh",
        coach_report_id="coach-zh",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text="我们来复盘。")
        ],
        intent=IntentResult(
            intent_id="development",
            performance_locale="zh-CN",
            performance_context="中文已确认表现",
            performance_items=[
                IntentGoalPerformanceItem(goal="目标达成总览", current_performance="已完成")
            ],
        ),
    )
    repository = _StateRepository(state)
    service = SessionService(
        repo=repository,
        runtime_recycler=SimpleNamespace(),
    )

    updated = service.update_locale(state.session_id, locale="en")

    assert updated.locale == "en"
    assert updated.setup_ready is True
    assert updated.stage == "rehearsal"
    assert updated.guidance_report_id is None
    assert updated.coach_report_id is None
    assert updated.intent is not None
    assert updated.intent.performance_locale == "zh-CN"
    assert updated.intent.performance_items[0].current_performance == "已完成"


def test_intent_performance_binds_english_titles_and_disclaimer() -> None:
    draft = IntentPerformanceDraftOutput(
        goal_overview="All agreed goals were reviewed.",
        positive_performance="Delivery remained stable.",
        performance_gaps="Cross-team lead time still needs improvement.",
    ).bind_sections("en")

    assert draft.locale == "en"
    assert [item.goal for item in draft.goal_performance_items] == [
        "Goal Achievement Overview",
        "Positive Performance / Progress",
        "Current Gaps and Behavioral Examples",
    ]
    assert "simulation based on limited information" in draft.performance_context
    assert "本内容基于有限信息" not in draft.performance_context


@pytest.mark.parametrize(
    ("locale", "expected_titles", "disclaimer_marker"),
    [
        (
            "de",
            [
                "Überblick über die Zielerreichung",
                "Positive Leistung / Fortschritt",
                "Aktuelle Lücken und Verhaltensbeispiele",
            ],
            "Simulation auf Grundlage begrenzter Informationen",
        ),
        (
            "ja",
            ["目標達成の概要", "良好な成果／進捗", "現在の課題と行動事例"],
            "限られた情報に基づくシミュレーション",
        ),
    ],
)
def test_intent_performance_binds_german_and_japanese_sections(
    locale: str,
    expected_titles: list[str],
    disclaimer_marker: str,
) -> None:
    draft = IntentPerformanceDraftOutput(
        goal_overview="Overview",
        positive_performance="Progress",
        performance_gaps="Gap",
    ).bind_sections(locale)

    assert draft.locale == locale
    assert [item.goal for item in draft.goal_performance_items] == expected_titles
    assert disclaimer_marker in draft.performance_context


@pytest.mark.parametrize(
    ("locale", "language_contract"),
    [
        ("zh-CN", "zh-CN 使用自然中文"),
        ("en", "en 使用自然英文"),
        ("de", "de 使用自然德语"),
        ("ja", "ja 使用自然日语"),
    ],
)
def test_employee_reply_prompt_uses_session_locale_without_translating_evidence(
    locale: str,
    language_contract: str,
) -> None:
    state = SessionState(session_id=f"reply-{locale}", locale=locale)

    prompt = EmployeeAgent._build_reply_prompt(
        state,
        "请说说你对本周结果的看法。",
        test_prompt_enabled=False,
    )

    assert f"output_locale={locale}" in prompt
    assert language_contract in prompt
    assert "请说说你对本周结果的看法。" in prompt


def test_coach_checkpoint_fingerprint_changes_with_output_locale() -> None:
    service = CoachService.__new__(CoachService)
    service.settings = SimpleNamespace(
        kb_index_version="kb-test",
        coach_evaluator_model="coach-test",
    )
    service.config_loader = SimpleNamespace(
        personality_behavior_map_hash=lambda: "personality-map-test"
    )
    service._runtime_coach_version = lambda: "coach-version-test"
    chinese = SessionState(session_id="fingerprint", locale="zh-CN")
    english = chinese.model_copy(update={"locale": "en"})

    assert service._input_fingerprint(chinese) != service._input_fingerprint(english)


def test_english_coach_report_localizes_fixed_fields() -> None:
    report = CoachOrchestrator.build_report("coach-en", [], locale="en")

    assert report.locale == "en"
    assert "final judgment" in report.disclaimer
    assert all("Evaluation" in result.task_name for result in report.task_results)
    assert all(
        result.summary.startswith("This dimension evaluation failed")
        for result in report.task_results
    )


def test_guidance_cache_is_not_reused_across_locales() -> None:
    report = GuidanceReport(
        session_id="guidance-cache",
        locale="zh-CN",
        intent_id="development",
        guidance_version="guidance-test",
        culture_version="culture-test",
        purpose="对齐沟通目标。",
        opening_suggestion="明确开场方式。",
        risk_preview=["识别潜在风险。"],
        response_strategies=["核对事实与标准。"],
        safer_phrases=["共同确认下一步。"],
    )
    service = GuidanceService.__new__(GuidanceService)
    service.report_repo = SimpleNamespace(get_guidance=lambda _session_id: report)
    service.config_loader = SimpleNamespace(
        guidance_version=lambda: "guidance-test",
        culture_version=lambda: "culture-test",
    )

    assert service._cached_report(report.session_id, "en") is None

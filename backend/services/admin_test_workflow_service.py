from __future__ import annotations

import uuid

from backend.schemas.admin_test_workflow import AdminTestWorkflowCreateRequest
from backend.schemas.conversation import ConversationTurn
from backend.schemas.intent import IntentConfig, IntentResult
from backend.schemas.locale import SessionLocale, localized_value
from backend.schemas.profile import EmployeeProfile
from backend.schemas.simulation import MotivationState
from backend.schemas.state import SessionState
from backend.services.session_service import SessionService
from backend.services.setup_service import SetupService


class AdminTestWorkflowService:
    """Creates complete synthetic sessions for the administrator test channel."""

    TEST_SESSION_WARNING = "管理员测试会话：员工档案和预置对话均为模拟数据。"
    TEST_SESSION_WARNING_EN = (
        "Administrator test session: the employee profile and seeded "
        "conversation are synthetic."
    )
    TEST_SESSION_WARNING_DE = (
        "Administrator-Testsession: Das Mitarbeiterprofil und der vorbelegte "
        "Gesprächsverlauf sind synthetisch."
    )
    TEST_SESSION_WARNING_JA = (
        "管理者テストセッション：従業員プロフィールと事前設定された対話は架空のデータです。"
    )
    TEST_INTENT_NAMES_EN = {
        "development": "Development",
        "improvement": "Improvement",
        "development_improvement": "Development and Improvement",
        "exit": "Exit",
        "improvement_exit": "Improvement and Exit",
    }
    TEST_INTENT_NAMES_DE = {
        "development": "Entwicklungsfeedback",
        "improvement": "Verbesserungsfeedback",
        "development_improvement": "Entwicklungs- und Verbesserungsfeedback",
        "exit": "Austrittsgespräch",
        "improvement_exit": "Verbesserungs- und Austrittswarnung",
    }
    TEST_INTENT_NAMES_JA = {
        "development": "育成フィードバック",
        "improvement": "改善フィードバック",
        "development_improvement": "育成・改善フィードバック",
        "exit": "退職に関するフィードバック",
        "improvement_exit": "改善・退職警告フィードバック",
    }

    def __init__(
        self,
        *,
        setup_service: SetupService,
        session_service: SessionService,
    ) -> None:
        self.setup_service = setup_service
        self.session_service = session_service

    def create(self, payload: AdminTestWorkflowCreateRequest) -> SessionState:
        intent_config = self.setup_service.loader.intents().get(payload.intent_id)
        if intent_config is None:
            raise ValueError(f"未知沟通意图: {payload.intent_id}")

        motives = self.setup_service.loader.motives()
        motive_ids = [payload.primary_motive_id, *payload.secondary_motive_ids]
        unknown_motives = [
            motive_id for motive_id in motive_ids if motive_id not in motives
        ]
        if unknown_motives:
            raise ValueError(f"未知诉求: {', '.join(unknown_motives)}")

        profile = self._build_test_profile(intent_config, locale=payload.locale)
        source_turns = payload.conversation if payload.destination == "report" else []
        conversation = [
            ConversationTurn(
                turn_index=index,
                speaker=turn.speaker,
                text=turn.text,
                metadata={"source": "admin_test_workflow", "synthetic": True},
            )
            for index, turn in enumerate(source_turns, start=1)
        ]
        personality = payload.personality.model_copy(deep=True)
        state = SessionState(
            session_id=str(uuid.uuid4()),
            locale=payload.locale,
            stage="rehearsal" if conversation else "setup_ready",
            run_mode="rehearsal_report",
            employee_profile=profile,
            intent=IntentResult(
                intent_id=intent_config.id,
                confidence=1.0,
                reason=localized_value(
                    payload.locale,
                    {
                        "zh-CN": "管理员测试通道显式选择",
                        "en": "Explicitly selected in the administrator test workflow",
                        "de": "Im Administrator-Testablauf ausdrücklich ausgewählt",
                        "ja": "管理者テストフローで明示的に選択",
                    },
                ),
                config=intent_config,
                performance_locale=payload.locale,
            ),
            personality=personality,
            motivation=MotivationState(
                primary_motive_id=payload.primary_motive_id,
                secondary_motive_ids=list(payload.secondary_motive_ids),
                primary_score=50.0,
                secondary_scores={
                    motive_id: 50.0 for motive_id in payload.secondary_motive_ids
                },
            ),
            emotion_state=self.setup_service.emotion_transition.initial_state(
                intent_id=intent_config.id,
                personality=personality,
            ),
            setup_ready=True,
            conversation=conversation,
            user_turn_count=sum(turn.speaker == "manager" for turn in conversation),
            warnings=[
                localized_value(
                    payload.locale,
                    {
                        "zh-CN": self.TEST_SESSION_WARNING,
                        "en": self.TEST_SESSION_WARNING_EN,
                        "de": self.TEST_SESSION_WARNING_DE,
                        "ja": self.TEST_SESSION_WARNING_JA,
                    },
                )
            ],
        )
        return self.session_service.save_session(state)

    @staticmethod
    def _build_test_profile(
        intent: IntentConfig,
        *,
        locale: SessionLocale = "zh-CN",
    ) -> EmployeeProfile:
        eligibility = intent.eligibility
        rating = None
        tcl = None
        if eligibility and eligibility.performance_ratings:
            rating = str(eligibility.performance_ratings[0])
        if eligibility and eligibility.tcl_values:
            tcl = str(eligibility.tcl_values[0])
        if not rating and not tcl:
            rating = "3"

        intent_names = localized_value(
            locale,
            {
                "zh-CN": {},
                "en": AdminTestWorkflowService.TEST_INTENT_NAMES_EN,
                "de": AdminTestWorkflowService.TEST_INTENT_NAMES_DE,
                "ja": AdminTestWorkflowService.TEST_INTENT_NAMES_JA,
            },
        )
        intent_name = intent_names.get(intent.id, intent.name)
        copy = localized_value(
            locale,
            {
                "zh-CN": {
                    "employee_alias": "测试员工",
                    "role": "测试岗位",
                    "department": "管理员测试环境",
                    "review_cycle": "管理员测试周期",
                    "goal": "验证多轮预演和四维复盘的完整运行链路",
                    "summary": "仅用于管理员测试，不对应真实员工。",
                    "profile": "管理员测试档案",
                    "intent": "沟通意图",
                    "rating": "绩效评级",
                    "not_set": "未设置",
                },
                "en": {
                    "employee_alias": "Test Employee",
                    "role": "Test Role",
                    "department": "Administrator Test Environment",
                    "review_cycle": "Administrator Test Cycle",
                    "goal": "Validate the complete multi-turn rehearsal and four-dimension review workflow",
                    "summary": "For administrator testing only; this does not represent a real employee.",
                    "profile": "Administrator test profile",
                    "intent": "communication intent",
                    "rating": "performance rating",
                    "not_set": "not set",
                },
                "de": {
                    "employee_alias": "Testmitarbeiter",
                    "role": "Testrolle",
                    "department": "Administrator-Testumgebung",
                    "review_cycle": "Administrator-Testzyklus",
                    "goal": "Den vollständigen Ablauf der mehrstufigen Übung und der vierdimensionalen Auswertung prüfen",
                    "summary": "Nur für Administratortests; dies stellt keine reale beschäftigte Person dar.",
                    "profile": "Administrator-Testprofil",
                    "intent": "Gesprächsabsicht",
                    "rating": "Leistungsbewertung",
                    "not_set": "nicht festgelegt",
                },
                "ja": {
                    "employee_alias": "テスト従業員",
                    "role": "テスト職務",
                    "department": "管理者テスト環境",
                    "review_cycle": "管理者テスト期間",
                    "goal": "複数ターンの演習と4観点の振り返りの一連のフローを検証する",
                    "summary": "管理者テスト専用であり、実在する従業員を表すものではありません。",
                    "profile": "管理者テストプロフィール",
                    "intent": "面談意図",
                    "rating": "評価レーティング",
                    "not_set": "未設定",
                },
            },
        )
        return EmployeeProfile(
            employee_alias=copy["employee_alias"],
            role=copy["role"],
            department=copy["department"],
            performance_rating=rating,
            tcl=tcl,
            review_cycle=copy["review_cycle"],
            conversation_topic=intent_name,
            key_goals=[copy["goal"]],
            employee_status_summary=copy["summary"],
            source_profile_text=(
                f"{copy['profile']}; {copy['intent']}: {intent_name}; "
                f"{copy['rating']}: {rating or copy['not_set']}; "
                f"TCL: {tcl or copy['not_set']}."
            ),
        )

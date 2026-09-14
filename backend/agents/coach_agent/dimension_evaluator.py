from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agents.coach_agent.generic_agent import (
    GenericCoachAgent,
    PreparedCoachInvocation,
)
from backend.exceptions.llm_errors import StructuredOutputError
from backend.business_config.loader import BusinessConfigLoader, get_config_loader
from backend.config.settings import get_settings
from backend.schemas.prompt_context import CoachPromptContext, prompt_context_json
from backend.schemas.locale import localized_value
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.state import SessionState
from backend.schemas.task import CoachTaskResult
from backend.services.career_elements import (
    career_elements_applicable,
    current_career_elements,
)
from backend.services.knowledge_skill_service import (
    build_knowledge_skill_context,
    get_knowledge_skill_router,
)
from backend.services.prompt_service import PromptService


@dataclass(frozen=True)
class CoachDimensionSpec:
    task_id: str
    task_name: str
    config_filename: str
    config_variable: str
    prompt_template: str
    dimension_id: str
    missing_summary: str
    requires_employee: bool = False


@dataclass(frozen=True, slots=True)
class PreparedDimensionEvaluation:
    config: dict[str, Any]
    invocation: PreparedCoachInvocation | None = None
    immediate_result: CoachTaskResult | None = None


COACH_DIMENSION_SPECS: tuple[CoachDimensionSpec, ...] = (
    CoachDimensionSpec(
        task_id="opening_evaluation",
        task_name="开场定调与绩效结果对齐评估",
        config_filename="start.yaml",
        config_variable="opening_dimension_config",
        prompt_template="coach/start.jinja2",
        dimension_id="opening_and_result_framing",
        missing_summary="没有 manager 开场对话，不能评估开场定调与结果对齐。",
    ),
    CoachDimensionSpec(
        task_id="emotion_evaluation",
        task_name="情绪承接评估",
        config_filename="emotion.yaml",
        config_variable="emotion_dimension_config",
        prompt_template="coach/emotion.jinja2",
        dimension_id="emotion_reception_and_empathy",
        missing_summary="缺少双方对话，不能判断情绪承接。",
        requires_employee=True,
    ),
    CoachDimensionSpec(
        task_id="output_expectations_evaluation",
        task_name="从情绪回归产出与标准评估",
        config_filename="requirement.yaml",
        config_variable="output_dimension_config",
        prompt_template="coach/requirement.jinja2",
        dimension_id="return_to_output_expectations",
        missing_summary="没有 manager 对话证据，不能评估产出与标准说明。",
    ),
    CoachDimensionSpec(
        task_id="development_plan_evaluation",
        task_name="总结与差异化发展计划评估",
        config_filename="plan.yaml",
        config_variable="plan_dimension_config",
        prompt_template="coach/plan.jinja2",
        dimension_id="summary_and_intent_specific_development_plan",
        missing_summary="没有 manager 对话证据，不能评估总结与发展计划。",
    ),
)

COACH_TASK_NAMES_EN: dict[str, str] = {
    "opening_evaluation": "Opening and Performance Result Alignment Evaluation",
    "emotion_evaluation": "Emotion Reception Evaluation",
    "output_expectations_evaluation": "Outputs and Standards Evaluation",
    "development_plan_evaluation": "Summary and Differentiated Development Plan Evaluation",
}

COACH_MISSING_SUMMARIES_EN: dict[str, str] = {
    "opening_evaluation": (
        "There is no Manager opening dialogue, so opening and result alignment "
        "cannot be evaluated."
    ),
    "emotion_evaluation": (
        "Dialogue from both parties is missing, so emotion reception cannot be evaluated."
    ),
    "output_expectations_evaluation": (
        "There is no Manager dialogue evidence, so outputs and standards cannot be evaluated."
    ),
    "development_plan_evaluation": (
        "There is no Manager dialogue evidence, so the summary and development plan cannot be evaluated."
    ),
}
COACH_TASK_NAMES_DE: dict[str, str] = {
    "opening_evaluation": "Bewertung von Gesprächseinstieg und Leistungsergebnis",
    "emotion_evaluation": "Bewertung der emotionalen Aufnahme",
    "output_expectations_evaluation": "Bewertung von Ergebnissen und Standards",
    "development_plan_evaluation": "Bewertung von Zusammenfassung und differenziertem Entwicklungsplan",
}
COACH_TASK_NAMES_JA: dict[str, str] = {
    "opening_evaluation": "導入と評価結果のすり合わせの評価",
    "emotion_evaluation": "感情の受け止めの評価",
    "output_expectations_evaluation": "成果と基準の評価",
    "development_plan_evaluation": "まとめと個別化した育成計画の評価",
}
COACH_MISSING_SUMMARIES_DE: dict[str, str] = {
    "opening_evaluation": (
        "Es fehlt ein eröffnender Dialog der Führungskraft; Gesprächseinstieg und "
        "Ergebnisabstimmung können daher nicht bewertet werden."
    ),
    "emotion_evaluation": (
        "Es fehlt ein Dialog beider Seiten; die emotionale Aufnahme kann daher nicht "
        "bewertet werden."
    ),
    "output_expectations_evaluation": (
        "Es fehlen Dialogbelege der Führungskraft; Ergebnisse und Standards können "
        "daher nicht bewertet werden."
    ),
    "development_plan_evaluation": (
        "Es fehlen Dialogbelege der Führungskraft; Zusammenfassung und Entwicklungsplan "
        "können daher nicht bewertet werden."
    ),
}
COACH_MISSING_SUMMARIES_JA: dict[str, str] = {
    "opening_evaluation": "管理職による導入の発言がないため、導入と評価結果のすり合わせを評価できません。",
    "emotion_evaluation": "双方の対話がないため、感情の受け止めを評価できません。",
    "output_expectations_evaluation": "管理職の発言証拠がないため、成果と基準の説明を評価できません。",
    "development_plan_evaluation": "管理職の発言証拠がないため、まとめと育成計画を評価できません。",
}

_COACH_TASK_NAMES_BY_LOCALE = {
    "zh-CN": {spec.task_id: spec.task_name for spec in COACH_DIMENSION_SPECS},
    "en": COACH_TASK_NAMES_EN,
    "de": COACH_TASK_NAMES_DE,
    "ja": COACH_TASK_NAMES_JA,
}
_COACH_MISSING_SUMMARIES_BY_LOCALE = {
    "zh-CN": {spec.task_id: spec.missing_summary for spec in COACH_DIMENSION_SPECS},
    "en": COACH_MISSING_SUMMARIES_EN,
    "de": COACH_MISSING_SUMMARIES_DE,
    "ja": COACH_MISSING_SUMMARIES_JA,
}


def coach_task_name(task_id: str, locale: str, fallback: str = "") -> str:
    return localized_value(locale, _COACH_TASK_NAMES_BY_LOCALE).get(
        task_id,
        fallback or task_id,
    )


def validate_coach_runtime(
    config_loader: BusinessConfigLoader | None = None,
    prompt_service: PromptService | None = None,
) -> str:
    loader = config_loader or get_config_loader()
    prompts = prompt_service or PromptService()
    seen_dimension_ids: set[str] = set()

    for spec in COACH_DIMENSION_SPECS:
        config = loader.coach_config(spec.config_filename)
        version = str(config.get("version") or "").strip()
        dimension = config.get("dimension")
        if not version or not isinstance(dimension, dict):
            raise ValueError(
                f"Coach config {spec.config_filename} requires version and dimension."
            )
        dimension_id = str(dimension.get("id") or "").strip()
        dimension_name = str(dimension.get("name") or "").strip()
        if dimension_id != spec.dimension_id or not dimension_name:
            raise ValueError(
                f"Coach config {spec.config_filename} dimension mismatch: "
                f"expected={spec.dimension_id}, actual={dimension_id or None}"
            )
        if dimension_id in seen_dimension_ids:
            raise ValueError(f"Duplicate Coach dimension id: {dimension_id}")
        seen_dimension_ids.add(dimension_id)

        score_scale = dimension.get("score_scale")
        if not isinstance(score_scale, dict):
            raise ValueError(f"Coach config {spec.config_filename} requires score_scale.")
        try:
            score_keys = {int(key) for key in score_scale}
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Coach config {spec.config_filename} has invalid score_scale keys."
            ) from exc
        if score_keys != {1, 2, 3, 4, 5}:
            raise ValueError(
                f"Coach config {spec.config_filename} score_scale must define 1 through 5."
            )
        prompts.render(
            spec.prompt_template,
            context_json=prompt_context_json(
                CoachPromptContext(dimension_config=config)
            ),
        )

    return loader.coach_version()


class DimensionEvaluator(GenericCoachAgent):
    def __init__(
        self,
        spec: CoachDimensionSpec,
        llm_service: Any | None = None,
        prompt_service: PromptService | None = None,
    ):
        super().__init__(llm_service=llm_service, prompt_service=prompt_service)
        self.spec = spec

    async def evaluate(
        self,
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk] | None = None,
        *,
        retry: bool = False,
        retry_model: str | None = None,
        prepared: PreparedDimensionEvaluation | None = None,
    ) -> CoachTaskResult:
        prepared_evaluation = prepared or self.prepare_evaluation(
            state,
            retrieved_chunks,
        )
        if prepared_evaluation.immediate_result is not None:
            return prepared_evaluation.immediate_result
        if prepared_evaluation.invocation is None:
            raise RuntimeError("Coach evaluation preparation did not produce an invocation.")

        result = await self.run_llm_task(
            task_id=self.spec.task_id,
            task_name=coach_task_name(
                self.spec.task_id,
                state.locale,
                self.spec.task_name,
            ),
            prompt_template=self.spec.prompt_template,
            task_model_name="coach_evaluator",
            retry=retry,
            retry_model=retry_model,
            prepared=prepared_evaluation.invocation,
        )
        try:
            return self._validate_result(result, prepared_evaluation.config)
        except ValueError as exc:
            raise StructuredOutputError("business_validation", str(exc)) from exc

    def prepare_evaluation(
        self,
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk] | None = None,
    ) -> PreparedDimensionEvaluation:
        if not self.manager_turns(state.conversation) or (
            self.spec.requires_employee and not self.employee_turns(state.conversation)
        ):
            return PreparedDimensionEvaluation(
                config={},
                immediate_result=CoachTaskResult(
                    task_id=self.spec.task_id,
                    task_name=coach_task_name(
                        self.spec.task_id,
                        state.locale,
                        self.spec.task_name,
                    ),
                    status="insufficient_information",
                    score=None,
                    summary=localized_value(
                        state.locale,
                        _COACH_MISSING_SUMMARIES_BY_LOCALE,
                    )[self.spec.task_id],
                ),
            )

        config = get_config_loader().coach_config(self.spec.config_filename)
        settings = get_settings()
        skill_router = get_knowledge_skill_router()
        active_skills = skill_router.select(
            self.spec.task_id,
            build_knowledge_skill_context(state, include_conversation=True),
            emit_metrics=False,
        )
        conversation = self.conversation_payload(
            state.conversation,
            max_text_chars=settings.coach_conversation_context_max_chars,
        )
        chunks = self.chunks_payload(
            retrieved_chunks,
            max_text_chars=settings.coach_rag_context_max_chars,
        )
        prompt_context = CoachPromptContext.model_validate(
            {
                "output_locale": state.locale,
                "profile": (
                    state.employee_profile.model_dump(exclude_none=True)
                    if state.employee_profile
                    else {}
                ),
                "supplemental_info": state.supplemental_info_excerpt(),
                "performance_context": (
                    state.intent.performance_context if state.intent else ""
                )
                or "",
                "conversation": conversation,
                "intent_id": state.intent.intent_id if state.intent else "",
                "intent_config": state.intent.config if state.intent else None,
                "retrieved_chunks": chunks,
                "active_skills": skill_router.prompt_payload(active_skills),
                "knowledge_base_folders": sorted(
                    {str(chunk.get("scope") or "") for chunk in chunks}
                ),
                "dimension_config": config,
                "career_elements_applicable": career_elements_applicable(
                    state.employee_profile,
                    state.intent.intent_id if state.intent else "",
                ),
                "current_career_elements": current_career_elements(
                    state.employee_profile
                ),
            }
        )
        invocation = self.prepare_llm_task(
            prompt_template=self.spec.prompt_template,
            prompt_context=prompt_context,
            evidence_conversation=state.conversation,
            citation_chunks=list(retrieved_chunks or []),
        )
        return PreparedDimensionEvaluation(config=config, invocation=invocation)

    def _validate_result(
        self,
        result: CoachTaskResult,
        _config: dict[str, Any],
    ) -> CoachTaskResult:
        if (
            result.status == "success"
            and result.score in {1, 2, 3}
            and not result.improvement_points
        ):
            raise ValueError(
                f"Coach task {self.spec.task_id} with score={result.score} must "
                "return at least one Manager-evidence-backed issue."
            )
        return result

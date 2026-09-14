from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from backend.exceptions.llm_errors import StructuredOutputError
from backend.observability.metrics import log_metric
from backend.rag.citation import (
    merge_citations,
    referenced_chunks_to_citations,
    validated_references_to_citations,
)
from backend.rag.parent_context import (
    generation_context_deduplication_key,
    generation_context_text,
)
from backend.schemas.conversation import ConversationTurn
from backend.schemas.locale import localized_value
from backend.schemas.prompt_context import CoachPromptContext, prompt_context_json
from backend.schemas.profile import EmployeeProfile
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.task import (
    CoachDimensionModelOutput,
    CoachTaskResult,
    EvidenceRef,
)
from backend.services.career_elements import (
    career_elements_applicable,
    current_career_elements,
)
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.prompt_service import PromptService


FIXED_DIAGNOSTIC_DIMENSION_IDS: dict[str, tuple[str, ...]] = {
    "opening_evaluation": (
        "communication_tone",
        "performance_result_alignment",
    ),
    "emotion_evaluation": (
        "listening_and_emotion_understanding",
        "empathic_summary",
        "joint_exploration",
    ),
    "output_expectations_evaluation": (
        "goal_direction_correctness",
        "result_breakthrough",
        "high_performance_culture",
    ),
    "development_plan_evaluation": (
        "job_level_requirements",
        "career_elements",
    ),
}
COACH_DIMENSION_NAMES_EN: dict[str, str] = {
    "opening_evaluation": "Opening and Performance Result Alignment",
    "emotion_evaluation": "Emotion Reception, Acceptance, and Empathy",
    "output_expectations_evaluation": "Outputs and Standards",
    "development_plan_evaluation": "Summary and Differentiated Development Plan",
}
COACH_SCORE_LEVELS_EN: dict[str, dict[int, str]] = {
    "opening_evaluation": {
        5: "Clear, direct, and confirms understanding",
        4: "The outcome is clear, with minor gaps in expression or confirmation",
        3: "The outcome is mentioned, but its relationship to expectations is not specific enough",
        2: "The relationship between the outcome and expectations is not clearly explained",
        1: "Opening tone-setting is absent or seriously inappropriate",
    },
    "emotion_evaluation": {
        5: "Listens deeply and acknowledges emotion effectively",
        4: "Correctly identifies and responds to emotion",
        3: "Notices emotion, but the response remains superficial",
        2: "Ignores, interrupts, or mishandles emotion",
        1: "Invalidates, shames, or confronts the employee's emotion",
    },
    "output_expectations_evaluation": {
        5: "Comprehensive and precise evaluation across all three dimensions",
        4: "Facts and standards are clear, but one dimension lacks depth or evidence",
        3: "Outcomes are discussed, but dimension coverage or the evaluation basis is incomplete",
        2: "The evaluation is subjective and vague and lacks standards",
        1: "The evaluation is not grounded in facts and standards",
    },
    "development_plan_evaluation": {
        5: "Accurate summary with a differentiated, executable plan",
        4: "The summary and plan are clear, but targeting or trackability is slightly weak",
        3: "A summary and plan are present, but they are broad",
        2: "The summary is rushed and the plan is merely formalistic",
        1: "There is no summary or concrete plan",
    },
}
COACH_DIMENSION_NAMES_DE: dict[str, str] = {
    "opening_evaluation": "Gesprächseinstieg und Abstimmung des Leistungsergebnisses",
    "emotion_evaluation": "Emotionale Aufnahme, Akzeptanz und Empathie",
    "output_expectations_evaluation": "Ergebnisse und Standards",
    "development_plan_evaluation": "Zusammenfassung und differenzierter Entwicklungsplan",
}
COACH_DIMENSION_NAMES_JA: dict[str, str] = {
    "opening_evaluation": "導入と評価結果のすり合わせ",
    "emotion_evaluation": "感情の受け止め、受容と共感",
    "output_expectations_evaluation": "成果と基準",
    "development_plan_evaluation": "まとめと個別化した育成計画",
}
COACH_SCORE_LEVELS_DE: dict[str, dict[int, str]] = {
    "opening_evaluation": {
        5: "Klar, direkt und mit Verständnissicherung",
        4: "Das Ergebnis ist klar; Ausdruck oder Verständnissicherung weisen kleinere Lücken auf",
        3: "Das Ergebnis wird genannt, aber der Bezug zu den Erwartungen bleibt zu ungenau",
        2: "Der Zusammenhang zwischen Ergebnis und Erwartungen wird nicht klar erläutert",
        1: "Die Rahmensetzung zu Beginn fehlt oder ist deutlich unangemessen",
    },
    "emotion_evaluation": {
        5: "Hört aufmerksam zu und nimmt Emotionen wirksam auf",
        4: "Erkennt Emotionen richtig und reagiert angemessen darauf",
        3: "Nimmt Emotionen wahr, reagiert jedoch nur oberflächlich",
        2: "Ignoriert, unterbricht oder behandelt Emotionen unangemessen",
        1: "Wertet Emotionen ab, beschämt die Person oder reagiert konfrontativ",
    },
    "output_expectations_evaluation": {
        5: "Umfassende und präzise Bewertung in allen drei Dimensionen",
        4: "Fakten und Standards sind klar, aber einer Dimension fehlen Tiefe oder Belege",
        3: "Ergebnisse werden besprochen, aber Dimensionsabdeckung oder Bewertungsgrundlage sind unvollständig",
        2: "Die Bewertung ist subjektiv und vage und enthält keine klaren Standards",
        1: "Die Bewertung stützt sich nicht auf Fakten und Standards",
    },
    "development_plan_evaluation": {
        5: "Präzise Zusammenfassung mit einem differenzierten, umsetzbaren Plan",
        4: "Zusammenfassung und Plan sind klar, aber Ausrichtung oder Nachverfolgbarkeit sind leicht eingeschränkt",
        3: "Zusammenfassung und Plan sind vorhanden, bleiben jedoch allgemein",
        2: "Die Zusammenfassung ist übereilt und der Plan bleibt rein formal",
        1: "Es gibt weder eine Zusammenfassung noch einen konkreten Plan",
    },
}
COACH_SCORE_LEVELS_JA: dict[str, dict[int, str]] = {
    "opening_evaluation": {
        5: "明確かつ率直で、理解も確認している",
        4: "結果は明確だが、表現または理解確認に小さな不足がある",
        3: "結果には触れているが、期待との関係が十分具体的でない",
        2: "結果と期待の関係を明確に説明していない",
        1: "導入での方向づけがない、または著しく不適切である",
    },
    "emotion_evaluation": {
        5: "深く耳を傾け、感情を効果的に受け止めている",
        4: "感情を正しく捉え、適切に応答している",
        3: "感情には気づいているが、応答が表面的である",
        2: "感情を無視する、中断する、または不適切に扱っている",
        1: "感情を否定する、恥をかかせる、または対立的に応答している",
    },
    "output_expectations_evaluation": {
        5: "3つの観点すべてを包括的かつ正確に評価している",
        4: "事実と基準は明確だが、1つの観点で深さまたは根拠が不足している",
        3: "成果には触れているが、観点の網羅性または評価根拠が不十分である",
        2: "評価が主観的かつ曖昧で、基準が不足している",
        1: "事実と基準に基づいて評価していない",
    },
    "development_plan_evaluation": {
        5: "正確なまとめと、個別化された実行可能な計画がある",
        4: "まとめと計画は明確だが、対象の明確さまたは追跡可能性がやや弱い",
        3: "まとめと計画はあるが、内容が広範で具体性に欠ける",
        2: "まとめが性急で、計画が形式的なものにとどまる",
        1: "まとめも具体的な計画もない",
    },
}

_COACH_DIMENSION_NAMES_BY_LOCALE = {
    "zh-CN": {},
    "en": COACH_DIMENSION_NAMES_EN,
    "de": COACH_DIMENSION_NAMES_DE,
    "ja": COACH_DIMENSION_NAMES_JA,
}
_COACH_SCORE_LEVELS_BY_LOCALE = {
    "zh-CN": {},
    "en": COACH_SCORE_LEVELS_EN,
    "de": COACH_SCORE_LEVELS_DE,
    "ja": COACH_SCORE_LEVELS_JA,
}
_IMPACT_LABELS = {
    "zh-CN": "影响",
    "en": "Impact",
    "de": "Auswirkung",
    "ja": "影響",
}

MIN_NORMALIZED_EVIDENCE_QUOTE_CHARS = 10
IGNORABLE_EVIDENCE_PUNCTUATION = frozenset(
    "，。！？；：、,.!?;:\"'“”‘’（）()[]【】{}《》〈〉…·"
)
ASR_FILLER_RUN_RE = re.compile(r"[嗯呃额啊]+")


@dataclass(frozen=True, slots=True)
class PreparedCoachInvocation:
    context: CoachPromptContext
    prompt: str
    retry_prompt: str
    evidence_conversation: tuple[ConversationTurn, ...]
    citation_chunks: tuple[RetrievedChunk, ...]


class GenericCoachAgent:
    def __init__(
        self,
        llm_service: LangChainLLMService | None = None,
        prompt_service: PromptService | None = None,
    ):
        self.llm = llm_service or LangChainLLMService()
        self.prompts = prompt_service or PromptService()

    def manager_turns(self, conversation: list[ConversationTurn]) -> list[ConversationTurn]:
        return [turn for turn in conversation if turn.speaker == "manager"]

    def employee_turns(self, conversation: list[ConversationTurn]) -> list[ConversationTurn]:
        return [turn for turn in conversation if turn.speaker == "employee"]

    @staticmethod
    def _normalize_model_payload(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        """Drop fields that cannot apply after the model chooses insufficient data."""

        normalized = dict(payload)
        repair_steps: list[str] = []
        status = str(normalized.get("status") or "").strip()
        if status == "success" and normalized.get("score") is None:
            normalized["status"] = "insufficient_information"
            status = "insufficient_information"
            repair_steps.append("downgraded_scoreless_success_to_insufficient_information")

        if status != "insufficient_information":
            return normalized, repair_steps

        if normalized.get("score") is not None:
            normalized["score"] = None
            repair_steps.append("cleared_insufficient_information_score")
        if normalized.get("strengths") not in (None, []):
            repair_steps.append("cleared_insufficient_information_strengths")
        normalized["strengths"] = []
        if normalized.get("issues") not in (None, []):
            repair_steps.append("cleared_insufficient_information_issues")
        normalized["issues"] = []
        if normalized.get("career_elements_advice") not in (None, []):
            repair_steps.append(
                "cleared_insufficient_information_career_elements_advice"
            )
        normalized["career_elements_advice"] = []
        return normalized, repair_steps

    def conversation_payload(
        self,
        conversation: list[ConversationTurn],
        *,
        max_text_chars: int | None = None,
    ) -> list[dict[str, Any]]:
        payload = [
            turn.model_dump(
                include={"turn_index", "speaker", "text"},
                exclude_none=True,
            )
            for turn in conversation
        ]
        return self._fit_text_budget(payload, max_text_chars=max_text_chars)

    async def run_llm_task(
        self,
        *,
        task_id: str,
        task_name: str,
        prompt_template: str,
        task_model_name: str,
        prompt_context: CoachPromptContext | None = None,
        evidence_conversation: list[ConversationTurn] | None = None,
        retry: bool = False,
        retry_model: str | None = None,
        prepared: PreparedCoachInvocation | None = None,
        **prompt_vars: Any,
    ) -> CoachTaskResult:
        prepared_invocation = prepared or self.prepare_llm_task(
            prompt_template=prompt_template,
            prompt_context=prompt_context,
            evidence_conversation=evidence_conversation,
            **prompt_vars,
        )
        prompt = (
            prepared_invocation.retry_prompt
            if retry
            else prepared_invocation.prompt
        )

        result = await self.llm.ainvoke_structured_single(
            prompt=prompt,
            schema=CoachDimensionModelOutput,
            task_name=task_model_name,
            model=retry_model if retry else None,
            temperature=0.0 if retry else None,
            enable_thinking=False if retry else None,
            payload_normalizer=self._normalize_model_payload,
            structured_transport="json_schema",
            json_schema_strict=False,
        )

        return self._finalize_task_result(
            result,
            task_id=task_id,
            task_name=task_name,
            context=prepared_invocation.context,
            citation_chunks=list(prepared_invocation.citation_chunks),
            evidence_conversation=list(prepared_invocation.evidence_conversation),
        )

    def prepare_llm_task(
        self,
        *,
        prompt_template: str,
        prompt_context: CoachPromptContext | None = None,
        evidence_conversation: list[ConversationTurn] | None = None,
        citation_chunks: list[RetrievedChunk] | None = None,
        **prompt_vars: Any,
    ) -> PreparedCoachInvocation:
        context = prompt_context or self._context_from_prompt_vars(prompt_vars)
        prompt = self.prompts.render(
            prompt_template,
            context_json=prompt_context_json(context),
        )
        retry_prompt = (
            f"{prompt}\n\n"
            "上一次输出因结构格式、业务校验、超时或临时网关错误被拒绝。"
            "请重新核对评分、证据轮次和 Manager 原话，并完整匹配响应 Schema。"
        )
        evidence = evidence_conversation or [
            ConversationTurn.model_validate(turn.model_dump(mode="python"))
            for turn in context.conversation
        ]
        source_chunks = citation_chunks or [
            RetrievedChunk.model_validate(chunk.model_dump(mode="python"))
            for chunk in context.retrieved_chunks
        ]
        return PreparedCoachInvocation(
            context=context,
            prompt=prompt,
            retry_prompt=retry_prompt,
            evidence_conversation=tuple(evidence),
            citation_chunks=tuple(source_chunks),
        )

    @staticmethod
    def _context_from_prompt_vars(prompt_vars: dict[str, Any]) -> CoachPromptContext:
        dimension_config: dict[str, Any] = {}
        for key, value in prompt_vars.items():
            if key.endswith("_dimension_config") and isinstance(value, dict):
                dimension_config = value
                break

        retrieved_chunks = prompt_vars.get("retrieved_chunks") or []
        folders = sorted(
            {
                str(chunk.get("scope") or "").strip()
                for chunk in retrieved_chunks
                if isinstance(chunk, dict)
                and str(chunk.get("scope") or "").strip()
            }
        )
        intent_config = prompt_vars.get("intent") or None
        intent_id = str(prompt_vars.get("selected_intent_id") or "").strip()
        if not intent_id and isinstance(intent_config, dict):
            intent_id = str(
                intent_config.get("intent_id")
                or intent_config.get("id")
                or ""
            ).strip()

        profile = prompt_vars.get("profile") or {}
        parsed_profile = None
        try:
            parsed_profile = EmployeeProfile.model_validate(profile)
        except (TypeError, ValueError):
            pass

        return CoachPromptContext.model_validate(
            {
                "output_locale": prompt_vars.get("output_locale") or "zh-CN",
                "profile": profile,
                "supplemental_info": prompt_vars.get("supplemental_info") or "",
                "performance_context": prompt_vars.get("performance_context") or "",
                "conversation": prompt_vars.get("conversation") or [],
                "intent_id": intent_id,
                "intent_config": intent_config,
                "personality_behavior_profile": prompt_vars.get(
                    "personality_behavior_profile"
                ),
                "retrieved_chunks": retrieved_chunks,
                "active_skills": prompt_vars.get("active_skills") or [],
                "knowledge_base_folders": folders,
                "dimension_config": dimension_config,
                "career_elements_applicable": career_elements_applicable(
                    parsed_profile,
                    intent_id,
                ),
                "current_career_elements": current_career_elements(
                    parsed_profile
                ),
            }
        )

    @staticmethod
    def _filter_inapplicable_career_elements_advice(
        result: CoachDimensionModelOutput,
        *,
        task_id: str,
        context: CoachPromptContext,
        citation_chunks: list[RetrievedChunk],
    ) -> CoachDimensionModelOutput:
        """Keep applicable advice; citation validity only controls citation output."""

        provided_advice = result.career_elements_advice
        if not provided_advice:
            return result

        has_career_source = any(
            chunk.scope == "career" for chunk in citation_chunks
        )
        advice_allowed = bool(
            task_id == "development_plan_evaluation"
            and context.career_elements_applicable
            and result.status == "success"
            and has_career_source
        )
        if advice_allowed:
            return result
        log_metric(
            "coach.career_elements_advice_filtered",
            task_id=task_id,
            provided_count=len(provided_advice),
            retained_count=0,
            rejected_count=len(provided_advice),
            rejection_reason="not_applicable_or_missing_career_source",
        )
        return result.model_copy(update={"career_elements_advice": []})

    @classmethod
    def _finalize_task_result(
        cls,
        result: CoachDimensionModelOutput,
        *,
        task_id: str,
        task_name: str,
        context: CoachPromptContext,
        citation_chunks: list[RetrievedChunk],
        evidence_conversation: list[ConversationTurn] | None,
    ) -> CoachTaskResult:
        result = cls._filter_inapplicable_career_elements_advice(
            result,
            task_id=task_id,
            context=context,
            citation_chunks=citation_chunks,
        )
        conversation = evidence_conversation or [
            ConversationTurn.model_validate(turn.model_dump(mode="python"))
            for turn in context.conversation
        ]
        try:
            result = cls._rebind_issue_quotes(result, conversation)
            result = cls._filter_issue_diagnostic_dimensions(
                result,
                task_id=task_id,
                context=context,
            )
            result = cls._order_issues_for_display(result, task_id=task_id)
            evidence = cls._validated_issue_evidence(result, conversation)
            citation_targets = cls._validated_knowledge_targets(
                result, citation_chunks
            )
        except ValueError as exc:
            raise StructuredOutputError("business_validation", str(exc)) from exc

        dimension = context.dimension_config.get("dimension")
        dimension_scores: list[dict[str, Any]] = []
        if isinstance(dimension, dict):
            scale = dimension.get("score_scale")
            scale_item: dict[str, Any] = {}
            if isinstance(scale, dict) and result.score is not None:
                candidate = scale.get(result.score) or scale.get(str(result.score))
                if isinstance(candidate, dict):
                    scale_item = candidate
            dimension_scores = [
                {
                    "id": str(dimension.get("id") or "").strip(),
                    "name": localized_value(
                        context.output_locale,
                        _COACH_DIMENSION_NAMES_BY_LOCALE,
                    ).get(task_id)
                    or str(dimension.get("name") or "").strip(),
                    "score": result.score,
                    "level": localized_value(
                        context.output_locale,
                        _COACH_SCORE_LEVELS_BY_LOCALE,
                    ).get(task_id, {}).get(result.score)
                    or str(scale_item.get("label") or "").strip()
                    or None,
                    "basis": result.basis,
                    "evidence": [item.model_dump(mode="python") for item in evidence],
                }
            ]

        impact_label = localized_value(context.output_locale, _IMPACT_LABELS)
        improvement_points = []
        for issue in result.issues:
            problem = issue.problem.rstrip("。！？；;.!?")
            impact = issue.impact.rstrip("。！？；;.!?")
            if context.output_locale in {"zh-CN", "ja"}:
                improvement_points.append(
                    f"{problem}（{impact_label}：{impact}）"
                )
            else:
                improvement_points.append(
                    f"{problem} ({impact_label}: {impact})"
                )
        better_phrases = [
            {
                "diagnostic_dimension_id": issue.diagnostic_dimension_id,
                "original": issue.quote,
                "suggestion": issue.suggestion,
                "reason": issue.reason,
            }
            for issue in result.issues
        ]
        career_elements_advice = [
            {
                "element": advice.element,
                "suggestion": advice.suggestion,
                "reason": advice.reason,
            }
            for advice in result.career_elements_advice
        ]
        references_by_target = {
            "summary": (result.summary, result.summary_citation_refs),
            "dimension_scores.0.basis": (
                result.basis,
                result.basis_citation_refs,
            ),
        }
        for issue_index, issue in enumerate(result.issues):
            improvement_target = f"improvement_points.{issue_index}"
            references_by_target[improvement_target] = (
                improvement_points[issue_index],
                [
                    *issue.problem_citation_refs,
                    *issue.impact_citation_refs,
                ],
            )
            references_by_target[
                f"better_phrases.{issue_index}.suggestion"
            ] = (
                issue.suggestion,
                issue.suggestion_citation_refs,
            )
            references_by_target[f"better_phrases.{issue_index}.reason"] = (
                issue.reason,
                issue.reason_citation_refs,
            )
        for advice_index, advice in enumerate(result.career_elements_advice):
            target_prefix = f"career_elements_advice.{advice_index}"
            references_by_target[f"{target_prefix}.element"] = (
                advice.element,
                advice.element_citation_refs,
            )
            references_by_target[f"{target_prefix}.suggestion"] = (
                advice.suggestion,
                advice.suggestion_citation_refs,
            )
            references_by_target[f"{target_prefix}.reason"] = (
                advice.reason,
                advice.reason_citation_refs,
            )
        citations = merge_citations(
            [
                *validated_references_to_citations(
                    citation_chunks,
                    [
                        skill.model_dump(mode="python")
                        for skill in context.active_skills
                    ],
                    references_by_target,
                ),
                *referenced_chunks_to_citations(
                    citation_chunks,
                    citation_targets,
                ),
            ]
        )
        return CoachTaskResult(
            task_id=task_id,
            task_name=task_name,
            status=result.status,
            score=result.score,
            summary=result.summary,
            strengths=result.strengths,
            dimension_scores=dimension_scores,
            evidence=evidence,
            improvement_points=improvement_points,
            better_phrases=better_phrases,
            career_elements_advice=career_elements_advice,
            citations=[
                citation.model_dump(exclude_none=True) for citation in citations
            ],
        )

    @staticmethod
    def _filter_issue_diagnostic_dimensions(
        result: CoachDimensionModelOutput,
        *,
        task_id: str,
        context: CoachPromptContext,
    ) -> CoachDimensionModelOutput:
        """Discard only issues that cannot belong to this task; never reject it."""

        expected_ids = FIXED_DIAGNOSTIC_DIMENSION_IDS.get(task_id)
        if not result.issues:
            return result
        if expected_ids is None:
            return result

        applicable_ids = list(expected_ids)
        if (
            task_id == "development_plan_evaluation"
            and not context.career_elements_applicable
        ):
            applicable_ids = [
                dimension_id
                for dimension_id in applicable_ids
                if dimension_id != "career_elements"
            ]

        fallback_seen = False
        retained_issues = []
        rejection_counts = {
            "missing_dimension": 0,
            "unknown_or_inapplicable_dimension": 0,
            "extra_fallback": 0,
        }
        for issue in result.issues:
            dimension_id = issue.diagnostic_dimension_id
            if not dimension_id:
                if task_id == "emotion_evaluation":
                    if fallback_seen:
                        rejection_counts["extra_fallback"] += 1
                        continue
                    fallback_seen = True
                    retained_issues.append(issue)
                    continue
                rejection_counts["missing_dimension"] += 1
                continue
            if dimension_id not in applicable_ids:
                rejection_counts["unknown_or_inapplicable_dimension"] += 1
                continue
            retained_issues.append(issue)

        if len(retained_issues) == len(result.issues):
            return result
        log_metric(
            "coach.issues_filtered",
            task_id=task_id,
            provided_count=len(result.issues),
            retained_count=len(retained_issues),
            rejected_missing_dimension=rejection_counts["missing_dimension"],
            rejected_unknown_or_inapplicable_dimension=rejection_counts[
                "unknown_or_inapplicable_dimension"
            ],
            rejected_extra_fallback=rejection_counts["extra_fallback"],
        )
        return result.model_copy(update={"issues": retained_issues})

    @staticmethod
    def _order_issues_for_display(
        result: CoachDimensionModelOutput,
        *,
        task_id: str,
    ) -> CoachDimensionModelOutput:
        """Apply stable presentation ordering without rejecting valid content."""

        expected_ids = FIXED_DIAGNOSTIC_DIMENSION_IDS.get(task_id)
        if expected_ids is None or len(result.issues) < 2:
            return result

        positions = {
            dimension_id: position
            for position, dimension_id in enumerate(expected_ids)
        }
        ordered_issues = sorted(
            result.issues,
            key=lambda issue: positions.get(
                issue.diagnostic_dimension_id,
                len(expected_ids),
            ),
        )
        if ordered_issues == result.issues:
            return result
        log_metric(
            "coach.issues_reordered_for_display",
            task_id=task_id,
            issue_count=len(ordered_issues),
        )
        return result.model_copy(update={"issues": ordered_issues})

    @staticmethod
    def _validated_knowledge_targets(
        result: CoachDimensionModelOutput,
        chunks: list[RetrievedChunk],
    ) -> dict[str, list[str]]:
        available_ids = {chunk.chunk_id for chunk in chunks}
        targets_by_chunk: dict[str, list[str]] = {}

        rejected_count = 0

        def add_targets(chunk_ids: list[str], *targets: str) -> None:
            nonlocal rejected_count
            for chunk_id in chunk_ids:
                if chunk_id not in available_ids:
                    rejected_count += 1
                    continue
                targets_by_chunk.setdefault(chunk_id, []).extend(targets)

        add_targets(result.summary_knowledge_chunk_ids, "summary")
        add_targets(
            result.basis_knowledge_chunk_ids,
            "dimension_scores.0.basis",
        )
        for issue_index, issue in enumerate(result.issues):
            add_targets(
                issue.knowledge_chunk_ids,
                f"improvement_points.{issue_index}",
            )
        for advice_index, advice in enumerate(result.career_elements_advice):
            target_prefix = f"career_elements_advice.{advice_index}"
            add_targets(
                advice.knowledge_chunk_ids,
                f"{target_prefix}.element",
            )
        if rejected_count:
            log_metric(
                "citation.legacy_reference_rejected",
                citation_flow="coach",
                rejected_count=rejected_count,
            )
        return targets_by_chunk

    @staticmethod
    def _normalized_evidence_text(text: str) -> tuple[str, list[int]]:
        """Return conservative comparison text plus offsets into the source.

        Report evidence still has to be copied from one Manager turn.  The
        normalized form exists only to recover that exact source span when an
        otherwise valid model quote differs in typography, whitespace, or
        narrow ASR hesitation noise.
        """

        filler_offsets: set[int] = set()
        for match in ASR_FILLER_RUN_RE.finditer(text):
            before = text[match.start() - 1] if match.start() else ""
            after = text[match.end()] if match.end() < len(text) else ""
            before_is_boundary = (
                not before
                or before.isspace()
                or before in IGNORABLE_EVIDENCE_PUNCTUATION
            )
            after_is_boundary = (
                not after
                or after.isspace()
                or after in IGNORABLE_EVIDENCE_PUNCTUATION
            )
            if before_is_boundary and after_is_boundary:
                filler_offsets.update(range(match.start(), match.end()))

        normalized: list[str] = []
        source_offsets: list[int] = []
        for source_index, source_char in enumerate(text):
            if source_index in filler_offsets:
                continue
            for char in unicodedata.normalize("NFKC", source_char).casefold():
                if char.isspace():
                    continue
                if char in IGNORABLE_EVIDENCE_PUNCTUATION:
                    before = text[source_index - 1] if source_index else ""
                    after = text[source_index + 1] if source_index + 1 < len(text) else ""
                    # Preserve numeric separators such as 1.5, 1,000, or 10:30.
                    if not (
                        char in {".", ",", ":"}
                        and before.isdigit()
                        and after.isdigit()
                    ):
                        continue
                normalized.append(char)
                source_offsets.append(source_index)
        return "".join(normalized), source_offsets

    @classmethod
    def _exact_manager_quote(cls, quote: str, turn_text: str) -> str | None:
        """Bind a narrowly normalized quote back to one exact source span."""

        stripped_quote = quote.strip()
        if stripped_quote and stripped_quote in turn_text:
            return stripped_quote

        normalized_quote, _ = cls._normalized_evidence_text(stripped_quote)
        normalized_turn, turn_offsets = cls._normalized_evidence_text(turn_text)
        if len(normalized_quote) < MIN_NORMALIZED_EVIDENCE_QUOTE_CHARS:
            return None
        start = normalized_turn.find(normalized_quote)
        if start < 0:
            return None
        if normalized_turn.find(normalized_quote, start + 1) >= 0:
            # A short/repeated phrase cannot identify one authoritative span.
            return None
        end = start + len(normalized_quote) - 1
        source_start = turn_offsets[start]
        source_end = turn_offsets[end] + 1
        rebound = turn_text[source_start:source_end].strip()
        return rebound or None

    @classmethod
    def _rebind_issue_quotes(
        cls,
        result: CoachDimensionModelOutput,
        conversation: list[ConversationTurn],
    ) -> CoachDimensionModelOutput:
        """Repair safe drift and discard only individual unverifiable issues."""

        manager_turns = {
            turn.turn_index: turn
            for turn in conversation
            if turn.speaker == "manager"
        }
        rebound_issues = []
        rebound_count = 0
        rejected_count = 0
        for issue in result.issues:
            turn = manager_turns.get(issue.turn_index)
            if turn is None:
                rejected_count += 1
                continue
            exact_quote = cls._exact_manager_quote(issue.quote, turn.text)
            if exact_quote is None:
                rejected_count += 1
                continue
            if exact_quote != issue.quote:
                rebound_count += 1
                issue = issue.model_copy(update={"quote": exact_quote})
            rebound_issues.append(issue)

        if rebound_count or rejected_count:
            log_metric(
                "coach.issue_quote_rebound",
                rebound_count=rebound_count,
                rejected_count=rejected_count,
            )
            return result.model_copy(update={"issues": rebound_issues})
        return result

    @staticmethod
    def _validated_issue_evidence(
        result: CoachDimensionModelOutput,
        conversation: list[ConversationTurn],
    ) -> list[EvidenceRef]:
        manager_turns = {
            turn.turn_index: turn
            for turn in conversation
            if turn.speaker == "manager"
        }
        evidence: list[EvidenceRef] = []
        seen: set[tuple[int, str]] = set()
        for issue in result.issues:
            turn = manager_turns.get(issue.turn_index)
            if turn is None:
                raise ValueError(
                    f"issue turn_index={issue.turn_index} is not a Manager turn"
                )
            quote = issue.quote.strip()
            if quote not in turn.text:
                raise ValueError(
                    f"issue quote does not occur in Manager turn {issue.turn_index}"
                )
            identity = (issue.turn_index, quote)
            if identity in seen:
                # One Manager statement can support multiple distinct coaching
                # issues. Keep every issue and phrase, but expose the shared
                # conversation evidence only once in the report.
                continue
            seen.add(identity)
            evidence.append(
                EvidenceRef(
                    turn_index=issue.turn_index,
                    speaker="manager",
                    quote=issue.quote,
                    note=issue.problem,
                )
            )
        return evidence

    @staticmethod
    def chunks_payload(
        chunks: list[RetrievedChunk] | None,
        *,
        max_text_chars: int | None = None,
    ) -> list[dict[str, Any]]:
        candidate_payload: list[dict[str, Any]] = []
        candidate_anchor_texts: list[str | None] = []
        identities: list[tuple[str, str, str, str] | None] = []
        for chunk in chunks or []:
            item = chunk.model_dump(
                include={"chunk_id", "source_id", "title", "scope", "text", "score"},
                exclude_none=True,
            )
            item["text"] = generation_context_text(chunk.text, chunk.metadata)
            candidate_payload.append(item)
            child_text = str(chunk.text or "").strip()
            candidate_anchor_texts.append(
                child_text
                if child_text and child_text != str(item["text"])
                else None
            )
            identities.append(
                generation_context_deduplication_key(
                    scope=chunk.scope,
                    source_id=chunk.source_id,
                    title=chunk.title,
                    text=str(item["text"]),
                )
            )

        deduplicated_payload: list[dict[str, Any]] = []
        deduplicated_anchor_texts: list[str | None] = []
        seen_contexts: set[tuple[str, str, str, str]] = set()
        duplicate_count = 0
        for item, anchor_text, identity in zip(
            candidate_payload,
            candidate_anchor_texts,
            identities,
            strict=True,
        ):
            if identity is not None and identity in seen_contexts:
                duplicate_count += 1
                continue
            if identity is not None:
                seen_contexts.add(identity)
            deduplicated_payload.append(item)
            deduplicated_anchor_texts.append(anchor_text)

        deduplicated_chars = sum(
            len(str(item.get("text") or "")) for item in deduplicated_payload
        )
        deduplication_is_safe = (
            max_text_chars is None
            or deduplicated_chars <= max(0, int(max_text_chars))
        )
        if duplicate_count and deduplication_is_safe:
            log_metric(
                "rag.generation_context.payload_deduplication",
                consumer="coach",
                duplicate_count=duplicate_count,
                retained_count=len(deduplicated_payload),
            )
        elif duplicate_count:
            log_metric(
                "rag.generation_context.payload_deduplication_skipped",
                consumer="coach",
                duplicate_count=duplicate_count,
                reason="distinct_anchor_budget_protection",
            )
        payload = (
            deduplicated_payload if deduplication_is_safe else candidate_payload
        )
        anchor_texts = (
            deduplicated_anchor_texts
            if deduplication_is_safe
            else candidate_anchor_texts
        )
        return GenericCoachAgent._fit_text_budget(
            payload,
            max_text_chars=max_text_chars,
            anchor_texts=anchor_texts,
        )

    @staticmethod
    def _fit_text_budget(
        items: list[dict[str, Any]],
        *,
        max_text_chars: int | None,
        anchor_texts: list[str | None] | None = None,
    ) -> list[dict[str, Any]]:
        if max_text_chars is None:
            return items
        budget = max(0, int(max_text_chars))
        texts = [str(item.get("text") or "") for item in items]
        if sum(len(text) for text in texts) <= budget:
            return items

        anchors = GenericCoachAgent._budget_anchors(texts, anchor_texts)
        minimums = [len(anchor) if anchor is not None else 0 for anchor in anchors]
        if any(anchors) and sum(minimums) <= budget:
            limits = GenericCoachAgent._anchored_text_limits(
                texts,
                minimums,
                budget,
            )
        else:
            low = 0
            high = max((len(text) for text in texts), default=0)
            while low < high:
                candidate = (low + high + 1) // 2
                if sum(min(len(text), candidate) for text in texts) <= budget:
                    low = candidate
                else:
                    high = candidate - 1
            limits = [low] * len(texts)

        marker = "\n[内容已按上下文预算截断]"
        bounded: list[dict[str, Any]] = []
        for item, text, limit, anchor in zip(
            items,
            texts,
            limits,
            anchors,
            strict=True,
        ):
            next_item = dict(item)
            if len(text) > limit:
                if anchor is not None:
                    next_item["text"] = GenericCoachAgent._text_around_anchor(
                        text,
                        anchor,
                        limit,
                        marker,
                    )
                elif limit <= len(marker):
                    next_item["text"] = text[:limit]
                else:
                    next_item["text"] = text[: limit - len(marker)] + marker
            bounded.append(next_item)
        return bounded

    @staticmethod
    def _budget_anchors(
        texts: list[str],
        anchor_texts: list[str | None] | None,
    ) -> list[str | None]:
        if anchor_texts is None:
            return [None] * len(texts)
        if len(anchor_texts) != len(texts):
            raise ValueError("anchor_texts must contain one entry per text item")

        anchors: list[str | None] = []
        for text, raw_anchor in zip(texts, anchor_texts, strict=True):
            anchor = str(raw_anchor or "").strip()
            anchors.append(anchor if anchor and anchor in text else None)
        return anchors

    @staticmethod
    def _anchored_text_limits(
        texts: list[str],
        minimums: list[int],
        budget: int,
    ) -> list[int]:
        """Share remaining context without taking space reserved for child chunks."""

        low = 0
        high = max(
            (len(text) - minimum for text, minimum in zip(texts, minimums, strict=True)),
            default=0,
        )
        while low < high:
            candidate = (low + high + 1) // 2
            total = sum(
                min(len(text), minimum + candidate)
                for text, minimum in zip(texts, minimums, strict=True)
            )
            if total <= budget:
                low = candidate
            else:
                high = candidate - 1

        limits = [
            min(len(text), minimum + low)
            for text, minimum in zip(texts, minimums, strict=True)
        ]
        remaining = budget - sum(limits)
        for index, text in enumerate(texts):
            if remaining <= 0:
                break
            if limits[index] >= len(text):
                continue
            limits[index] += 1
            remaining -= 1
        return limits

    @staticmethod
    def _text_around_anchor(
        text: str,
        anchor: str,
        limit: int,
        marker: str,
    ) -> str:
        """Bound parent context while retaining the complete retrieved child text."""

        if limit <= 0:
            return ""
        anchor_start = text.find(anchor)
        if anchor_start < 0 or len(anchor) > limit:
            if limit <= len(marker):
                return text[:limit]
            return text[: limit - len(marker)] + marker
        if len(anchor) == limit:
            return anchor

        anchor_end = anchor_start + len(anchor)
        has_prefix = anchor_start > 0
        has_suffix = anchor_end < len(text)
        marker_text = marker.strip()
        prefix_marker = f"{marker_text}\n" if has_prefix else ""
        suffix_marker = f"\n{marker_text}" if has_suffix else ""
        marker_size = len(prefix_marker) + len(suffix_marker)
        slack = limit - len(anchor)
        if marker_size > slack:
            prefix_marker = "…" if has_prefix and slack > 0 else ""
            suffix_marker = (
                "…"
                if has_suffix and slack > len(prefix_marker)
                else ""
            )

        content_budget = limit - len(prefix_marker) - len(suffix_marker)
        context_budget = max(0, content_budget - len(anchor))
        before = min(anchor_start, context_budget // 2)
        after = min(len(text) - anchor_end, context_budget - before)
        before = min(anchor_start, context_budget - after)
        start = anchor_start - before
        end = anchor_end + after
        return f"{prefix_marker}{text[start:end]}{suffix_marker}"

from __future__ import annotations

import asyncio
import logging

from backend.agents.coach_agent.dimension_evaluator import (
    COACH_DIMENSION_SPECS,
    CoachDimensionSpec,
    DimensionEvaluator,
    PreparedDimensionEvaluation,
    coach_task_name,
)
from backend.schemas.coach import CoachReport
from backend.schemas.locale import localized_value
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.state import SessionState
from backend.schemas.task import CoachTaskResult


logger = logging.getLogger(__name__)

COACH_TASK_SPECS: tuple[tuple[str, str], ...] = tuple(
    (spec.task_id, spec.task_name) for spec in COACH_DIMENSION_SPECS
)

_COACH_FAILED_PREFIXES = {
    "zh-CN": "该维度评估失败：",
    "en": "This dimension evaluation failed: ",
    "de": "Die Bewertung dieser Dimension ist fehlgeschlagen: ",
    "ja": "この評価項目の評価に失敗しました：",
}
_COACH_MISSING_RESULTS = {
    "zh-CN": "未返回评估结果",
    "en": "No evaluation result was returned",
    "de": "Es wurde kein Bewertungsergebnis zurückgegeben",
    "ja": "評価結果が返されませんでした",
}
_COACH_REPORT_DISCLAIMERS = {
    "zh-CN": "Coach 报告仅提供演练复盘和改进建议，最终判断由人工负责。",
    "en": (
        "The Coach report provides rehearsal review and improvement suggestions "
        "only; final judgment remains with human decision-makers."
    ),
    "de": (
        "Der Coach-Bericht enthält ausschließlich eine Auswertung der Übung und "
        "Verbesserungsvorschläge; die endgültige Beurteilung liegt bei den "
        "verantwortlichen Personen."
    ),
    "ja": (
        "Coachレポートは演習の振り返りと改善提案のみを提供するものであり、"
        "最終判断は担当者が行います。"
    ),
}


class CoachOrchestrator:
    """Run the four Coach dimensions and package their results directly."""

    def __init__(self):
        self.evaluators = {
            spec.task_id: DimensionEvaluator(spec)
            for spec in COACH_DIMENSION_SPECS
        }

    async def run(
        self,
        state: SessionState,
        retrieved_chunks_by_task: dict[str, list[RetrievedChunk]] | None = None,
    ) -> CoachReport:
        results = await self.run_tasks(state, retrieved_chunks_by_task)
        return self.build_report(state.session_id, results, locale=state.locale)

    async def run_tasks(
        self,
        state: SessionState,
        retrieved_chunks_by_task: dict[str, list[RetrievedChunk]] | None = None,
    ) -> list[CoachTaskResult]:
        chunks = retrieved_chunks_by_task or {}
        tasks = [
            self._run_task_safe(spec, state, chunks.get(spec.task_id, []))
            for spec in COACH_DIMENSION_SPECS
        ]
        return list(await asyncio.gather(*tasks))

    async def _run_task_safe(
        self,
        spec: CoachDimensionSpec,
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk],
    ) -> CoachTaskResult:
        try:
            return await self.run_task(spec.task_id, state, retrieved_chunks)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Coach dimension failed: session_id=%s task_id=%s",
                state.session_id,
                spec.task_id,
            )
            return self.failed_task_result(
                spec.task_id,
                coach_task_name(spec.task_id, state.locale, spec.task_name),
                exc,
                locale=state.locale,
            )

    async def run_task(
        self,
        task_id: str,
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk] | None = None,
        *,
        retry: bool = False,
        retry_model: str | None = None,
        prepared: PreparedDimensionEvaluation | None = None,
    ) -> CoachTaskResult:
        evaluator = self.evaluators.get(task_id)
        if evaluator is None:
            raise ValueError(f"Unknown Coach task_id: {task_id}")
        arguments = {}
        if prepared is not None:
            arguments["prepared"] = prepared
        if retry:
            return await evaluator.evaluate(
                state,
                retrieved_chunks=retrieved_chunks or [],
                retry=True,
                retry_model=retry_model,
                **arguments,
            )
        return await evaluator.evaluate(
            state,
            retrieved_chunks=retrieved_chunks or [],
            **arguments,
        )

    def prepare_task(
        self,
        task_id: str,
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk] | None = None,
    ) -> PreparedDimensionEvaluation:
        evaluator = self.evaluators.get(task_id)
        if evaluator is None:
            raise ValueError(f"Unknown Coach task_id: {task_id}")
        return evaluator.prepare_evaluation(
            state,
            retrieved_chunks=retrieved_chunks or [],
        )

    @staticmethod
    def failed_task_result(
        task_id: str,
        task_name: str,
        error: Exception | str,
        *,
        locale: str = "zh-CN",
    ) -> CoachTaskResult:
        message = str(error).strip() or type(error).__name__
        return CoachTaskResult(
            task_id=task_id,
            task_name=task_name,
            status="failed",
            score=None,
            summary=f"{localized_value(locale, _COACH_FAILED_PREFIXES)}{message[:320]}",
        )

    @staticmethod
    def build_report(
        session_id: str,
        task_results: list[CoachTaskResult],
        *,
        locale: str = "zh-CN",
    ) -> CoachReport:
        results_by_id = {result.task_id: result for result in task_results}
        ordered_results = [
            results_by_id.get(spec.task_id)
            or CoachOrchestrator.failed_task_result(
                spec.task_id,
                coach_task_name(spec.task_id, locale, spec.task_name),
                localized_value(locale, _COACH_MISSING_RESULTS),
                locale=locale,
            )
            for spec in COACH_DIMENSION_SPECS
        ]
        status = (
            "partial"
            if any(result.status == "failed" for result in ordered_results)
            else "success"
        )
        return CoachReport(
            session_id=session_id,
            locale=locale,
            status=status,
            task_results=ordered_results,
            disclaimer=localized_value(locale, _COACH_REPORT_DISCLAIMERS),
        )

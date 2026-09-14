from __future__ import annotations

import asyncio
import json
import logging

from backend.config.settings import Settings, get_settings
from backend.observability.metrics import elapsed_ms, log_metric, now_ms
from backend.schemas.rehearsal_dimensions import (
    REHEARSAL_DIMENSION_ORDER,
    RehearsalDimensionEvaluation,
    RehearsalDimensionId,
)
from backend.schemas.state import (
    REHEARSAL_DIMENSION_COVERAGE_VERSION,
    SessionState,
)
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.model_scheduler import ModelScheduler
from backend.services.prompt_service import PromptService


logger = logging.getLogger(__name__)


class RehearsalDimensionEvaluationService:
    """Evaluate cumulative conversation coverage without keyword heuristics."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        llm_service: LangChainLLMService | None = None,
        model_scheduler: ModelScheduler | None = None,
        prompt_service: PromptService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm_service = llm_service or LangChainLLMService()
        self.model_scheduler = model_scheduler or ModelScheduler(self.settings)
        self.prompt_service = prompt_service or PromptService()

    async def evaluate_manager_turn(
        self,
        state: SessionState,
        manager_message: str,
    ) -> list[RehearsalDimensionId] | None:
        task_name = "rehearsal_dimensions"
        previous = list(state.rehearsal_context.covered_dimensions)
        manager_only_coverage = (
            state.rehearsal_context.dimension_coverage_version
            >= REHEARSAL_DIMENSION_COVERAGE_VERSION
        )
        manager_conversation = [
            {
                "turn_index": turn.turn_index,
                "text": turn.text,
            }
            for turn in state.conversation
            if turn.speaker == "manager"
        ]
        if manager_only_coverage:
            manager_conversation = manager_conversation[-11:]
        context = {
            "previous_manager_messages": manager_conversation,
            "latest_manager_message": manager_message,
        }
        prompt = self.prompt_service.render(
            "rehearsal/dimension_evaluation.jinja2",
            context_json=json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        model_name = self.settings.model_for_task(task_name)
        started = now_ms()
        try:
            async with self.model_scheduler.slot(
                session_id=state.session_id,
                category="interactive",
                endpoint=self.settings.chat_url,
                model=model_name,
            ):
                output = await self.llm_service.ainvoke_structured_single(
                    prompt=prompt,
                    schema=RehearsalDimensionEvaluation,
                    task_name=task_name,
                    model=model_name,
                    temperature=self.settings.temperature_for_task(task_name),
                    max_tokens=self.settings.max_tokens_for_task(task_name),
                    timeout_seconds=self.settings.timeout_for_task(task_name),
                    enable_thinking=self.settings.enable_thinking_for_task(task_name),
                    structured_transport="json_schema",
                    json_schema_strict=False,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Rehearsal dimension model evaluation failed: error_type=%s",
                type(exc).__name__,
            )
            log_metric(
                "rehearsal.dimension_evaluation",
                status="failed",
                model=model_name,
                duration_ms=elapsed_ms(started),
                error_type=type(exc).__name__,
            )
            return None

        # Coverage from an older taxonomy is ignored until this evaluator has
        # rebuilt it once from manager-only history under the current version.
        selected = set(previous if manager_only_coverage else ())
        selected.update(output.covered_dimensions)
        merged = [
            dimension
            for dimension in REHEARSAL_DIMENSION_ORDER
            if dimension in selected
        ]
        log_metric(
            "rehearsal.dimension_evaluation",
            status="success",
            model=model_name,
            duration_ms=elapsed_ms(started),
            covered_count=len(merged),
            newly_covered_count=len(set(merged).difference(previous)),
        )
        return merged

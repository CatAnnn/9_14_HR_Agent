from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.schemas.intent import (
    IntentConfig,
    PERFORMANCE_DETAIL_TARGET_CHARS,
    PERFORMANCE_OVERVIEW_TARGET_CHARS,
    IntentPerformanceDraft,
    IntentPerformanceDraftOutput,
    expected_performance_goals,
)
from backend.schemas.prompt_context import (
    IntentPerformancePromptContext,
    PromptRetrievedChunk,
    prompt_context_json,
)
from backend.schemas.profile import EmployeeProfile
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.locale import SessionLocale
from backend.services.langchain_llm_service import (
    LangChainLLMService,
    StructuredStreamContentCallback,
)
from backend.services.prompt_service import PromptService


@dataclass(frozen=True, slots=True)
class PreparedIntentPerformance:
    prompt: str
    retry_prompt: str


class IntentPerformanceAgent:
    def __init__(
        self,
        *,
        llm: LangChainLLMService | None = None,
        prompt_service: PromptService | None = None,
    ):
        self.llm = llm or LangChainLLMService()
        self.prompt_service = prompt_service or PromptService()

    def prepare_generation(
        self,
        *,
        profile: EmployeeProfile,
        supplemental_info: str | None,
        intent: IntentConfig,
        eligibility: dict[str, object],
        retrieved_chunks: Sequence[RetrievedChunk] = (),
        organization_unit_chunks: Sequence[RetrievedChunk] = (),
        output_locale: SessionLocale = "zh-CN",
    ) -> PreparedIntentPerformance:
        inference_rule = intent.performance_inference
        if inference_rule is None:
            raise ValueError(f"Intent {intent.id} has no performance_inference config.")

        goals = expected_performance_goals(profile.key_goals)
        context_json = prompt_context_json(
            IntentPerformancePromptContext(
                output_locale=output_locale,
                intent={"id": intent.id, "name": intent.name},
                employee_profile=profile.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
                expected_goal_count=len(goals),
                supplemental_info=str(supplemental_info or ""),
                eligibility=eligibility,
                performance_inference=inference_rule.model_dump(mode="json"),
                retrieved_chunks=[
                    PromptRetrievedChunk(
                        chunk_id=chunk.chunk_id,
                        source_id=chunk.source_id,
                        title=chunk.title,
                        scope=chunk.scope,
                        text=chunk.text,
                        score=chunk.score,
                    )
                    for chunk in retrieved_chunks
                ],
                organization_unit_chunks=[
                    PromptRetrievedChunk(
                        chunk_id=chunk.chunk_id,
                        source_id=chunk.source_id,
                        title=chunk.title,
                        scope=chunk.scope,
                        text=chunk.text,
                        score=chunk.score,
                    )
                    for chunk in organization_unit_chunks
                ],
            )
        )
        render_arguments = {
            "context_json": context_json,
            "overview_target_chars": PERFORMANCE_OVERVIEW_TARGET_CHARS,
            "detail_target_chars": PERFORMANCE_DETAIL_TARGET_CHARS,
        }
        return PreparedIntentPerformance(
            prompt=self.prompt_service.render(
                "intent/performance_inference.jinja2",
                retry=False,
                **render_arguments,
            ),
            retry_prompt=self.prompt_service.render(
                "intent/performance_inference.jinja2",
                retry=True,
                **render_arguments,
            ),
        )

    async def generate(
        self,
        *,
        profile: EmployeeProfile,
        supplemental_info: str | None,
        intent: IntentConfig,
        eligibility: dict[str, object],
        retrieved_chunks: Sequence[RetrievedChunk] = (),
        organization_unit_chunks: Sequence[RetrievedChunk] = (),
        output_locale: SessionLocale = "zh-CN",
        retry: bool = False,
        retry_model: str | None = None,
        prepared: PreparedIntentPerformance | None = None,
        on_content_update: StructuredStreamContentCallback | None = None,
    ) -> IntentPerformanceDraft:
        prepared_generation = prepared or self.prepare_generation(
            profile=profile,
            supplemental_info=supplemental_info,
            intent=intent,
            eligibility=eligibility,
            retrieved_chunks=retrieved_chunks,
            organization_unit_chunks=organization_unit_chunks,
            output_locale=output_locale,
        )
        output = await self.llm.ainvoke_structured_single(
            prompt=(
                prepared_generation.retry_prompt
                if retry
                else prepared_generation.prompt
            ),
            schema=IntentPerformanceDraftOutput,
            task_name="intent_performance",
            model=retry_model if retry else None,
            temperature=0.0 if retry else None,
            enable_thinking=False if retry else None,
            stream=True,
            record_stream_timing=True,
            on_content_update=on_content_update,
            structured_transport="json_schema",
            json_schema_strict=False,
        )
        return output.bind_sections(output_locale)

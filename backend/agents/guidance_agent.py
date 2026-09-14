from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Literal, Mapping, Sequence

from pydantic import BaseModel, Field

from backend.business_config.loader import BusinessConfigLoader, get_config_loader
from backend.observability.metrics import log_metric
from backend.exceptions.llm_errors import StructuredOutputError
from backend.rag.citation import (
    merge_citations,
    referenced_chunks_to_citations,
    validated_references_to_citations,
)
from backend.rag.parent_context import (
    generation_context_deduplication_key,
    generation_context_text,
)
from backend.schemas.guidance import (
    GUIDANCE_DETAIL_MAX_ITEMS,
    GUIDANCE_DETAIL_MIN_ITEMS,
    GUIDANCE_DIMENSION_POINT_CONTRACTS,
    GUIDANCE_GROUP_MAX_ITEMS,
    GUIDANCE_GROUP_MIN_ITEMS,
    GUIDANCE_POINT_DETAIL_MAX_LENGTH,
    GUIDANCE_POINT_SUMMARY_MAX_LENGTH,
    GuidanceContentModel,
    GuidanceDimensionPoints,
    GuidanceGeneratedPlanPointContent,
    GuidanceGeneratedPointContent,
    GuidanceGeneratedPointGroup,
    GuidanceGeneratedPointGroupList,
    GuidanceGeneratedPlanPointGroup,
    GuidancePlanPointGroup,
    GuidancePointDetail,
    GuidancePointGroup,
    GuidanceReport,
    guidance_quality_issues,
)
from backend.schemas.prompt_context import (
    GuidanceMotives,
    GuidancePromptContext,
    PromptCompanyValues,
    PromptMotive,
    prompt_context_json,
)
from backend.schemas.locale import localized_value
from backend.schemas.state import SessionState
from backend.schemas.retrieval import CitationReference, RetrievedChunk
from backend.services.career_elements import (
    career_elements_applicable,
    current_career_elements,
)
from backend.services.knowledge_skill_service import (
    build_knowledge_skill_context,
    get_knowledge_skill_router,
)
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.personality_behavior_service import (
    get_personality_behavior_resolver,
)
from backend.services.prompt_service import PromptService


GuidanceSectionKey = Literal[
    "purpose",
    "opening_suggestion",
    "risk_preview",
    "response_strategies",
    "safer_phrases",
]

GuidanceSectionValue = str | list[str]
GuidanceDimensionKey = Literal["start", "emotion", "requirement", "plan"]
GuidancePointGroupKey = Literal[
    "start_points",
    "emotion_points",
    "requirement_points",
    "plan_points",
]
GuidancePointGroupPayload = list[dict[str, object]]
GuidanceCitationRefKey = Literal[
    "_citation_refs_start",
    "_citation_refs_emotion",
    "_citation_refs_requirement",
    "_citation_refs_plan",
]
GuidanceCitationRefPayload = dict[str, list[dict[str, str]]]
GuidanceResultKey = (
    GuidanceSectionKey | GuidancePointGroupKey | GuidanceCitationRefKey
)
GuidanceResultValue = (
    GuidanceSectionValue | GuidancePointGroupPayload | GuidanceCitationRefPayload
)

GUIDANCE_SECTION_KEYS: tuple[GuidanceSectionKey, ...] = (
    "purpose",
    "opening_suggestion",
    "risk_preview",
    "response_strategies",
    "safer_phrases",
)
GUIDANCE_POINT_GROUP_KEYS: tuple[GuidancePointGroupKey, ...] = (
    "start_points",
    "emotion_points",
    "requirement_points",
    "plan_points",
)
GUIDANCE_RESULT_KEYS: tuple[GuidanceResultKey, ...] = (
    *GUIDANCE_SECTION_KEYS,
    *GUIDANCE_POINT_GROUP_KEYS,
)
GUIDANCE_INTERNAL_RESULT_KEYS: tuple[GuidanceCitationRefKey, ...] = (
    "_citation_refs_start",
    "_citation_refs_emotion",
    "_citation_refs_requirement",
    "_citation_refs_plan",
)

GUIDANCE_REPORT_VERSION = get_config_loader().guidance_version()

# Keep public field names stable while aligning their visible meaning with CoachReport.
GUIDANCE_DIMENSION_ALIGNMENT: dict[str, dict[str, str]] = {
    "opening_suggestion": {
        "report_task_id": "opening_evaluation",
        "dimension_id": "opening_and_result_framing",
        "dimension_name": "开场定调与绩效结果对齐",
    },
    "risk_preview": {
        "report_task_id": "emotion_evaluation",
        "dimension_id": "emotion_reception_and_empathy",
        "dimension_name": "情绪承接、接纳与共情",
    },
    "response_strategies": {
        "report_task_id": "output_expectations_evaluation",
        "dimension_id": "return_to_output_expectations",
        "dimension_name": "产出与标准",
    },
    "safer_phrases": {
        "report_task_id": "development_plan_evaluation",
        "dimension_id": "summary_and_intent_specific_development_plan",
        "dimension_name": "总结与差异化发展计划",
    },
}
GUIDANCE_SECTION_TITLES: dict[GuidanceSectionKey, str] = {
    "purpose": "本次沟通目标",
    "opening_suggestion": "开场定调与绩效结果对齐",
    "risk_preview": "情绪承接、接纳与共情",
    "response_strategies": "产出与标准",
    "safer_phrases": "总结与差异化发展计划",
}
GUIDANCE_SECTION_TITLES_EN: dict[GuidanceSectionKey, str] = {
    "purpose": "Conversation Objective",
    "opening_suggestion": "Opening and Performance Result Alignment",
    "risk_preview": "Emotion Reception, Acceptance, and Empathy",
    "response_strategies": "Outputs and Standards",
    "safer_phrases": "Summary and Differentiated Development Plan",
}
GUIDANCE_SECTION_TITLES_DE: dict[GuidanceSectionKey, str] = {
    "purpose": "Gesprächsziel",
    "opening_suggestion": "Gesprächseinstieg und Abstimmung des Leistungsergebnisses",
    "risk_preview": "Emotionale Aufnahme, Akzeptanz und Empathie",
    "response_strategies": "Ergebnisse und Standards",
    "safer_phrases": "Zusammenfassung und differenzierter Entwicklungsplan",
}
GUIDANCE_SECTION_TITLES_JA: dict[GuidanceSectionKey, str] = {
    "purpose": "今回の面談目標",
    "opening_suggestion": "導入と評価結果のすり合わせ",
    "risk_preview": "感情の受け止め、受容と共感",
    "response_strategies": "成果と基準",
    "safer_phrases": "まとめと個別化した育成計画",
}

GUIDANCE_POINT_TITLES_EN: dict[GuidanceDimensionKey, tuple[str, ...]] = {
    "start": ("Opening and Performance Result Alignment",),
    "emotion": (
        "Listen Carefully and Understand Emotion",
        "Empathic Summary",
        "Joint Exploration",
    ),
    "requirement": (
        "Correct Goal Direction",
        "Breakthrough Results",
        "High-Performance Culture",
    ),
    "plan": ("Role Development", "Career Elements"),
}
GUIDANCE_POINT_TITLES_DE: dict[GuidanceDimensionKey, tuple[str, ...]] = {
    "start": ("Gesprächseinstieg und Abstimmung des Leistungsergebnisses",),
    "emotion": (
        "Aufmerksam zuhören und Emotionen verstehen",
        "Empathisch zusammenfassen",
        "Gemeinsam erkunden",
    ),
    "requirement": (
        "Angemessene Zielrichtung",
        "Anspruchsvolle Ergebnisse",
        "Hochleistungskultur",
    ),
    "plan": ("Rollenentwicklung", "Career Elements"),
}
GUIDANCE_POINT_TITLES_JA: dict[GuidanceDimensionKey, tuple[str, ...]] = {
    "start": ("導入と評価結果のすり合わせ",),
    "emotion": (
        "丁寧に聴き、感情を理解する",
        "共感的に要約する",
        "一緒に掘り下げる",
    ),
    "requirement": (
        "目標の方向性の妥当性",
        "成果の飛躍性",
        "高業績文化",
    ),
    "plan": ("役割開発", "Career Elements"),
}

_GUIDANCE_SECTION_TITLES_BY_LOCALE = {
    "zh-CN": GUIDANCE_SECTION_TITLES,
    "en": GUIDANCE_SECTION_TITLES_EN,
    "de": GUIDANCE_SECTION_TITLES_DE,
    "ja": GUIDANCE_SECTION_TITLES_JA,
}
_GUIDANCE_POINT_TITLES_BY_LOCALE = {
    "zh-CN": {
        key: tuple(contract["titles"])
        for key, contract in GUIDANCE_DIMENSION_POINT_CONTRACTS.items()
    },
    "en": GUIDANCE_POINT_TITLES_EN,
    "de": GUIDANCE_POINT_TITLES_DE,
    "ja": GUIDANCE_POINT_TITLES_JA,
}
_GUIDANCE_REPORT_POLICIES = {
    "zh-CN": (
        "谈前指导不评价用户表现，不引用用户对话原话，不做评分。",
        "本建议用于演练准备，不替代 HR/Legal 或 Manager 的最终判断。",
    ),
    "en": (
        "Pre-conversation guidance does not evaluate the user's performance, "
        "quote the user's dialogue, or assign scores.",
        "This guidance supports rehearsal preparation and does not replace final "
        "HR, Legal, or Manager judgment.",
    ),
    "de": (
        "Die Gesprächsvorbereitung bewertet nicht die Leistung der nutzenden Person, "
        "zitiert keine Äußerungen aus dem Gespräch und vergibt keine Bewertung.",
        "Diese Hinweise dienen der Vorbereitung und ersetzen nicht die abschließende "
        "Beurteilung durch HR, Legal oder die Führungskraft.",
    ),
    "ja": (
        "面談前ガイダンスは利用者のパフォーマンスを評価せず、利用者の発言を引用せず、"
        "採点も行いません。",
        "本ガイダンスは面談準備を支援するものであり、HR、法務、または管理職による"
        "最終判断に代わるものではありません。",
    ),
}


def guidance_section_titles(locale: str) -> dict[GuidanceSectionKey, str]:
    return localized_value(locale, _GUIDANCE_SECTION_TITLES_BY_LOCALE)


def _guidance_item_range(min_items: int, max_items: int) -> str:
    return str(min_items) if min_items == max_items else f"{min_items}-{max_items}"


GuidanceExactTwoDetailList = Annotated[
    list[GuidancePointDetail],
    Field(min_length=2, max_length=2),
]


class GuidanceGeneratedTwoDetailPointContent(GuidanceGeneratedPointContent):
    details: GuidanceExactTwoDetailList


GuidanceStartPointList = Annotated[
    list[GuidanceGeneratedTwoDetailPointContent],
    Field(min_length=1, max_length=1),
]
GuidanceThreePointList = Annotated[
    list[GuidanceGeneratedTwoDetailPointContent],
    Field(min_length=3, max_length=3),
]
GuidancePlanPointList = Annotated[
    list[GuidanceGeneratedPlanPointContent],
    Field(min_length=1, max_length=2),
]


def guidance_supplemental_info(state: SessionState) -> str:
    """Return the complete manager-provided context for Guidance."""
    return str(state.supplemental_info or "")


def guidance_motivation_context(state: SessionState) -> dict[str, object]:
    """Expose only the user-selected motive IDs to Guidance."""
    if state.motivation is None:
        return {}
    return {
        "primary_motive_id": state.motivation.primary_motive_id,
        "secondary_motive_ids": list(state.motivation.secondary_motive_ids),
    }


class GuidanceStartOutput(GuidanceContentModel):
    points: GuidanceStartPointList


class GuidanceEmotionOutput(GuidanceContentModel):
    points: GuidanceThreePointList


class GuidanceRequirementOutput(GuidanceContentModel):
    points: GuidanceThreePointList


class GuidancePlanOutput(GuidanceContentModel):
    points: GuidancePlanPointList


@dataclass(frozen=True, slots=True)
class GuidanceDimensionSpec:
    key: GuidanceDimensionKey
    template_path: str
    retrieval_name: str
    schema: type[BaseModel]
    section_keys: tuple[GuidanceSectionKey, ...]
    point_group_key: GuidancePointGroupKey
    group_min_items: int
    group_max_items: int
    detail_min_items: int
    detail_max_items: int
    retrieval_top_k: int = 8


@dataclass(frozen=True, slots=True)
class PreparedGuidanceDimension:
    prompt: str
    retry_prompt: str


GUIDANCE_DIMENSION_SPECS: tuple[GuidanceDimensionSpec, ...] = (
    GuidanceDimensionSpec(
        key="start",
        template_path="guidance/start.jinja2",
        retrieval_name="guidance_start",
        schema=GuidanceStartOutput,
        section_keys=("purpose", "opening_suggestion"),
        point_group_key="start_points",
        group_min_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["start"]["group_min_items"],
        group_max_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["start"]["group_max_items"],
        detail_min_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["start"]["detail_min_items"],
        detail_max_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["start"]["detail_max_items"],
    ),
    GuidanceDimensionSpec(
        key="emotion",
        template_path="guidance/emotion.jinja2",
        retrieval_name="guidance_emotion",
        schema=GuidanceEmotionOutput,
        section_keys=("risk_preview",),
        point_group_key="emotion_points",
        group_min_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["emotion"]["group_min_items"],
        group_max_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["emotion"]["group_max_items"],
        detail_min_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["emotion"]["detail_min_items"],
        detail_max_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["emotion"]["detail_max_items"],
    ),
    GuidanceDimensionSpec(
        key="requirement",
        template_path="guidance/requirement.jinja2",
        retrieval_name="guidance_requirement",
        schema=GuidanceRequirementOutput,
        section_keys=("response_strategies",),
        point_group_key="requirement_points",
        group_min_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["requirement"]["group_min_items"],
        group_max_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["requirement"]["group_max_items"],
        detail_min_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["requirement"]["detail_min_items"],
        detail_max_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["requirement"]["detail_max_items"],
    ),
    GuidanceDimensionSpec(
        key="plan",
        template_path="guidance/plan.jinja2",
        retrieval_name="guidance_plan",
        schema=GuidancePlanOutput,
        section_keys=("safer_phrases",),
        point_group_key="plan_points",
        group_min_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["plan"]["group_min_items"],
        group_max_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["plan"]["group_max_items"],
        detail_min_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["plan"]["detail_min_items"],
        detail_max_items=GUIDANCE_DIMENSION_POINT_CONTRACTS["plan"]["detail_max_items"],
    ),
)


def validate_guidance_runtime(
    config_loader: BusinessConfigLoader | None = None,
    prompt_service: PromptService | None = None,
) -> str:
    """Render every Guidance template under StrictUndefined at startup."""
    loader = config_loader or get_config_loader()
    prompts = prompt_service or PromptService()
    context_json = prompt_context_json(GuidancePromptContext())
    for spec in GUIDANCE_DIMENSION_SPECS:
        prompts.render(
            spec.template_path,
            guidance_group_range=_guidance_item_range(
                spec.group_min_items, spec.group_max_items
            ),
            guidance_detail_range=_guidance_item_range(
                spec.detail_min_items, spec.detail_max_items
            ),
            guidance_point_summary_max_length=GUIDANCE_POINT_SUMMARY_MAX_LENGTH,
            guidance_point_detail_max_length=GUIDANCE_POINT_DETAIL_MAX_LENGTH,
            context_json=context_json,
        )
    return loader.guidance_version()


class GuidanceAgent:
    def __init__(self, llm_service: LangChainLLMService | None = None):
        self.llm = llm_service or LangChainLLMService()

    async def generate(self, state: SessionState, retrieved_chunks: list[RetrievedChunk]) -> GuidanceReport:
        sections = await self.generate_sections(state, retrieved_chunks)
        return self.report_from_sections(state, retrieved_chunks, sections)

    async def generate_sections(
        self,
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk],
    ) -> dict[GuidanceResultKey, GuidanceResultValue]:
        tasks = [
            asyncio.create_task(
                self.generate_dimension(state, retrieved_chunks, spec),
                name=f"guidance-{spec.key}",
            )
            for spec in GUIDANCE_DIMENSION_SPECS
        ]
        try:
            dimension_results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        sections: dict[GuidanceResultKey, GuidanceResultValue] = {}
        for result in dimension_results:
            sections.update(result)
        ordered = {key: sections[key] for key in GUIDANCE_RESULT_KEYS}
        ordered.update(
            {
                key: sections[key]
                for key in GUIDANCE_INTERNAL_RESULT_KEYS
                if key in sections
            }
        )
        return ordered

    async def generate_dimension(
        self,
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk],
        spec: GuidanceDimensionSpec,
        *,
        retry: bool = False,
        retry_model: str | None = None,
        prepared: PreparedGuidanceDimension | None = None,
    ) -> dict[GuidanceResultKey, GuidanceResultValue]:
        prepared_dimension = prepared or self.prepare_dimension(
            state,
            retrieved_chunks,
            spec,
        )
        prompt = (
            prepared_dimension.retry_prompt
            if retry
            else prepared_dimension.prompt
        )
        result = await self.llm.ainvoke_structured_single(
            prompt=prompt,
            schema=spec.schema,
            task_name="guidance",
            model=retry_model if retry else None,
            temperature=0.0 if retry else None,
            enable_thinking=False if retry else None,
            stream=True,
            record_stream_timing=True,
            payload_normalizer=self._discard_model_generated_titles,
            structured_transport="json_schema",
            json_schema_strict=False,
        )
        raw_points = getattr(result, "points", None)
        if not isinstance(raw_points, list):
            raise TypeError("Guidance dimension output must contain a points array.")
        generated_points = self._attach_fixed_titles(state, spec, raw_points)
        payload = {
            "points": [point.model_dump(mode="json") for point in generated_points]
        }
        quality_issues = guidance_quality_issues(payload)
        if quality_issues:
            log_metric(
                "guidance.output_quality_warning",
                guidance_dimension=spec.key,
                warning_count=len(quality_issues),
                warnings=",".join(quality_issues),
            )
        points = [
            self._stored_point_group(point)
            for point in generated_points
        ]
        self._sanitize_knowledge_chunk_ids(points, retrieved_chunks)
        sections: dict[GuidanceResultKey, GuidanceResultValue] = {
            **self._legacy_sections(spec, points),
            spec.point_group_key: [
                point.model_dump(mode="json") for point in points
            ],
            self._citation_ref_key(spec.key): self._citation_ref_payload(
                spec.key,
                generated_points,
            ),
        }
        return sections

    def prepare_dimension(
        self,
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk],
        spec: GuidanceDimensionSpec,
    ) -> PreparedGuidanceDimension:
        if not state.intent or not state.intent.config:
            raise ValueError("intent is required for guidance")
        prompt = self._build_dimension_prompt(state, retrieved_chunks, spec)
        group_range = _guidance_item_range(
            spec.group_min_items, spec.group_max_items
        )
        if spec.key == "plan":
            expected_titles = self._expected_plan_titles(state)
            group_range = str(len(expected_titles))
        detail_range = _guidance_item_range(
            spec.detail_min_items, spec.detail_max_items
        )
        retry_prompt = (
            f"{prompt}\n\n"
            "上一次输出因结构格式、超时或临时网关错误被拒绝。补齐该维度要求的全部字段；"
            f"points 数量必须为 {group_range} 个；"
            f"每个 details 数量必须为 {detail_range} 条；"
            "每个 point 不得输出 title，显示标题由后端按固定顺序附加；"
            "每个 point 必须先生成 summary，再生成具体 details；"
            f"summary 必须是一句不超过 {GUIDANCE_POINT_SUMMARY_MAX_LENGTH} 字的完整概述，且不得复述 details；"
            f"每条 details 不超过 {GUIDANCE_POINT_DETAIL_MAX_LENGTH} 字；"
            "每个总体要点独立成为一个 points 元素；details 中每条只表达一句具体动作、建议或话术，"
            "不要用分号堆叠多个步骤，也不要写成长段；"
            "所有 details 都是带句末标点的完整句，直接话术使用与输出语言自然匹配且成对的引号；"
            "完整匹配当前响应 Schema。"
        )
        return PreparedGuidanceDimension(prompt=prompt, retry_prompt=retry_prompt)

    @staticmethod
    def _build_dimension_prompt(
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk],
        spec: GuidanceDimensionSpec,
    ) -> str:
        context = GuidanceAgent._build_context(
            state,
            retrieved_chunks,
            agent_name=spec.retrieval_name,
        )
        return PromptService().render(
            spec.template_path,
            guidance_group_range=_guidance_item_range(
                spec.group_min_items, spec.group_max_items
            ),
            guidance_detail_range=_guidance_item_range(
                spec.detail_min_items, spec.detail_max_items
            ),
            guidance_point_summary_max_length=GUIDANCE_POINT_SUMMARY_MAX_LENGTH,
            guidance_point_detail_max_length=GUIDANCE_POINT_DETAIL_MAX_LENGTH,
            context_json=prompt_context_json(context),
        )

    @staticmethod
    def _discard_model_generated_titles(
        payload: dict[str, object],
    ) -> tuple[dict[str, object], list[str]]:
        """Ignore any stray model title; configured titles are authoritative."""
        points = payload.get("points")
        if not isinstance(points, list):
            return payload, []

        removed_title = False
        normalized_points: list[object] = []
        for item in points:
            if not isinstance(item, Mapping):
                normalized_points.append(item)
                continue
            normalized_item = dict(item)
            if "title" in normalized_item:
                normalized_item.pop("title")
                removed_title = True
            normalized_points.append(normalized_item)

        if not removed_title:
            return payload, []
        return {
            **payload,
            "points": normalized_points,
        }, ["discarded_model_generated_point_titles"]

    @classmethod
    def _attach_fixed_titles(
        cls,
        state: SessionState,
        spec: GuidanceDimensionSpec,
        points: Sequence[GuidanceGeneratedPointContent],
    ) -> list[GuidanceGeneratedPointGroup]:
        fixed_titles = cls._fixed_point_titles(state, spec)
        if len(points) != len(fixed_titles):
            raise StructuredOutputError(
                "business_validation",
                "Guidance point count does not match the configured title contract: "
                f"dimension={spec.key}, expected={len(fixed_titles)}, "
                f"actual={len(points)}",
            )

        point_schema = (
            GuidanceGeneratedPlanPointGroup
            if spec.key == "plan"
            else GuidanceGeneratedPointGroup
        )
        return [
            point_schema.model_validate(
                {
                    **point.model_dump(mode="json"),
                    "title": fixed_title,
                }
            )
            for fixed_title, point in zip(fixed_titles, points, strict=True)
        ]

    @staticmethod
    def _fixed_point_titles(
        state: SessionState,
        spec: GuidanceDimensionSpec,
    ) -> list[str]:
        if spec.key == "plan":
            return GuidanceAgent._expected_plan_titles(state)
        return list(
            localized_value(state.locale, _GUIDANCE_POINT_TITLES_BY_LOCALE)[spec.key]
        )

    @staticmethod
    def _expected_plan_titles(state: SessionState) -> list[str]:
        intent_id = state.intent.intent_id if state.intent else ""
        titles = [
            localized_value(state.locale, _GUIDANCE_POINT_TITLES_BY_LOCALE)["plan"][0]
        ]
        if career_elements_applicable(state.employee_profile, intent_id):
            titles.append("Career Elements")
        return titles

    @staticmethod
    def _legacy_sections(
        spec: GuidanceDimensionSpec,
        points: Sequence[GuidancePointGroup],
    ) -> dict[GuidanceSectionKey, GuidanceSectionValue]:
        if not points:
            raise ValueError("Guidance dimension output must contain at least one point.")
        if spec.key == "start":
            all_details = [detail for point in points for detail in point.details]
            direct_phrase = next(
                (
                    detail
                    for detail in all_details
                    if "“" in detail and "”" in detail
                ),
                all_details[-1],
            )
            return {
                "purpose": points[0].summary or points[0].details[0],
                "opening_suggestion": direct_phrase,
            }

        section_key = spec.section_keys[0]
        return {
            section_key: [
                f"{point.title}：{point.summary or point.details[0]}"
                for point in points
            ]
        }

    @staticmethod
    def _build_report_prompt(
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk],
    ) -> str:
        """Render all active prompts for diagnostics; runtime calls them separately."""
        return "\n\n".join(
            GuidanceAgent._build_dimension_prompt(state, retrieved_chunks, spec)
            for spec in GUIDANCE_DIMENSION_SPECS
        )

    @staticmethod
    def _build_context(
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk],
        *,
        agent_name: str | None = None,
    ) -> GuidancePromptContext:
        skill_router = get_knowledge_skill_router()
        active_skills = (
            skill_router.select(
                agent_name,
                build_knowledge_skill_context(state, include_conversation=False),
                emit_metrics=False,
            )
            if agent_name
            else []
        )
        loader = get_config_loader()
        company_values = GuidanceAgent._company_values_prompt_payload(
            loader.company_values()
        )
        culture_enabled = company_values.enabled and bool(company_values.values)
        general_chunks = [chunk for chunk in retrieved_chunks if chunk.scope != "culture"]
        culture_chunks = [
            chunk for chunk in retrieved_chunks if culture_enabled and chunk.scope == "culture"
        ]
        motive_loader = getattr(loader, "motives", None)
        motive_options = motive_loader() if callable(motive_loader) else {}
        intent_id = state.intent.intent_id if state.intent else ""
        return GuidancePromptContext.model_validate(
            {
                "output_locale": state.locale,
                "profile": state.employee_profile,
                "supplemental_info": guidance_supplemental_info(state),
                "performance_context": (
                    state.intent.performance_context if state.intent else ""
                )
                or "",
                "intent": state.intent,
                "personality": state.personality,
                "personality_behavior_profile": (
                    get_personality_behavior_resolver().resolve(
                        state.personality,
                        getattr(state, "personality_facets", None),
                    )
                ),
                "motivation": GuidanceAgent._motives_prompt_payload(
                    state,
                    motive_options,
                ),
                "retrieved_chunks": GuidanceAgent._chunks_payload(general_chunks),
                "company_values": company_values,
                "culture_chunks": GuidanceAgent._chunks_payload(culture_chunks),
                "active_skills": skill_router.prompt_payload(active_skills),
                "knowledge_base_folders": sorted(
                    {chunk.scope for chunk in retrieved_chunks if chunk.scope}
                ),
                "career_elements_applicable": career_elements_applicable(
                    state.employee_profile,
                    intent_id,
                ),
                "current_career_elements": current_career_elements(
                    state.employee_profile
                ),
            }
        )

    @staticmethod
    def _motives_prompt_payload(
        state: SessionState,
        motive_options: Mapping[str, object],
    ) -> GuidanceMotives:
        if state.motivation is None:
            return GuidanceMotives()

        def motive_item(motive_id: str) -> PromptMotive:
            option = motive_options.get(motive_id)
            if isinstance(option, Mapping):
                name = str(option.get("name") or motive_id)
                description = str(option.get("description") or "")
            else:
                name = str(getattr(option, "name", "") or motive_id)
                description = str(getattr(option, "description", "") or "")
            return PromptMotive(
                id=motive_id,
                name=name,
                description=description,
            )

        return GuidanceMotives(
            primary=motive_item(state.motivation.primary_motive_id),
            secondary=[
                motive_item(motive_id)
                for motive_id in state.motivation.secondary_motive_ids
            ],
        )

    @staticmethod
    def _company_values_prompt_payload(
        config: Mapping[str, object],
    ) -> PromptCompanyValues:
        enabled = bool(config.get("enabled") and config.get("values"))
        values = []
        if enabled:
            for raw_value in config.get("values") or []:
                if not isinstance(raw_value, Mapping):
                    continue
                values.append(
                    {
                        key: raw_value.get(key)
                        for key in (
                            "id",
                            "name",
                            "name_zh",
                            "definition",
                            "desired_behaviors",
                            "desired_behaviors_zh",
                            "anti_patterns",
                            "manager_applications",
                        )
                    }
                )
        return PromptCompanyValues.model_validate(
            {
                "enabled": enabled,
                "version": config.get("version"),
                "company_name": config.get("company_name"),
                "culture_name": config.get("culture_name") or {},
                "values": values,
            }
        )

    @staticmethod
    def _chunks_payload(chunks: list[RetrievedChunk]) -> list[dict]:
        payload: list[dict] = []
        seen_contexts: set[tuple[str, str, str, str]] = set()
        duplicate_count = 0
        for chunk in chunks:
            item = chunk.model_dump(
                include={"chunk_id", "source_id", "title", "scope", "text", "score"},
                exclude_none=True,
            )
            item["text"] = generation_context_text(chunk.text, chunk.metadata)
            identity = generation_context_deduplication_key(
                scope=chunk.scope,
                source_id=chunk.source_id,
                title=chunk.title,
                text=str(item["text"]),
            )
            if identity is not None and identity in seen_contexts:
                duplicate_count += 1
                continue
            if identity is not None:
                seen_contexts.add(identity)
            payload.append(item)
        if duplicate_count:
            log_metric(
                "rag.generation_context.payload_deduplication",
                consumer="guidance",
                duplicate_count=duplicate_count,
                retained_count=len(payload),
            )
        return payload

    @staticmethod
    def _stored_point_group(
        point: GuidanceGeneratedPointGroup,
    ) -> GuidancePointGroup:
        stored_schema = (
            GuidancePlanPointGroup
            if isinstance(point, GuidanceGeneratedPlanPointGroup)
            else GuidancePointGroup
        )
        return stored_schema(
            title=point.title,
            summary=point.summary,
            details=point.details,
        )

    @staticmethod
    def _sanitize_knowledge_chunk_ids(
        points: Sequence[GuidancePointGroup],
        chunks: Sequence[RetrievedChunk],
    ) -> None:
        available_ids = {chunk.chunk_id for chunk in chunks}
        dropped_count = 0
        for point in points:
            summary_ids = [
                chunk_id
                for chunk_id in point.summary_knowledge_chunk_ids
                if chunk_id in available_ids
            ]
            dropped_count += (
                len(point.summary_knowledge_chunk_ids) - len(summary_ids)
            )
            point.summary_knowledge_chunk_ids = summary_ids
            sanitized_details: list[list[str]] = []
            for detail_ids in point.detail_knowledge_chunk_ids:
                valid_ids = [
                    chunk_id
                    for chunk_id in detail_ids
                    if chunk_id in available_ids
                ]
                dropped_count += len(detail_ids) - len(valid_ids)
                sanitized_details.append(valid_ids)
            point.detail_knowledge_chunk_ids = sanitized_details
        if dropped_count:
            log_metric(
                "citation.legacy_reference_rejected",
                citation_flow="guidance",
                rejected_count=dropped_count,
            )

    @staticmethod
    def _citation_ref_key(
        dimension_name: GuidanceDimensionKey,
    ) -> GuidanceCitationRefKey:
        return {
            "start": "_citation_refs_start",
            "emotion": "_citation_refs_emotion",
            "requirement": "_citation_refs_requirement",
            "plan": "_citation_refs_plan",
        }[dimension_name]

    @staticmethod
    def _citation_ref_payload(
        dimension_name: GuidanceDimensionKey,
        points: Sequence[GuidanceGeneratedPointGroup],
    ) -> GuidanceCitationRefPayload:
        references: GuidanceCitationRefPayload = {}
        for point_index, point in enumerate(points):
            for reference_target, reference in point.resolved_citation_refs():
                target_suffix = (
                    "summary"
                    if reference_target == "summary"
                    else f"details.{reference_target.removeprefix('detail_')}"
                )
                target = (
                    f"dimension_points.{dimension_name}.{point_index}."
                    f"{target_suffix}"
                )
                references.setdefault(target, []).append(
                    reference.model_dump(mode="json")
                )
        return references

    @staticmethod
    def _citation_targets(
        dimension_points: GuidanceDimensionPoints,
    ) -> dict[str, list[str]]:
        targets_by_chunk: dict[str, list[str]] = {}
        for dimension_name in ("start", "emotion", "requirement", "plan"):
            for point_index, point in enumerate(
                getattr(dimension_points, dimension_name)
            ):
                summary_target = (
                    f"dimension_points.{dimension_name}.{point_index}.summary"
                )
                for chunk_id in point.summary_knowledge_chunk_ids:
                    targets_by_chunk.setdefault(chunk_id, []).append(summary_target)
                for detail_index, chunk_ids in enumerate(
                    point.detail_knowledge_chunk_ids
                ):
                    detail_target = (
                        f"dimension_points.{dimension_name}.{point_index}."
                        f"details.{detail_index}"
                    )
                    for chunk_id in chunk_ids:
                        targets_by_chunk.setdefault(chunk_id, []).append(
                            detail_target
                        )
        return targets_by_chunk

    @staticmethod
    def _skill_citation_texts(
        dimension_name: GuidanceDimensionKey,
        points: Sequence[GuidancePointGroup],
    ) -> dict[str, str]:
        texts_by_target: dict[str, str] = {}
        for point_index, point in enumerate(points):
            if point.summary:
                texts_by_target[
                    f"dimension_points.{dimension_name}.{point_index}.summary"
                ] = point.summary
            for detail_index, detail in enumerate(point.details):
                texts_by_target[
                    f"dimension_points.{dimension_name}.{point_index}."
                    f"details.{detail_index}"
                ] = detail
        return texts_by_target

    @staticmethod
    def report_from_sections(
        state: SessionState,
        retrieved_chunks: list[RetrievedChunk],
        sections: Mapping[GuidanceResultKey, GuidanceResultValue],
    ) -> GuidanceReport:
        dimension_points = GuidanceDimensionPoints(
            start=GuidanceAgent._as_point_groups(sections["start_points"]),
            emotion=GuidanceAgent._as_point_groups(sections["emotion_points"]),
            requirement=GuidanceAgent._as_point_groups(
                sections["requirement_points"]
            ),
            plan=GuidanceAgent._as_point_groups(
                sections["plan_points"],
                point_schema=GuidancePlanPointGroup,
            ),
        )
        citation_targets = GuidanceAgent._citation_targets(dimension_points)
        target_texts: dict[str, str] = {}
        for dimension_name in ("start", "emotion", "requirement", "plan"):
            target_texts.update(
                GuidanceAgent._skill_citation_texts(
                    dimension_name,
                    getattr(dimension_points, dimension_name),
                )
            )

        references_by_target: dict[
            str,
            tuple[
                str,
                Sequence[CitationReference | Mapping[str, object]],
            ],
        ] = {}
        for internal_key in GUIDANCE_INTERNAL_RESULT_KEYS:
            raw_references = sections.get(internal_key)
            if not isinstance(raw_references, Mapping):
                continue
            for target, references in raw_references.items():
                if (
                    target in target_texts
                    and isinstance(references, Sequence)
                    and not isinstance(references, (str, bytes))
                ):
                    references_by_target[target] = (
                        target_texts[target],
                        references,
                    )

        skill_router = get_knowledge_skill_router()
        skill_context = build_knowledge_skill_context(
            state,
            include_conversation=False,
        )
        skill_payloads: list[dict[str, object]] = []
        seen_skills: set[str] = set()
        for spec in GUIDANCE_DIMENSION_SPECS:
            active_skills = skill_router.select(
                spec.retrieval_name,
                skill_context,
                emit_metrics=False,
            )
            for payload_item in skill_router.prompt_payload(active_skills):
                source_ref = str(payload_item.get("source_ref") or "")
                if not source_ref or source_ref in seen_skills:
                    continue
                seen_skills.add(source_ref)
                skill_payloads.append(payload_item)

        citations = merge_citations(
            [
                *validated_references_to_citations(
                    retrieved_chunks,
                    skill_payloads,
                    references_by_target,
                ),
                *referenced_chunks_to_citations(
                    retrieved_chunks,
                    citation_targets,
                ),
            ]
        )
        payload = {
            "purpose": GuidanceAgent._as_text(sections["purpose"]),
            "opening_suggestion": GuidanceAgent._as_text(sections["opening_suggestion"]),
            "risk_preview": GuidanceAgent._as_list(sections["risk_preview"]),
            "response_strategies": GuidanceAgent._as_list(sections["response_strategies"]),
            "safer_phrases": GuidanceAgent._as_list(sections["safer_phrases"]),
            "dimension_points": dimension_points.model_dump(mode="json"),
            "session_id": state.session_id,
            "locale": state.locale,
            "intent_id": state.intent.intent_id,
            "primary_motive_id": state.motivation.primary_motive_id if state.motivation else None,
            "secondary_motive_ids": state.motivation.secondary_motive_ids if state.motivation else [],
            "guidance_version": get_config_loader().guidance_version(),
            "culture_version": get_config_loader().culture_version(),
            "citations": [
                citation.model_dump(exclude_none=True)
                for citation in citations
            ],
        }
        evidence_policy, disclaimer = localized_value(
            state.locale,
            _GUIDANCE_REPORT_POLICIES,
        )
        payload.update(
            {
                "evidence_policy": evidence_policy,
                "disclaimer": disclaimer,
            }
        )
        return GuidanceReport.model_validate(payload)

    @staticmethod
    def _as_text(value: GuidanceSectionValue) -> str:
        if not isinstance(value, str):
            raise TypeError("Guidance scalar section must be a string.")
        return value

    @staticmethod
    def _as_list(value: GuidanceSectionValue) -> list[str]:
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise TypeError("Guidance list section must be a string array.")
        return value

    @staticmethod
    def _as_point_groups(
        value: GuidanceResultValue,
        *,
        point_schema: type[GuidancePointGroup] = GuidancePointGroup,
    ) -> list[GuidancePointGroup]:
        if not isinstance(value, list):
            raise TypeError("Guidance point groups must be an array.")
        public_fields = {
            "title",
            "summary",
            "details",
            "summary_knowledge_chunk_ids",
            "detail_knowledge_chunk_ids",
        }
        return [
            point_schema.model_validate(
                {
                    key: field_value
                    for key, field_value in (
                        item.model_dump(mode="json")
                        if hasattr(item, "model_dump")
                        else dict(item)
                    ).items()
                    if key in public_fields
                }
            )
            for item in value
        ]

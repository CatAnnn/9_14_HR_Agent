import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.agents.guidance_agent import (
    GUIDANCE_DIMENSION_ALIGNMENT,
    GUIDANCE_DIMENSION_SPECS,
    GUIDANCE_SECTION_TITLES,
    GuidanceAgent,
    GuidanceEmotionOutput,
    GuidancePlanOutput,
    GuidanceRequirementOutput,
    GuidanceStartOutput,
)
from backend.business_config.loader import get_config_loader
from backend.exceptions.llm_errors import StructuredOutputError
from backend.schemas.guidance import (
    GUIDANCE_DETAIL_MAX_ITEMS,
    GUIDANCE_DETAIL_MIN_ITEMS,
    GUIDANCE_GROUP_MAX_ITEMS,
    GUIDANCE_GROUP_MIN_ITEMS,
    GUIDANCE_LIST_MAX_ITEMS,
    GUIDANCE_LIST_MIN_ITEMS,
    GUIDANCE_POINT_DETAIL_MAX_LENGTH,
    GUIDANCE_POINT_SUMMARY_MAX_LENGTH,
    GuidanceGeneratedPointGroup,
    GuidancePointGroup,
    GuidanceReport,
    guidance_quality_issues,
)
from backend.schemas.intent import IntentConfig, IntentResult
from backend.schemas.profile import EmployeeProfile
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.simulation import (
    BigFivePersonality,
    EmotionState,
    MotivationState,
)
from backend.schemas.state import SessionState
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.guidance_service import GuidanceService


def _dimension_result(schema):
    if schema is GuidanceStartOutput:
        return GuidanceStartOutput(
            points=[
                {
                    "summary": "先对齐沟通事实与边界，再推进后续行动。",
                    "details": [
                        "先说明沟通目标和已确认的绩效结果。",
                        "先确认员工感受，再说明沟通目标。",
                    ],
                },
            ]
        )
    if schema is GuidanceEmotionOutput:
        return GuidanceEmotionOutput(
            points=[
                {
                    "summary": "员工质疑事实时，应先承接疑问并共同核对依据。",
                    "details": [
                        "员工可能要求说明事实来源。",
                        "先邀请员工补充其掌握的信息。",
                    ],
                },
                {
                    "summary": "员工出现明显反应时，应先留出表达空间。",
                    "details": [
                        "留意长时间沉默或语气明显升高。",
                        "先复述感受，再询问是否需要暂停。",
                    ],
                },
                {
                    "summary": "确认理解后，用开放问题共同核对顾虑和事实。",
                    "details": [
                        "提出一个开放问题邀请员工说明最重要的顾虑。",
                        "避免连续追问或用问题暗示既定结论。",
                    ],
                },
            ]
        )
    if schema is GuidanceRequirementOutput:
        return GuidanceRequirementOutput(
            points=[
                {
                    "summary": "先承接感受，再回到可以核对的行为事实。",
                    "details": [
                        "先确认员工的感受已被听见。",
                        "再逐项核对具体行为事实。",
                    ],
                },
                {
                    "summary": "使用既定标准说明差距，并保留员工补充空间。",
                    "details": [
                        "分别说明既定标准与当前差距。",
                        "邀请员工补充背景和待核实信息。",
                    ],
                },
                {
                    "summary": "结合具体行为与影响核对最相关的文化要求。",
                    "details": [
                        "只选择与当前目标最相关的一项文化要求。",
                        "结合具体情境、行为和影响讨论其实际体现。",
                    ],
                },
            ]
        )
    if schema is GuidancePlanOutput:
        return GuidancePlanOutput(
            points=[
                {
                    "summary": "将已确认事实转化为清晰的后续行动与责任。",
                    "details": [
                        "结合适用岗级要求和当前基础明确最关键的发展重点。",
                        "围绕该重点设计实际工作实践并观察后续结果。",
                    ],
                },
            ]
        )
    raise AssertionError(f"Unexpected schema: {schema}")


def _dimension_sections(spec):
    output = _dimension_result(spec.schema)
    points = GuidanceAgent._attach_fixed_titles(
        _guidance_state(),
        spec,
        output.points,
    )
    return {
        **GuidanceAgent._legacy_sections(spec, points),
        spec.point_group_key: [point.model_dump(mode="json") for point in points],
    }


def _guidance_state(session_id: str = "s1") -> SessionState:
    return SessionState(
        session_id=session_id,
        intent=IntentResult(
            intent_id="intent-1",
            config=IntentConfig(
                id="intent-1",
                name="绩效沟通",
            ),
        ),
    )


def _guidance_state_with_runtime_context(session_id: str = "s1") -> SessionState:
    state = _guidance_state(session_id)
    state.setup_ready = True
    state.supplemental_info = "补充信息" * 3000 + "完整结尾"
    state.motivation = MotivationState(
        primary_motive_id="development",
        secondary_motive_ids=["recognition"],
        primary_score=-25,
        secondary_scores={"recognition": 10},
        last_change_reason="不应进入 Guidance",
    )
    state.emotion_state = EmotionState(
        current_anchor_id="defensive",
        last_reason_summary="不应进入 Guidance",
    )
    return state


def test_guidance_prompt_context_keeps_full_info_and_excludes_runtime_scores(monkeypatch):
    monkeypatch.setattr(
        "backend.agents.guidance_agent.get_config_loader",
        lambda: SimpleNamespace(
            company_values=lambda: {"enabled": False, "values": []},
            motives=lambda: {
                "development": {
                    "name": "发展诉求",
                    "description": "希望获得成长机会。",
                },
                "recognition": {
                    "name": "认可诉求",
                    "description": "希望贡献得到确认。",
                },
            },
        ),
    )
    state = _guidance_state_with_runtime_context()
    state.employee_profile = EmployeeProfile(employee_alias="测试员工")
    state.personality = BigFivePersonality(openness=61)

    context = GuidanceAgent._build_context(state, [])
    payload = context.model_dump(mode="json")

    assert context.supplemental_info == state.supplemental_info
    assert context.supplemental_info.endswith("完整结尾")
    assert payload["profile"]["employee_alias"] == "测试员工"
    assert payload["personality"]["openness"] == 61
    assert payload["intent"]["config"]["id"] == "intent-1"
    assert payload["intent"]["config"]["name"] == "绩效沟通"
    assert not {
        "business_goal",
        "red_lines",
        "expected_outcome",
        "employee_agent_hint",
        "coach_focus",
    } & payload["intent"]["config"].keys()
    assert payload["motivation"] == {
        "primary": {
            "id": "development",
            "name": "发展诉求",
            "description": "希望获得成长机会。",
        },
        "secondary": [
            {
                "id": "recognition",
                "name": "认可诉求",
                "description": "希望贡献得到确认。",
            }
        ],
    }
    assert "conversation" not in payload
    assert "emotion_state" not in payload
    assert "primary_score" not in payload["motivation"]
    assert "total_satisfaction" not in payload["motivation"]
    assert "last_change_reason" not in payload["motivation"]


def test_guidance_g9_development_context_enables_career_elements(monkeypatch):
    monkeypatch.setattr(
        "backend.agents.guidance_agent.get_config_loader",
        lambda: SimpleNamespace(
            company_values=lambda: {"enabled": False, "values": []},
            motives=lambda: {},
        ),
    )
    state = _guidance_state_with_runtime_context("career-elements-context")
    state.employee_profile = EmployeeProfile(
        level="G9",
        current_career_elements=["Cross Function"],
    )
    state.intent = IntentResult(
        intent_id="development",
        confidence=1.0,
        reason="test",
        config=IntentConfig(id="development", name="发展型反馈"),
    )

    prompt_context = GuidanceAgent._build_context(state, [])
    service = object.__new__(GuidanceService)
    service.config_loader = SimpleNamespace(company_value_terms=lambda: "")
    retrieval_context = service._retrieval_context(state)

    assert prompt_context.career_elements_applicable is True
    assert prompt_context.current_career_elements == ["Cross Function"]
    assert retrieval_context["career_elements_applicable"] is True
    assert retrieval_context["current_career_elements"] == ["Cross Function"]


@pytest.mark.asyncio
async def test_guidance_retrieval_context_keeps_full_info_and_excludes_runtime_scores():
    calls = []

    class Retrieval:
        async def aretrieve(self, name, context, top_k=None):
            calls.append((name, context, top_k))
            return []

    service = object.__new__(GuidanceService)
    service.config_loader = SimpleNamespace(company_value_terms=lambda: "")
    service.retrieval = Retrieval()
    state = _guidance_state_with_runtime_context("retrieval-context")

    await service._retrieve_dimension_chunks(state, GUIDANCE_DIMENSION_SPECS[0])

    assert len(calls) == 1
    name, context, top_k = calls[0]
    assert name == "guidance_start"
    assert top_k == 8
    assert context["supplemental_info"] == state.supplemental_info
    assert context["motivation"] == {
        "primary_motive_id": "development",
        "secondary_motive_ids": ["recognition"],
    }
    assert "emotion_state" not in context


@pytest.mark.asyncio
async def test_guidance_dimensions_use_independent_rag_before_their_own_llm():
    state = _guidance_state("dimension-rag")
    state.setup_ready = True
    retrieval_calls = []
    llm_chunks = {}
    active = 0
    max_active = 0

    class Retrieval:
        async def aretrieve(self, name, context, top_k=None):
            nonlocal active, max_active
            retrieval_calls.append((name, top_k))
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            return [
                RetrievedChunk(
                    chunk_id=name,
                    source_id=f"{name}.md",
                    title=name,
                    scope="general",
                    text=f"{name} knowledge",
                    score=0.9,
                )
            ]

    service = object.__new__(GuidanceService)
    service.config_loader = SimpleNamespace(company_value_terms=lambda: "")
    service.retrieval = Retrieval()

    async def generate_dimension(session_id, current_state, chunks, spec):
        llm_chunks[spec.key] = [chunk.chunk_id for chunk in chunks]
        return spec, _dimension_sections(spec), 1, None

    service._generate_dimension = generate_dimension

    (
        sections,
        chunks,
        _retrieval_ms,
        llm_ms,
        fallback_dimensions,
    ) = await service._generate_sections_parallel(state.session_id, state)

    expected_names = {
        "start": "guidance_start",
        "emotion": "guidance_emotion",
        "requirement": "guidance_requirement",
        "plan": "guidance_plan",
    }
    assert set(retrieval_calls) == {(name, 8) for name in expected_names.values()}
    assert max_active == 4
    assert llm_chunks == {key: [name] for key, name in expected_names.items()}
    assert fallback_dimensions == []
    assert {chunk.chunk_id for chunk in chunks} == set(expected_names.values())
    assert set(sections) == {
        "purpose",
        "opening_suggestion",
        "risk_preview",
        "response_strategies",
        "safer_phrases",
        "start_points",
        "emotion_points",
        "requirement_points",
        "plan_points",
    }
    assert llm_ms == 1


@pytest.mark.asyncio
async def test_guidance_agent_builds_four_dimensions_with_independent_structured_calls(
    monkeypatch,
):
    calls = []
    active = 0
    max_active = 0

    async def fake_ainvoke_structured_single(self, **kwargs):
        nonlocal active, max_active
        calls.append(kwargs)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return _dimension_result(kwargs["schema"])

    monkeypatch.setattr(
        LangChainLLMService,
        "ainvoke_structured_single",
        fake_ainvoke_structured_single,
    )

    sections = await GuidanceAgent().generate_sections(_guidance_state(), [])

    assert sections["purpose"] == "先对齐沟通事实与边界，再推进后续行动。"
    assert sections["opening_suggestion"] == "先确认员工感受，再说明沟通目标。"
    assert [point["title"] for point in sections["start_points"]] == [
        "开场定调与绩效结果对齐"
    ]
    assert [point["title"] for point in sections["emotion_points"]] == [
        "认真倾听，理解情绪",
        "共情总结",
        "共同探索",
    ]
    assert [point["title"] for point in sections["requirement_points"]] == [
        "目标方向的正确性",
        "结果的突破性",
        "高绩效文化",
    ]
    assert [point["title"] for point in sections["plan_points"]] == ["职位发展"]
    assert sections["emotion_points"][0]["details"] == [
        "员工可能要求说明事实来源。",
        "先邀请员工补充其掌握的信息。",
    ]
    assert len(calls) == 4
    assert max_active == 4
    assert {call["schema"] for call in calls} == {
        GuidanceStartOutput,
        GuidanceEmotionOutput,
        GuidanceRequirementOutput,
        GuidancePlanOutput,
    }
    assert all(call["task_name"] == "guidance" for call in calls)
    assert all(call["stream"] is True for call in calls)
    assert all(call["record_stream_timing"] is True for call in calls)
    assert all(call["payload_normalizer"] for call in calls)
    assert all(call["structured_transport"] == "json_schema" for call in calls)
    assert all(call["json_schema_strict"] is False for call in calls)
    assert all("tools" not in call for call in calls)
    assert all("tool_choice" not in call for call in calls)

    calls_by_schema = {call["schema"]: call for call in calls}
    start_prompt = calls_by_schema[GuidanceStartOutput]["prompt"]
    emotion_prompt = calls_by_schema[GuidanceEmotionOutput]["prompt"]
    requirement_prompt = calls_by_schema[GuidanceRequirementOutput]["prompt"]
    plan_prompt = calls_by_schema[GuidancePlanOutput]["prompt"]
    assert "开场定调与绩效结果对齐" in start_prompt
    assert "details 必须且只能包含两条" in start_prompt
    assert "只返回 points" in start_prompt
    assert "情绪承接、接纳与共情" not in start_prompt
    assert "情绪承接、接纳与共情" in emotion_prompt
    assert "从情绪回归产出与标准" not in emotion_prompt
    assert "从情绪回归产出与标准" in requirement_prompt
    assert "总结与差异化发展计划" in plan_prompt
    assert "【输出契约】" in plan_prompt
    assert "每个 point 的 details 必须且只能包含两条" in plan_prompt
    assert "第一条负责“判断与重点”" in plan_prompt
    assert "第二条负责“实践与验证”" in plan_prompt
    for prompt in (start_prompt, emotion_prompt, requirement_prompt, plan_prompt):
        assert "当前处于面谈开始前" in prompt
        assert "当前谈前指导模块" in prompt
        assert "不是复盘、评分、诊断" in prompt
        assert "对应复盘维度" not in prompt
        assert "dimension_alignment=" not in prompt
        assert "report_task_id" not in prompt
    prompts_by_key = {
        "start": start_prompt,
        "emotion": emotion_prompt,
        "requirement": requirement_prompt,
        "plan": plan_prompt,
    }
    for spec in GUIDANCE_DIMENSION_SPECS:
        prompt = prompts_by_key[spec.key]
        group_range = (
            str(spec.group_min_items)
            if spec.group_min_items == spec.group_max_items
            else f"{spec.group_min_items}-{spec.group_max_items}"
        )
        detail_range = (
            str(spec.detail_min_items)
            if spec.detail_min_items == spec.detail_max_items
            else f"{spec.detail_min_items}-{spec.detail_max_items}"
        )
        assert f"points 只能包含 {group_range} 个" in prompt
        assert f"details 数量必须为 {detail_range} 条" in prompt
        assert "不得生成 title" in prompt
        assert f"不超过 {GUIDANCE_POINT_SUMMARY_MAX_LENGTH} 字" in prompt
        assert f"不超过 {GUIDANCE_POINT_DETAIL_MAX_LENGTH} 字" in prompt
        assert "每条 details 只表达一个" in prompt
        assert "不使用分号、编号或长段落" in prompt
        assert "【可读性与表达】" in prompt
        assert "summary 用一句符合 context.output_locale 的简短、完整句子" in prompt
        assert "先生成 summary，再生成 details" in prompt
        assert "每条 detail 必须是一个完整、简练" in prompt
        assert "内部变量名" in prompt


def test_guidance_dimensions_match_coach_report_configs():
    config_files = {
        "opening_suggestion": "start.yaml",
        "risk_preview": "emotion.yaml",
        "response_strategies": "requirement.yaml",
        "safer_phrases": "plan.yaml",
    }
    loader = get_config_loader()

    assert [spec.key for spec in GUIDANCE_DIMENSION_SPECS] == [
        "start",
        "emotion",
        "requirement",
        "plan",
    ]
    for field, filename in config_files.items():
        dimension = loader.coach_config(filename)["dimension"]
        alignment = GUIDANCE_DIMENSION_ALIGNMENT[field]
        assert alignment["dimension_id"] == dimension["id"]
        assert alignment["dimension_name"] == dimension["name"]
        assert GUIDANCE_SECTION_TITLES[field] == dimension["name"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["schema_validation", "timeout"])
async def test_guidance_agent_leaves_retry_orchestration_to_service(
    monkeypatch,
    error_code,
):
    calls = []
    skill_router = SimpleNamespace(
        select=lambda *args, **kwargs: [],
        prompt_payload=lambda skills: [],
    )

    async def fake_ainvoke_structured_single(self, **kwargs):
        calls.append(kwargs)
        raise StructuredOutputError(error_code, "invalid output")

    monkeypatch.setattr(
        "backend.agents.guidance_agent.get_knowledge_skill_router",
        lambda: skill_router,
    )
    monkeypatch.setattr(
        LangChainLLMService,
        "ainvoke_structured_single",
        fake_ainvoke_structured_single,
    )

    with pytest.raises(StructuredOutputError) as exc_info:
        await GuidanceAgent().generate_dimension(
            _guidance_state(),
            [],
            GUIDANCE_DIMENSION_SPECS[1],
        )

    assert exc_info.value.code == error_code
    assert len(calls) == 1
    assert calls[0]["model"] is None


@pytest.mark.asyncio
async def test_guidance_agent_retry_mode_is_one_corrected_attempt(monkeypatch):
    calls = []
    skill_router = SimpleNamespace(
        select=lambda *args, **kwargs: [],
        prompt_payload=lambda skills: [],
    )

    async def fake_ainvoke_structured_single(self, **kwargs):
        calls.append(kwargs)
        return _dimension_result(kwargs["schema"])

    monkeypatch.setattr(
        "backend.agents.guidance_agent.get_knowledge_skill_router",
        lambda: skill_router,
    )
    monkeypatch.setattr(
        LangChainLLMService,
        "ainvoke_structured_single",
        fake_ainvoke_structured_single,
    )

    sections = await GuidanceAgent().generate_dimension(
        _guidance_state(),
        [],
        GUIDANCE_DIMENSION_SPECS[1],
        retry=True,
        retry_model="deepseek-v4-pro",
    )

    assert sections["risk_preview"]
    assert len(calls) == 1
    assert calls[0]["stream"] is True
    assert calls[0]["record_stream_timing"] is True
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["enable_thinking"] is False
    assert calls[0]["structured_transport"] == "json_schema"
    assert "上一次输出因结构格式、超时或临时网关错误被拒绝" in calls[0]["prompt"]
    assert calls[0]["json_schema_strict"] is False
    assert "禁止英文半角双引号" not in calls[0]["prompt"]
    assert calls[0]["model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_guidance_agent_preserves_dangling_text_as_quality_warning():
    class LLM:
        async def ainvoke_structured_single(self, **kwargs):
            payload = _dimension_result(GuidanceEmotionOutput).model_dump()
            payload["points"][0]["details"][0] = "管理者可能把沟通转向"
            return GuidanceEmotionOutput.model_validate(payload)

    sections = await GuidanceAgent(llm_service=LLM()).generate_dimension(
        _guidance_state(),
        [],
        GUIDANCE_DIMENSION_SPECS[1],
    )

    assert sections["emotion_points"][0]["details"][0] == "管理者可能把沟通转向"
    assert "points[0].details[0]:dangling_fragment" in guidance_quality_issues(
        {"points": sections["emotion_points"]}
    )


def test_guidance_sections_keep_split_items_but_report_quality_warning():
    sections = {
        "purpose": "对齐事实与下一步行动。",
        "opening_suggestion": "先确认员工感受，再说明沟通目标。",
        "risk_preview": [
            "员工听到改进反馈时可能迅速将",
            "事情理解为对人的否定。",
            "员工可能选择沉默退缩。",
        ],
        "response_strategies": [
            "先确认员工的感受已被听见。",
            "再共同核对具体行为事实。",
            "最后共同确认下一步行动。",
        ],
        "safer_phrases": [
            "我们先确认沟通目标。",
            "我们再共同核对事实依据。",
            "我们最后约定行动计划。",
        ],
    }

    issues = guidance_quality_issues(sections)
    assert sections["risk_preview"][0] == "员工听到改进反馈时可能迅速将"
    assert "risk_preview[0]:missing_terminal_punctuation" in issues


def test_guidance_report_keeps_incomplete_persisted_items_with_quality_warnings():
    report = GuidanceReport.model_validate(
        {
            "session_id": "s1",
            "intent_id": "intent-1",
            "purpose": "对齐事实与下一步行动。",
            "opening_suggestion": "先确认员工感受，再说明沟通目标。",
            "risk_preview": ["避免把", "事情说成人格问题。", "先核对事实。"],
            "response_strategies": [
                "先确认员工的感受已被听见。",
                "再共同核对具体行为事实。",
                "最后共同确认下一步行动。",
            ],
            "safer_phrases": [
                "我们先确认沟通目标。",
                "我们再共同核对事实依据。",
                "我们最后约定行动计划。",
            ],
        }
    )

    issues = guidance_quality_issues(report.model_dump(mode="json"))
    assert report.risk_preview[0] == "避免把"
    assert "risk_preview[0]:short_text" in issues
    assert "risk_preview[0]:missing_terminal_punctuation" in issues


def test_guidance_output_rejects_extra_fields_and_out_of_range_lists():
    valid_payload = _dimension_result(GuidanceEmotionOutput).model_dump()
    extra_payload = {**valid_payload, "unexpected_section": "不应被静默忽略。"}
    with pytest.raises(ValidationError):
        GuidanceEmotionOutput.model_validate(extra_payload)
    with pytest.raises(ValidationError):
        GuidanceEmotionOutput.model_validate({"points": []})
    with pytest.raises(ValidationError):
        GuidanceEmotionOutput.model_validate(
            {
                "points": [
                    {"summary": f"概括当前要点{index}。", "details": ["第一条。", "第二条。"]}
                    for index in range(GUIDANCE_GROUP_MAX_ITEMS + 1)
                ]
            }
        )
    with pytest.raises(ValidationError):
        GuidanceEmotionOutput.model_validate(
            {"points": [{"summary": "先核对事实依据。", "details": ["只有一条。"]}]}
        )
    missing_summary_payload = _dimension_result(
        GuidanceEmotionOutput
    ).model_dump()
    del missing_summary_payload["points"][0]["summary"]
    with pytest.raises(ValidationError) as exc_info:
        GuidanceEmotionOutput.model_validate(missing_summary_payload)

    assert [
        (tuple(error["loc"]), error["type"])
        for error in exc_info.value.errors()
    ] == [(("points", 0, "summary"), "missing")]


def test_guidance_response_schema_exposes_strict_shape_and_item_bounds():
    schema = GuidanceEmotionOutput.model_json_schema()
    emotion_spec = next(
        spec for spec in GUIDANCE_DIMENSION_SPECS if spec.key == "emotion"
    )
    points_schema = schema["properties"]["points"]
    point_schema_name = points_schema["items"]["$ref"].rsplit("/", 1)[-1]
    point_schema = schema["$defs"][point_schema_name]
    summary_schema = point_schema["properties"]["summary"]
    details_schema = point_schema["properties"]["details"]
    assert GUIDANCE_POINT_DETAIL_MAX_LENGTH == 430

    assert schema["additionalProperties"] is False
    assert points_schema["minItems"] == emotion_spec.group_min_items
    assert points_schema["maxItems"] == emotion_spec.group_max_items
    assert point_schema["additionalProperties"] is False
    assert set(point_schema["required"]) == {
        "summary",
        "details",
    }
    assert "title" not in point_schema["properties"]
    assert "maxLength" not in summary_schema
    assert details_schema["minItems"] == emotion_spec.detail_min_items
    assert details_schema["maxItems"] == emotion_spec.detail_max_items
    assert "maxLength" not in details_schema["items"]
    assert {
        "citation_targets",
        "citation_source_refs",
        "citation_highlight_texts",
        "citation_source_quotes",
    }.issubset(point_schema["properties"])
    assert "summary_knowledge_chunk_ids" not in point_schema["properties"]
    assert "detail_knowledge_chunk_ids" not in point_schema["properties"]
    assert "summary_citation_refs" not in point_schema["properties"]
    assert "detail_citation_refs" not in point_schema["properties"]


@pytest.mark.parametrize(
    ("spec_index", "expected_titles"),
    [
        (0, ["开场定调与绩效结果对齐"]),
        (1, ["认真倾听，理解情绪", "共情总结", "共同探索"]),
        (2, ["目标方向的正确性", "结果的突破性", "高绩效文化"]),
        (3, ["职位发展"]),
    ],
)
def test_guidance_discards_model_titles_and_injects_configured_titles(
    spec_index,
    expected_titles,
):
    spec = GUIDANCE_DIMENSION_SPECS[spec_index]
    payload = _dimension_result(spec.schema).model_dump(mode="json")
    for index, point in enumerate(payload["points"]):
        point["title"] = f"模型生成的错误标题{index}"

    with pytest.raises(ValidationError):
        spec.schema.model_validate(payload)

    normalized, repair_steps = GuidanceAgent._discard_model_generated_titles(
        payload
    )
    result = spec.schema.model_validate(normalized)
    generated_points = GuidanceAgent._attach_fixed_titles(
        _guidance_state(f"fixed-title-{spec.key}"),
        spec,
        result.points,
    )

    assert repair_steps == ["discarded_model_generated_point_titles"]
    assert all("title" not in point for point in normalized["points"])
    assert [point.title for point in generated_points] == expected_titles


@pytest.mark.parametrize("intent_id", ["exit", "improvement_exit"])
def test_exit_guidance_plan_uses_backend_fixed_title(intent_id):
    state = _guidance_state(f"fixed-title-{intent_id}")
    state.intent = IntentResult(
        intent_id=intent_id,
        config=IntentConfig(id=intent_id, name="退出类反馈"),
    )
    model_output = _dimension_result(GuidancePlanOutput)

    generated_points = GuidanceAgent._attach_fixed_titles(
        state,
        GUIDANCE_DIMENSION_SPECS[3],
        model_output.points,
    )

    assert [point.title for point in generated_points] == ["职位发展"]


@pytest.mark.parametrize(
    ("schema", "point_count", "detail_count"),
    [
        (GuidanceStartOutput, 1, 2),
        (GuidanceEmotionOutput, 3, 2),
        (GuidanceRequirementOutput, 3, 2),
        (GuidancePlanOutput, 1, 2),
    ],
)
def test_guidance_dimension_schemas_match_prompt_contracts(
    schema,
    point_count,
    detail_count,
):
    output = _dimension_result(schema)

    assert len(output.points) == point_count
    assert all("title" not in point.model_dump() for point in output.points)
    assert all(len(point.details) == detail_count for point in output.points)

    json_schema = schema.model_json_schema()
    points_schema = json_schema["properties"]["points"]
    point_schema_name = points_schema["items"]["$ref"].rsplit("/", 1)[-1]
    details_schema = json_schema["$defs"][point_schema_name]["properties"][
        "details"
    ]
    assert points_schema["minItems"] == point_count
    assert points_schema["maxItems"] == (
        2 if schema is GuidancePlanOutput else point_count
    )
    assert details_schema["minItems"] == detail_count
    assert details_schema["maxItems"] == detail_count


@pytest.mark.parametrize(
    "details",
    [
        ["只生成一条建议。"],
        ["第一条建议。", "第二条建议。", "不应生成第三条建议。"],
    ],
)
def test_guidance_plan_rejects_detail_counts_outside_current_prompt(details):
    with pytest.raises(ValidationError):
        GuidancePlanOutput.model_validate(
            {
                "points": [
                    {
                        "summary": "根据当前基础形成发展重点和后续实践。",
                        "details": details,
                    }
                ]
            }
        )


def test_guidance_plan_fixed_titles_use_authoritative_career_applicability():
    one_point = _dimension_result(GuidancePlanOutput).points
    two_points = GuidancePlanOutput.model_validate(
        {
            "points": [
                *[point.model_dump(mode="json") for point in one_point],
                {
                    "summary": "选择最相关的职业要素并形成实践验证路径。",
                    "details": [
                        "结合当前发展要求确认最相关的职业要素和已有基础。",
                        "围绕该要素形成实践动作并观察后续结果。",
                    ],
                },
            ]
        }
    ).points

    ordinary_state = _guidance_state("plan-no-career")
    ordinary_points = GuidanceAgent._attach_fixed_titles(
        ordinary_state,
        GUIDANCE_DIMENSION_SPECS[3],
        one_point,
    )
    assert [point.title for point in ordinary_points] == ["职位发展"]
    with pytest.raises(StructuredOutputError, match="title contract"):
        GuidanceAgent._attach_fixed_titles(
            ordinary_state,
            GUIDANCE_DIMENSION_SPECS[3],
            two_points,
        )

    development_state = _guidance_state("plan-career")
    development_state.intent = IntentResult(
        intent_id="development",
        config=IntentConfig(id="development", name="发展型反馈"),
    )
    development_state.employee_profile = EmployeeProfile(
        employee_alias="测试员工",
        level="G9",
    )
    development_points = GuidanceAgent._attach_fixed_titles(
        development_state,
        GUIDANCE_DIMENSION_SPECS[3],
        two_points,
    )
    assert [point.title for point in development_points] == [
        "职位发展",
        "Career Elements",
    ]
    with pytest.raises(StructuredOutputError, match="title contract"):
        GuidanceAgent._attach_fixed_titles(
            development_state,
            GUIDANCE_DIMENSION_SPECS[3],
            one_point,
        )


def test_guidance_retry_prompts_use_each_dimension_exact_contract():
    agent = GuidanceAgent()
    state = _guidance_state("retry-contracts")

    for spec in GUIDANCE_DIMENSION_SPECS:
        prepared = agent.prepare_dimension(state, [], spec)
        expected_points = 1 if spec.key in {"start", "plan"} else 3
        assert f"points 数量必须为 {expected_points} 个" in prepared.retry_prompt
        assert (
            f"每个 details 数量必须为 {spec.detail_min_items} 条"
            in prepared.retry_prompt
        )

    assert "每个 point 不得输出 title，显示标题由后端按固定顺序附加" in (
        agent.prepare_dimension(state, [], GUIDANCE_DIMENSION_SPECS[3]).retry_prompt
    )


def test_guidance_detail_soft_target_does_not_truncate_long_text():
    valid_detail = "建" * (GUIDANCE_POINT_DETAIL_MAX_LENGTH + 100) + "。"
    output = GuidancePlanOutput.model_validate(
        {
            "points": [
                {
                    "summary": "结合岗位要求明确下一步发展重点。",
                    "details": [valid_detail, "围绕该重点通过实际工作验证后续进展。"],
                }
            ]
        }
    )
    assert output.points[0].details[0] == valid_detail


def test_guidance_summary_soft_target_does_not_truncate_long_text():
    valid_summary = "概" * (GUIDANCE_POINT_SUMMARY_MAX_LENGTH + 100) + "。"
    output = GuidancePlanOutput.model_validate(
        {
            "points": [
                {
                    "summary": valid_summary,
                    "details": [
                        "结合当前基础明确下一步最关键的发展重点。",
                        "围绕该重点通过实际工作验证后续进展。",
                    ],
                }
            ]
        }
    )
    assert output.points[0].summary == valid_summary


def test_stored_guidance_report_accepts_legacy_points_without_summary():
    legacy_group = {
        "title": "旧版要点",
        "details": ["旧版第一条建议。", "旧版第二条建议。"],
    }
    report = GuidanceReport.model_validate(
        {
            "session_id": "s1",
            "intent_id": "intent-1",
            "purpose": "对齐事实与下一步行动。",
            "opening_suggestion": "先说明沟通目的，再邀请员工表达。",
            "risk_preview": ["提前核对关键事实。"],
            "response_strategies": ["先承接员工观点，再共同核对事实。"],
            "safer_phrases": ["我们先把双方的信息放在一起核对。"],
            "dimension_points": {
                "start": [legacy_group],
                "emotion": [legacy_group],
                "requirement": [legacy_group],
                "plan": [
                    {
                        **legacy_group,
                        "details": ["旧版计划只有一条综合建议。"],
                    }
                ],
            },
        }
    )

    assert report.dimension_points is not None
    assert report.dimension_points.start[0].summary is None
    assert report.dimension_points.plan[0].details == ["旧版计划只有一条综合建议。"]


def test_invalid_cached_guidance_report_is_treated_as_cache_miss():
    def invalid_report(_session_id):
        return GuidanceReport.model_validate(
            {
                "session_id": "s1",
                "intent_id": "intent-1",
                "purpose": "未写完",
                "opening_suggestion": "先确认员工感受，再说明沟通目标。",
                "risk_preview": [],
                "response_strategies": [],
                "safer_phrases": [],
            }
        )

    service = object.__new__(GuidanceService)
    service.report_repo = SimpleNamespace(get_guidance=invalid_report)

    assert service._cached_report("s1") is None


def test_prose_warning_does_not_hide_cached_guidance_report():
    version = get_config_loader().guidance_version()
    report = GuidanceReport(
        session_id="s1",
        intent_id="intent-1",
        guidance_version=version,
        purpose="对齐事实与下一步行动。",
        opening_suggestion="先确认员工感受，再说明沟通目标。",
        risk_preview=["管理者可能把沟通转向"],
        response_strategies=["建议先核对已确认的事实。"],
        safer_phrases=["建议共同记录下一步安排。"],
    )
    service = object.__new__(GuidanceService)
    service.config_loader = SimpleNamespace(
        guidance_version=lambda: version,
        culture_version=lambda: None,
    )
    service.report_repo = SimpleNamespace(get_guidance=lambda _session_id: report)

    assert service._cached_report("s1") is report


def test_guidance_get_rejects_stale_report_so_client_can_regenerate():
    report = GuidanceReport(
        session_id="s1",
        intent_id="intent-1",
        guidance_version="guidance-stale",
        purpose="对齐事实与下一步行动。",
        opening_suggestion="先确认员工感受，再说明沟通目标。",
        risk_preview=["先识别员工可能出现的情绪触发。"],
        response_strategies=["建议先核对已确认的事实。"],
        safer_phrases=["建议共同记录下一步安排。"],
    )
    service = object.__new__(GuidanceService)
    service.config_loader = SimpleNamespace(
        guidance_version=lambda: "guidance-current",
        culture_version=lambda: None,
    )
    service.report_repo = SimpleNamespace(get_guidance=lambda _session_id: report)
    service.session_service = SimpleNamespace(
        get_session=lambda _session_id: SessionState(session_id="s1")
    )

    with pytest.raises(KeyError, match="stale"):
        service.get("s1")


def test_guidance_sections_do_not_extract_points_from_multiline_text():
    with pytest.raises(ValidationError, match="Input should be a valid list"):
        GuidanceReport(
            session_id="s1",
            intent_id="intent-1",
            purpose="对齐事实与下一步行动",
            opening_suggestion="先确认员工感受，再说明沟通目标。",
            risk_preview="1. 避免无证据定性。\n2. 避免替公司作承诺。\n3. 避免忽视员工情绪。",  # type: ignore[arg-type]
            response_strategies=["使用具体行为事实。"],
            safer_phrases=["我们先一起核对事实。"],
        )


def test_guidance_sections_preserve_complete_model_items_in_order():
    sections = GuidanceReport(
        session_id="s1",
        intent_id="intent-1",
        purpose="对齐事实与下一步行动。",
        opening_suggestion="先确认员工感受，再说明沟通目标。",
        risk_preview=[
            "员工可能质疑评级的公平性，管理者需准备事实依据。",
            "员工可能把绩效差距归因于外部因素，需区分责任边界。",
            "管理者若过度软化反馈，员工可能低估问题的严重性。",
        ],
        response_strategies=[
            "先逐项列出目标与实际结果，只说事实不评价人。",
            "当员工提出公平性质疑时，先承接情绪再邀请其补充背景。",
            "最后明确设定本次改进窗口期的量化标准。",
        ],
        safer_phrases=[
            "我们先一起核对已经确认的事实。",
            "我们共同记录下一步行动和各自责任。",
            "我们在确认审批边界后再约定跟进安排。",
        ],
    )

    assert sections.risk_preview == [
        "员工可能质疑评级的公平性，管理者需准备事实依据。",
        "员工可能把绩效差距归因于外部因素，需区分责任边界。",
        "管理者若过度软化反馈，员工可能低估问题的严重性。",
    ]
    assert sections.response_strategies == [
        "先逐项列出目标与实际结果，只说事实不评价人。",
        "当员工提出公平性质疑时，先承接情绪再邀请其补充背景。",
        "最后明确设定本次改进窗口期的量化标准。",
    ]


def test_guidance_report_preserves_fragments_and_reports_them():
    report = GuidanceReport.model_validate(
        {
            "session_id": "s1",
            "intent_id": "intent-1",
            "purpose": "对齐事实与下一步行动。",
            "opening_suggestion": "先确认员工感受，再说明沟通目标。",
            "risk_preview": [
                "管理者可能不自觉地将反馈滑向",
                "抽象评价会引发员工防御。",
                "管理者应提前准备行为证据。",
            ],
            "response_strategies": [
                "先对齐实际交付结果。",
                "再共同核对关键行动。",
                "最后共同确认下一步安排。",
            ],
            "safer_phrases": [
                "我们先一起核对已经确认的事实。",
                "我们共同记录下一步行动和各自责任。",
                "我们在确认审批边界后再约定跟进安排。",
            ],
        }
    )

    assert report.risk_preview[0] == "管理者可能不自觉地将反馈滑向"
    assert (
        "risk_preview[0]:missing_terminal_punctuation"
        in guidance_quality_issues(report.model_dump(mode="json"))
    )


def test_guidance_report_preserves_duplicate_or_whitespace_items_with_warnings():
    valid_points = [
        "我们先一起核对已经确认的事实。",
        "我们共同记录下一步行动和各自责任。",
        "我们在确认审批边界后再约定跟进安排。",
    ]
    report = GuidanceReport.model_validate(
        {
            "session_id": "s1",
            "intent_id": "intent-1",
            "purpose": "对齐事实与下一步行动。",
            "opening_suggestion": "先确认员工感受，再说明沟通目标。",
            "risk_preview": valid_points,
            "response_strategies": valid_points,
            "safer_phrases": [
                " 保留模型原始空格。",
                "重复分点不能进入最终报告。",
                "重复分点不能进入最终报告。",
            ],
        }
    )

    issues = guidance_quality_issues(report.model_dump(mode="json"))
    assert report.safer_phrases[0].startswith(" ")
    assert "safer_phrases:duplicate_items" in issues
    assert "safer_phrases[0]:surrounding_whitespace" in issues


def test_guidance_sections_report_dangling_tail_without_rewriting_it():
    item = "若面谈前缺乏书面绩效记录，员工可能质疑评价依据，导致谈话陷入"
    payload = _dimension_result(GuidanceEmotionOutput).model_dump()
    payload["points"][0]["details"][0] = item
    output = GuidanceEmotionOutput.model_validate(payload)

    assert output.points[0].details[0] == item
    assert (
        "points[0].details[0]:missing_terminal_punctuation"
        in guidance_quality_issues(output.model_dump(mode="json"))
    )


def test_guidance_sections_report_incomplete_scalar_without_rewriting_it():
    output = GuidanceStartOutput(
        points=[
            {
                "summary": "开场先说明沟通目标和事实边界。",
                "details": [
                    "本次沟通主要包括事实核对",
                    "先确认员工感受，再说明沟通目标。",
                ],
            }
        ]
    )

    assert output.points[0].details[0] == "本次沟通主要包括事实核对"
    assert (
        "points[0].details[0]:missing_terminal_punctuation"
        in guidance_quality_issues(output.model_dump(mode="json"))
    )


def test_guidance_sections_report_unclosed_straight_quote():
    payload = _dimension_result(GuidanceEmotionOutput).model_dump()
    payload["points"][0]["details"][0] = '建议 Manager 说"我们先核对事实。'
    output = GuidanceEmotionOutput.model_validate(payload)

    assert (
        "points[0].details[0]:unbalanced_delimiters"
        in guidance_quality_issues(output.model_dump(mode="json"))
    )


def test_guidance_stream_sections_forward_model_arrays_directly():
    model_points = [
        "员工若质疑事实依据，建议先邀请其补充信息。",
        "员工若出现明显沉默，建议先确认其是否需要时间。",
        "员工若表达强烈不满，建议先复述感受再核对事实。",
    ]
    report = GuidanceReport(
        session_id="s1",
        intent_id="intent-1",
        purpose="对齐事实与下一步行动。",
        opening_suggestion="先确认员工感受，再说明沟通目标。",
        risk_preview=model_points,
        response_strategies=[
            "先确认情绪已被听见，再逐项核对具体行为事实。",
            "分别说明既定标准与当前差距，并邀请员工补充背景。",
            "明确标记仍待核实的信息，再共同确认下一步结果。",
        ],
        safer_phrases=[
            "我们先一起核对已经确认的事实。",
            "我们共同记录下一步行动和各自责任。",
            "我们在确认审批边界后再约定跟进安排。",
        ],
    )

    section_values = {
        key: value
        for key, _title, value in GuidanceService._stream_sections(report)
    }

    assert section_values["purpose"] == report.purpose
    assert section_values["risk_preview"] is report.risk_preview
    assert section_values["risk_preview"] == model_points


@pytest.mark.asyncio
async def test_guidance_stream_emits_each_dimension_when_its_call_completes():
    state = _guidance_state("stream-four-dimensions")
    state.setup_ready = True
    completion_delays = {
        "start": 0.04,
        "emotion": 0.0,
        "requirement": 0.02,
        "plan": 0.01,
    }

    class DelayedAgent:
        def __init__(self):
            self.started = []
            self.chunk_ids = {}

        async def generate_dimension(self, current_state, chunks, spec):
            self.started.append(spec.key)
            self.chunk_ids[spec.key] = [chunk.chunk_id for chunk in chunks]
            await asyncio.sleep(completion_delays[spec.key])
            return _dimension_sections(spec)

        @staticmethod
        def report_from_sections(current_state, chunks, sections):
            return GuidanceAgent.report_from_sections(current_state, chunks, sections)

    class NoopScheduler:
        @asynccontextmanager
        async def slot(self, **kwargs):
            yield None

    settings = SimpleNamespace(
        chat_url="https://model.example/v1/chat/completions",
        model_for_task=lambda task_name: "guidance-model",
    )
    retrieval_calls = []

    class Retrieval:
        async def aretrieve(self, name, context, top_k=None):
            retrieval_calls.append(name)
            return [
                RetrievedChunk(
                    chunk_id=name,
                    source_id=f"{name}.md",
                    title=name,
                    scope="general",
                    text=f"{name} knowledge",
                    score=0.9,
                )
            ]

    agent = DelayedAgent()
    service = object.__new__(GuidanceService)
    service.session_service = SimpleNamespace(get_session=lambda session_id: state)
    service.report_repo = SimpleNamespace()
    service.retrieval = Retrieval()
    service.agent = agent
    service.model_scheduler = NoopScheduler()
    service.settings = settings
    service.config_loader = SimpleNamespace(company_value_terms=lambda: "")
    service._cached_report = lambda session_id: None
    service._save_report_state = lambda current_state, report: current_state

    events = [event async for event in service.stream_generate(state.session_id)]

    section_done_keys = [
        event["key"]
        for event in events
        if event["event"] == "section_done"
    ]
    assert set(agent.started) == {"start", "emotion", "requirement", "plan"}
    assert set(retrieval_calls) == {
        "guidance_start",
        "guidance_emotion",
        "guidance_requirement",
        "guidance_plan",
    }
    assert agent.chunk_ids == {
        "start": ["guidance_start"],
        "emotion": ["guidance_emotion"],
        "requirement": ["guidance_requirement"],
        "plan": ["guidance_plan"],
    }
    assert section_done_keys == [
        "risk_preview",
        "safer_phrases",
        "response_strategies",
        "purpose",
        "opening_suggestion",
    ]
    section_done_events = [
        event for event in events if event["event"] == "section_done"
    ]
    assert all(event["point_groups"] for event in section_done_events)
    assert events[-1]["event"] == "done"
    assert events[-1]["complete"] is True
    assert events[-1]["report"]["dimension_points"]["start"][0]["title"] == (
        "开场定调与绩效结果对齐"
    )
    assert events[-1]["report"]["citations"] == []


def test_guidance_report_links_only_explicit_knowledge_refs_with_full_text():
    sections = {}
    for spec in GUIDANCE_DIMENSION_SPECS:
        sections.update(_dimension_sections(spec))
    start_point = sections["start_points"][0]
    start_point["summary_knowledge_chunk_ids"] = ["chunk-guidance"]
    start_point["detail_knowledge_chunk_ids"] = [["chunk-guidance"], []]
    original_text = "知识库完整原文" * 80
    chunk = RetrievedChunk(
        chunk_id="chunk-guidance",
        source_id="source-guidance",
        title="开场沟通方法",
        scope="general",
        text=original_text,
    )

    report = GuidanceAgent.report_from_sections(
        _guidance_state(),
        [chunk],
        sections,
    )

    assert len(report.citations) == 1
    citation = report.citations[0]
    assert citation.chunk_id == "chunk-guidance"
    assert citation.quote == original_text
    assert citation.targets == [
        "dimension_points.start.0.summary",
        "dimension_points.start.0.details.0",
    ]


def test_guidance_report_preserves_validated_exact_anchor():
    sections = {}
    for spec in GUIDANCE_DIMENSION_SPECS:
        sections.update(_dimension_sections(spec))
    target = "dimension_points.start.0.summary"
    source_quote = "对齐沟通事实与边界后，再推进后续行动。"
    sections["_citation_refs_start"] = {
        target: [
            {
                "source_ref": "chunk-guidance",
                "highlight_text": "对齐沟通事实与边界",
                "source_quote": source_quote,
            }
        ]
    }
    chunk = RetrievedChunk(
        chunk_id="chunk-guidance",
        source_id="source-guidance",
        title="开场沟通方法",
        scope="development_dialog",
        text=f"# 开场方法\n{source_quote}\n邀请员工补充其理解。",
    )

    report = GuidanceAgent.report_from_sections(
        _guidance_state(),
        [chunk],
        sections,
    )

    assert len(report.citations) == 1
    anchor = report.citations[0].anchors[0]
    assert anchor.target == target
    assert anchor.highlight_text == "对齐沟通事实与边界"
    assert anchor.source_quote == source_quote
    assert "开场方法" in anchor.source_context


def test_guidance_invalid_anchor_falls_back_to_target_only_citation():
    sections = {}
    for spec in GUIDANCE_DIMENSION_SPECS:
        sections.update(_dimension_sections(spec))
    target = "dimension_points.start.0.summary"
    sections["_citation_refs_start"] = {
        target: [
            {
                "source_ref": "chunk-guidance",
                "highlight_text": "正文中并不存在的短语",
                "source_quote": "对齐沟通事实与边界后，再推进后续行动。",
            }
        ]
    }
    chunk = RetrievedChunk(
        chunk_id="chunk-guidance",
        source_id="source-guidance",
        title="开场沟通方法",
        scope="development_dialog",
        text="对齐沟通事实与边界后，再推进后续行动。",
    )

    report = GuidanceAgent.report_from_sections(
        _guidance_state(),
        [chunk],
        sections,
    )

    assert len(report.citations) == 1
    assert report.citations[0].chunk_id == "chunk-guidance"
    assert report.citations[0].targets == [target]
    assert report.citations[0].anchors == []


def test_guidance_drops_unavailable_legacy_knowledge_chunk_id():
    point = GuidancePointGroup.model_validate(
        {
            "title": "沟通目标与边界",
            "summary": "先对齐沟通事实与边界，再推进后续行动。",
            "details": ["先核对事实。", "再确认后续行动。"],
            "summary_knowledge_chunk_ids": ["missing-chunk"],
            "detail_knowledge_chunk_ids": [[], []],
        }
    )

    GuidanceAgent._sanitize_knowledge_chunk_ids([point], [])

    assert point.summary_knowledge_chunk_ids == []
    assert point.detail_knowledge_chunk_ids == [[], []]


def test_guidance_flat_citation_refs_derive_compatible_fields():
    point = GuidanceGeneratedPointGroup.model_validate(
        {
            "title": "沟通目标与边界",
            "summary": "先对齐沟通事实与边界，再推进后续行动。",
            "details": ["先核对事实。", "再确认后续行动。"],
            "citation_targets": ["summary", "detail_0", "detail_1"],
            "citation_source_refs": [
                "chunk-guidance",
                "chunk-guidance",
                "skill:feedback:1",
            ],
            "citation_highlight_texts": [
                "对齐沟通事实与边界",
                "先核对事实",
                "确认后续行动",
            ],
            "citation_source_quotes": [
                "对齐沟通事实与边界后，再推进后续行动。",
                "先核对事实，再确认后续行动。",
                "确认后续行动。",
            ],
        }
    )
    stored = GuidanceAgent._stored_point_group(point)
    references = GuidanceAgent._citation_ref_payload("start", [point])

    assert stored.summary_knowledge_chunk_ids == []
    assert stored.detail_knowledge_chunk_ids == []
    assert set(references) == {
        "dimension_points.start.0.summary",
        "dimension_points.start.0.details.0",
        "dimension_points.start.0.details.1",
    }
    assert all(
        "target" not in reference
        for target_references in references.values()
        for reference in target_references
    )

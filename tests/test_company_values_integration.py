from __future__ import annotations

import pytest

from backend.agents import guidance_agent as guidance_agent_module
from backend.business_config.loader import BusinessConfigLoader
from backend.schemas.guidance import GuidanceReport
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.state import SessionState
from backend.services.guidance_service import GuidanceService
from backend.services.knowledge_skill_service import KnowledgeSkillRouter


ACTIVE_COMPANY_VALUES = {
    "version": "culture-v1",
    "enabled": True,
    "company_name": "测试公司",
    "culture_name": {
        "en": "Test High Performance Culture",
        "zh": "测试高绩效文化",
    },
    "values": [
        {
            "id": "respect_and_trust",
            "name": "Respect and trust",
            "name_zh": "尊重与信任",
            "definition": "在困难沟通中尊重对方并用事实建立信任。",
            "desired_behaviors": ["Listen before responding"],
            "desired_behaviors_zh": ["先倾听并确认员工关切"],
            "anti_patterns": ["打断或否定员工感受"],
            "manager_applications": ["先复述关切，再说明事实和下一步"],
            "source_refs": ["culture/values.md"],
        }
    ],
}


class ActiveCultureLoader:
    def company_values(self):
        return ACTIVE_COMPANY_VALUES

    def company_values_enabled(self):
        return True

    def company_value_terms(self):
        return "Test High Performance Culture 测试高绩效文化 Respect and trust 尊重与信任"

    def culture_version(self):
        return "culture-v1"


def culture_chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id="culture-1",
        source_id="culture/values.md",
        title="公司价值观",
        scope="culture",
        text="尊重与信任要求管理者先倾听，再基于事实回应。",
        score=0.95,
    )


def general_chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id="general-1",
        source_id="general/feedback.md",
        title="绩效反馈",
        scope="general",
        text="绩效反馈应围绕事实、影响和下一步展开。",
        score=0.8,
    )


def test_company_values_loader_validates_normalizes_and_caches(tmp_path):
    path = tmp_path / "company_values.yaml"
    path.write_text(
        """
version: culture-v1
enabled: true
company_name: 测试公司
culture_name:
  en: " Test High Performance Culture "
  zh: " 测试高绩效文化 "
values:
  - id: respect_and_trust
    name: Respect and trust
    name_zh: " 尊重与信任 "
    definition: 在困难沟通中尊重对方并用事实建立信任。
    desired_behaviors:
      - " Listen before responding "
    desired_behaviors_zh:
      - " 先倾听并确认员工关切 "
    anti_patterns: []
    manager_applications: []
    source_refs:
      - culture/values.md
""".strip(),
        encoding="utf-8",
    )
    loader = BusinessConfigLoader(config_dir=tmp_path)

    first = loader.company_values()
    path.write_text("version: changed\nenabled: false\nvalues: []\n", encoding="utf-8")
    first["values"][0]["name_zh"] = "被调用方修改"
    second = loader.company_values()

    assert second["version"] == "culture-v1"
    assert second["culture_name"] == {
        "en": "Test High Performance Culture",
        "zh": "测试高绩效文化",
    }
    assert second["values"][0]["name"] == "Respect and trust"
    assert second["values"][0]["name_zh"] == "尊重与信任"
    assert second["values"][0]["desired_behaviors"] == ["Listen before responding"]
    assert second["values"][0]["desired_behaviors_zh"] == [
        "先倾听并确认员工关切"
    ]
    assert loader.company_value_terms() == (
        "Test High Performance Culture 测试高绩效文化 "
        "Respect and trust 尊重与信任"
    )
    assert loader.company_values_enabled() is True
    assert loader.culture_version() == "culture-v1"


def test_shipped_company_values_match_china_high_performance_culture():
    loader = BusinessConfigLoader()
    config = loader.company_values()

    assert config["version"] == "v0.2"
    assert config["culture_name"] == {
        "en": "Our High Performance Culture in China",
        "zh": "我们的中国区高绩效文化",
    }
    assert [value["id"] for value in config["values"]] == [
        "collaboration_to_deliver",
        "innovation_to_shape",
        "customer_centricity_to_grow",
        "commitment_to_win",
    ]
    assert [value["name_zh"] for value in config["values"]] == [
        "协同共进",
        "创变未来",
        "聚力共赢",
        "使命必达",
    ]
    assert all(value["desired_behaviors_zh"] for value in config["values"])
    assert config["source_policy"]["knowledge_scope"] == "culture"
    terms = loader.company_value_terms()
    assert "Collaboration to DELIVER" in terms
    assert "协同共进" in terms
    assert "Commitment to WIN" in terms
    assert "使命必达" in terms
    assert "Future and result focus" not in terms


def test_shipped_high_performance_culture_skill_is_current():
    active = KnowledgeSkillRouter().select(
        "guidance_plan",
        {"supplemental_info": "本次面谈需要结合使命必达准备后续行动。"},
        emit_metrics=False,
    )
    culture = next(
        item for item in active if item.skill.id == "high_performance_culture"
    )

    assert culture.stale is False
    assert "我们的中国区高绩效文化" in culture.core_knowledge
    assert "Commitment to WIN" in culture.core_knowledge
    assert "Future and result focus" not in culture.core_knowledge


@pytest.mark.asyncio
async def test_guidance_dimension_retrieval_keeps_culture_context():
    calls: list[tuple[str, int | None]] = []

    class Retrieval:
        async def aretrieve(self, name, context, top_k=None):
            calls.append((name, top_k))
            return [general_chunk(), culture_chunk()]

    service = object.__new__(GuidanceService)
    service.config_loader = ActiveCultureLoader()
    service.retrieval = Retrieval()
    state = SessionState(session_id="s1", setup_ready=True)

    chunks = await service._retrieve_dimension_chunks(
        state,
        guidance_agent_module.GUIDANCE_DIMENSION_SPECS[0],
    )

    assert calls == [("guidance_start", 8)]
    assert {chunk.scope for chunk in chunks} == {"general", "culture"}
    assert not state.warnings


def test_guidance_prompt_uses_values_inside_existing_sections(monkeypatch):
    monkeypatch.setattr(
        guidance_agent_module,
        "get_config_loader",
        lambda: ActiveCultureLoader(),
    )

    prompt = guidance_agent_module.GuidanceAgent._build_report_prompt(
        SessionState(session_id="s1", setup_ready=True),
        [general_chunk(), culture_chunk()],
    )

    assert "尊重与信任" in prompt
    assert "Respect and trust" in prompt
    assert "测试高绩效文化" in prompt
    assert "先倾听并确认员工关切" in prompt
    assert "culture/values.md" in prompt
    assert "仅是补充指导知识" in prompt
    assert "不新增价值观栏目" in prompt
    assert "不得沿用已被配置移除的旧价值观名称" in prompt


def test_changed_culture_version_invalidates_cached_guidance_report():
    class ChangedCultureLoader(ActiveCultureLoader):
        def culture_version(self):
            return "culture-v2"

    guidance = object.__new__(GuidanceService)
    guidance.config_loader = ChangedCultureLoader()
    guidance.report_repo = type(
        "GuidanceRepo",
        (),
        {
            "get_guidance": staticmethod(
                lambda session_id: GuidanceReport(
                    session_id=session_id,
                    intent_id="improvement",
                    culture_version="culture-v1",
                    guidance_version=guidance_agent_module.GUIDANCE_REPORT_VERSION,
                    purpose="对齐事实与下一步行动。",
                    opening_suggestion="先确认员工感受，再说明沟通目标。",
                    risk_preview=[
                        "员工若质疑事实依据，建议先邀请其补充信息。",
                        "员工若出现明显沉默，建议先确认其是否需要时间。",
                        "员工若表达强烈不满，建议先复述感受再核对事实。",
                    ],
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
            )
        },
    )()

    assert guidance._cached_report("s1") is None


def test_legacy_guidance_prompt_version_invalidates_cached_report():
    guidance = object.__new__(GuidanceService)
    guidance.config_loader = ActiveCultureLoader()
    guidance.report_repo = type(
        "GuidanceRepo",
        (),
        {
            "get_guidance": staticmethod(
                lambda session_id: GuidanceReport(
                    session_id=session_id,
                    intent_id="improvement",
                    culture_version="culture-v1",
                    purpose="对齐事实与下一步行动。",
                    opening_suggestion="先确认员工感受，再说明沟通目标。",
                    risk_preview=[
                        "员工若质疑事实依据，建议先邀请其补充信息。",
                        "员工若出现明显沉默，建议先确认其是否需要时间。",
                        "员工若表达强烈不满，建议先复述感受再核对事实。",
                    ],
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
            )
        },
    )()

    assert guidance._cached_report("s1") is None

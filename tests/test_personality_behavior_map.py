from __future__ import annotations

import hashlib
import json

import pytest

from backend.agents.employee_agent import EmployeeAgent
from backend.agents.guidance_agent import GuidanceAgent
from backend.business_config.loader import BusinessConfigLoader
from backend.schemas.conversation import ConversationTurn
from backend.schemas.personality_behavior import (
    PERSONALITY_BAND_IDS,
    PersonalityBehaviorMap,
    VERBATIM_PERSONALITY_SOURCE_SHA256,
    render_personality_band_description,
)
from backend.schemas.personality_facets import PersonalityFacetState
from backend.schemas.simulation import BigFivePersonality
from backend.schemas.state import SessionState
from backend.services.coach_service import CoachService
from backend.services.emotion_transition_service import EmotionTransitionService
from backend.services.personality_behavior_service import (
    PersonalityBehaviorResolver,
    personality_behavior_profile_json,
    personality_behavior_prompt_json,
)


FACETS = {
    "openness": (
        "fantasy",
        "aesthetics",
        "feelings",
        "actions",
        "ideas",
        "values",
    ),
    "conscientiousness": (
        "competence",
        "order",
        "dutifulness",
        "achievement_striving",
        "self_discipline",
        "deliberation",
    ),
    "extraversion": (
        "warmth",
        "gregariousness",
        "assertiveness",
        "activity",
        "excitement_seeking",
        "positive_emotions",
    ),
    "agreeableness": (
        "trust",
        "straightforwardness",
        "altruism",
        "compliance",
        "modesty",
        "tender_mindedness",
    ),
    "neuroticism": (
        "anxiety",
        "angry_hostility",
        "depression",
        "self_consciousness",
        "impulsiveness",
        "vulnerability",
    ),
}

ALL_TRAITS = tuple(FACETS) + tuple(
    f"{dimension_id}.{facet_id}"
    for dimension_id, facet_ids in FACETS.items()
    for facet_id in facet_ids
)

NEO_SEMANTIC_ANCHORS = {
    "openness": ("想象、审美、感受", "熟悉、具体", "新颖、复杂、多样"),
    "openness.fantasy": ("白日梦", "较少沉浸", "想象活动较丰富"),
    "openness.aesthetics": ("自然之美", "投入相对较少", "更敏感"),
    "openness.feelings": ("内在情绪体验", "较少关注", "觉察、体验并区分"),
    "openness.actions": ("Adventurousness", "熟悉活动", "多样性、新鲜感"),
    "openness.ideas": ("知识探索", "兴趣相对较低", "享受抽象思考"),
    "openness.values": ("社会、政治、宗教", "认同传统", "重新审视传统"),
    "conscientiousness": ("目标和规范组织行为", "自发、灵活", "目的性和自我约束"),
    "conscientiousness.competence": ("主观效能感", "效能感相对较弱", "稳定信心"),
    "conscientiousness.order": ("Orderliness", "偏好较弱", "偏好整洁"),
    "conscientiousness.dutifulness": ("伦理原则", "内在约束相对较弱", "重视伦理原则"),
    "conscientiousness.achievement_striving": ("追求成功", "驱动力相对较弱", "强烈追求成功"),
    "conscientiousness.self_discipline": ("启动并持续完成", "更费力", "开始并坚持"),
    "conscientiousness.deliberation": ("检查后果", "反复思考相对较少", "行动前充分思考"),
    "extraversion": ("社会与环境刺激", "低刺激、独立", "互动、表达机会"),
    "extraversion.warmth": ("亲和、喜爱", "关系距离", "亲和感"),
    "extraversion.gregariousness": ("多人陪伴", "独处或小范围", "享受他人陪伴"),
    "extraversion.assertiveness": ("主导、带领", "较少追求支配", "支配和领导"),
    "extraversion.activity": ("日常活动能量", "舒缓", "忙碌和快速"),
    "extraversion.excitement_seeking": ("强刺激", "需求较低", "更渴望"),
    "extraversion.positive_emotions": ("喜悦、热情", "相对较少或较弱", "更容易体验"),
    "agreeableness": ("信任、坦率、关怀", "保护自身或任务立场", "合作与长期关系"),
    "agreeableness.trust": ("缺少相反证据", "更谨慎、怀疑", "更相信"),
    "agreeableness.straightforwardness": ("不通过操纵或欺骗", "策略性影响", "避免操纵"),
    "agreeableness.altruism": ("提供帮助", "动机相对较弱", "主动和自然的关切"),
    "agreeableness.compliance": ("人际冲突", "竞争和对抗", "抑制攻击并寻求和解"),
    "agreeableness.modesty": ("谦逊", "不倾向弱化自己", "自我重要性的主观权重相对较低"),
    "agreeableness.tender_mindedness": ("困境和情感后果", "较少受同情", "更有同情心"),
    "neuroticism": ("负性反应的稳定易感性", "更直接、强烈或持续", "更容易较早产生负性反应"),
    "neuroticism.anxiety": ("潜在威胁", "较少提前担忧", "预期性担忧"),
    "neuroticism.angry_hostility": ("受挫、被阻碍", "激活阈值较高", "更容易体验恼怒"),
    "neuroticism.depression": ("损失、失败和挫折", "较弱，并较快恢复", "更容易体验低落"),
    "neuroticism.self_consciousness": ("羞怯、尴尬", "较少因被注视", "更容易感到羞怯"),
    "neuroticism.impulsiveness": ("即时欲望或诱惑", "抑制能力较强", "更难维持抑制"),
    "neuroticism.vulnerability": ("感到被压倒", "维持稳定和处理带宽", "接近处理上限更敏感"),
}


def _personality(default: int = 50, **overrides: int) -> BigFivePersonality:
    values = {dimension_id: default for dimension_id in FACETS}
    values.update(overrides)
    return BigFivePersonality.model_validate(values)


def _facets(
    personality: BigFivePersonality,
    default: int = 50,
    **overrides: int,
) -> PersonalityFacetState:
    payload = {}
    for dimension_id, facet_ids in FACETS.items():
        facet_values = {
            facet_id: overrides.get(f"{dimension_id}.{facet_id}", default)
            for facet_id in facet_ids
        }
        payload[dimension_id] = {
            "score": getattr(personality, dimension_id),
            "facets": facet_values,
        }
    payload.update(
        {
            "input_signature": "a" * 64,
            "source": "deterministic_fallback",
            "generator_version": "test",
        }
    )
    return PersonalityFacetState.model_validate(payload)


def _resolve(
    personality: BigFivePersonality,
    facets: PersonalityFacetState | None,
):
    return PersonalityBehaviorResolver(BusinessConfigLoader()).resolve(
        personality,
        facets,
    )


def test_verbatim_source_and_schema_ids_are_immutable():
    config = BusinessConfigLoader().personality_behavior_map()
    assert hashlib.sha256(config.source_text.encode("utf-8")).hexdigest() == (
        VERBATIM_PERSONALITY_SOURCE_SHA256
    )
    assert tuple(config.dimensions) == tuple(FACETS)
    assert tuple(band.id for band in config.score_bands) == PERSONALITY_BAND_IDS
    assert tuple(band.direction for band in config.score_bands) == (
        "low",
        "low",
        "low",
        "neutral",
        "high",
        "high",
        "high",
    )
    for dimension_id, facet_ids in FACETS.items():
        assert tuple(config.dimensions[dimension_id].facets) == facet_ids


@pytest.mark.parametrize(
    ("score", "band_id"),
    [
        (0, "very_low"),
        (19, "very_low"),
        (20, "clear_low"),
        (34, "clear_low"),
        (35, "mild_low"),
        (44, "mild_low"),
        (45, "neutral"),
        (55, "neutral"),
        (56, "mild_high"),
        (65, "mild_high"),
        (66, "clear_high"),
        (80, "clear_high"),
        (81, "very_high"),
        (100, "very_high"),
    ],
)
def test_all_score_boundaries_select_exactly_one_band(score: int, band_id: str):
    personality = _personality(score)
    profile = _resolve(personality, _facets(personality, score))
    assert profile is not None
    assert len(profile.scores) == 35
    assert {item.band_id for item in profile.scores} == {band_id}


BAND_SAMPLE_SCORES = {
    "very_low": 0,
    "clear_low": 20,
    "mild_low": 35,
    "neutral": 50,
    "mild_high": 56,
    "clear_high": 66,
    "very_high": 81,
}


def _selector_for_trait(config, trait_id: str):
    if "." not in trait_id:
        return config.dimensions[trait_id]
    dimension_id, facet_id = trait_id.split(".", 1)
    return config.dimensions[dimension_id].facets[facet_id]


def _score_for_trait(profile, trait_id: str):
    return next(item for item in profile.scores if item.trait_id == trait_id)


def _non_implications_text(specification) -> str:
    return "\n".join(specification.non_implications)


@pytest.mark.parametrize("band_id", tuple(BAND_SAMPLE_SCORES))
def test_every_trait_renders_a_detailed_current_band_profile(band_id: str):
    score = BAND_SAMPLE_SCORES[band_id]
    config = BusinessConfigLoader().personality_behavior_map()
    personality = _personality(score)
    profile = _resolve(personality, _facets(personality, score))

    assert profile is not None
    assert len(profile.scores) == len(ALL_TRAITS) == 35
    serialized_profile = personality_behavior_profile_json(profile)
    band = next(item for item in config.score_bands if item.id == band_id)
    marker = f"【{band.min_score}–{band.max_score}｜{band.label}】"

    for item in profile.scores:
        selector = _selector_for_trait(config, item.trait_id)
        specification = selector.specification
        description = item.detailed_description

        assert item.band_id == band_id
        assert item.display_name == selector.display_name
        assert description == getattr(selector.band_descriptions, band_id)
        assert description == render_personality_band_description(
            specification,
            band,
        )
        for other_band_id, other_description in (
            selector.band_descriptions.model_dump(mode="python").items()
        ):
            if other_band_id != band_id:
                assert other_description not in serialized_profile
        assert description.startswith(marker)
        for section_marker in (
            "【本档人格解释】",
            "【本档行为表现】",
            "【本档具体线索】",
            "【情境调节】",
            "【禁止推论】",
        ):
            assert description.count(section_marker) == 1
        assert specification.construct_definition in description
        assert specification.activation_context in description
        assert specification.contextual_modifiers.more_visible_when in description
        assert specification.contextual_modifiers.less_visible_when in description
        assert all(
            item in description for item in specification.non_implications
        )
        assert description.split("【本档行为表现】", 1)[1].split(
            "【本档具体线索】", 1
        )[0].count("* ") == 6
        concrete_section = description.split("【本档具体线索】", 1)[1].split(
            "【情境调节】", 1
        )[0]
        concrete_section = concrete_section.split("【本档特有校准】", 1)[0]
        assert description.split("【情境调节】", 1)[1].split(
            "【禁止推论】", 1
        )[0].count("* ") == 2
        assert description.split("【禁止推论】", 1)[1].count("* ") == len(
            specification.non_implications
        )

        if band_id.endswith("low"):
            expected = specification.low_profile.model_dump(mode="python")
            opposite = specification.high_profile.model_dump(mode="python")
        elif band_id.endswith("high"):
            expected = specification.high_profile.model_dump(mode="python")
            opposite = specification.low_profile.model_dump(mode="python")
        else:
            assert specification.neutral_tendency in description
            expected = {}
            opposite = {
                **specification.low_profile.model_dump(mode="python"),
                **{
                    f"high_{key}": value
                    for key, value in specification.high_profile.model_dump(
                        mode="python"
                    ).items()
                },
            }
        if expected:
            manifestations = expected["workplace_manifestations"]
            assert concrete_section.count("* ") == len(manifestations)
            assert f"* 内部倾向：{expected['internal_tendency']}" in description
            assert (
                f"* 注意与判断：{expected['attention_and_judgment']}"
                in description
            )
            assert (
                f"* 可观察外显：{expected['observable_expression']}"
                in description
            )
            assert (
                f"* 决策与互动：{expected['decision_and_interaction']}"
                in description
            )
            assert all(f"* {value}" in concrete_section for value in manifestations)
        else:
            assert concrete_section.count("* ") == 1
            assert "不预设低向或高向的固定行为信号" in concrete_section

        opposite_values = []
        for value in opposite.values():
            opposite_values.extend(value if isinstance(value, list) else [value])
        selected_values = []
        for value in expected.values():
            selected_values.extend(value if isinstance(value, list) else [value])
        assert all(
            value in selected_values or value not in description
            for value in opposite_values
        )


def test_each_trait_renders_seven_distinct_strength_levels_without_changing_construct():
    config = BusinessConfigLoader().personality_behavior_map()
    profiles = {}
    for band_id, score in BAND_SAMPLE_SCORES.items():
        personality = _personality(score)
        profiles[band_id] = _resolve(
            personality,
            _facets(personality, score),
        )

    for trait_id in ALL_TRAITS:
        selector = _selector_for_trait(config, trait_id)
        configured_descriptions = selector.band_descriptions.model_dump(
            mode="python"
        )
        assert tuple(configured_descriptions) == PERSONALITY_BAND_IDS
        descriptions = {
            band_id: _score_for_trait(profile, trait_id).detailed_description
            for band_id, profile in profiles.items()
        }
        assert descriptions == configured_descriptions
        assert len(set(descriptions.values())) == 7
        definition = selector.specification.construct_definition
        assert all(definition in value for value in descriptions.values())
        assert "低向路径" in descriptions["very_low"]
        assert "明显偏向低端" in descriptions["clear_low"]
        assert "轻度偏向低端" in descriptions["mild_low"]
        assert "不为低向或高向路径提供稳定" in descriptions["neutral"]
        assert "轻度偏向高端" in descriptions["mild_high"]
        assert "明显偏向高端" in descriptions["clear_high"]
        assert "高向路径" in descriptions["very_high"]


def test_employee_prompt_payload_is_the_single_seven_band_profile():
    personality = _personality(70)
    profile = _resolve(personality, _facets(personality, 70))

    assert profile is not None
    prompt_json = personality_behavior_prompt_json(profile)
    payload = json.loads(prompt_json)

    assert prompt_json == personality_behavior_profile_json(profile)
    assert '"band_descriptions"' not in prompt_json
    assert '"specification"' not in prompt_json
    assert set(payload) == {
        "general_rules",
        "map_hash",
        "map_version",
        "matched_combination_ids",
        "matched_combination_rules",
        "scores",
    }
    assert "behavior_contract" not in payload
    assert payload["map_hash"] == profile.map_hash
    assert len(payload["scores"]) == len(profile.scores) == 35
    assert "【一、人格输入】" in payload["general_rules"]
    assert "【十、五维组合优先】" in payload["general_rules"]
    assert "【四、Openness / 开放性】" not in payload["general_rules"]
    assert "【人格与说服速度】" not in payload["general_rules"]
    assert "Straightforwardness 影响是否直接表达内部真实立场" not in (
        payload["general_rules"]
    )
    assert "普通自评或宽泛发展讨论不自动触发完整计划" in (
        payload["general_rules"]
    )

    active_traits = {item["trait_id"]: item for item in payload["scores"]}
    for score in profile.scores:
        active = active_traits[score.trait_id]
        assert set(active) == {
            "band_id",
            "detailed_description",
            "display_name",
            "label",
            "score",
            "trait_id",
        }
        assert active["score"] == score.score
        assert active["band_id"] == score.band_id
        assert active["detailed_description"] == score.detailed_description
        assert "shared_definition" not in active
        assert "direction" not in active
        assert "intensity" not in active
        assert "specification" not in active
        assert "low_profile" not in active
        assert "high_profile" not in active
        assert "activation_context" not in active
        assert "band_descriptions" not in active


@pytest.mark.parametrize("score", tuple(BAND_SAMPLE_SCORES.values()))
def test_selected_profile_stays_bounded_without_repeating_global_rules(
    score: int,
):
    personality = _personality(score)
    profile = _resolve(personality, _facets(personality, score))

    assert profile is not None
    serialized = personality_behavior_profile_json(profile)
    assert len(serialized) < 34_000
    assert len(serialized) < (
        sum(len(item.detailed_description) for item in profile.scores) + 8_000
    )
    assert serialized.count("【十、五维组合优先】") == 1
    assert '"band_descriptions"' not in serialized
    assert "【本档作用方式】" not in serialized
    assert serialized.count("【本档具体线索】") == 35
    assert serialized.count("【本档特有校准】") == 35
    assert serialized.count("【情境调节】") == 35
    assert serialized.count("【禁止推论】") == 35


def test_all_35_traits_store_exactly_seven_explicit_generated_descriptions():
    config = BusinessConfigLoader().personality_behavior_map()
    description_count = 0

    for trait_id in ALL_TRAITS:
        selector = _selector_for_trait(config, trait_id)
        payload = selector.model_dump(mode="python")
        assert tuple(payload["band_descriptions"]) == PERSONALITY_BAND_IDS
        assert all(payload["band_descriptions"].values())
        assert len(set(payload["band_descriptions"].values())) == 7
        description_count += len(payload["band_descriptions"])
        for band in config.score_bands:
            assert payload["band_descriptions"][band.id] == (
                render_personality_band_description(
                    selector.specification,
                    band,
                )
            )
        assert set(payload["specification"]) == {
            "construct_definition",
            "activation_context",
            "low_profile",
            "neutral_tendency",
            "high_profile",
            "band_calibrations",
            "contextual_modifiers",
            "non_implications",
        }
        directional_fields = {
            "internal_tendency",
            "attention_and_judgment",
            "observable_expression",
            "decision_and_interaction",
            "workplace_manifestations",
        }
        assert set(payload["specification"]["low_profile"]) == directional_fields
        assert set(payload["specification"]["high_profile"]) == directional_fields
        for field in (
            "construct_definition",
            "activation_context",
            "neutral_tendency",
        ):
            assert payload["specification"][field].strip()
        assert set(payload["specification"]["contextual_modifiers"]) == {
            "more_visible_when",
            "less_visible_when",
        }
        assert all(
            value.strip()
            for value in payload["specification"]["contextual_modifiers"].values()
        )
        assert 2 <= len(payload["specification"]["non_implications"]) <= 4
        assert all(
            value.strip()
            for value in payload["specification"]["non_implications"]
        )
        for direction in ("low_profile", "high_profile"):
            profile = payload["specification"][direction]
            assert all(
                profile[field].strip()
                for field in directional_fields - {"workplace_manifestations"}
            )
            assert 2 <= len(profile["workplace_manifestations"]) <= 3
            assert len(set(profile["workplace_manifestations"])) == len(
                profile["workplace_manifestations"]
            )
            assert all(value.strip() for value in profile["workplace_manifestations"])
        assert payload["specification"]["low_profile"] != payload[
            "specification"
        ]["high_profile"]
        calibrations = payload["specification"]["band_calibrations"]
        assert tuple(calibrations) == PERSONALITY_BAND_IDS
        assert all(value.strip() for value in calibrations.values())
        assert all(8 <= len(value) <= 200 for value in calibrations.values())
        assert len(set(calibrations.values())) == 7

    assert description_count == 35 * 7 == 245


@pytest.mark.parametrize("score", tuple(BAND_SAMPLE_SCORES.values()))
def test_each_trait_renders_only_its_selected_specific_calibration(
    score: int,
):
    config = BusinessConfigLoader().personality_behavior_map()
    personality = _personality(score)
    profile = _resolve(personality, _facets(personality, score))

    assert profile is not None
    for trait_id in ALL_TRAITS:
        selector = _selector_for_trait(config, trait_id)
        calibrations = selector.specification.band_calibrations
        selected = _score_for_trait(profile, trait_id)
        calibration_payload = calibrations.model_dump(mode="python")
        assert calibration_payload[selected.band_id] in selected.detailed_description
        for band_id, calibration in calibration_payload.items():
            if band_id != selected.band_id:
                assert calibration not in selected.detailed_description


def test_all_35_traits_keep_their_neo_construct_and_endpoint_polarity():
    config = BusinessConfigLoader().personality_behavior_map()
    assert set(NEO_SEMANTIC_ANCHORS) == set(ALL_TRAITS)

    for trait_id, (construct_anchor, low_anchor, high_anchor) in (
        NEO_SEMANTIC_ANCHORS.items()
    ):
        specification = _selector_for_trait(config, trait_id).specification
        assert construct_anchor in specification.construct_definition
        assert low_anchor in specification.low_profile.internal_tendency
        assert high_anchor in specification.high_profile.internal_tendency
        assert low_anchor not in specification.high_profile.internal_tendency
        assert high_anchor not in specification.low_profile.internal_tendency


def test_neo_ipip_construct_corrections_are_explicit():
    config = BusinessConfigLoader().personality_behavior_map()

    fantasy = _selector_for_trait(config, "openness.fantasy").specification
    assert "白日梦" in fantasy.construct_definition
    assert "不是风险预测、情景规划或职业路径规划" in (
        _non_implications_text(fantasy)
    )

    actions = _selector_for_trait(config, "openness.actions").specification
    assert "Adventurousness" in actions.construct_definition
    assert "不是通用行动力、执行力、适应能力或方案落地能力" in (
        _non_implications_text(actions)
    )

    straightforwardness = _selector_for_trait(
        config,
        "agreeableness.straightforwardness",
    ).specification
    assert "不通过操纵或欺骗" in (
        straightforwardness.construct_definition
    )
    assert "不是 Assertiveness 式强势直接" in (
        _non_implications_text(straightforwardness)
    )

    assertiveness = _selector_for_trait(
        config,
        "extraversion.assertiveness",
    ).specification
    assert "主导、带领、影响他人" in assertiveness.construct_definition
    assert "需要占据话轮" in assertiveness.activation_context
    assert "不等于 Straightforwardness" in (
        _non_implications_text(assertiveness)
    )

    self_consciousness = _selector_for_trait(
        config,
        "neuroticism.self_consciousness",
    ).specification
    assert "羞怯、尴尬" in self_consciousness.construct_definition
    assert "不是泛化的职业认可敏感" in (
        _non_implications_text(self_consciousness)
    )

    impulsiveness = _selector_for_trait(
        config,
        "neuroticism.impulsiveness",
    ).specification
    assert "即时欲望或诱惑" in impulsiveness.construct_definition
    assert "不是快速说话、立即反驳" in (
        _non_implications_text(impulsiveness)
    )
    assert "普通绩效谈话通常保持静默" in impulsiveness.neutral_tendency

    neuroticism = _selector_for_trait(config, "neuroticism").specification
    assert "负性反应的稳定易感性" in neuroticism.construct_definition
    neuroticism_boundaries = _non_implications_text(neuroticism)
    assert "不决定具体是焦虑、愤怒、低落" in neuroticism_boundaries
    assert "不得由 Neuroticism 创造负面事件、情绪、分歧" in (
        neuroticism_boundaries
    )

    angry_hostility = _selector_for_trait(
        config,
        "neuroticism.angry_hostility",
    ).specification
    assert "受挫、被阻碍或感知不公" in (
        angry_hostility.construct_definition
    )
    assert "不得创建“不公平”事实或不同意见" in (
        _non_implications_text(angry_hostility)
    )

    depression = _selector_for_trait(
        config,
        "neuroticism.depression",
    ).specification
    assert "损失、失败和挫折后" in depression.construct_definition
    assert "不得自动生成悲伤、自我否定、内疚、自责" in (
        _non_implications_text(depression)
    )

    vulnerability = _selector_for_trait(
        config,
        "neuroticism.vulnerability",
    ).specification
    assert "持续、复杂或高强度压力" in vulnerability.construct_definition
    assert "没有负荷超过主观处理容量的明确证据时不得生成过载" in (
        _non_implications_text(vulnerability)
    )


def test_neighboring_facets_do_not_take_over_each_others_constructs():
    config = BusinessConfigLoader().personality_behavior_map()

    feelings = _selector_for_trait(config, "openness.feelings").specification
    assert "准确" not in feelings.high_profile.observable_expression
    assert "不等于情绪表达能力、情绪判断准确性" in _non_implications_text(
        feelings
    )

    conscientiousness = _selector_for_trait(
        config,
        "conscientiousness",
    ).specification
    assert "宽泛自评时更容易按目标、结果和证据组织回答" in (
        conscientiousness.high_profile.workplace_manifestations[0]
    )
    assert "不由高尽责性自动决定" in (
        conscientiousness.high_profile.workplace_manifestations[0]
    )

    dutifulness = _selector_for_trait(
        config,
        "conscientiousness.dutifulness",
    ).specification
    assert "区分已确认的自身责任" not in (
        dutifulness.high_profile.observable_expression
    )
    assert "个人便利发生冲突" in (
        dutifulness.high_profile.workplace_manifestations[1]
    )

    achievement = _selector_for_trait(
        config,
        "conscientiousness.achievement_striving",
    ).specification
    assert "评价是否匹配" not in achievement.high_profile.observable_expression
    assert "不直接决定是否争议绩效评级" in _non_implications_text(
        achievement
    )

    self_discipline = _selector_for_trait(
        config,
        "conscientiousness.self_discipline",
    ).specification
    assert "单轮语言通常不必出现固定" in (
        self_discipline.low_profile.observable_expression
    )
    assert "单轮语言通常不必出现固定" in (
        self_discipline.high_profile.observable_expression
    )
    assert "跨时间行为" in _non_implications_text(self_discipline)

    positive_emotions = _selector_for_trait(
        config,
        "extraversion.positive_emotions",
    ).specification
    assert positive_emotions.low_profile.decision_and_interaction == (
        positive_emotions.high_profile.decision_and_interaction
    )
    assert "不直接决定互动参与或行动选择" in (
        positive_emotions.high_profile.decision_and_interaction
    )

    agreeableness = _selector_for_trait(config, "agreeableness").specification
    assert "事实" not in agreeableness.low_profile.attention_and_judgment

    straightforwardness = _selector_for_trait(
        config,
        "agreeableness.straightforwardness",
    ).specification
    assert "不要求主动或完整披露" in _non_implications_text(
        straightforwardness
    )

    compliance = _selector_for_trait(
        config,
        "agreeableness.compliance",
    ).specification
    assert "不满" not in compliance.low_profile.internal_tendency
    assert "Compliance 不决定愤怒是否产生" in _non_implications_text(
        compliance
    )


def test_facet_band_calibrations_preserve_construct_boundaries():
    config = BusinessConfigLoader().personality_behavior_map()

    def calibration(trait_id: str, band_id: str) -> str:
        specification = _selector_for_trait(config, trait_id).specification
        return getattr(specification.band_calibrations, band_id)

    assert "不替代事实判断" in calibration("openness.fantasy", "very_high")
    assert "不靠华丽措辞" in calibration("openness.aesthetics", "very_high")
    assert "不得创建情绪" in calibration("openness.feelings", "very_high")
    assert "不等于冒险、转岗或承诺" in calibration(
        "openness.actions",
        "very_high",
    )
    assert "不表示理解能力不足" in calibration("openness.ideas", "very_low")
    assert "不编造原因" in calibration("openness.ideas", "very_high")
    assert "工作反规则" in calibration("openness.values", "very_high")

    assert "不证明真实能力" in calibration(
        "conscientiousness.competence",
        "very_high",
    )
    assert "不得凭空创建任务、期限或计划" in calibration(
        "conscientiousness.order",
        "very_high",
    )
    assert "自动认错" in calibration(
        "conscientiousness.dutifulness",
        "very_high",
    )
    assert "不得编造成绩、计划、晋升诉求" in calibration(
        "conscientiousness.achievement_striving",
        "very_high",
    )
    assert "不得创建承诺或历史事实" in calibration(
        "conscientiousness.self_discipline",
        "very_high",
    )
    assert "不得制造风险" in calibration(
        "conscientiousness.deliberation",
        "very_high",
    )

    assert "不因此认同评价" in calibration(
        "extraversion.warmth",
        "mild_high",
    )
    assert "一对一谈话中不得强行外显" in calibration(
        "extraversion.gregariousness",
        "very_high",
    )
    assert "不等于攻击" in calibration(
        "extraversion.assertiveness",
        "very_high",
    )
    assert "不得凭空生成计划" in calibration(
        "extraversion.activity",
        "very_high",
    )
    assert "不得覆盖同时存在的负面情绪或分歧" in calibration(
        "extraversion.positive_emotions",
        "very_high",
    )

    assert "不等于轻信、接受评价" in calibration(
        "agreeableness.trust",
        "very_high",
    )
    assert "外显和解不等于内在认同" in calibration(
        "agreeableness.compliance",
        "very_high",
    )
    assert "不得因此删除成果" in calibration(
        "agreeableness.modesty",
        "very_high",
    )
    assert "不等于实际助人、妥协或规则例外" in calibration(
        "agreeableness.tender_mindedness",
        "very_high",
    )


def test_neuroticism_facet_calibrations_keep_distinct_trigger_channels():
    config = BusinessConfigLoader().personality_behavior_map()

    def high_calibration(trait_id: str) -> str:
        specification = _selector_for_trait(config, trait_id).specification
        return specification.band_calibrations.clear_high

    assert "未决的未来风险" in high_calibration("neuroticism.anxiety")
    assert "阻碍或不公平判断" in high_calibration(
        "neuroticism.angry_hostility"
    )
    assert "重要挫折已经发生" in high_calibration("neuroticism.depression")
    assert "公开点名、失误曝光" in high_calibration(
        "neuroticism.self_consciousness"
    )
    assert "即时诱惑" in high_calibration("neuroticism.impulsiveness")
    assert "连续高负荷" in high_calibration("neuroticism.vulnerability")


def test_neuroticism_requires_external_trigger_evidence():
    config = BusinessConfigLoader().personality_behavior_map()
    neuroticism = _selector_for_trait(config, "neuroticism").specification
    self_consciousness = _selector_for_trait(
        config,
        "neuroticism.self_consciousness",
    ).specification
    vulnerability = _selector_for_trait(
        config,
        "neuroticism.vulnerability",
    ).specification

    assert "至少一个 Neuroticism facet 对应的负性刺激" in (
        neuroticism.activation_context
    )
    assert "父维度只调节敏感度、心理占用和恢复速度" in (
        neuroticism.activation_context
    )
    assert "不独立决定具体情绪" in neuroticism.activation_context
    assert "社会评价不适" not in self_consciousness.activation_context
    assert "个人呈现本身为焦点" in self_consciousness.activation_context
    assert "高分不证明当前已经羞怯、尴尬、拘谨或羞耻" in (
        _non_implications_text(self_consciousness)
    )
    assert "只有输入明确显示负荷超过" in vulnerability.activation_context
    assert "不认定容量已经被超过" in _non_implications_text(vulnerability)


def test_conscientiousness_supports_proportionate_plans_when_the_task_does():
    config = BusinessConfigLoader().personality_behavior_map()
    conscientiousness = config.dimensions[
        "conscientiousness"
    ].specification
    order = config.dimensions["conscientiousness"].facets[
        "order"
    ].specification

    assert "真实目标或差距、责任与必要信息已经明确" in (
        conscientiousness.high_profile.observable_expression
    )
    assert "不要求 Manager 必须说出“计划”" in (
        conscientiousness.high_profile.observable_expression
    )
    assert "当前问题直接涉及安排、步骤或计划且信息足够" in (
        order.high_profile.decision_and_interaction
    )
    assert "不得编造问题、行动、责任、资源、期限、标准或承诺" in (
        _non_implications_text(conscientiousness)
    )
    assert "普通自评或宽泛发展讨论" in _non_implications_text(order)
    assert "高分不等于主动自我批评、认错" in _non_implications_text(
        conscientiousness
    )


def test_personality_content_contract_does_not_force_short_or_long_replies():
    profile = _resolve(_personality(70), _facets(_personality(70), 70))
    assert profile is not None
    assert "不通过增加观点、句数、情绪或计划细节来模拟强度" in (
        profile.general_rules
    )
    assert "本档具体线索是构念激活后更可能出现的例子" in (
        profile.general_rules
    )
    assert "“本档特有校准”是当前分档相对于相邻档最具体的差异" in (
        profile.general_rules
    )
    assert "每个行为通道优先服从最具体的 facet" in profile.general_rules
    assert "父维度和同向 facet 双重放大" in profile.general_rules
    assert "不得为了展示完整画像逐项堆叠同义信号" in (
        profile.general_rules
    )
    assert "activation_context 是必要触发门槛" in profile.general_rules
    assert "人格分数、“更容易显现”的情境或模型推测都不能替代" in (
        profile.general_rules
    )
    assert "人格不得创建事实、立场、分歧、认可、自我批评、诉求、责任" in (
        profile.general_rules
    )
    assert "当前问题未要求计划或信息不足时凭空增加计划内容" in (
        profile.general_rules
    )


def test_map_rejects_missing_or_collapsed_trait_specifications():
    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    del payload["dimensions"]["openness"]["specification"][
        "construct_definition"
    ]
    with pytest.raises(ValueError):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    specification = payload["dimensions"]["openness"]["specification"]
    specification["low_profile"] = specification["high_profile"]
    with pytest.raises(ValueError, match="low and high"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    payload["dimensions"]["openness"]["specification"][
        "construct_definition"
    ] = "【错误的已渲染标题】"
    with pytest.raises(ValueError, match="plain construct text"):
        PersonalityBehaviorMap.model_validate(payload)


def test_map_rejects_missing_duplicate_or_stale_explicit_band_descriptions():
    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    del payload["dimensions"]["openness"]["band_descriptions"]["mild_low"]
    with pytest.raises(ValueError):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    descriptions = payload["dimensions"]["openness"]["band_descriptions"]
    descriptions["clear_low"] = descriptions["very_low"]
    with pytest.raises(ValueError, match="band descriptions must be unique"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    payload["dimensions"]["openness"]["band_descriptions"][
        "clear_high"
    ] += "\n手工漂移。"
    with pytest.raises(ValueError, match="stale or manually edited"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    payload["score_bands"][0]["direction"] = "high"
    with pytest.raises(ValueError, match="directions must be"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    payload["dimensions"]["openness"]["specification"]["low_profile"][
        "workplace_manifestations"
    ] = ["只有一条具体线索。"]
    with pytest.raises(ValueError):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    manifestations = payload["dimensions"]["openness"]["specification"][
        "high_profile"
    ]["workplace_manifestations"]
    manifestations[1] = manifestations[0]
    with pytest.raises(ValueError, match="manifestations must be unique"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    modifiers = payload["dimensions"]["openness"]["specification"][
        "contextual_modifiers"
    ]
    modifiers["less_visible_when"] = modifiers["more_visible_when"]
    with pytest.raises(ValueError, match="visibility contexts must differ"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    non_implications = payload["dimensions"]["openness"]["specification"][
        "non_implications"
    ]
    non_implications[1] = non_implications[0]
    with pytest.raises(ValueError, match="non-implications must be unique"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    payload["dimensions"]["openness"]["specification"]["high_profile"][
        "observable_expression"
    ] = "【错误的嵌套标题】"
    with pytest.raises(ValueError, match="plain construct text"):
        PersonalityBehaviorMap.model_validate(payload)


def test_map_requires_bounded_unique_band_calibrations_for_every_trait():
    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    del payload["dimensions"]["openness"]["specification"][
        "band_calibrations"
    ]
    with pytest.raises(ValueError):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    calibrations = payload["dimensions"]["openness"]["specification"][
        "band_calibrations"
    ]
    calibrations["clear_low"] = calibrations["very_low"]
    with pytest.raises(ValueError, match="band calibrations must be unique"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    facet_calibrations = payload["dimensions"]["openness"]["facets"][
        "fantasy"
    ]["specification"]["band_calibrations"]
    facet_calibrations["very_high"] = "过长" * 101
    with pytest.raises(ValueError):
        PersonalityBehaviorMap.model_validate(payload)


def test_only_the_selected_band_is_rendered():
    high_personality = _personality(70)
    high = _resolve(high_personality, _facets(high_personality, 70))
    low_personality = _personality(30)
    low = _resolve(low_personality, _facets(low_personality, 30))
    neutral_personality = _personality(50)
    neutral = _resolve(neutral_personality, _facets(neutral_personality, 50))

    assert high is not None
    assert low is not None
    assert neutral is not None
    high_fantasy = _score_for_trait(high, "openness.fantasy")
    low_fantasy = _score_for_trait(low, "openness.fantasy")
    neutral_fantasy = _score_for_trait(neutral, "openness.fantasy")
    fantasy_specification = _selector_for_trait(
        BusinessConfigLoader().personality_behavior_map(),
        "openness.fantasy",
    ).specification
    fantasy_selector = _selector_for_trait(
        BusinessConfigLoader().personality_behavior_map(),
        "openness.fantasy",
    )

    assert high_fantasy.band_id == "clear_high"
    assert high_fantasy.detailed_description == (
        fantasy_selector.band_descriptions.clear_high
    )
    assert "想象活动较丰富" in high_fantasy.detailed_description
    assert "较少沉浸于想象" not in high_fantasy.detailed_description
    assert low_fantasy.band_id == "clear_low"
    assert low_fantasy.detailed_description == (
        fantasy_selector.band_descriptions.clear_low
    )
    assert "较少沉浸于想象" in low_fantasy.detailed_description
    assert "想象活动较丰富" not in low_fantasy.detailed_description
    assert neutral_fantasy.band_id == "neutral"
    assert neutral_fantasy.detailed_description == (
        fantasy_selector.band_descriptions.neutral
    )
    assert "不主动强化现实取向或想象取向" in (
        neutral_fantasy.detailed_description
    )
    assert "较少沉浸于想象" not in neutral_fantasy.detailed_description
    assert "想象活动较丰富" not in neutral_fantasy.detailed_description
    for manifestation in fantasy_specification.high_profile.workplace_manifestations:
        assert manifestation in high_fantasy.detailed_description
        assert manifestation not in low_fantasy.detailed_description
        assert manifestation not in neutral_fantasy.detailed_description
    for manifestation in fantasy_specification.low_profile.workplace_manifestations:
        assert manifestation in low_fantasy.detailed_description
        assert manifestation not in high_fantasy.detailed_description
        assert manifestation not in neutral_fantasy.detailed_description


@pytest.mark.parametrize("trait_id", ALL_TRAITS)
def test_every_dimension_and_facet_selects_exactly_its_current_band(
    trait_id: str,
):
    if "." in trait_id:
        personality = _personality()
        high = _resolve(
            personality,
            _facets(personality, **{trait_id: 70}),
        )
        low = _resolve(
            personality,
            _facets(personality, **{trait_id: 30}),
        )
    else:
        high_personality = _personality(**{trait_id: 70})
        low_personality = _personality(**{trait_id: 30})
        high = _resolve(high_personality, _facets(high_personality))
        low = _resolve(low_personality, _facets(low_personality))

    assert high is not None
    assert low is not None
    high_score = _score_for_trait(high, trait_id)
    low_score = _score_for_trait(low, trait_id)
    assert high_score.band_id == "clear_high"
    assert low_score.band_id == "clear_low"
    assert "高向路径" in high_score.detailed_description
    assert "低向路径" in low_score.detailed_description
    assert high_score.detailed_description != low_score.detailed_description


def test_map_rejects_score_gaps_and_unknown_trait_references():
    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    payload["score_bands"][0]["max_score"] = 18
    with pytest.raises(ValueError, match="without gaps"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    payload["combination_rules"] = [
        {
            "id": "invalid_trait",
            "all_of": [
                {"trait_id": "unknown", "band_ids": ["clear_high"]}
            ],
            "any_of": [],
            "guidance": "只用于校验未知 trait。",
        }
    ]
    with pytest.raises(ValueError, match="unknown personality trait"):
        PersonalityBehaviorMap.model_validate(payload)

    payload = BusinessConfigLoader().personality_behavior_map().model_dump(
        mode="python"
    )
    payload["combination_rules"] = [
        {
            "id": "invalid_guidance",
            "all_of": [
                {"trait_id": "openness", "band_ids": ["clear_high"]}
            ],
            "any_of": [],
            "guidance": "【错误的渲染标题】",
        }
    ]
    with pytest.raises(ValueError, match="plain conditional text"):
        PersonalityBehaviorMap.model_validate(payload)


def test_personality_map_hash_changes_guidance_and_coach_cache_versions(
    monkeypatch: pytest.MonkeyPatch,
):
    loader = BusinessConfigLoader()
    coach_before = loader.coach_version()
    guidance_before = loader.guidance_version()

    monkeypatch.setattr(
        loader,
        "personality_behavior_map_hash",
        lambda: "f" * 64,
    )
    loader._coach_version_cached.cache_clear()
    loader._guidance_version_cached.cache_clear()

    assert loader.coach_version() != coach_before
    assert loader.guidance_version() != guidance_before


def test_no_preset_composite_outcome_overrides_the_selected_traits():
    config = BusinessConfigLoader().personality_behavior_map()
    assert config.combination_rules == []

    personality = _personality(
        extraversion=30,
        agreeableness=30,
        neuroticism=70,
    )
    profile = _resolve(personality, _facets(personality, 70))
    assert profile is not None
    assert profile.matched_combination_ids == []
    assert profile.matched_combination_rules == []
    serialized = personality_behavior_profile_json(profile)
    assert "resistance" not in serialized
    assert "明显心理压力" not in serialized


def test_missing_legacy_facets_emit_only_five_dimension_bands():
    profile = _resolve(_personality(70), None)
    assert profile is not None
    assert len(profile.scores) == 5
    assert {item.trait_id for item in profile.scores} == set(FACETS)
    assert {item.band_id for item in profile.scores} == {"clear_high"}
    assert "【四、Openness / 开放性】" not in profile.general_rules


def test_employee_guidance_emotion_and_coach_share_the_same_selected_profile():
    personality = _personality(
        openness=70,
        conscientiousness=30,
        extraversion=70,
        agreeableness=30,
        neuroticism=70,
    )
    facets = _facets(
        personality,
        50,
        **{
            "openness.fantasy": 70,
            "conscientiousness.order": 30,
            "extraversion.assertiveness": 70,
            "agreeableness.trust": 30,
            "agreeableness.compliance": 30,
            "agreeableness.straightforwardness": 70,
            "neuroticism.anxiety": 70,
        },
    )
    state = SessionState(
        session_id="personality-profile-contract",
        personality=personality,
        personality_facets=facets,
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text="我们讨论一下本周期的表现。",
            )
        ],
    )
    expected = _resolve(personality, facets)
    assert expected is not None

    employee_prompt = EmployeeAgent._build_reply_prompt(
        state,
        "我们讨论一下本周期的表现。",
    )
    assert expected.map_hash in employee_prompt
    assert '"matched_combination_ids":[]' in employee_prompt
    expected_openness = next(
        item for item in expected.scores if item.trait_id == "openness"
    )
    unselected_personality = _personality(
        openness=30,
        conscientiousness=30,
        extraversion=70,
        agreeableness=30,
        neuroticism=70,
    )
    unselected_profile = _resolve(
        unselected_personality,
        _facets(unselected_personality, 50),
    )
    assert unselected_profile is not None
    unselected_openness = next(
        item.detailed_description
        for item in unselected_profile.scores
        if item.trait_id == "openness"
    )
    serialized_openness = json.dumps(
        expected_openness.detailed_description,
        ensure_ascii=False,
    )[1:-1]
    serialized_unselected = json.dumps(
        unselected_openness,
        ensure_ascii=False,
    )[1:-1]
    assert serialized_openness in employee_prompt
    assert serialized_unselected not in employee_prompt

    guidance_context = GuidanceAgent._build_context(state, [])
    assert guidance_context.personality_behavior_profile == expected

    emotion_prompt = EmotionTransitionService()._build_prompt(
        state,
        "我们讨论一下本周期的表现。",
    )
    assert expected.map_hash in emotion_prompt
    assert serialized_openness in emotion_prompt
    assert serialized_unselected not in emotion_prompt
    assert "matched_combination_ids" not in emotion_prompt
    assert "matched_combination_rules" not in emotion_prompt

    coach_context = CoachService._generation_context(None, state)
    assert coach_context["personality_behavior_profile"] == expected

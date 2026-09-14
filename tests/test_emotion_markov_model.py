from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import backend.services.emotion_transition_service as emotion_transition_module
from backend.business_config.loader import get_config_loader
from backend.schemas.conversation import ConversationTurn, attach_emotion_snapshot
from backend.schemas.simulation import (
    BigFivePersonality,
    EmotionState,
    EmotionTransitionModelConfig,
    EmotionTransitionStructuredOutput,
    VADVector,
)
from backend.schemas.state import SessionState
from backend.services.emotion_markov_model import EmotionMarkovModel
from backend.services.emotion_transition_service import EmotionTransitionService
from backend.services.prompt_service import PromptService
from backend.services.session_service import SessionService
from backend.services.setup_service import SetupService


EXPECTED_ANCHORS = [
    ("surprised", "惊讶", "对结果意外，尚未形成明确立场", (0.05, 0.90, -0.10)),
    ("pleased", "满意", "对反馈结果正向接受", (0.85, 0.20, 0.35)),
    ("engaged", "积极投入", "愿意主动讨论和推进", (0.55, 0.75, 0.45)),
    ("confident", "自信坚定", "对自身判断有把握，主动表达", (0.50, 0.35, 0.85)),
    ("relieved", "释然", "担忧减轻，情绪放松", (0.75, -0.30, 0.20)),
    ("calm", "平静", "情绪稳定，愿意理性沟通", (0.35, -0.70, 0.30)),
    ("neutral", "中性观望", "暂不表态，继续观察信息", (0.00, -0.10, 0.00)),
    ("tired", "疲惫", "能量低，表达简短", (-0.10, -0.85, -0.40)),
    ("discouraged", "沮丧", "动力下降，但不一定悲伤", (-0.45, -0.65, -0.25)),
    ("sad", "失落", "负面、低控制感，倾向退缩", (-0.75, -0.35, -0.70)),
    ("disappointed", "失望", "预期落差明显，但激活中等", (-0.80, 0.10, -0.20)),
    ("anxious", "焦虑", "担心后果，控制感低", (-0.45, 0.75, -0.75)),
    ("confused", "困惑", "信息不足，不知道如何判断", (-0.15, 0.35, -0.55)),
    ("guarded", "戒备防御", "对评价保持警惕，准备解释", (-0.30, 0.50, 0.25)),
    ("angry", "愤怒", "强烈负面且主动对抗", (-0.80, 0.80, 0.80)),
    ("resentful", "不满反感", "持续不满，但未达到爆发状态", (-0.75, 0.30, 0.35)),
    ("hopeful", "希望", "看到改善或发展可能，但仍等待证据", (0.55, 0.35, 0.15)),
    ("proud", "自豪", "认可自己的贡献或成果，愿意正面表达", (0.82, 0.45, 0.78)),
    ("appreciative", "欣慰感激", "感受到具体认可、理解或善意回应", (0.72, 0.10, 0.30)),
    ("inspired", "鼓舞振奋", "被发展方向或有意义目标激发", (0.72, 0.82, 0.48)),
    ("curious", "好奇探索", "对反馈原因或新方案产生探索兴趣", (0.38, 0.58, 0.05)),
    ("determined", "下定决心", "面对挑战形成明确推进意愿", (0.35, 0.68, 0.92)),
    ("trusting", "信任开放", "相信沟通意图，愿意降低防备", (0.58, -0.12, 0.12)),
    ("grateful", "感激", "对具体支持或投入产生真诚谢意", (0.88, -0.05, 0.08)),
    ("accomplished", "成就感", "确认付出形成成果并获得胜任感", (0.92, 0.22, 0.72)),
    ("eager", "跃跃欲试", "对明确机会或下一步有较高行动期待", (0.62, 0.88, 0.18)),
    ("empowered", "获得力量", "因资源、授权或清晰边界而更有掌控感", (0.58, 0.52, 0.82)),
    ("attentive", "专注聆听", "集中接收信息，暂不作情绪性判断", (0.10, 0.42, 0.10)),
    ("reflective", "反思沉思", "放慢节奏，重新审视自身与事实", (0.05, -0.42, 0.08)),
    ("receptive", "开放接纳", "愿意听取反馈，但尚未表示同意", (0.28, 0.18, 0.05)),
    ("skeptical", "审慎怀疑", "对结论或依据存疑，保持核验立场", (-0.12, 0.25, 0.38)),
    ("cautious", "谨慎", "感知潜在风险，暂缓承诺或判断", (-0.08, 0.12, -0.18)),
    ("uncertain", "犹疑", "理解信息但尚不能确定选择或立场", (-0.10, 0.20, -0.42)),
    ("ambivalent", "矛盾纠结", "正反感受并存，难以立即形成单一立场", (0.00, 0.45, -0.28)),
    ("evaluating", "审视评估", "主动比较事实、标准与后果", (0.00, 0.28, 0.42)),
    ("composed", "沉着", "有压力但保持自控和清晰表达", (0.20, -0.38, 0.68)),
    ("alert", "警觉", "注意到可能的重要变化，但尚未进入防御", (-0.05, 0.72, 0.10)),
    ("reserved", "克制保留", "控制外显反应，只表达必要信息", (-0.02, -0.25, -0.08)),
    ("detached", "情感抽离", "降低情绪投入并与议题保持距离", (-0.18, -0.62, 0.05)),
    ("frustrated", "挫败", "努力受阻或反复无果，产生明显受挫感", (-0.55, 0.70, 0.10)),
    ("irritated", "恼火", "对阻碍或表达方式产生轻中度怒意", (-0.40, 0.62, 0.60)),
    ("aggrieved", "委屈", "认为付出、事实或处境未被公平看见", (-0.68, 0.42, -0.28)),
    ("embarrassed", "尴尬", "因评价、失误或当众处境感到不自在", (-0.42, 0.48, -0.42)),
    ("ashamed", "羞愧", "将负面评价内归因，自我价值受到冲击", (-0.72, 0.18, -0.82)),
    ("guilty", "内疚", "认为自己对具体影响负有责任并感到歉疚", (-0.58, 0.30, -0.48)),
    ("overwhelmed", "不堪重负", "信息、压力或任务超过当前承受能力", (-0.68, 0.60, -0.92)),
    ("afraid", "害怕", "感知到迫近且严重的职业或关系威胁", (-0.85, 0.95, -0.90)),
    ("helpless", "无助", "看不到可控行动路径，感到无法改变结果", (-0.88, -0.18, -0.95)),
]

EXPECTED_EXPRESSION_PROFILE_FIELDS = {
    "stance_cue",
    "rhythm_cue",
    "agency_cue",
    "avoid",
}

@pytest.fixture
def markov_model() -> EmotionMarkovModel:
    loader = get_config_loader()
    return EmotionMarkovModel(
        loader.emotion_anchors(),
        loader.emotion_transition_model_config(),
        loader.personality_transition_vad_weights(),
    )


def test_emotion_space_v6_matches_all_forty_eight_business_anchors() -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    anchors = loader.emotion_anchors()
    profiles = loader.emotion_expression_profiles()

    assert raw_config["version"] == "v6"
    assert raw_config["revision"] == "gradual_transition_and_stative_anchors_2026-08-28"
    assert raw_config["default_anchor"] == "neutral"
    assert list(anchors) == [item[0] for item in EXPECTED_ANCHORS]
    assert list(profiles) == [item[0] for item in EXPECTED_ANCHORS]
    assert len(anchors) == 48
    assert len({anchor.name for anchor in anchors.values()}) == 48
    assert len(
        {
            (anchor.vad.valence, anchor.vad.arousal, anchor.vad.dominance)
            for anchor in anchors.values()
        }
    ) == 48
    for anchor_id, name, _previous_description, coordinates in EXPECTED_ANCHORS:
        anchor = anchors[anchor_id]
        assert anchor.name == name
        assert anchor.description.strip()
        assert (
            anchor.vad.valence,
            anchor.vad.arousal,
            anchor.vad.dominance,
        ) == coordinates
        profile = profiles[anchor_id]
        assert set(profile) == EXPECTED_EXPRESSION_PROFILE_FIELDS
        assert all(profile.values())

    for field in EXPECTED_EXPRESSION_PROFILE_FIELDS:
        assert len({profile[field] for profile in profiles.values()}) == 48
    for profile in profiles.values():
        assert "已选" in profile["agency_cue"]
        assert not any(
            phrase in profile["agency_cue"]
            for phrase in (
                "只保留",
                "只表达",
                "只处理",
                "只推进",
                "只确认",
                "只聚焦",
            )
        )
    assert "已选内容本就包含" in profiles["evaluating"]["stance_cue"]
    assert "不改变上游已选的比较内容或数量" in profiles["evaluating"][
        "rhythm_cue"
    ]

    config = loader.emotion_transition_model_config()
    assert config.algorithm == "mecot_vad_v1"
    assert config.distance_temperature == 0.3
    assert config.stay_bias == 1.0
    assert config.slow_process_weight == 10.0
    assert config.personality_strength == 1.5
    assert config.neutral_delta_epsilon == 0.05
    assert config.decisive_intensity_threshold == 0.35
    assert config.decisive_probability_sharpness == 4.0
    assert config.min_transition_rate == 0.45
    assert config.max_transition_rate == 0.75
    assert config.max_axis_step == 0.35
    assert config.sampling_enabled is False

    expression_policy = loader.emotion_expression_policy()
    assert expression_policy["role"].startswith("情绪层只调节")
    assert len(expression_policy["invariants"]) >= 5
    assert set(expression_policy["field_semantics"]) == EXPECTED_EXPRESSION_PROFILE_FIELDS

    selection_policy = loader.emotion_anchor_selection_policy()
    direct_ids = set(selection_policy["direct_vad_anchor_ids"])
    contextual_ids = set(selection_policy["contextual_anchor_gates"])
    assert selection_policy["mode"] == "enforce"
    assert selection_policy["max_output_tags"] == 4
    assert len(direct_ids) == 37
    assert len(contextual_ids) == 11
    assert not direct_ids & contextual_ids
    assert direct_ids | contextual_ids == set(anchors)
    assert "guilty" in contextual_ids
    assert "afraid" in contextual_ids


def test_anchor_descriptions_are_stative_and_vad_aligned() -> None:
    anchors = get_config_loader().emotion_anchors()
    directional_terms = (
        "上升",
        "下降",
        "回落",
        "降低",
        "增强",
        "减弱",
        "恢复",
        "转为",
    )

    for anchor in anchors.values():
        assert not any(term in anchor.description for term in directional_terms)

    assert anchors["disappointed"].vad.arousal == pytest.approx(0.1)
    assert anchors["appreciative"].vad.arousal == pytest.approx(0.1)
    assert "低" in anchors["disappointed"].description
    assert "低" in anchors["appreciative"].description


@pytest.mark.parametrize(
    "field_name",
    ("neutral_delta_epsilon", "decisive_intensity_threshold"),
)
def test_transition_thresholds_must_stay_within_unit_interval(
    field_name: str,
) -> None:
    with pytest.raises(ValueError):
        EmotionTransitionModelConfig(**{field_name: 1.01})

    if field_name == "decisive_intensity_threshold":
        with pytest.raises(ValueError):
            EmotionTransitionModelConfig(**{field_name: 1.0})


@pytest.mark.parametrize("sharpness", (0.99, 32.01))
def test_decisive_probability_sharpness_is_bounded(sharpness: float) -> None:
    with pytest.raises(ValueError):
        EmotionTransitionModelConfig(
            decisive_probability_sharpness=sharpness,
        )


def test_expression_profiles_must_match_anchor_ids_exactly(monkeypatch) -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    raw_config["expression_profiles"].pop("neutral")
    monkeypatch.setattr(loader, "load_yaml", lambda _relative_path: raw_config)

    with pytest.raises(
        ValueError,
        match="Emotion expression profiles must match emotion anchors exactly",
    ):
        loader.emotion_expression_profiles()


def test_expression_profiles_reject_non_string_cues(monkeypatch) -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    raw_config["expression_profiles"]["neutral"]["stance_cue"] = None
    monkeypatch.setattr(loader, "load_yaml", lambda _relative_path: raw_config)

    with pytest.raises(ValueError, match="fields must be non-empty strings"):
        loader.emotion_expression_profiles()


def test_expression_profiles_reject_removed_opening_cue(monkeypatch) -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    raw_config["expression_profiles"]["neutral"]["opening_cue"] = "旧首句模块"
    monkeypatch.setattr(loader, "load_yaml", lambda _relative_path: raw_config)

    with pytest.raises(
        ValueError,
        match=r"unexpected=\['opening_cue'\]",
    ):
        loader.emotion_expression_profiles()


def test_expression_policy_rejects_missing_runtime_boundary(monkeypatch) -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    raw_config["expression_policy"].pop("role")
    monkeypatch.setattr(loader, "load_yaml", lambda _relative_path: raw_config)

    with pytest.raises(ValueError, match="Invalid emotion expression policy fields"):
        loader.emotion_expression_policy()


def test_expression_policy_rejects_non_string_invariants(monkeypatch) -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    raw_config["expression_policy"]["invariants"][0] = None
    monkeypatch.setattr(loader, "load_yaml", lambda _relative_path: raw_config)

    with pytest.raises(ValueError, match="invariants must be non-empty strings"):
        loader.emotion_expression_policy()


def test_expression_policy_rejects_removed_opening_semantics(monkeypatch) -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    raw_config["expression_policy"]["field_semantics"]["opening_cue"] = (
        "旧首句模块"
    )
    monkeypatch.setattr(loader, "load_yaml", lambda _relative_path: raw_config)

    with pytest.raises(
        ValueError,
        match=r"unexpected=\['opening_cue'\]",
    ):
        loader.emotion_expression_policy()


def test_anchor_selection_policy_must_cover_every_anchor(monkeypatch) -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    raw_config["anchor_selection_policy"]["direct_vad_anchor_ids"].remove("calm")
    monkeypatch.setattr(loader, "load_yaml", lambda _relative_path: raw_config)

    with pytest.raises(
        ValueError,
        match="Emotion anchor selection does not cover anchors",
    ):
        loader.emotion_anchor_selection_policy()


def test_contextual_gate_must_be_reachable_within_output_tag_limit(
    monkeypatch,
) -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    raw_config["anchor_selection_policy"]["max_output_tags"] = 1
    monkeypatch.setattr(loader, "load_yaml", lambda _relative_path: raw_config)

    with pytest.raises(ValueError, match="requires more tags than max_output_tags"):
        loader.emotion_anchor_selection_policy()


def test_initial_intent_anchor_must_not_bypass_contextual_gate(monkeypatch) -> None:
    loader = get_config_loader()
    raw_config = loader.load_yaml("emotion_space.yaml")
    raw_config["initial_anchor_by_intent"]["development"] = "afraid"
    monkeypatch.setattr(loader, "load_yaml", lambda _relative_path: raw_config)

    with pytest.raises(ValueError, match="initial anchors must be direct VAD anchors"):
        loader.emotion_anchor_selection_policy()


def test_contextual_anchor_gates_require_explicit_evidence_and_preserve_state(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    contextual_ids = set(
        service._anchor_selection_policy["contextual_anchor_gates"]
    )

    without_evidence = service._eligible_anchor_ids("neutral", [])
    assert contextual_ids.isdisjoint(without_evidence)

    severe_risk = service._eligible_anchor_ids(
        "neutral",
        ["explicit_severe_risk"],
    )
    assert "afraid" in severe_risk
    assert "guilty" not in severe_risk

    partial_guilt = service._eligible_anchor_ids(
        "neutral",
        ["specific_negative_impact"],
    )
    assert "guilty" not in partial_guilt
    supported_guilt = service._eligible_anchor_ids(
        "neutral",
        ["specific_negative_impact", "possible_employee_contribution"],
    )
    assert "guilty" in supported_guilt

    # A contextual state can persist long enough to transition away naturally even
    # when the next turn does not repeat the original evidence.
    assert "guilty" in service._eligible_anchor_ids("guilty", [])


def test_setup_options_expose_the_new_anchor_order_without_api_changes() -> None:
    service = object.__new__(SetupService)
    service.loader = get_config_loader()

    options = service.list_options()

    assert [anchor["id"] for anchor in options["emotion_anchors"]] == [
        item[0] for item in EXPECTED_ANCHORS
    ]
    assert options["emotion_anchors"][6]["vad"] == {
        "valence": 0.0,
        "arousal": -0.1,
        "dominance": 0.0,
    }


@pytest.mark.parametrize(
    "intent_id",
    [
        None,
        "development",
        "improvement",
        "development_improvement",
        "exit",
        "improvement_exit",
    ],
)
def test_every_intent_uses_big_five_to_set_initial_vad(
    markov_model: EmotionMarkovModel,
    intent_id: str | None,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    neutral = service.initial_state(intent_id, BigFivePersonality())
    sensitive = service.initial_state(
        intent_id,
        BigFivePersonality(
            openness=50,
            conscientiousness=20,
            extraversion=20,
            agreeableness=20,
            neuroticism=100,
        ),
    )
    composed = service.initial_state(
        intent_id,
        BigFivePersonality(
            openness=50,
            conscientiousness=100,
            extraversion=50,
            agreeableness=100,
            neuroticism=0,
        ),
    )

    assert neutral.current_anchor_id == "neutral"
    assert neutral.current_vad == VADVector(valence=0.0, arousal=-0.1, dominance=0.0)
    assert (
        sensitive.current_vad.valence,
        sensitive.current_vad.arousal,
        sensitive.current_vad.dominance,
    ) == pytest.approx((-0.35, 0.172, -0.35))
    assert (
        composed.current_vad.valence,
        composed.current_vad.arousal,
        composed.current_vad.dominance,
    ) == pytest.approx((0.35, -0.45, 0.35))
    assert sensitive.current_anchor_id == markov_model.nearest_anchor_id(
        sensitive.current_vad
    )
    assert composed.current_anchor_id == markov_model.nearest_anchor_id(
        composed.current_vad
    )
    assert sensitive.has_manager_response is False


def test_initial_personality_bias_cannot_bypass_contextual_anchor_gates(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    personality = BigFivePersonality(
        openness=0,
        conscientiousness=0,
        extraversion=100,
        agreeableness=0,
        neuroticism=100,
    )

    state = service.initial_state(None, personality)
    eligible = service._eligible_anchor_ids(None, [])
    contextual_ids = set(
        service._anchor_selection_policy["contextual_anchor_gates"]
    )

    assert state.current_anchor_id not in contextual_ids
    assert state.current_anchor_id == markov_model.nearest_anchor_id(
        state.current_vad,
        eligible,
    )


def test_base_matrix_rows_are_probabilities_and_favor_nearby_states(
    markov_model: EmotionMarkovModel,
) -> None:
    for anchor in markov_model.anchors:
        probabilities = markov_model.base_transition_probabilities(anchor.id)
        assert list(probabilities) == [item.id for item in markov_model.anchors]
        assert all(probability >= 0.0 for probability in probabilities.values())
        assert sum(probabilities.values()) == pytest.approx(1.0)

    neutral_probabilities = markov_model.base_transition_probabilities("neutral")
    assert neutral_probabilities["calm"] > neutral_probabilities["angry"]
    assert neutral_probabilities["confused"] > neutral_probabilities["angry"]


def test_dense_anchor_space_keeps_a_stable_neutral_baseline(
    markov_model: EmotionMarkovModel,
) -> None:
    probabilities = markov_model.base_transition_probabilities("neutral")
    neutral = markov_model.anchor_by_id["neutral"].vad

    assert probabilities["neutral"] >= 0.20
    for axis in ("valence", "arousal", "dominance"):
        expected = sum(
            probabilities[anchor.id] * getattr(anchor.vad, axis)
            for anchor in markov_model.anchors
        )
        assert abs(expected - getattr(neutral, axis)) <= 0.10

    assert all(
        markov_model.nearest_anchor_id(anchor.vad) == anchor.id
        for anchor in markov_model.anchors
    )


def test_transition_probabilities_use_continuous_current_vad(
    markov_model: EmotionMarkovModel,
) -> None:
    positive_current = VADVector(valence=0.20, arousal=-0.25, dominance=0.10)
    negative_current = VADVector(valence=-0.15, arousal=0.05, dominance=-0.10)
    assert markov_model.nearest_anchor_id(positive_current) == "reflective"
    assert markov_model.nearest_anchor_id(negative_current) == "cautious"

    positive_probabilities = markov_model.transition_probabilities(
        positive_current,
        "neutral",
        VADVector(),
        BigFivePersonality(),
    )
    negative_probabilities = markov_model.transition_probabilities(
        negative_current,
        "neutral",
        VADVector(),
        BigFivePersonality(),
    )

    assert positive_probabilities != negative_probabilities
    assert positive_probabilities["calm"] > negative_probabilities["calm"]


def test_eligible_anchor_mask_is_renormalized_and_applies_to_all_decision_ids(
    markov_model: EmotionMarkovModel,
) -> None:
    eligible = {"neutral", "angry"}
    current = markov_model.anchor_by_id["neutral"].vad
    delta = VADVector(valence=-0.8, arousal=0.8, dominance=0.8)

    probabilities = markov_model.transition_probabilities(
        current,
        "neutral",
        delta,
        BigFivePersonality(),
        eligible,
    )
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert all(
        probability == 0.0
        for anchor_id, probability in probabilities.items()
        if anchor_id not in eligible
    )

    decision = markov_model.transition(
        current_vad=current,
        current_anchor_id="neutral",
        vad_delta=delta,
        personality=BigFivePersonality(),
        requested_strategy="maximum_probability",
        eligible_anchor_ids=eligible,
    )
    assert decision.target_anchor_id in eligible
    assert decision.current_anchor_id in eligible
    assert decision.current_anchor_id == markov_model.nearest_anchor_id(
        decision.next_vad,
        eligible,
    )


@pytest.mark.parametrize(
    ("delta", "expected_anchor"),
    [
        (VADVector(valence=0.35, arousal=-0.20, dominance=0.25), "relieved"),
        (VADVector(valence=-0.30, arousal=0.35, dominance=-0.20), "guarded"),
        (VADVector(valence=-0.10, arousal=0.25, dominance=-0.35), "confused"),
        (VADVector(valence=-0.40, arousal=0.15, dominance=-0.10), "disappointed"),
    ],
)
def test_slow_process_biases_probabilities_toward_expected_anchor(
    markov_model: EmotionMarkovModel,
    delta: VADVector,
    expected_anchor: str,
) -> None:
    neutral = markov_model.anchor_by_id["neutral"].vad
    base = markov_model.base_transition_probabilities("neutral")
    shifted = markov_model.transition_probabilities(
        neutral,
        "neutral",
        delta,
        BigFivePersonality(),
    )

    assert shifted[expected_anchor] > base[expected_anchor]


def test_direct_confrontation_prefers_angry_anchor(
    markov_model: EmotionMarkovModel,
) -> None:
    neutral = markov_model.anchor_by_id["neutral"].vad
    delta = VADVector(valence=-0.8, arousal=0.8, dominance=0.8)
    probabilities = markov_model.transition_probabilities(
        neutral,
        "neutral",
        delta,
        BigFivePersonality(),
    )

    assert max(probabilities, key=probabilities.__getitem__) == "angry"


def test_personality_modulates_initial_vad_and_later_probabilities(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    sensitive = BigFivePersonality(
        openness=50,
        conscientiousness=20,
        extraversion=20,
        agreeableness=20,
        neuroticism=100,
    )
    composed = BigFivePersonality(
        openness=50,
        conscientiousness=100,
        extraversion=50,
        agreeableness=100,
        neuroticism=0,
    )
    sensitive_initial = service.initial_state(None, sensitive).current_vad
    composed_initial = service.initial_state(None, composed).current_vad
    assert sensitive_initial != composed_initial
    assert sensitive_initial.valence < composed_initial.valence
    assert sensitive_initial.arousal > composed_initial.arousal
    assert sensitive_initial.dominance < composed_initial.dominance

    neutral = markov_model.anchor_by_id["neutral"].vad
    delta = VADVector(valence=-0.30, arousal=0.35, dominance=-0.20)
    sensitive_probabilities = markov_model.transition_probabilities(
        neutral,
        "neutral",
        delta,
        sensitive,
    )
    composed_probabilities = markov_model.transition_probabilities(
        neutral,
        "neutral",
        delta,
        composed,
    )
    assert sensitive_probabilities["anxious"] > composed_probabilities["anxious"]
    assert sensitive_probabilities != composed_probabilities


def test_personality_cannot_reverse_the_current_stimulus_direction(
    markov_model: EmotionMarkovModel,
) -> None:
    current = VADVector(valence=-0.40, arousal=-0.40, dominance=-0.40)
    stimulus = VADVector(valence=-0.20, arousal=-0.20, dominance=-0.20)
    personalities = (
        BigFivePersonality(
            openness=100,
            conscientiousness=100,
            extraversion=100,
            agreeableness=100,
            neuroticism=0,
        ),
        BigFivePersonality(
            openness=0,
            conscientiousness=0,
            extraversion=0,
            agreeableness=0,
            neuroticism=100,
        ),
    )

    decisions = [
        markov_model.transition(
            current_vad=current,
            current_anchor_id=markov_model.nearest_anchor_id(current),
            vad_delta=stimulus,
            personality=personality,
            requested_strategy="expected_value",
        )
        for personality in personalities
    ]

    assert decisions[0].probabilities != decisions[1].probabilities
    assert any(decision.actual_delta != VADVector() for decision in decisions)
    for decision in decisions:
        for axis in ("valence", "arousal", "dominance"):
            assert (
                getattr(decision.actual_delta, axis) * getattr(stimulus, axis)
                >= 0.0
            )


def test_neutral_input_does_not_drift_and_transitions_are_bounded_and_reproducible(
    markov_model: EmotionMarkovModel,
) -> None:
    current = VADVector(valence=0.12, arousal=-0.08, dominance=0.04)
    neutral_decision = markov_model.transition(
        current,
        "neutral",
        VADVector(valence=-0.03, arousal=0.04, dominance=0.0),
        BigFivePersonality(neuroticism=100),
        "expected_value",
    )
    assert neutral_decision.next_vad == current
    assert neutral_decision.actual_delta == VADVector()
    assert neutral_decision.transition_rate == 0.0
    assert neutral_decision.current_anchor_id == markov_model.nearest_anchor_id(
        current
    )
    assert neutral_decision.target_anchor_id == neutral_decision.current_anchor_id

    kwargs = {
        "current_vad": current,
        "current_anchor_id": "neutral",
        "vad_delta": VADVector(valence=1.0, arousal=-1.0, dominance=1.0),
        "personality": BigFivePersonality(openness=100, extraversion=100),
        "requested_strategy": "expected_value",
    }
    first = markov_model.transition(**kwargs)
    second = markov_model.transition(**kwargs)
    assert first == second
    assert first.strategy == "expected_value"
    assert max(
        abs(first.actual_delta.valence),
        abs(first.actual_delta.arousal),
        abs(first.actual_delta.dominance),
    ) <= 0.35

    sampling = markov_model.transition(
        current,
        "neutral",
        VADVector(valence=0.20, arousal=0.0, dominance=0.0),
        BigFivePersonality(),
        "sampling",
    )
    assert sampling.strategy == "expected_value"


def test_legacy_anchor_mapping_preserves_continuous_vad_until_normal_save() -> None:
    original_vad = VADVector(valence=-0.41, arousal=0.19, dominance=0.27)
    state = SessionState(
        session_id="legacy-session",
        emotion_state=EmotionState(
            current_vad=original_vad,
            current_anchor_id="skeptical_controlled",
        ),
    )

    class Repository:
        saved: SessionState | None = None

        def get(self, session_id: str) -> SessionState:
            assert session_id == state.session_id
            return state

        def save(self, next_state: SessionState) -> SessionState:
            self.saved = next_state
            return next_state

    repository = Repository()
    service = SessionService(repo=repository, runtime_recycler=object())
    loaded = service.get_session(state.session_id)

    assert loaded.emotion_state is not None
    assert loaded.emotion_state.current_anchor_id == "guarded"
    assert loaded.emotion_state.current_vad == original_vad
    assert repository.saved is None

    service.save_session(loaded)
    assert repository.saved is loaded
    assert repository.saved.emotion_state is not None
    assert repository.saved.emotion_state.current_anchor_id == "guarded"
    assert repository.saved.emotion_state.current_vad == original_vad

    assert get_config_loader().emotion_anchor_aliases() == {
        "calm_receptive": "calm",
        "cautious_neutral": "neutral",
        "skeptical_controlled": "guarded",
        "disappointed_withdrawn": "disappointed",
        "anxious_defensive": "anxious",
        "defensive_resistant": "guarded",
        "angry_challenging": "angry",
        "hopeful_negotiating": "engaged",
        "aligned_ready": "pleased",
    }


@pytest.mark.parametrize(
    ("actual_delta", "expected_cue_field"),
    [
        (
            VADVector(valence=0.31, arousal=0.08, dominance=0.06),
            "stance_cue",
        ),
        (
            VADVector(valence=0.06, arousal=0.31, dominance=0.08),
            "rhythm_cue",
        ),
        (
            VADVector(valence=0.06, arousal=0.08, dominance=0.31),
            "agency_cue",
        ),
    ],
    ids=("valence-stance", "arousal-rhythm", "dominance-agency"),
)
def test_expression_guidance_is_specific_for_all_forty_eight_anchors_and_axes(
    markov_model: EmotionMarkovModel,
    actual_delta: VADVector,
    expected_cue_field: str,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    loader = get_config_loader()
    profiles = loader.emotion_expression_profiles()
    guidances: set[str] = set()

    for anchor_id, profile in profiles.items():
        guidance = service._expression_guidance(
            anchor_id=anchor_id,
            current_vad=markov_model.anchor_by_id[anchor_id].vad,
            actual_delta=actual_delta,
        )
        assert profile[expected_cue_field].rstrip("。；") in guidance
        assert profile["avoid"].rstrip("。；") in guidance
        assert all(
            invariant.rstrip("。；") in guidance
            for invariant in loader.emotion_expression_policy()["invariants"]
        )
        assert "情绪层只调节当前表达方式与外显强度" in guidance
        assert "Human Turn Selection" in guidance
        assert markov_model.anchor_by_id[anchor_id].name in guidance
        assert "opening_cue" not in guidance
        assert "可选 opening" not in guidance
        assert "首句" not in guidance
        assert "回复开头" not in guidance
        assert "至少两类可观察信号" not in guidance
        assert "VAD" not in guidance
        assert "。：" not in guidance
        assert "。。" not in guidance
        guidances.add(guidance)

    assert len(guidances) == 48


@pytest.mark.parametrize(
    ("actual_delta", "expected_strength", "expected_requirement"),
    [
        (
            VADVector(valence=0.05),
            "本轮内部情绪变化轻微",
            "若自然外显",
        ),
        (
            VADVector(valence=0.13),
            "本轮内部情绪变化明确",
            "不要求多个信号",
        ),
        (
            VADVector(valence=0.31),
            "本轮内部情绪变化强",
            "不要求固定信号数量",
        ),
    ],
)
def test_expression_guidance_offers_intensity_scaled_optional_cues(
    markov_model: EmotionMarkovModel,
    actual_delta: VADVector,
    expected_strength: str,
    expected_requirement: str,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    guidance = service._expression_guidance(
        anchor_id="neutral",
        current_vad=markov_model.anchor_by_id["neutral"].vad,
        actual_delta=actual_delta,
    )

    assert expected_strength in guidance
    assert expected_requirement in guidance


@pytest.mark.parametrize(
    ("anchor_id", "actual_delta", "expected_cue_field"),
    [
        ("neutral", VADVector(), "rhythm_cue"),
        ("confident", VADVector(valence=0.03), "agency_cue"),
    ],
)
def test_stable_expression_guidance_uses_current_anchor_dominant_axis(
    markov_model: EmotionMarkovModel,
    anchor_id: str,
    actual_delta: VADVector,
    expected_cue_field: str,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    profile = get_config_loader().emotion_expression_profiles()[anchor_id]

    guidance = service._expression_guidance(
        anchor_id=anchor_id,
        current_vad=markov_model.anchor_by_id[anchor_id].vad,
        actual_delta=actual_delta,
    )

    assert "本轮内部情绪基本稳定" in guidance
    assert profile[expected_cue_field].rstrip("。；") in guidance
    assert "opening_cue" not in guidance
    assert "首句" not in guidance
    assert "回复开头" not in guidance


@pytest.mark.parametrize(
    "template_path",
    ("employee/reply.jinja2", "employee/reply_test.jinja2"),
)
@pytest.mark.parametrize(
    ("actual_delta", "expected_cue_field"),
    [
        (VADVector(valence=0.31), "stance_cue"),
        (VADVector(arousal=0.31), "rhythm_cue"),
        (VADVector(dominance=0.31), "agency_cue"),
    ],
)
def test_reply_templates_receive_axis_specific_expression_guidance(
    markov_model: EmotionMarkovModel,
    template_path: str,
    actual_delta: VADVector,
    expected_cue_field: str,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    profile = get_config_loader().emotion_expression_profiles()["neutral"]
    guidance = service._expression_guidance(
        anchor_id="neutral",
        current_vad=markov_model.anchor_by_id["neutral"].vad,
        actual_delta=actual_delta,
    )

    rendered = PromptService().render(
        template_path,
        emotion_state={"reply_emotion_guidance": guidance},
    )

    assert profile[expected_cue_field].rstrip("。；") in rendered
    assert "情绪锚点和表达提示不是事实、立场或行动证据" in rendered
    assert "opening_cue" not in rendered
    assert "首句" not in guidance
    assert "回复开头" not in guidance


def test_rule_fallback_uses_markov_model_and_optional_expression_guidance(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    state = SessionState(
        session_id="fallback",
        personality=BigFivePersonality(),
        emotion_state=service.initial_state(None),
    )

    neutral_output = service._fallback_transition(state, "我们继续讨论。")
    neutral_result = service._apply_transition(state, neutral_output, "我们继续讨论。")
    assert neutral_result.current_vad == VADVector(valence=0.0, arousal=-0.1, dominance=0.0)

    positive_output = service._fallback_transition(state, "我理解你的顾虑，我们一起讨论支持计划。")
    assert positive_output.vad_delta == VADVector(valence=0.35, arousal=-0.20, dominance=0.25)
    assert positive_output.appraisal_tags == []
    positive_result = service._apply_transition(state, positive_output, "我理解并支持你。")
    assert positive_result.current_anchor_id == markov_model.nearest_anchor_id(
        positive_result.current_vad,
        service._eligible_anchor_ids("neutral", positive_output.appraisal_tags),
    )

    negative_output = service._fallback_transition(state, "别找理由，这就是你的问题，必须接受。")
    assert negative_output.vad_delta == VADVector(
        valence=-0.40,
        arousal=0.40,
        dominance=-0.25,
    )
    assert negative_output.appraisal_tags == []
    negative_result = service._apply_transition(state, negative_output, "别找理由。")
    assert negative_result.current_anchor_id == markov_model.nearest_anchor_id(
        negative_result.current_vad,
        service._eligible_anchor_ids("neutral", negative_output.appraisal_tags),
    )

    strong_output = EmotionTransitionStructuredOutput(
        vad_delta=VADVector(valence=-0.8, arousal=0.8, dominance=0.8),
        transition_strategy="maximum_probability",
        reason_summary="direct_confrontation",
    )
    strong_result = service._apply_transition(state, strong_output, "这就是你的问题。")
    assert strong_result.current_anchor_id == markov_model.nearest_anchor_id(
        strong_result.current_vad,
        service._eligible_anchor_ids("neutral", strong_output.appraisal_tags),
    )
    assert strong_result.reply_emotion_guidance is not None
    assert "不要求固定信号数量" in strong_result.reply_emotion_guidance
    assert "首句" not in strong_result.reply_emotion_guidance
    assert "回复开头" not in strong_result.reply_emotion_guidance
    assert "至少两类可观察信号" not in strong_result.reply_emotion_guidance
    assert "VAD" not in strong_result.reply_emotion_guidance

    unavailable_support = service._fallback_transition(
        state,
        "目前无法提供支持或资源。",
    )
    assert unavailable_support.vad_delta == VADVector(
        valence=-0.03,
        arousal=0.04,
        dominance=0.0,
    )
    concrete_problem = service._fallback_transition(
        state,
        "这个具体问题很严重。",
    )
    assert concrete_problem.vad_delta == VADVector(
        valence=-0.03,
        arousal=0.04,
        dominance=0.0,
    )
    shared_obligation = service._fallback_transition(
        state,
        "我们必须一起解决这个问题。",
    )
    assert shared_obligation.vad_delta == VADVector(
        valence=0.35,
        arousal=-0.20,
        dominance=0.25,
    )
    mixed_signal = service._fallback_transition(
        state,
        "我理解你的顾虑，但你必须接受这个决定。",
    )
    assert mixed_signal.vad_delta == VADVector(
        valence=-0.03,
        arousal=0.04,
        dominance=0.0,
    )
    negated_support = service._fallback_transition(
        state,
        "目前不会提供支持，也不会支持你。",
    )
    assert negated_support.vad_delta == VADVector(
        valence=-0.03,
        arousal=0.04,
        dominance=0.0,
    )
    local_negation = service._fallback_transition(
        state,
        "这不是威胁，但你必须服从。",
    )
    assert local_negation.vad_delta == VADVector(
        valence=-0.40,
        arousal=0.40,
        dominance=-0.25,
    )
    negated_dismissal = service._fallback_transition(
        state,
        "我们不会解雇你。",
    )
    assert negated_dismissal.vad_delta == VADVector(
        valence=-0.03,
        arousal=0.04,
        dominance=0.0,
    )
    severe_risk = service._fallback_transition(state, "如果再这样就会立即解雇。")
    assert severe_risk.appraisal_tags == []
    assert severe_risk.vad_delta.dominance == -0.25
    assert "afraid" not in service._eligible_anchor_ids(
        "neutral",
        severe_risk.appraisal_tags,
    )


@pytest.mark.parametrize(
    ("delta", "expected_anchor", "minimum_visible_change"),
    [
        (VADVector(valence=0.35, arousal=-0.20, dominance=0.25), "relieved", 0.20),
        (VADVector(valence=-0.30, arousal=0.35, dominance=-0.20), "embarrassed", 0.20),
        (VADVector(valence=-0.60, arousal=0.65, dominance=-0.45), "afraid", 0.30),
        (VADVector(valence=-0.80, arousal=0.80, dominance=0.80), "angry", 0.30),
    ],
)
def test_calibrated_transitions_are_visible_but_stimulus_bounded(
    markov_model: EmotionMarkovModel,
    delta: VADVector,
    expected_anchor: str,
    minimum_visible_change: float,
) -> None:
    neutral = markov_model.anchor_by_id["neutral"].vad
    decision = markov_model.transition(
        current_vad=neutral,
        current_anchor_id="neutral",
        vad_delta=delta,
        personality=BigFivePersonality(),
        requested_strategy="maximum_probability",
    )

    assert decision.strategy == "maximum_probability"
    assert decision.target_anchor_id == expected_anchor
    assert decision.current_anchor_id == markov_model.nearest_anchor_id(
        decision.next_vad
    )
    actual = decision.actual_delta
    assert max(
        abs(actual.valence),
        abs(actual.arousal),
        abs(actual.dominance),
    ) >= minimum_visible_change
    for axis in ("valence", "arousal", "dominance"):
        stimulus = getattr(delta, axis)
        applied = getattr(actual, axis)
        axis_budget = min(
            markov_model.config.max_axis_step,
            max(
                0.0,
                abs(stimulus) - markov_model.config.neutral_delta_epsilon,
            ),
        )
        assert abs(applied) <= axis_budget + 1e-9
        assert applied * stimulus >= 0.0


def test_expected_strategy_is_continuous_across_decisive_threshold(
    markov_model: EmotionMarkovModel,
) -> None:
    neutral = markov_model.anchor_by_id["neutral"].vad

    decisions = [
        markov_model.transition(
            current_vad=neutral,
            current_anchor_id="neutral",
            vad_delta=VADVector(valence=-value, arousal=value),
            personality=BigFivePersonality(),
            requested_strategy="expected_value",
        )
        for value in (0.349, 0.350, 0.351)
    ]

    assert all(decision.strategy == "expected_value" for decision in decisions)
    assert len({decision.current_anchor_id for decision in decisions}) == 1
    for earlier, later in zip(decisions[:-1], decisions[1:], strict=True):
        for axis in ("valence", "arousal", "dominance"):
            assert abs(
                getattr(later.actual_delta, axis)
                - getattr(earlier.actual_delta, axis)
            ) <= 0.01


def test_strong_expected_strategy_remains_visible_and_bounded(
    markov_model: EmotionMarkovModel,
) -> None:
    delta = VADVector(valence=-0.60, arousal=0.65, dominance=-0.45)
    decision = markov_model.transition(
        current_vad=markov_model.anchor_by_id["neutral"].vad,
        current_anchor_id="neutral",
        vad_delta=delta,
        personality=BigFivePersonality(),
        requested_strategy="expected_value",
    )

    assert decision.strategy == "expected_value"
    assert max(
        abs(decision.actual_delta.valence),
        abs(decision.actual_delta.arousal),
        abs(decision.actual_delta.dominance),
    ) >= 0.30
    for axis in ("valence", "arousal", "dominance"):
        applied = getattr(decision.actual_delta, axis)
        stimulus = getattr(delta, axis)
        assert abs(applied) <= min(
            markov_model.config.max_axis_step,
            abs(stimulus) - markov_model.config.neutral_delta_epsilon,
        ) + 1e-9
        assert applied * stimulus >= 0.0


def test_explicit_maximum_strategy_blends_continuously_at_threshold(
    markov_model: EmotionMarkovModel,
) -> None:
    calm = markov_model.anchor_by_id["calm"].vad

    decisions = [
        markov_model.transition(
            current_vad=calm,
            current_anchor_id="calm",
            vad_delta=VADVector(arousal=value),
            personality=BigFivePersonality(),
            requested_strategy="maximum_probability",
        )
        for value in (0.349, 0.350, 0.351)
    ]

    assert [decision.strategy for decision in decisions] == [
        "expected_value",
        "maximum_probability",
        "maximum_probability",
    ]
    assert decisions[1].actual_delta.arousal > 0.0
    assert decisions[2].actual_delta.arousal > 0.0
    assert all(
        decision.target_anchor_id
        == max(decision.probabilities, key=decision.probabilities.__getitem__)
        for decision in decisions[1:]
    )
    assert markov_model._decisive_target_blend(0.350) == pytest.approx(0.0)
    assert markov_model._decisive_target_blend(0.351) > 0.0
    assert markov_model._decisive_target_blend(1.0) == pytest.approx(1.0)
    for earlier, later in zip(decisions[:-1], decisions[1:], strict=True):
        assert abs(
            later.actual_delta.arousal - earlier.actual_delta.arousal
        ) <= 0.01


def test_maximum_numeric_target_is_continuous_across_argmax_crossing(
    markov_model: EmotionMarkovModel,
) -> None:
    tired = markov_model.anchor_by_id["tired"].vad
    decisions = [
        markov_model.transition(
            current_vad=tired,
            current_anchor_id="tired",
            vad_delta=VADVector(
                valence=-value,
                arousal=-value,
                dominance=-value,
            ),
            personality=BigFivePersonality(),
            requested_strategy="maximum_probability",
        )
        for value in (0.787, 0.788)
    ]
    top_anchors = [
        max(decision.probabilities, key=decision.probabilities.__getitem__)
        for decision in decisions
    ]

    assert top_anchors == ["tired", "sad"]
    assert [decision.target_anchor_id for decision in decisions] == top_anchors
    for earlier, later in zip(decisions[:-1], decisions[1:], strict=True):
        for axis in ("valence", "arousal", "dominance"):
            assert abs(
                getattr(later.actual_delta, axis)
                - getattr(earlier.actual_delta, axis)
            ) <= 0.01


def test_neutral_deadband_enters_continuously(
    markov_model: EmotionMarkovModel,
) -> None:
    neutral = markov_model.anchor_by_id["neutral"].vad

    decisions = []
    for value in (0.049, 0.050, 0.051):
        decision = markov_model.transition(
            current_vad=neutral,
            current_anchor_id="neutral",
            vad_delta=VADVector(valence=value),
            personality=BigFivePersonality(),
            requested_strategy="expected_value",
        )
        decisions.append(decision)

    changes = [abs(decision.actual_delta.valence) for decision in decisions]
    assert changes[0] == pytest.approx(0.0)
    assert changes[1] == pytest.approx(0.0)
    assert changes[2] > 0.0
    assert changes[2] <= 0.001 + 1e-9
    assert decisions[1].transition_rate == pytest.approx(0.0)
    assert decisions[1].target_anchor_id == decisions[1].current_anchor_id


def test_repeated_support_recovers_from_anxiety_gradually(
    markov_model: EmotionMarkovModel,
) -> None:
    current_vad = markov_model.anchor_by_id["anxious"].vad
    current_anchor_id = "anxious"
    support = VADVector(valence=0.35, arousal=-0.20, dominance=0.25)
    observed_anchors: list[str] = []
    observed_vads: list[VADVector] = []

    for _ in range(4):
        decision = markov_model.transition(
            current_vad=current_vad,
            current_anchor_id=current_anchor_id,
            vad_delta=support,
            personality=BigFivePersonality(),
            requested_strategy="expected_value",
        )
        current_vad = decision.next_vad
        current_anchor_id = decision.current_anchor_id
        observed_anchors.append(current_anchor_id)
        observed_vads.append(current_vad)

    assert all(
        anchor_id == markov_model.nearest_anchor_id(vad)
        for anchor_id, vad in zip(observed_anchors, observed_vads, strict=True)
    )
    assert observed_vads[-1].valence > 0.0
    assert all(
        later.valence >= earlier.valence
        and later.arousal <= earlier.arousal
        and later.dominance >= earlier.dominance
        for earlier, later in zip(
            observed_vads[:-1],
            observed_vads[1:],
            strict=True,
        )
    )


def test_emotion_transition_prompt_uses_visible_intensity_calibration(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    state = SessionState(
        session_id="prompt-calibration",
        personality=BigFivePersonality(),
        emotion_state=service.initial_state(None),
    )
    prompt = service._build_prompt(state, "我们需要明确讨论这次绩效差距。")

    assert "没有明显情绪刺激时各轴通常不超过0.04" in prompt
    assert "明确反馈、支持或施压的主轴通常为0.30到0.55" in prompt
    assert "强烈对抗、威胁或重大认可的主轴通常为0.60到0.90" in prompt
    assert "不得把明确刺激压缩为接近零的变化" in prompt
    assert "变化方向与幅度预算" in prompt
    assert "主轴达到0.35以上" in prompt


def test_emotion_prompt_keeps_anchor_semantics_without_exposing_contextual_gates(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    state = SessionState(
        session_id="prompt-anchor-semantics",
        personality=BigFivePersonality(),
        emotion_state=service.initial_state(None),
    )

    prompt = service._build_prompt(state, "请说说你现在的感受。")
    context = json.loads(prompt.rpartition("context=")[2])

    expected_anchors = [
        {
            "id": anchor.id,
            "name": anchor.name,
            "description": " ".join(anchor.description.split()),
            "vad": anchor.vad.model_dump(mode="json"),
        }
        for anchor in get_config_loader().emotion_anchors().values()
    ]
    assert context["emotion_anchors"] == expected_anchors
    assert all(
        set(anchor) == {"id", "name", "description", "vad"}
        and anchor["description"].strip()
        and "\n" not in anchor["description"]
        for anchor in context["emotion_anchors"]
    )
    assert set(context["emotion_appraisal_policy"]) == {
        "max_output_tags",
        "appraisal_tags",
    }
    assert "contextual_anchor_gates" not in context["emotion_appraisal_policy"]


def test_stimulus_intensity_uses_the_strongest_vad_axis(
    markov_model: EmotionMarkovModel,
) -> None:
    delta = VADVector(valence=0.30, arousal=0.0, dominance=0.0)

    decision = markov_model.transition(
        current_vad=markov_model.anchor_by_id["neutral"].vad,
        current_anchor_id="neutral",
        vad_delta=delta,
        personality=BigFivePersonality(),
        requested_strategy="expected_value",
    )

    assert markov_model.stimulus_intensity(delta) == pytest.approx(0.30)
    assert decision.intensity == pytest.approx(0.30)
    assert decision.strategy == "expected_value"

    below_threshold = markov_model.transition(
        current_vad=markov_model.anchor_by_id["neutral"].vad,
        current_anchor_id="neutral",
        vad_delta=VADVector(valence=0.34),
        personality=BigFivePersonality(),
        requested_strategy="maximum_probability",
    )
    at_threshold = markov_model.transition(
        current_vad=markov_model.anchor_by_id["neutral"].vad,
        current_anchor_id="neutral",
        vad_delta=VADVector(valence=0.35),
        personality=BigFivePersonality(),
        requested_strategy="maximum_probability",
    )
    assert below_threshold.strategy == "expected_value"
    assert at_threshold.strategy == "maximum_probability"


def test_emotion_prompt_deduplicates_latest_manager_turn_and_uses_schema_wording(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    manager_message = "这个结果和岗位要求之间仍有明显差距。"
    state = SessionState(
        session_id="prompt-deduplication",
        personality=BigFivePersonality(),
        emotion_state=service.initial_state(None),
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="manager",
                text=manager_message,
            )
        ],
    )

    prompt = service._build_prompt(state, manager_message)

    assert prompt.count(manager_message) == 1
    assert "语义方向校准" in prompt
    assert "appraisal_tags必填且必须是数组" in prompt
    assert "possible_employee_contribution" in prompt
    assert "只返回一个匹配响应Schema的对象" in prompt
    assert "必须调用指定工具" not in prompt


def test_emotion_prompt_projects_history_and_current_state_semantics(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    emotion_state = service.initial_state(None)
    emotion_state.reply_emotion_guidance = "PREVIOUS_GUIDANCE_MUST_NOT_APPEAR"
    state = SessionState(
        session_id="emotion-history-projection",
        personality=BigFivePersonality(),
        emotion_state=emotion_state,
        conversation=[
            ConversationTurn(
                turn_index=1,
                speaker="employee",
                text="我会先核对交付事实。",
                metadata={
                    "rehearsal_timing": {
                        "raw_operational_payload": "OPERATIONAL_SENTINEL"
                    },
                    "emotion_snapshot": {
                        "reason_summary": "HISTORICAL_INFERENCE_SENTINEL"
                    },
                },
            )
        ],
    )

    prompt = service._build_prompt(state, "请继续说明。")

    assert "我会先核对交付事实。" in prompt
    assert "created_at" not in prompt
    assert "rehearsal_timing" not in prompt
    assert "OPERATIONAL_SENTINEL" not in prompt
    assert "emotion_snapshot" not in prompt
    assert "HISTORICAL_INFERENCE_SENTINEL" not in prompt
    assert "reply_emotion_guidance" not in prompt
    assert "PREVIOUS_GUIDANCE_MUST_NOT_APPEAR" not in prompt
    assert "updated_at" not in prompt


def test_appraisal_tag_normalizer_filters_unknown_duplicates_and_policy_overflow(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    payload, repair_steps = service._normalize_structured_payload(
        {
            "vad_delta": {"valence": 0.1, "arousal": 0.0, "dominance": 0.0},
            "transition_strategy": "expected_value",
            "appraisalTags": [
                "explicit_severe_risk",
                "explicit_severe_risk",
                "unknown_tag",
                "concrete_support_or_investment",
                "explicit_expectation_gap",
                "load_exceeds_capacity",
                "no_controllable_path",
            ],
        }
    )

    assert payload["appraisal_tags"] == [
        "explicit_severe_risk",
        "concrete_support_or_investment",
        "explicit_expectation_gap",
        "load_exceeds_capacity",
    ]
    assert "mapped_appraisalTags_to_appraisal_tags" in repair_steps
    assert "deduplicated_appraisal_tag" in repair_steps
    assert "discarded_unknown_appraisal_tag" in repair_steps
    assert "truncated_appraisal_tags" in repair_steps


def test_appraisal_tag_mapping_only_accepts_boolean_true(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    payload, repair_steps = service._normalize_structured_payload(
        {
            "vad_delta": {"valence": 0.0, "arousal": 0.0, "dominance": 0.0},
            "transition_strategy": "expected_value",
            "appraisal_tags": {
                "explicit_severe_risk": "false",
                "concrete_support_or_investment": True,
                "explicit_expectation_gap": 1,
                "no_controllable_path": False,
            },
        }
    )

    assert payload["appraisal_tags"] == ["concrete_support_or_investment"]
    assert "converted_appraisal_tags_mapping" in repair_steps
    assert "discarded_non_true_appraisal_tag_flags" in repair_steps


@pytest.mark.parametrize("stored_anchor_id", [None, "removed_anchor"])
def test_transition_repairs_missing_or_unknown_stored_anchor(
    markov_model: EmotionMarkovModel,
    stored_anchor_id: str | None,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    cautious_vad = markov_model.anchor_by_id["cautious"].vad
    state = SessionState(
        session_id="stored-anchor-repair",
        emotion_state=EmotionState(
            current_vad=cautious_vad,
            current_anchor_id=stored_anchor_id,
        ),
    )

    result = service._apply_transition(
        state,
        EmotionTransitionStructuredOutput(
            vad_delta=VADVector(),
            transition_strategy="expected_value",
        ),
        "继续讨论。",
    )

    assert result.current_anchor_id == "cautious"
    assert result.current_vad == cautious_vad


def test_same_anchor_guidance_keeps_material_vad_movement_internal_by_default(
    markov_model: EmotionMarkovModel,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)

    guidance = service._expression_guidance(
        anchor_id="neutral",
        previous_anchor_id="neutral",
        current_vad=VADVector(valence=0.14, arousal=-0.10, dominance=0.0),
        actual_delta=VADVector(valence=0.14),
    )

    assert "本轮内部情绪变化明确" in guidance
    assert "正向情绪色彩可略增强" in guidance
    assert "锚点未变但内部情绪已移动" in guidance
    assert "仅在本轮选择外显时" in guidance
    assert "不得照搬上一轮" not in guidance


@pytest.mark.asyncio
async def test_emotion_transition_uses_json_schema_response_format(
    markov_model: EmotionMarkovModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class CapturingLLM:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            return EmotionTransitionStructuredOutput(
                vad_delta=VADVector(valence=0.1),
                transition_strategy="expected_value",
                reason_summary="supportive signal",
            )

    settings = SimpleNamespace(
        model_for_task=lambda task_name: "emotion-model",
        temperature_for_task=lambda task_name: 0.0,
        max_tokens_for_task=lambda task_name: 512,
        timeout_for_task=lambda task_name: 20.0,
        enable_thinking_for_task=lambda task_name: False,
    )
    monkeypatch.setattr(
        emotion_transition_module,
        "LangChainLLMService",
        CapturingLLM,
    )
    monkeypatch.setattr(emotion_transition_module, "get_settings", lambda: settings)
    service = EmotionTransitionService(markov_model=markov_model)
    state = SessionState(
        session_id="emotion-json-schema",
        emotion_state=service.initial_state(None),
    )

    await service.update_after_manager_message(state, "这部分工作有明显进展。")

    assert len(calls) == 1
    call = calls[0]
    assert call["schema"] is EmotionTransitionStructuredOutput
    assert call["structured_transport"] == "json_schema"
    assert call["json_schema_strict"] is False
    assert "tools" not in call
    assert "tool_choice" not in call


def test_applied_transition_persists_diagnostics_and_snapshot(
    markov_model: EmotionMarkovModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EmotionTransitionService(markov_model=markov_model)
    state = SessionState(
        session_id="transition-diagnostics",
        personality=BigFivePersonality(),
        emotion_state=service.initial_state(None),
    )
    recorded: dict[str, object] = {}
    monkeypatch.setattr(
        "backend.services.emotion_transition_service.record_emotion_transition",
        lambda **fields: recorded.update(fields),
    )

    result = service._apply_transition(
        state,
        EmotionTransitionStructuredOutput(
            vad_delta=VADVector(valence=0.14),
            transition_strategy="expected_value",
            reason_summary="recognition",
        ),
        "这部分工作已经取得了进展。",
    )

    assert result.previous_anchor_id == "neutral"
    assert result.last_vad_delta != VADVector()
    assert result.transition_intensity == pytest.approx(0.14)
    assert recorded["source_anchor"] == "neutral"
    assert recorded["actual_change"] == pytest.approx(
        max(
            abs(result.last_vad_delta.valence),
            abs(result.last_vad_delta.arousal),
            abs(result.last_vad_delta.dominance),
        )
    )

    turn = ConversationTurn(turn_index=1, speaker="manager", text="测试")
    attach_emotion_snapshot(turn, result)
    snapshot = turn.metadata["emotion_snapshot"]
    assert snapshot["previous_anchor_id"] == "neutral"
    assert snapshot["vad_delta"] == result.last_vad_delta.model_dump()
    assert snapshot["transition_intensity"] == pytest.approx(0.14)

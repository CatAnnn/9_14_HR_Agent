from __future__ import annotations

from functools import lru_cache
import json

from backend.business_config.loader import BusinessConfigLoader, get_config_loader
from backend.schemas.personality_behavior import (
    PersonalityBandDescriptions,
    PersonalityBehaviorMap,
    PersonalityBehaviorProfile,
    PersonalityBehaviorScore,
    PersonalityCombinationCondition,
    PersonalityScoreBand,
)
from backend.schemas.personality_facets import PersonalityFacetState
from backend.schemas.simulation import BigFivePersonality


_PERSONALITY_RUNTIME_RULES = """【一、人格输入】
* 五项总维度与已提供的 facet 分数是整场面谈中的稳定参数；只使用 scores 中当前命中的唯一档位，不补回相反端或其他档位。
* detailed_description 采用 NEO/IPIP 构念解释当前档位；其中“本档特有校准”是当前分档相对于相邻档最具体的差异，不得套用相邻档校准。分数高低只改变倾向方向和稳定程度，不代表能力、道德、事实或临床状态。

【十、五维组合优先】
* 先确定本轮必须回应的事实、问题、立场或行动内容，再选择被真实触发的人格通道；人格只能改变该内容的注意顺序、措辞、互动方式、承诺边界和跨轮残留。
* 先判断当前话题与真实刺激实际触发了哪些构念；没有直接关联的总维度或 facet 保持静默。
* activation_context 是必要触发门槛；人格分数、“更容易显现”的情境或模型推测都不能替代当前输入中的触发证据。缺少触发证据时必须静默；“较少显现”的情境命中时，除非存在更强的直接触发证据，否则也保持静默。
* 通常只让最能解释当前反应的一项或少量 facet 成为主要行为通道；多个构念确实共同作用时可以合并，但不得为了展示完整画像逐项堆叠同义信号。
* 每个行为通道优先服从最具体的 facet，总维度只作宽泛基线；发生表面冲突时按当前行为所对应的 facet 和真实情境分别解释，不机械平均，也不把父维度和同向 facet 双重放大。
* 档位越极端只表示相关场景中的方向更稳定，不通过增加观点、句数、情绪或计划细节来模拟强度。
* 本档具体线索是构念激活后更可能出现的例子，不是每轮必须逐项展示的清单；只选最符合当前问题的一项或自然保持隐性。
* 同一稳定人格可因话题变化而显现不同通道，但不得仅因 Manager 改变语气就在相同事实下反转；人格信号可以只改变内部权重，无须每轮显性说出。
* 情境调节只用于判断人格是否容易显现，不得直接复制进 Employee 的正文。

【人格与其他状态的边界】
* 事实、当前问题、Acceptance/Dispute、motivation 和 emotion_state 先决定回应内容；人格只影响注意、体验、表达、互动与行动组织方式。
* personality 是稳定倾向，emotion_state 是当前实际情绪；人格可以调节对已存在刺激的敏感性，但不得创造刺激或情绪。
* 人格不得创建事实、立场、分歧、认可、自我批评、诉求、责任、资源、期限或承诺，也不得在当前问题未要求计划或信息不足时凭空增加计划内容。
* 未确认的责任、资源、期限和承诺不得写成既定事实。普通自评或宽泛发展讨论不自动触发完整计划。"""


class PersonalityBehaviorResolver:
    """Resolve every Big Five and facet score to exactly one seven-band rule."""

    def __init__(self, loader: BusinessConfigLoader | None = None):
        self.loader = loader or get_config_loader()

    def resolve(
        self,
        personality: BigFivePersonality | None,
        facets: PersonalityFacetState | None,
    ) -> PersonalityBehaviorProfile | None:
        if personality is None:
            return None
        dimension_scores = tuple(
            (dimension_id, int(getattr(personality, dimension_id)))
            for dimension_id in BigFivePersonality.model_fields
        )
        facet_context = facets.model_context() if facets is not None else {}
        facet_scores: list[tuple[str, int]] = []
        for dimension_id, selector in self.loader.personality_behavior_map().dimensions.items():
            group = facet_context.get(dimension_id) or {}
            values = group.get("facets") or {}
            for facet_id in selector.facets:
                value = values.get(facet_id)
                if value is not None:
                    facet_scores.append(
                        (f"{dimension_id}.{facet_id}", int(value))
                    )

        config_hash = self.loader.personality_behavior_map_hash()
        return self._resolve_cached(
            config_hash,
            dimension_scores,
            tuple(facet_scores),
        )

    @lru_cache(maxsize=512)
    def _resolve_cached(
        self,
        config_hash: str,
        dimension_scores: tuple[tuple[str, int], ...],
        facet_scores: tuple[tuple[str, int], ...],
    ) -> PersonalityBehaviorProfile:
        config = self.loader.personality_behavior_map()
        current_hash = self.loader.personality_behavior_map_hash()
        if current_hash != config_hash:
            return self._resolve_cached(
                current_hash,
                dimension_scores,
                facet_scores,
            )

        raw_scores = dict((*dimension_scores, *facet_scores))
        bands = {
            trait_id: self._band_for_score(config, score)
            for trait_id, score in raw_scores.items()
        }
        matched_combination_ids: list[str] = []
        matched_combination_rules: list[str] = []
        for rule in config.combination_rules:
            matched = self._combination_matches(
                rule.all_of,
                rule.any_of,
                bands,
            )
            if matched:
                matched_combination_ids.append(rule.id)
                matched_combination_rules.append(rule.guidance)

        trait_profiles: dict[str, tuple[str, PersonalityBandDescriptions]] = {}
        for dimension_id, selector in config.dimensions.items():
            trait_profiles[dimension_id] = (
                selector.display_name,
                selector.band_descriptions,
            )
            for facet_id, facet_selector in selector.facets.items():
                trait_profiles[f"{dimension_id}.{facet_id}"] = (
                    facet_selector.display_name,
                    facet_selector.band_descriptions,
                )

        score_payload: list[PersonalityBehaviorScore] = []
        for trait_id, score in (*dimension_scores, *facet_scores):
            band = bands[trait_id]
            display_name, band_descriptions = trait_profiles[trait_id]
            score_payload.append(
                PersonalityBehaviorScore(
                    trait_id=trait_id,
                    score=score,
                    band_id=band.id,
                    label=band.label,
                    display_name=display_name,
                    detailed_description=getattr(band_descriptions, band.id),
                )
            )
        return PersonalityBehaviorProfile(
            map_version=config.version,
            map_hash=config_hash,
            scores=score_payload,
            matched_combination_ids=matched_combination_ids,
            matched_combination_rules=matched_combination_rules,
            general_rules=_PERSONALITY_RUNTIME_RULES,
        )

    @staticmethod
    def _combination_matches(
        all_of: list[PersonalityCombinationCondition],
        any_of: list[PersonalityCombinationCondition],
        bands: dict[str, PersonalityScoreBand],
    ) -> bool:
        def matches(condition: PersonalityCombinationCondition) -> bool:
            band = bands.get(condition.trait_id)
            return bool(band and band.id in condition.band_ids)

        return all(matches(condition) for condition in all_of) and (
            not any_of or any(matches(condition) for condition in any_of)
        )

    @staticmethod
    def _band_for_score(
        config: PersonalityBehaviorMap,
        score: int,
    ) -> PersonalityScoreBand:
        for band in config.score_bands:
            if band.min_score <= score <= band.max_score:
                return band
        raise ValueError(f"No personality score band configured for score={score}")

@lru_cache(maxsize=1)
def get_personality_behavior_resolver() -> PersonalityBehaviorResolver:
    return PersonalityBehaviorResolver()


def personality_behavior_profile_json(
    profile: PersonalityBehaviorProfile | None,
) -> str:
    if profile is None:
        return "{}"
    return json.dumps(
        profile.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def personality_behavior_prompt_json(
    profile: PersonalityBehaviorProfile | None,
) -> str:
    """Serialize the same seven-band-only profile used by every model flow."""

    return personality_behavior_profile_json(profile)

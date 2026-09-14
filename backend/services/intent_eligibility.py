from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.schemas.intent import IntentConfig
from backend.schemas.profile import EmployeeProfile
from backend.utils.rating_values import (
    extract_profile_rating,
    extract_profile_tcl,
    normalize_performance_rating,
    normalize_tcl,
)


@dataclass(frozen=True)
class IntentEligibilityResult:
    status: Literal["unconfigured", "allowed", "mismatch", "unknown"]
    allowed: bool
    performance_rating: int | None
    tcl: str | None
    message: str | None = None


def evaluate_intent_eligibility(
    profile: EmployeeProfile | None,
    intent: IntentConfig,
) -> IntentEligibilityResult:
    rating = _profile_rating(profile)
    tcl = _profile_tcl(profile)
    rule = intent.eligibility

    if rule is None or (not rule.performance_ratings and not rule.tcl_values):
        return IntentEligibilityResult("unconfigured", True, rating, tcl)

    if rating is None and tcl is None:
        return IntentEligibilityResult(
            "unknown",
            True,
            None,
            None,
            "员工档案缺少可识别的 performance rating 或 TCL，暂时无法判断该选择是否符合建议范围。"
            "建议补充或核对员工信息；如已掌握相关业务背景，仍可继续使用当前沟通意图。",
        )

    rating_matches = rating is not None and rating in rule.performance_ratings
    tcl_matches = tcl is not None and tcl in rule.tcl_values
    if rating_matches or tcl_matches:
        return IntentEligibilityResult("allowed", True, rating, tcl)

    current_values = []
    if rating is not None:
        current_values.append(f"performance rating 为 {rating}")
    if tcl is not None:
        current_values.append(f"TCL 为 {tcl}")
    expected_values = []
    if rule.performance_ratings:
        expected_values.append(
            f"performance rating {_format_ratings(rule.performance_ratings)}"
        )
    if rule.tcl_values:
        expected_values.append(f"TCL {'/'.join(rule.tcl_values)}")

    return IntentEligibilityResult(
        "mismatch",
        True,
        rating,
        tcl,
        f"当前员工的 {'，'.join(current_values)}；“{intent.name}”的建议适用范围为 "
        f"{' 或 '.join(expected_values)}。建议核对并选择更匹配的沟通意图；"
        "如有额外业务依据，仍可确认当前选择并继续。",
    )


def _profile_rating(profile: EmployeeProfile | None) -> int | None:
    if profile is None:
        return None
    rating = normalize_performance_rating(profile.performance_rating)
    return rating if rating is not None else extract_profile_rating(profile.source_profile_text)


def _profile_tcl(profile: EmployeeProfile | None) -> str | None:
    if profile is None:
        return None
    tcl = normalize_tcl(profile.tcl)
    return tcl if tcl is not None else extract_profile_tcl(profile.source_profile_text)


def _format_ratings(values: list[int]) -> str:
    ratings = sorted(set(values))
    if len(ratings) > 1 and ratings == list(range(ratings[0], ratings[-1] + 1)):
        return f"{ratings[0]}-{ratings[-1]}"
    return "/".join(str(value) for value in ratings)

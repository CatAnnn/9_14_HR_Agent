from __future__ import annotations

from scripts.import_employees import (
    build_profile,
    build_profile_text,
    normalize_goal_format,
)


FLATTENED_GOAL_TABLE = (
    '"Goal/目标\tAction Plan/目标行动计划\tBottom/保底值\t'
    'Meet Expectation/达标值\tChallenging/挑战值\n'
    '完成2026年VIS CN BP26\nTNS\nGM\t1，制定开票计划；\n2，与项目经理跟踪交付；\t'
    '45m CNY\n22%\t47m CNY\n24%\t50m CNY\n26%\n'
    'AI Authoring POC成功\t1, Local competence build up\n2, Deploy on customer\t'
    '1个客户AI POC\t2个客户AI POC\t3个客户AI POC"'
)


def test_goal_import_restores_flattened_table_without_rewriting_content():
    normalized = normalize_goal_format(FLATTENED_GOAL_TABLE)

    assert normalized.startswith("完成2026年VIS CN BP26\t\n- TNS\n- GM")
    assert "Goal/目标" not in normalized
    assert normalized.count("Action Plan/目标行动计划") == 2
    assert normalized.count("Bottom/保底值") == 2
    assert normalized.count("Meet Expectation/达标值") == 2
    assert normalized.count("Challenging/挑战值") == 2
    for original_content in (
        "1，制定开票计划；",
        "2，与项目经理跟踪交付；",
        "45m CNY",
        "22%",
        "47m CNY",
        "24%",
        "50m CNY",
        "26%",
        "AI Authoring POC成功",
        "1, Local competence build up",
        "2, Deploy on customer",
        "1个客户AI POC",
        "2个客户AI POC",
        "3个客户AI POC",
    ):
        assert original_content in normalized


def test_goal_format_normalization_is_idempotent():
    normalized = normalize_goal_format(FLATTENED_GOAL_TABLE)

    assert normalize_goal_format(normalized) == normalized


def test_regular_goal_normalization_only_changes_layout_whitespace():
    raw = "Dimension A\t  \r\nFirst result  \r\n\r\n\r\nSecond result"

    assert normalize_goal_format(raw) == "Dimension A\t\nFirst result\n\nSecond result"


def test_profile_and_source_text_use_the_same_normalized_goal_order():
    profile = build_profile({"Name": "Example Employee", "Goal": FLATTENED_GOAL_TABLE})
    profile_text = build_profile_text({"Name": "Example Employee", "Goal": FLATTENED_GOAL_TABLE})

    assert profile.key_goals[0] == "完成2026年VIS CN BP26"
    value_positions = {
        value: next(index for index, item in enumerate(profile.key_goals) if value in item)
        for value in ("50m CNY", "AI Authoring POC成功")
    }
    assert value_positions["AI Authoring POC成功"] > value_positions["50m CNY"]
    assert "Goal：完成2026年VIS CN BP26\t" in profile_text
    assert "Challenging/挑战值\n- 3个客户AI POC" in profile_text

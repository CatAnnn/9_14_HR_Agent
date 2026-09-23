from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


AGENTS = ("guidance", "employee", "coach")
LOCALES = ("zh-CN", "en", "de", "ja")
EXPECTED_COUNT = 30


class DatasetError(ValueError):
    """评测数据不满足首版契约。"""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path}:{line_number}: JSON 无效: {exc}") from exc
        if not isinstance(row, dict):
            raise DatasetError(f"{path}:{line_number}: 必须是 JSON 对象")
        rows.append(row)
    return rows


def validate_cases(rows: list[dict[str, Any]], *, complete: bool = True) -> None:
    if complete and len(rows) != EXPECTED_COUNT:
        raise DatasetError(f"首版必须恰好有 {EXPECTED_COUNT} 例，实际 {len(rows)} 例")
    ids: set[str] = set()
    counts: Counter[str] = Counter()
    for row in rows:
        case_id = row.get("case_id")
        agent = row.get("agent")
        locale = row.get("locale")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise DatasetError(f"case_id 为空或重复: {case_id!r}")
        ids.add(case_id)
        if row.get("provenance") != "synthetic":
            raise DatasetError(f"{case_id}: 首版必须明确标记为 synthetic")
        if agent not in AGENTS or locale not in LOCALES:
            raise DatasetError(f"{case_id}: agent 或 locale 无效")
        if not isinstance(row.get("state"), dict):
            raise DatasetError(f"{case_id}: state 必须是对象")
        state = row["state"]
        if state.get("locale") != locale or not state.get("setup_ready"):
            raise DatasetError(f"{case_id}: 会话语言或准备状态无效")
        if not isinstance(state.get("employee_profile"), dict) or not isinstance(state.get("intent"), dict):
            raise DatasetError(f"{case_id}: 缺少员工画像或沟通意图")
        turns = row.get("manager_turns")
        if not isinstance(turns, list) or (agent == "employee" and not turns):
            raise DatasetError(f"{case_id}: manager_turns 无效")
        if any(not isinstance(turn, str) or not turn.strip() for turn in turns):
            raise DatasetError(f"{case_id}: manager_turns 存在空消息")
        if agent == "coach" and not isinstance(state.get("conversation"), list):
            raise DatasetError(f"{case_id}: Coach 必须提供固定对话")
        counts[agent] += 1
    if complete and any(counts[agent] != 10 for agent in AGENTS):
        raise DatasetError(f"每类 Agent 必须有 10 例: {dict(counts)}")


def validate_drafts(rows: list[dict[str, Any]], cases: list[dict[str, Any]]) -> None:
    expected = {case["case_id"] for case in cases}
    actual: set[str] = set()
    for row in rows:
        case_id = row.get("case_id")
        if case_id not in expected or case_id in actual:
            raise DatasetError(f"标签 case_id 不存在或重复: {case_id!r}")
        actual.add(case_id)
        if row.get("provenance") != "synthetic" or row.get("review_status") != "draft_unreviewed":
            raise DatasetError(f"{case_id}: 标签不得冒充专家确认结果")
        for key in ("expected_facts", "forbidden_claims"):
            if not isinstance(row.get(key), list) or any(not isinstance(item, str) for item in row[key]):
                raise DatasetError(f"{case_id}: {key} 必须是字符串数组")
        ranges = row.get("suggested_score_ranges")
        if not isinstance(ranges, dict) or any(
            not isinstance(value, list) or len(value) != 2
            or not all(isinstance(number, int) and 1 <= number <= 5 for number in value)
            or value[0] > value[1]
            for value in ranges.values()
        ):
            raise DatasetError(f"{case_id}: 建议分数范围无效")
    if actual != expected:
        raise DatasetError(f"标签缺少案例: {sorted(expected - actual)}")


def load_dataset(case_path: Path, label_path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    cases = read_jsonl(case_path)
    drafts = read_jsonl(label_path)
    validate_cases(cases)
    validate_drafts(drafts, cases)
    return cases, {row["case_id"]: row for row in drafts}

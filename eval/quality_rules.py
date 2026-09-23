from __future__ import annotations

import re
from typing import Any


COACH_TASKS = {
    "opening_evaluation",
    "emotion_evaluation",
    "output_expectations_evaluation",
    "development_plan_evaluation",
}
HIDDEN_RATING = re.compile(
    r"(?:performance\s*rating|rating\s*[:：=]|绩效评级|当前评级|评价等級|Leistungsbewertung|\bTCL\b)",
    re.IGNORECASE,
)
HAN = re.compile(r"[\u4e00-\u9fff]")
KANA = re.compile(r"[\u3040-\u30ff]")
LATIN = re.compile(r"[A-Za-zÄÖÜäöüß]")


def _schema_error(agent: str, output: dict[str, Any]) -> str | None:
    try:
        if agent == "guidance":
            from backend.schemas.guidance import GuidanceReport
            GuidanceReport.model_validate(output)
        elif agent == "coach":
            from backend.schemas.coach import CoachReport
            CoachReport.model_validate(output)
        elif agent == "employee":
            if not isinstance(output.get("replies"), list) or not output["replies"]:
                raise ValueError("replies 必须是非空数组")
            if any(not isinstance(reply, str) or not reply.strip() for reply in output["replies"]):
                raise ValueError("回复必须是非空字符串")
        else:
            raise ValueError("未知 Agent")
    except (TypeError, ValueError) as exc:
        return f"schema_invalid: {str(exc)[:350]}"
    return None


def _generated_text(agent: str, output: dict[str, Any]) -> str:
    if agent == "employee":
        return "\n".join(output.get("replies") or [])
    if agent == "guidance":
        parts = [str(output.get("purpose") or ""), str(output.get("opening_suggestion") or "")]
        for key in ("risk_preview", "response_strategies", "safer_phrases"):
            parts.extend(str(item) for item in output.get(key) or [])
        return "\n".join(parts)
    parts = []
    for task in output.get("task_results") or []:
        if isinstance(task, dict):
            parts.extend([str(task.get("summary") or ""), *[str(item) for item in task.get("improvement_points") or []]])
    return "\n".join(parts)


def _language_issue(locale: str, text: str) -> str | None:
    if len(text.strip()) < 8:
        return None
    if locale == "ja" and not KANA.search(text):
        return "locale_mismatch: 日语输出缺少假名"
    if locale == "zh-CN" and len(HAN.findall(text)) < 4:
        return "locale_mismatch: 中文输出缺少中文内容"
    if locale in {"en", "de"} and (len(LATIN.findall(text)) < 8 or len(HAN.findall(text)) > 5):
        return "locale_mismatch: 拉丁文字输出疑似错误语言"
    return None


def _citation_ids(node: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"knowledge_chunk_ids", "summary_knowledge_chunk_ids"} and isinstance(value, list):
                ids.update(str(item) for item in value if isinstance(item, str) and item)
            elif key == "detail_knowledge_chunk_ids" and isinstance(value, list):
                for group in value:
                    if isinstance(group, list):
                        ids.update(str(item) for item in group if isinstance(item, str) and item)
            elif key == "citations" and isinstance(value, list):
                for citation in value:
                    if isinstance(citation, dict) and citation.get("source_type") != "skill" and citation.get("chunk_id"):
                        ids.add(str(citation["chunk_id"]))
            else:
                ids.update(_citation_ids(value))
    elif isinstance(node, list):
        for item in node:
            ids.update(_citation_ids(item))
    return ids


def evaluate_rules(
    case: dict[str, Any],
    output: dict[str, Any],
    *,
    known_chunk_ids: set[str],
) -> list[str]:
    """仅验证能客观判定的契约；开放性质量交给独立 Judge。"""
    issues: list[str] = []
    agent = case["agent"]
    schema_issue = _schema_error(agent, output)
    if schema_issue:
        issues.append(schema_issue)
        return issues
    if output.get("locale", case["locale"]) != case["locale"]:
        issues.append("locale_mismatch: 报告语言字段与案例不符")
    generated = _generated_text(agent, output)
    language_issue = _language_issue(case["locale"], generated)
    if language_issue:
        issues.append(language_issue)
    if agent == "employee" and HIDDEN_RATING.search(generated):
        issues.append("hidden_rating_leak: 员工回复包含内部评级或 TCL 字段")
    if agent == "coach":
        task_results = output["task_results"]
        actual = [task["task_id"] for task in task_results]
        if set(actual) != COACH_TASKS or len(actual) != 4:
            issues.append("coach_dimensions_invalid: 缺少或重复四维结果")
        manager_turns = {
            turn["turn_index"]: turn["text"]
            for turn in case["state"].get("conversation", [])
            if turn.get("speaker") == "manager"
        }
        for task in task_results:
            for evidence in task.get("evidence") or []:
                original = manager_turns.get(evidence.get("turn_index"), "")
                quote = str(evidence.get("quote") or "")
                if evidence.get("speaker") != "manager" or not quote or quote not in original:
                    issues.append(f"coach_quote_invalid: {task['task_id']} 的证据不在对应 Manager 原话中")
            for phrase in task.get("better_phrases") or []:
                quote = phrase.get("original")
                if quote and not any(quote in text for text in manager_turns.values()):
                    issues.append(f"coach_quote_invalid: {task['task_id']} 的改进原话无法核对")
    invalid_ids = sorted(_citation_ids(output) - known_chunk_ids)
    if invalid_ids:
        issues.append(f"citation_invalid: 知识库不存在的 chunk_id: {invalid_ids[:8]}")
    return issues

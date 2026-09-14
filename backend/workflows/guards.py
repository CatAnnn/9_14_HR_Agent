from __future__ import annotations

from backend.exceptions.workflow_errors import SetupNotReadyError, WorkflowError
from backend.schemas.locale import localized_value
from backend.schemas.state import SessionState


_PERFORMANCE_LOCALE_MISMATCH_MESSAGES = {
    "zh-CN": "语言已切换，请先确认当前语言的员工表现，再继续生成。",
    "en": (
        "The session language has changed. Confirm the employee performance "
        "content in the current language before continuing."
    ),
    "de": (
        "Die Sitzungssprache wurde geändert. Bestätigen Sie zuerst die "
        "Leistungsbeschreibung des Mitarbeiters in der aktuellen Sprache."
    ),
    "ja": (
        "セッションの言語が変更されています。続行する前に、現在の言語で"
        "従業員のパフォーマンス内容を確認してください。"
    ),
}


def ensure_current_performance_locale(state: SessionState) -> None:
    """Reject downstream generation while the confirmed draft is in another locale.

    The prior draft remains persisted so a manager's confirmed business content is
    not destroyed by a locale switch. It becomes usable again after confirmation in
    the current locale (or after switching back to its original locale).
    """

    if state.intent is None or state.intent.performance_locale == state.locale:
        return
    raise SetupNotReadyError(
        localized_value(state.locale, _PERFORMANCE_LOCALE_MISMATCH_MESSAGES)
    )


def ensure_setup_ready(state: SessionState) -> None:
    if not state.setup_ready:
        raise SetupNotReadyError("setup_ready=false，不能进入预演。")
    ensure_current_performance_locale(state)


def ensure_rehearsal_allowed(state: SessionState) -> None:
    ensure_setup_ready(state)
    if state.run_mode == "guidance_only":
        raise WorkflowError("run_mode=guidance_only，不允许进入多轮预演。")
    if state.run_mode == "guidance_then_rehearsal" and not state.guidance_report_id:
        raise WorkflowError("run_mode=guidance_then_rehearsal，必须先生成谈前指导。")


def ensure_can_add_user_turn(state: SessionState) -> None:
    """Turn count is tracked for reporting only; rehearsal conversations are unlimited."""
    return None

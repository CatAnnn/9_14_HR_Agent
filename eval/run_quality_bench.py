from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.quality_contract import load_dataset
from eval.quality_judge import IndependentJudge
from eval.quality_rules import _citation_ids, evaluate_rules


DEFAULT_CASES = PROJECT_ROOT / "eval/datasets/synthetic_v1.jsonl"
DEFAULT_DRAFTS = PROJECT_ROOT / "eval/labels/synthetic_v1_draft.jsonl"


def isolated_target_errors(database_url: str, redis_url: str, runtime_data_dir: Path | None) -> list[str]:
    """拒绝将真实评测写入日常服务的数据存储。"""
    errors: list[str] = []
    database = urlparse(database_url).path.strip("/").lower()
    if not database.endswith("_eval"):
        errors.append("DATABASE_URL 的数据库名必须以 _eval 结尾")
    redis_db = urlparse(redis_url).path.strip("/")
    if not redis_db.isdigit() or int(redis_db) < 1:
        errors.append("REDIS_URL 必须指向独立的非零 DB")
    if runtime_data_dir is None or "eval" not in str(runtime_data_dir).lower():
        errors.append("RUNTIME_DATA_DIR 必须是独立的 eval 数据目录")
    return errors


def _files_hash(directory: Path, suffix: str) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob(f"*{suffix}")):
        digest.update(str(path.relative_to(directory)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _git_head() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _known_chunks(repository: Any, output: dict[str, Any]) -> set[str]:
    ids = sorted(_citation_ids(output))
    if not ids:
        return set()
    with repository.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM kb_chunks WHERE chunk_id = ANY(%s::text[])",
            (ids,),
        ).fetchall()
    return {str(row[0] if not isinstance(row, dict) else row["chunk_id"]) for row in rows}


def _kb_count(repository: Any) -> int:
    with repository.connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()
    return int(row[0] if not isinstance(row, dict) else row["count"])


def _cleanup_session(repository: Any, session_id: str) -> None:
    # 仅删除本次 runner 创建的会话，不清空共享缓存或知识库。
    with repository.connection() as conn:
        conn.execute("DELETE FROM guidance_reports WHERE session_id = %s", (session_id,))
        conn.execute("DELETE FROM coach_reports WHERE session_id = %s", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))


def _build_state(case: dict[str, Any], session_id: str) -> Any:
    from backend.business_config.loader import get_config_loader
    from backend.schemas.state import SessionState
    from backend.services.emotion_transition_service import EmotionTransitionService

    payload = dict(case["state"])
    payload["session_id"] = session_id
    payload["locale"] = case["locale"]
    intent = dict(payload["intent"])
    intent["config"] = get_config_loader().intents()[intent["intent_id"]].model_dump(mode="json")
    payload["intent"] = intent
    payload["run_mode"] = "guidance_only" if case["agent"] == "guidance" else "rehearsal_report"
    payload["stage"] = "setup_ready"
    payload["guidance_report_id"] = None
    payload["coach_report_id"] = None
    state = SessionState.model_validate(payload)
    if state.emotion_state is None:
        state.emotion_state = EmotionTransitionService().initial_state(
            intent_id=state.intent.intent_id,
            personality=state.personality,
        )
    return state


async def _invoke_case(runtime: Any, case: dict[str, Any], session_id: str) -> dict[str, Any]:
    if case["agent"] == "guidance":
        report = await runtime.guidance_service.generate(session_id)
        return report.model_dump(mode="json")
    if case["agent"] == "coach":
        report = await runtime.coach_service.generate(session_id)
        return report.model_dump(mode="json")
    replies: list[str] = []
    for turn in case["manager_turns"]:
        state = await runtime.rehearsal_service.send_manager_message(session_id, turn)
        if not state.conversation or state.conversation[-1].speaker != "employee":
            raise RuntimeError("预演服务未保存员工回复")
        replies.append(state.conversation[-1].text)
    return {"locale": case["locale"], "replies": replies}


async def _one_trial(
    runtime: Any,
    case: dict[str, Any],
    trial: int,
    judge: IndependentJudge | None,
) -> dict[str, Any]:
    session_id = f"eval-{uuid4().hex}"
    record: dict[str, Any] = {
        "case_id": case["case_id"], "agent": case["agent"], "trial": trial,
        "session_id": session_id, "output": None, "rule_issues": [],
        "judge": None, "error": None, "execution_pass": False,
        "rules_pass": False, "overall_pass": None,
    }
    started = time.perf_counter()
    saved = False
    try:
        state = _build_state(case, session_id)
        saved = True
        await asyncio.to_thread(runtime.session_service.save_session, state)
        output = await _invoke_case(runtime, case, session_id)
        record["output"] = output
        record["execution_pass"] = True
        known_ids = await asyncio.to_thread(_known_chunks, runtime.repository, output)
        issues = evaluate_rules(case, output, known_chunk_ids=known_ids)
        record["rule_issues"] = issues
        record["rules_pass"] = not issues
        if judge is not None:
            try:
                record["judge"] = await judge.assess(case, output)
                record["overall_pass"] = not issues and record["judge"]["verdict"] == "pass"
            except Exception as exc:  # noqa: BLE001
                record["error"] = f"judge_error: {type(exc).__name__}: {str(exc)[:300]}"
                record["overall_pass"] = False
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"run_error: {type(exc).__name__}: {str(exc)[:300]}"
        record["overall_pass"] = False if judge is not None else None
    finally:
        if saved:
            try:
                await asyncio.to_thread(_cleanup_session, runtime.repository, session_id)
            except Exception as exc:  # noqa: BLE001
                record["cleanup_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                record["overall_pass"] = False if judge is not None else None
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return record


def summarize(records: list[dict[str, Any]], *, trials: int, judge_enabled: bool, _include_agent: bool = True) -> dict[str, Any]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_case.setdefault(record["case_id"], []).append(record)
    summary: dict[str, Any] = {
        "case_count": len(by_case),
        "trial_count": len(records),
        "execution_success_rate": round(sum(bool(row["execution_pass"]) for row in records) / len(records), 4) if records else 0,
        "rules_pass_rate": round(sum(bool(row["rules_pass"]) for row in records) / len(records), 4) if records else 0,
        "quality_scored": judge_enabled,
    }
    if judge_enabled:
        summary["pass_at_1"] = round(sum(bool(rows[0]["overall_pass"]) for rows in by_case.values()) / len(by_case), 4) if by_case else 0
        summary[f"pass_at_{trials}"] = round(sum(any(row["overall_pass"] is True for row in rows) for rows in by_case.values()) / len(by_case), 4) if by_case else 0
        summary[f"pass_all_{trials}"] = round(sum(len(rows) == trials and all(row["overall_pass"] is True for row in rows) for rows in by_case.values()) / len(by_case), 4) if by_case else 0
    if _include_agent:
        agents = sorted({row["agent"] for row in records if row.get("agent")})
        if agents:
            summary["by_agent"] = {
                agent: summarize(
                    [row for row in records if row.get("agent") == agent],
                    trials=trials,
                    judge_enabled=judge_enabled,
                    _include_agent=False,
                )
                for agent in agents
            }
    return summary


async def run_live(cases: list[dict[str, Any]], drafts: dict[str, dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    from backend.config.settings import get_settings
    from backend.repositories.postgres_repository import PostgresRepository
    from backend.services.application_runtime import ApplicationRuntime

    settings = get_settings()
    errors = isolated_target_errors(settings.database_url, settings.redis_url, settings.runtime_data_dir)
    if errors:
        raise RuntimeError("隔离环境检查失败: " + "; ".join(errors))
    tested_models = {
        settings.guidance_model,
        settings.employee_model,
        settings.coach_evaluator_model,
        settings.model_retry_race_model,
        settings.default_chat_model,
    }
    judge = None if args.skip_judge else IndependentJudge.from_environment(tested_models)
    runtime = ApplicationRuntime(settings)
    runtime.start()
    try:
        kb_count = await asyncio.to_thread(_kb_count, runtime.repository)
        if kb_count == 0:
            raise RuntimeError("隔离数据库尚未导入知识库，拒绝运行内容评测")
        records = []
        for case in cases:
            for trial in range(1, args.trials + 1):
                records.append(await _one_trial(runtime, case, trial, judge))
        profile = runtime.embedding_profile
        report = {
            "benchmark_id": "synthetic-agent-quality-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_provenance": "synthetic",
            "label_status": "draft_unreviewed",
            "release_eligible": False,
            "quality_conclusion": "仅用于框架试运行和明显问题排查；未经专家审核，不构成真实业务质量基线。",
            "versions": {
                "git_head": _git_head(),
                "model_names": sorted(tested_models),
                "judge_model": judge.model if judge else None,
                "prompt_sha256": _files_hash(PROJECT_ROOT / "backend/prompts", ".jinja2"),
                "business_config_sha256": _files_hash(PROJECT_ROOT / "backend/business_config", ".yaml"),
                "kb_build_id": profile.active_build_id,
                "kb_vector_count": profile.vector_count,
                "dataset_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
            },
            "isolated_kb_chunk_count": kb_count,
            "summary": summarize(records, trials=args.trials, judge_enabled=judge is not None),
            "cases": [{"case_id": case["case_id"], "draft_label_status": drafts[case["case_id"]]["review_status"]} for case in cases],
            "trials": records,
        }
        return report
    finally:
        await runtime.shutdown()
        PostgresRepository.close_connection_pools()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合成 Agent 内容质量 benchmark")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--draft-labels", type=Path, default=DEFAULT_DRAFTS)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--confirm-live-model-cost", action="store_true")
    parser.add_argument("--skip-judge", action="store_true", help="仅做工程试运行，不输出质量通过率")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases, drafts = load_dataset(args.cases, args.draft_labels)
    if args.trials < 1:
        raise SystemExit("--trials 必须至少为 1")
    selected = set(args.case_ids or [])
    known = {case["case_id"] for case in cases}
    if selected - known:
        raise SystemExit(f"未知 case_id: {sorted(selected - known)}")
    if selected:
        cases = [case for case in cases if case["case_id"] in selected]
    if args.validate_only:
        result = {"valid": True, "data_provenance": "synthetic", "label_status": "draft_unreviewed", "case_count": len(cases), "by_agent": dict(Counter(case["agent"] for case in cases)), "live_executed": False}
    else:
        if not args.confirm_live_model_cost:
            raise SystemExit("真实模型调用需要 --confirm-live-model-cost")
        result = asyncio.run(run_live(cases, drafts, args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

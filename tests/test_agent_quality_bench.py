from __future__ import annotations

import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from eval.generate_synthetic_v1 import build_rows
from eval.quality_contract import DatasetError, load_dataset, validate_cases, validate_drafts
from eval.quality_rules import COACH_TASKS, evaluate_rules
from eval.run_quality_bench import DEFAULT_CASES, DEFAULT_DRAFTS, _one_trial, isolated_target_errors, summarize


class AgentQualityBenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases, cls.drafts = load_dataset(DEFAULT_CASES, DEFAULT_DRAFTS)

    def test_dataset_is_reproducible_and_explicitly_unreviewed(self) -> None:
        generated_cases, generated_drafts = build_rows()
        self.assertEqual(self.cases, generated_cases)
        self.assertEqual(list(self.drafts.values()), generated_drafts)
        self.assertEqual(len(self.cases), 30)
        self.assertEqual({case["agent"] for case in self.cases}, {"guidance", "employee", "coach"})
        self.assertTrue(all(label["review_status"] == "draft_unreviewed" for label in self.drafts.values()))
        sparse = next(case for case in self.cases if case["case_id"] == "coach_006")
        self.assertEqual([turn["speaker"] for turn in sparse["state"]["conversation"]], ["manager"])

    def test_dataset_rejects_false_provenance_and_missing_labels(self) -> None:
        rows = [dict(row) for row in self.cases]
        rows[0]["provenance"] = "real"
        with self.assertRaises(DatasetError):
            validate_cases(rows)
        with self.assertRaises(DatasetError):
            validate_drafts(list(self.drafts.values())[:-1], self.cases)

    def test_employee_rating_leak_and_locale_mismatch_fail(self) -> None:
        case = next(row for row in self.cases if row["case_id"] == "employee_001")
        output = {"locale": "zh-CN", "replies": ["我的绩效评级是 2，我希望继续讨论项目交付。"]}
        issues = evaluate_rules(case, output, known_chunk_ids=set())
        self.assertTrue(any("hidden_rating_leak" in issue for issue in issues))
        output = {"locale": "en", "replies": ["We can discuss my next goal and delivery."]}
        issues = evaluate_rules(case, output, known_chunk_ids=set())
        self.assertTrue(any("locale_mismatch" in issue for issue in issues))
        self.assertTrue(any("employee_turn_count_invalid" in issue for issue in issues))

    def test_coach_forged_quote_and_unknown_chunk_fail(self) -> None:
        case = next(row for row in self.cases if row["case_id"] == "coach_001")
        tasks = [
            {"task_id": task_id, "task_name": task_id, "status": "success", "score": 3, "summary": "经理提出了后续沟通行动。"}
            for task_id in sorted(COACH_TASKS)
        ]
        tasks[0]["evidence"] = [{"turn_index": 1, "speaker": "manager", "quote": "这句话并不存在"}]
        tasks[0]["citations"] = [{"chunk_id": "invented-chunk", "source_id": "x", "title": "x", "explanation": "x"}]
        output = {"session_id": "eval-test", "locale": "zh-CN", "status": "success", "task_results": tasks}
        issues = evaluate_rules(case, output, known_chunk_ids=set())
        self.assertTrue(any("coach_quote_invalid" in issue for issue in issues))
        self.assertTrue(any("citation_invalid" in issue for issue in issues))
        tasks[1]["status"] = "failed"
        issues = evaluate_rules(case, output, known_chunk_ids=set())
        self.assertTrue(any("coach_task_failed" in issue for issue in issues))

    def test_schema_error_and_isolation_guard(self) -> None:
        case = next(row for row in self.cases if row["case_id"] == "employee_001")
        self.assertTrue(any("schema_invalid" in issue for issue in evaluate_rules(case, {"replies": []}, known_chunk_ids=set())))
        self.assertTrue(isolated_target_errors("postgresql://x@localhost/hr_agent", "redis://localhost/0", None))
        self.assertEqual(isolated_target_errors("postgresql://x@localhost/hr_agent_eval", "redis://localhost/1", DEFAULT_CASES.parent), [])

    def test_failed_trials_never_count_as_success(self) -> None:
        rows = [
            {"case_id": "a", "execution_pass": True, "rules_pass": True, "overall_pass": True},
            {"case_id": "a", "execution_pass": False, "rules_pass": False, "overall_pass": False},
            {"case_id": "a", "execution_pass": True, "rules_pass": True, "overall_pass": True},
        ]
        metrics = summarize(rows, trials=3, judge_enabled=True)
        self.assertEqual(metrics["pass_at_1"], 1.0)
        self.assertEqual(metrics["pass_at_3"], 1.0)
        self.assertEqual(metrics["pass_all_3"], 0.0)

    def test_runner_replays_service_and_preserves_failure_reason(self) -> None:
        case = next(row for row in self.cases if row["case_id"] == "employee_001")
        saved: list[str] = []
        cleaned: list[str] = []

        class SessionService:
            def save_session(self, state):
                saved.append(state.session_id)

        class RehearsalService:
            async def send_manager_message(self, session_id, message):
                return SimpleNamespace(conversation=[SimpleNamespace(speaker="employee", text="我愿意讨论项目交付和下一步目标。")])

        class Judge:
            async def assess(self, _case, _output):
                return {"verdict": "pass", "reasons": ["事实一致"]}

        runtime = SimpleNamespace(session_service=SessionService(), rehearsal_service=RehearsalService(), repository=object())
        with patch("eval.run_quality_bench._build_state", side_effect=lambda _, sid: SimpleNamespace(session_id=sid)), \
             patch("eval.run_quality_bench._known_chunks", return_value=set()), \
             patch("eval.run_quality_bench._cleanup_session", side_effect=lambda _, sid: cleaned.append(sid)):
            result = asyncio.run(_one_trial(runtime, case, 1, Judge()))
        self.assertEqual(result["output"]["replies"], ["我愿意讨论项目交付和下一步目标。"] * 2)
        self.assertTrue(result["overall_pass"])
        self.assertEqual(saved, cleaned)

        class BrokenRehearsal:
            async def send_manager_message(self, session_id, message):
                raise RuntimeError("模型不可用")

        runtime.rehearsal_service = BrokenRehearsal()
        with patch("eval.run_quality_bench._build_state", side_effect=lambda _, sid: SimpleNamespace(session_id=sid)), \
             patch("eval.run_quality_bench._cleanup_session", side_effect=lambda _, sid: cleaned.append(sid)):
            failed = asyncio.run(_one_trial(runtime, case, 2, Judge()))
        self.assertFalse(failed["execution_pass"])
        self.assertFalse(failed["overall_pass"])
        self.assertIn("模型不可用", failed["error"])
        self.assertEqual(saved, cleaned)


if __name__ == "__main__":
    unittest.main()

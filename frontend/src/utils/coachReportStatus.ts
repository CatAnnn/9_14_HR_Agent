import type { CoachReport } from '../types/domain';

const EXPECTED_COACH_TASK_IDS = new Set([
  'opening_evaluation',
  'emotion_evaluation',
  'output_expectations_evaluation',
  'development_plan_evaluation',
]);

export function isCompleteCoachReport(report: CoachReport | null | undefined): boolean {
  if (!report || report.status !== 'success') return false;
  const tasks = report.task_results || [];
  const taskIds = new Set(tasks.map((task) => task.task_id));
  return tasks.length === EXPECTED_COACH_TASK_IDS.size
    && taskIds.size === EXPECTED_COACH_TASK_IDS.size
    && [...EXPECTED_COACH_TASK_IDS].every((taskId) => taskIds.has(taskId))
    && tasks.every((task) => task.status !== 'failed');
}

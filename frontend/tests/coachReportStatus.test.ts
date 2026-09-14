import assert from 'node:assert/strict';
import test from 'node:test';

import type { CoachReport, CoachTaskResult } from '../src/types/domain.ts';
import { isCompleteCoachReport } from '../src/utils/coachReportStatus.ts';

function task(status: CoachTaskResult['status']): CoachTaskResult {
  return {
    task_id: 'opening_evaluation',
    task_name: '开场评估',
    status,
    score: status === 'success' ? 4 : null,
    summary: '结果',
  };
}

function report(status: string, tasks: CoachTaskResult[]): CoachReport {
  return { session_id: 'session-1', locale: 'zh-CN', status, task_results: tasks };
}

test('accepts a complete report without failed dimensions', () => {
  const tasks = [
    'opening_evaluation',
    'emotion_evaluation',
    'output_expectations_evaluation',
    'development_plan_evaluation',
  ].map((taskId) => ({ ...task('success'), task_id: taskId }));
  assert.equal(isCompleteCoachReport(report('success', tasks)), true);
});

test('rejects both explicit partial reports and hidden failed dimensions', () => {
  assert.equal(isCompleteCoachReport(report('partial', [task('success')])), false);
  assert.equal(isCompleteCoachReport(report('success', [task('failed')])), false);
});

test('rejects success-shaped reports with missing or duplicate dimensions', () => {
  const opening = task('success');
  assert.equal(isCompleteCoachReport(report('success', [opening])), false);
  assert.equal(isCompleteCoachReport(report('success', [opening, opening, opening, opening])), false);
});

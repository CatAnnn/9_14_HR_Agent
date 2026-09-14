import assert from 'node:assert/strict';
import test from 'node:test';

import type { SessionState } from '../src/types/domain.ts';
import { isReportAccessible, isStepUnlocked } from '../src/utils/workflowSteps.ts';

function rehearsalSession(overrides: Partial<SessionState> = {}): SessionState {
  return {
    session_id: 'session-1',
    stage: 'rehearsal',
    conversation: [
      { turn_index: 1, speaker: 'manager', text: '我们开始绩效反馈。' },
      { turn_index: 2, speaker: 'employee', text: '好的。' },
    ],
    user_turn_count: 1,
    warnings: [],
    ...overrides,
  };
}

test('manager turns alone do not unlock the report step', () => {
  const session = rehearsalSession();
  assert.equal(isReportAccessible(session, 'idle'), false);
  assert.equal(isStepUnlocked('report', session, false), false);
});

test('ending rehearsal unlocks report navigation before generation starts', () => {
  const session = rehearsalSession({ rehearsal_ended_at: '2026-08-17T01:00:00Z' });
  assert.equal(isReportAccessible(session, 'idle'), true);
  assert.equal(isStepUnlocked('report', session, true), true);
});

test('report generation and persisted reports remain accessible', () => {
  const session = rehearsalSession();
  assert.equal(isReportAccessible(session, 'streaming'), true);
  assert.equal(isReportAccessible(session, 'partial_error'), true);
  assert.equal(isReportAccessible(
    rehearsalSession({ coach_report_id: 'report-1' }),
    'idle',
  ), true);
  assert.equal(isReportAccessible(
    rehearsalSession({ stage: 'report_ready' }),
    'idle',
  ), true);
});

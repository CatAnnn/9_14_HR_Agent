import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const workflowSource = readFileSync(
  new URL('../src/context/WorkflowContext.tsx', import.meta.url),
  'utf8',
);

test('an existing coach report is loaded through the version-aware generation endpoint', () => {
  const existingReportBranch = workflowSource.match(
    /if \(activeSession\.coach_report_id && !force\) \{[\s\S]*?\n    \}/,
  )?.[0] || '';

  assert.match(
    existingReportBranch,
    /api\.generateCoachReport\(activeSession\.session_id\)/,
  );
  assert.doesNotMatch(
    existingReportBranch,
    /api\.getCoachReport\(activeSession\.session_id\)/,
  );
});

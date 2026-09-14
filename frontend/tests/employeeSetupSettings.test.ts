import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeLatestEmployeeSetupSettings,
  shouldRestorePreviousEmployeeSettings,
} from '../src/utils/employeeSetupSettings.ts';

test('only administrators can automatically restore an employee previous settings', () => {
  assert.equal(shouldRestorePreviousEmployeeSettings('admin'), true);
  assert.equal(shouldRestorePreviousEmployeeSettings('user'), false);
  assert.equal(shouldRestorePreviousEmployeeSettings(null), false);
  assert.equal(shouldRestorePreviousEmployeeSettings(undefined), false);
});

test('normalizes only supplemental text, personality, and motives', () => {
  const response = {
    employee_id: 'E001',
    found: true,
    supplemental_info: ' 上次填写的员工背景 ',
    personality: {
      openness: 61,
      conscientiousness: 72,
      extraversion: 43,
      agreeableness: 58,
      neuroticism: 37,
    },
    primary_motive_id: 'growth',
    secondary_motive_ids: ['recognition', 'growth', 'recognition', 'autonomy'],
  };

  const restored = normalizeLatestEmployeeSetupSettings(response);

  assert.equal(restored.found, true);
  assert.equal(restored.supplementalInfo, '上次填写的员工背景');
  assert.deepEqual(restored.secondaryMotiveIds, ['recognition', 'autonomy']);
  assert.notEqual(restored.personality, response.personality);
});

test('returns empty reusable values when no settings exist', () => {
  const restored = normalizeLatestEmployeeSetupSettings({
    employee_id: 'E001',
    found: false,
    supplemental_info: null,
    personality: null,
    primary_motive_id: null,
    secondary_motive_ids: [],
  });

  assert.equal(restored.supplementalInfo, '');
  assert.equal(restored.personality, null);
});

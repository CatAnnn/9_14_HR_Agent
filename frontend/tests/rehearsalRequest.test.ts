import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createRehearsalRequestId,
  hasCompletedRehearsalRequest,
} from '../src/utils/rehearsalRequest.ts';

test('creates API-safe rehearsal request identifiers', () => {
  const first = createRehearsalRequestId();
  const second = createRehearsalRequestId();

  assert.match(first, /^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$/);
  assert.match(second, /^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$/);
  assert.notEqual(first, second);
});

test('recognizes only a completed Manager and Employee request pair', () => {
  const requestId = 'request_1234567890abcdef';
  const manager = {
    turn_index: 3,
    speaker: 'manager',
    text: '请说明你的顾虑。',
    metadata: { rehearsal_request_id: requestId },
  };

  assert.equal(hasCompletedRehearsalRequest([manager], requestId), false);
  assert.equal(
    hasCompletedRehearsalRequest([
      manager,
      {
        turn_index: 4,
        speaker: 'employee',
        text: '我想先确认标准。',
        metadata: { rehearsal_request_id: requestId },
      },
    ], requestId),
    true,
  );
  assert.equal(
    hasCompletedRehearsalRequest([
      manager,
      {
        turn_index: 6,
        speaker: 'employee',
        text: '另一轮回复',
        metadata: { rehearsal_request_id: requestId },
      },
    ], requestId),
    false,
  );
});

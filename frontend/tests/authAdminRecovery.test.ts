import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ADMIN_TEST_SESSION_STORAGE_KEY,
  shouldReturnToAdminTestConfiguration,
} from '../src/utils/adminTestWorkflow.ts';
import { shouldClearWorkspaceAfterAuthFailure } from '../src/utils/authFailure.ts';

test('admin bootstrap returns to configuration only after its stored session id is absent', () => {
  const present = {
    getItem(key: string) {
      assert.equal(key, ADMIN_TEST_SESSION_STORAGE_KEY);
      return 'admin-session-7';
    },
  };
  const absent = { getItem: () => null };
  const blocked = {
    getItem() {
      throw new Error('blocked');
    },
  };

  assert.equal(shouldReturnToAdminTestConfiguration(present), false);
  assert.equal(shouldReturnToAdminTestConfiguration(absent), true);
  assert.equal(shouldReturnToAdminTestConfiguration(blocked), false);
  assert.equal(shouldReturnToAdminTestConfiguration(null), false);
});

test('auth bootstrap cleanup is limited to failures carrying status 401', () => {
  assert.equal(
    shouldClearWorkspaceAfterAuthFailure({ status: 401 }),
    true,
  );
  assert.equal(
    shouldClearWorkspaceAfterAuthFailure({ status: 500 }),
    false,
  );
  assert.equal(shouldClearWorkspaceAfterAuthFailure(new Error('network failed')), false);
  assert.equal(shouldClearWorkspaceAfterAuthFailure(new DOMException('aborted', 'AbortError')), false);
  assert.equal(shouldClearWorkspaceAfterAuthFailure(null), false);

  const inaccessibleStatus = Object.create(null) as Record<string, unknown>;
  Object.defineProperty(inaccessibleStatus, 'status', {
    get() {
      throw new Error('unavailable');
    },
  });
  assert.equal(shouldClearWorkspaceAfterAuthFailure(inaccessibleStatus), false);
});

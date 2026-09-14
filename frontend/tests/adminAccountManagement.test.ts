import assert from 'node:assert/strict';
import test from 'node:test';

import type { AdminAccount } from '../src/types/auth.ts';
import {
  adminAccountMatchesFilter,
  canAdminAccountLogin,
  isDisabledAdminAccount,
  isInactiveRegisteredAccount,
  removeAdminAccount,
  upsertAdminAccount,
} from '../src/utils/adminAccounts.ts';

function account(overrides: Partial<AdminAccount> = {}): AdminAccount {
  return {
    email: 'user@example.com',
    display_name: 'User',
    role: 'user',
    whitelist_enabled: true,
    registered: true,
    is_active: true,
    ...overrides,
  };
}

test('login-enabled accounts satisfy registration, active, and allowlist state', () => {
  const enabled = account();
  const pending = account({ registered: false, is_active: false });
  const inactive = account({ is_active: false });
  const blocked = account({ whitelist_enabled: false });

  assert.equal(canAdminAccountLogin(enabled), true);
  assert.equal(canAdminAccountLogin(pending), false);
  assert.equal(canAdminAccountLogin(inactive), false);
  assert.equal(canAdminAccountLogin(blocked), false);
  assert.equal(isInactiveRegisteredAccount(inactive), true);
  assert.equal(isDisabledAdminAccount(inactive), true);
  assert.equal(adminAccountMatchesFilter(inactive, 'enabled'), false);
  assert.equal(adminAccountMatchesFilter(inactive, 'disabled'), true);
  assert.equal(adminAccountMatchesFilter(pending, 'pending'), true);
});

test('account responses update and remove the local catalog case-insensitively', () => {
  const first = account({ email: 'z@example.com' });
  const stale = account({ email: 'User@Example.com', whitelist_enabled: false });
  const updated = account({ email: 'user@example.com', display_name: 'Updated' });

  const upserted = upsertAdminAccount([first, stale], updated);
  assert.deepEqual(upserted.map((item) => item.email), ['user@example.com', 'z@example.com']);
  assert.equal(upserted[0]?.display_name, 'Updated');
  assert.deepEqual(removeAdminAccount(upserted, ' USER@example.com '), [first]);
});

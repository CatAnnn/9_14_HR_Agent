import type { AdminAccount } from '../types/auth';

export type AdminAccountFilter = 'all' | 'registered' | 'enabled' | 'pending' | 'disabled';

export function normalizedAdminEmail(value: string): string {
  return value.trim().toLowerCase();
}

export function isInactiveRegisteredAccount(account: AdminAccount): boolean {
  return account.registered && !account.is_active;
}

export function canAdminAccountLogin(account: AdminAccount): boolean {
  return account.registered && account.is_active && account.whitelist_enabled;
}

export function isDisabledAdminAccount(account: AdminAccount): boolean {
  return !account.whitelist_enabled || isInactiveRegisteredAccount(account);
}

export function adminAccountMatchesFilter(
  account: AdminAccount,
  filter: AdminAccountFilter,
): boolean {
  if (filter === 'registered') return account.registered;
  if (filter === 'enabled') return canAdminAccountLogin(account);
  if (filter === 'pending') return !account.registered;
  if (filter === 'disabled') return isDisabledAdminAccount(account);
  return true;
}

export function upsertAdminAccount(
  accounts: readonly AdminAccount[],
  updated: AdminAccount,
): AdminAccount[] {
  const updatedEmail = normalizedAdminEmail(updated.email);
  return [
    ...accounts.filter((account) => normalizedAdminEmail(account.email) !== updatedEmail),
    updated,
  ].sort((left, right) => normalizedAdminEmail(left.email).localeCompare(
    normalizedAdminEmail(right.email),
    'en',
  ));
}

export function removeAdminAccount(
  accounts: readonly AdminAccount[],
  email: string,
): AdminAccount[] {
  const removedEmail = normalizedAdminEmail(email);
  return accounts.filter(
    (account) => normalizedAdminEmail(account.email) !== removedEmail,
  );
}

export type AdminUsageSortOrder = 'newest' | 'oldest';
export type AdminUsageScope = 'all' | 'complete' | 'incomplete';

export interface AdminUsageFilters {
  query: string;
  userEmail: string;
  startDate: string;
  endDate: string;
  sortOrder: AdminUsageSortOrder;
  scope: AdminUsageScope;
}

export const DEFAULT_ADMIN_USAGE_FILTERS: AdminUsageFilters = {
  query: '',
  userEmail: '',
  startDate: '',
  endDate: '',
  sortOrder: 'newest',
  scope: 'all',
};

const USAGE_FILTER_KEYS = ['q', 'user', 'from', 'to', 'order', 'scope'] as const;

function validIsoDate(value: string | null): string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/u.test(value)) return '';
  const [year, month, day] = value.split('-').map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day
    ? value
    : '';
}

export function readAdminUsageFilters(searchParams: URLSearchParams): AdminUsageFilters {
  const sortOrder = searchParams.get('order') === 'oldest' ? 'oldest' : 'newest';
  const rawScope = searchParams.get('scope');
  const scope: AdminUsageScope = rawScope === 'complete' || rawScope === 'incomplete'
    ? rawScope
    : 'all';

  return {
    query: searchParams.get('q') || '',
    userEmail: (searchParams.get('user') || '').trim().toLowerCase(),
    startDate: validIsoDate(searchParams.get('from')),
    endDate: validIsoDate(searchParams.get('to')),
    sortOrder,
    scope,
  };
}

export function writeAdminUsageFilters(
  currentSearchParams: URLSearchParams,
  filters: AdminUsageFilters,
): URLSearchParams {
  const next = new URLSearchParams(currentSearchParams);
  for (const key of USAGE_FILTER_KEYS) next.delete(key);
  next.set('section', 'usage');

  if (filters.query) next.set('q', filters.query);
  if (filters.userEmail) next.set('user', filters.userEmail.trim().toLowerCase());
  if (filters.startDate) next.set('from', filters.startDate);
  if (filters.endDate) next.set('to', filters.endDate);
  if (filters.sortOrder !== 'newest') next.set('order', filters.sortOrder);
  if (filters.scope !== 'all') next.set('scope', filters.scope);
  return next;
}

export function hasAdminUsageFilters(filters: AdminUsageFilters): boolean {
  return Boolean(
    filters.query
    || filters.userEmail
    || filters.startDate
    || filters.endDate
    || filters.sortOrder !== 'newest'
    || filters.scope !== 'all',
  );
}

export function adminUsageFilterSearch(searchParams: URLSearchParams): string {
  const filters = readAdminUsageFilters(searchParams);
  const normalized = writeAdminUsageFilters(new URLSearchParams(), filters);
  normalized.delete('section');
  const query = normalized.toString();
  return query ? `?${query}` : '';
}

export function adminUsageReturnPath(searchParams: URLSearchParams): string {
  const normalized = writeAdminUsageFilters(new URLSearchParams(), readAdminUsageFilters(searchParams));
  return `/admin?${normalized.toString()}`;
}

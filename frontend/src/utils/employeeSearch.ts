import type { EmployeeRecord } from '../types/domain';

export const EMPLOYEE_SEARCH_DEBOUNCE_MS = 80;
export const EMPLOYEE_DIRECTORY_REFRESH_MS = 60_000;

interface EmployeeResultsPointerContext {
  insideLookup: boolean;
  insideSidebar: boolean;
}

interface EmployeeDirectoryRefreshContext {
  cacheIsStale: boolean;
  cachedMatchCount: number;
  query: string;
  silent: boolean;
}

export function shouldCloseEmployeeResults({
  insideLookup,
  insideSidebar,
}: EmployeeResultsPointerContext): boolean {
  return !insideLookup && !insideSidebar;
}

export function normalizeEmployeeSearchQuery(query: string): string {
  return query.trim().replace(/\s+/g, ' ');
}

export function shouldAwaitFreshEmployeeDirectory({
  cacheIsStale,
  cachedMatchCount,
  query,
  silent,
}: EmployeeDirectoryRefreshContext): boolean {
  return (
    cacheIsStale
    && !silent
    && Boolean(normalizeEmployeeSearchQuery(query))
    && cachedMatchCount === 0
  );
}

export function buildEmployeeSearchParams(query: string): URLSearchParams {
  const params = new URLSearchParams();
  const normalizedQuery = normalizeEmployeeSearchQuery(query);
  if (normalizedQuery) params.set('q', normalizedQuery);
  return params;
}

export function filterEmployeeRecords(
  records: readonly EmployeeRecord[],
  query: string,
): EmployeeRecord[] {
  const terms = normalizeEmployeeSearchQuery(query).toLocaleLowerCase().split(' ').filter(Boolean);
  if (!terms.length) return [...records];

  return records.filter((record) => {
    const searchableValues = [
      record.employee_id,
      record.employee_alias,
      record.name,
      record.department,
      record.role,
      record.manager,
      record.profile_text,
    ].map((value) => String(value || '').toLocaleLowerCase());
    return terms.every((term) => searchableValues.some((value) => value.includes(term)));
  });
}

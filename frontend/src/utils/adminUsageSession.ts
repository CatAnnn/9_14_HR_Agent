import type { AdminExportConversation } from '../types/auth';
import { adminUsageFilterSearch } from './adminUsageFilters.ts';

export const ADMIN_USAGE_SECTIONS = [
  'overview',
  'guidance',
  'rehearsal',
  'report',
] as const;

export type AdminUsageSection = (typeof ADMIN_USAGE_SECTIONS)[number];

export function isAdminUsageSection(value: string | undefined): value is AdminUsageSection {
  return ADMIN_USAGE_SECTIONS.some((section) => section === value);
}

export function adminUsageSessionPath(
  sessionId: string,
  section: AdminUsageSection = 'overview',
  usageSearchParams?: URLSearchParams,
): string {
  const search = usageSearchParams ? adminUsageFilterSearch(usageSearchParams) : '';
  return `/admin/usage/${encodeURIComponent(sessionId)}/${section}${search}`;
}

export function defaultAdminUsageSection(
  conversation: Pick<AdminExportConversation, 'has_rehearsal' | 'stage'>,
): AdminUsageSection {
  if (conversation.stage === 'report_ready') return 'report';
  if (conversation.has_rehearsal) return 'rehearsal';
  if (conversation.stage === 'guidance_ready') return 'guidance';
  return 'overview';
}

import { PERFORMANCE_MANAGEMENT_BASE_PATH } from '../content/performance-management-content';

export const RESOURCE_PATHS = {
  home: '/resources',
  performanceManagement: PERFORMANCE_MANAGEMENT_BASE_PATH,
  performanceManagementDimension: `${PERFORMANCE_MANAGEMENT_BASE_PATH}/:dimensionSlug`,
} as const;

export const RESOURCE_LEGACY_HASHES = ['#resource-books'] as const;

export const RESOURCE_LEGACY_PATHS = [
  '/resource',
  '/resource/report/local',
  '/resource/news/insight',
  '/resource/activities',
  '/resource/knowledge',
  '/resource/topic',
  '/resources/reports',
  '/resources/insights',
  '/resources/activities',
  '/resources/knowledge',
  '/resources/topics',
] as const;

export function isLegacyResourceHash(hash: string): boolean {
  return (RESOURCE_LEGACY_HASHES as readonly string[]).includes(hash.toLocaleLowerCase('en-US'));
}

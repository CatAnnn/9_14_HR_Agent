import type {
  BigFivePersonality,
  EmployeeLatestSetupSettingsResponse,
} from '../types/domain';

export interface RestoredEmployeeSetupSettings {
  found: boolean;
  supplementalInfo: string;
  personality: BigFivePersonality | null;
  primaryMotiveId: string | null;
  secondaryMotiveIds: string[];
}

function normalizedId(value: string | null | undefined): string | null {
  const normalized = String(value || '').trim();
  return normalized || null;
}

export function shouldRestorePreviousEmployeeSettings(
  role: string | null | undefined,
): boolean {
  return role === 'admin';
}

export function normalizeLatestEmployeeSetupSettings(
  response: EmployeeLatestSetupSettingsResponse,
): RestoredEmployeeSetupSettings {
  const primaryMotiveId = normalizedId(response.primary_motive_id);
  const secondaryMotiveIds = (response.secondary_motive_ids || [])
    .map(normalizedId)
    .filter((id): id is string => Boolean(id))
    .filter((id, index, all) => id !== primaryMotiveId && all.indexOf(id) === index)
    .slice(0, 2);

  return {
    found: Boolean(response.found),
    supplementalInfo: String(response.supplemental_info || '').trim(),
    personality: response.personality ? { ...response.personality } : null,
    primaryMotiveId,
    secondaryMotiveIds,
  };
}

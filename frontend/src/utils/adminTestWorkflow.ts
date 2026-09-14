export const ADMIN_TEST_SESSION_STORAGE_KEY = 'hr_agent_admin_test_session_id';

interface AdminTestSessionStorageReader {
  getItem(key: string): string | null;
}

/**
 * A failed bootstrap may return to configuration only after WorkflowContext has
 * invalidated a missing/gone session and removed its stored id.
 */
export function shouldReturnToAdminTestConfiguration(
  storage: AdminTestSessionStorageReader | null | undefined,
): boolean {
  if (!storage) return false;
  try {
    return storage.getItem(ADMIN_TEST_SESSION_STORAGE_KEY) === null;
  } catch {
    return false;
  }
}

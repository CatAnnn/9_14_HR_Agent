export function shouldClearWorkspaceAfterAuthFailure(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  try {
    return (error as { status?: unknown }).status === 401;
  } catch {
    return false;
  }
}

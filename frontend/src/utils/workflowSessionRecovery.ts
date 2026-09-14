export type WorkflowSessionRecoverySource = 'loaded' | 'created';

export interface WorkflowSessionRecoveryResult<Session> {
  session: Session;
  source: WorkflowSessionRecoverySource;
  invalidatedSessionId: string | null;
}

export interface WorkflowSessionRecoveryOptions<Session> {
  storedSessionId: string | null;
  allowSessionCreation: boolean;
  loadSession: (sessionId: string) => Promise<Session>;
  createSession: () => Promise<Session>;
  onInvalidated: (sessionId: string) => void | Promise<void>;
  isInvalidStoredSession: (error: unknown) => boolean;
}

export class WorkflowSessionCreationDisabledError extends Error {
  constructor() {
    super('Workflow session creation is disabled.');
    this.name = 'WorkflowSessionCreationDisabledError';
  }
}

export async function recoverWorkflowSession<Session>({
  storedSessionId,
  allowSessionCreation,
  loadSession,
  createSession,
  onInvalidated,
  isInvalidStoredSession,
}: WorkflowSessionRecoveryOptions<Session>): Promise<WorkflowSessionRecoveryResult<Session>> {
  let invalidatedSessionId: string | null = null;

  if (storedSessionId) {
    try {
      return {
        session: await loadSession(storedSessionId),
        source: 'loaded',
        invalidatedSessionId: null,
      };
    } catch (error) {
      if (!isInvalidStoredSession(error)) throw error;
      invalidatedSessionId = storedSessionId;
      await onInvalidated(storedSessionId);
    }
  }

  if (!allowSessionCreation) throw new WorkflowSessionCreationDisabledError();

  return {
    session: await createSession(),
    source: 'created',
    invalidatedSessionId,
  };
}

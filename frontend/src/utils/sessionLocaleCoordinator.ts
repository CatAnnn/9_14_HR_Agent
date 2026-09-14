import type { SessionLocale, SessionState } from '../types/domain';

export interface SessionLocaleOperationToken {
  epoch: number;
  locale: SessionLocale;
  sessionId: string;
}

export class SessionLocaleSyncError extends Error {
  readonly cause: unknown;

  constructor(cause: unknown) {
    super('The session language could not be synchronized.');
    this.name = 'SessionLocaleSyncError';
    this.cause = cause;
  }
}

interface SessionLocaleSyncCallbacks {
  getSession: () => SessionState | null;
  updateLocale: (sessionId: string, locale: SessionLocale) => Promise<SessionState>;
  refreshSession: (sessionId: string) => Promise<SessionState>;
  applySession: (session: SessionState, locale: SessionLocale) => void;
  rollbackLanguage: (locale: SessionLocale) => void;
}

function sessionLocale(session: SessionState): SessionLocale {
  return session.locale || 'zh-CN';
}

/**
 * Serializes the session-locale PATCH and gives generated requests an epoch.
 * A rapid language change converges to the latest target before any waiter is
 * released, while results captured before that change become stale.
 */
export class SessionLocaleCoordinator {
  private desiredLocale: SessionLocale;
  private epoch = 0;
  private running: Promise<SessionState | null> | null = null;

  constructor(initialLocale: SessionLocale) {
    this.desiredLocale = initialLocale;
  }

  setDesiredLocale(locale: SessionLocale): void {
    if (locale === this.desiredLocale) return;
    this.desiredLocale = locale;
    this.epoch += 1;
  }

  currentEpoch(): number {
    return this.epoch;
  }

  capture(session: SessionState): SessionLocaleOperationToken {
    return {
      epoch: this.epoch,
      locale: this.desiredLocale,
      sessionId: session.session_id,
    };
  }

  isCurrent(token: SessionLocaleOperationToken, session: SessionState | null): boolean {
    return Boolean(
      session
      && token.epoch === this.epoch
      && token.locale === this.desiredLocale
      && token.sessionId === session.session_id
      && token.locale === sessionLocale(session),
    );
  }

  async synchronize(callbacks: SessionLocaleSyncCallbacks): Promise<SessionState | null> {
    while (true) {
      if (this.running) {
        try {
          await this.running;
        } catch (error) {
          if (error instanceof SessionLocaleSyncError) throw error;
          const current = callbacks.getSession();
          if (current && sessionLocale(current) === this.desiredLocale) return current;
          throw error;
        }
        continue;
      }

      const activeSession = callbacks.getSession();
      if (!activeSession) return null;
      const targetLocale = this.desiredLocale;
      if (sessionLocale(activeSession) === targetLocale) return activeSession;

      let task: Promise<SessionState | null>;
      task = (async () => {
        let updated: SessionState;
        try {
          updated = await callbacks.updateLocale(activeSession.session_id, targetLocale);
          if (sessionLocale(updated) !== targetLocale) {
            throw new Error('The server returned a session with an unexpected locale.');
          }
        } catch (firstPatchError) {
          let terminalPatchError: unknown = firstPatchError;
          let refreshed: SessionState | null = null;
          try {
            refreshed = await callbacks.refreshSession(activeSession.session_id);
          } catch {
            // Roll back to the last confirmed session locale when recovery is unavailable.
          }

          if (refreshed && callbacks.getSession()?.session_id === activeSession.session_id) {
            callbacks.applySession(refreshed, sessionLocale(refreshed));
          }
          if (refreshed && sessionLocale(refreshed) === targetLocale) return refreshed;

          // A superseded PATCH must not roll back a newer language choice.
          if (this.desiredLocale !== targetLocale) return refreshed;

          // A concurrent session revision can make the first PATCH fail. After
          // refreshing that revision, retry exactly once before rolling back.
          if (refreshed) {
            try {
              updated = await callbacks.updateLocale(activeSession.session_id, targetLocale);
              if (sessionLocale(updated) !== targetLocale) {
                throw new Error('The server returned a session with an unexpected locale.');
              }
              if (callbacks.getSession()?.session_id === activeSession.session_id) {
                callbacks.applySession(updated, targetLocale);
              }
              return updated;
            } catch (retryError) {
              terminalPatchError = retryError;
              try {
                const retryRefresh = await callbacks.refreshSession(activeSession.session_id);
                refreshed = retryRefresh;
                if (callbacks.getSession()?.session_id === activeSession.session_id) {
                  callbacks.applySession(retryRefresh, sessionLocale(retryRefresh));
                }
                if (sessionLocale(retryRefresh) === targetLocale) return retryRefresh;
              } catch {
                // The last confirmed refreshed locale below remains the rollback target.
              }
              if (this.desiredLocale !== targetLocale) return refreshed;
            }
          }

          const rollbackLocale = refreshed ? sessionLocale(refreshed) : sessionLocale(activeSession);
          this.setDesiredLocale(rollbackLocale);
          callbacks.rollbackLanguage(rollbackLocale);
          throw new SessionLocaleSyncError(terminalPatchError);
        }

        if (callbacks.getSession()?.session_id === activeSession.session_id) {
          callbacks.applySession(updated, targetLocale);
        }
        return updated;
      })().finally(() => {
        if (this.running === task) this.running = null;
      });
      this.running = task;

      try {
        await task;
      } catch (error) {
        if (error instanceof SessionLocaleSyncError) throw error;
        const current = callbacks.getSession();
        if (current && sessionLocale(current) === this.desiredLocale) return current;
        throw error;
      }
      // The user may have switched again while PATCH was in flight. Loop until
      // the server session matches the latest desired locale.
    }
  }
}

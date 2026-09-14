import assert from 'node:assert/strict';
import test from 'node:test';

import type { SessionLocale, SessionState } from '../src/types/domain.ts';
import {
  SessionLocaleCoordinator,
  SessionLocaleSyncError,
} from '../src/utils/sessionLocaleCoordinator.ts';

function state(locale: SessionLocale): SessionState {
  return { session_id: 'session-1', stage: 'setup_ready', locale };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test('a generated operation waits until the locale PATCH barrier has completed', async () => {
  const coordinator = new SessionLocaleCoordinator('zh-CN');
  let current = state('zh-CN');
  coordinator.setDesiredLocale('en');
  const patch = deferred<SessionState>();
  let generationStarted = false;

  const barrier = coordinator.synchronize({
    getSession: () => current,
    updateLocale: () => patch.promise,
    refreshSession: async () => current,
    applySession: (next) => { current = next; },
    rollbackLanguage: () => undefined,
  });
  const generation = barrier.then(() => { generationStarted = true; });

  await Promise.resolve();
  assert.equal(generationStarted, false);
  patch.resolve(state('en'));
  await generation;
  assert.equal(generationStarted, true);
  assert.equal(current.locale, 'en');
});

test('rapid zh-CN to en to zh-CN switching converges before releasing waiters', async () => {
  const coordinator = new SessionLocaleCoordinator('zh-CN');
  let current = state('zh-CN');
  const patches: Array<{ locale: SessionLocale; result: ReturnType<typeof deferred<SessionState>> }> = [];
  const callbacks = {
    getSession: () => current,
    updateLocale: (_sessionId: string, locale: SessionLocale) => {
      const result = deferred<SessionState>();
      patches.push({ locale, result });
      return result.promise;
    },
    refreshSession: async () => current,
    applySession: (next: SessionState) => { current = next; },
    rollbackLanguage: () => undefined,
  };

  coordinator.setDesiredLocale('en');
  const oldToken = coordinator.capture(current);
  const firstWaiter = coordinator.synchronize(callbacks);
  await Promise.resolve();
  assert.deepEqual(patches.map(({ locale }) => locale), ['en']);

  coordinator.setDesiredLocale('zh-CN');
  const latestWaiter = coordinator.synchronize(callbacks);
  patches[0].result.resolve(state('en'));
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(patches.map(({ locale }) => locale), ['en', 'zh-CN']);
  assert.equal(coordinator.isCurrent(oldToken, current), false);

  patches[1].result.resolve(state('zh-CN'));
  await Promise.all([firstWaiter, latestWaiter]);
  assert.equal(current.locale, 'zh-CN');
  assert.equal(coordinator.isCurrent(coordinator.capture(current), current), true);
});

test('rapid four-language switching converges on the latest requested locale', async () => {
  const coordinator = new SessionLocaleCoordinator('zh-CN');
  let current = state('zh-CN');
  const patches: Array<{ locale: SessionLocale; result: ReturnType<typeof deferred<SessionState>> }> = [];
  const callbacks = {
    getSession: () => current,
    updateLocale: (_sessionId: string, locale: SessionLocale) => {
      const result = deferred<SessionState>();
      patches.push({ locale, result });
      return result.promise;
    },
    refreshSession: async () => current,
    applySession: (next: SessionState) => { current = next; },
    rollbackLanguage: () => undefined,
  };

  coordinator.setDesiredLocale('en');
  const firstWaiter = coordinator.synchronize(callbacks);
  await Promise.resolve();
  coordinator.setDesiredLocale('de');
  coordinator.setDesiredLocale('ja');

  patches[0].result.resolve(state('en'));
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(patches.map(({ locale }) => locale), ['en', 'ja']);

  patches[1].result.resolve(state('ja'));
  await firstWaiter;
  assert.equal(current.locale, 'ja');
  assert.equal(coordinator.capture(current).locale, 'ja');
});

test('a permanent PATCH failure rolls the UI back and rejects the waiting operation', async () => {
  const coordinator = new SessionLocaleCoordinator('zh-CN');
  let current = state('zh-CN');
  let rolledBackTo: SessionLocale | null = null;
  let patchAttempts = 0;
  coordinator.setDesiredLocale('en');

  await assert.rejects(
    coordinator.synchronize({
      getSession: () => current,
      updateLocale: async () => {
        patchAttempts += 1;
        throw new Error('PATCH failed');
      },
      refreshSession: async () => current,
      applySession: (next) => { current = next; },
      rollbackLanguage: (locale) => { rolledBackTo = locale; },
    }),
    SessionLocaleSyncError,
  );
  assert.equal(patchAttempts, 2);
  assert.equal(rolledBackTo, 'zh-CN');
  assert.equal(coordinator.capture(current).locale, 'zh-CN');
});

test('a revision conflict refreshes once and retries the locale PATCH exactly once', async () => {
  const coordinator = new SessionLocaleCoordinator('zh-CN');
  let current = state('zh-CN');
  let patchAttempts = 0;
  let refreshes = 0;
  coordinator.setDesiredLocale('en');

  const synchronized = await coordinator.synchronize({
    getSession: () => current,
    updateLocale: async () => {
      patchAttempts += 1;
      if (patchAttempts === 1) throw new Error('revision conflict');
      return state('en');
    },
    refreshSession: async () => {
      refreshes += 1;
      return current;
    },
    applySession: (next) => { current = next; },
    rollbackLanguage: () => { throw new Error('must not roll back'); },
  });

  assert.equal(patchAttempts, 2);
  assert.equal(refreshes, 1);
  assert.equal(synchronized?.locale, 'en');
  assert.equal(current.locale, 'en');
});

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  recoverWorkflowSession,
  WorkflowSessionCreationDisabledError,
} from '../src/utils/workflowSessionRecovery.ts';

interface TestSession {
  session_id: string;
}

const isInvalidStoredSession = (error: unknown): boolean => (
  typeof error === 'object'
  && error !== null
  && (
    (error as { status?: unknown }).status === 404
    || (error as { status?: unknown }).status === 410
  )
);

test('loads the stored session without creating or invalidating anything', async () => {
  let createCalls = 0;
  const invalidated: string[] = [];
  const loaded = { session_id: 'session-12' };

  const result = await recoverWorkflowSession({
    storedSessionId: loaded.session_id,
    allowSessionCreation: true,
    loadSession: async (sessionId) => {
      assert.equal(sessionId, loaded.session_id);
      return loaded;
    },
    createSession: async () => {
      createCalls += 1;
      return { session_id: 'unexpected' };
    },
    isInvalidStoredSession,
    onInvalidated: (sessionId) => { invalidated.push(sessionId); },
  });

  assert.deepEqual(result, {
    session: loaded,
    source: 'loaded',
    invalidatedSessionId: null,
  });
  assert.equal(createCalls, 0);
  assert.deepEqual(invalidated, []);
});

test('timeout, network, auth, rate-limit, and server failures preserve the stored session', async () => {
  const failures: unknown[] = [
    new DOMException('timed out', 'AbortError'),
    new TypeError('network failed'),
    { status: 401 },
    { status: 429 },
    { status: 500 },
    { status: 503 },
  ];

  for (const failure of failures) {
    let createCalls = 0;
    let invalidationCalls = 0;
    await assert.rejects(
      recoverWorkflowSession<TestSession>({
        storedSessionId: 'session-12',
        allowSessionCreation: true,
        loadSession: async () => { throw failure; },
        createSession: async () => {
          createCalls += 1;
          return { session_id: 'unexpected' };
        },
        isInvalidStoredSession,
        onInvalidated: () => { invalidationCalls += 1; },
      }),
      (error) => error === failure,
    );
    assert.equal(createCalls, 0);
    assert.equal(invalidationCalls, 0);
  }
});

test('404 and 410 invalidate once and create exactly one replacement session', async () => {
  for (const status of [404, 410]) {
    let createCalls = 0;
    const invalidated: string[] = [];
    const replacement = { session_id: 'replacement-' + status };

    const result = await recoverWorkflowSession<TestSession>({
      storedSessionId: 'stale-session',
      allowSessionCreation: true,
      loadSession: async () => { throw { status }; },
      createSession: async () => {
        createCalls += 1;
        return replacement;
      },
      isInvalidStoredSession,
      onInvalidated: (sessionId) => { invalidated.push(sessionId); },
    });

    assert.deepEqual(result, {
      session: replacement,
      source: 'created',
      invalidatedSessionId: 'stale-session',
    });
    assert.equal(createCalls, 1);
    assert.deepEqual(invalidated, ['stale-session']);
  }
});

test('admin workflow invalidates 404/410 but never creates a replacement', async () => {
  let createCalls = 0;
  const invalidated: string[] = [];

  await assert.rejects(
    recoverWorkflowSession<TestSession>({
      storedSessionId: 'stale-admin-session',
      allowSessionCreation: false,
      loadSession: async () => { throw { status: 404 }; },
      createSession: async () => {
        createCalls += 1;
        return { session_id: 'unexpected' };
      },
      isInvalidStoredSession,
      onInvalidated: (sessionId) => { invalidated.push(sessionId); },
    }),
    WorkflowSessionCreationDisabledError,
  );

  assert.equal(createCalls, 0);
  assert.deepEqual(invalidated, ['stale-admin-session']);
});

test('a workflow with no stored id creates once, while admin remains creation-disabled', async () => {
  let normalCreateCalls = 0;
  const created = await recoverWorkflowSession<TestSession>({
    storedSessionId: null,
    allowSessionCreation: true,
    loadSession: async () => { throw new Error('load must not run'); },
    createSession: async () => {
      normalCreateCalls += 1;
      return { session_id: 'new-session' };
    },
    isInvalidStoredSession,
    onInvalidated: () => { throw new Error('invalidate must not run'); },
  });
  assert.equal(normalCreateCalls, 1);
  assert.equal(created.source, 'created');
  assert.equal(created.invalidatedSessionId, null);

  let adminCreateCalls = 0;
  await assert.rejects(
    recoverWorkflowSession<TestSession>({
      storedSessionId: null,
      allowSessionCreation: false,
      loadSession: async () => { throw new Error('load must not run'); },
      createSession: async () => {
        adminCreateCalls += 1;
        return { session_id: 'unexpected' };
      },
      isInvalidStoredSession,
      onInvalidated: () => { throw new Error('invalidate must not run'); },
    }),
    WorkflowSessionCreationDisabledError,
  );
  assert.equal(adminCreateCalls, 0);
});

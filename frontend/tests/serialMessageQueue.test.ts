import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createSerialMessageQueue,
  type SerialMessageQueueSnapshot,
} from '../src/utils/serialMessageQueue.ts';

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

async function waitFor(predicate: () => boolean, description: string) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (predicate()) return;
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
  assert.fail(`Timed out waiting for ${description}`);
}

test('runs messages strictly FIFO with at most one active worker', async () => {
  const gates = new Map([
    ['A', deferred<boolean>()],
    ['B', deferred<boolean>()],
    ['C', deferred<boolean>()],
  ]);
  const calls: string[] = [];
  let activeWorkers = 0;
  let maximumActiveWorkers = 0;

  const queue = createSerialMessageQueue(async (message: string) => {
    calls.push(message);
    activeWorkers += 1;
    maximumActiveWorkers = Math.max(maximumActiveWorkers, activeWorkers);
    try {
      return await gates.get(message)!.promise;
    } finally {
      activeWorkers -= 1;
    }
  });

  queue.enqueue('A');
  queue.enqueue('B');
  queue.enqueue('C');
  queue.retry();
  queue.retry();

  await waitFor(() => calls.length === 1, 'the first worker to start');
  assert.deepEqual(calls, ['A']);
  assert.deepEqual(queue.getSnapshot(), {
    active: true,
    paused: false,
    size: 3,
    waiting: 2,
  });

  gates.get('A')!.resolve(true);
  await waitFor(() => calls.length === 2, 'the second worker to start');
  assert.deepEqual(calls, ['A', 'B']);

  gates.get('B')!.resolve(true);
  await waitFor(() => calls.length === 3, 'the third worker to start');
  assert.deepEqual(calls, ['A', 'B', 'C']);

  gates.get('C')!.resolve(true);
  await waitFor(() => queue.getSnapshot().size === 0, 'the queue to drain');
  assert.equal(maximumActiveWorkers, 1);
  assert.deepEqual(queue.getSnapshot(), {
    active: false,
    paused: false,
    size: 0,
    waiting: 0,
  });
});

test('pauses on a false result, retains the failed item, and retries it first', async () => {
  const calls: string[] = [];
  let firstAttempt = true;
  const queue = createSerialMessageQueue(async (message: string) => {
    calls.push(message);
    if (message === 'A' && firstAttempt) {
      firstAttempt = false;
      return false;
    }
    return true;
  });

  queue.enqueue('A');
  queue.enqueue('B');
  await waitFor(() => queue.getSnapshot().paused, 'the queue to pause');

  assert.deepEqual(calls, ['A']);
  assert.deepEqual(queue.getSnapshot(), {
    active: false,
    paused: true,
    size: 2,
    waiting: 2,
  });

  assert.equal(queue.retry(), true);
  assert.equal(queue.retry(), false);
  await waitFor(() => queue.getSnapshot().size === 0, 'the retried queue to drain');

  assert.deepEqual(calls, ['A', 'A', 'B']);
  assert.equal(queue.getSnapshot().paused, false);
});

test('pauses when the worker throws and resumes from the same item', async () => {
  const calls: string[] = [];
  let shouldThrow = true;
  const queue = createSerialMessageQueue(async (message: string) => {
    calls.push(message);
    if (shouldThrow) {
      shouldThrow = false;
      throw new Error('temporary failure');
    }
    return true;
  });

  queue.enqueue('A');
  queue.enqueue('B');
  await waitFor(() => queue.getSnapshot().paused, 'the thrown worker to pause the queue');
  assert.deepEqual(calls, ['A']);

  assert.equal(queue.retry(), true);
  await waitFor(() => queue.getSnapshot().size === 0, 'the thrown item retry to drain');
  assert.deepEqual(calls, ['A', 'A', 'B']);
});

test('clearPending keeps an active item but removes every waiting item', async () => {
  const gate = deferred<boolean>();
  const calls: string[] = [];
  const queue = createSerialMessageQueue(async (message: string) => {
    calls.push(message);
    return await gate.promise;
  });

  queue.enqueue('A');
  queue.enqueue('B');
  queue.enqueue('C');
  await waitFor(() => calls.length === 1, 'the active item to start');

  queue.clearPending();
  assert.deepEqual(queue.getSnapshot(), {
    active: true,
    paused: false,
    size: 1,
    waiting: 0,
  });

  gate.resolve(true);
  await waitFor(() => queue.getSnapshot().size === 0, 'the retained active item to finish');
  assert.deepEqual(calls, ['A']);
});

test('clearPending clears a paused failed item and all items behind it', async () => {
  const calls: string[] = [];
  const snapshots: SerialMessageQueueSnapshot[] = [];
  const queue = createSerialMessageQueue(
    async (message: string) => {
      calls.push(message);
      return false;
    },
    (snapshot) => snapshots.push(snapshot),
  );

  queue.enqueue('A');
  queue.enqueue('B');
  await waitFor(() => queue.getSnapshot().paused, 'the queue to pause before clearing');

  queue.clearPending();
  assert.deepEqual(queue.getSnapshot(), {
    active: false,
    paused: false,
    size: 0,
    waiting: 0,
  });
  assert.equal(queue.retry(), false);
  assert.deepEqual(calls, ['A']);
  assert.ok(snapshots.some((snapshot) => snapshot.active && snapshot.waiting === 1));
});

test('treats identical message values as independent queue items', async () => {
  const calls: string[] = [];
  const queue = createSerialMessageQueue(async (message: string) => {
    calls.push(message);
    return true;
  });

  queue.enqueue('same text');
  queue.enqueue('same text');
  await waitFor(() => queue.getSnapshot().size === 0, 'both identical items to finish');

  assert.deepEqual(calls, ['same text', 'same text']);
});

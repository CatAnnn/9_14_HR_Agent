import assert from 'node:assert/strict';
import test from 'node:test';

import { lazyRoute } from '../src/utils/lazyRoute.ts';

const TestPage = () => null;

test('preload shares one in-flight module request', async () => {
  let calls = 0;
  const route = lazyRoute(async () => {
    calls += 1;
    return { default: TestPage };
  });

  await Promise.all([route.preload(), route.preload(), route.preload()]);

  assert.equal(calls, 1);
});

test('preload retries one transient chunk loading failure', async () => {
  let calls = 0;
  const route = lazyRoute(async () => {
    calls += 1;
    if (calls === 1) {
      throw new TypeError('Failed to fetch dynamically imported module');
    }
    return { default: TestPage };
  });

  await route.preload();

  assert.equal(calls, 2);
});

test('preload does not retry module evaluation errors', async () => {
  let calls = 0;
  const route = lazyRoute(async () => {
    calls += 1;
    throw new Error('route module evaluation failed');
  });

  await assert.rejects(route.preload(), /module evaluation failed/);
  assert.equal(calls, 1);
});

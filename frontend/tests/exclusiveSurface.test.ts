import assert from 'node:assert/strict';
import test from 'node:test';

import { createExclusiveSurfaceCoordinator } from '../src/utils/exclusiveSurface.ts';

test('claiming a new surface dismisses the previous owner immediately', () => {
  const coordinator = createExclusiveSurfaceCoordinator();
  const firstOwner = {};
  const secondOwner = {};
  let firstDismissals = 0;
  let secondDismissals = 0;

  coordinator.claim(firstOwner, () => {
    firstDismissals += 1;
    coordinator.release(firstOwner);
  });
  coordinator.claim(secondOwner, () => {
    secondDismissals += 1;
  });

  assert.equal(firstDismissals, 1);
  assert.equal(secondDismissals, 0);

  // Releasing the stale owner from its dismiss callback must not clear the new
  // lease; a third owner still has to dismiss the second one.
  coordinator.claim({}, () => {});
  assert.equal(secondDismissals, 1);
});

test('reclaiming the same surface refreshes its callback without self-dismissal', () => {
  const coordinator = createExclusiveSurfaceCoordinator();
  const owner = {};
  let staleDismissals = 0;
  let currentDismissals = 0;

  coordinator.claim(owner, () => {
    staleDismissals += 1;
  });
  coordinator.claim(owner, () => {
    currentDismissals += 1;
  });
  coordinator.claim({}, () => {});

  assert.equal(staleDismissals, 0);
  assert.equal(currentDismissals, 1);
});

test('releasing the active surface leaves no callback to dismiss', () => {
  const coordinator = createExclusiveSurfaceCoordinator();
  const owner = {};
  let dismissals = 0;

  coordinator.claim(owner, () => {
    dismissals += 1;
  });
  coordinator.release(owner);
  coordinator.claim({}, () => {});

  assert.equal(dismissals, 0);
});

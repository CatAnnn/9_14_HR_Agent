import assert from 'node:assert/strict';
import test from 'node:test';

import {
  activatePushToTalkShortcut,
  cancelPushToTalkShortcut,
  createPushToTalkShortcutState,
  pushToTalkShortcutKeyDown,
  pushToTalkShortcutKeyUp,
  setPushToTalkShortcutEnabled,
} from '../src/utils/pushToTalkShortcut.ts';

test('an enabled plain Space press starts once and its release stops once', () => {
  let state = createPushToTalkShortcutState(true);

  const down = pushToTalkShortcutKeyDown(state, { key: ' ' }, true);
  assert.deepEqual(
    { start: down.start, stop: down.stop, preventDefault: down.preventDefault },
    { start: true, stop: false, preventDefault: true },
  );
  state = down.state;

  const activated = activatePushToTalkShortcut(state);
  assert.equal(activated.start, false);
  assert.equal(activated.state.activated, true);
  state = activated.state;

  const up = pushToTalkShortcutKeyUp(state, { key: ' ' });
  assert.deepEqual(
    { start: up.start, stop: up.stop, preventDefault: up.preventDefault },
    { start: false, stop: true, preventDefault: true },
  );
  assert.equal(up.cancel, false);
  assert.equal(up.insertSpace, false);
  assert.deepEqual(up.state, createPushToTalkShortcutState(true));
});

test('quick keydown then keyup cancels the preview and restores one ordinary space', () => {
  const initial = createPushToTalkShortcutState(true);
  const down = pushToTalkShortcutKeyDown(initial, { key: ' ' }, true);
  const up = pushToTalkShortcutKeyUp(down.state, { key: ' ' });

  assert.equal(down.start, true);
  assert.equal(up.stop, false);
  assert.equal(up.cancel, true);
  assert.equal(up.insertSpace, true);
  assert.equal(up.preventDefault, true);
  assert.equal(up.state.ownsRecording, false);
});

test('keyboard repeat is consumed without starting another recording', () => {
  const down = pushToTalkShortcutKeyDown(
    createPushToTalkShortcutState(true),
    { key: ' ' },
    true,
  );
  const repeated = pushToTalkShortcutKeyDown(
    down.state,
    { key: ' ', repeat: true },
    true,
  );

  assert.equal(repeated.start, false);
  assert.equal(repeated.stop, false);
  assert.equal(repeated.preventDefault, true);
  assert.deepEqual(repeated.state, down.state);
});

test('IME and modified Space shortcuts remain available to the browser and editor', () => {
  const state = createPushToTalkShortcutState(true);
  const inputs = [
    { key: ' ', isComposing: true },
    { key: ' ', altKey: true },
    { key: ' ', ctrlKey: true },
    { key: ' ', metaKey: true },
    { key: ' ', shiftKey: true },
  ];

  for (const input of inputs) {
    const result = pushToTalkShortcutKeyDown(state, input, true);
    assert.equal(result.start, false);
    assert.equal(result.preventDefault, false);
    assert.deepEqual(result.state, state);
  }
});

test('canStart false neither owns nor consumes Space and keyup cannot stop another recording', () => {
  const state = createPushToTalkShortcutState(true);
  const down = pushToTalkShortcutKeyDown(state, { key: ' ' }, false);
  const up = pushToTalkShortcutKeyUp(down.state, { key: ' ' });

  assert.equal(down.start, false);
  assert.equal(down.preventDefault, false);
  assert.equal(up.stop, false);
  assert.equal(up.preventDefault, false);
  assert.deepEqual(up.state, state);
});

test('the disabled shortcut never intercepts Space', () => {
  const state = createPushToTalkShortcutState(false);
  const down = pushToTalkShortcutKeyDown(state, { key: ' ' }, true);
  const up = pushToTalkShortcutKeyUp(down.state, { key: ' ' });

  assert.equal(down.start, false);
  assert.equal(down.preventDefault, false);
  assert.equal(up.stop, false);
});

test('disabling while held cancels the shortcut-owned recording exactly once', () => {
  const down = pushToTalkShortcutKeyDown(
    createPushToTalkShortcutState(true),
    { key: ' ' },
    true,
  );
  const disabled = setPushToTalkShortcutEnabled(down.state, false);
  const disabledAgain = setPushToTalkShortcutEnabled(disabled.state, false);
  const lateKeyUp = pushToTalkShortcutKeyUp(disabledAgain.state, { key: ' ' });

  assert.equal(disabled.cancel, true);
  assert.equal(disabled.stop, false);
  assert.equal(disabledAgain.cancel, false);
  assert.equal(lateKeyUp.stop, false);
  assert.deepEqual(disabled.state, createPushToTalkShortcutState(false));
});

test('cancel on blur or visibility change aborts once and keeps the mode enabled', () => {
  const down = pushToTalkShortcutKeyDown(
    createPushToTalkShortcutState(true),
    { key: 'Spacebar' },
    true,
  );
  const cancelled = cancelPushToTalkShortcut(down.state);
  const cancelledAgain = cancelPushToTalkShortcut(cancelled.state);

  assert.equal(cancelled.cancel, true);
  assert.equal(cancelled.stop, false);
  assert.equal(cancelled.insertSpace, false);
  assert.equal(cancelled.preventDefault, false);
  assert.equal(cancelled.state.enabled, true);
  assert.equal(cancelledAgain.cancel, false);
});

test('unrelated keys never change shortcut ownership', () => {
  const state = createPushToTalkShortcutState(true);
  const down = pushToTalkShortcutKeyDown(state, { key: 'Enter' }, true);
  const up = pushToTalkShortcutKeyUp(state, { key: 'Enter' });

  assert.deepEqual(down.state, state);
  assert.deepEqual(up.state, state);
  assert.equal(down.preventDefault, false);
  assert.equal(up.preventDefault, false);
});

test('uses KeyboardEvent.code Space before localized key values', () => {
  const state = createPushToTalkShortcutState(true);
  const down = pushToTalkShortcutKeyDown(
    state,
    { key: 'Unidentified', code: 'Space' },
    true,
  );
  const activated = activatePushToTalkShortcut(down.state);
  const up = pushToTalkShortcutKeyUp(
    activated.state,
    { key: 'Unidentified', code: 'Space' },
  );

  assert.equal(down.start, true);
  assert.equal(down.preventDefault, true);
  assert.equal(up.stop, true);
  assert.equal(up.preventDefault, true);
});

test('a non-Space code does not fall back to a misleading key value', () => {
  const state = createPushToTalkShortcutState(true);
  const down = pushToTalkShortcutKeyDown(
    state,
    { key: ' ', code: 'KeyK' },
    true,
  );

  assert.equal(down.start, false);
  assert.equal(down.preventDefault, false);
  assert.deepEqual(down.state, state);
});

test('activation is idempotent and never emits a second start command', () => {
  const down = pushToTalkShortcutKeyDown(
    createPushToTalkShortcutState(true),
    { key: ' ', code: 'Space' },
    true,
  );
  const activated = activatePushToTalkShortcut(down.state);
  const activatedAgain = activatePushToTalkShortcut(activated.state);

  assert.equal(activated.start, false);
  assert.equal(activated.state.activated, true);
  assert.equal(activatedAgain.start, false);
  assert.deepEqual(activatedAgain.state, activated.state);
});

test('a stale activation timer after quick release or cancellation is harmless', () => {
  const down = pushToTalkShortcutKeyDown(
    createPushToTalkShortcutState(true),
    { key: ' ' },
    true,
  );
  const released = pushToTalkShortcutKeyUp(down.state, { key: ' ' });
  const staleAfterRelease = activatePushToTalkShortcut(released.state);

  const downAgain = pushToTalkShortcutKeyDown(released.state, { key: ' ' }, true);
  const cancelled = cancelPushToTalkShortcut(downAgain.state);
  const staleAfterCancel = activatePushToTalkShortcut(cancelled.state);

  assert.equal(staleAfterRelease.start, false);
  assert.equal(staleAfterRelease.state.activated, false);
  assert.equal(staleAfterCancel.start, false);
  assert.equal(staleAfterCancel.state.activated, false);
});

test('releasing an activated hold finalizes even if the original start is still requesting', () => {
  const down = pushToTalkShortcutKeyDown(
    createPushToTalkShortcutState(true),
    { key: ' ' },
    true,
  );
  const activated = activatePushToTalkShortcut(down.state);
  const released = pushToTalkShortcutKeyUp(activated.state, { key: ' ' });

  assert.equal(released.stop, true);
  assert.equal(released.cancel, false);
  assert.equal(released.insertSpace, false);
});

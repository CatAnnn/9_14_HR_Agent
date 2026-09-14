import assert from 'node:assert/strict';
import test from 'node:test';

import { createRehearsalStreamDraft } from '../src/utils/rehearsalStreamDraft.ts';

test('keeps published conversation snapshots immutable while tokens accumulate', () => {
  const previousTurn = Object.freeze({ speaker: 'employee', text: 'Earlier reply.' });
  const history = Object.freeze([previousTurn]);
  const draft = createRehearsalStreamDraft(history, 'Tell me more.');
  const initial = draft.getSnapshot();
  Object.freeze(initial);
  Object.freeze(initial[initial.length - 1]);

  assert.deepEqual(initial, [
    previousTurn,
    { speaker: 'manager', text: 'Tell me more.' },
    { speaker: 'employee', text: '' },
  ]);

  draft.append('First');
  draft.append(' reply');
  const first = draft.getSnapshot();
  Object.freeze(first);
  Object.freeze(first[first.length - 1]);
  draft.append('.');
  const final = draft.getSnapshot();

  assert.equal(initial[2].text, '');
  assert.equal(first[2].text, 'First reply');
  assert.equal(final[2].text, 'First reply.');
  assert.equal(first[0], previousTurn);
  assert.equal(final[0], previousTurn);
  assert.equal(first[1], initial[1]);
  assert.equal(final[1], initial[1]);
  assert.deepEqual(history, [previousTurn]);
});

test('reuses snapshots until a nonempty delta arrives', () => {
  const draft = createRehearsalStreamDraft([], 'Hello');
  const initial = draft.getSnapshot();
  assert.equal(draft.getSnapshot(), initial);
  assert.equal(draft.append(''), false);
  assert.equal(draft.getSnapshot(), initial);

  assert.equal(draft.append('Hello back'), true);
  const updated = draft.getSnapshot();
  assert.notEqual(updated, initial);
  assert.equal(draft.getSnapshot(), updated);
  assert.equal(draft.append(''), false);
  assert.equal(draft.getSnapshot(), updated);
});

test('coalesces a burst of deltas without losing whitespace or split Unicode characters', () => {
  const history = Array.from({ length: 200 }, (_, index) => ({
    speaker: index % 2 ? 'employee' : 'manager',
    text: `Earlier message ${index}`,
  }));
  const draft = createRehearsalStreamDraft(history, 'Continue');
  const initial = draft.getSnapshot();
  const reply = '  我想先确认标准。\nI agree. \uD83D\uDE42  '.repeat(100);

  // Split into UTF-16 code units just as arbitrary transport chunks may do.
  for (let index = 0; index < reply.length; index += 1) {
    draft.append(reply[index]);
  }
  const final = draft.getSnapshot();

  assert.equal(initial.at(-1)?.text, '');
  assert.equal(final.at(-1)?.text, reply);
  assert.equal(final.length, history.length + 2);
  history.forEach((turn, index) => assert.equal(final[index], turn));
  assert.equal(draft.getSnapshot(), final);
});

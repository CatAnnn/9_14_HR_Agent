import assert from 'node:assert/strict';
import test from 'node:test';

import {
  advanceSpeechPlaybackEpoch,
  createSpeechPlaybackEpoch,
  isSpeechPlaybackEpochCurrent,
  type SpeechPlaybackEpoch,
} from '../src/utils/speechPlaybackEpoch.ts';

test('advancing the epoch invalidates old work even when the stream id is reused', () => {
  let current = createSpeechPlaybackEpoch();
  const first = advanceSpeechPlaybackEpoch(current, 'speech-stream-a');
  current = first;
  assert.equal(isSpeechPlaybackEpochCurrent(current, first), true);

  const replacement = advanceSpeechPlaybackEpoch(current, 'speech-stream-a');
  current = replacement;
  assert.equal(isSpeechPlaybackEpochCurrent(current, first), false);
  assert.equal(isSpeechPlaybackEpochCurrent(current, replacement), true);
});

test('a token must match both generation and stream id', () => {
  const current = advanceSpeechPlaybackEpoch(
    createSpeechPlaybackEpoch(),
    'speech-stream-a',
  );
  const wrongStream: SpeechPlaybackEpoch = {
    generation: current.generation,
    streamId: 'speech-stream-b',
  };

  assert.equal(isSpeechPlaybackEpochCurrent(current, wrongStream), false);
});

test('an async continuation cannot commit after a newer playback starts', async () => {
  let current = advanceSpeechPlaybackEpoch(
    createSpeechPlaybackEpoch(),
    'speech-stream-old',
  );
  const oldWork = current;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let sourceStarted = false;

  const queuedWork = (async () => {
    await gate;
    if (isSpeechPlaybackEpochCurrent(current, oldWork)) sourceStarted = true;
  })();

  current = advanceSpeechPlaybackEpoch(current, 'speech-stream-new');
  release();
  await queuedWork;

  assert.equal(sourceStarted, false);
});

test('an old ready callback cannot overwrite the status of a replay epoch', () => {
  let current = advanceSpeechPlaybackEpoch(
    createSpeechPlaybackEpoch(),
    'speech-stream-old',
  );
  const oldReadyTimer = current;
  let status = 'buffering';

  current = advanceSpeechPlaybackEpoch(current, null);
  status = 'playing';
  if (isSpeechPlaybackEpochCurrent(current, oldReadyTimer)) status = 'ready';

  assert.equal(status, 'playing');
});

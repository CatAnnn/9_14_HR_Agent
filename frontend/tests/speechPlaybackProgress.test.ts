import assert from 'node:assert/strict';
import test from 'node:test';

import {
  bufferedSpeechSeconds,
  createSpeechPlaybackProgressReporter,
  pcmAudioSeconds,
  SPEECH_PLAYBACK_PROGRESS_INTERVAL_MS,
} from '../src/utils/speechPlaybackProgress.ts';

function fakeClock() {
  let currentTime = 0;
  let nextId = 1;
  const timers = new Map<number, { callback: () => void; at: number }>();
  return {
    now: () => currentTime,
    setTimer(callback: () => void, delayMs: number) {
      const id = nextId++;
      timers.set(id, { callback, at: currentTime + delayMs });
      return id;
    },
    clearTimer(id: number) {
      timers.delete(id);
    },
    get timers() { return [...timers.values()]; },
    advance(milliseconds: number) {
      const target = currentTime + milliseconds;
      let timer = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      while (timer && timer[1].at <= target) {
        currentTime = timer[1].at;
        timers.delete(timer[0]);
        timer[1].callback();
        timer = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      }
      currentTime = target;
    },
  };
}

type ProgressMessage = {
  type: string;
  speech_stream_id: string;
  buffered_seconds: number;
  sequence: number;
};

function fakeSocket(clock: ReturnType<typeof fakeClock>) {
  const messages: { at: number; data: ProgressMessage }[] = [];
  return {
    readyState: 1,
    messages,
    send(data: string) {
      messages.push({ at: clock.now(), data: JSON.parse(data) as ProgressMessage });
    },
  };
}

function sender(clock: ReturnType<typeof fakeClock>, streamId = 'stream-a') {
  const socket = fakeSocket(clock);
  const state = { currentSocket: socket, current: true, bufferedSeconds: 0 };
  const reporter = createSpeechPlaybackProgressReporter({
    streamId,
    socket,
    getCurrentSocket: () => state.currentSocket,
    isCurrent: () => state.current,
    getBufferedSeconds: () => state.bufferedSeconds,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  return { socket, state, reporter };
}

test('buffered audio includes received PCM awaiting scheduling and scheduled audio awaiting playback', () => {
  const pendingSeconds = pcmAudioSeconds(44_100 * 2 * 3, 44_100);
  assert.equal(pendingSeconds, 3);
  assert.equal(bufferedSpeechSeconds(pendingSeconds, 12, 10), 5);
  // Scheduling transfers the duration between counters without double counting.
  assert.equal(bufferedSpeechSeconds(0, 15, 10), 5);
  assert.equal(bufferedSpeechSeconds(0, 15, 11), 4);
  assert.equal(bufferedSpeechSeconds(0, 15, 20), 0);
  assert.equal(pcmAudioSeconds(5, 2), 1);
});

test('invalid duration inputs produce finite nonnegative feedback', () => {
  for (const value of [Number.NaN, Infinity, -Infinity, -3]) {
    assert.equal(pcmAudioSeconds(value, 44_100), 0);
    assert.equal(pcmAudioSeconds(88_200, value), 0);
    assert.equal(bufferedSpeechSeconds(value, 12, 10), 2);
    assert.equal(bufferedSpeechSeconds(3, value, 10), 3);
    assert.equal(bufferedSpeechSeconds(3, 12, value), 3);
  }
  assert.equal(pcmAudioSeconds(88_200, 0), 0);
  assert.equal(bufferedSpeechSeconds(Number.MAX_VALUE, Number.MAX_VALUE, 0), 0);
});

test('first audio is reported immediately and later chunks stay within the 500 ms limit', () => {
  const clock = fakeClock();
  const user = sender(clock);
  user.reporter.notifyAudioReceived();
  assert.equal(clock.timers.length, 0);
  assert.equal(user.socket.messages.length, 0);
  user.reporter.start();
  assert.equal(clock.timers.length, 1);
  assert.equal(user.socket.messages.length, 0);

  clock.advance(100);
  user.state.bufferedSeconds = 1.25;
  user.reporter.notifyAudioReceived();
  assert.deepEqual(user.socket.messages, [{
    at: 100,
    data: {
      type: 'playback_progress', speech_stream_id: 'stream-a',
      buffered_seconds: 1.25, sequence: 1,
    },
  }]);
  for (let index = 0; index < 10; index += 1) {
    clock.advance(40);
    user.state.bufferedSeconds += 0.2;
    user.reporter.notifyAudioReceived();
  }
  assert.equal(user.socket.messages.length, 1);
  assert.equal(clock.timers.length, 1);
  clock.advance(100);
  assert.deepEqual(user.socket.messages.map(({ at, data }) => [at, data.sequence]), [
    [100, 1], [600, 2],
  ]);
  assert.equal(SPEECH_PLAYBACK_PROGRESS_INTERVAL_MS, 500);
});

test('four independent users maintain separate buffers, sequences and timers', () => {
  const clock = fakeClock();
  const users = Array.from({ length: 4 }, (_, index) => sender(clock, `user-${index + 1}`));
  users.forEach((user, index) => {
    user.state.bufferedSeconds = index + 1;
    user.reporter.start();
    user.reporter.notifyAudioReceived();
  });
  clock.advance(500);
  users.forEach((user, index) => {
    assert.deepEqual(user.socket.messages.map(({ data }) => data.sequence), [1, 2]);
    assert.ok(user.socket.messages.every(({ data }) => (
      data.speech_stream_id === `user-${index + 1}` && data.buffered_seconds === index + 1
    )));
  });
  users[0].reporter.stop();
  clock.advance(500);
  assert.equal(users[0].socket.messages.length, 2);
  assert.ok(users.slice(1).every((user) => user.socket.messages.length === 3));
  assert.equal(clock.timers.length, 3);
});

test('feedback sanitizes invalid buffer values before serializing the protocol payload', () => {
  for (const value of [Number.NaN, Infinity, -Infinity, -1]) {
    const clock = fakeClock();
    const user = sender(clock);
    user.state.bufferedSeconds = value;
    user.reporter.start();
    user.reporter.notifyAudioReceived();
    assert.equal(user.socket.messages[0].data.buffered_seconds, 0);
    user.reporter.stop();
  }
});

test('large buffers saturate the reported value without changing local accounting', () => {
  const clock = fakeClock();
  const user = sender(clock);
  user.state.bufferedSeconds = bufferedSpeechSeconds(50, 160, 10);
  user.reporter.start();
  user.reporter.notifyAudioReceived();

  assert.equal(user.socket.messages[0].data.buffered_seconds, 120);
  assert.equal(user.state.bufferedSeconds, 200);
  user.reporter.stop();
});

test('a replaced timer from the same stream cannot steal its current timer', () => {
  const clock = fakeClock();
  const user = sender(clock);
  user.reporter.start();
  const replacedTimer = clock.timers[0].callback;
  clock.advance(100);
  user.reporter.notifyAudioReceived();
  replacedTimer();
  assert.equal(clock.timers.length, 1);
  clock.advance(500);
  assert.deepEqual(user.socket.messages.map(({ at }) => at), [100, 600]);
  assert.equal(clock.timers.length, 1);
});

test('a suspended AudioContext retains scheduled buffer while wall time advances', () => {
  const clock = fakeClock();
  const socket = fakeSocket(clock);
  let audioContextTime = 10;
  const reporter = createSpeechPlaybackProgressReporter({
    streamId: 'suspended-audio', socket,
    getCurrentSocket: () => socket,
    isCurrent: () => true,
    getBufferedSeconds: () => bufferedSpeechSeconds(3, 12, audioContextTime),
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  reporter.start();
  clock.advance(2_000);
  assert.ok(socket.messages.every(({ data }) => data.buffered_seconds === 5));
  audioContextTime = 11;
  clock.advance(500);
  assert.equal(socket.messages.at(-1)?.data.buffered_seconds, 4);
});

test('replacement connections and stale epochs stop reporting without sending to either socket', () => {
  for (const replaceConnection of [true, false]) {
    const clock = fakeClock();
    const user = sender(clock);
    const replacement = fakeSocket(clock);
    user.reporter.start();
    if (replaceConnection) user.state.currentSocket = replacement;
    else user.state.current = false;
    clock.advance(500);
    user.reporter.notifyAudioReceived();
    assert.equal(clock.timers.length, 0);
    assert.deepEqual(user.socket.messages, []);
    assert.deepEqual(replacement.messages, []);
  }
});

test('stopping is idempotent and a stale timer cannot affect the replacement stream', () => {
  const clock = fakeClock();
  const oldUser = sender(clock, 'old-stream');
  oldUser.reporter.start();
  const staleCallback = clock.timers[0].callback;
  oldUser.reporter.stop();
  oldUser.reporter.stop();
  assert.equal(clock.timers.length, 0);
  const replacement = sender(clock, 'new-stream');
  replacement.reporter.start();
  staleCallback();
  oldUser.reporter.start();
  oldUser.reporter.notifyAudioReceived();
  assert.equal(clock.timers.length, 1);
  clock.advance(500);
  assert.deepEqual(oldUser.socket.messages, []);
  assert.equal(replacement.socket.messages[0].data.sequence, 1);
  assert.equal(replacement.socket.messages[0].data.speech_stream_id, 'new-stream');
});

test('closed sockets do not schedule feedback and send errors remain best effort', () => {
  for (const readyState of [0, 2, 3]) {
    const clock = fakeClock();
    const user = sender(clock);
    user.socket.readyState = readyState;
    user.reporter.start();
    user.reporter.notifyAudioReceived();
    assert.equal(clock.timers.length, 0);
    assert.equal(user.socket.messages.length, 0);
  }
  const clock = fakeClock();
  const user = sender(clock);
  const originalSend = user.socket.send;
  user.socket.send = () => { throw new Error('Connection is closing'); };
  user.reporter.start();
  assert.doesNotThrow(() => user.reporter.notifyAudioReceived());
  assert.equal(clock.timers.length, 1);
  user.socket.send = originalSend;
  clock.advance(500);
  assert.equal(user.socket.messages[0].data.sequence, 1);
  user.reporter.stop();
  assert.equal(clock.timers.length, 0);
});

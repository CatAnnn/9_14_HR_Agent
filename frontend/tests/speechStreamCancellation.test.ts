import assert from 'node:assert/strict';
import test from 'node:test';

import { sendSpeechStreamCancellation } from '../src/utils/speechStreamCancellation.ts';

function fakeSocket(readyState = 1) {
  const messages: string[] = [];
  return {
    readyState,
    messages,
    send(data: string) {
      messages.push(data);
    },
  };
}

test('cancels the active stream on its existing socket without including session authority', () => {
  const socket = fakeSocket();

  assert.equal(sendSpeechStreamCancellation({
    activeSocket: socket,
    currentSocket: socket,
    streamId: 'speech-stream-old',
  }), true);
  assert.deepEqual(socket.messages.map((message) => JSON.parse(message)), [
    { type: 'cancel', speech_stream_id: 'speech-stream-old' },
  ]);
});

test('a replaced socket cannot cancel a stream through the new connection', () => {
  const oldSocket = fakeSocket();
  const newSocket = fakeSocket();

  assert.equal(sendSpeechStreamCancellation({
    activeSocket: oldSocket,
    currentSocket: newSocket,
    streamId: 'speech-stream-old',
  }), false);
  assert.deepEqual(oldSocket.messages, []);
  assert.deepEqual(newSocket.messages, []);
});

test('a stream still preparing is not cancelled when an old socket closes', () => {
  const oldSocket = fakeSocket();

  assert.equal(sendSpeechStreamCancellation({
    activeSocket: null,
    currentSocket: oldSocket,
    streamId: 'speech-stream-new',
  }), false);
  assert.deepEqual(oldSocket.messages, []);
});

test('missing streams and connecting or closed sockets require no connection or send', () => {
  const socket = fakeSocket();
  assert.equal(sendSpeechStreamCancellation({
    activeSocket: socket,
    currentSocket: socket,
    streamId: null,
  }), false);
  assert.equal(sendSpeechStreamCancellation({
    activeSocket: null,
    currentSocket: null,
    streamId: 'speech-stream-old',
  }), false);
  assert.deepEqual(socket.messages, []);

  for (const readyState of [0, 2, 3]) {
    const unavailableSocket = fakeSocket(readyState);
    assert.equal(sendSpeechStreamCancellation({
      activeSocket: unavailableSocket,
      currentSocket: unavailableSocket,
      streamId: 'speech-stream-old',
    }), false);
    assert.deepEqual(unavailableSocket.messages, []);
  }
});

test('send failures do not interrupt local stopping', () => {
  const socket = {
    readyState: 1,
    send() {
      throw new Error('Connection closed during send');
    },
  };
  let locallyStopped = false;

  assert.doesNotThrow(() => {
    assert.equal(sendSpeechStreamCancellation({
      activeSocket: socket,
      currentSocket: socket,
      streamId: 'speech-stream-old',
    }), false);
    locallyStopped = true;
  });
  assert.equal(locallyStopped, true);
});

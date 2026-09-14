import assert from 'node:assert/strict';
import test from 'node:test';

import {
  assertSpeechSampleRate,
  decodePcm16Le,
  isValidPcm16Chunk,
  MAX_SPEECH_SAMPLE_RATE,
  MIN_SPEECH_SAMPLE_RATE,
  parseSpeechByteLength,
  parseSpeechSampleRate,
  SpeechSampleRateError,
  SPEECH_PLAYBACK_START_BUFFER_SECONDS,
  SPEECH_REPLAY_START_BUFFER_SECONDS,
} from '../src/utils/pcmPlayback.ts';

test('uses the measured safe start buffer for two concurrent streams', () => {
  assert.equal(SPEECH_PLAYBACK_START_BUFFER_SECONDS, 1.4);
  assert.equal(SPEECH_REPLAY_START_BUFFER_SECONDS, 0.1);
});

test('decodes signed little-endian PCM16 with exact endpoint scaling', () => {
  const bytes = Uint8Array.from([
    0x00, 0x80,
    0xff, 0xff,
    0x00, 0x00,
    0x01, 0x00,
    0xff, 0x7f,
  ]);

  assert.deepEqual(Array.from(decodePcm16Le(bytes)), [
    -1,
    -1 / 32768,
    0,
    Math.fround(1 / 32767),
    1,
  ]);
});

test('supports unaligned subarrays and ignores an odd trailing byte', () => {
  const backing = Uint8Array.from([0xaa, 0x00, 0x40, 0x00, 0xc0, 0xbb]);
  const bytes = backing.subarray(1);
  const before = Array.from(backing);

  assert.deepEqual(Array.from(decodePcm16Le(bytes)), [
    Math.fround(16_384 / 32_767),
    -16_384 / 32_768,
  ]);
  assert.deepEqual(Array.from(backing), before);
});

test('accepts only finite integer speech sample rates in the protocol range', () => {
  assert.equal(parseSpeechSampleRate(MIN_SPEECH_SAMPLE_RATE), MIN_SPEECH_SAMPLE_RATE);
  assert.equal(parseSpeechSampleRate(MAX_SPEECH_SAMPLE_RATE), MAX_SPEECH_SAMPLE_RATE);
  assert.equal(parseSpeechSampleRate('44100'), 44_100);
  assert.equal(parseSpeechSampleRate('44100.0'), 44_100);
  assert.equal(parseSpeechSampleRate(' 48000 '), 48_000);

  for (const value of [
    undefined,
    null,
    '',
    '44.1k',
    '44100.5',
    '4.41e4',
    '0xAC44',
    true,
    0,
    MIN_SPEECH_SAMPLE_RATE - 1,
    MAX_SPEECH_SAMPLE_RATE + 1,
    Number.NaN,
    Number.POSITIVE_INFINITY,
  ]) {
    assert.equal(parseSpeechSampleRate(value), null, `expected ${String(value)} to be rejected`);
  }
});

test('accepts only positive, complete PCM16 packet lengths', () => {
  assert.equal(parseSpeechByteLength(2), 2);
  assert.equal(parseSpeechByteLength(' 4 '), 4);
  assert.equal(parseSpeechByteLength(0), null);
  assert.equal(parseSpeechByteLength(1), null);
  assert.equal(parseSpeechByteLength('2.0'), null);
  assert.equal(parseSpeechByteLength(Number.NaN), null);
  assert.equal(parseSpeechByteLength(Number.POSITIVE_INFINITY), null);
  assert.equal(parseSpeechByteLength(Number.MAX_SAFE_INTEGER), null);

  assert.equal(isValidPcm16Chunk(Uint8Array.from([0, 1])), true);
  assert.equal(isValidPcm16Chunk(Uint8Array.from([0])), false);
  assert.equal(isValidPcm16Chunk(new Uint8Array()), false);
});

test('anchors the first legal stream rate and rejects a later mismatch', () => {
  const first = assertSpeechSampleRate(44_100);
  assert.equal(first, 44_100);
  assert.equal(assertSpeechSampleRate('44100', first), 44_100);
  // Repeated metadata may be omitted by an older packet format once the
  // stream has already established its rate.
  assert.equal(assertSpeechSampleRate(undefined, first), first);

  assert.throws(
    () => assertSpeechSampleRate(48_000, first),
    (error: unknown) => (
      error instanceof SpeechSampleRateError
      && error.kind === 'mismatch'
      && error.received === 48_000
      && error.expected === 44_100
    ),
  );
  assert.throws(
    () => assertSpeechSampleRate(undefined),
    (error: unknown) => (
      error instanceof SpeechSampleRateError
      && error.kind === 'invalid'
    ),
  );
});

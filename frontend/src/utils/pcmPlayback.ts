/** Covers the measured worst inter-chunk gap at two concurrent Fish streams. */
export const SPEECH_PLAYBACK_START_BUFFER_SECONDS = 1.4;
export const SPEECH_REPLAY_START_BUFFER_SECONDS = 0.1;

/**
 * The browser can decode a broad PCM rate range, but accepting arbitrary or
 * missing metadata is unsafe: AudioBuffer interprets samples at the supplied
 * rate, so a wrong fallback changes both pitch and playback speed.  Keep the
 * protocol guard deliberately independent from the model's configured rate;
 * the server may legitimately return another rate within this range.
 */
export const MIN_SPEECH_SAMPLE_RATE = 8_000;
export const MAX_SPEECH_SAMPLE_RATE = 96_000;

export type SpeechSampleRateErrorKind = 'invalid' | 'mismatch';

export class SpeechSampleRateError extends Error {
  readonly kind: SpeechSampleRateErrorKind;
  readonly received: number | null;
  readonly expected: number | null;

  constructor(
    kind: SpeechSampleRateErrorKind,
    received: number | null,
    expected: number | null = null,
  ) {
    super(kind === 'mismatch'
      ? 'Speech sample rate changed during the stream.'
      : 'Speech stream reported an invalid sample rate.');
    this.name = 'SpeechSampleRateError';
    this.kind = kind;
    this.received = received;
    this.expected = expected;
  }
}

/**
 * Parse and validate a sample-rate value received over the JSON WebSocket
 * protocol. Numeric strings are accepted because WebSocket metadata can be
 * serialized by different server versions; booleans, empty strings, non-integer
 * decimal fractions and non-finite values are rejected.
 */
export function parseSpeechSampleRate(value: unknown): number | null {
  let parsed: number;
  if (typeof value === 'number') {
    parsed = value;
  } else if (typeof value === 'string') {
    const normalized = value.trim();
    // Metadata should be a plain decimal integer.  Accept a harmless
    // ``44100.0`` emitted by some serializers, but reject scientific/hex
    // notation so malformed protocol values cannot be silently normalised.
    if (!/^\d+(?:\.0+)?$/.test(normalized)) return null;
    parsed = Number(normalized);
  } else {
    return null;
  }
  if (
    !Number.isInteger(parsed)
    || !Number.isFinite(parsed)
    || parsed < MIN_SPEECH_SAMPLE_RATE
    || parsed > MAX_SPEECH_SAMPLE_RATE
  ) {
    return null;
  }
  return parsed;
}

/**
 * Validate one stream-rate announcement and, when supplied, compare it with
 * the stream's already-anchored rate. The return value is always the legal
 * rate that callers should use for decoding/scheduling.
 */
export function assertSpeechSampleRate(
  value: unknown,
  expected: number | null = null,
): number {
  // Once a stream has announced a legal rate, a legacy packet that omits the
  // repeated metadata can safely reuse that anchor.  A first packet still
  // must provide an explicit legal value (the caller passes expected=null).
  if (value === undefined && expected !== null) return expected;
  const parsed = parseSpeechSampleRate(value);
  if (parsed === null) throw new SpeechSampleRateError('invalid', null, expected);
  if (expected !== null && parsed !== expected) {
    throw new SpeechSampleRateError('mismatch', parsed, expected);
  }
  return expected ?? parsed;
}

/**
 * Validate the byte length announced before a binary audio packet.  The
 * length is a framing value, not a playback setting, so accepting missing,
 * zero, NaN, fractions or odd values would let a malformed packet bypass the
 * integrity check and contaminate the replay buffer.
 */
export function parseSpeechByteLength(value: unknown): number | null {
  let parsed: number;
  if (typeof value === 'number') {
    parsed = value;
  } else if (typeof value === 'string' && /^\d+$/.test(value.trim())) {
    parsed = Number(value.trim());
  } else {
    return null;
  }
  if (
    !Number.isSafeInteger(parsed)
    || parsed <= 0
    || parsed % 2 !== 0
  ) {
    return null;
  }
  return parsed;
}

/** A PCM16 packet must contain at least one complete little-endian sample. */
export function isValidPcm16Chunk(bytes: Uint8Array): boolean {
  return bytes.byteLength > 0 && bytes.byteLength % 2 === 0;
}

export function decodePcm16Le(bytes: Uint8Array): Float32Array {
  const sampleCount = Math.floor(bytes.byteLength / 2);
  const samples = new Float32Array(sampleCount);
  const view = new DataView(bytes.buffer, bytes.byteOffset, sampleCount * 2);
  for (let index = 0; index < sampleCount; index += 1) {
    const sample = view.getInt16(index * 2, true);
    samples[index] = sample < 0 ? sample / 32768 : sample / 32767;
  }
  return samples;
}

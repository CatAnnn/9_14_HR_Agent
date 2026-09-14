import { useCallback, useEffect, useRef, useState } from 'react';
import { useLanguage } from '../i18n/LanguageContext';
import { getWsApiBase } from '../utils/format';
import {
  assertSpeechSampleRate,
  decodePcm16Le,
  isValidPcm16Chunk,
  parseSpeechByteLength,
  SpeechSampleRateError,
  SPEECH_PLAYBACK_START_BUFFER_SECONDS,
  SPEECH_REPLAY_START_BUFFER_SECONDS,
} from '../utils/pcmPlayback';
import {
  advanceSpeechPlaybackEpoch,
  createSpeechPlaybackEpoch,
  isSpeechPlaybackEpochCurrent,
  type SpeechPlaybackEpoch,
} from '../utils/speechPlaybackEpoch';
import { sendSpeechStreamCancellation } from '../utils/speechStreamCancellation';
import {
  bufferedSpeechSeconds,
  createSpeechPlaybackProgressReporter,
  pcmAudioSeconds,
  type SpeechPlaybackProgressReporter,
} from '../utils/speechPlaybackProgress';

export type StreamingSpeechStatus = 'idle' | 'buffering' | 'playing' | 'ready' | 'error';

type AudioContextWindow = Window & {
  webkitAudioContext?: typeof AudioContext;
};

type PcmChunk = {
  bytes: Uint8Array | null;
  sampleRate: number;
  audioBuffer: AudioBuffer | null;
};

type PendingBinaryChunk = {
  sessionId: string;
  streamId: string;
  sampleRate: number;
  byteLength: number;
  playbackEpoch: SpeechPlaybackEpoch;
};

type Translate = (source: string, english?: string) => string;

const SOCKET_READY_TIMEOUT_MS = 8_000;
const SOCKET_HEARTBEAT_MS = 15_000;

function createSpeechStreamId(): string {
  if (typeof window.crypto?.randomUUID === 'function') {
    return window.crypto.randomUUID();
  }
  const words = new Uint32Array(4);
  window.crypto.getRandomValues(words);
  return Array.from(words, (word) => word.toString(36)).join('_');
}

function decodeBase64(value: string): Uint8Array {
  const binary = window.atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

export function useStreamingSpeechPlayback() {
  const { language, translate } = useLanguage();
  const translateRef = useRef(translate);
  translateRef.current = translate;
  const playbackText = useCallback<Translate>(
    (source, english) => translateRef.current(source, english),
    [],
  );
  const [status, setStatus] = useState<StreamingSpeechStatus>('idle');
  const [muted, setMuted] = useState(false);
  const [hasReplay, setHasReplay] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const gainRef = useRef<GainNode | null>(null);
  const mutedRef = useRef(false);
  const sourcesRef = useRef(new Set<AudioBufferSourceNode>());
  const scheduledEndRef = useRef(0);
  const pendingAudioSecondsRef = useRef(0);
  const playbackProgressRef = useRef<SpeechPlaybackProgressReporter | null>(null);
  const currentReplyRef = useRef<PcmChunk[]>([]);
  const lastReplyRef = useRef<PcmChunk[]>([]);
  const finishTimerRef = useRef<number | null>(null);
  const scheduleChainRef = useRef<Promise<void>>(Promise.resolve());
  const activeStreamIdRef = useRef<string | null>(null);
  const activeSessionIdRef = useRef<string | null>(null);
  const activeStreamSocketRef = useRef<WebSocket | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const socketSessionIdRef = useRef<string | null>(null);
  const socketLocaleRef = useRef<string | null>(null);
  const socketReadyRef = useRef(false);
  const socketConnectPromiseRef = useRef<Promise<void> | null>(null);
  const socketConnectSessionIdRef = useRef<string | null>(null);
  const socketConnectLocaleRef = useRef<string | null>(null);
  const socketHeartbeatRef = useRef<number | null>(null);
  const pendingBinaryRef = useRef<PendingBinaryChunk | null>(null);
  // `speech_done` is a terminal marker for one stream.  WebSocket binary
  // frames can be delivered through an asynchronous Blob conversion (and a
  // misbehaving server can send a late JSON frame), so an active stream id
  // alone is not enough to reject packets after completion.
  const terminalStreamIdRef = useRef<string | null>(null);
  // A stream may contain several JSON/binary packets.  Keep one validated
  // rate for its entire lifetime; decoding a later packet at a different rate
  // would silently change pitch and speaking speed.
  const streamSampleRateRef = useRef<number | null>(null);
  const playbackEpochRef = useRef<SpeechPlaybackEpoch>(createSpeechPlaybackEpoch());
  const playbackLanguageRef = useRef(language);

  const advancePlaybackEpoch = useCallback((streamId: string | null) => {
    const next = advanceSpeechPlaybackEpoch(playbackEpochRef.current, streamId);
    playbackEpochRef.current = next;
    return next;
  }, []);

  const isPlaybackEpochCurrent = useCallback((candidate: SpeechPlaybackEpoch) => (
    isSpeechPlaybackEpochCurrent(playbackEpochRef.current, candidate)
    && activeStreamIdRef.current === candidate.streamId
  ), []);

  const clearFinishTimer = useCallback(() => {
    if (finishTimerRef.current !== null) {
      window.clearTimeout(finishTimerRef.current);
      finishTimerRef.current = null;
    }
  }, []);

  const clearSocketHeartbeat = useCallback(() => {
    if (socketHeartbeatRef.current !== null) {
      window.clearInterval(socketHeartbeatRef.current);
      socketHeartbeatRef.current = null;
    }
  }, []);

  const clearPlaybackProgress = useCallback(() => {
    playbackProgressRef.current?.stop();
    playbackProgressRef.current = null;
    pendingAudioSecondsRef.current = 0;
  }, []);

  const cancelActiveStream = useCallback(() => {
    clearPlaybackProgress();
    sendSpeechStreamCancellation({
      activeSocket: activeStreamSocketRef.current,
      currentSocket: socketRef.current,
      streamId: activeStreamIdRef.current,
    });
    activeStreamSocketRef.current = null;
  }, [clearPlaybackProgress]);

  const closeSocket = useCallback(() => {
    cancelActiveStream();
    clearSocketHeartbeat();
    pendingBinaryRef.current = null;
    streamSampleRateRef.current = null;
    terminalStreamIdRef.current = null;
    const socket = socketRef.current;
    socketRef.current = null;
    socketSessionIdRef.current = null;
    socketLocaleRef.current = null;
    socketReadyRef.current = false;
    socketConnectPromiseRef.current = null;
    socketConnectSessionIdRef.current = null;
    socketConnectLocaleRef.current = null;
    if (
      socket
      && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)
    ) {
      socket.close(1000, 'Speech playback closed');
    }
  }, [cancelActiveStream, clearSocketHeartbeat]);

  const ensureContext = useCallback(() => {
    if (
      contextRef.current
      && contextRef.current.state !== 'closed'
      && gainRef.current
    ) {
      return { context: contextRef.current, gain: gainRef.current };
    }
    contextRef.current = null;
    gainRef.current = null;
    const AudioContextConstructor = window.AudioContext
      || (window as AudioContextWindow).webkitAudioContext;
    if (!AudioContextConstructor) {
      throw new Error(playbackText(
        '当前浏览器不支持流式语音播放，请使用最新版 Chrome 或 Edge。',
        'This browser does not support streaming speech playback. Use the latest Chrome or Edge.',
      ));
    }
    const context = new AudioContextConstructor({ latencyHint: 'interactive' });
    const gain = context.createGain();
    gain.gain.value = mutedRef.current ? 0 : 1;
    gain.connect(context.destination);
    contextRef.current = context;
    gainRef.current = gain;
    return { context, gain };
  }, [playbackText]);

  const unlock = useCallback(async () => {
    try {
      const { context } = ensureContext();
      if (context.state === 'suspended') await context.resume();
      if (context.state !== 'running') {
        throw new Error(playbackText(
          '浏览器阻止了语音播放，请点击页面后重试。',
          'The browser blocked speech playback. Click the page and try again.',
        ));
      }
      setError(null);
      setStatus((current) => current === 'error' ? 'idle' : current);
      return true;
    } catch (nextError) {
      const message = nextError instanceof Error
        ? playbackText(nextError.message)
        : playbackText('语音播放初始化失败。', 'Speech playback failed to initialize.');
      setError(message);
      setStatus('error');
      return false;
    }
  }, [ensureContext, playbackText]);

  const stopSources = useCallback(() => {
    clearFinishTimer();
    clearPlaybackProgress();
    for (const source of sourcesRef.current) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // A source that has already ended cannot be stopped again.
      }
      source.disconnect();
    }
    sourcesRef.current.clear();
    scheduledEndRef.current = 0;
    scheduleChainRef.current = Promise.resolve();
  }, [clearFinishTimer, clearPlaybackProgress]);

  const scheduleChunk = useCallback(async (
    chunk: PcmChunk,
    playbackEpoch: SpeechPlaybackEpoch,
    startBufferSeconds = SPEECH_PLAYBACK_START_BUFFER_SECONDS,
  ) => {
    if (!isPlaybackEpochCurrent(playbackEpoch)) return;
    const { context, gain } = ensureContext();
    if (!isPlaybackEpochCurrent(playbackEpoch)) return;
    if (context.state === 'suspended') await context.resume();
    if (!isPlaybackEpochCurrent(playbackEpoch)) return;
    if (context.state !== 'running') {
      throw new Error(playbackText(
        '浏览器阻止了语音播放，请点击页面后重试。',
        'The browser blocked speech playback. Click the page and try again.',
      ));
    }
    let audioBuffer = chunk.audioBuffer;
    if (!audioBuffer) {
      if (!chunk.bytes) return;
      const samples = decodePcm16Le(chunk.bytes);
      if (!samples.length) return;
      audioBuffer = context.createBuffer(1, samples.length, chunk.sampleRate);
      audioBuffer.getChannelData(0).set(samples);
      chunk.audioBuffer = audioBuffer;
      chunk.bytes = null;
    }
    if (!isPlaybackEpochCurrent(playbackEpoch)) return;

    const source = context.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(gain);
    sourcesRef.current.add(source);
    source.onended = () => {
      sourcesRef.current.delete(source);
      source.disconnect();
    };

    if (!isPlaybackEpochCurrent(playbackEpoch)) {
      source.onended = null;
      sourcesRef.current.delete(source);
      source.disconnect();
      return;
    }

    const now = context.currentTime;
    const startAt = scheduledEndRef.current > now
      ? scheduledEndRef.current
      : now + startBufferSeconds;
    source.start(startAt);
    scheduledEndRef.current = startAt + audioBuffer.duration;
    setStatus('playing');
  }, [ensureContext, isPlaybackEpochCurrent, playbackText]);

  const trackPendingAudio = useCallback((
    byteLength: number,
    sampleRate: number,
    playbackEpoch: SpeechPlaybackEpoch,
  ) => {
    if (!isPlaybackEpochCurrent(playbackEpoch)) return () => {};
    const pendingSeconds = pcmAudioSeconds(byteLength, sampleRate);
    pendingAudioSecondsRef.current += pendingSeconds;
    playbackProgressRef.current?.notifyAudioReceived();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      // A stale resume()/decode continuation cannot decrement the new stream.
      if (!isPlaybackEpochCurrent(playbackEpoch)) return;
      pendingAudioSecondsRef.current = Math.max(
        0, pendingAudioSecondsRef.current - pendingSeconds,
      );
    };
  }, [isPlaybackEpochCurrent]);

  const resolveStreamSampleRate = useCallback((
    value: unknown,
    optional = false,
  ): number | null => {
    // `speech_start` is informational on older backends and may omit the
    // field; the first audio packet still has to provide a legal rate.  An
    // explicitly present but malformed value is never treated as absent.
    if (
      optional
      && (value === undefined || value === null)
      && streamSampleRateRef.current !== null
    ) {
      return streamSampleRateRef.current;
    }
    if (value === undefined && streamSampleRateRef.current !== null) {
      return streamSampleRateRef.current;
    }
    if (optional && (value === undefined || value === null)) return null;
    const sampleRate = assertSpeechSampleRate(value, streamSampleRateRef.current);
    if (streamSampleRateRef.current === null) {
      streamSampleRateRef.current = sampleRate;
    }
    return sampleRate;
  }, []);

  const enqueueChunk = useCallback((
    bytes: Uint8Array,
    sampleRate: number,
    playbackEpoch: SpeechPlaybackEpoch,
    releaseReceivedAudio?: () => void,
  ) => {
    if (!bytes.byteLength || !isPlaybackEpochCurrent(playbackEpoch)) {
      releaseReceivedAudio?.();
      return;
    }
    const releasePendingAudio = releaseReceivedAudio
      ?? trackPendingAudio(bytes.byteLength, sampleRate, playbackEpoch);
    const chunk: PcmChunk = { bytes, sampleRate, audioBuffer: null };
    currentReplyRef.current.push(chunk);
    scheduleChainRef.current = scheduleChainRef.current
      .then(async () => {
        if (!isPlaybackEpochCurrent(playbackEpoch)) return;
        try {
          await scheduleChunk(chunk, playbackEpoch);
        } finally {
          // Transfer pending audio to scheduled audio only after scheduling
          // completes; failed scheduling also releases its pending count.
          releasePendingAudio();
        }
      })
      .catch((nextError) => {
        if (!isPlaybackEpochCurrent(playbackEpoch)) return;
        const message = nextError instanceof Error
          ? playbackText(nextError.message)
          : playbackText('语音播放失败。', 'Speech playback failed.');
        setError(message);
        setStatus('error');
      });
  }, [isPlaybackEpochCurrent, playbackText, scheduleChunk, trackPendingAudio]);

  const scheduleReadyState = useCallback((playbackEpoch: SpeechPlaybackEpoch) => {
    if (!isPlaybackEpochCurrent(playbackEpoch)) return;
    clearFinishTimer();
    const context = contextRef.current;
    const remainingMs = context
      ? Math.max(0, (scheduledEndRef.current - context.currentTime) * 1000)
      : 0;
    const timerId = window.setTimeout(() => {
      if (finishTimerRef.current === timerId) finishTimerRef.current = null;
      if (!isPlaybackEpochCurrent(playbackEpoch)) return;
      clearPlaybackProgress();
      setStatus(lastReplyRef.current.length ? 'ready' : 'idle');
    }, remainingMs + 30);
    finishTimerRef.current = timerId;
  }, [clearFinishTimer, clearPlaybackProgress, isPlaybackEpochCurrent]);

  const speechSampleRateErrorMessage = useCallback((error: unknown) => {
    if (error instanceof SpeechSampleRateError && error.kind === 'mismatch') {
      return playbackText(
        '语音流的采样率在播放过程中发生变化，本轮语音播放已停止。',
        'The speech sample rate changed during playback, so this turn was stopped.',
      );
    }
    return playbackText(
      '语音流返回了无效的采样率，本轮语音播放已停止。',
      'The speech stream returned an invalid sample rate, so this turn was stopped.',
    );
  }, [playbackText]);

  const abortSpeechStreamForProtocolError = useCallback((message: string) => {
    // Cancel upstream synthesis and invalidate every queued callback. Keep
    // already received chunks available for replay, matching the normal
    // speech_error path, but never schedule more bytes from this stream.
    const abortedStreamId = activeStreamIdRef.current;
    cancelActiveStream();
    advancePlaybackEpoch(null);
    activeStreamIdRef.current = null;
    activeSessionIdRef.current = null;
    activeStreamSocketRef.current = null;
    pendingBinaryRef.current = null;
    streamSampleRateRef.current = null;
    terminalStreamIdRef.current = abortedStreamId;
    if (currentReplyRef.current.length) {
      lastReplyRef.current = currentReplyRef.current;
      currentReplyRef.current = [];
      setHasReplay(true);
    }
    stopSources();
    setError(message);
    setStatus('error');
  }, [advancePlaybackEpoch, cancelActiveStream, stopSources]);

  const handleEvent = useCallback((event: string, data: Record<string, unknown>) => {
    const eventStreamId = typeof data.speech_stream_id === 'string'
      ? data.speech_stream_id
      : '';
    const eventSessionId = typeof data.session_id === 'string' ? data.session_id : '';
    if (
      !activeStreamIdRef.current
      || eventStreamId !== activeStreamIdRef.current
      || (activeSessionIdRef.current && eventSessionId !== activeSessionIdRef.current)
    ) {
      return;
    }
    const playbackEpoch = playbackEpochRef.current;
    if (
      playbackEpoch.streamId !== eventStreamId
      || !isPlaybackEpochCurrent(playbackEpoch)
    ) {
      return;
    }
    // Keep duplicate `speech_done` harmless, but reject every other event
    // after the first terminal marker.  This is especially important for a
    // binary frame whose Blob conversion resolves after the stream ended.
    if (
      terminalStreamIdRef.current === eventStreamId
      && event !== 'speech_done'
    ) {
      return;
    }
    if (event === 'speech_stream_ready') {
      setStatus('buffering');
      return;
    }
    if (event === 'speech_start') {
      try {
        // Older servers may omit the informational rate on speech_start, but
        // an explicitly supplied value must still pass the same stream guard.
        resolveStreamSampleRate(
          data.sample_rate,
          !('sample_rate' in data) || data.sample_rate === null,
        );
      } catch (nextError) {
        abortSpeechStreamForProtocolError(speechSampleRateErrorMessage(nextError));
        return;
      }
      setStatus((current) => current === 'playing' ? current : 'buffering');
      return;
    }
    if (event === 'speech_audio') {
      const encoded = typeof data.audio === 'string' ? data.audio : '';
      if (!encoded) return;
      try {
        const sampleRate = resolveStreamSampleRate(data.sample_rate);
        if (sampleRate === null) throw new Error('Speech sample rate is missing.');
        const bytes = decodeBase64(encoded);
        if (!isValidPcm16Chunk(bytes)) {
          throw new Error(
            playbackText(
              '语音数据不是完整的 PCM 音频，本轮语音播放已停止。',
              'The speech packet is not a complete PCM16 chunk, so this turn was stopped.',
            ),
          );
        }
        enqueueChunk(bytes, sampleRate, playbackEpoch);
      } catch (nextError) {
        if (nextError instanceof SpeechSampleRateError) {
          abortSpeechStreamForProtocolError(speechSampleRateErrorMessage(nextError));
          return;
        }
        const message = nextError instanceof Error
          ? playbackText(nextError.message)
          : playbackText('语音数据解码失败。', 'Speech audio could not be decoded.');
        abortSpeechStreamForProtocolError(message);
      }
      return;
    }
    if (event === 'speech_done') {
      // A terminal marker is idempotent.  Do not let a duplicate marker
      // replace the already captured replay buffer with the now-empty current
      // buffer (which would make the replay button disappear).
      if (terminalStreamIdRef.current === eventStreamId) return;
      clearPlaybackProgress();
      activeStreamSocketRef.current = null;
      terminalStreamIdRef.current = eventStreamId;
      pendingBinaryRef.current = null;
      streamSampleRateRef.current = null;
      lastReplyRef.current = currentReplyRef.current;
      currentReplyRef.current = [];
      setHasReplay(lastReplyRef.current.length > 0);
      void scheduleChainRef.current.then(() => scheduleReadyState(playbackEpoch));
      return;
    }
    if (event === 'speech_error') {
      const message = typeof data.message === 'string'
        ? playbackText(data.message)
        : playbackText(
          '语音服务暂不可用，本轮文字回复不受影响。',
          'The speech service is temporarily unavailable. The text response is unaffected.',
        );
      abortSpeechStreamForProtocolError(message);
    }
  }, [
    abortSpeechStreamForProtocolError,
    clearPlaybackProgress,
    enqueueChunk,
    playbackText,
    resolveStreamSampleRate,
    scheduleReadyState,
    speechSampleRateErrorMessage,
  ]);

  const handleSocketJson = useCallback((data: Record<string, unknown>) => {
    const event = typeof data.event === 'string' ? data.event : '';
    if (!event || event === 'speech_socket_ready' || event === 'speech_pong') return;
    if (event === 'speech_audio' && data.transport === 'binary') {
      const sessionId = typeof data.session_id === 'string' ? data.session_id : '';
      const streamId = typeof data.speech_stream_id === 'string'
        ? data.speech_stream_id
        : '';
      const playbackEpoch = playbackEpochRef.current;
      if (
        !isPlaybackEpochCurrent(playbackEpoch)
        || playbackEpoch.streamId !== streamId
        || (activeSessionIdRef.current && activeSessionIdRef.current !== sessionId)
        || terminalStreamIdRef.current === streamId
      ) {
        return;
      }
      let sampleRate: number;
      try {
        sampleRate = resolveStreamSampleRate(data.sample_rate) as number;
      } catch (nextError) {
        if (nextError instanceof SpeechSampleRateError) {
          abortSpeechStreamForProtocolError(speechSampleRateErrorMessage(nextError));
        } else {
          abortSpeechStreamForProtocolError(
            playbackText(
              '语音数据元信息无效，本轮语音播放已停止。',
              'The speech packet metadata is invalid, so this turn was stopped.',
            ),
          );
        }
        return;
      }
      const byteLength = parseSpeechByteLength(data.byte_length);
      if (byteLength === null) {
        abortSpeechStreamForProtocolError(
          playbackText(
            '语音数据长度无效，本轮语音播放已停止。',
            'The speech packet length is invalid, so this turn was stopped.',
          ),
        );
        return;
      }
      try {
        pendingBinaryRef.current = {
          sessionId,
          streamId,
          sampleRate,
          byteLength,
          playbackEpoch,
        };
      } catch {
        // The object assignment above cannot fail under the typed contract;
        // keep a defensive protocol abort if a future refactor changes it.
        abortSpeechStreamForProtocolError(
          playbackText(
            '语音数据元信息无效，本轮语音播放已停止。',
            'The speech packet metadata is invalid, so this turn was stopped.',
          ),
        );
      }
      return;
    }
    handleEvent(event, data);
  }, [
    abortSpeechStreamForProtocolError,
    handleEvent,
    isPlaybackEpochCurrent,
    playbackText,
    resolveStreamSampleRate,
    speechSampleRateErrorMessage,
  ]);

  const handleSocketBinary = useCallback((
    data: ArrayBuffer | Blob,
    sourceSocket?: WebSocket,
  ) => {
    if (sourceSocket && socketRef.current !== sourceSocket) return;
    const pending = pendingBinaryRef.current;
    pendingBinaryRef.current = null;
    if (!pending) return;
    if (!isPlaybackEpochCurrent(pending.playbackEpoch)) return;
    // Blob conversion can wait asynchronously. Include those received bytes
    // while they are still waiting to become an AudioBuffer.
    const releasePendingAudio = trackPendingAudio(
      data instanceof Blob ? data.size : data.byteLength,
      pending.sampleRate,
      pending.playbackEpoch,
    );

    const deliver = (buffer: ArrayBuffer) => {
      if (
        !isPlaybackEpochCurrent(pending.playbackEpoch)
        || pending.playbackEpoch.streamId !== pending.streamId
        || !activeStreamIdRef.current
        || pending.streamId !== activeStreamIdRef.current
        || terminalStreamIdRef.current === pending.streamId
        || (activeSessionIdRef.current && pending.sessionId !== activeSessionIdRef.current)
      ) {
        releasePendingAudio();
        return;
      }
      const bytes = new Uint8Array(buffer);
      if (
        !isValidPcm16Chunk(bytes)
        || bytes.byteLength !== pending.byteLength
      ) {
        releasePendingAudio();
        abortSpeechStreamForProtocolError(playbackText(
          '语音数据不是完整的 PCM 音频，本轮语音播放已停止。',
          'The speech packet is not a complete PCM16 chunk, so this turn was stopped.',
        ));
        return;
      }
      enqueueChunk(bytes, pending.sampleRate, pending.playbackEpoch, releasePendingAudio);
    };

    if (data instanceof Blob) {
      void data.arrayBuffer()
        .then(deliver)
        .catch(() => {
          releasePendingAudio();
          if (!isPlaybackEpochCurrent(pending.playbackEpoch)) return;
          abortSpeechStreamForProtocolError(playbackText(
            '语音数据读取失败。',
            'The speech data could not be read.',
          ));
        });
      return;
    }
    deliver(data);
  }, [
    abortSpeechStreamForProtocolError,
    enqueueChunk,
    isPlaybackEpochCurrent,
    playbackText,
    trackPendingAudio,
  ]);

  const ensureSocket = useCallback(async (sessionId: string) => {
    const current = socketRef.current;
    if (
      current
      && current.readyState === WebSocket.OPEN
      && socketReadyRef.current
      && socketSessionIdRef.current === sessionId
      && socketLocaleRef.current === language
    ) {
      return;
    }
    if (
      socketConnectPromiseRef.current
      && socketConnectSessionIdRef.current === sessionId
      && socketConnectLocaleRef.current === language
    ) {
      await socketConnectPromiseRef.current;
      return;
    }

    closeSocket();
    const params = new URLSearchParams({
      session_id: sessionId,
      locale: language,
      language,
    });
    const socket = new WebSocket(
      `${getWsApiBase()}/rehearsal/speech/realtime?${params.toString()}`,
    );
    socket.binaryType = 'arraybuffer';
    socketRef.current = socket;
    socketSessionIdRef.current = sessionId;
    socketLocaleRef.current = language;
    socketReadyRef.current = false;
    socketConnectSessionIdRef.current = sessionId;
    socketConnectLocaleRef.current = language;

    const connectionPromise = new Promise<void>((resolve, reject) => {
      let settled = false;
      const timeoutId = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        reject(new Error(playbackText(
          '语音播放通道连接超时，本轮仅显示文字。',
          'The speech-playback connection timed out. This turn will show text only.',
        )));
        if (socketRef.current === socket) closeSocket();
      }, SOCKET_READY_TIMEOUT_MS);

      const resolveReady = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeoutId);
        socketReadyRef.current = true;
        clearSocketHeartbeat();
        socketHeartbeatRef.current = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ type: 'ping' }));
          }
        }, SOCKET_HEARTBEAT_MS);
        resolve();
      };

      const rejectConnection = (message: string) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeoutId);
        reject(new Error(message));
      };

      socket.onmessage = (messageEvent) => {
        if (socketRef.current !== socket) return;
        if (typeof messageEvent.data !== 'string') {
          handleSocketBinary(messageEvent.data as ArrayBuffer | Blob, socket);
          return;
        }
        let payload: Record<string, unknown>;
        try {
          payload = JSON.parse(messageEvent.data) as Record<string, unknown>;
        } catch {
          return;
        }
        const event = typeof payload.event === 'string' ? payload.event : '';
        if (event === 'speech_socket_ready') {
          const eventSessionId = typeof payload.session_id === 'string'
            ? payload.session_id
            : '';
          if (eventSessionId !== sessionId) {
            rejectConnection(playbackText(
              '语音播放会话校验失败。',
              'Speech-playback session validation failed.',
            ));
            closeSocket();
            return;
          }
          resolveReady();
          return;
        }
        handleSocketJson(payload);
      };

      socket.onerror = () => {
        rejectConnection(playbackText(
          '无法建立语音播放通道，本轮仅显示文字。',
          'The speech-playback connection could not be established. This turn will show text only.',
        ));
      };

      socket.onclose = () => {
        const isCurrent = socketRef.current === socket;
        const hadActiveStream = Boolean(activeStreamIdRef.current);
        if (isCurrent) {
          advancePlaybackEpoch(null);
          activeStreamIdRef.current = null;
          activeSessionIdRef.current = null;
          activeStreamSocketRef.current = null;
          stopSources();
          clearSocketHeartbeat();
          pendingBinaryRef.current = null;
          streamSampleRateRef.current = null;
          socketRef.current = null;
          socketSessionIdRef.current = null;
          socketLocaleRef.current = null;
          socketReadyRef.current = false;
          socketConnectPromiseRef.current = null;
          socketConnectSessionIdRef.current = null;
          socketConnectLocaleRef.current = null;
        }
        rejectConnection(playbackText(
          '语音播放通道已断开，本轮仅显示文字。',
          'The speech-playback connection closed. This turn will show text only.',
        ));
        if (isCurrent && hadActiveStream) {
          setError(playbackText(
            '语音播放通道已断开，本轮文字回复不受影响。',
            'The speech-playback connection closed. The text response is unaffected.',
          ));
          setStatus('error');
        } else if (isCurrent) {
          setStatus(lastReplyRef.current.length ? 'ready' : 'idle');
        }
      };
    });

    socketConnectPromiseRef.current = connectionPromise;
    try {
      await connectionPromise;
    } finally {
      if (socketConnectPromiseRef.current === connectionPromise) {
        socketConnectPromiseRef.current = null;
        socketConnectSessionIdRef.current = null;
        socketConnectLocaleRef.current = null;
      }
    }
  }, [
    advancePlaybackEpoch,
    clearSocketHeartbeat,
    closeSocket,
    handleSocketBinary,
    handleSocketJson,
    language,
    playbackText,
    stopSources,
  ]);

  const preconnect = useCallback(async (sessionId?: string | null) => {
    const resolvedSessionId = sessionId?.trim() || '';
    if (!resolvedSessionId) return false;
    try {
      await ensureSocket(resolvedSessionId);
      return true;
    } catch {
      return false;
    }
  }, [ensureSocket]);

  const prepare = useCallback(async (sessionId?: string | null) => {
    cancelActiveStream();
    stopSources();
    currentReplyRef.current = [];
    streamSampleRateRef.current = null;
    const streamId = createSpeechStreamId();
    const resolvedSessionId = sessionId?.trim() || '';
    const playbackEpoch = advancePlaybackEpoch(streamId);
    activeStreamIdRef.current = streamId;
    activeSessionIdRef.current = resolvedSessionId || null;
    terminalStreamIdRef.current = null;
    setError(null);
    setStatus('buffering');
    try {
      if (!resolvedSessionId) {
        throw new Error(playbackText(
          '当前会话尚未准备好，无法启用语音播放。',
          'The current session is not ready for speech playback.',
        ));
      }
      const { context } = ensureContext();
      await Promise.all([
        context.state === 'suspended' ? context.resume() : Promise.resolve(),
        ensureSocket(resolvedSessionId),
      ]);
      if (!isPlaybackEpochCurrent(playbackEpoch)) return null;
      // Only a prepared stream can have upstream synthesis to cancel. Binding
      // after connection setup keeps closing an old socket from cancelling this
      // new stream while prepare() is still awaiting the connection.
      activeStreamSocketRef.current = socketRef.current;
      const socket = activeStreamSocketRef.current;
      if (socket) {
        const reporter = createSpeechPlaybackProgressReporter({
          streamId,
          socket,
          getCurrentSocket: () => socketRef.current,
          isCurrent: () => isPlaybackEpochCurrent(playbackEpoch)
            && activeStreamSocketRef.current === socket,
          getBufferedSeconds: () => bufferedSpeechSeconds(
            pendingAudioSecondsRef.current,
            scheduledEndRef.current,
            contextRef.current?.currentTime ?? 0,
          ),
          now: () => window.performance.now(),
          setTimer: (callback, delayMs) => window.setTimeout(callback, delayMs),
          clearTimer: (timerId) => window.clearTimeout(timerId),
        });
        playbackProgressRef.current = reporter;
        reporter.start();
      }
      return streamId;
    } catch (nextError) {
      if (!isPlaybackEpochCurrent(playbackEpoch)) return null;
      advancePlaybackEpoch(null);
      activeStreamIdRef.current = null;
      activeSessionIdRef.current = null;
      streamSampleRateRef.current = null;
      closeSocket();
      const message = nextError instanceof Error
        ? playbackText(nextError.message)
        : playbackText('语音播放初始化失败。', 'Speech playback failed to initialize.');
      setError(message);
      setStatus('error');
      return null;
    }
  }, [
    advancePlaybackEpoch,
    cancelActiveStream,
    closeSocket,
    ensureContext,
    ensureSocket,
    isPlaybackEpochCurrent,
    playbackText,
    stopSources,
  ]);

  const stop = useCallback(() => {
    cancelActiveStream();
    advancePlaybackEpoch(null);
    activeStreamIdRef.current = null;
    activeSessionIdRef.current = null;
    terminalStreamIdRef.current = null;
    pendingBinaryRef.current = null;
    streamSampleRateRef.current = null;
    stopSources();
    setStatus(lastReplyRef.current.length ? 'ready' : 'idle');
  }, [advancePlaybackEpoch, cancelActiveStream, stopSources]);

  const replay = useCallback(async () => {
    if (!lastReplyRef.current.length) return;
    cancelActiveStream();
    const playbackEpoch = advancePlaybackEpoch(null);
    activeStreamIdRef.current = null;
    activeSessionIdRef.current = null;
    terminalStreamIdRef.current = null;
    pendingBinaryRef.current = null;
    streamSampleRateRef.current = null;
    stopSources();
    setError(null);
    setStatus('buffering');
    try {
      const { context } = ensureContext();
      if (context.state === 'suspended') await context.resume();
      for (const chunk of lastReplyRef.current) {
        if (!isPlaybackEpochCurrent(playbackEpoch)) return;
        await scheduleChunk(chunk, playbackEpoch, SPEECH_REPLAY_START_BUFFER_SECONDS);
      }
      if (!isPlaybackEpochCurrent(playbackEpoch)) return;
      scheduleReadyState(playbackEpoch);
    } catch (nextError) {
      if (!isPlaybackEpochCurrent(playbackEpoch)) return;
      const message = nextError instanceof Error
        ? playbackText(nextError.message)
        : playbackText('语音重播失败。', 'Speech replay failed.');
      setError(message);
      setStatus('error');
    }
  }, [
    advancePlaybackEpoch,
    cancelActiveStream,
    ensureContext,
    isPlaybackEpochCurrent,
    playbackText,
    scheduleChunk,
    scheduleReadyState,
    stopSources,
  ]);

  const toggleMuted = useCallback(() => {
    setMuted((current) => {
      const next = !current;
      mutedRef.current = next;
      const gain = gainRef.current;
      const context = contextRef.current;
      if (gain && context) {
        gain.gain.setValueAtTime(next ? 0 : 1, context.currentTime);
      }
      return next;
    });
  }, []);

  useEffect(() => {
    if (playbackLanguageRef.current === language) return;
    playbackLanguageRef.current = language;

    // Audio and replay data belong to the language in which they were created.
    // A language switch invalidates every pending callback before closing the socket.
    cancelActiveStream();
    advancePlaybackEpoch(null);
    activeStreamIdRef.current = null;
    activeSessionIdRef.current = null;
    terminalStreamIdRef.current = null;
    pendingBinaryRef.current = null;
    streamSampleRateRef.current = null;
    currentReplyRef.current = [];
    lastReplyRef.current = [];
    stopSources();
    closeSocket();
    setHasReplay(false);
    setError(null);
    setStatus('idle');
  }, [advancePlaybackEpoch, cancelActiveStream, closeSocket, language, stopSources]);

  useEffect(() => () => {
    cancelActiveStream();
    advancePlaybackEpoch(null);
    activeStreamIdRef.current = null;
    activeSessionIdRef.current = null;
    terminalStreamIdRef.current = null;
    streamSampleRateRef.current = null;
    stopSources();
    closeSocket();
    const context = contextRef.current;
    contextRef.current = null;
    gainRef.current = null;
    if (context && context.state !== 'closed') void context.close();
  }, [advancePlaybackEpoch, cancelActiveStream, closeSocket, stopSources]);

  return {
    status,
    muted,
    hasReplay,
    error,
    unlock,
    prepare,
    preconnect,
    handleEvent,
    toggleMuted,
    stop,
    replay,
  };
}

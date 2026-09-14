import { useCallback, useEffect, useRef, useState } from 'react';
import type { AsrReadinessResponse, AsrTranscribeResponse } from '../types/domain';
import { api } from '../api/client';
import pcm16ProcessorUrl from '../audio-worklets/pcm16-processor.js?url&no-inline';
import { getWsApiBase } from '../utils/format';
import { userFacingErrorMessage } from '../utils/displayText';
import { useLanguage } from '../i18n/LanguageContext';
import {
  normalizeSpeechRecordingLocale,
  speechRecognitionLanguage,
  shouldCancelRecordingForLanguageChange,
} from '../utils/speechRecordingLocale';

export type SpeechToTextStatus = 'idle' | 'requesting' | 'recording' | 'transcribing' | 'error';
export type RealtimeSpeechStatus = 'idle' | 'connecting' | 'connected' | 'fallback';

type UseSpeechToTextOptions = {
  sessionId?: string | null;
  language?: string;
  beforeStart?: () => Promise<void>;
  onPartialTranscript?: (text: string, response: AsrTranscribeResponse) => void;
  onTranscript?: (text: string, response: AsrTranscribeResponse) => void;
  onError?: (message: string, fatal: boolean) => void;
};

export type StartSpeechRecordingOptions = {
  /** Capture PCM locally first; call activateRecording() to open realtime ASR. */
  bufferedStart?: boolean;
};

type AsrServerEvent = {
  type?: 'recording_ready' | 'capture_stopped' | 'status' | 'partial' | 'preview_unavailable' | 'finalizing' | 'final' | 'error';
  code?: string;
  message?: string;
  provider?: string;
  text?: string;
  preview?: string;
  transcript?: string;
  recording_id?: string;
  session_id?: string;
  recoverable?: boolean;
  completed_segments?: number;
  total_segments?: number | null;
  duration_seconds?: number | null;
  stable_text?: string;
  unstable_text?: string;
  revision?: number;
  segment_id?: number;
  correction_state?: 'live' | 'finalized';
  window_seconds?: number;
};

type AudioContextWindow = Window & {
  webkitAudioContext?: typeof AudioContext;
};

type BrowserSpeechRecognitionAlternative = {
  transcript: string;
  confidence: number;
};

type BrowserSpeechRecognitionResult = {
  readonly isFinal: boolean;
  readonly length: number;
  readonly [index: number]: BrowserSpeechRecognitionAlternative;
};

type BrowserSpeechRecognitionResultList = {
  readonly length: number;
  readonly [index: number]: BrowserSpeechRecognitionResult;
};

type BrowserSpeechRecognitionEvent = Event & {
  readonly resultIndex: number;
  readonly results: BrowserSpeechRecognitionResultList;
};

type BrowserSpeechRecognitionErrorEvent = Event & {
  readonly error: string;
  readonly message?: string;
};

type BrowserSpeechRecognition = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onstart: (() => void) | null;
  onresult: ((event: BrowserSpeechRecognitionEvent) => void) | null;
  onerror: ((event: BrowserSpeechRecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
};

type BrowserSpeechRecognitionConstructor = new () => BrowserSpeechRecognition;

type SpeechRecognitionWindow = Window & {
  SpeechRecognition?: BrowserSpeechRecognitionConstructor;
  webkitSpeechRecognition?: BrowserSpeechRecognitionConstructor;
};

type SpeechRecognitionSegment = {
  text: string;
  final: boolean;
};

type AsrProviderMode = 'local' | 'bosch' | 'browser';

type FinalizingProgress = {
  completed: number;
  total: number | null;
};

type Translate = (source: string, english?: string) => string;

const PCM_SAMPLE_RATE = 16_000;
const PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * 2;
const CAPTURE_READY_TIMEOUT_MS = 20_000;
const STARTUP_PCM_MAX_BYTES = Math.ceil(
  PCM_BYTES_PER_SECOND * (CAPTURE_READY_TIMEOUT_MS / 1_000),
);
const WORKLET_FLUSH_TIMEOUT_MS = 150;

function audioWorkletModuleUrl() {
  return pcm16ProcessorUrl;
}

function secureContextMessage(translate: Translate) {
  if (window.isSecureContext || ['localhost', '127.0.0.1'].includes(window.location.hostname)) {
    return '';
  }
  return translate(
    '浏览器只允许在 HTTPS 或 localhost 页面使用麦克风。',
    'Microphone access is available only on HTTPS or localhost pages.',
  );
}

function microphoneErrorMessage(error: unknown, translate: Translate) {
  const name = error instanceof DOMException ? error.name : '';
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return translate(
      '浏览器没有获得麦克风权限。请在地址栏左侧的网站权限中允许麦克风，然后刷新页面。',
      'Microphone permission was not granted. Allow it in the site permissions beside the address bar, then reload the page.',
    );
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return translate(
      '没有检测到可用麦克风。请连接或启用麦克风，并在系统声音设置中选择输入设备。',
      'No microphone was detected. Connect or enable one and select it in the system sound settings.',
    );
  }
  if (name === 'NotReadableError' || name === 'TrackStartError' || name === 'AbortError') {
    return translate(
      '麦克风无法打开，可能正被其他应用占用。请关闭占用麦克风的应用后重试。',
      'The microphone could not be opened and may be in use by another application. Close that application and try again.',
    );
  }
  if (name === 'OverconstrainedError' || name === 'ConstraintNotSatisfiedError') {
    return translate(
      '当前麦克风不支持所需录音参数，请切换输入设备后重试。',
      'This microphone does not support the required recording settings. Switch input devices and try again.',
    );
  }
  return error instanceof Error && error.message
    ? `${translate('语音输入启动失败：', 'Speech input failed to start: ')}${error.message}`
    : translate(
      '语音输入启动失败，请检查浏览器麦克风权限和输入设备。',
      'Speech input failed to start. Check browser microphone permissions and the input device.',
    );
}

async function requestMicrophoneStream() {
  const supported = navigator.mediaDevices.getSupportedConstraints?.() ?? {};
  const audioConstraints: MediaTrackConstraints = {
    channelCount: { ideal: 1 },
  };
  if (supported.noiseSuppression) {
    audioConstraints.noiseSuppression = { ideal: true };
  }
  if (supported.echoCancellation) {
    audioConstraints.echoCancellation = { ideal: true };
  }
  if (supported.autoGainControl) {
    audioConstraints.autoGainControl = { ideal: true };
  }

  try {
    return await navigator.mediaDevices.getUserMedia({
      audio: audioConstraints,
      video: false,
    });
  } catch (error) {
    const name = error instanceof DOMException ? error.name : '';
    if (!['OverconstrainedError', 'ConstraintNotSatisfiedError'].includes(name)) {
      throw error;
    }
    return await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  }
}

function asrResponse(
  text: string,
  provider: string,
  durationSeconds: number | null = null,
): AsrTranscribeResponse {
  return {
    text,
    provider,
    audio_emotion: null,
    duration_seconds: durationSeconds,
  };
}

function browserSpeechRecognitionConstructor() {
  const speechWindow = window as SpeechRecognitionWindow;
  return speechWindow.SpeechRecognition || speechWindow.webkitSpeechRecognition;
}

function joinSpeechText(parts: string[]) {
  return parts.reduce((combined, rawPart) => {
    const part = rawPart.trim();
    if (!part) return combined;
    if (!combined) return part;
    const needsSpace = (
      combined.slice(-1).match(/[A-Za-z0-9]/)
      && part[0]?.match(/[A-Za-z0-9]/)
    );
    return `${combined}${needsSpace ? ' ' : ''}${part}`;
  }, '');
}

export function useSpeechToText(options: UseSpeechToTextOptions = {}) {
  const { language: appLanguage, translate } = useLanguage();
  const translateRef = useRef(translate);
  translateRef.current = translate;
  const speechText = useCallback<Translate>(
    (source, english) => translateRef.current(source, english),
    [],
  );
  const requestedLanguage = options.language?.trim();
  // Existing workflow callers used "zh" as a fixed placeholder. Treat that
  // legacy value as "follow the application language" while preserving real
  // caller overrides such as en-US, de-DE, ja-JP, or zh-CN.
  const speechLanguage = requestedLanguage === 'zh'
    ? speechRecognitionLanguage(appLanguage)
    : requestedLanguage || speechRecognitionLanguage(appLanguage);
  const [status, setStatus] = useState<SpeechToTextStatus>('idle');
  const [realtimeStatus, setRealtimeStatus] = useState<RealtimeSpeechStatus>('idle');
  const [error, setError] = useState<string | null>(null);
  const [finalizingProgress, setFinalizingProgress] = useState<FinalizingProgress | null>(null);
  const [readiness, setReadiness] = useState<AsrReadinessResponse>(() => ({
    status: 'warming',
    model: 'Qwen/Qwen3-ASR-1.7B',
    active_preview_sessions: 0,
    max_preview_sessions: 0,
    max_inference_batch_size: 11,
    admission_mode: 'unbounded',
    detail: speechText('实时语音准备中。', 'Preparing real-time speech input.'),
  }));

  const optionsRef = useRef({ ...options, language: speechLanguage });
  const mountedRef = useRef(true);
  const generationRef = useRef(0);
  const recordingActiveRef = useRef(false);
  const captureActiveRef = useRef(false);
  const captureDrainingRef = useRef(false);
  const startingRef = useRef(false);
  const startCancelledRef = useRef(false);
  const stopRequestedRef = useRef(false);
  const stoppingRef = useRef(false);
  const earlyCaptureStopPromiseRef = useRef<Promise<void> | null>(null);
  const bufferedStartRef = useRef(false);
  const bufferedActivationPromiseRef = useRef<Promise<void> | null>(null);
  const resolveBufferedActivationRef = useRef<(() => void) | null>(null);

  const streamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const audioWorkletReadyRef = useRef<Promise<void> | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const silentGainRef = useRef<GainNode | null>(null);
  const flushResolveRef = useRef<(() => void) | null>(null);
  const startupPcmRef = useRef<ArrayBuffer[]>([]);
  const startupPcmBytesRef = useRef(0);

  const websocketRef = useRef<WebSocket | null>(null);
  const cancelRealtimeReadyRef = useRef<(() => void) | null>(null);
  const captureReadyRef = useRef(false);
  const providerRef = useRef('Qwen/Qwen3-ASR-1.7B');
  const recordingIdRef = useRef<string | null>(null);
  const recordingLocaleRef = useRef<string | null>(null);
  const transcriptRef = useRef('');
  const finalEmittedRef = useRef(false);
  const terminalFailureRef = useRef(false);
  const finalPromiseRef = useRef<Promise<string> | null>(null);
  const resolveFinalRef = useRef<((text: string) => void) | null>(null);
  const pendingPartialRef = useRef('');
  const pendingPartialAllowsEmptyRef = useRef(false);
  const partialFrameRef = useRef<number | null>(null);
  const partialsAcceptedRef = useRef(false);
  const captureStartedAtRef = useRef(0);
  const lastRevisionRef = useRef(-1);
  const readinessRef = useRef(readiness);
  const activeProviderRef = useRef<AsrProviderMode>('local');
  const browserRecognitionRef = useRef<BrowserSpeechRecognition | null>(null);
  const cancelBrowserInitialStartRef = useRef<(() => void) | null>(null);
  const browserResultsRef = useRef<SpeechRecognitionSegment[]>([]);
  const browserCommittedRef = useRef('');
  const browserRestartTimerRef = useRef<number | null>(null);
  const browserStartedAtRef = useRef(0);

  useEffect(() => {
    optionsRef.current = { ...options, language: speechLanguage };
  }, [options, speechLanguage]);

  useEffect(() => {
    const controller = new AbortController();
    void fetch(audioWorkletModuleUrl(), {
      cache: 'force-cache',
      signal: controller.signal,
    }).catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    const poll = async () => {
      try {
        const next = await api.asrReadiness();
        if (cancelled) return;
        setReadiness(next);
        readinessRef.current = next;
        timer = window.setTimeout(poll, next.status === 'ready' ? 10_000 : 2_000);
      } catch {
        if (cancelled) return;
        setReadiness((current) => ({
          ...current,
          status: 'degraded',
          detail: speechText(
            '实时语音状态暂不可用，仍可录音并在停止后完成转写。',
            'Real-time speech status is temporarily unavailable. You can still record and transcribe after stopping.',
          ),
        }));
        timer = window.setTimeout(poll, 5_000);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [speechText]);

  const resolveFinal = useCallback((text = '') => {
    const resolve = resolveFinalRef.current;
    resolveFinalRef.current = null;
    resolve?.(text);
  }, []);

  const activateRecording = useCallback(() => {
    if (!bufferedStartRef.current) return false;
    const resolve = resolveBufferedActivationRef.current;
    if (!resolve) return false;
    resolveBufferedActivationRef.current = null;
    resolve();
    return true;
  }, []);

  const resetBufferedStart = useCallback(() => {
    const resolve = resolveBufferedActivationRef.current;
    resolveBufferedActivationRef.current = null;
    bufferedStartRef.current = false;
    bufferedActivationPromiseRef.current = null;
    resolve?.();
  }, []);

  const resetStartupPcm = useCallback(() => {
    startupPcmRef.current = [];
    startupPcmBytesRef.current = 0;
  }, []);

  const cleanupAudio = useCallback((stopTracks = true, closeContext = false) => {
    flushResolveRef.current?.();
    flushResolveRef.current = null;
    if (workletRef.current) workletRef.current.port.onmessage = null;
    workletRef.current?.disconnect();
    sourceRef.current?.disconnect();
    silentGainRef.current?.disconnect();
    workletRef.current = null;
    sourceRef.current = null;
    silentGainRef.current = null;
    captureDrainingRef.current = false;
    resetStartupPcm();

    const context = audioContextRef.current;
    if (closeContext) {
      audioContextRef.current = null;
      audioWorkletReadyRef.current = null;
      if (context && context.state !== 'closed') void context.close();
    } else if (context?.state === 'running') {
      void context.suspend();
    }

    if (stopTracks) {
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
  }, [resetStartupPcm]);

  const closeRealtime = useCallback(() => {
    const cancelReady = cancelRealtimeReadyRef.current;
    cancelRealtimeReadyRef.current = null;
    cancelReady?.();
    const websocket = websocketRef.current;
    websocketRef.current = null;
    captureReadyRef.current = false;
    recordingIdRef.current = null;
    if (!websocket) return;
    websocket.onopen = null;
    websocket.onmessage = null;
    websocket.onerror = null;
    websocket.onclose = null;
    if (
      websocket.readyState === WebSocket.OPEN
      || websocket.readyState === WebSocket.CONNECTING
    ) {
      websocket.close();
    }
  }, []);

  const reportError = useCallback((message: string, fatal = false) => {
    if (!mountedRef.current) return;
    setError(message);
    if (fatal) setStatus('error');
    optionsRef.current.onError?.(message, fatal);
  }, []);

  const cancelPendingPartial = useCallback(() => {
    partialsAcceptedRef.current = false;
    pendingPartialRef.current = '';
    pendingPartialAllowsEmptyRef.current = false;
    if (partialFrameRef.current !== null) {
      window.cancelAnimationFrame(partialFrameRef.current);
      partialFrameRef.current = null;
    }
  }, []);

  const emitPartial = useCallback((text: string, allowEmpty = false) => {
    const normalized = text.trim();
    if (
      (!normalized && !allowEmpty)
      || !mountedRef.current
      || !partialsAcceptedRef.current
      || !recordingActiveRef.current
      || stoppingRef.current
      || finalEmittedRef.current
    ) return;
    if (normalized === transcriptRef.current) return;
    transcriptRef.current = normalized;
    optionsRef.current.onPartialTranscript?.(
      normalized,
      asrResponse(normalized, providerRef.current),
    );
  }, []);

  const schedulePartial = useCallback((text: string, allowEmpty = false) => {
    const normalized = text.trim();
    if (
      (!normalized && !allowEmpty)
      || !mountedRef.current
      || !partialsAcceptedRef.current
      || !recordingActiveRef.current
      || stoppingRef.current
      || finalEmittedRef.current
    ) return;
    pendingPartialRef.current = normalized;
    pendingPartialAllowsEmptyRef.current = allowEmpty;
    if (partialFrameRef.current !== null) return;
    partialFrameRef.current = window.requestAnimationFrame(() => {
      partialFrameRef.current = null;
      const latest = pendingPartialRef.current;
      const latestAllowsEmpty = pendingPartialAllowsEmptyRef.current;
      pendingPartialRef.current = '';
      pendingPartialAllowsEmptyRef.current = false;
      if (latest || latestAllowsEmpty) emitPartial(latest, latestAllowsEmpty);
    });
  }, [emitPartial]);

  const emitFinal = useCallback((
    text: string,
    provider: string,
    durationSeconds: number | null,
  ) => {
    cancelPendingPartial();
    const normalized = text.trim();
    if (!normalized) {
      resolveFinal('');
      return;
    }
    transcriptRef.current = normalized;
    try {
      if (!finalEmittedRef.current && mountedRef.current) {
        finalEmittedRef.current = true;
        optionsRef.current.onTranscript?.(
          normalized,
          asrResponse(normalized, provider, durationSeconds),
        );
      }
    } finally {
      resolveFinal(normalized);
    }
  }, [cancelPendingPartial, resolveFinal]);


  const browserSnapshot = useCallback((includeInterim = true) => {
    const finalParts = browserResultsRef.current
      .filter((segment) => segment.final)
      .map((segment) => segment.text);
    const interimParts = includeInterim
      ? browserResultsRef.current
        .filter((segment) => !segment.final)
        .map((segment) => segment.text)
      : [];
    return joinSpeechText([
      browserCommittedRef.current,
      ...finalParts,
      ...interimParts,
    ]);
  }, []);

  const disposeBrowserRecognition = useCallback((abort = true) => {
    if (browserRestartTimerRef.current !== null) {
      window.clearTimeout(browserRestartTimerRef.current);
      browserRestartTimerRef.current = null;
    }
    const cancelInitialStart = cancelBrowserInitialStartRef.current;
    cancelBrowserInitialStartRef.current = null;
    cancelInitialStart?.();
    const recognition = browserRecognitionRef.current;
    browserRecognitionRef.current = null;
    if (!recognition) return;
    recognition.onstart = null;
    recognition.onresult = null;
    recognition.onerror = null;
    recognition.onend = null;
    if (abort) {
      try {
        recognition.abort();
      } catch {
        // The browser may already have closed this recognition session.
      }
    }
  }, []);

  const startBrowserRecognition = useCallback((generation: number) => new Promise<void>((resolve, reject) => {
    const Recognition = browserSpeechRecognitionConstructor();
    if (!Recognition) {
      reject(new Error(speechText(
        '当前浏览器不支持内置语音识别，请使用最新版 Chrome 或 Edge。',
        'This browser does not support built-in speech recognition. Use the latest Chrome or Edge.',
      )));
      return;
    }

    providerRef.current = 'browser-web-speech';
    activeProviderRef.current = 'browser';
    browserCommittedRef.current = '';
    browserResultsRef.current = [];
    browserStartedAtRef.current = performance.now();
    let initialStartSettled = false;
    let initialStartTimer: number | null = null;
    let cancelInitialStart: (() => void) | null = null;

    const settleInitialStart = (failure?: Error) => {
      if (initialStartSettled) return;
      initialStartSettled = true;
      if (initialStartTimer !== null) {
        window.clearTimeout(initialStartTimer);
        initialStartTimer = null;
      }
      if (cancelBrowserInitialStartRef.current === cancelInitialStart) {
        cancelBrowserInitialStartRef.current = null;
      }
      if (failure) reject(failure);
      else resolve();
    };
    cancelInitialStart = () => settleInitialStart(new Error(speechText(
      '语音录音启动已取消。',
      'Speech recording was cancelled.',
    )));
    cancelBrowserInitialStartRef.current = cancelInitialStart;

    const failRecognition = (message: string) => {
      settleInitialStart(new Error(message));
      terminalFailureRef.current = true;
      recordingActiveRef.current = false;
      captureActiveRef.current = false;
      recordingLocaleRef.current = null;
      partialsAcceptedRef.current = false;
      disposeBrowserRecognition(false);
      reportError(message, true);
      resolveFinal('');
    };

    const startSession = () => {
      if (
        !mountedRef.current
        || generation !== generationRef.current
        || stoppingRef.current
        || !recordingActiveRef.current
      ) return;

      const recognition = new Recognition();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = optionsRef.current.language?.trim() || 'zh-CN';
      browserResultsRef.current = [];
      browserRecognitionRef.current = recognition;

      recognition.onstart = () => {
        if (generation !== generationRef.current) return;
        captureActiveRef.current = true;
        setRealtimeStatus('connected');
        setStatus('recording');
        setError(null);
        settleInitialStart();
      };

      recognition.onresult = (event) => {
        if (
          generation !== generationRef.current
          || (!recordingActiveRef.current && !stoppingRef.current)
        ) return;
        for (let index = event.resultIndex; index < event.results.length; index += 1) {
          const result = event.results[index];
          browserResultsRef.current[index] = {
            text: result?.[0]?.transcript || '',
            final: Boolean(result?.isFinal),
          };
        }
        const nextText = browserSnapshot(true);
        transcriptRef.current = nextText;
        if (!stoppingRef.current) {
          schedulePartial(nextText, true);
        }
      };

      recognition.onerror = (event) => {
        if (generation !== generationRef.current) return;
        if (event.error === 'no-speech') return;
        if (event.error === 'aborted' && stoppingRef.current) return;
        const message = speechText(
          event.error === 'not-allowed' || event.error === 'service-not-allowed'
            ? '浏览器没有获得语音识别权限，请检查麦克风和网站权限。'
            : event.error === 'audio-capture'
              ? '浏览器无法读取麦克风，请检查输入设备。'
              : event.error === 'language-not-supported'
                ? '浏览器内置语音识别不支持当前语言。'
                : '浏览器内置语音识别暂不可用，请稍后重试。',
          event.error === 'not-allowed' || event.error === 'service-not-allowed'
            ? 'Browser speech-recognition permission was not granted. Check microphone and site permissions.'
            : event.error === 'audio-capture'
              ? 'The browser cannot read the microphone. Check the input device.'
              : event.error === 'language-not-supported'
                ? 'Built-in browser speech recognition does not support the selected language.'
                : 'Built-in browser speech recognition is temporarily unavailable. Please try again later.',
        );
        failRecognition(message);
      };

      recognition.onend = () => {
        if (browserRecognitionRef.current === recognition) {
          browserRecognitionRef.current = null;
        }
        if (generation !== generationRef.current) return;
        const snapshot = browserSnapshot(true);
        if (stoppingRef.current || !recordingActiveRef.current) {
          const durationSeconds = browserStartedAtRef.current > 0
            ? Math.max(0, (performance.now() - browserStartedAtRef.current) / 1000)
            : null;
          if (snapshot) {
            emitFinal(snapshot, 'browser-web-speech', durationSeconds);
          } else {
            resolveFinal('');
          }
          return;
        }

        browserCommittedRef.current = snapshot;
        browserResultsRef.current = [];
        browserRestartTimerRef.current = window.setTimeout(() => {
          browserRestartTimerRef.current = null;
          try {
            startSession();
          } catch {
            failRecognition(speechText(
              '浏览器内置语音识别无法继续，请重新录音。',
              'Built-in browser speech recognition cannot continue. Start a new recording.',
            ));
          }
        }, 80);
      };

      try {
        recognition.start();
      } catch {
        failRecognition(speechText(
          '浏览器内置语音识别启动失败，请重新录音。',
          'Built-in browser speech recognition failed to start. Start a new recording.',
        ));
      }
    };

    initialStartTimer = window.setTimeout(() => {
      if (
        !mountedRef.current
        || generation !== generationRef.current
        || startCancelledRef.current
        || !recordingActiveRef.current
      ) {
        settleInitialStart(new Error(speechText(
          '语音录音启动已取消。',
          'Speech recording was cancelled.',
        )));
        return;
      }
      failRecognition(speechText(
        '浏览器内置语音识别启动超时，请重新录音。',
        'Built-in browser speech recognition timed out while starting. Start a new recording.',
      ));
    }, CAPTURE_READY_TIMEOUT_MS);
    startSession();
  }), [
    browserSnapshot,
    disposeBrowserRecognition,
    emitFinal,
    reportError,
    resolveFinal,
    schedulePartial,
    speechText,
  ]);
  const openRealtime = useCallback((generation: number) => new Promise<void>((resolve, reject) => {
    setRealtimeStatus('connecting');
    const sessionId = optionsRef.current.sessionId?.trim() || '';
    if (!sessionId) {
      reject(new Error(speechText(
        '当前业务会话尚未就绪，无法开始语音输入。',
        'The current session is not ready for speech input.',
      )));
      return;
    }
    const activeSpeechLanguage = optionsRef.current.language?.trim() || appLanguage;
    const query = new URLSearchParams({
      client_started_at_ms: String(captureStartedAtRef.current || Date.now()),
      session_id: sessionId,
      locale: normalizeSpeechRecordingLocale(activeSpeechLanguage),
      language: activeSpeechLanguage,
    });
    const websocket = new WebSocket(`${getWsApiBase()}/asr/realtime?${query.toString()}`);
    websocket.binaryType = 'arraybuffer';
    websocketRef.current = websocket;
    let readySettled = false;
    const readyTimeout = window.setTimeout(() => {
      if (readySettled) return;
      readySettled = true;
      reject(new Error(speechText(
        '语音录音服务连接超时，请稍后重试。',
        'The speech-recording service connection timed out. Please try again later.',
      )));
      websocket.close();
    }, CAPTURE_READY_TIMEOUT_MS);

    let cancelReady: (() => void) | null = null;
    const settleReady = (failure?: Error) => {
      if (readySettled) return;
      readySettled = true;
      window.clearTimeout(readyTimeout);
      if (cancelRealtimeReadyRef.current === cancelReady) {
        cancelRealtimeReadyRef.current = null;
      }
      if (failure) reject(failure);
      else resolve();
    };
    cancelReady = () => settleReady(new Error(speechText(
      '语音录音启动已取消。',
      'Speech recording was cancelled.',
    )));
    cancelRealtimeReadyRef.current = cancelReady;

    websocket.onmessage = (message) => {
      if (generation !== generationRef.current) return;
      let event: AsrServerEvent;
      try {
        event = JSON.parse(String(message.data)) as AsrServerEvent;
      } catch {
        return;
      }
      const expectedSessionId = optionsRef.current.sessionId?.trim() || '';
      if (event.session_id && event.session_id !== expectedSessionId) return;
      if (
        recordingIdRef.current
        && event.recording_id
        && event.recording_id !== recordingIdRef.current
      ) return;

      if (event.type === 'recording_ready') {
        recordingIdRef.current = event.recording_id || null;
        lastRevisionRef.current = -1;
        captureReadyRef.current = true;
        providerRef.current = event.provider || providerRef.current;
        const pending = startupPcmRef.current;
        resetStartupPcm();
        if (websocket.readyState === WebSocket.OPEN) {
          pending.forEach((pcm) => websocket.send(pcm));
        }
        settleReady();
        return;
      }
      if (event.type === 'status' && event.code === 'ready') {
        providerRef.current = event.provider || providerRef.current;
        setRealtimeStatus('connected');
        setError(null);
        return;
      }
      if (event.type === 'partial') {
        if (typeof event.revision === 'number') {
          if (event.revision < lastRevisionRef.current) return;
          lastRevisionRef.current = event.revision;
        }
        const segmentedText = [event.stable_text, event.unstable_text]
          .filter((value): value is string => Boolean(value?.trim()))
          .join(' ');
        const correctedText = typeof event.preview === 'string'
          ? event.preview
          : typeof event.text === 'string'
            ? event.text
            : typeof event.transcript === 'string'
              ? event.transcript
              : segmentedText;
        schedulePartial(correctedText, true);
        return;
      }
      if (event.type === 'preview_unavailable') {
        setRealtimeStatus('fallback');
        reportError(userFacingErrorMessage(
          event.code || event.message,
          undefined,
          speechText(
            '实时语音预览暂不可用，完整录音仍在继续。',
            'Real-time speech preview is temporarily unavailable. Full recording is continuing.',
          ),
        ));
        return;
      }
      if (event.type === 'capture_stopped') {
        cancelPendingPartial();
        setFinalizingProgress({ completed: 0, total: null });
        return;
      }
      if (event.type === 'finalizing') {
        setFinalizingProgress({
          completed: Math.max(0, event.completed_segments || 0),
          total: typeof event.total_segments === 'number' ? event.total_segments : null,
        });
        return;
      }
      if (event.type === 'final') {
        const provider = event.provider || providerRef.current;
        providerRef.current = provider;
        emitFinal(
          event.transcript || event.text || '',
          provider,
          typeof event.duration_seconds === 'number' ? event.duration_seconds : null,
        );
        return;
      }
      if (event.type === 'error') {
        cancelPendingPartial();
        const messageText = userFacingErrorMessage(
          event.code || event.message,
          undefined,
          speechText(
            '语音录音或转写失败，请重试。',
            'Speech recording or transcription failed. Please try again.',
          ),
        );
        if (event.recoverable) {
          setRealtimeStatus('fallback');
          reportError(messageText);
          return;
        }
        terminalFailureRef.current = true;
        recordingActiveRef.current = false;
        captureActiveRef.current = false;
        cleanupAudio();
        reportError(messageText, true);
        settleReady(new Error(messageText));
        resolveFinal('');
      }
    };

    websocket.onerror = () => {
      if (generation !== generationRef.current) return;
      if (finalEmittedRef.current || terminalFailureRef.current) return;
      const connectionWasReady = readySettled;
      captureReadyRef.current = false;
      terminalFailureRef.current = true;
      recordingActiveRef.current = false;
      captureActiveRef.current = false;
      cancelPendingPartial();
      cleanupAudio();
      const message = speechText(
        '语音录音连接失败，请重试。',
        'The speech-recording connection failed. Please try again.',
      );
      settleReady(new Error(message));
      if (connectionWasReady) reportError(message, true);
      resolveFinal('');
      if (
        websocket.readyState === WebSocket.OPEN
        || websocket.readyState === WebSocket.CONNECTING
      ) {
        websocket.close();
      }
    };

    websocket.onclose = () => {
      if (websocketRef.current === websocket) websocketRef.current = null;
      captureReadyRef.current = false;
      if (generation !== generationRef.current) return;
      const connectionWasReady = readySettled;
      if (!connectionWasReady) settleReady(new Error(speechText(
        '语音录音服务未能建立连接。',
        'The speech-recording service could not connect.',
      )));
      if (!finalEmittedRef.current && !terminalFailureRef.current) {
        terminalFailureRef.current = true;
        recordingActiveRef.current = false;
        captureActiveRef.current = false;
        cancelPendingPartial();
        cleanupAudio();
        if (connectionWasReady) {
          reportError(speechText(
            '语音录音连接已中断，未生成不完整的最终文本。',
            'The speech-recording connection was interrupted; no incomplete final text was produced.',
          ), true);
        }
        resolveFinal('');
      }
    };
  }), [appLanguage, cancelPendingPartial, cleanupAudio, emitFinal, reportError, resetStartupPcm, resolveFinal, schedulePartial, speechText]);

  const prepareAudioContext = useCallback(async () => {
    const AudioContextCtor =
      window.AudioContext || (window as AudioContextWindow).webkitAudioContext;
    if (!AudioContextCtor || typeof AudioWorkletNode === 'undefined') {
      throw new Error(speechText(
        '当前浏览器不支持 AudioWorklet，请使用最新版 Chrome 或 Edge。',
        'This browser does not support AudioWorklet. Use the latest Chrome or Edge.',
      ));
    }

    let context = audioContextRef.current;
    if (!context || context.state === 'closed') {
      try {
        context = new AudioContextCtor({
          latencyHint: 'interactive',
          sampleRate: PCM_SAMPLE_RATE,
        });
      } catch {
        context = new AudioContextCtor({ latencyHint: 'interactive' });
      }
      audioContextRef.current = context;
      const loading = context.audioWorklet.addModule(audioWorkletModuleUrl());
      audioWorkletReadyRef.current = loading;
    }

    const ready = audioWorkletReadyRef.current || Promise.resolve();
    try {
      await Promise.all([
        ready,
        context.state === 'suspended' ? context.resume() : Promise.resolve(),
      ]);
    } catch (error) {
      if (audioContextRef.current === context) {
        audioContextRef.current = null;
        audioWorkletReadyRef.current = null;
      }
      if (context.state !== 'closed') await context.close();
      throw error;
    }
    return context;
  }, [speechText]);

  const startAudioCapture = useCallback(async (
    stream: MediaStream,
    context: AudioContext,
    generation: number,
  ) => {
    const source = context.createMediaStreamSource(stream);
    const worklet = new AudioWorkletNode(context, 'pcm16-processor', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: { targetSampleRate: PCM_SAMPLE_RATE },
    });
    const silentGain = context.createGain();
    silentGain.gain.value = 0;

    worklet.port.onmessage = (
      message: MessageEvent<{ type?: string; pcm?: ArrayBuffer }>,
    ) => {
      if (message.data?.type === 'flushed') {
        flushResolveRef.current?.();
        return;
      }
      if (
        generation !== generationRef.current
        || (!recordingActiveRef.current && !captureDrainingRef.current)
        || message.data?.type !== 'pcm'
        || !(message.data.pcm instanceof ArrayBuffer)
      ) {
        return;
      }
      const pcm = message.data.pcm;
      const websocket = websocketRef.current;
      if (
        captureReadyRef.current
        && websocket?.readyState === WebSocket.OPEN
      ) {
        websocket.send(pcm);
        return;
      }

      const nextBytes = startupPcmBytesRef.current + pcm.byteLength;
      if (nextBytes > STARTUP_PCM_MAX_BYTES) {
        recordingActiveRef.current = false;
        captureActiveRef.current = false;
        terminalFailureRef.current = true;
        cleanupAudio();
        closeRealtime();
        reportError(speechText(
          '语音录音服务启动超时，未提交不完整录音。',
          'The speech-recording service timed out while starting; no incomplete recording was submitted.',
        ), true);
        resolveFinal('');
        return;
      }
      startupPcmRef.current.push(pcm);
      startupPcmBytesRef.current = nextBytes;
    };

    source.connect(worklet);
    worklet.connect(silentGain);
    silentGain.connect(context.destination);
    sourceRef.current = source;
    workletRef.current = worklet;
    silentGainRef.current = silentGain;
  }, [cleanupAudio, closeRealtime, reportError, resolveFinal, speechText]);

  const flushWorklet = useCallback(async () => {
    const worklet = workletRef.current;
    if (!worklet) return;
    await new Promise<void>((resolve) => {
      let settled = false;
      let timeout: number | null = null;
      const settle = () => {
        if (settled) return;
        settled = true;
        if (timeout !== null) window.clearTimeout(timeout);
        if (flushResolveRef.current === settle) flushResolveRef.current = null;
        resolve();
      };
      flushResolveRef.current = settle;
      timeout = window.setTimeout(settle, WORKLET_FLUSH_TIMEOUT_MS);
      worklet.port.postMessage({ type: 'flush' });
    });
  }, []);

  const stopCaptureWhileConnecting = useCallback(() => {
    if (earlyCaptureStopPromiseRef.current) return earlyCaptureStopPromiseRef.current;

    captureDrainingRef.current = true;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    const pendingStop = flushWorklet().finally(() => {
      captureDrainingRef.current = false;
    });
    earlyCaptureStopPromiseRef.current = pendingStop;
    return pendingStop;
  }, [flushWorklet]);

  const finishFinalization = useCallback(async () => {
    try {
      const finalText = finalPromiseRef.current
        ? await finalPromiseRef.current
        : '';

      if (!finalText && mountedRef.current && !terminalFailureRef.current) {
        reportError(speechText(
          '完整语音转写没有返回可用文本，请重试。',
          'The full speech transcription returned no usable text. Please try again.',
        ), true);
      }
    } finally {
      finalPromiseRef.current = null;
      closeRealtime();
      disposeBrowserRecognition(false);
      if (mountedRef.current && !terminalFailureRef.current) {
        setStatus('idle');
        setRealtimeStatus('idle');
        setFinalizingProgress(null);
      }
      captureActiveRef.current = false;
      recordingLocaleRef.current = null;
      stopRequestedRef.current = false;
      earlyCaptureStopPromiseRef.current = null;
      resetBufferedStart();
      stoppingRef.current = false;
    }
  }, [closeRealtime, disposeBrowserRecognition, reportError, resetBufferedStart, speechText]);

  const cancelRecording = useCallback(() => {
    const latestTranscript = (
      pendingPartialAllowsEmptyRef.current
        ? pendingPartialRef.current
        : pendingPartialRef.current || transcriptRef.current
    ).trim();
    if (startingRef.current) startCancelledRef.current = true;
    generationRef.current += 1;
    recordingActiveRef.current = false;
    captureActiveRef.current = false;
    captureDrainingRef.current = false;
    recordingLocaleRef.current = null;
    stopRequestedRef.current = false;
    earlyCaptureStopPromiseRef.current = null;
    resetBufferedStart();
    stoppingRef.current = false;
    if (activeProviderRef.current === 'browser') {
      disposeBrowserRecognition();
    }

    const websocket = websocketRef.current;
    if (captureReadyRef.current && websocket?.readyState === WebSocket.OPEN) {
      try {
        websocket.send(JSON.stringify({ type: 'cancel' }));
      } catch {
        // Closing the socket below still cancels the server-side recording.
      }
    }

    cancelPendingPartial();
    closeRealtime();
    cleanupAudio();
    resolveFinal('');
    finalPromiseRef.current = null;
    if (mountedRef.current) {
      setStatus('idle');
      setRealtimeStatus('idle');
      setFinalizingProgress(null);
    }
    return latestTranscript;
  }, [cancelPendingPartial, cleanupAudio, closeRealtime, disposeBrowserRecognition, resetBufferedStart, resolveFinal]);

  // A page entering the browser back-forward cache is not unmounted.  Clean
  // up an in-flight capture before the page is frozen so returning to it can
  // start a fresh microphone stream and ASR WebSocket.
  useEffect(() => {
    const handlePageHide = () => {
      if (
        startingRef.current
        || recordingActiveRef.current
        || captureActiveRef.current
        || websocketRef.current !== null
      ) {
        cancelRecording();
      }
    };
    window.addEventListener('pagehide', handlePageHide);
    return () => window.removeEventListener('pagehide', handlePageHide);
  }, [cancelRecording]);

  useEffect(() => {
    if (!shouldCancelRecordingForLanguageChange(
      recordingLocaleRef.current,
      appLanguage,
      startingRef.current
        || recordingActiveRef.current
        || captureActiveRef.current
        || stoppingRef.current
        || finalPromiseRef.current !== null,
    )) return;
    cancelRecording();
    reportError(speechText(
      '语言已切换，本次录音已取消，请使用新语言重新录音。',
      'The language changed, so this recording was cancelled. Record again in the new language.',
    ), true);
  }, [appLanguage, cancelRecording, reportError, speechText]);

  const stopRecording = useCallback(async () => {
    if (startingRef.current) {
      if (stopRequestedRef.current) return finalPromiseRef.current || '';
      if (!captureActiveRef.current) {
        cancelRecording();
        return '';
      }

      stopRequestedRef.current = true;
      cancelPendingPartial();
      setStatus('transcribing');
      setFinalizingProgress({ completed: 0, total: null });
      activateRecording();
      if (activeProviderRef.current !== 'browser') {
        void stopCaptureWhileConnecting();
      }
      return finalPromiseRef.current || '';
    }
    if (!recordingActiveRef.current || stoppingRef.current) {
      return finalPromiseRef.current || '';
    }
    stopRequestedRef.current = false;
    stoppingRef.current = true;
    recordingActiveRef.current = false;
    captureActiveRef.current = false;
    cancelPendingPartial();
    captureDrainingRef.current = true;
    setStatus('transcribing');
    setFinalizingProgress({ completed: 0, total: null });
    if (activeProviderRef.current === 'browser') {
      captureDrainingRef.current = false;
      const finalPromise = finalPromiseRef.current || Promise.resolve('');
      const recognition = browserRecognitionRef.current;
      const stopTimeout = window.setTimeout(() => {
        if (finalEmittedRef.current || terminalFailureRef.current) return;
        const snapshot = browserSnapshot(true);
        disposeBrowserRecognition();
        if (snapshot) {
          const durationSeconds = browserStartedAtRef.current > 0
            ? Math.max(0, (performance.now() - browserStartedAtRef.current) / 1000)
            : null;
          emitFinal(snapshot, 'browser-web-speech', durationSeconds);
        } else {
          resolveFinal('');
        }
      }, 1_500);
      void finalPromise.finally(() => window.clearTimeout(stopTimeout));
      if (recognition) {
        try {
          recognition.stop();
        } catch {
          const snapshot = browserSnapshot(true);
          disposeBrowserRecognition();
          if (snapshot) emitFinal(snapshot, 'browser-web-speech', null);
          else resolveFinal('');
        }
      } else {
        const snapshot = browserSnapshot(true);
        disposeBrowserRecognition();
        if (snapshot) emitFinal(snapshot, 'browser-web-speech', null);
        else resolveFinal('');
      }
      void finishFinalization();
      return finalPromise;
    }

    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    await flushWorklet();
    captureDrainingRef.current = false;
    cleanupAudio(false);

    const websocket = websocketRef.current;
    if (captureReadyRef.current && websocket?.readyState === WebSocket.OPEN) {
      websocket.send(JSON.stringify({
        type: 'stop',
        client_stopped_at_ms: Date.now(),
      }));
    } else {
      terminalFailureRef.current = true;
      reportError(speechText(
        '语音录音连接已中断，无法生成完整转写。',
        'The speech-recording connection was interrupted, so a complete transcription cannot be produced.',
      ), true);
      resolveFinal('');
    }

    const finalPromise = finalPromiseRef.current || Promise.resolve('');
    void finishFinalization();
    return finalPromise;
  }, [activateRecording, browserSnapshot, cancelPendingPartial, cancelRecording, cleanupAudio, disposeBrowserRecognition, emitFinal, finishFinalization, flushWorklet, reportError, resolveFinal, speechText, stopCaptureWhileConnecting]);

  const startRecording = useCallback(async (
    startOptions: StartSpeechRecordingOptions = {},
  ) => {
    if (startingRef.current || recordingActiveRef.current || stoppingRef.current) return;
    startingRef.current = true;
    startCancelledRef.current = false;
    try {
      await optionsRef.current.beforeStart?.();
    } catch (syncError) {
      startingRef.current = false;
      if (!startCancelledRef.current && mountedRef.current) {
        reportError(userFacingErrorMessage(
          syncError,
          undefined,
          speechText(
            '语言设置尚未同步，暂时无法开始语音输入。',
            'The language setting is not synchronized, so speech input cannot start yet.',
          ),
        ), true);
      }
      startCancelledRef.current = false;
      return;
    }
    if (!mountedRef.current || startCancelledRef.current) {
      startingRef.current = false;
      startCancelledRef.current = false;
      return;
    }
    recordingLocaleRef.current = normalizeSpeechRecordingLocale(
      optionsRef.current.language || appLanguage,
    );
    resetBufferedStart();
    const bufferedStartRequested = startOptions.bufferedStart === true;
    if (bufferedStartRequested) {
      bufferedStartRef.current = true;
      bufferedActivationPromiseRef.current = new Promise<void>((resolve) => {
        resolveBufferedActivationRef.current = resolve;
      });
    }
    stopRequestedRef.current = false;
    captureActiveRef.current = false;
    earlyCaptureStopPromiseRef.current = null;
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    captureStartedAtRef.current = Date.now();
    try {
      const secureMessage = secureContextMessage(speechText);
      if (secureMessage) throw new Error(secureMessage);

      setError(null);
      setFinalizingProgress(null);
      setStatus('requesting');
      setRealtimeStatus(bufferedStartRequested ? 'idle' : 'connecting');
      transcriptRef.current = '';
      lastRevisionRef.current = -1;
      finalEmittedRef.current = false;
      terminalFailureRef.current = false;
      captureReadyRef.current = false;
      recordingIdRef.current = null;
      resetStartupPcm();
      finalPromiseRef.current = new Promise<string>((resolve) => {
        resolveFinalRef.current = resolve;
      });
      let selectedReadiness = readinessRef.current;
      if (!selectedReadiness.provider && !bufferedStartRequested) {
        try {
          selectedReadiness = await api.asrReadiness();
          readinessRef.current = selectedReadiness;
          setReadiness(selectedReadiness);
        } catch {
          // The first page load has no provider snapshot, so retain the safe default.
        }
      } else if (!bufferedStartRequested) {
        void api.asrReadiness().then((nextReadiness) => {
          if (!mountedRef.current) return;
          readinessRef.current = nextReadiness;
          setReadiness(nextReadiness);
        }).catch(() => undefined);
      }
      if (
        !mountedRef.current
        || generation !== generationRef.current
        || startCancelledRef.current
      ) {
        return;
      }
      const provider = selectedReadiness.provider || 'local';
      activeProviderRef.current = provider;

      if (provider === 'browser') {
        // Web Speech cannot replay locally buffered PCM. Keep its established
        // immediate-start behavior and advertise that limitation to callers.
        resetBufferedStart();
        recordingActiveRef.current = true;
        partialsAcceptedRef.current = true;
        await startBrowserRecognition(generation);
        if (
          generation !== generationRef.current
          || !recordingActiveRef.current
          || startCancelledRef.current
        ) {
          return;
        }
        if (stopRequestedRef.current) {
          startingRef.current = false;
          void stopRecording();
        }
        return;
      }
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error(speechText(
          '当前浏览器不支持录音，请使用最新版 Chrome 或 Edge。',
          'This browser does not support recording. Use the latest Chrome or Edge.',
        ));
      }

      const [stream, context] = await Promise.all([
        requestMicrophoneStream(),
        prepareAudioContext(),
      ]);
      stream.getAudioTracks().forEach((track) => {
        try {
          track.contentHint = 'speech';
        } catch {
          // Some older browsers expose contentHint as read-only.
        }
      });
      if (
        !mountedRef.current
        || generation !== generationRef.current
        || startCancelledRef.current
      ) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }

      streamRef.current = stream;
      recordingActiveRef.current = true;
      partialsAcceptedRef.current = true;
      const captureStarted = startAudioCapture(stream, context, generation).then(() => {
        if (
          generation === generationRef.current
          && recordingActiveRef.current
          && !startCancelledRef.current
        ) {
          captureActiveRef.current = true;
        }
      });
      if (bufferedStartRequested) {
        await captureStarted;
        if (
          generation !== generationRef.current
          || !recordingActiveRef.current
          || startCancelledRef.current
        ) {
          return;
        }
        setStatus('recording');
        setRealtimeStatus('idle');
        const activation = bufferedActivationPromiseRef.current;
        if (activation) await activation;
        if (
          generation !== generationRef.current
          || !recordingActiveRef.current
          || startCancelledRef.current
        ) {
          return;
        }
        bufferedStartRef.current = false;
        bufferedActivationPromiseRef.current = null;
        await openRealtime(generation);
      } else {
        await Promise.all([
          openRealtime(generation),
          captureStarted,
        ]);
      }
      if (
        generation !== generationRef.current
        || !recordingActiveRef.current
        || startCancelledRef.current
      ) {
        return;
      }
      if (stopRequestedRef.current) {
        const pendingCaptureStop = earlyCaptureStopPromiseRef.current;
        if (pendingCaptureStop) await pendingCaptureStop;
        if (
          generation !== generationRef.current
          || !recordingActiveRef.current
          || startCancelledRef.current
        ) {
          return;
        }
        startingRef.current = false;
        void stopRecording();
        return;
      }
      startingRef.current = false;
      setStatus('recording');
    } catch (startError) {
      const cancelled = (
        startCancelledRef.current
        || generation !== generationRef.current
        || !mountedRef.current
      );
      recordingActiveRef.current = false;
      captureActiveRef.current = false;
      recordingLocaleRef.current = null;
      stopRequestedRef.current = false;
      earlyCaptureStopPromiseRef.current = null;
      resetBufferedStart();
      disposeBrowserRecognition();
      closeRealtime();
      cleanupAudio();
      resolveFinal('');
      if (!cancelled) {
        terminalFailureRef.current = true;
        reportError(microphoneErrorMessage(startError, speechText), true);
      }
    } finally {
      startingRef.current = false;
      startCancelledRef.current = false;
    }
  }, [
    appLanguage,
    cleanupAudio,
    closeRealtime,
    disposeBrowserRecognition,
    openRealtime,
    prepareAudioContext,
    reportError,
    resetBufferedStart,
    resetStartupPcm,
    resolveFinal,
    speechText,
    startBrowserRecognition,
    startAudioCapture,
    stopRecording,
  ]);

  useEffect(() => {
    // React.StrictMode intentionally runs effect setup, cleanup, and setup
    // again in development.  The cleanup below marks the hook as unmounted,
    // so every subsequent mount must explicitly restore the live marker or
    // recording/WebSocket callbacks will be discarded forever.
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      recordingActiveRef.current = false;
      captureActiveRef.current = false;
      recordingLocaleRef.current = null;
      stopRequestedRef.current = false;
      earlyCaptureStopPromiseRef.current = null;
      resetBufferedStart();
      startCancelledRef.current = true;
      cancelPendingPartial();
      disposeBrowserRecognition();
      closeRealtime();
      cleanupAudio(true, true);
      resolveFinal('');
    };
  }, [cancelPendingPartial, cleanupAudio, closeRealtime, disposeBrowserRecognition, resetBufferedStart, resolveFinal]);

  return {
    status,
    realtimeStatus,
    error,
    finalizingProgress,
    readiness,
    readinessStatus: readiness.status,
    requesting: status === 'requesting',
    recording: status === 'recording',
    transcribing: status === 'transcribing',
    supportsBufferedStart: Boolean(readiness.provider && readiness.provider !== 'browser'),
    startRecording,
    activateRecording,
    stopRecording,
    cancelRecording,
  };
}

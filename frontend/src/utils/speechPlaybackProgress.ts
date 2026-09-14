export const SPEECH_PLAYBACK_PROGRESS_INTERVAL_MS = 500;
const MAX_REPORTED_BUFFER_SECONDS = 120;

type SpeechProgressSocket = {
  readonly readyState: number;
  send(data: string): void;
};

function nonNegativeFinite(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 0;
}

export function pcmAudioSeconds(byteLength: number, sampleRate: number): number {
  if (!Number.isFinite(sampleRate) || sampleRate <= 0) return 0;
  return nonNegativeFinite(Math.floor(nonNegativeFinite(byteLength) / 2) / sampleRate);
}

export function bufferedSpeechSeconds(
  pendingAudioSeconds: number,
  scheduledEndSeconds: number,
  audioContextTimeSeconds: number,
): number {
  const scheduledSeconds = Number.isFinite(scheduledEndSeconds)
    && Number.isFinite(audioContextTimeSeconds)
    && scheduledEndSeconds >= 0
    && audioContextTimeSeconds >= 0
    ? Math.max(0, scheduledEndSeconds - audioContextTimeSeconds)
    : 0;
  return nonNegativeFinite(nonNegativeFinite(pendingAudioSeconds) + scheduledSeconds);
}

export function createSpeechPlaybackProgressReporter({
  streamId,
  socket,
  getCurrentSocket,
  isCurrent,
  getBufferedSeconds,
  now,
  setTimer,
  clearTimer,
}: {
  streamId: string;
  socket: SpeechProgressSocket;
  getCurrentSocket: () => SpeechProgressSocket | null;
  isCurrent: () => boolean;
  getBufferedSeconds: () => number;
  now: () => number;
  setTimer: (callback: () => void, delayMs: number) => number;
  clearTimer: (timerId: number) => void;
}) {
  let timerId: number | null = null;
  let started = false;
  let stopped = false;
  let sequence = 0;
  let lastAttemptAt: number | null = null;

  const clearPendingTimer = () => {
    if (timerId === null) return;
    clearTimer(timerId);
    timerId = null;
  };

  const stop = () => {
    stopped = true;
    clearPendingTimer();
  };

  const active = () => {
    if (!started || stopped) return false;
    if (!streamId || !isCurrent() || getCurrentSocket() !== socket || socket.readyState !== 1) {
      stop();
      return false;
    }
    return true;
  };

  const schedule = (delayMs: number) => {
    if (stopped || timerId !== null) return;
    const nextTimerId = setTimer(() => {
      if (timerId !== nextTimerId) return;
      timerId = null;
      report();
    }, delayMs);
    timerId = nextTimerId;
  };

  const report = () => {
    if (!active()) return;
    const currentTime = now();
    if (!Number.isFinite(currentTime)) {
      schedule(SPEECH_PLAYBACK_PROGRESS_INTERVAL_MS);
      return;
    }
    const elapsed = lastAttemptAt === null
      ? SPEECH_PLAYBACK_PROGRESS_INTERVAL_MS
      : Math.max(0, currentTime - lastAttemptAt);
    if (elapsed < SPEECH_PLAYBACK_PROGRESS_INTERVAL_MS) {
      schedule(SPEECH_PLAYBACK_PROGRESS_INTERVAL_MS - elapsed);
      return;
    }
    lastAttemptAt = currentTime;
    try {
      socket.send(JSON.stringify({
        type: 'playback_progress',
        speech_stream_id: streamId,
        buffered_seconds: Math.min(
          MAX_REPORTED_BUFFER_SECONDS, nonNegativeFinite(getBufferedSeconds()),
        ),
        sequence: sequence + 1,
      }));
      sequence += 1;
    } catch {
      // Progress is advisory; a failed send must not interrupt audio playback.
    }
    clearPendingTimer();
    schedule(SPEECH_PLAYBACK_PROGRESS_INTERVAL_MS);
  };

  return {
    start() {
      if (started || stopped) return;
      started = true;
      if (active()) schedule(SPEECH_PLAYBACK_PROGRESS_INTERVAL_MS);
    },
    notifyAudioReceived: report,
    stop,
  };
}

export type SpeechPlaybackProgressReporter = ReturnType<typeof createSpeechPlaybackProgressReporter>;

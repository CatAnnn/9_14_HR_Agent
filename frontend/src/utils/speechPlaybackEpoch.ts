export type SpeechPlaybackEpoch = Readonly<{
  generation: number;
  streamId: string | null;
}>;

export function createSpeechPlaybackEpoch(): SpeechPlaybackEpoch {
  return { generation: 0, streamId: null };
}

export function advanceSpeechPlaybackEpoch(
  current: SpeechPlaybackEpoch,
  streamId: string | null,
): SpeechPlaybackEpoch {
  return {
    generation: current.generation + 1,
    streamId,
  };
}

export function isSpeechPlaybackEpochCurrent(
  current: SpeechPlaybackEpoch,
  candidate: SpeechPlaybackEpoch,
): boolean {
  return current.generation === candidate.generation
    && current.streamId === candidate.streamId;
}

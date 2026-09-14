export interface EditableSpeechDraft {
  value: string;
  lastTranscript: string;
  consumedTranscriptLength: number;
  autoStart: number;
  autoEnd: number;
}

function clampIndex(value: string, index: number): number {
  if (!Number.isFinite(index)) return value.length;
  return Math.max(0, Math.min(value.length, Math.trunc(index)));
}

function normalizeTranscript(transcript: string): string {
  return transcript.trim();
}

function transformedSpeechEnd(previous: string, next: string, speechEnd: number): number {
  let editStart = 0;
  const commonLength = Math.min(previous.length, next.length);
  while (editStart < commonLength && previous[editStart] === next[editStart]) editStart += 1;

  let previousEditEnd = previous.length;
  let nextEditEnd = next.length;
  while (
    previousEditEnd > editStart
    && nextEditEnd > editStart
    && previous[previousEditEnd - 1] === next[nextEditEnd - 1]
  ) {
    previousEditEnd -= 1;
    nextEditEnd -= 1;
  }

  if (speechEnd < editStart) return speechEnd;
  if (speechEnd > previousEditEnd) {
    return speechEnd + (nextEditEnd - editStart) - (previousEditEnd - editStart);
  }
  return nextEditEnd;
}

function initialSpeechInsertion(
  value: string,
  start: number,
  transcript: string,
): string {
  if (!transcript || start === 0 || /\s$/u.test(value.slice(0, start))) return transcript;
  return ` ${transcript}`;
}

/**
 * Starts an editable speech span at the current caret. The span is the only
 * part later ASR snapshots may replace; the rest of the composer stays owned
 * by the user.
 */
export function beginEditableSpeechDraft(
  value: string,
  caret: number = value.length,
): EditableSpeechDraft {
  const insertionPoint = clampIndex(value, caret);
  return {
    value,
    lastTranscript: '',
    consumedTranscriptLength: 0,
    autoStart: insertionPoint,
    autoEnd: insertionPoint,
  };
}

/**
 * Applies the latest cumulative ASR snapshot. Before a keyboard edit the
 * current automatic span is replaced. After an edit, all visible text is
 * frozen and only transcript characters not seen at edit time are inserted.
 */
export function applyEditableSpeechTranscript(
  state: EditableSpeechDraft,
  transcript: string,
): EditableSpeechDraft {
  const nextTranscript = normalizeTranscript(transcript);
  const consumed = Math.min(
    state.consumedTranscriptLength,
    nextTranscript.length,
  );
  let automaticText = nextTranscript.slice(consumed);
  if (state.consumedTranscriptLength === 0) {
    automaticText = initialSpeechInsertion(state.value, state.autoStart, automaticText);
  }

  const value = `${state.value.slice(0, state.autoStart)}${automaticText}${state.value.slice(state.autoEnd)}`;
  return {
    value,
    lastTranscript: nextTranscript,
    consumedTranscriptLength: state.consumedTranscriptLength,
    autoStart: state.autoStart,
    autoEnd: state.autoStart + automaticText.length,
  };
}

/**
 * Makes keyboard input authoritative. The currently visible ASR preview is
 * retained, while future ASR snapshots can only contribute their new tail at
 * the transformed end of that speech span. This keeps an interior correction
 * in place without inserting later spoken words into the middle of the text.
 */
export function editEditableSpeechDraft(
  state: EditableSpeechDraft,
  value: string,
  caret: number = value.length,
): EditableSpeechDraft {
  const insertionPoint = clampIndex(
    value,
    state.value === value
      ? caret
      : transformedSpeechEnd(state.value, value, state.autoEnd),
  );
  return {
    value,
    lastTranscript: state.lastTranscript,
    consumedTranscriptLength: state.lastTranscript.length,
    autoStart: insertionPoint,
    autoEnd: insertionPoint,
  };
}

/** Move the initial empty speech insertion point when the user moves caret. */
export function moveEditableSpeechCaret(
  state: EditableSpeechDraft,
  caret: number,
): EditableSpeechDraft {
  if (state.autoStart !== state.autoEnd) return state;
  const insertionPoint = clampIndex(state.value, caret);
  return {
    ...state,
    autoStart: insertionPoint,
    autoEnd: insertionPoint,
  };
}

/**
 * Removes only the still-automatic preview on a fatal/cancelled recording.
 * Keyboard edits and any transcript text frozen by those edits are preserved.
 */
export function cancelEditableSpeechDraft(state: EditableSpeechDraft): string {
  return `${state.value.slice(0, state.autoStart)}${state.value.slice(state.autoEnd)}`;
}

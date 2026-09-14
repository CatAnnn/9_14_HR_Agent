export interface PushToTalkShortcutState {
  enabled: boolean;
  spaceHeld: boolean;
  ownsRecording: boolean;
  activated: boolean;
}

export interface PushToTalkShortcutAction {
  state: PushToTalkShortcutState;
  start: boolean;
  stop: boolean;
  cancel: boolean;
  insertSpace: boolean;
  preventDefault: boolean;
}

export interface PushToTalkKeyInput {
  key: string;
  code?: string;
  repeat?: boolean;
  isComposing?: boolean;
  altKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
}

export const PUSH_TO_TALK_HOLD_MS = 220;

const NO_COMMAND = {
  start: false,
  stop: false,
  cancel: false,
  insertSpace: false,
  preventDefault: false,
} as const;

function action(
  state: PushToTalkShortcutState,
  command: Partial<Omit<PushToTalkShortcutAction, 'state'>> = NO_COMMAND,
): PushToTalkShortcutAction {
  return {
    state,
    start: command.start ?? false,
    stop: command.stop ?? false,
    cancel: command.cancel ?? false,
    insertSpace: command.insertSpace ?? false,
    preventDefault: command.preventDefault ?? false,
  };
}

function isPlainSpace(input: PushToTalkKeyInput): boolean {
  return (
    (
      input.code === 'Space'
      || (!input.code && (input.key === ' ' || input.key === 'Spacebar'))
    )
    && !input.isComposing
    && !input.altKey
    && !input.ctrlKey
    && !input.metaKey
    && !input.shiftKey
  );
}

export function createPushToTalkShortcutState(
  enabled = false,
): PushToTalkShortcutState {
  return {
    enabled,
    spaceHeld: false,
    ownsRecording: false,
    activated: false,
  };
}

/**
 * Enables or disables the shortcut. Disabling it also releases a recording
 * started by this shortcut so a hidden/unmounted control cannot leave the mic
 * running.
 */
export function setPushToTalkShortcutEnabled(
  state: PushToTalkShortcutState,
  enabled: boolean,
): PushToTalkShortcutAction {
  if (state.enabled === enabled) return action(state);

  return action(
    {
      enabled,
      spaceHeld: false,
      ownsRecording: false,
      activated: false,
    },
    { cancel: state.ownsRecording },
  );
}

/**
 * Handles the initial Space press. `canStart` should be false while another
 * recording owns the microphone or while final transcription is in progress.
 * In that case the shortcut leaves the key completely untouched.
 */
export function pushToTalkShortcutKeyDown(
  state: PushToTalkShortcutState,
  input: PushToTalkKeyInput,
  canStart: boolean,
): PushToTalkShortcutAction {
  if (!state.enabled || !isPlainSpace(input)) return action(state);

  if (state.spaceHeld || input.repeat) {
    return action(state, { preventDefault: state.ownsRecording });
  }

  if (!canStart) return action(state);

  return action(
    {
      ...state,
      spaceHeld: true,
      ownsRecording: true,
      activated: false,
    },
    { start: true, preventDefault: true },
  );
}

/**
 * Promotes the immediately-started recording to a real push-to-talk hold once
 * the component's hold threshold expires. Calling this after keyup/cancel is a
 * safe no-op, so a stale timer cannot restart the microphone.
 */
export function activatePushToTalkShortcut(
  state: PushToTalkShortcutState,
): PushToTalkShortcutAction {
  if (
    !state.enabled
    || !state.spaceHeld
    || !state.ownsRecording
    || state.activated
  ) return action(state);

  return action({
    ...state,
    activated: true,
  });
}

/**
 * A release before activation cancels the temporary recording and asks the
 * composer to insert one ordinary space. A release after activation finalizes
 * the recording.
 */
export function pushToTalkShortcutKeyUp(
  state: PushToTalkShortcutState,
  input: Pick<PushToTalkKeyInput, 'key' | 'code'>,
): PushToTalkShortcutAction {
  if (
    input.code !== 'Space'
    && (input.code || (input.key !== ' ' && input.key !== 'Spacebar'))
  ) return action(state);
  if (!state.spaceHeld || !state.ownsRecording) return action(state);

  return action(
    {
      ...state,
      spaceHeld: false,
      ownsRecording: false,
      activated: false,
    },
    {
      stop: state.activated,
      cancel: !state.activated,
      insertSpace: !state.activated,
      preventDefault: true,
    },
  );
}

/**
 * Releases shortcut ownership on blur, visibility change, route change or
 * unmount. This is intentionally idempotent.
 */
export function cancelPushToTalkShortcut(
  state: PushToTalkShortcutState,
): PushToTalkShortcutAction {
  if (!state.spaceHeld && !state.ownsRecording) return action(state);

  return action(
    {
      ...state,
      spaceHeld: false,
      ownsRecording: false,
      activated: false,
    },
    { cancel: state.ownsRecording },
  );
}

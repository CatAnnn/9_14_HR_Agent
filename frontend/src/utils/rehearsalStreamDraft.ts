import type { ConversationTurn } from '../types/domain';

/** Accumulate tokens without copying conversation history until a render consumes them. */
export function createRehearsalStreamDraft(
  history: readonly ConversationTurn[],
  managerText: string,
) {
  const completedTurns: ConversationTurn[] = [
    ...history,
    { speaker: 'manager', text: managerText },
  ];
  let employeeText = '';
  let snapshot: ConversationTurn[] = [
    ...completedTurns,
    { speaker: 'employee', text: employeeText },
  ];
  let changed = false;

  return {
    append(delta: string): boolean {
      if (!delta) return false;
      employeeText += delta;
      changed = true;
      return true;
    },
    getSnapshot(): ConversationTurn[] {
      if (changed) {
        snapshot = [...completedTurns, { speaker: 'employee', text: employeeText }];
        changed = false;
      }
      return snapshot;
    },
  };
}

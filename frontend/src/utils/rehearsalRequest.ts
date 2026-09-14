import type { ConversationTurn } from '../types/domain';

export const REHEARSAL_REQUEST_ID_METADATA_KEY = 'rehearsal_request_id';

export function createRehearsalRequestId(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === 'function') {
    return cryptoApi.randomUUID();
  }
  if (typeof cryptoApi?.getRandomValues === 'function') {
    const words = new Uint32Array(4);
    cryptoApi.getRandomValues(words);
    return Array.from(words, (word) => word.toString(36)).join('_');
  }
  return `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}_request`;
}

export function hasCompletedRehearsalRequest(
  conversation: ConversationTurn[],
  requestId: string,
): boolean {
  const managerTurn = conversation.find(
    (turn) => turn.speaker === 'manager'
      && turn.metadata?.[REHEARSAL_REQUEST_ID_METADATA_KEY] === requestId,
  );
  if (!managerTurn) return false;

  return conversation.some(
    (turn) => turn.speaker === 'employee'
      && turn.metadata?.[REHEARSAL_REQUEST_ID_METADATA_KEY] === requestId
      && (
        typeof managerTurn.turn_index !== 'number'
        || typeof turn.turn_index !== 'number'
        || turn.turn_index === managerTurn.turn_index + 1
      ),
  );
}

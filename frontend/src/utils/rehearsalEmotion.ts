import type { ConversationTurn, EmotionTurnSnapshot } from '../types/domain';

export const NEGATIVE_EMOTION_VALENCE_THRESHOLD = 0;
export const NEGATIVE_EMOTION_STREAK_LENGTH = 3;

const NEGATIVE_EMOTION_ANCHOR_IDS = new Set([
  'tired',
  'discouraged',
  'sad',
  'disappointed',
  'anxious',
  'confused',
  'guarded',
  'angry',
  'resentful',
  'detached',
  'frustrated',
  'irritated',
  'aggrieved',
  'embarrassed',
  'ashamed',
  'guilty',
  'overwhelmed',
  'afraid',
  'helpless',
]);

const LEGACY_EMOTION_ANCHOR_ALIASES: Readonly<Record<string, string>> = {
  calm_receptive: 'calm',
  cautious_neutral: 'neutral',
  skeptical_controlled: 'guarded',
  disappointed_withdrawn: 'disappointed',
  anxious_defensive: 'anxious',
  defensive_resistant: 'guarded',
  angry_challenging: 'angry',
  hopeful_negotiating: 'engaged',
  aligned_ready: 'pleased',
};

function getMeasuredEmployeeEmotion(
  turn: ConversationTurn,
): EmotionTurnSnapshot | null {
  if (turn.speaker !== 'manager' && turn.speaker !== 'you') return null;

  const snapshot = turn.metadata?.emotion_snapshot;
  return snapshot
    && typeof snapshot.valence === 'number'
    && Number.isFinite(snapshot.valence)
    ? snapshot
    : null;
}

function isExplicitlyNegativeEmotion(snapshot: EmotionTurnSnapshot): boolean {
  const rawAnchorId = snapshot.anchor_id?.normalize('NFKC').trim().toLowerCase();
  if (!rawAnchorId) return false;

  const anchorId = LEGACY_EMOTION_ANCHOR_ALIASES[rawAnchorId] || rawAnchorId;
  return NEGATIVE_EMOTION_ANCHOR_IDS.has(anchorId)
    && snapshot.valence < NEGATIVE_EMOTION_VALENCE_THRESHOLD;
}

export function shouldPromptEmotionReview(
  turns: readonly ConversationTurn[],
): boolean {
  const recentEmotions: EmotionTurnSnapshot[] = [];

  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const emotion = getMeasuredEmployeeEmotion(turns[index]);
    if (emotion === null) continue;

    recentEmotions.push(emotion);
    if (recentEmotions.length === NEGATIVE_EMOTION_STREAK_LENGTH) break;
  }

  return recentEmotions.length === NEGATIVE_EMOTION_STREAK_LENGTH
    && recentEmotions.every(isExplicitlyNegativeEmotion);
}

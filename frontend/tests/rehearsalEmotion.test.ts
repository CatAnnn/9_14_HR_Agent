import assert from 'node:assert/strict';
import test from 'node:test';

import type { ConversationTurn } from '../src/types/domain.ts';
import { shouldPromptEmotionReview } from '../src/utils/rehearsalEmotion.ts';

function managerTurn(valence?: number, anchorId?: string): ConversationTurn {
  return {
    speaker: 'manager',
    text: '经理发言',
    metadata: valence === undefined
      ? {}
      : {
          emotion_snapshot: {
            valence,
            arousal: 0,
            dominance: 0,
            anchor_id: anchorId,
          },
        },
  };
}

test('prompts after three consecutive measured negative employee states', () => {
  const turns: ConversationTurn[] = [
    managerTurn(-0.2, 'confused'),
    { speaker: 'employee', text: '员工回复' },
    managerTurn(-0.35, 'guarded'),
    { speaker: 'employee', text: '员工回复' },
    managerTurn(-0.4, 'anxious'),
  ];

  assert.equal(shouldPromptEmotionReview(turns), true);
});

test('recognizes newly added negative emotion anchors', () => {
  assert.equal(
    shouldPromptEmotionReview([
      managerTurn(-0.18, 'detached'),
      managerTurn(-0.55, 'frustrated'),
      managerTurn(-0.68, 'overwhelmed'),
    ]),
    true,
  );
});

test('does not classify new positive or mixed anchors as a negative streak', () => {
  assert.equal(
    shouldPromptEmotionReview([
      managerTurn(0.55, 'hopeful'),
      managerTurn(-0.12, 'skeptical'),
      managerTurn(-0.10, 'uncertain'),
    ]),
    false,
  );
});

test('does not prompt before three negative states or after emotion recovers', () => {
  assert.equal(
    shouldPromptEmotionReview([
      managerTurn(-0.3, 'guarded'),
      managerTurn(-0.4, 'anxious'),
    ]),
    false,
  );
  assert.equal(
    shouldPromptEmotionReview([
      managerTurn(-0.3, 'guarded'),
      managerTurn(-0.4, 'anxious'),
      managerTurn(0, 'neutral'),
    ]),
    false,
  );
});

test('uses the latest measured snapshots and ignores unmeasured streaming turns', () => {
  const turns: ConversationTurn[] = [
    managerTurn(-0.25, 'confused'),
    managerTurn(-0.3, 'guarded'),
    managerTurn(-0.35, 'anxious'),
    managerTurn(),
    { speaker: 'employee', text: '正在生成回复' },
  ];

  assert.equal(shouldPromptEmotionReview(turns), true);
});

test('does not treat neutral threshold values or invalid snapshots as negative', () => {
  const invalidTurn = managerTurn(Number.NaN);
  assert.equal(
    shouldPromptEmotionReview([
      managerTurn(-0.3, 'guarded'),
      managerTurn(-0.4, 'anxious'),
      invalidTurn,
      managerTurn(0, 'neutral'),
    ]),
    false,
  );
});

test('neutral anchors never count as negative even when continuous valence drifts below zero', () => {
  assert.equal(
    shouldPromptEmotionReview([
      managerTurn(-0.3, 'guarded'),
      managerTurn(-0.4, 'anxious'),
      managerTurn(-0.2, 'neutral'),
    ]),
    false,
  );
});

test('does not infer a negative state when the semantic anchor is missing', () => {
  assert.equal(
    shouldPromptEmotionReview([
      managerTurn(-0.3),
      managerTurn(-0.4),
      managerTurn(-0.5),
    ]),
    false,
  );
});

test('supports legacy negative anchors while excluding legacy neutral anchors', () => {
  assert.equal(
    shouldPromptEmotionReview([
      managerTurn(-0.3, 'skeptical_controlled'),
      managerTurn(-0.4, 'anxious_defensive'),
      managerTurn(-0.5, 'disappointed_withdrawn'),
    ]),
    true,
  );
  assert.equal(
    shouldPromptEmotionReview([
      managerTurn(-0.3, 'skeptical_controlled'),
      managerTurn(-0.4, 'anxious_defensive'),
      managerTurn(-0.2, 'cautious_neutral'),
    ]),
    false,
  );
});

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyEditableSpeechTranscript,
  beginEditableSpeechDraft,
  cancelEditableSpeechDraft,
  editEditableSpeechDraft,
  moveEditableSpeechCaret,
} from '../src/utils/editableSpeechDraft.ts';

test('cumulative partials replace the automatic span instead of duplicating it', () => {
  let draft = beginEditableSpeechDraft('已有内容', 4);
  draft = applyEditableSpeechTranscript(draft, '你好');
  assert.equal(draft.value, '已有内容 你好');

  draft = applyEditableSpeechTranscript(draft, '你好，今天');
  assert.equal(draft.value, '已有内容 你好，今天');

  draft = applyEditableSpeechTranscript(draft, '你好，今天');
  assert.equal(draft.value, '已有内容 你好，今天');
});

test('keyboard corrections stay authoritative while later speech keeps appending', () => {
  let draft = beginEditableSpeechDraft('', 0);
  draft = applyEditableSpeechTranscript(draft, '这个方案不太');

  const corrected = '这个方案需要调整';
  draft = editEditableSpeechDraft(draft, corrected, corrected.length);
  draft = applyEditableSpeechTranscript(draft, '这个方案不太合适');

  assert.equal(draft.value, '这个方案需要调整合适');
});

test('an interior correction keeps later speech at the end of the spoken span', () => {
  let draft = beginEditableSpeechDraft('', 0);
  draft = applyEditableSpeechTranscript(draft, 'abcdef');
  draft = editEditableSpeechDraft(draft, 'abXdef', 3);
  draft = applyEditableSpeechTranscript(draft, 'abcdefgh');

  assert.equal(draft.value, 'abXdefgh');
});

test('keyboard input before and after speech is never overwritten by final transcript', () => {
  let draft = beginEditableSpeechDraft('开场', 2);
  draft = applyEditableSpeechTranscript(draft, '我理解');

  const edited = '先说：开场 我理解，再确认事实';
  draft = editEditableSpeechDraft(draft, edited, edited.length);
  draft = applyEditableSpeechTranscript(draft, '我理解你的顾虑');

  assert.equal(draft.value, '先说：开场 我理解，再确认事实你的顾虑');
});

test('the first partial follows a caret moved after recording starts', () => {
  let draft = beginEditableSpeechDraft('前后', 2);
  draft = moveEditableSpeechCaret(draft, 1);
  draft = applyEditableSpeechTranscript(draft, '语音');
  assert.equal(draft.value, '前 语音后');
});

test('fatal cancellation removes only the current automatic preview', () => {
  let untouched = beginEditableSpeechDraft('手工内容', 4);
  untouched = applyEditableSpeechTranscript(untouched, '临时转写');
  assert.equal(cancelEditableSpeechDraft(untouched), '手工内容');

  let edited = beginEditableSpeechDraft('', 0);
  edited = applyEditableSpeechTranscript(edited, '初始转写');
  edited = editEditableSpeechDraft(edited, '人工修正', 4);
  edited = applyEditableSpeechTranscript(edited, '初始转写新增');
  assert.equal(cancelEditableSpeechDraft(edited), '人工修正');
});

test('a repeated final snapshot is idempotent', () => {
  let draft = beginEditableSpeechDraft('', 0);
  draft = applyEditableSpeechTranscript(draft, '完整话语');
  const once = draft.value;
  draft = applyEditableSpeechTranscript(draft, '完整话语');
  assert.equal(draft.value, once);
});

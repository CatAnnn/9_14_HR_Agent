import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeRuntimeNoteDrafts,
  runtimeNotesToDrafts,
} from '../src/utils/rehearsalRuntimeNotes.ts';

test('loads every saved runtime note as a separate editable draft', () => {
  const notes = ['第一条背景', '第二条背景\n包含补充说明', '  '];

  assert.deepEqual(runtimeNotesToDrafts(notes), [
    '第一条背景',
    '第二条背景\n包含补充说明',
  ]);
});

test('normalizes edited notes without merging their boundaries', () => {
  assert.deepEqual(
    normalizeRuntimeNoteDrafts([
      '  修改后的第一条  ',
      '',
      '第二条\n仍保留多行',
    ]),
    ['修改后的第一条', '第二条\n仍保留多行'],
  );
});

test('removes blank and duplicate notes before replacing the saved list', () => {
  assert.deepEqual(
    normalizeRuntimeNoteDrafts(['相同背景', '  相同背景  ', '', '   ']),
    ['相同背景'],
  );
});

test('keeps one empty editor when no saved background exists', () => {
  assert.deepEqual(runtimeNotesToDrafts([]), ['']);
});

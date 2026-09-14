import assert from 'node:assert/strict';
import test from 'node:test';

import {
  emptyDraft,
  goalsMatch,
  migrateLegacyPerformanceItems,
  normalizeGeneratedPerformanceItem,
  normalizeGeneratedPerformanceItems,
  performanceSectionTitles,
} from '../src/utils/intentPerformanceSections.ts';

const CHINESE_TITLES = [
  '目标达成总览',
  '正向表现/取得进展',
  '现存差距与行为实例',
];
const ENGLISH_TITLES = [
  'Goal Achievement Overview',
  'Positive Performance / Progress',
  'Current Gaps and Behavioral Examples',
];

function itemsFor(titles: string[]) {
  return titles.map((goal, index) => ({
    goal,
    current_performance: `Performance ${index + 1}`,
    generation_reason: null,
  }));
}

test('uses the exact backend canonical titles for each locale', () => {
  assert.deepEqual(performanceSectionTitles('zh-CN'), CHINESE_TITLES);
  assert.deepEqual(performanceSectionTitles('en'), ENGLISH_TITLES);
  assert.deepEqual(
    emptyDraft('development', 'en').performance_items.map((item) => item.goal),
    ENGLISH_TITLES,
  );
});

test('normalization preserves valid server titles instead of rewriting them', () => {
  const englishItems = itemsFor(ENGLISH_TITLES);
  const chineseItems = itemsFor(CHINESE_TITLES);

  assert.deepEqual(
    normalizeGeneratedPerformanceItems(englishItems, 'en')?.map((item) => item.goal),
    ENGLISH_TITLES,
  );
  assert.deepEqual(
    normalizeGeneratedPerformanceItems(chineseItems, 'zh-CN')?.map((item) => item.goal),
    CHINESE_TITLES,
  );
  assert.equal(
    normalizeGeneratedPerformanceItem(englishItems[0], 0, 'en')?.goal,
    ENGLISH_TITLES[0],
  );
});

test('rejects mixed-language, missing, and incorrectly ordered sections', () => {
  const mixedItems = itemsFor([
    ENGLISH_TITLES[0],
    CHINESE_TITLES[1],
    ENGLISH_TITLES[2],
  ]);
  const reorderedItems = itemsFor([
    ENGLISH_TITLES[1],
    ENGLISH_TITLES[0],
    ENGLISH_TITLES[2],
  ]);

  assert.equal(goalsMatch(mixedItems, 'en'), false);
  assert.equal(normalizeGeneratedPerformanceItems(mixedItems, 'en'), null);
  assert.equal(normalizeGeneratedPerformanceItems(reorderedItems, 'en'), null);
  assert.equal(normalizeGeneratedPerformanceItems(itemsFor(ENGLISH_TITLES.slice(0, 2)), 'en'), null);
});

test('migrates only a complete legacy title set and keeps manager edits', () => {
  const legacyItems = itemsFor(CHINESE_TITLES);
  const migrated = migrateLegacyPerformanceItems(legacyItems, 'en');

  assert.deepEqual(migrated?.map((item) => item.goal), ENGLISH_TITLES);
  assert.deepEqual(
    migrated?.map((item) => item.current_performance),
    legacyItems.map((item) => item.current_performance),
  );
  assert.equal(
    migrateLegacyPerformanceItems(itemsFor(['unknown', ...CHINESE_TITLES.slice(1)]), 'en'),
    null,
  );
});

import assert from 'node:assert/strict';
import test from 'node:test';

import { GERMAN_TEXT } from '../src/i18n/messages/de.ts';
import { JAPANESE_TEXT } from '../src/i18n/messages/ja.ts';
import {
  fromPersonalitySliderValue,
  PERSONALITY_DIMENSIONS,
  personalitySliderText,
  toPersonalitySliderValue,
} from '../src/utils/personalitySlider.ts';

const personalityTranslationKeys = PERSONALITY_DIMENSIONS.flatMap((dimension) => [
  dimension.label,
  dimension.low,
  dimension.mid,
  dimension.high,
]);

test('uses consistent action-based titles and positive direction copy', () => {
  assert.deepEqual(
    PERSONALITY_DIMENSIONS.map((dimension) => dimension.label),
    [
      '愿意尝试新方法',
      '做事具有计划性',
      '主动地表达想法',
      '愿意和别人商量',
      '面对压力保持平稳',
    ],
  );

  const pressure = PERSONALITY_DIMENSIONS.find((dimension) => dimension.key === 'neuroticism');
  assert.ok(pressure);
  assert.equal(pressure.low, '比较容易紧张或担心');
  assert.equal(pressure.high, '遇到压力时比较平静');
});

test('provides native German and Japanese copy for every personality slider label', () => {
  assert.equal(personalityTranslationKeys.length, 20);
  for (const key of personalityTranslationKeys) {
    assert.ok(GERMAN_TEXT[key]?.trim(), `missing German personality translation: ${key}`);
    assert.ok(JAPANESE_TEXT[key]?.trim(), `missing Japanese personality translation: ${key}`);
    assert.notEqual(GERMAN_TEXT[key], key, `German personality text fell back to Chinese: ${key}`);
    assert.notEqual(JAPANESE_TEXT[key], key, `Japanese personality text fell back to Chinese: ${key}`);
  }
});

test('reverses only neuroticism for a consistent left-to-right UI', () => {
  for (const dimension of PERSONALITY_DIMENSIONS) {
    const storedValue = 23;
    const sliderValue = toPersonalitySliderValue(dimension.key, storedValue);
    assert.equal(
      sliderValue,
      dimension.key === 'neuroticism' ? 77 : storedValue,
    );
    assert.equal(fromPersonalitySliderValue(dimension.key, sliderValue), storedValue);
  }
});

test('selects descriptions from the displayed slider direction', () => {
  const pressure = PERSONALITY_DIMENSIONS.find((dimension) => dimension.key === 'neuroticism');
  assert.ok(pressure);
  assert.equal(personalitySliderText(20, pressure), pressure.low);
  assert.equal(personalitySliderText(50, pressure), pressure.mid);
  assert.equal(personalitySliderText(80, pressure), pressure.high);
});

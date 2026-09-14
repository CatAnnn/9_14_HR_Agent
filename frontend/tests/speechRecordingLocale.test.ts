import assert from 'node:assert/strict';
import test from 'node:test';

import type { SessionLocale } from '../src/types/domain.ts';
import {
  normalizeSpeechRecordingLocale,
  speechRecognitionLanguage,
  shouldCancelRecordingForLanguageChange,
} from '../src/utils/speechRecordingLocale.ts';

test('normalizes application and browser speech language aliases', () => {
  const cases = [
    ['en-US', 'en'],
    ['en', 'en'],
    ['de-DE', 'de'],
    ['de', 'de'],
    ['ja-JP', 'ja'],
    ['ja', 'ja'],
    ['zh', 'zh-CN'],
    ['zh-CN', 'zh-CN'],
    ['zh-Hans', 'zh-CN'],
    ['unsupported', 'zh-CN'],
  ] as const;

  for (const [input, expected] of cases) {
    assert.equal(normalizeSpeechRecordingLocale(input), expected);
  }
});

test('uses locale-specific browser speech recognition language tags', () => {
  assert.deepEqual(
    (['zh-CN', 'en', 'de', 'ja'] satisfies SessionLocale[]).map(speechRecognitionLanguage),
    ['zh-CN', 'en-US', 'de-DE', 'ja-JP'],
  );
});

test('cancels only an active recording whose language is now stale', () => {
  assert.equal(shouldCancelRecordingForLanguageChange('zh-CN', 'en', true), true);
  assert.equal(shouldCancelRecordingForLanguageChange('en-US', 'en', true), false);
  assert.equal(shouldCancelRecordingForLanguageChange('de-DE', 'de', true), false);
  assert.equal(shouldCancelRecordingForLanguageChange('de-DE', 'ja', true), true);
  assert.equal(shouldCancelRecordingForLanguageChange('ja-JP', 'ja', true), false);
  assert.equal(shouldCancelRecordingForLanguageChange('ja-JP', 'zh-CN', true), true);
  assert.equal(shouldCancelRecordingForLanguageChange('zh-CN', 'en', false), false);
  assert.equal(shouldCancelRecordingForLanguageChange(null, 'en', true), false);
});

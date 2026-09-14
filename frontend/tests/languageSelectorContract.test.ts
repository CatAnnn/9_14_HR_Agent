import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { GERMAN_TEXT } from '../src/i18n/messages/de.ts';
import { JAPANESE_TEXT } from '../src/i18n/messages/ja.ts';
import {
  normalizeAppLanguage,
  SUPPORTED_LANGUAGES,
} from '../src/i18n/languageRuntime.ts';

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8');
}

const selectorConsumers = [
  '../src/pages/LoginPage.tsx',
  '../src/pages/RegisterPage.tsx',
  '../src/components/LandingExperience.tsx',
  '../src/components/AccountActions.tsx',
  '../src/pages/AdminTestRunnerPage.tsx',
  '../src/pages/AdminTestWorkflowPage.tsx',
];

test('language selector renders the complete supported-locale catalog', () => {
  const selector = source('../src/components/LanguageSelector.tsx');
  const runtime = source('../src/i18n/languageRuntime.ts');

  assert.match(runtime, /SUPPORTED_LANGUAGES = \['zh-CN', 'en', 'de', 'ja'\] as const/u);
  assert.match(selector, /SUPPORTED_LANGUAGES\.map\(\(locale\)/u);
  assert.match(
    selector,
    /<option key=\{locale\} value=\{locale\} lang=\{locale\}>\{languageName\(locale\)\}<\/option>/u,
  );
  assert.match(selector, /normalizeAppLanguage\(event\.target\.value\)/u);
  assert.match(selector, /aria-label=\{translate\('语言', 'Language'\)\}/u);
});

test('landing language selector uses a designed accessible popover instead of the native menu', () => {
  const selector = source('../src/components/LanguageSelector.tsx');
  const landingHeader = source('../src/components/LandingExperience.tsx');

  assert.match(landingHeader, /<LanguageSelector[\s\S]*?variant="popover"[\s\S]*?onOpen=\{closeNavigation\}/u);
  assert.match(selector, /const menuId = `\$\{useId\(\)\}-language-menu`/u);
  assert.match(selector, /aria-haspopup="listbox"/u);
  assert.match(selector, /aria-expanded=\{menuOpen\}/u);
  assert.match(selector, /role="listbox"/u);
  assert.match(selector, /role="option"/u);
  assert.match(selector, /aria-selected=\{selected\}/u);
  assert.match(selector, /SUPPORTED_LANGUAGES\.map\(\(locale, index\)/u);
  assert.match(selector, /closeOnPointerDown/u);
  assert.match(selector, /event\.key === 'Escape'/u);
  assert.match(selector, /event\.key === 'ArrowDown'/u);
  assert.match(selector, /event\.key === 'Home'/u);
  assert.match(selector, /triggerRef\.current\?\.focus\(\)/u);
  assert.match(selector, /'zh-CN': '\/assets\/brand\/flag-zh\.svg'/u);
  assert.match(selector, /en: '\/assets\/brand\/flag-en\.svg'/u);
  assert.match(selector, /de: '\/assets\/brand\/flag-de\.svg'/u);
  assert.match(selector, /ja: '\/assets\/brand\/flag-ja\.svg'/u);
  assert.match(selector, /className="language-selector-option-flag"/u);
  assert.doesNotMatch(selector, /language-selector-option-code/u);
});

test('browser locale aliases normalize to one of the four supported languages', () => {
  assert.deepEqual(SUPPORTED_LANGUAGES, ['zh-CN', 'en', 'de', 'ja']);
  assert.equal(normalizeAppLanguage('zh-Hans-CN'), 'zh-CN');
  assert.equal(normalizeAppLanguage('en-US'), 'en');
  assert.equal(normalizeAppLanguage('de-DE'), 'de');
  assert.equal(normalizeAppLanguage('ja-JP'), 'ja');
  assert.equal(normalizeAppLanguage('fr-FR'), null);
});

test('language context does not expose a legacy English-versus-other-language switch', () => {
  assert.doesNotMatch(source('../src/i18n/LanguageContext.tsx'), /\bisEnglish\b/u);
});

test('every global language switch entry point uses the four-language selector', () => {
  for (const path of selectorConsumers) {
    const consumer = source(path);
    assert.match(consumer, /import \{ LanguageSelector \}/u, path);
    assert.match(consumer, /<LanguageSelector/u, path);
    assert.doesNotMatch(consumer, /toggleLanguage/u, path);
  }
});

test('German and Japanese catalogs translate critical account and workflow chrome', () => {
  const requiredKeys = [
    '语言',
    '个人资料',
    '退出登录',
    '工作台',
    '开始准备',
    '员工信息',
    '沟通意图',
    '人格与诉求',
    '谈前指导',
    '多轮预演',
    '复盘报告',
  ];

  for (const key of requiredKeys) {
    assert.ok(GERMAN_TEXT[key]?.trim(), `missing German translation: ${key}`);
    assert.ok(JAPANESE_TEXT[key]?.trim(), `missing Japanese translation: ${key}`);
    assert.notEqual(GERMAN_TEXT[key], key, `untranslated German key: ${key}`);
    assert.notEqual(JAPANESE_TEXT[key], key, `untranslated Japanese key: ${key}`);
  }
});

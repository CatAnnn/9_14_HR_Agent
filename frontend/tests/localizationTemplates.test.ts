import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import test from 'node:test';

import { formatTranslationTemplate } from '../src/i18n/languageRuntime.ts';
import { GERMAN_TEXT } from '../src/i18n/messages/de.ts';
import { JAPANESE_TEXT } from '../src/i18n/messages/ja.ts';

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8');
}

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.tsx?$/u.test(name) ? [path] : [];
  });
}

function literalTranslationKeys(): Set<string> {
  const keys = new Set<string>();
  const root = fileURLToPath(new URL('../src', import.meta.url));
  const patterns = [
    /\btranslate(?:Template)?(?:\?\.)?\(\s*'([^']+)'/gsu,
    /\btranslate(?:Template)?(?:\?\.)?\(\s*"([^"]+)"/gsu,
  ];
  for (const path of sourceFiles(root)) {
    const content = readFileSync(path, 'utf8');
    for (const pattern of patterns) {
      for (const match of content.matchAll(pattern)) keys.add(match[1]);
    }
  }
  return keys;
}

function singleArgumentTranslationKeys(): Set<string> {
  const keys = new Set<string>();
  const root = fileURLToPath(new URL('../src', import.meta.url));
  const patterns = [
    /\btranslate(?:\?\.)?\(\s*'([^']+)'\s*,?\s*\)/gsu,
    /\btranslate(?:\?\.)?\(\s*"([^"]+)"\s*,?\s*\)/gsu,
  ];
  for (const path of sourceFiles(root)) {
    const content = readFileSync(path, 'utf8');
    for (const pattern of patterns) {
      for (const match of content.matchAll(pattern)) keys.add(match[1]);
    }
  }
  return keys;
}

function placeholders(value: string): string[] {
  return [...value.matchAll(/\{([a-z][a-z0-9_]*)\}/giu)]
    .map((match) => match[1])
    .sort();
}

const templateConsumers = [
  '../src/components/EmotionTrajectoryChart.tsx',
  '../src/components/EmployeeProfileCard.tsx',
  '../src/components/KnowledgeLinkedText.tsx',
  '../src/components/LandingExperience.tsx',
  '../src/pages/AdminTestWorkflowPage.tsx',
  '../src/pages/EbookDetailPage.tsx',
  '../src/pages/EbookReaderPage.tsx',
  '../src/pages/ResourcePage.tsx',
  '../src/pages/SolutionsPage.tsx',
  '../src/pages/ToolkitPage.tsx',
  '../src/pages/steps/IntentStep.tsx',
  '../src/pages/steps/ProfileStep.tsx',
  '../src/pages/steps/RehearsalStep.tsx',
  '../src/pages/steps/ReportStep.tsx',
] as const;

const requiredTemplateKeys = [
  '员工情绪在 {count} 个经理对话轮次中的愉快程度与激动程度变化轨迹',
  '第 {round} 轮',
  '{label}维度',
  '{label}列表',
  '第 {turn} 轮发言人',
  '第 {turn} 轮内容',
  '删除第 {turn} 轮',
  '第 {index} 维：{title}，{summary}',
  '约 {minutes} 分钟阅读',
  '正在完成最终转写（{completed}/{total}）',
  '刷新后无法恢复文件“{filename}”，请点击上传按钮重新选择。',
  '{employee}的消息',
  '评级 {rating}',
  '修改会话背景（已补充 {count} 条）',
  '等待重试的追加消息：{count} 条',
  '已追加 {count} 条，将在当前回复后依次发送',
  '背景信息 {index}',
  '会话背景 {index}',
  '删除背景信息 {index}',
  '{name} {score} 分，满分 5 分',
  '{name}暂无分数',
  '查看{name}评分依据',
] as const;

const profileSystemLabels = [
  '关键目标',
  '目标说明',
  'Action Plan / 目标行动计划',
  'Bottom / 保底值',
  'Meet Expectation / 达标值',
  'Challenging / 挑战值',
  '评级',
] as const;

test('translation templates replace known values and preserve unknown placeholders', () => {
  assert.equal(
    formatTranslationTemplate('Runde {round}: {name} ({missing})', { round: 3, name: 'Mika' }),
    'Runde 3: Mika ({missing})',
  );
});

test('dynamic system UI uses stable template keys rather than runtime translation keys', () => {
  for (const path of templateConsumers) {
    assert.doesNotMatch(source(path), /translate\(\s*`/u, path);
  }
});

test('German and Japanese catalogs cover workflow templates and generated goal labels', () => {
  for (const key of [...requiredTemplateKeys, ...profileSystemLabels]) {
    assert.ok(GERMAN_TEXT[key]?.trim(), `missing German translation: ${key}`);
    assert.ok(JAPANESE_TEXT[key]?.trim(), `missing Japanese translation: ${key}`);
    assert.notEqual(GERMAN_TEXT[key], key, `untranslated German key: ${key}`);
    assert.notEqual(JAPANESE_TEXT[key], key, `untranslated Japanese key: ${key}`);
  }
});

test('every literal UI translation key has German and Japanese text with matching placeholders', () => {
  for (const key of literalTranslationKeys()) {
    const german = GERMAN_TEXT[key];
    const japanese = JAPANESE_TEXT[key];
    assert.ok(german?.trim(), `missing German translation: ${key}`);
    assert.ok(japanese?.trim(), `missing Japanese translation: ${key}`);
    assert.deepEqual(placeholders(german), placeholders(key), `German placeholders differ: ${key}`);
    assert.deepEqual(placeholders(japanese), placeholders(key), `Japanese placeholders differ: ${key}`);
  }
});

test('every single-argument UI translation has an English catalog fallback', () => {
  const languageContext = source('../src/i18n/LanguageContext.tsx');
  for (const key of singleArgumentTranslationKeys()) {
    assert.ok(languageContext.includes(`'${key}':`), `missing English catalog key: ${key}`);
  }
});

test('administrator test samples are native in all four supported languages', () => {
  const adminTestSource = source('../src/pages/AdminTestWorkflowPage.tsx');
  assert.match(adminTestSource, /de: SAMPLE_CONVERSATION_GERMAN/u);
  assert.match(adminTestSource, /ja: SAMPLE_CONVERSATION_JAPANESE/u);
  assert.doesNotMatch(adminTestSource, /de: SAMPLE_CONVERSATION_ENGLISH/u);
  assert.doesNotMatch(adminTestSource, /ja: SAMPLE_CONVERSATION_ENGLISH/u);
});

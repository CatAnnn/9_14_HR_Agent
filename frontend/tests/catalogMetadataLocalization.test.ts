import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
import { toolkitPageList, toolkitToolVisuals } from '../src/content/toolkit-content.ts';
import { GERMAN_TEXT } from '../src/i18n/messages/de.ts';
import { JAPANESE_TEXT } from '../src/i18n/messages/ja.ts';
import {
  KNOWLEDGE_SCOPE_ENGLISH_LABELS,
  KNOWLEDGE_SCOPE_LABELS,
} from '../src/utils/knowledgeCitations.ts';

const sourceUrl = (path: string) => new URL(`../src/${path}`, import.meta.url);
const readSource = (path: string) => readFileSync(sourceUrl(path), 'utf8');
const parseSource = (path: string) => ts.createSourceFile(
  path,
  readSource(path),
  ts.ScriptTarget.Latest,
  true,
  path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
);

const searchSource = readSource('components/LandingSiteSearchPanel.tsx');
const solutionsSource = readSource('pages/SolutionsPage.tsx');
const toolkitSource = readSource('pages/ToolkitPage.tsx');
const resourceBookSource = readSource('content/resource-book-content.ts');
const ebookCatalog = JSON.parse(readFileSync(
  new URL('../../Ebook/catalog.json', import.meta.url),
  'utf8',
)) as { items: { id: string; title: string; summary: string }[] };

type TextPair = { source: string; english: string; label: string };

function propertyName(node: ts.PropertyName): string | undefined {
  if (ts.isIdentifier(node) || ts.isStringLiteral(node) || ts.isNumericLiteral(node)) {
    return node.text;
  }
  return undefined;
}

function literalText(node: ts.Expression | undefined): string | undefined {
  return node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
    ? node.text
    : undefined;
}

function objectTextFields(node: ts.ObjectLiteralExpression): Readonly<Record<string, string>> {
  const fields: Record<string, string> = {};
  for (const property of node.properties) {
    if (!ts.isPropertyAssignment(property)) continue;
    const name = propertyName(property.name);
    const value = literalText(property.initializer);
    if (name && value !== undefined) fields[name] = value;
  }
  return fields;
}

function collectLiteralPairs(path: string, sourceField: string, englishField: string): TextPair[] {
  const sourceFile = parseSource(path);
  const pairs: TextPair[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isObjectLiteralExpression(node)) {
      const fields = objectTextFields(node);
      if (fields[sourceField] && fields[englishField]) {
        pairs.push({ source: fields[sourceField], english: fields[englishField], label: `${path}:${sourceField}` });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return pairs;
}

function variableObject(path: string, variableName: string): ts.ObjectLiteralExpression {
  const sourceFile = parseSource(path);
  let result: ts.ObjectLiteralExpression | undefined;
  const visit = (node: ts.Node) => {
    if (
      ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && node.name.text === variableName
      && node.initializer
      && ts.isObjectLiteralExpression(node.initializer)
    ) {
      result = node.initializer;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  assert.ok(result, `Expected ${variableName} in ${path}`);
  return result;
}

function objectStringRecord(node: ts.ObjectLiteralExpression): Readonly<Record<string, string>> {
  const result: Record<string, string> = {};
  for (const property of node.properties) {
    if (!ts.isPropertyAssignment(property)) continue;
    const name = propertyName(property.name);
    const value = literalText(property.initializer);
    if (name && value !== undefined) result[name] = value;
  }
  return result;
}

function nestedStringRecord(node: ts.ObjectLiteralExpression, valueField: string): Readonly<Record<string, string>> {
  const result: Record<string, string> = {};
  for (const property of node.properties) {
    if (!ts.isPropertyAssignment(property) || !ts.isObjectLiteralExpression(property.initializer)) continue;
    const name = propertyName(property.name);
    const value = objectTextFields(property.initializer)[valueField];
    if (name && value) result[name] = value;
  }
  return result;
}

function assertNativePair({ source, english, label }: TextPair) {
  assert.ok(source.trim(), `${label} source must not be empty`);
  assert.ok(english?.trim(), `${label} English fallback must not be empty`);
  for (const [language, catalog] of [
    ['de', GERMAN_TEXT],
    ['ja', JAPANESE_TEXT],
  ] as const) {
    const localized = catalog[source];
    assert.ok(localized?.trim(), `${label} is missing a ${language} catalog entry: ${source}`);
    assert.notEqual(localized, source, `${label} still renders the Chinese source in ${language}`);
    assert.notEqual(localized, english, `${label} still renders the English fallback in ${language}`);
  }
}

test('solution and toolkit directory summaries have native German and Japanese metadata', () => {
  const summaryPairs = collectLiteralPairs('content/toolkit-content.ts', 'summary', 'summaryEnglish');
  assert.equal(summaryPairs.length, 19);
  summaryPairs.forEach(assertNativePair);

  assert.match(solutionsSource, /translate\(page\.summary, page\.summaryEnglish\)/u);
  assert.match(toolkitSource, /translate\(tool\.summary, tool\.summaryEnglish\)/u);
  assert.match(toolkitSource, /translate\(item\.summary, item\.summaryEnglish\)/u);
  assert.match(toolkitSource, /Record<ToolkitPageConfig\['slug'\], \{ title: string; description: string \}>/u);
  assert.doesNotMatch(toolkitSource, /english\?\.(?:title|description)/u);
});

test('toolkit directory shell projects native category metadata and accessible image text', () => {
  const englishCatalog = objectStringRecord(variableObject('i18n/LanguageContext.tsx', 'ENGLISH_TEXT'));
  for (const [sourceField, englishField] of [
    ['navLabel', 'navLabelEnglish'],
    ['title', 'titleEnglish'],
    ['toolsHeading', 'toolsHeadingEnglish'],
  ] as const) {
    const pairs = collectLiteralPairs('content/toolkit-content.ts', sourceField, englishField);
    assert.equal(pairs.length, 4, `Expected four toolkit category ${sourceField} pairs`);
    pairs.forEach(assertNativePair);
  }

  for (const page of toolkitPageList) {
    assertNativePair({
      source: page.heroImageAlt,
      english: englishCatalog[page.heroImageAlt],
      label: `toolkit:${page.slug}:hero-alt`,
    });
  }
  for (const [toolId, visual] of Object.entries(toolkitToolVisuals)) {
    assertNativePair({
      source: visual.imageAlt,
      english: englishCatalog[visual.imageAlt],
      label: `toolkit:${toolId}:image-alt`,
    });
  }

  assert.match(toolkitSource, /translate\(page\.title, page\.titleEnglish\)/u);
  assert.match(toolkitSource, /translate\(page\.summary, page\.summaryEnglish\)/u);
  assert.match(toolkitSource, /translate\(page\.navLabel, page\.navLabelEnglish\)/u);
  assert.match(toolkitSource, /translate\(page\.toolsHeading, page\.toolsHeadingEnglish\)/u);
  assert.match(toolkitSource, /alt=\{translate\(visual\.imageAlt\)\}/u);
  assert.match(toolkitSource, /const title = tool\?\.title \?\? translate\(page\.title/u);
});

test('navigation and resource directory metadata are native in German and Japanese', () => {
  const navigationPairs = collectLiteralPairs(
    'content/home-experience-content.ts',
    'description',
    'descriptionEnglish',
  );
  assert.equal(navigationPairs.length, 2);
  navigationPairs.forEach(assertNativePair);

  const englishDescriptions = objectStringRecord(variableObject(
    'content/home-experience-content.ts',
    'RESOURCE_MODULE_ENGLISH_DESCRIPTIONS',
  ));
  const dimensionSource = parseSource('content/performance-management-content.ts');
  const dimensionPairs: TextPair[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isObjectLiteralExpression(node)) {
      const fields = objectTextFields(node);
      if (fields.slug && fields.title && fields.englishTitle && fields.summary && englishDescriptions[fields.slug]) {
        dimensionPairs.push({ source: fields.title, english: fields.englishTitle, label: `resource:${fields.slug}:title` });
        dimensionPairs.push({ source: fields.summary, english: englishDescriptions[fields.slug], label: `resource:${fields.slug}:summary` });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(dimensionSource);
  assert.equal(dimensionPairs.length, 10);
  dimensionPairs.forEach(assertNativePair);

  const altPairs = collectLiteralPairs('content/performance-management-presentation.ts', 'alt', 'altEnglish');
  assert.equal(altPairs.length, 5);
  altPairs.forEach(assertNativePair);
});

test('site-search fixed metadata uses stable keys with native catalog values', () => {
  assert.doesNotMatch(searchSource, /title:\s*`\$\{dimension\.title\}/u);
  assert.doesNotMatch(searchSource, /toLocaleLowerCase\('zh-CN'\)/u);
  assert.match(searchSource, /translate\(item\.title, item\.titleEnglish\)/u);
  assert.match(searchSource, /translate\(item\.summary, item\.summaryEnglish\)/u);
  assert.match(searchSource, /translate\(item\.typeLabel, item\.typeLabelEnglish\)/u);
  assert.match(searchSource, /titleEnglish:\s*page\.titleEnglish/u);

  const inlineSearchPairs = [
    ...collectLiteralPairs('components/LandingSiteSearchPanel.tsx', 'summary', 'summaryEnglish'),
    ...collectLiteralPairs('components/LandingSiteSearchPanel.tsx', 'typeLabel', 'typeLabelEnglish'),
  ];
  assert.ok(inlineSearchPairs.length >= 7);
  inlineSearchPairs.forEach(assertNativePair);

  const englishCatalog = objectStringRecord(variableObject('i18n/LanguageContext.tsx', 'ENGLISH_TEXT'));
  for (const source of [
    '首页',
    '管理沟通工作台',
    '解决方案',
    '资源中心',
    '结合员工档案、沟通意图、谈前指导与多轮预演，准备下一次管理对话。',
    '从战略分析、教练目标、反馈评估到发展框架，查找可直接应用的方法。',
  ]) {
    assertNativePair({ source, english: englishCatalog[source], label: 'site-search:fixed' });
  }
});

test('site-search toolkit category titles project to native German and Japanese', () => {
  for (const page of toolkitPageList) {
    assertNativePair({
      source: page.title,
      english: page.titleEnglish,
      label: `site-search:${page.slug}:title`,
    });
  }
});

test('knowledge citation scope labels project through one stable localized label set', () => {
  const englishCatalog = objectStringRecord(variableObject('i18n/LanguageContext.tsx', 'ENGLISH_TEXT'));
  const scopeLabels = [...new Set([
    ...Object.values(KNOWLEDGE_SCOPE_LABELS),
    '知识资料',
  ])];
  for (const source of scopeLabels) {
    const english = KNOWLEDGE_SCOPE_ENGLISH_LABELS[source];
    assert.ok(english, `Missing English scope label for ${source}`);
    if (source === 'Career Elements') {
      assert.equal(english, source, 'The formal Career Elements name must remain unchanged');
      continue;
    }
    assert.equal(englishCatalog[source], english, `English catalog drift for scope ${source}`);
    assertNativePair({ source, english, label: `knowledge-scope:${source}` });
  }

  const linkedTextSource = readSource('components/KnowledgeLinkedText.tsx');
  const printAppendixSource = readSource('components/KnowledgeCitationPrintAppendix.tsx');
  assert.match(linkedTextSource, /KNOWLEDGE_SCOPE_ENGLISH_LABELS\[scopeLabel\]/u);
  assert.match(printAppendixSource, /KNOWLEDGE_SCOPE_ENGLISH_LABELS\[source\.scopeLabel\]/u);
});

test('ebook search titles and summaries have native German and Japanese metadata', () => {
  const englishSource = variableObject('content/resource-book-content.ts', 'EBOOK_SEARCH_ENGLISH_BY_ID');
  const englishById = nestedStringRecord(englishSource, 'title');
  const englishSummaryById = nestedStringRecord(englishSource, 'summary');

  for (const book of ebookCatalog.items) {
    assert.ok(englishById[book.id], `Missing English title for ${book.id}`);
    assert.ok(englishSummaryById[book.id], `Missing English summary for ${book.id}`);
    assertNativePair({ source: book.title, english: englishById[book.id], label: `ebook:${book.id}:title` });
    assertNativePair({ source: book.summary, english: englishSummaryById[book.id], label: `ebook:${book.id}:summary` });
    assert.match(resourceBookSource, new RegExp(`['"]${book.id}['"]:\\s*\\{`, 'u'));
  }
});

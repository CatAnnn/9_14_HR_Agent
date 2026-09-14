import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

import { GERMAN_TEXT } from '../src/i18n/messages/de.ts';
import { JAPANESE_TEXT } from '../src/i18n/messages/ja.ts';
import { routeDocumentMetadata } from '../src/utils/routeDocumentMetadata.ts';

const sourceUrl = (path: string) => new URL(`../src/${path}`, import.meta.url);
const readSource = (path: string) => readFileSync(sourceUrl(path), 'utf8');

function parseSource(path: string): ts.SourceFile {
  return ts.createSourceFile(
    path,
    readSource(path),
    ts.ScriptTarget.Latest,
    true,
    path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}

function findFunction(path: string, name: string): ts.FunctionDeclaration {
  const source = parseSource(path);
  let result: ts.FunctionDeclaration | undefined;
  const visit = (node: ts.Node) => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) result = node;
    ts.forEachChild(node, visit);
  };
  visit(source);
  assert.ok(result, `Expected function ${name} in ${path}`);
  return result;
}

function assignmentTargets(node: ts.Node): Set<string> {
  const targets = new Set<string>();
  const visit = (child: ts.Node) => {
    if (
      ts.isBinaryExpression(child)
      && child.operatorToken.kind === ts.SyntaxKind.EqualsToken
    ) {
      targets.add(child.left.getText());
    }
    ts.forEachChild(child, visit);
  };
  visit(node);
  return targets;
}

function callFirstArguments(path: string, functionName: string): string[] {
  const source = parseSource(path);
  const argumentsFound: string[] = [];
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === functionName
    ) {
      const argument = node.arguments[0];
      if (argument && (ts.isStringLiteral(argument) || ts.isNoSubstitutionTemplateLiteral(argument))) {
        argumentsFound.push(argument.text);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return argumentsFound;
}

function assertNativeMetadata(source: string, english: string): void {
  for (const [language, catalog] of [
    ['de', GERMAN_TEXT],
    ['ja', JAPANESE_TEXT],
  ] as const) {
    const localized = catalog[source];
    assert.ok(localized?.trim(), `Missing ${language} route title: ${source}`);
    assert.notEqual(localized, source, `${language} route title still uses Chinese: ${source}`);
    assert.notEqual(localized, english, `${language} route title still uses English fallback: ${source}`);
  }
}

test('stable public, authentication, admin, and workspace routes resolve native titles', () => {
  const routes = [
    '/',
    '/login',
    '/register/',
    '/solutions',
    '/resources/',
    '/app/introduction',
    '/app',
    '/app/report',
    '/admin',
    '/admin/usage/session/report',
    '/admin/test-workflow',
    '/admin/test-workflow/profile',
  ] as const;

  for (const route of routes) {
    const metadata = routeDocumentMetadata(route);
    assert.ok(metadata, `Missing route metadata for ${route}`);
    assertNativeMetadata(metadata.title, metadata.titleEnglish);
  }
});

test('dynamic detail routes defer to their content-aware page metadata', () => {
  for (const route of [
    '/solutions/strategy-analysis/swot-analysis',
    '/resources/performance-management/overview',
    '/resource/book/example-book',
    '/resource/book/example-book/read',
  ]) {
    assert.equal(routeDocumentMetadata(route), undefined, route);
  }
});

test('the app synchronizes the document language and stable route title', () => {
  const metadataFunction = findFunction('App.tsx', 'RouteDocumentMetadata');
  const targets = assignmentTargets(metadataFunction);
  assert.ok(targets.has('document.documentElement.lang'));
  assert.ok(targets.has('document.documentElement.dataset.language'));
  assert.ok(targets.has('document.title'));

  const source = metadataFunction.getText();
  assert.match(source, /routeDocumentMetadata\(location\.pathname\)/u);
  assert.match(source, /translate\(metadata\.title, metadata\.titleEnglish\)/u);
});

test('dynamic detail pages translate content-aware document titles with stable templates', () => {
  for (const path of [
    'pages/ToolkitPage.tsx',
    'pages/PerformanceManagementDetailPage.tsx',
    'pages/EbookDetailPage.tsx',
  ]) {
    assert.ok(
      callFirstArguments(path, 'translateTemplate').includes('{title} | Performance Feedback'),
      `${path} must use the stable detail-title template`,
    );
    assert.ok(assignmentTargets(parseSource(path)).has('document.title'), `${path} must set document.title`);
  }

  assert.ok(
    callFirstArguments('pages/EbookReaderPage.tsx', 'translateTemplate').includes('正在阅读 {title}'),
  );
  assert.ok(assignmentTargets(parseSource('pages/EbookReaderPage.tsx')).has('document.title'));
  assertNativeMetadata('{title} | Performance Feedback', '{title} | Performance Feedback');
  assertNativeMetadata('正在阅读 {title}', 'Reading {title}');
});

test('index.html provides a meaningful Chinese fallback before React starts', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.match(html, /<html lang="zh-CN">/u);
  assert.match(html, /<title>绩效反馈与对话预演<\/title>/u);
});

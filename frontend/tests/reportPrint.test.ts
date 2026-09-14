import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

function ruleBody(source: string, selector: string): string {
  const selectorIndex = source.lastIndexOf(selector);
  assert.notEqual(selectorIndex, -1, `Missing CSS selector: ${selector}`);
  const openingBrace = source.indexOf('{', selectorIndex);
  const closingBrace = source.indexOf('}', openingBrace);
  assert.notEqual(openingBrace, -1, `Missing CSS body: ${selector}`);
  assert.notEqual(closingBrace, -1, `Unclosed CSS body: ${selector}`);
  return source.slice(openingBrace + 1, closingBrace);
}

test('coach report print layout starts on page one and allows long cards to paginate', () => {
  const globalStyles = readFileSync(
    new URL('../src/styles/global.css', import.meta.url),
    'utf8',
  );
  const referenceStyles = readFileSync(
    new URL('../src/styles/reference-v8.css', import.meta.url),
    'utf8',
  );
  const reportSource = readFileSync(
    new URL('../src/pages/steps/ReportStep.tsx', import.meta.url),
    'utf8',
  );
  const workflowSource = readFileSync(
    new URL('../src/context/WorkflowContext.tsx', import.meta.url),
    'utf8',
  );
  const marker = '/* Report-only print pagination.';
  const markerIndex = referenceStyles.lastIndexOf(marker);

  assert.notEqual(markerIndex, -1, 'Missing final report print stylesheet');
  const printStyles = referenceStyles.slice(markerIndex);
  const viewportRule = ruleBody(printStyles, 'html:has(#screen-report),');
  const layoutRule = ruleBody(
    printStyles,
    'body:has(#screen-report) .app-shell,',
  );
  const cardRule = ruleBody(printStyles, '#screen-report .dimension-report {');
  const bodyRule = ruleBody(printStyles, '#screen-report .dimension-report-body {');

  assert.match(viewportRule, /height:\s*auto !important/);
  assert.match(viewportRule, /min-height:\s*0 !important/);
  assert.match(viewportRule, /overflow:\s*visible !important/);
  assert.match(layoutRule, /display:\s*block !important/);
  assert.match(layoutRule, /transform:\s*none !important/);
  assert.match(layoutRule, /animation:\s*none !important/);
  assert.match(cardRule, /break-inside:\s*auto !important/);
  assert.match(cardRule, /page-break-inside:\s*auto !important/);
  assert.match(cardRule, /overflow:\s*visible !important/);
  assert.doesNotMatch(cardRule, /break-inside:\s*avoid/);
  assert.match(bodyRule, /display:\s*block !important/);
  assert.match(
    printStyles,
    /#screen-report \.report-page-actions,[\s\S]*?display:\s*none !important/,
  );
  assert.doesNotMatch(
    globalStyles,
    /\.dimension-report\s*\{\s*break-inside:\s*avoid;\s*\}/,
  );
  assert.match(reportSource, /className="report-export-button"/);
  assert.match(reportSource, /<KnowledgeCitationPrintAppendix citations=\{reportCitations\} \/>/);
  assert.match(
    printStyles,
    /#screen-report \.knowledge-citation-print-appendix\s*\{[^}]*display:\s*block !important/s,
  );
  assert.match(
    printStyles,
    /#screen-report \.knowledge-reference-trigger\.is-related-source\s*\{[^}]*display:\s*none !important/s,
  );
  assert.match(workflowSource, /useCallback\(\(\) => window\.print\(\), \[\]\)/);
});

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const knowledgeSource = readFileSync(
  new URL('../src/components/KnowledgeLinkedText.tsx', import.meta.url),
  'utf8',
);
const reportSource = readFileSync(
  new URL('../src/pages/steps/ReportStep.tsx', import.meta.url),
  'utf8',
);
const guidanceSource = readFileSync(
  new URL('../src/pages/steps/GuidanceStep.tsx', import.meta.url),
  'utf8',
);

test('coach report suppresses field-level related-source pills without removing exact citations', () => {
  const reportLinkedTextCount = reportSource.match(/<KnowledgeLinkedText/g)?.length || 0;
  const suppressedCount = reportSource.match(/showRelatedSources=\{false\}/g)?.length || 0;

  assert.equal(reportLinkedTextCount, 8);
  assert.equal(suppressedCount, reportLinkedTextCount);
  assert.match(reportSource, /<KnowledgeCitationPrintAppendix citations=\{reportCitations\} \/>/);
});

test('knowledge text keeps related sources enabled by default for other pages', () => {
  assert.match(knowledgeSource, /showRelatedSources\?: boolean/);
  assert.match(knowledgeSource, /showRelatedSources = true/);
  assert.match(
    knowledgeSource,
    /const visibleRelatedReferences = showRelatedSources \? relatedReferences : \[\]/,
  );
  assert.doesNotMatch(guidanceSource, /showRelatedSources=\{false\}/);
});

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import type { KnowledgeCitation } from '../src/types/domain.ts';
import {
  collectKnowledgeCitationSources,
  matchingKnowledgeCitations,
} from '../src/utils/knowledgeCitations.ts';

function citation(
  scope: string,
  target: string,
  chunkId: string,
): KnowledgeCitation {
  return {
    chunk_id: chunkId,
    source_id: `source-${chunkId}`,
    title: `${scope} source`,
    scope,
    targets: [target],
    anchors: [{
      target,
      highlight_text: '需要核对事实',
      source_quote: `${scope} 的直接依据`,
      source_context: `${scope} 的上下文与直接依据`,
    }],
  };
}

test('keeps every backend citation scope when it is linked to the target', () => {
  const target = 'summary';
  const scopes = [
    'career',
    'culture',
    'development_dialog',
    'emotion',
    'employee',
    'feedback',
    'general',
    'job_level',
    'organization_unit',
    'performance',
    'redline',
  ];
  const citations = scopes.map((scope, index) => citation(scope, target, String(index)));

  assert.deepEqual(
    matchingKnowledgeCitations(citations, target).map((item) => item.scope),
    scopes,
  );
});

test('accepts an anchor-only citation and rejects unrelated or empty evidence', () => {
  const anchorOnly = citation('feedback', 'summary', 'anchor-only');
  anchorOnly.targets = [];
  const unrelated = citation('general', 'other', 'other');
  const empty: KnowledgeCitation = {
    chunk_id: 'empty',
    source_id: 'empty-source',
    title: 'empty',
    scope: 'general',
    targets: ['summary'],
  };

  assert.deepEqual(
    matchingKnowledgeCitations([anchorOnly, unrelated, empty], 'summary'),
    [anchorOnly],
  );
});

test('collects all distinct sources and exact quotes for print output', () => {
  const first = citation('feedback', 'summary', 'one');
  first.anchors?.push({
    target: 'summary',
    highlight_text: '共同规划',
    source_quote: '第二条直接依据',
    source_context: '第二条直接依据所在的上下文',
  });
  const second = citation('redline', 'summary', 'two');

  const sources = collectKnowledgeCitationSources([first, second]);

  assert.equal(sources.length, 2);
  assert.deepEqual(sources[0].quotes, ['feedback 的直接依据', '第二条直接依据']);
  assert.equal(sources[1].scopeLabel, '制度红线');
});

test('renders only validated phrase anchors while preserving legacy related sources', () => {
  const componentSource = readFileSync(
    new URL('../src/components/KnowledgeLinkedText.tsx', import.meta.url),
    'utf8',
  );

  assert.match(componentSource, /Only backend-validated anchors may paint a phrase/);
  assert.doesNotMatch(componentSource, /function legacyCitationMarks/);
  assert.match(componentSource, /visibleRelatedReferences\.length > 0/);
  assert.match(componentSource, /showRelatedSources = true/);
  assert.match(componentSource, /\.slice\(0, MAX_VISIBLE_SOURCES\)/);
  assert.match(componentSource, /<mark>\{normalizeDisplayText\(exactQuote\)\}<\/mark>/);
  assert.match(componentSource, /role="dialog"/);
});

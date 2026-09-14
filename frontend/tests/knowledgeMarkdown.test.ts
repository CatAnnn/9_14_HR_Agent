import assert from 'node:assert/strict';
import test from 'node:test';

import { compileKnowledgeMarkdown } from '../src/utils/knowledgeMarkdown.ts';

test('compiles headings, inline markdown, lists, quotes, and safe links', () => {
  const blocks = compileKnowledgeMarkdown([
    '## **高绩效文化**',
    '- **使命必达**：使用 `G9` 标准',
    '1. *核对*事实',
    '> [查看资料](https://example.com/report)',
  ].join('\n'));

  assert.deepEqual(blocks.map((block) => block.type), [
    'heading',
    'bullet',
    'ordered',
    'quote',
  ]);
  assert.equal(blocks[0].inline[0].type, 'strong');
  assert.equal(blocks[1].inline.some((token) => token.type === 'code'), true);
  assert.equal(blocks[2].marker, '1.');
  assert.equal(blocks[3].inline[0].type, 'link');
});

test('turns HTML and standalone slash separators into lines without splitting codes', () => {
  const blocks = compileKnowledgeMarkdown(
    '**第一项** / 第二项 ／ 第三项<br>G9/SL1 与 WHAT/HOW',
  );

  assert.deepEqual(blocks.map((block) => block.text), [
    '第一项',
    '第二项',
    '第三项',
    'G9/SL1 与 WHAT/HOW',
  ]);
});

test('does not turn unsafe markdown links into clickable links', () => {
  const [block] = compileKnowledgeMarkdown('[内容](javascript:alert(1))');
  assert.equal(block.inline.some((token) => token.type === 'link'), false);
  assert.equal(block.text, '内容');
});

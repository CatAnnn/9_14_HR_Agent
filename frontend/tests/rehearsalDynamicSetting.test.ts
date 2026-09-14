import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../src/pages/steps/RehearsalStep.tsx', import.meta.url),
  'utf8',
);
const styles = readFileSync(
  new URL('../src/styles/reference-v8.css', import.meta.url),
  'utf8',
);

test('places the conversation background prompt before the edit action', () => {
  const row = source.match(
    /<div className="context-fact-dynamic">([\s\S]*?)<\/div>/u,
  )?.[1] ?? '';

  const promptIndex = row.indexOf('是否需要修改或新增当前会话背景？');
  const buttonIndex = row.indexOf('className="context-dynamic-setting"');

  assert.ok(promptIndex >= 0);
  assert.ok(buttonIndex > promptIndex);
  assert.match(row, /type="button"/u);
  assert.match(row, /onClick=\{onEdit\}/u);
  assert.match(row, /translate\('修改', 'Edit'\)/u);
  assert.match(row, /runtimeNotesCount/u);
});

test('keeps the long prompt flexible and the edit action visually restrained', () => {
  assert.match(
    styles,
    /\.context-fact-dynamic\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto/su,
  );
  assert.match(
    styles,
    /\.context-dynamic-prompt\s*\{[^}]*min-width:\s*0[^}]*overflow-wrap:\s*anywhere/su,
  );
  assert.match(
    styles,
    /\.context-dynamic-setting\s*\{[^}]*justify-self:\s*end[^}]*min-width:\s*40px[^}]*border:\s*0 !important[^}]*background:\s*transparent !important[^}]*box-shadow:\s*none !important/su,
  );
});

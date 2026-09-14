import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const reportSource = readFileSync(
  new URL('../src/pages/steps/ReportStep.tsx', import.meta.url),
  'utf8',
);
const globalStyles = readFileSync(
  new URL('../src/styles/global.css', import.meta.url),
  'utf8',
);

test('report strengths label uses the report blue while its content keeps the paragraph color', () => {
  assert.match(
    reportSource,
    /<p className="dimension-strengths">[\s\S]*?<strong className="dimension-strength-label">\{translate\('做得好的地方：', 'What went well: '\)\}<\/strong>/,
  );
  assert.match(
    globalStyles,
    /\.dimension-strength-label\s*\{[^}]*color:\s*var\(--bosch-blue-dark\);[^}]*\}/s,
  );
  assert.match(
    globalStyles,
    /\.dimension-summary,\s*\.dimension-strengths\s*\{[^}]*width:\s*100%;[^}]*max-width:\s*none;[^}]*white-space:\s*normal;/s,
  );
});

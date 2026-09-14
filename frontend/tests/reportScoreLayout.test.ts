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
const referenceStyles = readFileSync(
  new URL('../src/styles/reference-v8.css', import.meta.url),
  'utf8',
);

test('report scores and descriptions share one responsive line', () => {
  assert.match(reportSource, /const displayScore = typeof result\.score === 'number'/);
  assert.match(reportSource, /: scoreDetail\?\.score;/);
  assert.match(reportSource, /className="dimension-score"\s+role="group"/);
  assert.match(reportSource, /className="dimension-heading-top"/);
  assert.match(reportSource, /className="dimension-summary"/);
  assert.match(reportSource, /className="dimension-strengths"/);
  assert.match(reportSource, /className="dimension-score-line"/);
  assert.match(reportSource, /className="dimension-score-value" aria-hidden="true"/);
  assert.match(reportSource, /className=\{hasScore \? undefined : 'is-placeholder'\}>\/5<\/span>/);
  assert.match(reportSource, /\{level && <small>\{level\}<\/small>\}/);
  assert.doesNotMatch(reportSource, /<small title=\{level\}>/);
  assert.match(
    globalStyles,
    /\.dimension-report-header\s*\{[^}]*--dimension-score-width:\s*clamp\(320px,\s*38vw,\s*460px\);[^}]*grid-template-columns:\s*32px minmax\(0,\s*1fr\)/s,
  );
  assert.match(
    globalStyles,
    /\.dimension-heading-top\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\) var\(--dimension-score-width\)/s,
  );
  assert.match(
    globalStyles,
    /\.dimension-score-line\s*\{[^}]*display:\s*flex;[^}]*align-items:\s*center;[^}]*justify-content:\s*flex-end;/s,
  );
  assert.match(
    globalStyles,
    /\.dimension-score-value\s*\{[^}]*flex:\s*0 0 auto;[^}]*display:\s*flex;[^}]*align-items:\s*baseline;/s,
  );
  assert.match(
    globalStyles,
    /\.dimension-score-value > span\.is-placeholder\s*\{[^}]*visibility:\s*hidden;/s,
  );
  assert.match(
    globalStyles,
    /\.dimension-score small\s*\{[^}]*white-space:\s*normal;[^}]*overflow-wrap:\s*anywhere;/s,
  );
});

test('summary and strengths reclaim the full content width below the score line', () => {
  const headingTopIndex = reportSource.indexOf('className="dimension-heading-top"');
  const scoreIndex = reportSource.indexOf('className="dimension-score"', headingTopIndex);
  const summaryIndex = reportSource.indexOf('className="dimension-summary"', scoreIndex);
  const strengthsIndex = reportSource.indexOf('className="dimension-strengths"', summaryIndex);

  assert.ok(headingTopIndex >= 0);
  assert.ok(scoreIndex > headingTopIndex);
  assert.ok(summaryIndex > scoreIndex);
  assert.ok(strengthsIndex > summaryIndex);
  assert.match(
    globalStyles,
    /\.dimension-summary,\s*\.dimension-strengths\s*\{[^}]*width:\s*100%;[^}]*max-width:\s*none;[^}]*white-space:\s*normal;/s,
  );
});

test('score basis opens from an accessible hover and focus control', () => {
  assert.match(reportSource, /className="dimension-score-basis-trigger"/);
  assert.match(reportSource, /aria-describedby=\{basisTooltipId\}/);
  assert.match(reportSource, /className="dimension-score-basis-tooltip"/);
  assert.match(reportSource, /role="tooltip"/);
  assert.match(reportSource, /<span>\{basis\}<\/span>/);
  assert.match(
    globalStyles,
    /\.dimension-score-basis:hover \.dimension-score-basis-tooltip,[\s\S]*?\.dimension-score-basis:focus-within \.dimension-score-basis-tooltip\s*\{[^}]*opacity:\s*1;[^}]*visibility:\s*visible;/s,
  );
  assert.match(
    globalStyles,
    /\.dimension-score-basis-trigger:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--bosch-blue-dark\)/s,
  );
});

test('print keeps the inline score and restores the static rating basis', () => {
  assert.match(
    referenceStyles,
    /#screen-report \.dimension-report-header\s*\{[^}]*grid-template-columns:\s*32px minmax\(0,\s*1fr\)\s*!important;/s,
  );
  assert.match(
    referenceStyles,
    /#screen-report \.dimension-heading-top\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\) var\(--dimension-score-width\)\s*!important;/s,
  );
  assert.match(
    referenceStyles,
    /#screen-report \.dimension-score\s*\{[^}]*grid-column:\s*2\s*!important;[^}]*width:\s*var\(--dimension-score-width\)\s*!important;/s,
  );
  assert.match(
    referenceStyles,
    /#screen-report \.dimension-score-basis\s*\{[^}]*display:\s*none\s*!important;/s,
  );
  assert.match(
    referenceStyles,
    /#screen-report \.dimension-heading \.dimension-basis-print\s*\{[^}]*display:\s*flex\s*!important;/s,
  );
});

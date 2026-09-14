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

test('each report improvement row keeps its finding and suggestion in one paired grid row', () => {
  assert.match(reportSource, /buildReportImprovementRows\(result\)/);
  assert.equal(reportSource.match(/improvementRows\.map/g)?.length, 1);
  assert.equal(
    reportSource.match(/const rawRowTitle = title \|\| issue\?\.title/g)?.length,
    1,
  );
  assert.match(
    reportSource,
    /const localizedBaseTitle = rawRowTitle[\s\S]*?translate\(rawRowTitle, IMPROVEMENT_TITLES_ENGLISH\[rawRowTitle\]\)/,
  );
  assert.match(reportSource, /className="improvement-comparison-header"/);
  assert.match(reportSource, /className="improvement-comparison-list"/);
  assert.match(
    reportSource,
    /className="improvement-comparison-row"[\s\S]*?className="dimension-findings"[\s\S]*?className="dimension-actions"/,
  );
  assert.match(
    reportSource,
    /<section className="dimension-findings"[^>]*>[\s\S]*?className="improvement-row-title"[\s\S]*?className="issue-dimension-copy"/,
  );
  assert.match(
    reportSource,
    /const previousRawRowTitle = previousRow[\s\S]*?const showRowTitle = Boolean\([\s\S]*?rawRowTitle !== previousRawRowTitle[\s\S]*?\)/,
  );
  assert.match(reportSource, /\{showRowTitle && \([\s\S]*?className="improvement-row-title"/);
  assert.match(reportSource, /translateTemplate\([\s\S]*?'\{title\}：需要改进',[\s\S]*?'\{title\}: needs improvement'/);
  assert.match(reportSource, /translateTemplate\([\s\S]*?'\{title\}：具体建议',[\s\S]*?'\{title\}: specific suggestion'/);
  assert.match(reportSource, /translate\('需要改进', 'Needs improvement'\)/);
  assert.match(reportSource, /translate\('具体建议', 'Specific suggestion'\)/);
  assert.match(reportSource, /<p className="original-phrase"><span>\{translate\('经理实际表达', 'Manager’s exact wording'\)\}<\/span>\{phrase\.original\}<\/p>/);
  assert.match(reportSource, /phrase\?\.original\?\.trim\(\)/);
  assert.doesNotMatch(reportSource, /normalizeDisplayText\(phrase\.original\)/);
  assert.match(reportSource, /improvement_points\.\$\{issue\.sourceIndex\}/);
  assert.match(reportSource, /better_phrases\.\$\{phraseSourceIndex \?\? sourceIndex\}\.suggestion/);
  assert.match(reportSource, /translate\('本项未记录可核验的经理原话。', 'No verifiable manager wording was recorded for this item.'\)/);
  assert.match(reportSource, /translate\('本项未返回可执行的具体建议。', 'No actionable specific suggestion was returned for this item.'\)/);
  assert.match(reportSource, /translate\('历史报告未记录该子维度的具体建议。', 'The historical report did not record a specific suggestion for this subdimension.'\)/);
  assert.match(
    reportSource,
    /translateTemplate\(\s*'该维度评分为 \{score\} 分，但评估结果未返回与评分对应的改进问题。'/u,
  );
  assert.doesNotMatch(reportSource, /translate\(rawImprovementEmptyCopy\./u);
  assert.match(reportSource, /\{improvementEmptyCopy\.finding\}/);
  assert.match(reportSource, /\{improvementEmptyCopy\.suggestion\}/);
  assert.doesNotMatch(reportSource, /该维度表现达标/);
  assert.doesNotMatch(reportSource, /该子维度未识别出明确问题/);
});

test('paired report rows use a 40/60 desktop split and stack within each pair on mobile', () => {
  assert.match(
    globalStyles,
    /\.dimension-report-body\s*\{[^}]*--improvement-column-layout:\s*minmax\(0,\s*2fr\)\s+minmax\(0,\s*3fr\)/s,
  );
  assert.match(
    globalStyles,
    /\.improvement-comparison-header,[\s\S]*?\.improvement-comparison-row,[\s\S]*?\.improvement-comparison-empty\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*var\(--improvement-column-layout\)/,
  );
  assert.match(
    globalStyles,
    /\.dimension-actions\s*\{[^}]*border-left:\s*1px\s+solid\s+var\(--border-subtle\)/s,
  );
  assert.match(
    globalStyles,
    /\.original-phrase\s*\{[^}]*background:\s*transparent/s,
  );
  assert.match(
    globalStyles,
    /\.improvement-row-title\s*\{[^}]*margin:\s*0 0 8px;[^}]*padding:\s*0;[^}]*border:\s*0;[^}]*background:\s*transparent;/s,
  );
  assert.doesNotMatch(
    globalStyles.match(/\.improvement-row-title\s*\{[^}]*\}/s)?.[0] || '',
    /grid-column/,
  );
  assert.match(
    globalStyles,
    /@media \(max-width:\s*760px\)[\s\S]*?\.improvement-comparison-header\s*\{\s*display:\s*none;\s*\}/,
  );
  assert.match(
    globalStyles,
    /@media \(max-width:\s*760px\)[\s\S]*?\.improvement-comparison-row,\s*\.improvement-comparison-empty\s*\{\s*grid-template-columns:\s*1fr;/,
  );
  assert.match(
    globalStyles,
    /@media \(max-width:\s*760px\)[\s\S]*?\.improvement-cell-label\s*\{\s*display:\s*block;/,
  );
});

test('print layout preserves paired rows and avoids splitting a pair across pages', () => {
  assert.match(
    referenceStyles,
    /#screen-report \.improvement-comparison-header,[\s\S]*?#screen-report \.improvement-comparison-row,[\s\S]*?#screen-report \.improvement-comparison-empty\s*\{[^}]*grid-template-columns:\s*var\(--improvement-column-layout\)\s*!important;/,
  );
  assert.match(
    referenceStyles,
    /#screen-report \.improvement-comparison-header\s*\{[^}]*display:\s*grid\s*!important;/,
  );
  assert.match(
    referenceStyles,
    /#screen-report \.improvement-cell-label\s*\{[^}]*display:\s*none\s*!important;/,
  );
  assert.match(
    referenceStyles,
    /#screen-report \.improvement-comparison-row,[\s\S]*?\{[^}]*break-inside:\s*avoid-page;[^}]*page-break-inside:\s*avoid;/,
  );
});

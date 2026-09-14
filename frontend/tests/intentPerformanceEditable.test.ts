import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../src/pages/steps/IntentStep.tsx', import.meta.url),
  'utf8',
);
const styles = readFileSync(
  new URL('../src/styles/reference-v8.css', import.meta.url),
  'utf8',
);
const eligibilitySource = readFileSync(
  new URL('../src/utils/intentEligibility.ts', import.meta.url),
  'utf8',
);

test('employee performance makes editable generated content explicit', () => {
  assert.match(source, /translate\('生成后修改', 'Edit after generation'\)/);
  assert.match(source, /translate\('点击文字修改', 'Click the text to edit'\)/);
  assert.match(
    source,
    /translateTemplate\([\s\S]*?'\{goal\}的可编辑内容',[\s\S]*?'Editable content for \{goal\}'/,
  );
  assert.match(source, /intent-performance-editor-shell/);
  assert.match(source, /<PencilLine aria-hidden="true" \/>/);
});

test('employee performance editors have visible hover and focus affordances', () => {
  assert.match(
    styles,
    /#screen-intent \.intent-performance-editor-shell > svg\s*\{[^}]*color:\s*#61747d;[^}]*opacity:\s*\.72;[^}]*transition:/s,
  );
  assert.match(
    styles,
    /#screen-intent \.intent-performance-editor-shell:focus-within > svg\s*\{[^}]*color:\s*#051c2c;[^}]*opacity:\s*1;/s,
  );
  assert.match(
    styles,
    /#screen-intent \.intent-performance-line-editor:focus,[\s\S]*?outline:\s*none;[\s\S]*?background:\s*transparent !important;/s,
  );
  assert.match(styles, /#screen-intent \.intent-performance-editor-shell > svg/);
});

test('manager edits cannot trigger regeneration or duplicate confirmation requests', () => {
  assert.match(
    source,
    /goalsMatch\(cachedDraft\.performance_items, cachedDraft\.locale\)/,
  );
  assert.match(source, /normalizeGeneratedPerformanceItems\([\s\S]*?fallback\.performance_items,[\s\S]*?fallback\.locale,/);
  assert.match(source, /normalizeGeneratedPerformanceItem\([\s\S]*?rawItem,[\s\S]*?sectionIndex,[\s\S]*?draft\.locale,/);
  assert.match(source, /confirmIntent\(performanceContext, submittedItems\)/);
  assert.match(source, /generation_reason: null,/);
  assert.match(source, /isSubmittingRef\.current/);
  assert.match(
    source,
    /员工表现保存失败，已保留当前修改，请稍后重试。/,
  );
  assert.match(
    source,
    /disabled=\{!hasCompleteDraft \|\| isEditorLocked\}/,
  );
});

test('intent eligibility warnings use stable locale templates', () => {
  assert.match(
    source,
    /translateTemplate\(\s*'当前员工为 \{current\}；“\{intent\}”的建议适用范围为 \{expected\}/u,
  );
  assert.match(source, /joinEligibilityValues\(currentValues, language, 'and'\)/u);
  assert.match(source, /joinEligibilityValues\(expectedValues, language, 'or'\)/u);
  assert.doesNotMatch(source, /translate\(\s*eligibility\.message,/u);
  assert.doesNotMatch(eligibilitySource, /message:\s*['"`]/u);
});

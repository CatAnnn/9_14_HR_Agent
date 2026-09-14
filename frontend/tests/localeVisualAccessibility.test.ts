import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8');
}

test('language selector preserves a visible keyboard focus ring', () => {
  const css = source('../src/styles/language-selector.css');
  const focusRule = css.match(/\.language-selector\s*>\s*select:focus-visible\s*\{([^}]*)\}/u)?.[1] || '';

  assert.match(focusRule, /outline:\s*3px solid #007bc0 !important/u);
  assert.match(focusRule, /outline-offset:\s*2px/u);
  assert.doesNotMatch(focusRule, /outline:\s*none/u);
});

test('landing language selector uses a near-transparent adaptive glass surface', () => {
  const css = source('../src/styles/language-selector.css');
  const triggerFocusRule = css.match(/\.language-selector-trigger:focus-visible\s*\{([^}]*)\}/u)?.[1] || '';
  const menuRule = css.match(/\.language-selector-menu\s*\{([^}]*)\}/u)?.[1] || '';
  const optionFocusRules = [...css.matchAll(/\.language-selector-option:focus-visible\s*\{([^}]*)\}/gu)]
    .map((match) => match[1])
    .join('\n');

  assert.match(triggerFocusRule, /outline:\s*3px solid #007bc0 !important/u);
  const selectedRule = css.match(/\.language-selector-option\[aria-selected="true"\]\s*\{([^}]*)\}/u)?.[1] || '';

  assert.match(menuRule, /background:\s*rgba\(255, 255, 255, \.10\)/u);
  assert.match(menuRule, /backdrop-filter:\s*blur\(26px\)/u);
  assert.match(menuRule, /width:\s*max-content/u);
  assert.doesNotMatch(menuRule, /(?:^|\n)\s*width:\s*\d+(?:\.\d+)?(?:px|rem|em)\s*;/u);
  assert.match(menuRule, /left:\s*50%/u);
  assert.match(menuRule, /transform:\s*translateX\(-50%\)/u);
  assert.match(menuRule, /transform-origin:\s*top center/u);
  assert.doesNotMatch(menuRule, /(?:^|\n)\s*right:\s*0\s*;/u);
  assert.doesNotMatch(menuRule, /background:\s*rgba\(3, 21, 34/u);
  assert.match(optionFocusRules, /background:\s*var\(--language-option-focus\)/u);
  assert.match(optionFocusRules, /box-shadow:\s*none !important/u);
  assert.match(selectedRule, /background:\s*transparent/u);
  assert.match(
    css,
    /\.home-header\.is-transparent:not\(\.is-inverse\) \.language-selector-menu\s*\{[\s\S]*?--language-option-active:\s*#051c2c/u,
  );
  assert.doesNotMatch(css, /language-selector-option::before/u);
  assert.doesNotMatch(css, /language-selector-option-code/u);
});

test('Japanese locale overrides the important visual-layer font variable', () => {
  const css = source('../src/styles/reference-v8.css');
  const japaneseRule = css.match(/html\[lang="ja"\]\s*\{([^}]*)\}/u)?.[1] || '';

  assert.match(css, /body\s*\{[^}]*font-family:\s*var\(--apple-font\)\s*!important/su);
  assert.match(japaneseRule, /--apple-font:/u);
  assert.match(japaneseRule, /"Hiragino Sans"/u);
  assert.match(japaneseRule, /"Yu Gothic"/u);
  assert.match(japaneseRule, /"Noto Sans JP"/u);
  assert.doesNotMatch(japaneseRule, /Microsoft YaHei/u);
});

test('expanded German workflow rail supports long labels without changing other locales', () => {
  const css = source('../src/styles/reference-v8.css');

  assert.match(
    css,
    /html\[lang="de"\] \.app-sidebar:not\(\.is-collapsed\) \.step-copy strong\s*\{[^}]*white-space:\s*normal !important[^}]*-webkit-line-clamp:\s*2/su,
  );
  assert.match(
    css,
    /@media \(min-width:\s*1100px\)\s*\{[\s\S]*?html\[lang="de"\] \.app-shell:not\(\.is-sidebar-collapsed\)\s*\{[^}]*grid-template-columns:\s*248px minmax\(0, 1fr\) !important/u,
  );
  assert.match(
    css,
    /html\[lang="de"\] \.app-sidebar:not\(\.is-collapsed\)\s*\{[^}]*width:\s*248px !important/su,
  );
  assert.doesNotMatch(css, /html\[lang="(?:zh-CN|en|ja)"\] \.app-sidebar[^\{]*\.step-copy strong/u);
});

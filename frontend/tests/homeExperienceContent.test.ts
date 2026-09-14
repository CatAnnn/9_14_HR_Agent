import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const contentSource = readFileSync(
  new URL('../src/content/home-experience-content.ts', import.meta.url),
  'utf8',
);
const componentSource = readFileSync(
  new URL('../src/components/LandingExperience.tsx', import.meta.url),
  'utf8',
);
const stylesSource = readFileSync(
  new URL('../src/styles/home-page.css', import.meta.url),
  'utf8',
);

test('homepage hero title is presented as exactly two semantic lines', () => {
  assert.match(contentSource, /titleLines:\s*\['让每一次绩效反馈，',\s*'成为真实成长的起点'\]/);
  assert.match(componentSource, /hero\.titleLines\.map\(\(line\)/);
  assert.match(stylesSource, /\.home-hero-copy h1 > span\s*\{[^}]*display:\s*block;/s);
});

test('homepage hero title keeps the current compact original typography', () => {
  assert.match(
    stylesSource,
    /\.home-hero-copy h1\s*\{[^}]*font-size:\s*48px[^}]*font-weight:\s*500[^}]*line-height:\s*1\.2/s,
  );
  assert.match(
    stylesSource,
    /@media \(max-width:\s*480px\)[\s\S]*?\.home-hero-copy h1\s*\{[^}]*font-size:\s*32px/,
  );
});

test('homepage solution cards use accessible icon-only actions and a refined all-solutions link', () => {
  assert.doesNotMatch(componentSource, /了解这一解决方案|Explore this solution/);
  assert.match(
    componentSource,
    /ariaLabel=\{translateTemplate\('查看 \{title\}', 'Explore \{title\}', \{ title: solution\.title \}\)\}/,
  );
  assert.match(
    componentSource,
    /<span className="home-approach-card-action" aria-hidden="true">\s*<ArrowRight aria-hidden="true" \/>\s*<\/span>/,
  );
  assert.match(
    componentSource,
    /<SmartLink className="home-approach-all-link" href="\/solutions">\s*\{translate\('查看全部解决方案', 'Explore all solutions'\)\}\s*<ArrowRight aria-hidden="true" \/>/,
  );
  assert.match(
    stylesSource,
    /\.home-approach-card-action svg\s*\{[^}]*width:\s*44px;[^}]*height:\s*44px;[^}]*border-radius:\s*50%;/s,
  );
  assert.match(
    stylesSource,
    /\.home-approach-all-link\s*\{[^}]*min-height:\s*56px;[^}]*border-radius:\s*999px;[^}]*transform \.32s/s,
  );
  assert.match(
    stylesSource,
    /@media \(max-width:\s*480px\)[\s\S]*?\.home-approach-all-link\s*\{[^}]*width:\s*100%;/,
  );
});

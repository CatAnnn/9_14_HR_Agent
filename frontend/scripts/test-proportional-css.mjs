import assert from 'node:assert/strict';
import postcss from 'postcss';
import {
  convertPixelsToRem,
  createProportionalRemPlugin,
} from '../postcss.config.mjs';

assert.equal(convertPixelsToRem('16px'), '1rem');
assert.equal(convertPixelsToRem('calc(100% - 24px)'), 'calc(100% - 1.5rem)');
assert.equal(convertPixelsToRem('1px solid #000'), '1px solid #000');
assert.equal(convertPixelsToRem('translateY(-8px)'), 'translateY(-0.5rem)');

const source = `
.sample { font-size: 16px; gap: 24px; border: 1px solid #000; }
@media (max-width: 1180px) { .sample { font-size: 14px; } }
@media (max-width: 760px) { .sample { font-size: 12px; } }
`;

const result = await postcss([createProportionalRemPlugin()]).process(source, {
  from: '/project/frontend/src/styles/sample.css',
});

assert.match(result.css, /font-size:\s*1rem/);
assert.match(result.css, /gap:\s*1\.5rem/);
assert.match(result.css, /border:\s*1px solid/);
assert.match(result.css, /max-width:\s*1180px\) and \(max-width:\s*959px/);
assert.match(result.css, /@media \(max-width:\s*760px\)/);

const scaleSource = await postcss([createProportionalRemPlugin()]).process(
  ':root { font-size: 16px; }',
  { from: '/project/frontend/src/styles/responsive-scale.css' },
);
assert.match(scaleSource.css, /font-size:\s*16px/);

console.log('proportional CSS checks passed');

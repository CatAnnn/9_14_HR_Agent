import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  createExpandedKeySet,
  toggleExpandedKey,
} from '../src/components/ui/accordionState.ts';

const guidanceSource = readFileSync(
  new URL('../src/pages/steps/GuidanceStep.tsx', import.meta.url),
  'utf8',
);
const accordionSource = readFileSync(
  new URL('../src/components/ui/accordion.tsx', import.meta.url),
  'utf8',
);
const accordionStyles = readFileSync(
  new URL('../src/components/ui/accordion.css', import.meta.url),
  'utf8',
);
const globalStyles = readFileSync(
  new URL('../src/styles/global.css', import.meta.url),
  'utf8',
);

test('guidance expandable sentences use the shared accordion API', () => {
  assert.match(guidanceSource, /from '\.\.\/\.\.\/components\/ui\/accordion'/);
  assert.match(
    guidanceSource,
    /<Accordion defaultExpandedKeys=\{\[\]\} className="guidance-point-groups">/,
  );
  assert.match(
    guidanceSource,
    /<AccordionItem className="guidance-point-group" id=\{targetPrefix\}>/,
  );
  assert.match(
    guidanceSource,
    /<AccordionTrigger className="guidance-detail-summary">/,
  );
  assert.match(guidanceSource, /<AccordionContent>/);
  assert.doesNotMatch(guidanceSource, /guidance-detail-state|guidance-detail-reveal/);
});

test('accordion state starts from defaults and toggles multiple items independently', () => {
  const initial = createExpandedKeySet(['shipping']);
  const withReturns = toggleExpandedKey(initial, 'returns');
  const withoutShipping = toggleExpandedKey(withReturns, 'shipping');

  assert.deepEqual([...initial], ['shipping']);
  assert.deepEqual([...withReturns], ['shipping', 'returns']);
  assert.deepEqual([...withoutShipping], ['returns']);
});

test('accordion source uses a native toggle control without nesting knowledge buttons', () => {
  assert.match(accordionSource, /import \{ ChevronDown \} from 'lucide-react'/);
  assert.match(accordionSource, /<button/);
  assert.match(accordionSource, /type="button"/);
  assert.match(accordionSource, /aria-expanded=\{expanded\}/);
  assert.match(accordionSource, /aria-labelledby=\{labelId\}/);
  assert.match(accordionSource, /aria-hidden=\{!expanded\}/);
  assert.match(accordionSource, /cameFromNestedInteractiveElement\(event\)/);
  assert.match(accordionSource, /event\.stopPropagation\(\)/);
  assert.match(
    accordionSource,
    /<span className="ui-accordion-trigger-label" id=\{labelId\}>\{children\}<\/span>\s*<button/,
  );
});

test('accordion styles declare hover underlines, icon rotation, and mobile columns', () => {
  assert.match(
    accordionStyles,
    /\.ui-accordion-trigger:hover > \.ui-accordion-trigger-label\s*\{[^}]*text-decoration-line:\s*underline/s,
  );
  assert.match(
    accordionStyles,
    /\.ui-accordion-trigger:hover > \.ui-accordion-trigger-label \.knowledge-reference-trigger\s*\{[^}]*text-decoration-line:\s*underline\s*!important/s,
  );
  assert.match(
    accordionStyles,
    /\.ui-accordion-trigger\[data-state='open'\] \.ui-accordion-trigger-icon\s*\{[^}]*rotate\(180deg\)/s,
  );
  assert.match(
    globalStyles,
    /@media \(max-width: 520px\)[\s\S]*?#screen-guidance \.guidance-detail-summary\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto/s,
  );
});

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  getAgentIntroductionVariant,
  isWorkflowPageGuideAlwaysShow,
  isWorkflowPageGuideEnabled,
  parseAgentIntroductionVariant,
  parseRuntimeBoolean,
} from '../src/utils/runtimeConfig.ts';

test('parses enabled runtime flag values', () => {
  for (const value of [true, 1, '1', ' true ', 'YES', 'on']) {
    assert.equal(parseRuntimeBoolean(value, false), true);
  }
});

test('parses disabled runtime flag values', () => {
  for (const value of [false, 0, '0', ' false ', 'NO', 'off']) {
    assert.equal(parseRuntimeBoolean(value, true), false);
  }
});

test('uses the explicit fallback for missing or invalid values', () => {
  for (const value of [undefined, null, '', 'enabled', 2, {}]) {
    assert.equal(parseRuntimeBoolean(value, true), true);
    assert.equal(parseRuntimeBoolean(value, false), false);
  }
});

test('uses safe guide defaults outside a browser runtime', () => {
  assert.equal(isWorkflowPageGuideEnabled(), true);
  assert.equal(isWorkflowPageGuideAlwaysShow(), false);
  assert.equal(getAgentIntroductionVariant(), 'classic');
});

test('accepts only the explicit journey introduction variant', () => {
  assert.equal(parseAgentIntroductionVariant('journey'), 'journey');
  assert.equal(parseAgentIntroductionVariant(' JOURNEY '), 'journey');
  for (const value of ['classic', 'current', '', undefined, null, true]) {
    assert.equal(parseAgentIntroductionVariant(value), 'classic');
  }
});

test('wires the always-show env flag through Compose and Nginx runtime config', () => {
  const compose = readFileSync(
    new URL('../../deployment/compose/compose.services.yml', import.meta.url),
    'utf8',
  );
  const nginx = readFileSync(new URL('../nginx.conf', import.meta.url), 'utf8');

  assert.match(compose, /WORKFLOW_PAGE_GUIDE_ALWAYS_SHOW/);
  assert.match(nginx, /workflowPageGuideAlwaysShow: "\$\{WORKFLOW_PAGE_GUIDE_ALWAYS_SHOW\}"/);
  assert.match(compose, /AGENT_INTRODUCTION_VARIANT/);
  assert.match(nginx, /agentIntroductionVariant: "\$\{AGENT_INTRODUCTION_VARIANT\}"/);
});

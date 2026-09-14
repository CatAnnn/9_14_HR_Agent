import assert from 'node:assert/strict';
import test from 'node:test';

import type {
  GuidancePointGroup,
  GuidanceSectionDraft,
  GuidanceSectionKey,
} from '../src/types/domain.ts';
import { buildCompactGuidance } from '../src/utils/compactGuidance.ts';

function section(
  key: GuidanceSectionKey,
  overrides: Partial<GuidanceSectionDraft> = {},
): GuidanceSectionDraft {
  return {
    key,
    title: key,
    text: '',
    items: null,
    point_groups: null,
    status: 'done',
    error: null,
    ...overrides,
  };
}

test('maps the four guidance drafts to canonical compact sections', () => {
  const sections = [
    section('safer_phrases', { items: ['确认下一步。'] }),
    section('purpose', { items: ['不会单独显示。'] }),
    section('risk_preview', { items: ['留意防御反应。'] }),
    section('opening_suggestion', { items: ['明确反馈目标。'] }),
    section('response_strategies', { items: ['回到事实标准。'] }),
  ];

  assert.deepEqual(buildCompactGuidance(sections), [
    { id: 'start', title: '开场与目标', points: ['明确反馈目标。'] },
    { id: 'emotion', title: '情绪与风险', points: ['留意防御反应。'] },
    { id: 'requirement', title: '标准与回应', points: ['回到事实标准。'] },
    { id: 'plan', title: '行动与收尾', points: ['确认下一步。'] },
  ]);
});

test('keeps every structured group, prefers the full summary, and deduplicates', () => {
  const longSummary = '这是一段有意超过旧版长度限制的完整摘要，它需要原样保留，不能因为侧栏展示而进行任意字符截断。摘要的第二句话也属于显式 summary。';
  const groups: GuidancePointGroup[] = [
    {
      title: '目标',
      summary: longSummary,
      details: ['这条 detail 不应覆盖 summary。'],
    },
    {
      title: '开场',
      summary: null,
      details: ['先陈述可核验事实。再补充评价。'],
    },
    {
      title: '提问',
      details: ['邀请员工说明看法；随后共同核对差异。'],
    },
    {
      title: '开场',
      summary: null,
      details: ['先陈述可核验事实。再补充评价。'],
    },
  ];

  assert.deepEqual(buildCompactGuidance([
    section('opening_suggestion', { point_groups: groups }),
  ]), [
    {
      id: 'start',
      title: '开场与目标',
      points: [
        '目标：' + longSummary,
        '开场：先陈述可核验事实。',
        '提问：邀请员工说明看法；',
      ],
    },
  ]);
});

test('keeps all legacy items, takes one complete sentence, and preserves long text', () => {
  const longTextWithoutTerminator = '这是一条没有句末标点但明显超过旧版八十八字符上限的建议内容，因此它必须完整保留而不能通过字符数量进行截断以免丢失关键管理语义和行动要求';

  assert.deepEqual(buildCompactGuidance([
    section('risk_preview', {
      items: [
        '员工可能对结果感到失望。继续展开说明。',
        longTextWithoutTerminator,
        '先承接情绪！再讨论事实。',
        '员工可能对结果感到失望。继续展开说明。',
      ],
    }),
  ]), [
    {
      id: 'emotion',
      title: '情绪与风险',
      points: [
        '员工可能对结果感到失望。',
        longTextWithoutTerminator,
        '先承接情绪！',
      ],
    },
  ]);
});

test('returns an empty list for missing or empty input and skips empty sections', () => {
  assert.deepEqual(buildCompactGuidance(undefined), []);
  assert.deepEqual(buildCompactGuidance(null), []);
  assert.deepEqual(buildCompactGuidance([]), []);
  assert.deepEqual(buildCompactGuidance([
    section('opening_suggestion', { point_groups: [], items: [] }),
  ]), []);
});

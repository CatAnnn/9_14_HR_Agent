import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import type { CoachTaskResult } from '../src/types/domain.ts';
import {
  buildReportImprovementRows,
  DEVELOPMENT_PLAN_ISSUE_TITLES,
  EMOTION_ISSUE_TITLES,
  OPENING_ISSUE_TITLES,
  OUTPUT_EXPECTATIONS_ISSUE_TITLES,
  reportImprovementEmptyCopy,
} from '../src/utils/reportIssues.ts';

const reportStepSource = readFileSync(
  new URL('../src/pages/steps/ReportStep.tsx', import.meta.url),
  'utf8',
);

function coachResult(
  overrides: Partial<CoachTaskResult> = {},
): CoachTaskResult {
  return {
    task_id: 'output_expectations_evaluation',
    task_name: '回归产出与标准',
    status: 'success',
    score: 4,
    summary: '评估摘要',
    ...overrides,
  };
}

test('keeps all three titled output-standard issues even when text repeats', () => {
  const rows = buildReportImprovementRows(coachResult({
    improvement_points: ['相同判断。', '相同判断。', '第三项判断。'],
  }));

  assert.deepEqual(
    rows.map((row) => row.title),
    [...OUTPUT_EXPECTATIONS_ISSUE_TITLES],
  );
  assert.deepEqual(
    rows.map((row) => row.issue?.text),
    ['相同判断。', '相同判断。', '第三项判断。'],
  );
});

test('does not manufacture fixed-dimension conclusions for a successful empty result', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'opening_evaluation',
    task_name: '开场定调与绩效结果对齐',
    improvement_points: [],
    better_phrases: [],
  }));

  assert.deepEqual(rows, []);
});

test('treats an empty low-score success as an incomplete evaluation instead of no issue', () => {
  const result = coachResult({
    task_id: 'emotion_evaluation',
    task_name: '情绪承接',
    score: 3,
    improvement_points: [],
    better_phrases: [],
  });

  assert.deepEqual(reportImprovementEmptyCopy(result, 3), {
    finding: '该维度评分为 3 分，但评估结果未返回与评分对应的改进问题。',
    suggestion: '请重新生成该维度评估，以获取可核验的具体建议。',
  });
});

test('uses neutral empty-result copy for successful scores four and five', () => {
  const result = coachResult({ improvement_points: [], better_phrases: [] });

  assert.deepEqual(reportImprovementEmptyCopy(result, 4), {
    finding: '本轮评分为 4 分，未返回有经理原话支持的明确改进问题。',
    suggestion: '本轮未返回对应的具体建议。',
  });
  assert.deepEqual(reportImprovementEmptyCopy({ ...result, score: 5 }, 5), {
    finding: '本轮评分为 5 分，未返回明确改进问题。',
    suggestion: '本轮未返回对应的具体建议。',
  });
});

test('distinguishes insufficient-information and failed empty results', () => {
  assert.deepEqual(reportImprovementEmptyCopy(coachResult({
    status: 'insufficient_information',
    score: null,
  }), null), {
    finding: '当前对话信息不足，尚无法形成改进判断。',
    suggestion: '补充完成相关对话后再生成具体建议。',
  });
  assert.deepEqual(reportImprovementEmptyCopy(coachResult({
    status: 'failed',
    score: null,
  }), null), {
    finding: '该维度评估失败，未生成改进内容。',
    suggestion: '请重新生成报告后查看具体建议。',
  });
});

test('places a sparse opening issue under its declared fixed label', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'opening_evaluation',
    task_name: '开场定调与绩效结果对齐',
    improvement_points: ['经理列举了事实，但没有明确说明绩效结果。'],
    better_phrases: [
      {
        diagnostic_dimension_id: 'performance_result_alignment',
        original: '我们先看一下你今年做的事情。',
        suggestion: '今年你的绩效结果是三分，核心交付达到预期，但高端岗位仍有差距。你怎么理解这个结果？',
        reason: '明确结果及其与预期的关系，并确认员工理解。',
      },
    ],
  }));

  assert.deepEqual(rows.map((row) => row.title), [OPENING_ISSUE_TITLES[1]]);
  assert.equal(rows[0]?.issue?.text, '经理列举了事实，但没有明确说明绩效结果。');
  assert.equal(rows[0]?.phrase?.diagnostic_dimension_id, 'performance_result_alignment');
});

test('keeps eight manager-backed suggestions under the same fixed dimension', () => {
  const suggestionCount = 8;
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'opening_evaluation',
    task_name: '开场定调与绩效结果对齐',
    improvement_points: Array.from(
      { length: suggestionCount },
      (_, index) => `绩效结果对齐中的独立问题 ${index + 1}。`,
    ),
    better_phrases: Array.from(
      { length: suggestionCount },
      (_, index) => ({
        diagnostic_dimension_id: 'performance_result_alignment',
        original: `经理第 ${index + 1} 处实际表达。`,
        suggestion: `针对第 ${index + 1} 个独立问题的具体建议。`,
        reason: `第 ${index + 1} 条建议对应不同的经理原话。`,
      }),
    ),
  }));

  assert.equal(rows.length, suggestionCount);
  assert.ok(rows.every((row) => row.title === OPENING_ISSUE_TITLES[1]));
  assert.deepEqual(
    rows.map((row) => row.phrase?.suggestion),
    Array.from(
      { length: suggestionCount },
      (_, index) => `针对第 ${index + 1} 个独立问题的具体建议。`,
    ),
  );
  assert.ok(rows.every((row) => row.title !== '未标明子维度的改进项'));
});

test('keeps sparse historical output issues unclassified instead of shifting their title', () => {
  const rows = buildReportImprovementRows(coachResult({
    improvement_points: ['只有第一项。'],
  }));

  assert.equal(rows.length, 4);
  assert.deepEqual(rows.slice(0, 3).map((row) => row.title), [...OUTPUT_EXPECTATIONS_ISSUE_TITLES]);
  assert.deepEqual(
    rows.slice(0, 3).map((row) => row.issue?.text),
    Array(3).fill('历史报告未记录该子维度的判断。'),
  );
  assert.equal(rows[3]?.title, '未标明子维度的改进项');
  assert.equal(rows[3]?.issue?.text, '只有第一项。');
});

test('lists the three emotion-review dimensions as direct fixed titles', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'emotion_evaluation',
    task_name: '情绪承接',
    improvement_points: ['倾听问题。', '总结问题。', '探索问题。'],
  }));

  assert.deepEqual(
    rows.map((row) => row.title),
    [...EMOTION_ISSUE_TITLES],
  );
  assert.deepEqual(
    rows.map((row) => row.issue?.text),
    ['倾听问题。', '总结问题。', '探索问题。'],
  );
});

test('does not present empty successful emotion output as three no-issue judgments', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'emotion_evaluation',
    task_name: '情绪承接',
    improvement_points: [],
    better_phrases: [],
  }));

  assert.deepEqual(rows, []);
});

test('keeps all emotion-review titles when historical content is missing', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'emotion_evaluation',
    task_name: '情绪承接',
    improvement_points: ['只有倾听问题。'],
    better_phrases: [
      { original: '我先说结论。', suggestion: '我听到你现在很担心这个影响。', reason: '先准确承接情绪。' },
    ],
  }));

  assert.deepEqual(
    rows.slice(0, 3).map((row) => row.issue?.title),
    [...EMOTION_ISSUE_TITLES],
  );
  assert.equal(rows[0]?.phrase, undefined);
  assert.equal(rows[1]?.phrase, undefined);
  assert.equal(rows[2]?.phrase, undefined);
  assert.equal(rows[3]?.title, '当前需要解决的问题');
  assert.equal(rows[3]?.issue?.text, '只有倾听问题。');
  assert.equal(rows[3]?.phrase?.suggestion, '我听到你现在很担心这个影响。');
});

test('adds an unclassified emotion need as a complete paired row after fixed dimensions', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'emotion_evaluation',
    task_name: '情绪承接',
    improvement_points: [
      '经理虽然回应了情绪，但没有处理员工此刻最需要确认的职业影响。',
      '经理很快转回任务安排，没有先解决员工对评价后果的顾虑。',
    ],
    better_phrases: [
      {
        diagnostic_dimension_id: 'empathic_summary',
        original: '我知道你有点失望。',
        suggestion: '听起来这个结果和你的预期有落差，你也在担心它会影响后续发展。',
        reason: '先准确总结情绪及其来源，再继续讨论事实。',
      },
      {
        diagnostic_dimension_id: null,
        original: '这个后面再说，我们先看计划。',
        suggestion: '你现在最需要先确认的，是这次结果会不会影响后续发展，对吗？我们先把这一点说清楚。',
        reason: '先回应当前最迫切的顾虑，避免员工带着未解决的问题进入后续讨论。',
      },
    ],
  }));

  assert.equal(rows.length, 2);
  assert.equal(rows[0]?.title, EMOTION_ISSUE_TITLES[1]);
  assert.equal(rows[1]?.title, '当前需要解决的问题');
  assert.equal(
    rows[1]?.issue?.text,
    '经理很快转回任务安排，没有先解决员工对评价后果的顾虑。',
  );
  assert.equal(
    rows[1]?.phrase?.suggestion,
    '你现在最需要先确认的，是这次结果会不会影响后续发展，对吗？我们先把这一点说清楚。',
  );
  assert.equal(
    rows[1]?.phrase?.reason,
    '先回应当前最迫切的顾虑，避免员工带着未解决的问题进入后续讨论。',
  );
});

test('keeps repeated unclassified titles stable and exposes their sequence separately', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'emotion_evaluation',
    task_name: '情绪承接',
    improvement_points: ['已归类问题。', '未归类问题一。', '未归类问题二。'],
    better_phrases: [
      {
        diagnostic_dimension_id: 'empathic_summary',
        suggestion: '已归类建议。',
      },
      { suggestion: '未归类建议一。' },
      { suggestion: '未归类建议二。' },
    ],
  }));

  assert.deepEqual(rows.map((row) => row.title), [
    EMOTION_ISSUE_TITLES[1],
    '当前需要解决的问题',
    '当前需要解决的问题',
  ]);
  assert.deepEqual(rows.map((row) => row.titleIndex), [undefined, 1, 2]);
  assert.deepEqual(rows.map((row) => row.issue?.titleIndex), [undefined, 1, 2]);
});

test('report UI localizes canonical task titles and formats sequence numbers from stable templates', () => {
  assert.match(reportStepSource, /const TASK_TITLES:/);
  assert.match(reportStepSource, /translate\(dimension\.fallbackName, dimension\.englishName\)/);
  assert.match(reportStepSource, /translate\(taskTitle\[0\], taskTitle\[1\]\)/);
  assert.match(reportStepSource, /'改进项 \{index\}'/);
  assert.match(reportStepSource, /'\{title\} \{index\}'/);
  assert.doesNotMatch(reportStepSource, /`改进项 \$\{/);
});

test('places a sparse emotion issue under its declared fixed dimension without shifting', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'emotion_evaluation',
    task_name: '情绪承接',
    improvement_points: ['承接后仍使用封闭式追问。'],
    better_phrases: [
      {
        diagnostic_dimension_id: 'joint_exploration',
        original: '你就说能不能完成吧？',
        suggestion: '你现在最担心哪一部分，我们可以先从那里继续看。',
        reason: '给员工继续说明顾虑的空间。',
      },
    ],
  }));

  assert.equal(rows.length, 1);
  assert.equal(rows[0]?.title, EMOTION_ISSUE_TITLES[2]);
  assert.equal(rows[0]?.sourceIndex, 2);
  assert.equal(rows[0]?.issue?.sourceIndex, 0);
  assert.equal(rows[0]?.phraseSourceIndex, 0);
  assert.equal(rows[0]?.issue?.text, '承接后仍使用封闭式追问。');
  assert.equal(rows[0]?.phrase?.diagnostic_dimension_id, 'joint_exploration');
});

test('labels only the declared development-plan dimension without adding placeholders', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'development_plan_evaluation',
    task_name: '总结与差异化发展计划',
    improvement_points: ['计划没有与适用岗位要求形成清楚连接。'],
    better_phrases: [
      {
        diagnostic_dimension_id: 'job_level_requirements',
        original: '你明年要做出一些创新。',
        suggestion: '我们先把下一岗级的具体要求与当前目标逐项核对。',
        reason: '把要求落到当前工作后，计划才能被观察和验证。',
      },
    ],
  }));

  assert.equal(rows.length, 1);
  assert.equal(rows[0]?.title, DEVELOPMENT_PLAN_ISSUE_TITLES[0]);
  assert.equal(rows[0]?.issue?.text, '计划没有与适用岗位要求形成清楚连接。');
});

test('uses the Career Elements title when that development-plan issue is declared', () => {
  const rows = buildReportImprovementRows(coachResult({
    task_id: 'development_plan_evaluation',
    task_name: '总结与差异化发展计划',
    improvement_points: ['发展路径没有连接当前 Career Elements 基础。'],
    better_phrases: [
      {
        diagnostic_dimension_id: 'career_elements',
        original: '可以多做一些跨部门工作。',
        suggestion: '我们先确认已体现的跨职能经历，再明确下一步实践。',
        reason: '先确认基础可以避免把未知状态写成缺失。',
      },
    ],
  }));

  assert.equal(rows.length, 1);
  assert.equal(rows[0]?.title, DEVELOPMENT_PLAN_ISSUE_TITLES[1]);
});

test('pairs every improvement need with the manager quote and suggestion at the same index', () => {
  const rows = buildReportImprovementRows(coachResult({
    improvement_points: ['第一项问题。', '第二项问题。', '第三项问题。'],
    better_phrases: [
      { original: '第一句原话。', suggestion: '第一项建议。', reason: '第一项原因。' },
      { original: '第二句原话。', suggestion: '第二项建议。', reason: '第二项原因。' },
      { original: '第三句原话。', suggestion: '第三项建议。', reason: '第三项原因。' },
    ],
  }));

  assert.deepEqual(rows.map((row) => row.sourceIndex), [0, 1, 2]);
  assert.equal(rows[0]?.issue?.title, '目标方向的正确性');
  assert.equal(rows[0]?.issue?.text, '第一项问题。');
  assert.equal(rows[0]?.phrase?.original, '第一句原话。');
  assert.equal(rows[0]?.phrase?.suggestion, '第一项建议。');
  assert.equal(rows[2]?.issue?.title, '高绩效文化');
  assert.equal(rows[2]?.phrase?.suggestion, '第三项建议。');
});

test('keeps historical missing opening content aligned under fixed labels', () => {
  const rows = buildReportImprovementRows({
    ...coachResult(),
    task_id: 'opening_evaluation',
    improvement_points: ['', '第二项问题。'],
    better_phrases: [
      { original: '第一句原话。', suggestion: '第一项建议。', reason: '第一项原因。' },
    ],
  });

  assert.deepEqual(rows.map((row) => row.sourceIndex), [0, 1]);
  assert.deepEqual(rows.map((row) => row.title), [...OPENING_ISSUE_TITLES]);
  assert.equal(rows[0]?.issue?.text, '历史报告未记录该子维度的判断。');
  assert.equal(rows[0]?.issue?.isPlaceholder, true);
  assert.equal(rows[0]?.phrase?.suggestion, '第一项建议。');
  assert.equal(rows[1]?.issue?.text, '第二项问题。');
  assert.equal(rows[1]?.phrase, undefined);
});

test('does not drop a repeated need when it has a separate indexed suggestion', () => {
  const rows = buildReportImprovementRows({
    ...coachResult(),
    task_id: 'opening_evaluation',
    improvement_points: ['相同问题。', '相同问题。'],
    better_phrases: [
      { original: '第一句原话。', suggestion: '第一项建议。', reason: '第一项原因。' },
      { original: '第二句原话。', suggestion: '第二项建议。', reason: '第二项原因。' },
    ],
  });

  assert.equal(rows.length, 2);
  assert.equal(rows[0]?.issue?.text, '相同问题。');
  assert.equal(rows[1]?.issue?.text, '相同问题。');
  assert.equal(rows[1]?.phrase?.suggestion, '第二项建议。');
});

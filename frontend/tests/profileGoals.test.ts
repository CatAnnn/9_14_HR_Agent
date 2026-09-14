import assert from 'node:assert/strict';
import test from 'node:test';

import {
  parseProfileGoalDimensions,
  profileGoalSource,
} from '../src/utils/profileGoals.ts';

test('separates Chinese dimension titles from their inline descriptions', () => {
  const dimensions = parseProfileGoalDimensions([
    '一、核心业务招聘目标：精准补齐自动驾驶人才梯队',
    '二、人才质量与结构优化目标：搭建高竞争力专业团队',
  ]);

  assert.deepEqual(dimensions.map((dimension) => dimension.title), [
    '一、核心业务招聘目标',
    '二、人才质量与结构优化目标',
  ]);
  assert.equal(dimensions[0].groups[0].points[0].text, '精准补齐自动驾驶人才梯队');
  assert.equal(dimensions[1].groups[0].points[0].text, '搭建高竞争力专业团队');
});

test('normalizes inline bilingual goal stages and removes stray spreadsheet quotes', () => {
  const dimensions = parseProfileGoalDimensions(
    'ASCMO/AI-Coder sold to Geely\t\n'
      + 'Action Plan/目标行动计划\n• close follow AI tools evaluation progress.\n'
      + 'Bottom/保底值• evaluation use of ASCMO/AI-Coder\n'
      + 'Meet Expectation/达标值 "•get one set of ASCMO/AI-Coder order" '
      + 'Challenging/挑战值"•get two sets of ASCMO/AI-Coder"',
  );

  assert.equal(dimensions.length, 1);
  assert.equal(dimensions[0].title, 'ASCMO/AI-Coder sold to Geely');
  assert.deepEqual(dimensions[0].groups.map((group) => group.title), [
    'Action Plan / 目标行动计划',
    'Bottom / 保底值',
    'Meet Expectation / 达标值',
    'Challenging / 挑战值',
  ]);
  assert.deepEqual(
    dimensions[0].groups.map((group) => group.points[0]?.text),
    [
      'close follow AI tools evaluation progress.',
      'evaluation use of ASCMO/AI-Coder',
      'get one set of ASCMO/AI-Coder order',
      'get two sets of ASCMO/AI-Coder',
    ],
  );
});

test('drops duplicated compact summaries and preserves detailed numbered groups', () => {
  const dimensions = parseProfileGoalDimensions(
    '1. New project TCA<1 2. Ramp up project, safe launch\t\n'
      + '1. New project TCA<1\n- KP31 delivery\n'
      + '2. Ramp up project, safe launch\n- Xin1 safe launch',
  );

  assert.equal(dimensions.length, 1);
  assert.equal(dimensions[0].title, '关键目标');
  assert.deepEqual(dimensions[0].groups.map((group) => group.title), [
    '1. New project TCA<1',
    '2. Ramp up project, safe launch',
  ]);
  assert.deepEqual(
    dimensions[0].groups.map((group) => group.points[0]?.text),
    ['KP31 delivery', 'Xin1 safe launch'],
  );
});

test('cleans markdown, HTML breaks, escaped newlines, and spreadsheet headers', () => {
  const dimensions = parseProfileGoalDimensions(
    'Goal (Results | Learning | Collaboration)\tDescription<br>'
      + '**KPI Goal**\\t\\n- **Improve release quality**\\n- [Reduce defects](https://example.com)',
  );

  assert.equal(dimensions.length, 1);
  assert.equal(dimensions[0].title, 'KPI Goal');
  assert.deepEqual(
    dimensions[0].groups[0].points.map((point) => point.text),
    ['Improve release quality', 'Reduce defects'],
  );
});

test('ignores repeated trailing tabs from empty spreadsheet columns', () => {
  const dimensions = parseProfileGoalDimensions(
    'Goal (Results | Learning | Collaboration)\tDescription\n'
      + 'Business delivery\t\n'
      + '1) Complete the release plan\t\t\t\n'
      + '2) Close critical issues\t\t\n'
      + 'Capability growth\t\n'
      + '1) Complete the training plan\t\t\t\t\n'
      + '2) Share the learning summary\t\t\n'
      + 'Team collaboration\t\n'
      + '1) Improve cross-team alignment\t\t\t\t\t\n'
      + '2) Document the agreed actions',
  );

  assert.deepEqual(dimensions.map((dimension) => dimension.title), [
    'Business delivery',
    'Capability growth',
    'Team collaboration',
  ]);
  assert.ok(dimensions.every((dimension) => dimension.groups.length > 0));
  assert.deepEqual(
    dimensions.map((dimension) => dimension.groups[0].points.length),
    [2, 2, 2],
  );
});

test('moves long metric prose out of the dimension heading', () => {
  const dimensions = parseProfileGoalDimensions(
    'Goal 2026-3 Quality and Sustainability '
      + '3a) Reduce quality incidents against the prior-year baseline; '
      + '3b) Reach the supplier coverage target across all relevant purchasing volume; '
      + '3c) Improve the audited sustainability response rate for the current cycle.\\t',
  );

  assert.equal(dimensions.length, 1);
  assert.equal(dimensions[0].title, 'Goal 2026-3 Quality and Sustainability');
  assert.equal(dimensions[0].groups[0].points[0].marker, '3a)');
  assert.match(dimensions[0].groups[0].points[0].text, /^Reduce quality incidents/u);
});

test('prefers the unflattened source goal block and stops before profile fields', () => {
  const source = profileGoalSource({
    key_goals: ['flattened goal'],
    source_profile_text: 'Name：Test\nGoal：Dimension A\t\n- Result A\nTCL (SLx)：SL1',
  });

  assert.equal(source, 'Dimension A\t\n- Result A');
});

test('restores flattened five-column goal tables without turning values into headings', () => {
  const dimensions = parseProfileGoalDimensions(
    '"Goal/目标\tAction Plan/目标行动计划\tBottom/保底值\tMeet Expectation/达标值\tChallenging/挑战值\n'
      + '完成2026年VIS CN BP26\nTNS\nGM\t1，制定开票计划；\n2，与项目经理跟踪交付；'
      + '\t45m CNY\n22%\t47m CNY\n24%\t50m CNY\n26%\n'
      + 'AI Authoring POC成功\t1, Local competence build up\n2, Deploy on customer'
      + '\t1个客户AI POC\t2个客户AI POC\t3个客户AI POC"',
  );

  assert.deepEqual(dimensions.map((dimension) => dimension.title), [
    '完成2026年VIS CN BP26',
    'AI Authoring POC成功',
  ]);
  assert.deepEqual(dimensions[0].groups.map((group) => group.title), [
    '目标说明',
    'Action Plan / 目标行动计划',
    'Bottom / 保底值',
    'Meet Expectation / 达标值',
    'Challenging / 挑战值',
  ]);
  assert.deepEqual(
    dimensions[0].groups[0].points.map((point) => point.text),
    ['TNS', 'GM'],
  );
  assert.deepEqual(
    dimensions[0].groups[1].points.map((point) => [point.marker, point.text]),
    [
      ['1,', '制定开票计划;'],
      ['2,', '与项目经理跟踪交付;'],
    ],
  );
  assert.deepEqual(
    dimensions[0].groups[4].points.map((point) => point.text),
    ['50m CNY', '26%'],
  );
  assert.equal(dimensions[1].groups[0].title, 'Action Plan / 目标行动计划');
});

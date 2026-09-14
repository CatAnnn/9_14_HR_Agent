import assert from 'node:assert/strict';
import test from 'node:test';
import {
  agentIntroductionContent,
  localizeIntroductionText,
  type LocalizedIntroductionText,
} from '../src/content/agent-introduction-content.ts';

function collectLocalizedText(value: unknown, result: LocalizedIntroductionText[] = []) {
  if (!value || typeof value !== 'object') return result;
  const candidate = value as Partial<Record<keyof LocalizedIntroductionText, unknown>>;
  if (
    typeof candidate.zh === 'string'
    && typeof candidate.en === 'string'
    && typeof candidate.de === 'string'
    && typeof candidate.ja === 'string'
  ) {
    result.push(candidate as LocalizedIntroductionText);
    return result;
  }
  Object.values(value).forEach((item) => collectLocalizedText(item, result));
  return result;
}

test('defines the complete six-step Agent introduction in workflow order', () => {
  assert.equal(agentIntroductionContent.title, 'Performance Feedback');
  assert.deepEqual(
    agentIntroductionContent.steps.map(({ id, title }) => [id, title.zh]),
    [
      ['profile', '员工信息'],
      ['intent', '沟通意图'],
      ['simulation', '人格与诉求'],
      ['guidance', '谈前指导'],
      ['rehearsal', '多轮预演'],
      ['report', '复盘报告'],
    ],
  );
});

test('keeps the required intent choices and guidance dimensions visible', () => {
  const intent = agentIntroductionContent.steps.find(({ id }) => id === 'intent');
  const guidance = agentIntroductionContent.steps.find(({ id }) => id === 'guidance');

  assert.deepEqual(intent?.options?.map(({ zh }) => zh), [
    '发展',
    '改进',
    '退出',
    '发展与改进',
    '改进与退出预警',
  ]);
  assert.deepEqual(guidance?.options?.map(({ zh }) => zh), [
    '开场定调',
    '情绪承接',
    '产出与标准',
    '发展计划',
  ]);
});

test('all Agent introduction content has native German and Japanese text', () => {
  const localizedContent = collectLocalizedText(agentIntroductionContent);
  assert.ok(localizedContent.length >= 83);

  for (const value of localizedContent) {
    assert.ok(value.de.trim(), `missing German Agent introduction text: ${value.zh}`);
    assert.ok(value.ja.trim(), `missing Japanese Agent introduction text: ${value.zh}`);
    assert.notEqual(value.de, value.en, `German Agent introduction text fell back to English: ${value.zh}`);
    assert.notEqual(value.ja, value.en, `Japanese Agent introduction text fell back to English: ${value.zh}`);
    assert.equal(localizeIntroductionText(value, 'de'), value.de);
    assert.equal(localizeIntroductionText(value, 'ja'), value.ja);
  }
});

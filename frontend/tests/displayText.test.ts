import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeDisplayText,
  userFacingErrorMessage,
} from '../src/utils/displayText.ts';
import { setCurrentLanguage } from '../src/i18n/languageRuntime.ts';

test.afterEach(() => setCurrentLanguage('zh-CN'));

test('normalizes internal conditions in historical report prose', () => {
  assert.equal(
    normalizeDisplayText(
      '在career_elements_applicable=true且对话进入计划阶段时，Manager需要回应Employee的发展需要。',
    ),
    '当前适用 Career Elements，且对话进入计划阶段时，管理者需要回应员工的发展需要。',
  );
});

test('turns backend field names into user-facing business language', () => {
  assert.equal(
    normalizeDisplayText(
      '依据context.performance_context、primary_motive_id和retrieved_chunks形成建议。',
    ),
    '依据员工当前表现、员工的主诉求和知识库参考资料形成建议。',
  );
});

test('keeps legitimate business codes and terms unchanged', () => {
  assert.equal(
    normalizeDisplayText('G9员工的ASR rating与Career Elements需要一起核对。'),
    'G9员工的ASR rating与Career Elements需要一起核对。',
  );
});

test('normalizes internal fields and errors in the active English language', () => {
  setCurrentLanguage('en');
  assert.equal(
    normalizeDisplayText(
      'Use context.performance_context, primary_motive_id, and retrieved_chunks without changing Manager quotes.',
    ),
    'Use current employee performance, employee primary motive, and knowledge-base references without changing Manager quotes.',
  );
  assert.equal(
    userFacingErrorMessage({ detail: 'workflow_error' }, 409),
    'This action is not available at the current workflow stage. Refresh and try again.',
  );
  assert.equal(
    userFacingErrorMessage({ detail: '员工资料尚未确认，请先完成员工信息。' }, 409),
    'This content changed while you were working. Refresh and try again.',
  );
});

test('normalizes internal fields and errors in German without falling back to Chinese', () => {
  setCurrentLanguage('de');
  assert.equal(
    normalizeDisplayText('Use context.performance_context and retrieved_chunks for the Manager.'),
    'Use aktuelle Leistung des Mitarbeitenden and Wissensbasis-Referenzen for the Führungskraft.',
  );
  assert.equal(
    userFacingErrorMessage({ detail: 'workflow_error' }, 409),
    'Diese Aktion ist im aktuellen Prozessschritt nicht verfügbar. Bitte aktualisieren Sie die Seite und versuchen Sie es erneut.',
  );
  assert.equal(
    userFacingErrorMessage({ detail: '员工资料尚未确认，请先完成员工信息。' }, 409),
    'Der Inhalt wurde zwischenzeitlich geändert. Bitte aktualisieren Sie die Seite und versuchen Sie es erneut.',
  );
});

test('normalizes internal fields and errors in Japanese without falling back to Chinese', () => {
  setCurrentLanguage('ja');
  assert.equal(
    normalizeDisplayText('Use context.performance_context and retrieved_chunks for the Employee.'),
    'Use 従業員の現在のパフォーマンス and ナレッジベースの参照情報 for the 従業員.',
  );
  assert.equal(
    userFacingErrorMessage({ detail: 'workflow_error' }, 409),
    '現在のプロセス段階では、この操作を実行できません。ページを更新して、もう一度お試しください。',
  );
  assert.equal(
    userFacingErrorMessage({ detail: '员工资料尚未确认，请先完成员工信息。' }, 409),
    '操作中に内容が変更されました。ページを更新して、もう一度お試しください。',
  );
});

test('localizes real-time speech error codes without exposing backend language', () => {
  setCurrentLanguage('de');
  assert.equal(
    userFacingErrorMessage('no_speech_detected'),
    'Es wurde keine verständliche Sprache erkannt. Bitte nehmen Sie erneut auf.',
  );
  setCurrentLanguage('ja');
  assert.equal(
    userFacingErrorMessage('preview_capacity_exceeded'),
    'リアルタイム音声プレビューが混み合っています。録音全体は継続しています。',
  );
});

test('does not serialize pydantic validation details for users', () => {
  assert.equal(
    userFacingErrorMessage(
      {
        detail: [
          {
            loc: ['body', 'secondary_motive_ids'],
            msg: 'List should have at most 2 items after validation, not 3',
            type: 'too_long',
          },
        ],
      },
      422,
    ),
    '提交的信息不完整或格式不正确，请检查后重试。',
  );
});

test('preserves safe chinese errors and hides technical exceptions', () => {
  assert.equal(
    userFacingErrorMessage({ detail: '员工资料尚未确认，请先完成员工信息。' }, 409),
    '员工资料尚未确认，请先完成员工信息。',
  );
  assert.equal(
    userFacingErrorMessage(
      { detail: 'structured_output.schema_validation: ValidationError at /app/backend/task.py' },
      502,
    ),
    '模型服务暂时无法完成请求，请稍后重试。',
  );
});

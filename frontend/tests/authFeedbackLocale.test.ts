import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8');
}

test('authentication feedback stores semantic state instead of translated prose', () => {
  const login = source('../src/pages/LoginPage.tsx');
  const register = source('../src/pages/RegisterPage.tsx');

  assert.match(login, /const \[loginFailed, setLoginFailed\] = useState\(false\)/u);
  assert.doesNotMatch(login, /setError\(translate\(/u);
  assert.match(register, /useState<RegisterErrorCode \| null>\(null\)/u);
  assert.match(register, /const error = errorCode ===/u);
  assert.doesNotMatch(register, /setError\(translate\(/u);
  assert.doesNotMatch(register, /setMessage\(translate\(/u);
});

test('admin account feedback is localized from semantic state during render', () => {
  const admin = source('../src/pages/AdminPage.tsx');

  assert.match(
    admin,
    /const \[error, setError\] = useState<AdminFeedbackError \| null>\(null\)/u,
  );
  assert.match(
    admin,
    /const \[message, setMessage\] = useState<AdminFeedbackMessageCode \| null>\(null\)/u,
  );
  assert.match(
    admin,
    /const accountActionErrorMessage = adminFeedbackErrorMessage\(translate, error\)/u,
  );
  assert.match(
    admin,
    /const accountFeedbackMessage = adminFeedbackMessage\(translate, message\)/u,
  );
  assert.match(
    admin,
    /return userFacingErrorMessage\([\s\S]*?feedback\.cause,[\s\S]*?adminRequestErrorFallback\(translate, feedback\.fallbackCode\)/u,
  );
  assert.match(admin, /setError\(\{ kind: 'request', cause: err, fallbackCode:/u);
  assert.match(admin, /setPasswordDialogError\(\{[\s\S]*?cause: err,[\s\S]*?fallbackCode:/u);
  assert.match(admin, /setDeleteDialogError\(\{ kind: 'request', cause: err, fallbackCode:/u);
  assert.doesNotMatch(
    admin,
    /set(?:Error|Message|PasswordDialogError|DeleteDialogError)\(\s*(?:translate|userFacingErrorMessage)\(/u,
  );
});

test('workflow, reader, and export feedback also redraw in the active language', () => {
  const workflow = source('../src/pages/AdminTestWorkflowPage.tsx');
  const reader = source('../src/pages/EbookReaderPage.tsx');
  const exportsPanel = source('../src/components/admin/AdminExportPanel.tsx');

  assert.match(workflow, /useState<AdminTestFeedback \| null>\(null\)/u);
  assert.match(workflow, /const error = adminTestFeedbackMessage\(feedback, translate\)/u);
  assert.doesNotMatch(workflow, /setError\(\s*(?:translate|userFacingErrorMessage)\(/u);

  assert.match(reader, /useState<ReaderErrorCode \| null>\(null\)/u);
  assert.match(reader, /const error = readerErrorMessage\(errorCode, translate\)/u);
  assert.doesNotMatch(reader, /setError\(\s*translate\(/u);

  assert.match(exportsPanel, /useState<AdminExportFeedbackError \| null>\(null\)/u);
  assert.match(exportsPanel, /const error = adminExportErrorMessage\(translate, actionError\)/u);
  assert.match(exportsPanel, /const message = adminExportMessage\(translate, actionMessage\)/u);
  assert.doesNotMatch(exportsPanel, /set(?:Error|Message)\(\s*translate\(/u);
});

test('workflow toast callers keep source keys until the active-language render', () => {
  const guidance = source('../src/pages/steps/GuidanceStep.tsx');
  const rehearsal = source('../src/pages/steps/RehearsalStep.tsx');
  const profile = source('../src/pages/steps/ProfileStep.tsx');
  const englishCatalog = source('../src/i18n/LanguageContext.tsx');

  for (const page of [guidance, rehearsal, profile]) {
    assert.doesNotMatch(page, /showToast\(\s*translate\(/u);
    assert.doesNotMatch(
      page,
      /showToast\(userFacingErrorMessage\([\s\S]{0,180}?translate\(/u,
    );
  }
  for (const key of [
    '谈前指导 Word 已导出',
    '谈前指导导出失败。',
    '会话已变化，未发送的追加消息已暂停，请确认后重试。',
    '请先输入新增信息或模拟要求。',
    '当前没有需要重试的追加消息。',
    '员工搜索失败，请稍后重试。',
  ]) {
    assert.equal(englishCatalog.includes(`'${key}':`), true, `missing English toast key: ${key}`);
  }
});

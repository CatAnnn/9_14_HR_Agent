import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8');
}

test('propagates the active language through every frontend HTTP transport', () => {
  const client = source('../src/api/client.ts');
  const auth = source('../src/api/auth.ts');
  const ebooks = source('../src/api/ebooks.ts');

  assert.match(client, /headers\.set\('Accept-Language', getCurrentLanguage\(\)\)/u);
  assert.match(client, /headers: localizedHeaders\(fetchOptions\.headers, true\)/u);
  assert.match(client, /headers: localizedHeaders\(\)/u);
  assert.match(client, /headers: localizedHeaders\(options\.headers, true\)/u);
  assert.match(auth, /headers\.set\('Accept-Language', getCurrentLanguage\(\)\)/u);
  assert.equal((ebooks.match(/'Accept-Language': getCurrentLanguage\(\)/gu) || []).length, 3);
});

test('creates and safely updates workflow sessions in the selected locale', () => {
  const client = source('../src/api/client.ts');
  const workflow = source('../src/context/WorkflowContext.tsx');
  const domain = source('../src/types/domain.ts');

  assert.match(client, /createSession: \(locale: SessionLocale = getCurrentLanguage\(\)\)/u);
  assert.match(client, /JSON\.stringify\(\{ locale \}\)/u);
  assert.match(client, /locale: payload\.locale \|\| getCurrentLanguage\(\)/u);
  assert.match(client, /updateSessionLocale:[\s\S]*?\/sessions\/\$\{encodeURIComponent\(sessionId\)\}\/locale/su);
  assert.match(domain, /export type SessionLocale = 'zh-CN' \| 'en' \| 'de' \| 'ja';/u);
  assert.match(domain, /locale\?: SessionLocale;/u);
  assert.match(domain, /performance_locale\?: SessionLocale \| null;/u);
  assert.match(workflow, /new SessionLocaleCoordinator\(language\)/u);
  assert.match(workflow, /await localeCoordinator\.synchronize/u);
  assert.match(workflow, /await beginLocaleOperation\(\)/u);
  assert.match(workflow, /api\.createSession\(languageRef\.current\)/u);
  assert.match(workflow, /setSession\(\(current\) => current\?\.session_id === normalizedSession\.session_id[\s\S]*?normalizedSession/su);
  assert.match(workflow, /setGuidanceReport\(null\);[\s\S]*?setCoachReport\(null\);/su);
  assert.match(workflow, /presentedLanguageRef\.current === language[\s\S]*?setGuidanceReport\(null\);[\s\S]*?setCoachReport\(null\);[\s\S]*?setLiveConversation\(null\);[\s\S]*?setToast\(null\);/su);
  assert.match(workflow, /refreshSession: api\.getSession/u);
  assert.doesNotMatch(workflow, /useEffect\([\s\S]{0,400}api\.createSession/u);
});

test('sends dynamic locale hints over speech WebSockets', () => {
  const speechInput = source('../src/hooks/useSpeechToText.ts');
  const speechPlayback = source('../src/hooks/useStreamingSpeechPlayback.ts');
  const profile = source('../src/pages/steps/ProfileStep.tsx');
  const rehearsal = source('../src/pages/steps/RehearsalStep.tsx');

  assert.match(speechInput, /requestedLanguage === 'zh'[\s\S]*?speechRecognitionLanguage\(appLanguage\)/su);
  assert.match(speechInput, /await optionsRef\.current\.beforeStart\?\.\(\)/u);
  assert.match(speechInput, /locale: normalizeSpeechRecordingLocale\(activeSpeechLanguage\)/u);
  assert.match(speechInput, /shouldCancelRecordingForLanguageChange/u);
  assert.match(speechInput, /speechText\(\s*'当前浏览器不支持录音/u);
  assert.match(profile, /language: speechRecognitionLanguage\(language\)/u);
  assert.match(rehearsal, /language: speechRecognitionLanguage\(language\)/u);
  assert.match(speechPlayback, /locale: language,\s*language,/u);
  assert.match(speechPlayback, /socketLocaleRef\.current === language/u);
  assert.match(speechPlayback, /playbackLanguageRef\.current === language/u);
  assert.match(speechPlayback, /lastReplyRef\.current = \[\][\s\S]*?closeSocket\(\)[\s\S]*?setStatus\('idle'\)/u);
  assert.match(speechPlayback, /playbackText\(\s*'浏览器阻止了语音播放/u);
});

test('exposes four-language selection and localized workflow chrome in the sidebar', () => {
  const account = source('../src/components/AccountActions.tsx');
  const selector = source('../src/components/LanguageSelector.tsx');
  const navigation = source('../src/components/StepNav.tsx');
  const workspace = source('../src/pages/WorkspacePage.tsx');

  assert.match(account, /<LanguageSelector className="sidebar-account-menu-item" compact \/>/u);
  assert.match(selector, /SUPPORTED_LANGUAGES\.map\(\(locale\)/u);
  assert.match(selector, /value=\{language\}/u);
  assert.match(selector, /setLanguage\(nextLanguage\)/u);
  assert.doesNotMatch(account, /toggleLanguage/u);
  assert.match(navigation, /profile: \['员工信息', 'Employee profile'\]/u);
  assert.match(navigation, /aria-label=\{translate\('工作台导航'\)\}/u);
  assert.match(workspace, /translate\('工作台载入失败，请重新加载'\)/u);
});

test('localizes workflow feedback once at the provider boundary', () => {
  const workflow = source('../src/context/WorkflowContext.tsx');

  assert.match(
    workflow,
    /const localizedGuidanceSections = useMemo\(\(\) => guidanceSections\.map\([\s\S]*?title: translate\(section\.title\),[\s\S]*?error: section\.error \? translate\(section\.error\)/u,
  );
  assert.match(
    workflow,
    /const localizedCoachTasks = useMemo\(\(\) => coachTasks\.map\([\s\S]*?task_name: translate\(task\.task_name\),[\s\S]*?error: task\.error \? translate\(task\.error\)/u,
  );
  assert.match(
    workflow,
    /const feedbackValue = useMemo<WorkflowFeedbackContextValue>\(\(\) => \(\{[\s\S]*?loading: \{ \.\.\.loading, text: translate\(loading\.text\) \},[\s\S]*?toast: toast \? \{ \.\.\.toast, message: translate\(toast\.message\) \} : null/u,
  );
  assert.match(
    workflow,
    /<WorkflowFeedbackContext\.Provider value=\{feedbackValue\}>/u,
  );
  assert.doesNotMatch(workflow, /setToast\(\{\s*message:\s*translate\(/u);
});

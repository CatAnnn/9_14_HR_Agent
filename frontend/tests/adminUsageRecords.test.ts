import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  ADMIN_USAGE_SECTIONS,
  adminUsageSessionPath,
  defaultAdminUsageSection,
  isAdminUsageSection,
} from '../src/utils/adminUsageSession.ts';
import {
  DEFAULT_ADMIN_USAGE_FILTERS,
  adminUsageReturnPath,
  hasAdminUsageFilters,
  readAdminUsageFilters,
  writeAdminUsageFilters,
} from '../src/utils/adminUsageFilters.ts';
import { GERMAN_TEXT } from '../src/i18n/messages/de.ts';
import { JAPANESE_TEXT } from '../src/i18n/messages/ja.ts';

const panelSource = readFileSync(
  new URL('../src/components/admin/AdminExportPanel.tsx', import.meta.url),
  'utf8',
);
const dialogSource = readFileSync(
  new URL('../src/components/admin/AdminUsageSessionDialog.tsx', import.meta.url),
  'utf8',
);
const dialogFocusSource = readFileSync(
  new URL('../src/hooks/useDialogFocus.ts', import.meta.url),
  'utf8',
);
const pageSource = readFileSync(
  new URL('../src/pages/AdminUsageSessionPage.tsx', import.meta.url),
  'utf8',
);
const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const authSource = readFileSync(new URL('../src/api/auth.ts', import.meta.url), 'utf8');
const adminSource = readFileSync(new URL('../src/pages/AdminPage.tsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/styles/admin-page.css', import.meta.url), 'utf8');

test('usage records retain download and lazily request one read-only session detail', () => {
  assert.match(adminSource, /translate\('使用记录', 'Usage records'\)/);
  assert.match(panelSource, /translate\('用户使用记录', 'User usage records'\)/);
  assert.match(panelSource, /className="admin-export-row-view"/);
  assert.match(panelSource, /className="admin-export-row-open admin-export-row-primary"/);
  assert.match(panelSource, /className="admin-export-row-download"/);
  assert.match(panelSource, /onClick=\{\(\) => void loadUsageDetail\(conversation\)\}/);
  assert.match(authSource, /getUsageSessionDetail:[\s\S]*?\/admin\/exports\/user-content\/sessions\/\$\{encodeURIComponent\(sessionId\)\}/);
});

test('the usage viewer is independent, read-only, dismissible, and has four content tabs', () => {
  for (const tab of ['overview', 'guidance', 'rehearsal', 'report']) {
    assert.match(dialogSource, new RegExp(`id: '${tab}' as const`));
  }
  assert.match(dialogSource, /role="dialog"/);
  assert.match(dialogSource, /aria-modal="true"/);
  assert.match(dialogSource, /useDialogFocus/);
  assert.match(dialogFocusSource, /event\.key === 'Escape'/);
  assert.match(dialogFocusSource, /event\.key !== 'Tab'/);
  assert.match(dialogSource, /event\.target === event\.currentTarget/);
  assert.match(dialogSource, /translate\('只读记录', 'Read-only record'\)/);
  assert.doesNotMatch(dialogSource, /WorkflowContext|workflowApi|authApi/);
});

test('administrators can open a canonical read-only session deep link', () => {
  assert.match(appSource, /const AdminUsageSessionPage = lazyRoute/);
  assert.match(appSource, /pathname\.startsWith\('\/admin\/usage\/'\)[\s\S]*?AdminUsageSessionPage\.preload\(\)/);
  assert.match(appSource, /path="\/admin\/usage\/:sessionId\/:section\?" element=\{<AdminUsageSessionPage \/>\}/);
  assert.match(panelSource, /adminUsageSessionPath\([\s\S]*?conversation\.session_id,[\s\S]*?defaultAdminUsageSection\(conversation\)/);
  assert.match(dialogSource, /translate\('进入会话页面', 'Open session page'\)/);
  assert.match(adminSource, /searchParams\.get\('section'\) === 'usage'/);
  assert.match(pageSource, /adminUsageReturnPath\(searchParams\)/);
});

test('session detail tabs use the URL as their source of truth', () => {
  assert.deepEqual(ADMIN_USAGE_SECTIONS, ['overview', 'guidance', 'rehearsal', 'report']);
  assert.equal(adminUsageSessionPath('a/b', 'report'), '/admin/usage/a%2Fb/report');
  assert.equal(isAdminUsageSection('rehearsal'), true);
  assert.equal(isAdminUsageSection('conversation'), false);
  assert.equal(defaultAdminUsageSection({ has_rehearsal: true, stage: 'rehearsal' }), 'rehearsal');
  assert.equal(defaultAdminUsageSection({ has_rehearsal: false, stage: 'guidance_ready' }), 'guidance');
  assert.equal(defaultAdminUsageSection({ has_rehearsal: true, stage: 'report_ready' }), 'report');
  assert.match(pageSource, /useParams<\{[\s\S]*?sessionId: string;[\s\S]*?section\?: string;/);
  assert.match(pageSource, /isAdminUsageSection\(section\)[\s\S]*?section[\s\S]*?: 'overview'/);
  assert.match(pageSource, /navigate\(adminUsageSessionPath\(sessionId, nextSection, searchParams\), \{ replace: true \}\)/);
  assert.match(pageSource, /adminUsageSessionPath\(sessionId, 'overview', searchParams\)[\s\S]*?replace/);
});

test('usage filters round-trip through canonical URLs and ignore invalid values', () => {
  assert.deepEqual(readAdminUsageFilters(new URLSearchParams()), DEFAULT_ADMIN_USAGE_FILTERS);
  assert.equal(hasAdminUsageFilters(DEFAULT_ADMIN_USAGE_FILTERS), false);

  const filters = {
    query: 'alex',
    userEmail: 'Alex@Example.com ',
    startDate: '2026-08-01',
    endDate: '2026-08-31',
    sortOrder: 'oldest' as const,
    scope: 'incomplete' as const,
  };
  const params = writeAdminUsageFilters(new URLSearchParams('unrelated=keep'), filters);
  assert.equal(params.get('section'), 'usage');
  assert.equal(params.get('user'), 'alex@example.com');
  assert.equal(params.get('unrelated'), 'keep');
  assert.deepEqual(readAdminUsageFilters(params), {
    ...filters,
    userEmail: 'alex@example.com',
  });
  assert.equal(
    adminUsageSessionPath('session / 1', 'report', params),
    '/admin/usage/session%20%2F%201/report?q=alex&user=alex%40example.com&from=2026-08-01&to=2026-08-31&order=oldest&scope=incomplete',
  );
  assert.equal(
    adminUsageReturnPath(params),
    '/admin?section=usage&q=alex&user=alex%40example.com&from=2026-08-01&to=2026-08-31&order=oldest&scope=incomplete',
  );
  assert.equal(hasAdminUsageFilters(readAdminUsageFilters(params)), true);

  assert.deepEqual(
    readAdminUsageFilters(new URLSearchParams('from=2026-02-30&to=2026-8-1&order=random&scope=unknown')),
    DEFAULT_ADMIN_USAGE_FILTERS,
  );
});

test('admin dynamic labels use localized static templates instead of runtime translation keys', () => {
  for (const source of [adminSource, panelSource, dialogSource, pageSource]) {
    assert.doesNotMatch(source, /translate\(\s*`/u);
  }

  const templateKeys = [
    '显示 {visible} / {total} 个账号',
    '查看 {email} 的使用记录',
    '{email} 的白名单授权',
    '创建于 {date}',
    '导出已生成：{filename}',
    '{count} 次会话',
    '{results} 条结果 · 可见已选 {visible} · 隐藏已选 {hidden}',
    '选择 {name} 的 {date} 会话',
    '{count} 条发言',
    '第 {turn} 轮',
    '改进项 {index}',
    '{title}：需要改进',
    '{label}，{status}',
    '{name} 的会话',
  ] as const;
  for (const key of templateKeys) {
    assert.ok(GERMAN_TEXT[key], `missing German template: ${key}`);
    assert.ok(JAPANESE_TEXT[key], `missing Japanese template: ${key}`);
  }
  assert.equal(GERMAN_TEXT['显示 {visible} / {total} 个账号'], '{visible} von {total} Konten angezeigt');
  assert.equal(JAPANESE_TEXT['显示 {visible} / {total} 个账号'], '{total}件中{visible}件のアカウントを表示');
});

test('admin report titles and intent labels use stable multilingual identifiers', () => {
  assert.match(panelSource, /import \{ intentName \} from '..\/..\/i18n\/businessLabels';/);
  assert.match(panelSource, /return intentName\([\s\S]*?normalizedId,[\s\S]*?INTENT_NAMES_CHINESE/);
  assert.doesNotMatch(panelSource, /const INTENT_LABELS:/);
  assert.match(dialogSource, /const COACH_TASK_TITLES:/);
  assert.match(dialogSource, /const taskTitle = canonicalTitle[\s\S]*?translate\(canonicalTitle\[0\], canonicalTitle\[1\]\)/);
  assert.match(dialogSource, /<h3>\{taskTitle\}<\/h3>/);

  const dynamicReportTitles = [
    '开场定调与结果对齐',
    '回归产出与标准',
    '总结与发展计划',
    '沟通基调',
    '绩效结果对齐',
    '目标方向的正确性',
    '结果的突破性',
    '高绩效文化',
    '认真倾听，理解情绪',
    '共情总结',
    '共同探索',
    '岗位等级要求',
    '当前需要解决的问题',
    '未标明子维度的改进项',
    '{title} {index}',
  ] as const;
  for (const key of dynamicReportTitles) {
    assert.ok(GERMAN_TEXT[key]?.trim(), `missing German report title: ${key}`);
    assert.ok(JAPANESE_TEXT[key]?.trim(), `missing Japanese report title: ${key}`);
    if (key !== '{title} {index}') {
      assert.notEqual(GERMAN_TEXT[key], key, `untranslated German report title: ${key}`);
      assert.notEqual(JAPANESE_TEXT[key], key, `untranslated Japanese report title: ${key}`);
    }
  }
});

test('the deep-linked session page gates by administrator role and performs one read-only API call', () => {
  assert.match(
    pageSource,
    /if \(user\?\.role !== 'admin'\) return <RouteRedirect to="\/" \/>;[\s\S]*?return <AdminUsageSessionLoader \/>;/,
  );
  const calls = [...pageSource.matchAll(/authApi\.(\w+)/g)].map((match) => match[1]);
  assert.deepEqual([...new Set(calls)], ['getUsageSessionDetail']);
  assert.doesNotMatch(
    `${pageSource}\n${dialogSource}`,
    /WorkflowContext|workflowApi|from ['"][^'"]*\/api\/client|\b(?:fetch|axios)\s*\(/,
  );
});

test('the large usage viewer has a full-height responsive mobile layout', () => {
  assert.match(styles, /\.admin-usage-detail-dialog\s*\{[\s\S]*?height:\s*min\(860px, calc\(100dvh - 40px\)\)/);
  assert.match(styles, /@media \(max-width: 720px\)[\s\S]*?\.admin-usage-detail-dialog\s*\{[\s\S]*?width:\s*100vw;[\s\S]*?height:\s*100dvh;/);
  assert.match(styles, /\.admin-usage-detail-body\s*\{[\s\S]*?overflow:\s*auto;/);
});

test('opening and returning from a session preserves both mobile and desktop list position', () => {
  assert.match(panelSource, /const saveScrollPosition = useCallback/);
  assert.match(
    panelSource,
    /admin-export-row-open admin-export-row-primary[\s\S]*?onClick=\{saveScrollPosition\}/,
  );
  assert.match(panelSource, /onOpenPage=\{saveScrollPosition\}/);
  assert.match(dialogSource, /onClick=\{onOpenPage\}/);
  assert.match(panelSource, /lastWindowScrollRef\.current = window\.scrollY/);
  assert.doesNotMatch(
    panelSource,
    /return \(\) => \{[\s\S]*?window\.removeEventListener\('scroll', scheduleSave\);[\s\S]*?saveWindowScroll\(\);/,
  );
});

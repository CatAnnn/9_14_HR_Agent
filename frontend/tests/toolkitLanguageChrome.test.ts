import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const toolkitSource = readFileSync(
  new URL('../src/pages/ToolkitPage.tsx', import.meta.url),
  'utf8',
);
const shellSource = readFileSync(
  new URL('../src/components/toolkit/MethodArticleShell.tsx', import.meta.url),
  'utf8',
);

test('Toolkit system chrome exposes catalog-backed copy with English fallback', () => {
  assert.match(toolkitSource, /import \{ useLanguage \} from '\.\.\/i18n\/LanguageContext'/);
  assert.match(toolkitSource, /translate\('返回解决方案主页', 'Back to the Solutions homepage'\)/);
  assert.match(toolkitSource, /translate\('文章信息', 'Article information'\)/);
  assert.match(toolkitSource, /translate\('方法指南', 'Method guide'\)/);
  assert.match(toolkitSource, /translateTemplate\('约 \{minutes\} 分钟阅读', 'About \{minutes\} min read'/);
  assert.match(toolkitSource, /translate\('相关内容', 'Related content'\)/);
  assert.match(toolkitSource, /translateTemplate\([\s\S]*?'继续探索 \{title\}',[\s\S]*?'Continue exploring \{title\}'/);
  assert.match(toolkitSource, /translate\('在场景中练习', 'Practice in context'\)/);
  assert.match(toolkitSource, /translate\('进入沟通工作台', 'Open conversation workspace'\)/);
  assert.match(toolkitSource, /translate\('资源中心', 'Resources'\)/);
  assert.match(toolkitSource, /translate\('进入工作台', 'Open workspace'\)/);
});

test('method navigation localizes controls while preserving article labels and content', () => {
  assert.match(shellSource, /import \{ useLanguage \}/);
  assert.match(shellSource, /translate\('本页内容', 'On this page'\)/);
  assert.match(shellSource, /translate\('展开页内导航', 'Expand in-page navigation'\)/);
  assert.match(shellSource, /translate\('收起页内导航', 'Collapse in-page navigation'\)/);
  assert.match(shellSource, /translate\('打开页内导航', 'Open in-page navigation'\)/);
  assert.match(shellSource, /translate\('关闭页内导航', 'Close in-page navigation'\)/);
  assert.match(shellSource, /translate\('方法章节', 'Method sections'\)/);
  assert.match(shellSource, /<span className="method-section-label">\{section\.label\}<\/span>/);
  assert.match(shellSource, /\{children\}/);
  assert.match(
    toolkitSource,
    /const title = tool\?\.title \?\? translate\(page\.title, page\.titleEnglish\)/,
  );
  assert.match(toolkitSource, /customArticle \?\? <GenericMethodArticle tool=\{tool\} \/>/);
});

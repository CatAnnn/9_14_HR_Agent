import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../src/pages/EbookDetailPage.tsx', import.meta.url),
  'utf8',
);

test('ebook download errors are localized while rendering instead of when caught', () => {
  assert.match(
    source,
    /setDownloadError\(\{\s*code:\s*'ebook_download_failed',\s*cause:\s*error\s*\}\)/u,
  );
  assert.doesNotMatch(
    source,
    /catch\s*\(error\)[\s\S]*?setDownloadError\(userFacingErrorMessage/u,
  );
  assert.match(
    source,
    /const downloadErrorMessage = downloadError\?\.code === 'ebook_download_failed'\s*\?\s*userFacingErrorMessage\(\s*downloadError\.cause,[\s\S]*?translate\('电子书下载失败。'\)\s*\)/u,
  );
  assert.match(source, /role="alert">\{downloadErrorMessage\}/u);
});

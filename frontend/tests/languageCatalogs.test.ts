import assert from 'node:assert/strict';
import test from 'node:test';
import {
  createLanguageCatalogLoader,
  createLanguageChangeHandler,
  type LanguageCatalog,
} from '../src/i18n/languageCatalogs.ts';
import type { AppLanguage } from '../src/i18n/languageRuntime.ts';

function deferredCatalog() {
  let resolve!: (catalog: LanguageCatalog) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<LanguageCatalog>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const flushLoads = () => new Promise<void>((resolve) => setImmediate(resolve));

function languageFixture() {
  const german = deferredCatalog();
  const japanese = deferredCatalog();
  const catalogs = createLanguageCatalogLoader({
    de: () => german.promise,
    ja: () => japanese.promise,
  });
  const changes: AppLanguage[] = [];
  const errors: unknown[] = [];
  const selection = createLanguageChangeHandler(
    'en', catalogs, (language) => changes.push(language), (error) => errors.push(error),
  );
  return { german, japanese, catalogs, changes, errors, selection };
}

test('Chinese and English are ready without loading an additional catalog', async () => {
  let loads = 0;
  const load = async () => { loads += 1; return {}; };
  const catalogs = createLanguageCatalogLoader({ de: load, ja: load });

  for (const language of ['zh-CN', 'en'] as const) {
    assert.equal(catalogs.isReady(language), true);
    assert.equal(await catalogs.load(language), undefined);
  }
  assert.equal(loads, 0);
});

test('loads only the selected catalog, sharing concurrent requests and cached results', async () => {
  const german = deferredCatalog();
  const loads: string[] = [];
  const catalogs = createLanguageCatalogLoader({
    de: () => { loads.push('de'); return german.promise; },
    ja: async () => { loads.push('ja'); return {}; },
  });
  const first = catalogs.load('de');
  assert.equal(catalogs.load('de'), first);
  assert.equal(catalogs.isReady('de'), false);
  await flushLoads();
  assert.deepEqual(loads, ['de']);

  const dictionary = { '语言': 'Sprache' };
  german.resolve(dictionary);
  assert.equal(await first, dictionary);
  assert.equal(catalogs.get('de'), dictionary);
  assert.equal(catalogs.isReady('de'), true);
  assert.equal(catalogs.isReady('ja'), false);
  assert.equal(await catalogs.load('de'), dictionary);
  assert.deepEqual(loads, ['de']);
});

test('failed catalog requests can be retried', async () => {
  let attempts = 0;
  const failure = new Error('Failed to fetch dynamically imported module');
  const dictionary = { '语言': 'Sprache' };
  const catalogs = createLanguageCatalogLoader({
    de: async () => {
      attempts += 1;
      if (attempts === 1) throw failure;
      return dictionary;
    },
    ja: async () => ({}),
  });

  await assert.rejects(catalogs.load('de'), failure);
  assert.equal(catalogs.isReady('de'), false);
  assert.equal(await catalogs.load('de'), dictionary);
  assert.equal(attempts, 2);
});

test('language selection waits for its catalog and reuses loaded languages immediately', async () => {
  const { german, changes, errors, selection } = languageFixture();
  selection.setLanguage('de');
  assert.deepEqual(changes, []);
  german.resolve({ '语言': 'Sprache' });
  await flushLoads();
  assert.deepEqual(changes, ['de']);

  selection.setLanguage('en');
  selection.setLanguage('de');
  assert.deepEqual(changes, ['de', 'en', 'de']);
  assert.deepEqual(errors, []);
});

test('rapid language switches cannot be overwritten by a slower earlier catalog', async () => {
  const { german, japanese, changes, selection } = languageFixture();
  selection.setLanguage('de');
  selection.setLanguage('ja');
  assert.equal(selection.requestedLanguage, 'ja');
  japanese.resolve({ '语言': '言語' });
  await flushLoads();
  german.resolve({ '语言': 'Sprache' });
  await flushLoads();
  assert.deepEqual(changes, ['ja']);
});

test('returning to an already available language cancels a pending selection', async () => {
  const { german, changes, selection } = languageFixture();
  selection.setLanguage('de');
  selection.setLanguage('en');
  assert.deepEqual(changes, ['en']);
  german.resolve({});
  await flushLoads();
  assert.deepEqual(changes, ['en']);
  assert.equal(selection.requestedLanguage, 'en');
});

test('load failures keep the committed language and allow the same selection to be retried', async () => {
  const changes: AppLanguage[] = [];
  const errors: unknown[] = [];
  const failure = new Error('Offline');
  let attempts = 0;
  const catalogs = createLanguageCatalogLoader({
    de: async () => {
      attempts += 1;
      if (attempts === 1) throw failure;
      return {};
    },
    ja: async () => ({}),
  });
  const selection = createLanguageChangeHandler(
    'en', catalogs, (language) => changes.push(language), (error) => errors.push(error),
  );
  selection.setLanguage('de');
  await flushLoads();
  assert.deepEqual(changes, []);
  assert.deepEqual(errors, [failure]);
  assert.equal(selection.requestedLanguage, 'en');

  selection.setLanguage('de');
  await flushLoads();
  assert.deepEqual(changes, ['de']);
  assert.equal(attempts, 2);
});

test('a superseded request failure does not affect the active selection', async () => {
  const { german, japanese, changes, errors, selection } = languageFixture();
  selection.setLanguage('de');
  selection.setLanguage('ja');
  german.reject(new Error('Offline'));
  japanese.resolve({});
  await flushLoads();
  assert.deepEqual(changes, ['ja']);
  assert.deepEqual(errors, []);
  assert.equal(selection.requestedLanguage, 'ja');
});

test('unmounted providers ignore pending catalog results', async () => {
  const { german, catalogs, changes, errors, selection } = languageFixture();
  selection.setLanguage('de');
  selection.cancel();
  german.resolve({});
  await flushLoads();
  assert.deepEqual(changes, []);
  assert.deepEqual(errors, []);
  assert.equal(catalogs.isReady('de'), true);
});

import type { AppLanguage } from './languageRuntime';

export type LanguageCatalog = Readonly<Record<string, string>>;
type CatalogLanguage = 'de' | 'ja';
type CatalogLoaders = Record<CatalogLanguage, () => Promise<LanguageCatalog>>;

const catalogLoaders: CatalogLoaders = {
  de: () => import('./messages/de.ts').then((module) => module.GERMAN_TEXT),
  ja: () => import('./messages/ja.ts').then((module) => module.JAPANESE_TEXT),
};

function needsCatalog(language: AppLanguage): language is CatalogLanguage {
  return language === 'de' || language === 'ja';
}

export function createLanguageCatalogLoader(loaders: CatalogLoaders = catalogLoaders) {
  const catalogs = new Map<CatalogLanguage, LanguageCatalog>();
  const pending = new Map<CatalogLanguage, Promise<LanguageCatalog>>();

  return {
    get(language: AppLanguage): LanguageCatalog | undefined {
      return needsCatalog(language) ? catalogs.get(language) : undefined;
    },
    isReady(language: AppLanguage): boolean {
      return !needsCatalog(language) || catalogs.has(language);
    },
    load(language: AppLanguage): Promise<LanguageCatalog | undefined> {
      if (!needsCatalog(language)) return Promise.resolve(undefined);
      const cached = catalogs.get(language);
      if (cached) return Promise.resolve(cached);
      const existing = pending.get(language);
      if (existing) return existing;

      const request = Promise.resolve().then(loaders[language]).then((catalog) => {
        catalogs.set(language, catalog);
        pending.delete(language);
        return catalog;
      }, (error: unknown) => {
        pending.delete(language);
        throw error;
      });
      pending.set(language, request);
      return request;
    },
  };
}

export const languageCatalogs = createLanguageCatalogLoader();

// Keep UI and request locales together until the selected catalog is available.
export function createLanguageChangeHandler(
  initialLanguage: AppLanguage,
  catalogs: Pick<ReturnType<typeof createLanguageCatalogLoader>, 'isReady' | 'load'>,
  onChange: (language: AppLanguage) => void,
  onError: (error: unknown) => void,
) {
  let committedLanguage = initialLanguage;
  let requestedLanguage = initialLanguage;
  let requestVersion = 0;

  return {
    get requestedLanguage(): AppLanguage {
      return requestedLanguage;
    },
    setLanguage(language: AppLanguage): void {
      requestedLanguage = language;
      const version = ++requestVersion;
      const commit = () => {
        if (version !== requestVersion) return;
        committedLanguage = language;
        onChange(language);
      };
      if (catalogs.isReady(language)) {
        commit();
        return;
      }
      void catalogs.load(language).then(commit).catch((error: unknown) => {
        if (version !== requestVersion) return;
        requestedLanguage = committedLanguage;
        onError(error);
      });
    },
    cancel(): void {
      requestVersion += 1;
      requestedLanguage = committedLanguage;
    },
  };
}

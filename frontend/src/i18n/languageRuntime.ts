import type { SessionLocale } from '../types/domain';

export type AppLanguage = SessionLocale;

export type TranslationTemplateValues = Readonly<Record<string, string | number>>;

export const SUPPORTED_LANGUAGES = ['zh-CN', 'en', 'de', 'ja'] as const satisfies readonly AppLanguage[];

const LANGUAGE_NAMES: Readonly<Record<AppLanguage, string>> = {
  'zh-CN': '中文',
  en: 'English',
  de: 'Deutsch',
  ja: '日本語',
};

export function languageName(language: AppLanguage): string {
  return LANGUAGE_NAMES[language];
}

export function normalizeAppLanguage(value: unknown): AppLanguage | null {
  const normalized = String(value || '').trim().toLowerCase().replace(/_/gu, '-');
  if (normalized === 'zh' || normalized.startsWith('zh-')) return 'zh-CN';
  if (normalized === 'en' || normalized.startsWith('en-')) return 'en';
  if (normalized === 'de' || normalized.startsWith('de-')) return 'de';
  if (normalized === 'ja' || normalized.startsWith('ja-') || normalized === 'jp') return 'ja';
  return null;
}

let activeLanguage: AppLanguage = 'zh-CN';

export function getCurrentLanguage(): AppLanguage {
  return activeLanguage;
}

export function setCurrentLanguage(language: AppLanguage): void {
  activeLanguage = language;
}

export function formatTranslationTemplate(
  template: string,
  values: TranslationTemplateValues,
): string {
  return template.replace(/\{([a-z][a-z0-9_]*)\}/giu, (placeholder, key: string) => (
    Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : placeholder
  ));
}

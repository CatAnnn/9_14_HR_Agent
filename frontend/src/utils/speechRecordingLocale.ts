import type { SessionLocale } from '../types/domain.ts';

const SPEECH_LANGUAGE_BY_LOCALE = {
  'zh-CN': 'zh-CN',
  en: 'en-US',
  de: 'de-DE',
  ja: 'ja-JP',
} as const satisfies Readonly<Record<SessionLocale, string>>;

export function speechRecognitionLanguage(locale: SessionLocale): string {
  return SPEECH_LANGUAGE_BY_LOCALE[locale];
}

export function normalizeSpeechRecordingLocale(
  language: string | null | undefined,
): SessionLocale {
  const normalized = String(language || '').trim().toLowerCase();
  if (normalized === 'en' || normalized.startsWith('en-')) return 'en';
  if (normalized === 'de' || normalized.startsWith('de-')) return 'de';
  if (normalized === 'ja' || normalized.startsWith('ja-')) return 'ja';
  return 'zh-CN';
}

export function shouldCancelRecordingForLanguageChange(
  recordingLocale: string | null | undefined,
  currentLocale: string | null | undefined,
  active: boolean,
): boolean {
  return Boolean(
    active
    && recordingLocale
    && normalizeSpeechRecordingLocale(recordingLocale) !== normalizeSpeechRecordingLocale(currentLocale),
  );
}

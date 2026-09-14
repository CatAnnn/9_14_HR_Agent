import type {
  IntentGoalPerformanceItem,
  IntentPerformanceDraftResponse,
  SessionLocale,
} from '../types/domain.ts';
import { normalizeDisplayText } from './displayText.ts';

const PERFORMANCE_SECTION_TITLES_BY_LOCALE = {
  'zh-CN': [
    '目标达成总览',
    '正向表现/取得进展',
    '现存差距与行为实例',
  ],
  en: [
    'Goal Achievement Overview',
    'Positive Performance / Progress',
    'Current Gaps and Behavioral Examples',
  ],
  de: [
    'Übersicht der Zielerreichung',
    'Positive Leistung / Fortschritte',
    'Bestehende Lücken und Verhaltensbeispiele',
  ],
  ja: [
    '目標達成の概要',
    'ポジティブな成果／進捗',
    '現在のギャップと行動例',
  ],
} as const satisfies Readonly<Record<SessionLocale, readonly string[]>>;

export function performanceSectionTitles(locale: SessionLocale): readonly string[] {
  return PERFORMANCE_SECTION_TITLES_BY_LOCALE[locale];
}

export function emptyDraft(
  intentId: string,
  locale: SessionLocale,
): IntentPerformanceDraftResponse {
  return {
    intent_id: intentId,
    locale,
    performance_context: '',
    performance_items: performanceSectionTitles(locale).map((goal) => ({
      goal,
      current_performance: '',
      generation_reason: null,
    })),
  };
}

export function goalsMatch(
  items: IntentGoalPerformanceItem[] | null | undefined,
  locale: SessionLocale,
): boolean {
  if (!Array.isArray(items)) return false;
  const expectedTitles = performanceSectionTitles(locale);
  const labels = items.map((item) => item.goal.trim());
  return labels.length === expectedTitles.length
    && labels.every((label, index) => label === expectedTitles[index]);
}

export function normalizeGeneratedPerformanceItem(
  rawItem: unknown,
  index: number,
  locale: SessionLocale,
): IntentGoalPerformanceItem | null {
  const expectedTitle = performanceSectionTitles(locale)[index];
  if (
    expectedTitle === undefined
    || typeof rawItem !== 'object'
    || rawItem === null
  ) return null;

  const item = rawItem as Record<string, unknown>;
  if (
    typeof item.goal !== 'string'
    || item.goal.trim() !== expectedTitle
    || typeof item.current_performance !== 'string'
  ) return null;

  return {
    // Keep the server-provided canonical label. It is part of the save contract,
    // while any friendlier wording belongs only in the presentation layer.
    goal: item.goal.trim(),
    current_performance: normalizeDisplayText(item.current_performance),
    generation_reason: typeof item.generation_reason === 'string'
      ? normalizeDisplayText(item.generation_reason)
      : null,
  };
}

export function normalizeGeneratedPerformanceItems(
  items: unknown,
  locale: SessionLocale,
): IntentGoalPerformanceItem[] | null {
  const expectedTitles = performanceSectionTitles(locale);
  if (!Array.isArray(items) || items.length !== expectedTitles.length) {
    return null;
  }

  const normalized = items.map((item, index) => (
    normalizeGeneratedPerformanceItem(item, index, locale)
  ));
  return normalized.every((item): item is IntentGoalPerformanceItem => item !== null)
    ? normalized
    : null;
}

export function migrateLegacyPerformanceItems(
  items: IntentGoalPerformanceItem[] | null | undefined,
  locale: SessionLocale,
): IntentGoalPerformanceItem[] | null {
  if (!Array.isArray(items)) return null;
  if (goalsMatch(items, locale)) return items.map((item) => ({ ...item }));

  const sourceLocale = (Object.keys(PERFORMANCE_SECTION_TITLES_BY_LOCALE) as SessionLocale[])
    .find((candidateLocale) => candidateLocale !== locale && goalsMatch(items, candidateLocale));
  if (!sourceLocale) return null;

  const targetTitles = performanceSectionTitles(locale);
  return items.map((item, index) => ({
    ...item,
    goal: targetTitles[index],
  }));
}

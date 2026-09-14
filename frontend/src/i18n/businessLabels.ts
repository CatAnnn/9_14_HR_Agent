import type { SessionLocale } from '../types/domain';

interface BusinessLabel {
  name: string;
  description?: string;
}

type TranslatedLocale = Exclude<SessionLocale, 'zh-CN'>;
type BusinessLabelCatalog = Readonly<Record<string, BusinessLabel>>;
type NameCatalog = Readonly<Record<string, string>>;

const INTENT_LABELS_ENGLISH: BusinessLabelCatalog = {
  development: {
    name: 'Development feedback',
    description: 'Build on strengths and plan long-term growth.',
  },
  improvement: {
    name: 'Improvement feedback',
    description: 'Identify gaps and improve performance to expectations.',
  },
  exit: {
    name: 'Exit feedback',
    description: 'Address sustained performance gaps and explain the applicable next steps.',
  },
  development_improvement: {
    name: 'Development + improvement feedback',
    description: 'Recognize strengths while addressing performance gaps.',
  },
  improvement_exit: {
    name: 'Improvement + exit-warning feedback',
    description: 'Set clear improvement expectations and explain the consequences if they are not met.',
  },
};

const INTENT_LABELS_GERMAN: BusinessLabelCatalog = {
  development: {
    name: 'Entwicklungsfeedback',
    description: 'Stärken ausbauen und langfristiges Wachstum planen.',
  },
  improvement: {
    name: 'Verbesserungsfeedback',
    description: 'Lücken erkennen und die Leistung auf das erwartete Niveau bringen.',
  },
  exit: {
    name: 'Trennungsorientiertes Feedback',
    description: 'Anhaltende Leistungslücken ansprechen und die geltenden nächsten Schritte erläutern.',
  },
  development_improvement: {
    name: 'Entwicklungs- und Verbesserungsfeedback',
    description: 'Stärken würdigen und zugleich Leistungslücken bearbeiten.',
  },
  improvement_exit: {
    name: 'Verbesserungsfeedback mit Trennungswarnung',
    description: 'Klare Verbesserungserwartungen und die Folgen bei Nichterfüllung erläutern.',
  },
};

const INTENT_LABELS_JAPANESE: BusinessLabelCatalog = {
  development: {
    name: '育成フィードバック',
    description: '強みを生かし、長期的な成長を計画します。',
  },
  improvement: {
    name: '改善フィードバック',
    description: 'ギャップを特定し、期待水準までパフォーマンスを改善します。',
  },
  exit: {
    name: '退職を視野に入れたフィードバック',
    description: '継続的なパフォーマンスギャップを扱い、該当する次の手順を説明します。',
  },
  development_improvement: {
    name: '育成＋改善フィードバック',
    description: '強みを認めながら、パフォーマンスギャップにも対応します。',
  },
  improvement_exit: {
    name: '改善＋退職警告フィードバック',
    description: '明確な改善期待と、満たされない場合の結果を説明します。',
  },
};

const INTENT_LABELS: Readonly<Record<TranslatedLocale, BusinessLabelCatalog>> = {
  en: INTENT_LABELS_ENGLISH,
  de: INTENT_LABELS_GERMAN,
  ja: INTENT_LABELS_JAPANESE,
};

const MOTIVE_LABELS_ENGLISH: BusinessLabelCatalog = {
  commerce: {
    name: 'Compensation',
    description: 'Salary increases, bonuses, and financial rewards.',
  },
  power: {
    name: 'Influence',
    description: 'Promotion, authority, and influence over decisions.',
  },
  recognition: {
    name: 'Recognition',
    description: 'Being seen, appreciated, and given visibility.',
  },
  affiliation: {
    name: 'Team belonging',
    description: 'Supportive relationships, collaboration, and a sense of belonging.',
  },
  security: {
    name: 'Security',
    description: 'Job security, predictable processes, and clarity about what comes next.',
  },
  hedonism: {
    name: 'Comfortable work environment',
    description: 'Workplace comfort, variety, and a sustainable level of pressure.',
  },
};

const MOTIVE_LABELS_GERMAN: BusinessLabelCatalog = {
  commerce: {
    name: 'Vergütung',
    description: 'Gehaltserhöhungen, Boni und finanzielle Anerkennung.',
  },
  power: {
    name: 'Einfluss',
    description: 'Beförderung, Entscheidungsspielraum und Einfluss auf Entscheidungen.',
  },
  recognition: {
    name: 'Anerkennung',
    description: 'Gesehen, wertgeschätzt und sichtbar gemacht werden.',
  },
  affiliation: {
    name: 'Teamzugehörigkeit',
    description: 'Unterstützende Beziehungen, Zusammenarbeit und Zugehörigkeitsgefühl.',
  },
  security: {
    name: 'Sicherheit',
    description: 'Arbeitsplatzsicherheit, verlässliche Prozesse und Klarheit über die nächsten Schritte.',
  },
  hedonism: {
    name: 'Angenehmes Arbeitsumfeld',
    description: 'Arbeitskomfort, Abwechslung und ein dauerhaft tragbares Belastungsniveau.',
  },
};

const MOTIVE_LABELS_JAPANESE: BusinessLabelCatalog = {
  commerce: {
    name: '報酬',
    description: '昇給、賞与、金銭的な報酬。',
  },
  power: {
    name: '影響力',
    description: '昇進、権限、意思決定への影響力。',
  },
  recognition: {
    name: '承認',
    description: '成果や価値を認められ、評価され、可視化されること。',
  },
  affiliation: {
    name: 'チームへの帰属',
    description: '支え合う関係、協働、帰属意識。',
  },
  security: {
    name: '安定性',
    description: '雇用の安定、予測可能なプロセス、今後に関する明確さ。',
  },
  hedonism: {
    name: '快適な職場環境',
    description: '働きやすさ、変化、持続可能な負荷。',
  },
};

const MOTIVE_LABELS: Readonly<Record<TranslatedLocale, BusinessLabelCatalog>> = {
  en: MOTIVE_LABELS_ENGLISH,
  de: MOTIVE_LABELS_GERMAN,
  ja: MOTIVE_LABELS_JAPANESE,
};

const EMOTION_NAMES_ENGLISH: NameCatalog = {
  surprised: 'Surprised',
  pleased: 'Pleased',
  engaged: 'Engaged',
  confident: 'Confident',
  relieved: 'Relieved',
  calm: 'Calm',
  neutral: 'Neutral and observant',
  tired: 'Tired',
  discouraged: 'Discouraged',
  sad: 'Sad',
  disappointed: 'Disappointed',
  anxious: 'Anxious',
  confused: 'Confused',
  guarded: 'Guarded',
  angry: 'Angry',
  resentful: 'Resentful',
  hopeful: 'Hopeful',
  proud: 'Proud',
  appreciative: 'Appreciative',
  inspired: 'Inspired',
  curious: 'Curious',
  determined: 'Determined',
  trusting: 'Trusting',
  grateful: 'Grateful',
  accomplished: 'Accomplished',
  eager: 'Eager',
  empowered: 'Empowered',
  attentive: 'Attentive',
  reflective: 'Reflective',
  receptive: 'Receptive',
  skeptical: 'Skeptical',
  cautious: 'Cautious',
  uncertain: 'Uncertain',
  ambivalent: 'Ambivalent',
  evaluating: 'Evaluating',
  composed: 'Composed',
  alert: 'Alert',
  reserved: 'Reserved',
  detached: 'Detached',
  frustrated: 'Frustrated',
  irritated: 'Irritated',
  aggrieved: 'Aggrieved',
  embarrassed: 'Embarrassed',
  ashamed: 'Ashamed',
  guilty: 'Guilty',
  overwhelmed: 'Overwhelmed',
  afraid: 'Afraid',
  helpless: 'Helpless',
};

const EMOTION_NAMES_GERMAN: NameCatalog = {
  surprised: 'Überrascht',
  pleased: 'Erfreut',
  engaged: 'Engagiert',
  confident: 'Selbstsicher',
  relieved: 'Erleichtert',
  calm: 'Ruhig',
  neutral: 'Neutral und beobachtend',
  tired: 'Müde',
  discouraged: 'Entmutigt',
  sad: 'Traurig',
  disappointed: 'Enttäuscht',
  anxious: 'Besorgt',
  confused: 'Verwirrt',
  guarded: 'Zurückhaltend',
  angry: 'Wütend',
  resentful: 'Verbittert',
  hopeful: 'Hoffnungsvoll',
  proud: 'Stolz',
  appreciative: 'Wertschätzend',
  inspired: 'Inspiriert',
  curious: 'Neugierig',
  determined: 'Entschlossen',
  trusting: 'Vertrauend',
  grateful: 'Dankbar',
  accomplished: 'Erfolgreich',
  eager: 'Tatendurstig',
  empowered: 'Bestärkt',
  attentive: 'Aufmerksam',
  reflective: 'Reflektiert',
  receptive: 'Aufgeschlossen',
  skeptical: 'Skeptisch',
  cautious: 'Vorsichtig',
  uncertain: 'Unsicher',
  ambivalent: 'Zwiespältig',
  evaluating: 'Abwägend',
  composed: 'Gefasst',
  alert: 'Wachsam',
  reserved: 'Reserviert',
  detached: 'Distanziert',
  frustrated: 'Frustriert',
  irritated: 'Gereizt',
  aggrieved: 'Gekränkt',
  embarrassed: 'Verlegen',
  ashamed: 'Beschämt',
  guilty: 'Schuldig',
  overwhelmed: 'Überfordert',
  afraid: 'Ängstlich',
  helpless: 'Hilflos',
};

const EMOTION_NAMES_JAPANESE: NameCatalog = {
  surprised: '驚いている',
  pleased: 'うれしい',
  engaged: '積極的',
  confident: '自信がある',
  relieved: '安心している',
  calm: '落ち着いている',
  neutral: '中立的に観察している',
  tired: '疲れている',
  discouraged: '意気消沈している',
  sad: '悲しい',
  disappointed: '失望している',
  anxious: '不安',
  confused: '混乱している',
  guarded: '警戒している',
  angry: '怒っている',
  resentful: '不満を抱いている',
  hopeful: '希望を持っている',
  proud: '誇らしい',
  appreciative: '感謝している',
  inspired: '刺激を受けている',
  curious: '好奇心がある',
  determined: '決意している',
  trusting: '信頼している',
  grateful: 'ありがたく感じている',
  accomplished: '達成感がある',
  eager: '意欲的',
  empowered: '力づけられている',
  attentive: '注意深い',
  reflective: '内省的',
  receptive: '受容的',
  skeptical: '懐疑的',
  cautious: '慎重',
  uncertain: '確信が持てない',
  ambivalent: '複雑な気持ち',
  evaluating: '見極めている',
  composed: '冷静',
  alert: '注意を払っている',
  reserved: '控えめ',
  detached: '距離を置いている',
  frustrated: 'もどかしい',
  irritated: 'いら立っている',
  aggrieved: '不当だと感じている',
  embarrassed: 'きまり悪い',
  ashamed: '恥じている',
  guilty: '罪悪感がある',
  overwhelmed: '圧倒されている',
  afraid: '恐れている',
  helpless: '無力感がある',
};

const EMOTION_NAMES: Readonly<Record<TranslatedLocale, NameCatalog>> = {
  en: EMOTION_NAMES_ENGLISH,
  de: EMOTION_NAMES_GERMAN,
  ja: EMOTION_NAMES_JAPANESE,
};

function localizedLabel(
  labels: Readonly<Record<TranslatedLocale, BusinessLabelCatalog>>,
  id: string | null | undefined,
  fallback: string | null | undefined,
  locale: SessionLocale,
  field: keyof BusinessLabel,
): string {
  const original = String(fallback || id || '').trim();
  if (locale === 'zh-CN') return original;
  const key = String(id || '');
  return String(labels[locale]?.[key]?.[field] || labels.en[key]?.[field] || original).trim();
}

export function intentName(
  id: string | null | undefined,
  fallback: string | null | undefined,
  locale: SessionLocale,
): string {
  return localizedLabel(INTENT_LABELS, id, fallback, locale, 'name');
}

export function intentDescription(
  id: string | null | undefined,
  fallback: string | null | undefined,
  locale: SessionLocale,
): string {
  return localizedLabel(INTENT_LABELS, id, fallback, locale, 'description');
}

export function motiveName(
  id: string | null | undefined,
  fallback: string | null | undefined,
  locale: SessionLocale,
): string {
  return localizedLabel(MOTIVE_LABELS, id, fallback, locale, 'name');
}

export function motiveDescription(
  id: string | null | undefined,
  fallback: string | null | undefined,
  locale: SessionLocale,
): string {
  return localizedLabel(MOTIVE_LABELS, id, fallback, locale, 'description');
}

export function emotionName(
  id: string | null | undefined,
  fallback: string | null | undefined,
  locale: SessionLocale,
): string {
  const original = String(fallback || id || '').trim();
  if (locale === 'zh-CN') return original;
  const key = String(id || '');
  return EMOTION_NAMES[locale]?.[key] || EMOTION_NAMES.en[key] || original;
}

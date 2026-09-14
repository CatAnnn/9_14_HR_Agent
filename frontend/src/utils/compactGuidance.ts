import type {
  GuidanceDimensionPoints,
  GuidancePointGroup,
  GuidanceSectionDraft,
  GuidanceSectionKey,
} from '../types/domain';

export interface CompactGuidanceSection {
  id: keyof GuidanceDimensionPoints;
  title: string;
  points: string[];
}

interface CompactGuidanceSectionDefinition {
  id: keyof GuidanceDimensionPoints;
  sourceKey: GuidanceSectionKey;
  title: string;
}

const COMPACT_GUIDANCE_SECTIONS: readonly CompactGuidanceSectionDefinition[] = [
  { id: 'start', sourceKey: 'opening_suggestion', title: '开场与目标' },
  { id: 'emotion', sourceKey: 'risk_preview', title: '情绪与风险' },
  { id: 'requirement', sourceKey: 'response_strategies', title: '标准与回应' },
  { id: 'plan', sourceKey: 'safer_phrases', title: '行动与收尾' },
];

function normalizeText(value: unknown): string {
  if (typeof value !== 'string') return '';
  return value
    .replace(/结果的突破型/g, '结果的突破性')
    .replace(/\s+/g, ' ')
    .trim();
}

function firstCompleteSentence(value: unknown): string {
  const normalized = normalizeText(value);
  if (!normalized) return '';
  return normalized.match(/^[\s\S]*?[。！？!?；;.]/)?.[0] || normalized;
}

function pointFromGroup(group: GuidancePointGroup): string {
  const title = normalizeText(group.title);
  const summary = normalizeText(group.summary);
  const content = summary || firstCompleteSentence(group.details[0]);
  if (!content) return '';
  return title ? title + '：' + content : content;
}

function uniquePoints(points: readonly string[]): string[] {
  const seen = new Set<string>();
  return points.filter((point) => {
    if (!point || seen.has(point)) return false;
    seen.add(point);
    return true;
  });
}

function pointsFromSection(section: GuidanceSectionDraft): string[] {
  const structuredPoints = uniquePoints(
    (section.point_groups || []).map(pointFromGroup),
  );
  if (structuredPoints.length > 0) return structuredPoints;

  return uniquePoints((section.items || []).map(firstCompleteSentence));
}

export function buildCompactGuidance(
  sections: readonly GuidanceSectionDraft[] | null | undefined,
): CompactGuidanceSection[] {
  if (!sections?.length) return [];

  const sectionByKey = new Map(sections.map((section) => [section.key, section]));
  return COMPACT_GUIDANCE_SECTIONS.flatMap((definition) => {
    const source = sectionByKey.get(definition.sourceKey);
    if (!source) return [];
    const points = pointsFromSection(source);
    return points.length > 0
      ? [{ id: definition.id, title: definition.title, points }]
      : [];
  });
}

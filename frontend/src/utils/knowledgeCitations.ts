import type { KnowledgeCitation } from '../types/domain';

export const KNOWLEDGE_SCOPE_LABELS: Record<string, string> = {
  career: 'Career Elements',
  culture: '文化与价值观',
  development_dialog: '发展对话',
  emotion: '情绪与沟通',
  employee: '员工与岗位',
  feedback: '反馈方法',
  general: '通用管理',
  job_level: '岗位与职级',
  organization_unit: '组织信息',
  performance: '绩效管理',
  redline: '制度红线',
};

export const KNOWLEDGE_SCOPE_ENGLISH_LABELS: Readonly<Record<string, string>> = {
  'Career Elements': 'Career Elements',
  '文化与价值观': 'Culture and values',
  '发展对话': 'Development conversations',
  '情绪与沟通': 'Emotion and communication',
  '员工与岗位': 'Employees and roles',
  '反馈方法': 'Feedback methods',
  '通用管理': 'General management',
  '岗位与职级': 'Roles and levels',
  '组织信息': 'Organization information',
  '绩效管理': 'Performance management',
  '制度红线': 'Policy boundaries',
  '知识资料': 'Knowledge source',
};

export function normalizeKnowledgeScope(scope: string | null | undefined) {
  return String(scope || '')
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_');
}

export function knowledgeCitationIdentity(citation: KnowledgeCitation) {
  const sourceType = citation.source_type === 'skill' ? 'skill' : 'knowledge_base';
  return [
    sourceType,
    citation.chunk_id
      || [citation.source_id || '', citation.title || '', citation.scope || ''].join(':'),
  ].join(':');
}

function targetCitationIdentity(citation: KnowledgeCitation, target: string) {
  const anchors = (citation.anchors || [])
    .filter((anchor) => anchor.target === target)
    .map((anchor) => [
      anchor.highlight_text,
      anchor.source_quote,
      anchor.source_context,
    ].join(':'))
    .sort()
    .join('|');
  return [knowledgeCitationIdentity(citation), target, anchors, citation.quote || ''].join(':');
}

/**
 * Select target-aware citations without maintaining a second, frontend-only
 * allowlist of knowledge scopes. The backend has already validated exact
 * anchors; silently filtering a valid scope here makes evidence disappear.
 */
export function matchingKnowledgeCitations(
  citations: KnowledgeCitation[],
  target: string,
) {
  const seen = new Set<string>();
  return citations.filter((citation) => {
    const targetAnchors = (citation.anchors || []).filter(
      (anchor) => anchor.target === target,
    );
    const hasTarget = Boolean(citation.targets?.includes(target) || targetAnchors.length);
    const hasSourceText = Boolean(
      String(citation.quote || '').trim()
      || targetAnchors.some((anchor) => (
        String(anchor.source_quote || '').trim()
        || String(anchor.source_context || '').trim()
      )),
    );
    if (!hasTarget || !hasSourceText) return false;

    const identity = targetCitationIdentity(citation, target);
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

export interface KnowledgeCitationSource {
  id: string;
  title: string;
  scopeLabel: string;
  sourceTypeLabel: string;
  quotes: string[];
}

export function collectKnowledgeCitationSources(
  citations: KnowledgeCitation[],
): KnowledgeCitationSource[] {
  const sources = new Map<string, KnowledgeCitationSource>();
  for (const citation of citations) {
    const id = knowledgeCitationIdentity(citation);
    const source = sources.get(id) || {
      id,
      title: String(
        citation.title
          || citation.source_id
          || (citation.source_type === 'skill' ? '业务 Skill' : '知识库资料'),
      ).trim(),
      scopeLabel: KNOWLEDGE_SCOPE_LABELS[normalizeKnowledgeScope(citation.scope)]
        || normalizeKnowledgeScope(citation.scope)
        || '知识资料',
      sourceTypeLabel: citation.source_type === 'skill' ? 'Skill' : '知识库',
      quotes: [],
    };
    const candidates = (citation.anchors || []).length
      ? (citation.anchors || []).map((anchor) => anchor.source_quote)
      : [citation.quote || ''];
    for (const candidate of candidates) {
      const quote = String(candidate || '').trim();
      if (quote && !source.quotes.includes(quote)) source.quotes.push(quote);
    }
    sources.set(id, source);
  }
  return [...sources.values()].filter((source) => source.quotes.length);
}

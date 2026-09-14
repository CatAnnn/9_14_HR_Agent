import { useState } from 'react';
import { Download } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { api } from '../../api/client';
import KnowledgeCitationPrintAppendix from '../../components/KnowledgeCitationPrintAppendix';
import KnowledgeLinkedText from '../../components/KnowledgeLinkedText';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '../../components/ui/accordion';
import { useWorkflow } from '../../context/WorkflowContext';
import { useLanguage } from '../../i18n/LanguageContext';
import type {
  GuidancePointGroup,
  GuidanceSectionDraft,
  KnowledgeCitation,
  WorkflowStreamStatus,
} from '../../types/domain';
import { normalizeDisplayText, userFacingErrorMessage } from '../../utils/displayText';

type GuidanceDimensionKey = 'start' | 'emotion' | 'requirement' | 'plan';

const GUIDANCE_DIMENSION_BY_SECTION: Partial<
  Record<GuidanceSectionDraft['key'], GuidanceDimensionKey>
> = {
  opening_suggestion: 'start',
  risk_preview: 'emotion',
  response_strategies: 'requirement',
  safer_phrases: 'plan',
};

function guidancePointPresentation(group: GuidancePointGroup, targetPrefix: string) {
  const normalizedDetails = group.details
    .map((detail, detailIndex) => ({
      text: normalizeDisplayText(detail).trim(),
      target: `${targetPrefix}.details.${detailIndex}`,
    }))
    .filter((detail) => Boolean(detail.text));
  const explicitSummary = typeof group.summary === 'string'
    ? normalizeDisplayText(group.summary).trim()
    : '';
  if (explicitSummary) {
    return {
      lead: explicitSummary,
      leadTarget: `${targetPrefix}.summary`,
      expanded: normalizedDetails,
    };
  }

  const first = normalizedDetails[0]?.text || '—';
  const firstTarget = normalizedDetails[0]?.target || `${targetPrefix}.details.0`;
  const sentenceMatch = first.match(/^([\s\S]*?[。！？!?；;])\s*([\s\S]+)$/);
  const lead = sentenceMatch?.[1]?.trim() || first;
  const firstRemainder = sentenceMatch?.[2]?.trim();
  return {
    lead,
    leadTarget: firstTarget,
    expanded: [
      ...(firstRemainder ? [{ text: firstRemainder, target: firstTarget }] : []),
      ...normalizedDetails.slice(1),
    ],
  };
}

function GuidancePointDisclosure({
  group,
  targetPrefix,
  citations,
}: {
  group: GuidancePointGroup;
  targetPrefix: string;
  citations?: KnowledgeCitation[];
}) {
  const presentation = guidancePointPresentation(group, targetPrefix);
  const hasMore = presentation.expanded.length > 0;

  const lead = (
    <span className="guidance-detail-lead">
      <KnowledgeLinkedText
        text={presentation.lead}
        target={presentation.leadTarget}
        citations={citations}
      />
    </span>
  );

  if (!hasMore) {
    return (
      <section className="guidance-point-group">
        <h4>{normalizeDisplayText(group.title)}</h4>
        {lead}
      </section>
    );
  }

  return (
    <AccordionItem className="guidance-point-group" id={targetPrefix}>
      <h4>{normalizeDisplayText(group.title)}</h4>
      <AccordionTrigger className="guidance-detail-summary">
        {lead}
      </AccordionTrigger>
      <AccordionContent>
        <ul className="guidance-detail-more">
          {presentation.expanded.map((detail, detailIndex) => (
            <li key={`${detailIndex}-${detail.text}`}>
              <KnowledgeLinkedText
                text={detail.text}
                target={detail.target}
                citations={citations}
              />
            </li>
          ))}
        </ul>
      </AccordionContent>
    </AccordionItem>
  );
}

function renderDraftBody(
  section: GuidanceSectionDraft,
  guidanceStatus: WorkflowStreamStatus,
  citations?: KnowledgeCitation[],
  translate: (source: string, english?: string) => string = (source) => source,
) {
  if (section.error) return <p>{normalizeDisplayText(section.error)}</p>;
  if (section.point_groups !== null) {
    const dimension = GUIDANCE_DIMENSION_BY_SECTION[section.key];
    return (
      <Accordion defaultExpandedKeys={[]} className="guidance-point-groups">
        {section.point_groups.map((group, groupIndex) => (
          <GuidancePointDisclosure
            group={group}
            targetPrefix={`dimension_points.${dimension}.${groupIndex}`}
            citations={citations}
            key={`${section.key}-${groupIndex}-${group.title}`}
          />
        ))}
      </Accordion>
    );
  }
  if (section.items !== null) {
    return (
      <ul>
        {section.items.length
          ? section.items.map((item, index) => <li key={`${section.key}-${index}`}>{normalizeDisplayText(item)}</li>)
          : <li>—</li>}
      </ul>
    );
  }
  const active = guidanceStatus === 'streaming' && section.status === 'generating';
  return (
    <p>
      {section.text
        ? normalizeDisplayText(section.text)
        : (section.status === 'error'
          ? translate('该部分生成失败', 'This section failed to generate')
          : translate('正在整理结构化内容', 'Organizing structured content'))}
      {active && (
        <span className="streaming-dots" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
      )}
    </p>
  );
}

export default function GuidanceStep() {
  const navigate = useNavigate();
  const { translate } = useLanguage();
  const { guidanceReport, guidanceSections, guidanceStatus, showToast } = useWorkflow();
  const [exporting, setExporting] = useState(false);
  const guidanceGenerating = guidanceStatus === 'idle' || guidanceStatus === 'streaming';

  const exportGuidanceWord = async () => {
    const sessionId = guidanceReport?.session_id;
    if (!sessionId || exporting) return;
    setExporting(true);
    try {
      const { blob, filename } = await api.downloadGuidanceWord(sessionId);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      showToast('谈前指导 Word 已导出');
    } catch (error) {
      showToast(userFacingErrorMessage(error, undefined, '谈前指导导出失败。'), 'error');
    } finally {
      setExporting(false);
    }
  };

  return (
    <section id="screen-guidance" className="screen active">
      <div className="page-intro guidance-page-intro">
        <h1>{translate('谈前指导', 'Preparation guidance')}</h1>
      </div>
      <div className="briefing-layout">
        <section className="soft-card guidance-card briefing-card">
          <p className="guidance-principle-line">
            {translate(
              '通过明确标准、尊重倾听和共同规划，把绩效反馈转化为员工成长与公司发展的共同动力',
              'Turn performance feedback into shared momentum for employee growth and organizational development through clear standards, respectful listening, and joint planning.',
            )}
          </p>
          <div className="guidance-list">
            {guidanceSections.map((section) => (
              <section className={`guidance-section ${section.key === 'risk_preview' ? 'risk-section' : ''}`} key={section.key}>
                <div>
                  <div className="stream-section-title">
                    <h3>{normalizeDisplayText(section.title)}</h3>
                    {section.status === 'error' && (
                      <span className="status-badge danger">
                        {translate('失败', 'Failed')}
                      </span>
                    )}
                  </div>
                  {renderDraftBody(
                    section,
                    guidanceStatus,
                    guidanceReport?.citations,
                    translate,
                  )}
                  {section.status === 'generating' && !section.text && section.items === null && section.point_groups === null && <div className="skeleton-line" />}
                </div>
              </section>
            ))}
          </div>
          <div className="guidance-card-footer">
            <div className="guidance-footer-note">
              {guidanceReport?.disclaimer && <p className="guidance-disclaimer">{normalizeDisplayText(guidanceReport.disclaimer)}</p>}
              {guidanceReport?.session_id && (
                <button
                  className="guidance-export-button"
                  type="button"
                  onClick={exportGuidanceWord}
                  disabled={exporting}
                  aria-busy={exporting}
                  aria-label={exporting
                    ? translate('正在导出谈前指导 Word 文档', 'Exporting preparation-guidance Word document')
                    : translate('导出谈前指导 Word 文档', 'Export preparation-guidance Word document')}
                  title={translate('导出谈前指导 Word 文档', 'Export preparation-guidance Word document')}
                >
                  <Download size={15} aria-hidden="true" />
                  <span>{translate('下载', 'Download')}</span>
                </button>
              )}
            </div>
            <button className="btn btn-primary guidance-start-button" onClick={() => navigate('/app/rehearsal')} disabled={guidanceStatus !== 'ready'}>
              {guidanceGenerating
                ? translate('生成中…', 'Generating…')
                : translate('开始预演', 'Start rehearsal')}
            </button>
          </div>
        </section>
        <KnowledgeCitationPrintAppendix citations={guidanceReport?.citations} />
      </div>
    </section>
  );
}
